"""Default-off owner API for the Codex App Server host slice.

The router is mounted only when an operator enables the feature and the host is
not in trial mode.  It exposes thread preparation, an explicit message turn,
interrupt and recovery reconciliation.  It never selects a model, never sends a
turn on its own, never grants an approval and never returns host filesystem
paths.  Product conversation message persistence is intentionally not wired
here; this surface is an isolated engineering-executor pilot.
"""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field

from .codex_appserver import (
    CodexHost, CodexHostBusy, CodexHostConflict, CodexHostDisabled,
    CodexHostError, CodexHostRejected, CodexHostUnavailable,
)
from .conversation_title import first_message_title
from .db import now, offload
from .models import InputModel
from .security import AppError


class CodexTurnRequest(InputModel):
    client_request_id: str = Field(
        min_length=8, max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
    text: str = Field(min_length=1, max_length=20000)
    prepare_if_needed: bool = Field(default=False, strict=True)


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


def _run_owner_turn(host, service, conversation, user_id, body):
    retry_title = False

    def set_first_title():
        nonlocal retry_title
        try:
            service.db.execute(
                "UPDATE conversations SET title=?,auto_title_pending=0,updated_at=? "
                "WHERE id=? AND user_id=? AND auto_title_pending=1",
                (first_message_title(body.text, engineering=True), now(), conversation["id"], user_id),
            )
            retry_title = False
        except sqlite3.Error:
            # The native receipt is durable. Its outcome must survive a failing
            # workbench projection, including a disconnect from the HTTP client.
            retry_title = True
            logging.getLogger(__name__).warning("Could not persist the Codex conversation topic", exc_info=True)

    try:
        return host.run_turn(user_id, conversation["id"], body.client_request_id, body.text,
                             body.prepare_if_needed,
                             on_first_receipt=set_first_title if conversation["auto_title_pending"] else None)
    finally:
        if retry_title:
            set_first_title()


_HOME_TERMINAL = {"completed": "completed", "interrupted": "interrupted", "cancelled": "interrupted",
                  "canceled": "interrupted", "failed": "failed"}


def _home_phase(status):
    operation = status.get("operation") or {}
    active = status.get("active_turn") or {}
    last = status.get("last_turn") or {}
    terminal = _HOME_TERMINAL.get(last.get("status"))
    current_id = operation.get("request_id") if operation else active.get("request_id")
    if terminal and current_id and last.get("request_id") == current_id:
        return terminal
    if operation:
        if current_id and active.get("interrupt_requested") and active.get("request_id") == current_id:
            return "interrupting"
        phase = operation.get("phase")
        return phase if phase in ("preparing", "thread_ready", "running", "output") else "unavailable"
    if active.get("can_interrupt"):
        return "interrupting" if active.get("interrupt_requested") else "running"
    if status.get("recovery_required"):
        return "recovery_required"
    return terminal


def _read_home_phases(host, service, user, conversation_ids):
    # The caller already has its list of rows. An inaccessible row and a row
    # with no Codex activity have the same projection.
    phases = {}
    for cid in dict.fromkeys(conversation_ids):
        try:
            require_codex_conversation(service, cid, user)
        except AppError as exc:
            if exc.code in ("USER_DISABLED", "CODEX_DISABLED") or exc.status not in (403, 404, 409):
                raise
            phases[cid] = None
            continue
        try:
            phase = _home_phase(host.status(user["id"], cid))
            if phase == "recovery_required":
                # A turn can finish between the durable binding read and the
                # live lock probe. Confirm an orphan before labelling it.
                phase = _home_phase(host.status(user["id"], cid))
            phases[cid] = phase
        except CodexHostUnavailable:
            phases[cid] = "unavailable"
    return {"phases": [phases[cid] for cid in conversation_ids]}


def create_codex_router(svc, authenticated, host: CodexHost):
    root = APIRouter(prefix="/api/v1")
    router = APIRouter(prefix="/conversations/{conversation_id}/codex")

    @root.get("/workspace/codex-phases")
    async def home_phases(response: Response, conversation_id: list[str] = Query(min_length=1, max_length=5),
                          service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        return await _call(_read_home_phases, host, service, user, conversation_id)

    @router.get("")
    async def status(conversation_id: str, response: Response,
                     service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.status, user["id"], conversation_id)

    @router.get("/requests/{request_id}")
    async def request_status(conversation_id: str, request_id: str, response: Response,
                             service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.request_status, user["id"], conversation_id, request_id)

    @router.get("/history")
    async def history(conversation_id: str, response: Response,
                      service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_codex_conversation(service, conversation_id, user)
        return await _call(host.receipt_history, user["id"], conversation_id)

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
        conversation = require_codex_conversation(service, conversation_id, user)
        return await _call(_run_owner_turn, host, service, conversation, user["id"], body)

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

    root.include_router(router)
    return root
