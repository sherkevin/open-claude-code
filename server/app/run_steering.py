"""Durable admission and delivery receipts for current-turn text steering."""
from pydantic import Field

from .db import now, offload_write, shielded, uid
from .models import InputModel
from .security import AppError


class SteerTurn(InputModel):
    client_request_id: str = Field(min_length=8, max_length=100)
    text: str = Field(min_length=1, max_length=4000)


def view(row):
    return {key: row[key] for key in ("id", "text", "status", "created_at")}


def list_steers(service, run_id, user):
    service.require("runs", run_id, user)
    return [view(row) for row in service.db.all("SELECT * FROM run_steers WHERE run_id=? ORDER BY created_at,id", (run_id,))]


def reserve(service, run_id, body, user):
    text = body.text.strip()
    if not text:
        raise AppError(422, "EMPTY_MESSAGE", "请输入追加指令")
    with service.db.transaction():
        run = service.require("runs", run_id, user)
        conversation = service.require("conversations", run["conversation_id"], user)
        owner = service.require("users", user["id"])
        if not owner["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        service.usable_assistant(conversation["assistant_id"], owner)
        existing = service.db.one("SELECT * FROM run_steers WHERE run_id=? AND request_id=?", (run_id, body.client_request_id))
        if existing:
            if existing["text"] != text:
                raise AppError(409, "REQUEST_CHANGED", "该请求已用于不同的追加指令")
            return run, existing, False
        if (conversation["runtime_type"] != "harness" or conversation["archived"]
                or run.get("kind", "chat") != "chat" or run["status"] != "running"
                or conversation["active_run_id"] != run_id):
            raise AppError(409, "STEER_UNAVAILABLE", "当前轮次暂时无法接收追加指令，可保留文字或发送到下一轮")
        limit = service.db.one("SELECT COUNT(*) AS n FROM run_steers WHERE run_id=?", (run_id,))["n"]
        if limit >= 10:
            raise AppError(409, "STEER_LIMIT", "本轮追加次数已达上限，可发送到下一轮")
        identity, timestamp = uid(), now()
        service.db.execute("INSERT INTO run_steers VALUES(?,?,?,?,?,?,?)",
                           (identity, run_id, body.client_request_id, text, "pending", timestamp, timestamp))
        return run, service.db.one("SELECT * FROM run_steers WHERE id=?", (identity,)), True


def settle(service, run_id, identity, status):
    with service.db.transaction():
        allowed = ["pending"]
        if status != "queued":
            allowed.append("queued")
        if status == "applied":
            allowed.append("uncertain")
        changed = service.db.execute(
            f"UPDATE run_steers SET status=?,updated_at=? WHERE run_id=? AND id=? AND status IN ({','.join('?' for _ in allowed)})",
            (status, now(), run_id, identity, *allowed),
        )
        row = service.db.one("SELECT * FROM run_steers WHERE run_id=? AND id=?", (run_id, identity))
        if row and changed.rowcount:
            if status == "applied":
                from .service import message_view

                run = service.require("runs", run_id)
                stamp = now()
                service.db.execute("INSERT INTO messages(id,conversation_id,role,text,file_ids,run_id,created_at) VALUES(?,?,'user',?,'[]',?,?)",
                                   (identity, run["conversation_id"], row["text"], run_id, stamp))
                service.db.execute("UPDATE messages SET created_at=? WHERE id=?", (now(), run["assistant_message_id"]))
                service.db.event(run_id, "steer.applied", message_view(service.require("messages", identity)))
            service.db.event(run_id, "steer.updated", view(row))
        return view(row) if row else None


def expire_steers(service, run_id):
    rows = service.db.all("SELECT * FROM run_steers WHERE run_id=? AND status IN ('queued','pending')", (run_id,))
    service.db.execute("UPDATE run_steers SET status='uncertain',updated_at=? WHERE run_id=? AND status IN ('queued','pending')", (now(), run_id))
    for row in rows:
        service.db.event(run_id, "steer.updated", view({**row, "status": "uncertain"}))


async def dispatch_steer(service, run_id, body, user):
    async def dispatch():
        run, row, created = await offload_write(reserve, service, run_id, body, user)
        if not created:
            return view(row)
        actor = service.manager.actors.get(run["conversation_id"])
        runtime = actor.runtime if actor and actor.current_run_id == run_id else None
        send = getattr(runtime, "steer", None)
        if not send:
            return await offload_write(settle, service, run_id, row["id"], "rejected")
        try:
            accepted = await send(run_id, row["id"], row["text"])
        except Exception:
            # A lost receipt cannot establish whether the native inbox accepted it.
            current = await offload_write(settle, service, run_id, row["id"], "uncertain")
            return current
        return await offload_write(settle, service, run_id, row["id"], "queued" if accepted else "rejected")

    return await shielded(dispatch())
