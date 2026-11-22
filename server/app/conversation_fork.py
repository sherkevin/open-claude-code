"""Fork a completed Harness turn into a new conversation with its own runtime."""
import hashlib
import json
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .db import dump, offload, uid
from .security import AppError
from .service import conversation_view


class ForkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=16, max_length=128)


def fork_boundary(service, conversation_id, body, user):
    with service.db.lock:
        parent = service.require("conversations", conversation_id, user)
        service.usable_assistant(parent["assistant_id"], user)
        if parent["runtime_type"] != "harness" or not parent["harness_session_id"]:
            raise AppError(409, "FORK_NOT_AVAILABLE", "此会话没有可分支的 Harness 原生历史")
        child_id = hashlib.sha256(dump(["conversation-fork", user["id"], conversation_id,
                                       body.message_id, body.client_request_id]).encode()).hexdigest()[:32]
        existing = service.db.one("SELECT * FROM conversations WHERE id=? AND user_id=?", (child_id, user["id"]))
        if existing:
            return child_id, conversation_view(existing, user), None
        if parent["active_run_id"]:
            raise AppError(409, "CONVERSATION_BUSY", "请等待当前任务结束，再创建分支")
        boundary = service.db.one("""
            SELECT json_extract(e.data,'$.seq') AS seq FROM runs r JOIN events e ON e.run_id=r.id
            WHERE r.conversation_id=? AND r.user_id=? AND r.assistant_message_id=? AND r.status='succeeded'
                AND r.kind='chat' AND e.type='harness.event' AND json_extract(e.data,'$.type')='turn/end'
                AND json_extract(r.execution_meta,'$.native_epoch')=?
            ORDER BY e.id DESC LIMIT 1
        """, (conversation_id, user["id"], body.message_id, parent["native_epoch"]))
        if not boundary or type(boundary["seq"]) is not int or not 0 <= boundary["seq"] < 100_000:
            raise AppError(404, "FORK_BOUNDARY_NOT_FOUND", "该消息没有可分支的已完成原生轮次")
        return child_id, None, boundary["seq"]


def fork_home(service, child_id):
    return service.settings.data_dir / "harness" / child_id


def _attachment(service, file_id, user, parent_id, child_id, links):
    try:
        row = service.require("files", file_id, user)
    except AppError:
        return None
    if row["conversation_id"] is None:
        return file_id
    if row["conversation_id"] != parent_id:
        return None
    source = Path(row["path"])
    directory = service.settings.data_dir / "files"
    if source.is_symlink() or not source.is_file() or source.resolve().parent != directory.resolve():
        return None
    new_id = uid()
    destination = directory / new_id
    os.link(source, destination, follow_symlinks=False)
    links.append(destination)
    service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)",
                       (new_id, user["id"], row["name"], row["mime_type"], row["size"], str(destination), row["created_at"], child_id))
    return new_id


def save_fork(service, parent_id, child_id, body, user):
    from .harness_attachments import MAX_HISTORY_FILES, MAX_HISTORY_RUNS, _binding, fork_attachment_receipts

    new_links = []
    try:
        with service.db.transaction():
            parent = service.require("conversations", parent_id, user)
            service.usable_assistant(parent["assistant_id"], user)
            if parent["active_run_id"]:
                raise AppError(409, "CONVERSATION_BUSY", "请等待当前任务结束，再创建分支")
            receipts = {item["id"]: item for item in fork_attachment_receipts(service, parent_id, parent["native_epoch"])}
            runs = service.db.all("""
                SELECT r.execution_meta FROM runs r JOIN runs boundary ON boundary.assistant_message_id=?
                WHERE r.conversation_id=? AND r.rowid<=boundary.rowid
                    AND json_extract(r.execution_meta,'$.native_epoch')=?
                    AND json_type(r.execution_meta,'$.harness_attachments')='array'
                LIMIT ?
            """, (body.message_id, parent_id, parent["native_epoch"], MAX_HISTORY_RUNS + 1))
            if len(runs) > MAX_HISTORY_RUNS:
                raise AppError(409, "FORK_ATTACHMENTS_LIMIT", "历史附件过多，无法安全创建分支")
            for run in runs:
                for item in json.loads(run["execution_meta"])["harness_attachments"]:
                    if receipts.setdefault(item["id"], item) != item:
                        raise AppError(409, "FORK_ATTACHMENT_CHANGED", "历史附件已经变更，无法创建分支")
            if len(receipts) > MAX_HISTORY_FILES:
                raise AppError(409, "FORK_ATTACHMENTS_LIMIT", "历史附件过多，无法安全创建分支")
            for item in receipts.values():
                row = service.require("files", item["id"], user)
                if row["conversation_id"] is not None or _binding(row) != item["binding"]:
                    raise AppError(409, "FORK_ATTACHMENT_CHANGED", "历史附件已变更或仅限源会话使用")
            settings = {"assistant_id": parent["assistant_id"], "title": parent["title"][:194] + " · 分支",
                        "category": parent["category"], "tags": json.loads(parent["tags"]),
                        "model_id": parent["model_id"], "reasoning_level": parent["reasoning_level"]}
            service._create_conversation(settings, user, item_id=child_id, runtime_type="harness")
            source_rows = service.db.all("""
                SELECT m.* FROM messages m JOIN messages boundary ON boundary.id=? AND boundary.conversation_id=m.conversation_id
                WHERE m.conversation_id=? AND (m.created_at,m.rowid)<=(boundary.created_at,boundary.rowid)
                ORDER BY m.created_at,m.rowid
            """, (body.message_id, parent_id))
            attachments, sources = {}, {}
            for row in source_rows:
                files = []
                for file_id in json.loads(row["file_ids"]):
                    if file_id not in attachments:
                        attachments[file_id] = _attachment(service, file_id, user, parent_id, child_id, new_links)
                    if attachments[file_id]:
                        files.append(attachments[file_id])
                citations = json.loads(row.get("citations") or "[]")
                if citations and row["run_id"] and row["run_id"] not in sources:
                    sources[row["run_id"]] = {source["id"] for source in service.evidence.list(row["run_id"], user)}
                allowed = sources.get(row["run_id"], ())
                citations = [citation for citation in citations if isinstance(citation, dict) and citation.get("id") in allowed]
                copied_id = uid()
                service.db.execute("INSERT INTO messages(id,conversation_id,role,text,file_ids,created_at,skills,citations) VALUES(?,?,?,?,?,?,?,?)",
                                   (copied_id, child_id, row["role"], row["text"], dump(files), row["created_at"], row.get("skills") or "[]", dump(citations)))
                if row["id"] == body.message_id:
                    service.db.execute("INSERT INTO conversation_forks VALUES(?,?)", (child_id, copied_id))
            # Initialize the child's empty snapshot before binding its seeded
            # session. Legacy snapshot initialization resets existing bindings.
            service.memory.initialize(user["id"], child_id)
            service.db.execute("UPDATE conversations SET harness_session_id=? WHERE id=?", (f"workspace-{child_id}", child_id))
            if receipts:
                child = service.require("conversations", child_id, user)
                service.db.execute("INSERT INTO conversation_fork_attachments VALUES(?,?,?)",
                                   (child_id, child["native_epoch"], dump(list(receipts.values()))))
            return conversation_view(service.require("conversations", child_id, user), user)
    except BaseException:
        for path in new_links:
            path.unlink(missing_ok=True)
        raise


def clear_fork_home(service, child_id):
    shutil.rmtree(fork_home(service, child_id), ignore_errors=True)


def create_conversation_fork_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

    @router.post("/{item_id}/fork", status_code=201)
    async def fork(item_id: str, body: ForkRequest, user=Depends(authenticated), service=Depends(svc)):
        child, existing, boundary = await offload(fork_boundary, service, item_id, body, user)
        if existing:
            return existing
        return await service.manager.fork(item_id, child, boundary, body, user)

    return router
