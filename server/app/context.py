"""Native context reporting and compaction; no application summarization."""
from __future__ import annotations

import math
import hashlib
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .db import dump, now, offload, offload_write, shielded
from .security import AppError

CONTEXT_TTL_SECONDS = 60


class CompactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: str = Field(min_length=1, max_length=128)
    instructions: str = Field(default="", max_length=2000)


def context_binding(config, session_id=None):
    """Bind measurements to host-authorized configuration, never raw provider data."""
    return hashlib.sha256(dump([session_id or config.get("native_session_id"), config["fingerprint"]]).encode()).hexdigest()


def harness_context_receipt(service, conversation_id):
    import json
    # Product-metadata answers do not enter the native session or invoke a model.
    latest = service.db.one("SELECT status,execution_meta FROM runs WHERE conversation_id=? AND kind='chat' AND CASE WHEN json_valid(execution_meta) THEN COALESCE(json_extract(execution_meta,'$.execution_kind'),'model') ELSE 'model' END!='product_metadata' ORDER BY rowid DESC LIMIT 1", (conversation_id,))
    if not latest or latest["status"] != "succeeded":
        return None
    metadata = json.loads(latest.get("execution_meta") or "{}")
    value = metadata.get("context_binding")
    return value if isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value) else None


def harness_context_snapshot(raw=None, error=None, stale_reason=None):
    ready = raw is not None and raw["baselineKind"] != "none" and raw["routeMatches"] and not stale_reason
    reason = stale_reason or ("route_changed" if raw is not None and not raw["routeMatches"] else None)
    if raw is not None and not ready and reason is None:
        reason = "request_unobserved"
    return {
        "source": "harness_token_meter", "status": "partial" if ready else ("unavailable" if error or raw is not None or reason else "not_measured"),
        "total_tokens": raw["totalTokens"] if ready else None,
        # Normalized upstream usage cannot prove raw provider field presence.
        "observed_prompt_tokens": None,
        "max_tokens": None, "percentage": None, "auto_compact_threshold": None,
        "auto_compact_enabled": False, "manual_compact_available": False,
        "precision": "estimated" if ready else "unknown", "baseline_kind": raw["baselineKind"] if ready else None,
        "stale": reason is not None, "stale_reason": reason,
        "projection_seq": raw["projectionSeq"] if raw is not None else None,
        "breakdown": ({"system_tokens": raw["breakdown"]["systemTokens"], "tools_tokens": raw["breakdown"]["toolsTokens"],
                       "message_tokens": raw["breakdown"]["messageTokens"]} if ready else None),
        # Upstream's projection does not carry the provider sample timestamp.
        "observed_at": None, "measured_at": now() if raw is not None or error else None,
        "model": None, "cached": False, "error": error,
    }


def harness_manual_compact_available(service, conversation_id):
    """Return the client-facing capability hint for a new manual compaction.

    This does not replace endpoint authorization. The compact endpoint repeats
    ownership, lifecycle, credential and configuration validation.
    """
    try:
        conversation = service.require("conversations", conversation_id)
        if (conversation.get("runtime_type") != "harness"
                or not conversation.get("harness_session_id")
                or conversation.get("active_run_id")):
            return False
        user = service.require("users", conversation["user_id"])
        if not user["enabled"]:
            return False
        service.usable_assistant(conversation["assistant_id"], user)
        service.runtime_config(conversation_id)
        return True
    except Exception:
        return False


def context_snapshot(raw=None, error=None, model_label=None, runtime_type=None):
    if runtime_type == "harness":
        return {**harness_context_snapshot(error=error), "model": model_label}
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
        "precision": "native_reported" if isinstance(raw, dict) else "unknown",
        "stale": False, "stale_reason": None, "observed_prompt_tokens": None,
        "observed_at": None, "projection_seq": None, "breakdown": None,
        "baseline_kind": None, "manual_compact_available": True,
    }
    if isinstance(raw, dict) and any(value[key] is None for key in ("total_tokens", "max_tokens", "percentage")):
        value.update(status="unavailable", precision="unknown", error="当前运行时未返回完整的上下文统计；缺失数值保持未知")
    return value


def cached_context(service, conversation_id):
    import json
    row = service.require("conversations", conversation_id)
    value = json.loads(row["context_usage"]) if row.get("context_usage") else context_snapshot(runtime_type=row.get("runtime_type"))
    if row.get("runtime_type") == "harness":
        if value.get("source") != "harness_token_meter":
            value = harness_context_snapshot()
        try:
            binding = context_binding(service.runtime_config(conversation_id))
            receipt = harness_context_receipt(service, conversation_id)
            if value.get("measured_at") and (not receipt or not value.get("_binding")) and not value.get("error"):
                value = harness_context_snapshot(stale_reason="request_unobserved")
            elif value.get("_binding") and (value["_binding"] != binding or receipt != binding):
                value = harness_context_snapshot(stale_reason="route_changed")
            elif value.get("measured_at") and not fresh(value) and not value.get("error") and not value.get("stale"):
                value = {**harness_context_snapshot(stale_reason="snapshot_expired"), "measured_at": value["measured_at"]}
        except Exception:
            value = harness_context_snapshot(error="当前授权或模型配置无法核验，上下文统计保持未知")
        value.pop("_binding", None)
        # Older cached projections also lack raw provider field-presence evidence.
        value["observed_prompt_tokens"] = None
        value["manual_compact_available"] = harness_manual_compact_available(service, conversation_id)
    last = service.db.one("SELECT events.* FROM events JOIN runs ON runs.id=events.run_id WHERE runs.conversation_id=? AND events.type IN ('context.compacted','context.compaction_result') ORDER BY events.created_at DESC,events.id DESC LIMIT 1", (conversation_id,))
    last_compaction = None
    if last:
        data = json.loads(last["data"])
        last_compaction = {"run_id": last["run_id"], "status": data.get("status", "completed"), "trigger": data.get("trigger"), "pre_tokens": data.get("pre_tokens"), "created_at": last["created_at"], "error": data.get("error"), "followup_status": data.get("followup_status")}
    return {**value, "model": row.get("model_label"), "cached": bool(value.get("measured_at")), "last_compaction": last_compaction}


def fresh(value):
    measured = value.get("measured_at")
    if value.get("error") or value.get("stale") or value.get("status") not in {"available", "partial"}:
        return False
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(measured)).total_seconds()
        return 0 <= age < CONTEXT_TTL_SECONDS
    except (TypeError, ValueError):
        return False


def save_context(service, conversation_id, value):
    conversation = service.require("conversations", conversation_id)
    value = {**value, "model": conversation.get("model_label")}
    if conversation.get("runtime_type") == "harness":
        value["manual_compact_available"] = harness_manual_compact_available(service, conversation_id)
    service.db.execute("UPDATE conversations SET context_usage=? WHERE id=?", (dump(value), conversation_id))
    return {key: item for key, item in value.items() if key != "_binding"}


def create_context_router(service_dependency, authenticated_dependency):
    router = APIRouter(prefix="/api/v1/conversations/{conversation_id}", tags=["Native context"])

    @router.get("/context")
    def get(conversation_id: str, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        service.require("conversations", conversation_id, user)
        return cached_context(service, conversation_id)

    @router.post("/context/refresh")
    async def refresh(conversation_id: str, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        await offload(service.require, "conversations", conversation_id, user)
        result = await service.manager.context(conversation_id)
        checked = await offload(cached_context, service, conversation_id)
        if result.get("source") == "harness_token_meter":
            # Closing the owned process awaits after measurement. Authorization
            # or configuration may change then; return the entire rechecked
            # snapshot, not the old numbers with only fresh compaction metadata.
            if result.get("error") and checked["status"] == "not_measured":
                checked = {**checked, "status": "unavailable", "error": result["error"]}
            return {**checked, "cached": bool(result.get("cached"))}
        return {**result, "last_compaction": checked["last_compaction"]}

    @router.post("/compact", status_code=202)
    async def compact(conversation_id: str, body: CompactRequest, service=Depends(service_dependency), user=Depends(authenticated_dependency)):
        conversation = await offload(service.require, "conversations", conversation_id, user)
        runtime_type = conversation.get("runtime_type", "claude")
        session_key = "harness_session_id" if runtime_type == "harness" else "sdk_session_id"
        if not conversation.get(session_key):
            raise AppError(409, "CONTEXT_NOT_READY", "此会话还没有可整理的原生上下文")
        instructions = body.instructions.strip()
        if "\x00" in instructions:
            raise AppError(422, "COMPACTION_INSTRUCTIONS_INVALID", "压缩要求包含无效字符")
        text = "/compact" + (" " + instructions if instructions else "")
        data = {"text": text, "file_ids": [], "client_request_id": body.client_request_id}
        await service.review_run_input(conversation_id, {**data, "text": instructions}, user)

        async def _start_compaction():
            # One cancellation-safe unit: a run committed by prepare_run must
            # be marked as a compaction and delivered before a cancellation
            # can propagate, otherwise it would stay queued and hold the
            # conversation busy.
            result, created = await service.prepare_run_async(conversation_id, data, user)
            if created:
                def mark_compaction():
                    with service.db.transaction():
                        run = service.require("runs", result["run_id"])
                        metadata = json.loads(run.get("execution_meta") or "{}")
                        metadata["compaction_instructions"] = instructions
                        service.db.execute("UPDATE runs SET kind='compact',execution_meta=? WHERE id=?", (dump(metadata), result["run_id"]))

                await offload_write(mark_compaction)
            elif (await offload(service.require, "runs", result["run_id"]))["kind"] != "compact":
                raise AppError(409, "REQUEST_ID_REUSED", "该请求标识已用于其他任务")
            await service.manager.ensure_submitted(result["run_id"])
            return result

        return await shielded(_start_compaction())

    return router
