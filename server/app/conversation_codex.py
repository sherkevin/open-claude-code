"""Default-off owner API for the Codex App Server host slice.

The router is mounted only when an operator enables the feature and the host is
not in trial mode.  It exposes thread preparation, an explicit message turn,
interrupt and recovery reconciliation.  It never selects a model, never sends a
turn on its own, never grants an approval and never returns host filesystem
paths.  Product conversation message persistence is intentionally not wired
here; this surface is an isolated engineering-executor pilot.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import Field

from .codex_appserver import (
    CodexHost, CodexHostBusy, CodexHostConflict, CodexHostDisabled,
    CodexHostError, CodexHostRejected, CodexHostUnavailable,
)
from .db import offload
from .models import InputModel
from .security import AppError


class CodexTurnRequest(InputModel):
    client_request_id: str = Field(
        min_length=8, max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
    text: str = Field(min_length=1, max_length=20000)


class CodexInterruptRequest(InputModel):
    turn_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def require_codex_conversation(service, conversation_id: str, user: dict,
                               expected_session: str | None = None) -> dict:
    if service.policy.is_trial or not service.settings.codex_appserver_enabled:
        raise AppError(404, "CODEX_DISABLED", "Codex 工程执行器试点尚未开启")
    row = service.require("conversations", conversation_id, user)
    if not service.require("users", user["id"])["enabled"]:
        raise AppError(403, "USER_DISABLED", "账号已停用")
    if row["archived"] or row["active_run_id"]:
        raise AppError(409, "CODEX_SESSION_BUSY", "Codex 仅支持未归档的空闲会话")
    if expected_session is not None and row.get("harness_session_id") != expected_session:
        raise AppError(409, "CODEX_SESSION_CHANGED", "会话已变化，请刷新后重试")
    service.usable_assistant(row["assistant_id"], user)
    return row


def _app_error(exc: CodexHostError) -> AppError:
    if isinstance(exc, CodexHostDisabled):
        return AppError(404, "CODEX_DISABLED", "Codex 工程执行器试点尚未开启")
    if isinstance(exc, CodexHostBusy):
        return AppError(409, "CODEX_BUSY", "该会话已有 Codex 操作在进行")
    if isinstance(exc, CodexHostConflict):
        return AppError(409, "CODEX_CONFLICT", "Codex 会话状态不允许此操作")
    if isinstance(exc, CodexHostRejected):
        return AppError(422, "CODEX_REQUEST_REJECTED", "Codex 请求未通过宿主校验")
    if isinstance(exc, CodexHostUnavailable):
        return AppError(503, "CODEX_UNAVAILABLE", "Codex 执行器暂时不可用")
    return AppError(503, "CODEX_UNAVAILABLE", "Codex 执行器暂时不可用")


async def _call(fn, *args):
    try:
        return await offload(fn, *args)
    except CodexHostError as exc:
        raise _app_error(exc) from None


def create_codex_router(svc, authenticated, host: CodexHost):
    router = APIRouter(prefix="/api/v1/conversations/{conversation_id}/codex")

    @router.get("")
    async def status(conversation_id: str, response: Response,
                     service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.status, user["id"], conversation_id)

    @router.post("/thread")
    async def prepare(conversation_id: str, response: Response,
                      service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.ensure_thread, user["id"], conversation_id)

    @router.post("/turn")
    async def turn(conversation_id: str, body: CodexTurnRequest, response: Response,
                   service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.run_turn, user["id"], conversation_id,
                           body.client_request_id, body.text)

    @router.post("/interrupt")
    async def interrupt(conversation_id: str, response: Response,
                        body: CodexInterruptRequest | None = None,
                        service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.interrupt, user["id"], conversation_id,
                           body.turn_id if body else None)

    @router.post("/reconcile")
    async def reconcile(conversation_id: str, response: Response,
                        service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.reconcile, user["id"], conversation_id)

    return router
