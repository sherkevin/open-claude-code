"""Private, durable FIFO drafts with host-owned dispatch and recovery."""
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
        if payload.get("recovery_run_id"):
            raise AppError(422, "QUEUE_RECOVERY_UNSUPPORTED", "请打开未完成的任务，直接选择恢复")
        self.service.require_trial_fields(auto_route=payload.get("auto_route"), file_ids=payload.get("file_ids"), skill_ids=payload.get("skill_ids"))
        if payload["client_request_id"].startswith("schedule-"):
            raise AppError(409, "SCHEDULE_NOT_DUE", "此任务由计划队列启动")
        if self.service.prompt_safety.enabled:
            self._authorize(conversation_id, user)
            prior = self.db.one("SELECT id FROM message_queue WHERE conversation_id=? AND id=?", (conversation_id, payload["client_request_id"]))
            if not prior:
                self.service.prompt_safety.require_allowed(payload["text"], user["id"])
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
        if text is not None and self.service.prompt_safety.enabled:
            self._authorize(conversation_id, user)
            self.service.prompt_safety.require_allowed(text, user["id"])
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

    def _dispatch_candidate(self, conversation_id, item_id, data, user):
        state = self.snapshot(conversation_id, user)
        row = self.db.one("SELECT * FROM message_queue WHERE conversation_id=? AND id=?", (conversation_id, item_id))
        if not row:
            raise AppError(404, "NOT_FOUND", "待发消息不存在")
        if row["status"] == "accepted":
            return row, {"status": "accepted", "run_id": row["run_id"], "queue": state}
        if row["status"] != "pending" or not state["items"] or state["items"][0]["id"] != item_id or row["revision"] != data["revision"]:
            return row, {"status": "changed", "queue": state}
        latest = state["latest_run"]
        if state["archived"] or state["active_run_id"] or (latest["id"] if latest else None) != data.get("after_run_id"):
            return row, {"status": "waiting", "queue": state}
        if (row["error"] or latest and latest["status"] != "succeeded") and not data["resume"]:
            return row, {"status": "paused", "queue": state}
        return row, None

    def dispatch(self, conversation_id, item_id, data, user):
        rejection = None
        if self.service.prompt_safety.enabled:
            with self.db.lock:
                row, response = self._dispatch_candidate(conversation_id, item_id, data, user)
            if response:
                return response
            try:
                self.service.prompt_safety.require_allowed(json.loads(row["payload"])["text"], user["id"])
            except AppError as exc:
                rejection = exc

        # Recheck the queue head and immutable draft revision after review.
        # Never hold the database lock while waiting for the classifier.
        with self.db.lock:
            row, response = self._dispatch_candidate(conversation_id, item_id, data, user)
            if response:
                return response
            try:
                if rejection:
                    raise rejection
                result, _created = self.service.prepare_run(conversation_id, json.loads(row["payload"]), user,
                                                            queue_sequence=row["sequence"])
            except AppError as exc:
                if exc.code in {"CONTEXT_BUSY", "MEMORY_BUSY", "CONVERSATION_BUSY"}:
                    return {"status": "waiting", "queue": self.snapshot(conversation_id, user)}
                self.db.execute("UPDATE message_queue SET error=?,updated_at=? WHERE sequence=?", (exc.message, now(), row["sequence"]))
                return {"status": "paused", "queue": self.snapshot(conversation_id, user)}
            return {"status": "accepted", **result, "queue": self.snapshot(conversation_id, user)}

    def ready(self):
        return self.db.all("""SELECT q.conversation_id, q.id, q.revision, r.id AS after_run_id, c.user_id
            FROM message_queue q JOIN conversations c ON c.id=q.conversation_id
            JOIN users u ON u.id=c.user_id AND u.enabled=1
            LEFT JOIN runs r ON r.rowid=(SELECT rr.rowid FROM runs rr WHERE rr.conversation_id=c.id
                ORDER BY rr.created_at DESC,rr.rowid DESC LIMIT 1)
            WHERE q.status='pending' AND q.error IS NULL AND c.archived=0 AND c.active_run_id IS NULL
            AND (r.id IS NULL OR r.status='succeeded') AND q.sequence=(SELECT MIN(qq.sequence) FROM message_queue qq
                WHERE qq.conversation_id=c.id AND qq.status='pending') ORDER BY q.sequence LIMIT 50""")

    def advance(self, candidate):
        user = self.service.require("users", candidate["user_id"])
        result = self.dispatch(candidate["conversation_id"], candidate["id"], {
            "revision": candidate["revision"], "after_run_id": candidate["after_run_id"], "resume": False,
        }, user)
        return result.get("run_id")


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
