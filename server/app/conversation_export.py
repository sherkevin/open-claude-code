"""Export the user's conversation and the evidence they can still access."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .db import now
from .security import AppError, safe_name
from .service import approval_view, conversation_view, file_view, message_view


TOOL_EVENT_TYPES = ("tool.started", "tool.completed", "tool.discarded")
TOOL_FIELDS = ("tool_call_id", "tool_name", "success", "duration_ms", "error_code")
APPROVAL_FIELDS = ("id", "tool_name", "status", "created_at", "decided_at", "decision_source", "argument_digest")
RUN_FIELDS = ("id", "message_id", "assistant_message_id", "kind", "status", "error_code", "created_at", "completed_at")
SOURCE_LINK = re.compile(r"\[([^\]\n]*)\]\(/api/v1/sources/([A-Za-z0-9_-]+)\)")


def _fields(value, keys):
    return {key: value.get(key) for key in keys}


def _snapshot(service, conversation_id, user):
    db = service.db
    with db.lock:
        conversation = conversation_view(service.require("conversations", conversation_id, user))
        messages = [message_view(row) for row in db.all(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at,rowid", (conversation_id,)
        )]
        runs, visible_sources = [], {}
        for row in db.all("SELECT * FROM runs WHERE conversation_id=? AND user_id=? ORDER BY created_at,rowid",
                          (conversation_id, user["id"])):
            run_id = row["id"]
            sources = service.evidence.list(run_id, user)
            visible_sources[run_id] = {source["id"] for source in sources}
            approvals = db.all("SELECT * FROM approvals WHERE run_id=? ORDER BY created_at,rowid", (run_id,))
            events = db.all("SELECT id,type,data,created_at FROM events WHERE run_id=? AND type IN (?,?,?) ORDER BY id",
                            (run_id, *TOOL_EVENT_TYPES))
            tools = []
            for event in events:
                data = json.loads(event["data"])
                tools.append({**_fields(event, ("id", "type", "created_at")), **_fields(data, TOOL_FIELDS)})
            runs.append({
                **_fields(row, RUN_FIELDS), **service.public_run_model(row),
                "approvals": [_fields(approval_view(approval), APPROVAL_FIELDS) for approval in approvals],
                "tools": tools, "sources": sources,
            })
        for message in messages:
            message["citations"] = [citation for citation in message["citations"]
                                    if isinstance(citation, dict) and citation.get("id") in visible_sources.get(message["run_id"], ())]
        files = []
        for file_id in dict.fromkeys(file_id for message in messages for file_id in message["file_ids"]):
            try:
                row = service.require("files", file_id, user)
            except AppError:
                continue
            if row["conversation_id"] in (None, conversation_id):
                files.append(file_view(row))
    return {"schema": "conversation-export/v1", "exported_at": now(), "conversation": conversation,
            "messages": messages, "runs": runs, "attachments": files}


def _markdown(value):
    def inline(text):
        return re.sub(r"([\\\[\]<>*_`])", r"\\\1", " ".join(str(text).split()))

    header = f"# {inline(value['conversation']['title'])}"
    lines = [header, "", f"导出时间 {value['exported_at']}",
             "记录包含全部消息、附件索引、可访问的来源，以及工具和审批摘要。附件原件以 ZIP 内实际包含的文件为准。", ""]
    file_map = {file["id"]: file for file in value["attachments"]}
    source_map = {source["id"]: source for run in value["runs"] for source in run["sources"]}
    for message in value["messages"]:
        label = {"user": "用户", "assistant": "助手"}.get(message["role"], message["role"])
        lines.extend([f"## {inline(label)} · {message['created_at']}", ""])
        text = SOURCE_LINK.sub(lambda match: (
            f"[{match[1]}](#source-{match[2]})" if match[2] in source_map else "（来源不可导出）"
        ), message["text"])
        lines.extend([text, ""] if text else [])
        for file_id in message["file_ids"]:
            file = file_map.get(file_id)
            if file:
                name = inline(file["name"])
                if file.get("archive_path"):
                    name = f"[{name}]({quote(file['archive_path'])})"
                lines.append(f"- 附件 {name}")
        lines.append("")
    if value["runs"]:
        lines.extend(["## 工具和审批记录", ""])
        for run in value["runs"]:
            lines.extend([f"### {run['created_at']} · {inline(run['model'])} · {inline(run['status'])}", ""])
            for event in run["tools"]:
                outcome = "开始"
                if event["type"] != "tool.started":
                    outcome = "成功" if event["success"] is True else "失败"
                lines.append(f"- 工具 {inline(event['tool_name'])} · {outcome}")
            for approval in run["approvals"]:
                lines.append(f"- 审批 {inline(approval['tool_name'])} · {inline(approval['status'])}")
            lines.append("")
    if source_map:
        lines.extend(["## 来源", ""])
        for source in source_map.values():
            lines.extend([f"### Source {source['id']}", "", inline(source["title"]), "", source["excerpt"] or "", ""])
            if source["original_url"]:
                lines.extend([source["original_url"], ""])
    return "\n".join(lines)


def _archive_attachment(archive, service, user, conversation_id, file, position):
    try:
        row = service.require("files", file["id"], user)
        path = Path(row["path"])
        directory = service.settings.data_dir / "files"
        if (row["conversation_id"] not in (None, conversation_id) or path.is_symlink()
                or path.resolve().parent != directory.resolve() or not path.is_file()):
            return
        name = f"attachments/{position:04d}-{safe_name(file['name'])}"
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as source:
            value = os.fstat(source.fileno())
            if not stat.S_ISREG(value.st_mode) or value.st_size != file["size"]:
                return
            with archive.open(name, "w", force_zip64=True) as target:
                shutil.copyfileobj(source, target)
        file["archive_path"] = name
    except (AppError, FileNotFoundError):
        return


def create_conversation_export_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

    @router.get("/{item_id}/export")
    def conversation_export(item_id: str, format: Literal["markdown", "zip"] = "zip",
                            user=Depends(authenticated), service=Depends(svc)):
        value = _snapshot(service, item_id, user)
        headers = {"Cache-Control": "private, no-store"}
        if format == "markdown":
            return JSONResponse({"markdown": _markdown(value)}, headers=headers)

        output = tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024)
        try:
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for position, file in enumerate(value["attachments"], 1):
                    _archive_attachment(archive, service, user, item_id, file, position)
                archive.writestr("conversation.md", _markdown(value))
                archive.writestr("conversation.json", json.dumps(value, ensure_ascii=False, indent=2))
            headers["Content-Length"] = str(output.tell())
            headers["Content-Disposition"] = f'attachment; filename="conversation-{safe_name(item_id)}.zip"'
            output.seek(0)
        except BaseException:
            output.close()
            raise
        return StreamingResponse(iter(lambda: output.read(1024 * 1024), b""), media_type="application/zip",
                                 headers=headers, background=BackgroundTask(output.close))

    return router
