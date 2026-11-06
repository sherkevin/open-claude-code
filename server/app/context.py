"""Native context reporting and compaction; no application summarization."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .db import dump, now
from .security import AppError

CONTEXT_TTL_SECONDS = 60


class CompactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    instructions: str = Field(default="", max_length=2000)


def context_snapshot(raw=None, error=None, model_label=None):
    def number(key):
        value = raw.get(key) if isinstance(raw, dict) else None
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    value = {
        "source": "native_runtime", "status": "available" if isinstance(raw, dict) else ("unavailable" if error else "not_measured"),
        "total_tokens": number("totalTokens"), "max_tokens": number("maxTokens"), "percentage": number("percentage"),
        "auto_compact_threshold": number("autoCompactThreshold"),
        "auto_compact_enabled": raw.get("isAutoCompactEnabled") if isinstance(raw, dict) and type(raw.get("isAutoCompactEnabled")) is bool else None,
        "model": model_label,
        "measured_at": now() if raw is not None or error else None,
        "cached": False, "error": error,
    }
    if isinstance(raw, dict) and any(value[key] is None for key in ("total_tokens", "max_tokens", "percentage")):
        value.update(status="unavailable", error="当前运行时未返回完整的上下文统计；缺失数值保持未知")
    return value


def cached_context(service, conversation_id):
    import json
    row = service.require("conversations", conversation_id)
    value = json.loads(row["context_usage"]) if row.get("context_usage") else context_snapshot()
    last = service.db.one("SELECT events.* FROM events JOIN runs ON runs.id=events.run_id WHERE runs.conversation_id=? AND events.type IN ('context.compacted','context.compaction_result') ORDER BY events.created_at DESC,events.id DESC LIMIT 1", (conversation_id,))
    last_compaction = None
    if last:
        data = json.loads(last["data"])
        last_compaction = {"run_id": last["run_id"], "status": data.get("status", "completed"), "trigger": data.get("trigger"), "pre_tokens": data.get("pre_tokens"), "created_at": last["created_at"], "error": data.get("error"), "followup_status": data.get("followup_status")}
    return {**value, "model": row.get("model_label"), "cached": bool(value.get("measured_at")), "last_compaction": last_compaction}


def fresh(value):
    measured = value.get("measured_at")
    return bool(measured and (datetime.now(timezone.utc) - datetime.fromisoformat(measured)).total_seconds() < CONTEXT_TTL_SECONDS)


def save_context(service, conversation_id, value):
    value = {**value, "model": service.require("conversations", conversation_id).get("model_label")}
    service.db.execute("UPDATE conversations SET context_usage=? WHERE id=?", (dump(value), conversation_id))
    return value


def create_context_router(service_dependency, authenticated_dependency):
    router = APIRouter(prefix="/api/v1/conversations/{conversation_id}", tags=["Native context"])

    @router.get("/context")
    def get(conversation_id: str, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        service.require("conversations", conversation_id, user)
        return cached_context(service, conversation_id)

    @router.post("/context/refresh")
    async def refresh(conversation_id: str, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        service.require("conversations", conversation_id, user)
        result = await service.manager.context(conversation_id)
        return {**result, "last_compaction": cached_context(service, conversation_id)["last_compaction"]}

    @router.post("/compact", status_code=202)
    async def compact(conversation_id: str, body: CompactRequest, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        conversation = service.require("conversations", conversation_id, user)
        if not conversation["sdk_session_id"]:
            raise AppError(409, "CONTEXT_NOT_READY", "此会话还没有可整理的原生上下文")
        text = "/compact" + (" " + body.instructions.strip() if body.instructions.strip() else "")
        result, created = service.prepare_run(conversation_id, {"text": text, "file_ids": [], "client_request_id": body.client_request_id}, user)
        if created:
            service.db.execute("UPDATE runs SET kind='compact' WHERE id=?", (result["run_id"],))
            service.manager.submit(result["run_id"])
        elif service.require("runs", result["run_id"])["kind"] != "compact":
            raise AppError(409, "REQUEST_ID_REUSED", "该请求标识已用于其他任务")
        return result

    return router
