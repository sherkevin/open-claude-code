"""Persistent, one-time, text-only scheduled conversation runs."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, Field

from .db import dump, now, uid
from .models import InputModel, SendMessage
from .security import AppError


class ScheduleCreate(InputModel):
    text: str = Field(min_length=1, max_length=50000)
    scheduled_at: AwareDatetime


def enqueue_due_schedules(service):
    db = service.db
    for due in db.all("SELECT id FROM conversation_schedules WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at,id LIMIT 20", (now(),)):
        with db.transaction():
            row = db.one("SELECT * FROM conversation_schedules WHERE id=? AND status='scheduled'", (due["id"],))
            if row is None:
                continue
            conversation_id = row["conversation_id"]
            try:
                conversation = service.require("conversations", conversation_id)
                user = service.require("users", conversation["user_id"])
                if not user["enabled"] or conversation["archived"]:
                    raise AppError(409, "SCHEDULE_UNAVAILABLE", "会话已归档或账号已停用")
                service.usable_assistant(conversation["assistant_id"], user)
            except AppError as error:
                db.execute("UPDATE conversation_schedules SET status='failed',error=? WHERE id=?", (error.message, row["id"]))
                continue
            if db.one("SELECT COUNT(*) AS n FROM message_queue WHERE conversation_id=? AND status='pending'", (conversation_id,))["n"] >= 20:
                db.execute("UPDATE conversation_schedules SET status='failed',error=? WHERE id=?", ("到期时会话待发队列已满", row["id"]))
                continue
            timestamp = now()
            payload = SendMessage(client_request_id=row["id"], text=row["text"], auto_route=False).model_dump()
            db.execute("INSERT INTO message_queue(id,conversation_id,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (row["id"], conversation_id, dump(payload), timestamp, timestamp))
            db.execute("UPDATE conversation_schedules SET status='queued' WHERE id=?", (row["id"],))


def create_schedule_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/conversations/{conversation_id}/schedules")

    @router.get("")
    def list_schedules(conversation_id: str, service=Depends(svc), user=Depends(authenticated)):
        service.require("conversations", conversation_id, user)
        return service.db.all("""SELECT s.id,s.text,s.scheduled_at,COALESCE(r.status,q.status,s.status) AS status,
            COALESCE(r.error,q.error,s.error) AS error,r.id AS run_id FROM conversation_schedules s
            LEFT JOIN message_queue q ON q.conversation_id=s.conversation_id AND q.id=s.id
            LEFT JOIN runs r ON r.id=q.run_id WHERE s.conversation_id=? ORDER BY s.created_at DESC,s.id DESC LIMIT 100""",
            (conversation_id,))

    @router.post("", status_code=201)
    def create_schedule(conversation_id: str, body: ScheduleCreate, service=Depends(svc), user=Depends(authenticated)):
        stamp = body.scheduled_at.astimezone(timezone.utc)
        current = datetime.now(timezone.utc)
        if not body.text.strip() or not current < stamp <= current + timedelta(days=365):
            raise AppError(422, "SCHEDULE_INVALID", "请输入任务内容，并选择未来一年内的执行时间")
        if service.prompt_safety.enabled:
            service.require("conversations", conversation_id, user)
            service.prompt_safety.require_allowed(body.text, user["id"])
        identity = f"schedule-{uid()}"
        with service.db.transaction():
            conversation = service.require("conversations", conversation_id, user)
            service.usable_assistant(conversation["assistant_id"], user)
            if conversation["archived"]:
                raise AppError(409, "CONVERSATION_ARCHIVED", "请先恢复此对话")
            if service.db.one("""SELECT COUNT(*) AS n FROM conversation_schedules s JOIN conversations c ON c.id=s.conversation_id
                LEFT JOIN message_queue q ON q.conversation_id=s.conversation_id AND q.id=s.id
                WHERE c.user_id=? AND (s.status='scheduled' OR q.status='pending')""", (user["id"],))["n"] >= 20:
                raise AppError(409, "SCHEDULE_LIMIT", "最多保留 20 项未执行的计划任务")
            service.db.execute("INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at) VALUES(?,?,?,?,'scheduled',?)",
                               (identity, conversation_id, body.text.strip(), stamp.isoformat(), now()))
        return {"id": identity, "status": "scheduled"}

    @router.delete("/{identity}", status_code=204)
    def cancel_schedule(conversation_id: str, identity: str, service=Depends(svc), user=Depends(authenticated)):
        with service.db.transaction():
            service.require("conversations", conversation_id, user)
            row = service.db.one("SELECT status FROM conversation_schedules WHERE id=? AND conversation_id=?", (identity, conversation_id))
            if row is None:
                raise AppError(404, "NOT_FOUND", "计划任务不存在")
            if row["status"] == "cancelled":
                return
            if row["status"] == "queued":
                changed = service.db.execute("UPDATE message_queue SET status='cancelled',revision=revision+1,updated_at=? WHERE id=? AND conversation_id=? AND status='pending'",
                                             (now(), identity, conversation_id))
                if not changed.rowcount:
                    raise AppError(409, "SCHEDULE_STARTED", "任务已启动，请在会话里操作")
            elif row["status"] != "scheduled":
                raise AppError(409, "SCHEDULE_INACTIVE", "该计划任务已结束")
            service.db.execute("UPDATE conversation_schedules SET status='cancelled' WHERE id=?", (identity,))

    return router
