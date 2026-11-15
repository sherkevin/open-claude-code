"""Private, durable FIFO drafts. Dispatch reuses the normal run idempotency key.

The open client advances the queue; closing it never implies background work.
Only successful predecessor runs advance automatically. Unknown/failed/stopped
outcomes require an explicit continuation, never replay of an accepted run.
"""
import json

from fastapi import APIRouter, Depends
from pydantic import Field

from .db import dump, now, offload_write, shielded
from .models import InputModel, SendMessage
from .security import AppError


class QueueEdit(InputModel):
    revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=50000)


class QueueRevision(InputModel):
    revision: int = Field(ge=1)


class QueueDispatch(QueueRevision):
    after_run_id: str | None = Field(default=None, max_length=100)
    resume: bool = False


class MessageQueue:
    def __init__(self, service):
        self.service, self.db = service, service.db

    def _authorize(self, conversation_id, user):
        conversation = self.service.require("conversations", conversation_id, user)
        if not self.service.require("users", user["id"])["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        return conversation

    def _reconcile(self, conversation_id):
        # A process interruption after prepare_run committed but before this
        # queue row changed is resolved by the durable run key, not a replay.
        self.db.execute("""UPDATE message_queue SET status='accepted',run_id=(
            SELECT id FROM runs WHERE runs.conversation_id=message_queue.conversation_id
            AND runs.client_request_id=message_queue.id),error=NULL
            WHERE conversation_id=? AND status='pending' AND EXISTS (
            SELECT 1 FROM runs WHERE runs.conversation_id=message_queue.conversation_id
            AND runs.client_request_id=message_queue.id)""", (conversation_id,))

    def snapshot(self, conversation_id, user):
        with self.db.lock:
            conversation = self._authorize(conversation_id, user)
            self._reconcile(conversation_id)
            rows = self.db.all("SELECT * FROM message_queue WHERE conversation_id=? AND status='pending' ORDER BY sequence", (conversation_id,))
            latest = self.db.one("SELECT id,status FROM runs WHERE conversation_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (conversation_id,))
            return {"conversation_id": conversation_id, "items": [self.view(row) for row in rows],
                    "active_run_id": conversation["active_run_id"], "latest_run": latest,
                    "paused": bool(rows and (rows[0]["error"] or latest and latest["status"] in {"failed", "cancelled", "interrupted"})),
                    "archived": bool(conversation["archived"])}

    @staticmethod
    def view(row):
        payload = json.loads(row["payload"])
        return {"id": row["id"], "revision": row["revision"], "status": row["status"],
                "run_id": row["run_id"], "error": row["error"], "created_at": row["created_at"],
                **{key: payload.get(key) for key in ("text", "file_ids", "skill_ids", "model_id", "reasoning_level")}}

    def add(self, conversation_id, payload, user):
        self.service.require_trial_fields(auto_route=payload.get("auto_route"), file_ids=payload.get("file_ids"), skill_ids=payload.get("skill_ids"))
        with self.db.transaction():
            conversation = self._authorize(conversation_id, user)
            if conversation["archived"]:
                raise AppError(409, "CONVERSATION_ARCHIVED", "请先恢复此对话")
            row = self.db.one("SELECT * FROM message_queue WHERE conversation_id=? AND id=?", (conversation_id, payload["client_request_id"]))
            if row:
                # The first accepted draft wins, including a later edit/cancel.
                return self.view(row)
            if self.db.one("SELECT id FROM runs WHERE conversation_id=? AND client_request_id=?", (conversation_id, payload["client_request_id"])):
                raise AppError(409, "QUEUE_ID_USED", "该消息已发送，请刷新会话")
            if not payload["text"].strip() and not payload["file_ids"] and not payload.get("skill_ids"):
                raise AppError(422, "EMPTY_MESSAGE", "请输入消息、选择 Skill 或添加附件")
            if self.db.one("SELECT COUNT(*) n FROM message_queue WHERE conversation_id=? AND status='pending'", (conversation_id,))["n"] >= 20:
                raise AppError(409, "QUEUE_FULL", "最多保留20条待发消息，请先发送或取消部分消息")
            for file_id in payload["file_ids"]:
                self.service.require("files", file_id, user)
            timestamp = now()
            self.db.execute("INSERT INTO message_queue(id,conversation_id,payload,created_at,updated_at) VALUES(?,?,?,?,?)", (payload["client_request_id"], conversation_id, dump(payload), timestamp, timestamp))
            return self.view(self.db.one("SELECT * FROM message_queue WHERE conversation_id=? AND id=?", (conversation_id, payload["client_request_id"])))

    def change(self, conversation_id, item_id, revision, user, text=None):
        with self.db.transaction():
            self._authorize(conversation_id, user)
            self._reconcile(conversation_id)
            row = self.db.one("SELECT * FROM message_queue WHERE conversation_id=? AND id=?", (conversation_id, item_id))
            if not row:
                raise AppError(404, "NOT_FOUND", "待发消息不存在")
            if row["status"] != "pending" or row["revision"] != revision:
                raise AppError(409, "QUEUE_CHANGED", "待发消息已发送、取消或被修改，请刷新后再操作")
            if text is None:
                self.db.execute("UPDATE message_queue SET status='cancelled',revision=revision+1,updated_at=? WHERE sequence=?", (now(), row["sequence"]))
            else:
                if not text.strip():
                    raise AppError(422, "EMPTY_MESSAGE", "请输入消息内容")
                payload = json.loads(row["payload"])
                payload["text"] = text
                self.db.execute("UPDATE message_queue SET payload=?,revision=revision+1,error=NULL,updated_at=? WHERE sequence=?", (dump(payload), now(), row["sequence"]))
        return self.snapshot(conversation_id, user)

    def dispatch(self, conversation_id, item_id, data, user):
        # The application supports one server process. Its shared re-entrant DB
        # lock keeps head selection, run preparation and acceptance ordered.
        # prepare_run owns its transaction; do not nest a second BEGIN here.
        with self.db.lock:
            state = self.snapshot(conversation_id, user)
            row = self.db.one("SELECT * FROM message_queue WHERE conversation_id=? AND id=?", (conversation_id, item_id))
            if not row:
                raise AppError(404, "NOT_FOUND", "待发消息不存在")
            if row["status"] == "accepted":
                return {"status": "accepted", "run_id": row["run_id"], "queue": state}
            if row["status"] != "pending" or not state["items"] or state["items"][0]["id"] != item_id or row["revision"] != data["revision"]:
                return {"status": "changed", "queue": state}
            latest = state["latest_run"]
            if state["archived"] or state["active_run_id"] or (latest["id"] if latest else None) != data.get("after_run_id"):
                return {"status": "waiting", "queue": state}
            if (row["error"] or latest and latest["status"] != "succeeded") and not data["resume"]:
                return {"status": "paused", "queue": state}
            try:
                result, _created = self.service.prepare_run(conversation_id, json.loads(row["payload"]), user)
            except AppError as exc:
                self.db.execute("UPDATE message_queue SET error=?,updated_at=? WHERE sequence=?", (exc.message, now(), row["sequence"]))
                return {"status": "paused", "queue": self.snapshot(conversation_id, user)}
            self.db.execute("UPDATE message_queue SET status='accepted',run_id=?,error=NULL,updated_at=? WHERE sequence=?", (result["run_id"], now(), row["sequence"]))
            return {"status": "accepted", **result, "queue": self.snapshot(conversation_id, user)}


def create_message_queue_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/conversations/{item_id}/queue")

    @router.get("")
    def read(item_id: str, user=Depends(authenticated), service=Depends(svc)):
        return MessageQueue(service).snapshot(item_id, user)

    @router.post("", status_code=201)
    def add(item_id: str, body: SendMessage, user=Depends(authenticated), service=Depends(svc)):
        return MessageQueue(service).add(item_id, body.model_dump(), user)

    @router.patch("/{queue_id}")
    def edit(item_id: str, queue_id: str, body: QueueEdit, user=Depends(authenticated), service=Depends(svc)):
        return MessageQueue(service).change(item_id, queue_id, body.revision, user, body.text)

    @router.post("/{queue_id}/cancel")
    def cancel(item_id: str, queue_id: str, body: QueueRevision, user=Depends(authenticated), service=Depends(svc)):
        return MessageQueue(service).change(item_id, queue_id, body.revision, user)

    @router.post("/{queue_id}/dispatch")
    async def dispatch(item_id: str, queue_id: str, body: QueueDispatch, user=Depends(authenticated), service=Depends(svc)):
        async def start():
            result = await offload_write(MessageQueue(service).dispatch, item_id, queue_id, body.model_dump(), user)
            if result.get("run_id"):
                await service.manager.ensure_submitted(result["run_id"])
            return result
        return await shielded(start())

    return router
