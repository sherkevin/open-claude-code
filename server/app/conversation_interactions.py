"""Owner-scoped questions that pause the current turn until the user answers."""
import asyncio
import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import Field

from .db import dump, now, offload, offload_write, uid
from .models import InputModel
from .security import AppError


class InteractionAnswer(InputModel):
    answer: Annotated[str, Field(min_length=1, max_length=2000)]


def _awaiting(service, run_id, user):
    run = service.require("runs", run_id, user)
    conversation = service.require("conversations", run["conversation_id"], user)
    service.usable_assistant(conversation["assistant_id"], user)
    if (not user["enabled"] or conversation["archived"] or run["user_id"] != user["id"]
            or conversation["active_run_id"] != run_id or run["status"] not in {"running", "awaiting_approval"}):
        raise AppError(409, "INTERACTION_INACTIVE", "该任务已结束，无法提交回答")
    return run


async def request_interaction(runtime, run_id, kind, arguments):
    service = runtime.service
    prompt = arguments.get("question" if kind == "question" else "plan")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > (2000 if kind == "question" else 12000):
        raise AppError(422, "INTERACTION_INVALID", "提问或计划内容无效")
    options = arguments.get("options", []) if kind == "question" else []
    if (not isinstance(options, list) or len(options) > 6
            or any(not isinstance(item, str) or not item.strip() or len(item) > 160 for item in options)
            or len(set(options)) != len(options)):
        raise AppError(422, "INTERACTION_INVALID", "最多提供六个不同的短选项")
    identity = uid()

    def open_request():
        with service.db.transaction():
            run = service.require("runs", run_id)
            _awaiting(service, run_id, service.require("users", run["user_id"]))
            service.db.execute(
                """INSERT INTO conversation_interactions(id,run_id,kind,prompt,options,status,created_at)
                   VALUES (?,?,?,?,?,'pending',?)""", (identity, run_id, kind, prompt, dump(options), now()),
            )

    await offload_write(open_request)
    try:
        def read_answer():
            run = service.require("runs", run_id)
            _awaiting(service, run_id, service.require("users", run["user_id"]))
            return service.db.one("SELECT status,answer FROM conversation_interactions WHERE id=?", (identity,))

        deadline = time.monotonic() + service.settings.approval_seconds
        while time.monotonic() < deadline and not runtime.interrupted:
            record = await offload(read_answer)
            if record["status"] == "answered":
                return {"status": "answered", "answer": record["answer"]}
            await asyncio.sleep(.25)
        if runtime.interrupted:
            raise asyncio.CancelledError()
        return {"status": "expired", "message": "用户尚未回答，本项未获确认"}
    finally:
        await offload_write(service.db.execute,
            "UPDATE conversation_interactions SET status='expired' WHERE id=? AND status='pending'", (identity,))


def create_interaction_router(service_dependency, authenticated):
    router = APIRouter(prefix="/api/v1/runs")

    @router.get("/{run_id}/interactions")
    def list_interactions(run_id: str, service=Depends(service_dependency), user=Depends(authenticated)):
        service.require("runs", run_id, user)
        try:
            _awaiting(service, run_id, user)
        except AppError as error:
            if error.code == "INTERACTION_INACTIVE":
                return []
            raise
        rows = service.db.all(
            "SELECT id,kind,prompt,options FROM conversation_interactions WHERE run_id=? AND status='pending' ORDER BY created_at,id",
            (run_id,),
        )
        return [{**item, "options": json.loads(item["options"])} for item in rows]

    @router.post("/{run_id}/interactions/{identity}")
    def answer_interaction(run_id: str, identity: str, data: InteractionAnswer, service=Depends(service_dependency), user=Depends(authenticated)):
        answer = data.answer.strip()
        if not answer:
            raise AppError(422, "INTERACTION_INVALID", "请输入回答")
        with service.db.transaction():
            _awaiting(service, run_id, user)
            row = service.db.one("SELECT status,kind FROM conversation_interactions WHERE id=? AND run_id=?", (identity, run_id))
            if not row or row["status"] != "pending":
                raise AppError(409, "INTERACTION_INACTIVE", "该问题已处理")
            if row["kind"] == "plan" and answer != "确认计划" and not answer.startswith("修改计划："):
                raise AppError(422, "INTERACTION_INVALID", "请选择确认计划或提出修改")
            service.db.execute(
                "UPDATE conversation_interactions SET status='answered',answer=?,answered_by=?,answered_at=? WHERE id=?",
                (answer, user["id"], now(), identity),
            )
        return {"status": "answered"}

    return router
