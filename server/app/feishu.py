"""Loopback-only gateway used by the Feishu long-connection sidecar.

The sidecar owns Feishu credentials and WebSocket lifecycle.  This module
owns product authorization, conversation mapping, idempotency, and access to
the existing run manager.  Keeping those responsibilities in the API server
means a Feishu event can never choose an arbitrary local user, assistant, or
workspace by sending IDs from the chat client.
"""
from __future__ import annotations

import hmac
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import now, offload, offload_write
from .security import AppError
from .service import conversation_view, message_view


class FeishuInbound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=200)
    chat_type: str = Field(default="p2p", min_length=1, max_length=30)
    open_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=50000)


class FeishuDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str = Field(pattern="^(allow|deny)$")


def _configured_user(service, open_id: str):
    settings = service.settings
    if not settings.feishu_allowed_open_ids:
        raise AppError(503, "FEISHU_ALLOWLIST_NOT_CONFIGURED", "服务端尚未配置飞书 open_id 白名单")
    if open_id not in settings.feishu_allowed_open_ids:
        raise AppError(403, "FEISHU_USER_DENIED", "此飞书账号未被允许使用云端智能体")
    if settings.feishu_user_id:
        user = service.db.one("SELECT * FROM users WHERE id=? AND enabled=1", (settings.feishu_user_id,))
    elif settings.feishu_username:
        user = service.db.one("SELECT * FROM users WHERE username=? AND enabled=1", (settings.feishu_username,))
    else:
        raise AppError(503, "FEISHU_USER_NOT_CONFIGURED", "服务端尚未配置飞书对应的工作台账号")
    if not user:
        raise AppError(503, "FEISHU_USER_NOT_FOUND", "飞书对应的工作台账号不存在或已停用")
    return user


def create_feishu_router(service_dependency):
    """Create the private API consumed by ``feishu_bridge``.

    The server is expected to bind to loopback.  A separate high-entropy
    bridge token is still required so an unrelated local process cannot drive
    agent runs through this surface.
    """
    router = APIRouter(prefix="/internal/feishu", tags=["Feishu bridge"])

    def bridge_auth(request: Request, service=Depends(service_dependency)):
        configured = service.settings.feishu_token()
        supplied = request.headers.get("x-agent-feishu-bridge-token", "")
        if not configured or not hmac.compare_digest(supplied, configured):
            raise AppError(401, "FEISHU_BRIDGE_UNAUTHORIZED", "飞书桥接未授权")
        host = request.client.host if request.client else ""
        if host and host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise AppError(403, "FEISHU_BRIDGE_LOOPBACK_ONLY", "飞书桥接接口只允许本机访问")
        return service

    def binding_for(service, payload: FeishuInbound, user):
        row = service.db.one("SELECT * FROM feishu_chat_bindings WHERE chat_id=?", (payload.chat_id,))
        if row:
            if row["open_id"] != payload.open_id or row["user_id"] != user["id"]:
                raise AppError(403, "FEISHU_CHAT_BOUND", "此飞书会话已绑定到其他工作台账号")
            conversation = service.require("conversations", row["conversation_id"], user)
            return row, conversation

        settings = service.settings
        if payload.chat_type != "p2p":
            allowed = payload.chat_id in settings.feishu_allowed_chat_ids
            if not settings.feishu_allow_group_chats or not allowed:
                raise AppError(403, "FEISHU_GROUP_NOT_ALLOWED", "群聊尚未加入飞书桥接白名单")
        if not settings.feishu_assistant_id:
            raise AppError(503, "FEISHU_ASSISTANT_NOT_CONFIGURED", "服务端尚未配置飞书默认助手")
        conversation = service.create_conversation({
            "assistant_id": settings.feishu_assistant_id,
            "title": payload.text.strip()[:40] or "飞书任务",
            "auto_model_routing": True,
            "category": "飞书",
            "tags": ["feishu"],
        }, user)
        created = now()
        service.db.execute(
            "INSERT INTO feishu_chat_bindings(chat_id,open_id,user_id,conversation_id,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (payload.chat_id, payload.open_id, user["id"], conversation["id"], created, created),
        )
        return service.db.one("SELECT * FROM feishu_chat_bindings WHERE chat_id=?", (payload.chat_id,)), service.require("conversations", conversation["id"], user)

    @router.post("/messages", status_code=202)
    async def inbound_message(payload: FeishuInbound, service=Depends(bridge_auth)):
        user = await offload(_configured_user, service, payload.open_id)
        # The event identifier is the same idempotency key used by the public
        # message API.  A bridge reconnect or Feishu retry therefore cannot
        # start the same task twice.
        duplicate = await offload(service.db.one, "SELECT * FROM feishu_inbound_events WHERE event_id=?", (payload.event_id,))
        if duplicate:
            existing = await offload(service.db.one, "SELECT id FROM runs WHERE client_request_id=?", (payload.event_id,))
            return {"accepted": True, "duplicate": True, "run_id": existing["id"] if existing else None}
        binding, conversation = await offload_write(binding_for, service, payload, user)
        result, created = await service.prepare_and_submit_async(conversation["id"], {
            "client_request_id": payload.event_id,
            "text": payload.text,
            "file_ids": [],
            "skill_ids": [],
        }, user)
        # Record the event only after prepare_run succeeds.  If the server is
        # temporarily busy, the bridge can retry the same event safely.
        await offload_write(service.db.execute,
            "INSERT OR IGNORE INTO feishu_inbound_events(event_id,chat_id,open_id,message_id,received_at) VALUES(?,?,?,?,?)",
            (payload.event_id, payload.chat_id, payload.open_id, payload.message_id, now()),
        )
        await offload_write(service.db.execute, "UPDATE feishu_chat_bindings SET updated_at=? WHERE chat_id=?", (now(), binding["chat_id"]))
        return {"accepted": True, "duplicate": not created, "run_id": result["run_id"], "conversation_id": conversation["id"], "message_id": result["message"]["id"]}

    @router.get("/runs/{run_id}/events")
    def run_events(run_id: str, after: int = 0, service=Depends(bridge_auth)):
        run = service.require("runs", run_id)
        configured_id = service.settings.feishu_user_id
        if not configured_id and service.settings.feishu_username:
            configured_row = service.db.one("SELECT id FROM users WHERE username=?", (service.settings.feishu_username,))
            configured_id = configured_row["id"] if configured_row else ""
        if not configured_id or run["user_id"] != configured_id:
            raise AppError(404, "NOT_FOUND", "任务不存在")
        assistant = service.db.one("SELECT * FROM messages WHERE id=?", (run["assistant_message_id"],)) if run.get("assistant_message_id") else None
        return {
            "run": service.run_view(run),
            "assistant_message": message_view(assistant) if assistant else None,
            "events": service.public_events(run_id, max(0, after)),
        }

    @router.get("/status")
    def status(service=Depends(bridge_auth)):
        user_id = service.settings.feishu_user_id
        if not user_id and service.settings.feishu_username:
            row = service.db.one("SELECT id FROM users WHERE username=?", (service.settings.feishu_username,))
            user_id = row["id"] if row else ""
        if not user_id:
            raise AppError(503, "FEISHU_USER_NOT_CONFIGURED", "服务端尚未配置飞书对应的工作台账号")
        rows = service.db.all("SELECT * FROM runs WHERE user_id=? AND status NOT IN ('succeeded','failed','cancelled','interrupted') ORDER BY created_at DESC", (user_id,))
        return [{"run": service.run_view(row), "conversation": conversation_view(service.require("conversations", row["conversation_id"]))} for row in rows]

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str, service=Depends(bridge_auth)):
        run = await offload(service.require, "runs", run_id)
        user = await offload(service.require, "users", run["user_id"])
        if service.settings.feishu_user_id and user["id"] != service.settings.feishu_user_id:
            raise AppError(404, "NOT_FOUND", "任务不存在")
        await service.manager.cancel(run_id)
        return await offload(lambda: service.run_view(service.require("runs", run_id)))

    @router.post("/approvals/{approval_id}/decision")
    async def decide(approval_id: str, data: FeishuDecision, service=Depends(bridge_auth)):
        row = await offload(service.require, "approvals", approval_id)
        if row.get("approval_mode") == "dual":
            raise AppError(409, "FEISHU_DUAL_APPROVAL_REQUIRED", "双人高风险审批请在工作台中完成")
        run = await offload(service.require, "runs", row["run_id"])
        user = await offload(service.require, "users", run["user_id"])
        if service.settings.feishu_user_id and user["id"] != service.settings.feishu_user_id:
            raise AppError(404, "NOT_FOUND", "审批不存在")
        future = service.manager.approvals.get(approval_id)
        if row["status"] != "pending" or row["expires_at"] <= now() or not future or future.done():
            raise AppError(409, "APPROVAL_EXPIRED", "此审批已处理或过期")
        decided_at = now()
        status = "allowed" if data.decision == "allow" else "denied"
        updated = await offload_write(
            service.db.execute,
            "UPDATE approvals SET status=?,decided_at=?,decided_by=?,decision_source='user' WHERE id=? AND status='pending'",
            (status, decided_at, user["id"], approval_id),
        )
        if updated.rowcount != 1:
            raise AppError(409, "APPROVAL_EXPIRED", "此审批已处理或过期")
        # Wake on the loop thread only if the waiter is still pending.  If it
        # timed out while the UPDATE ran on a worker thread, the conditional
        # update above and the waiter's settlement have one durable winner and
        # the runtime aligns its decision to that row.
        future = service.manager.approvals.get(approval_id)
        if future is not None and not future.done():
            future.set_result(data.decision)
        return {"id": approval_id, "status": status, "decided_at": decided_at, "decision_source": "user"}

    return router
