"""Feature-gated owner API for Goal execution rounds, budgets and leases.

``POST /execution`` records explicit approval. ``POST /execution/rounds``
reserves exactly one real host run for the next approved native Goal round; the
run still goes through the existing RunManager/FIFO, approvals and usage path,
and the pinned official driver must pass host admission inside that run before
a model request is made. ``DELETE`` stops future continuation. No endpoint
replays a round or arms a native goal outside the official seam.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import Field

from .conversation_goal import require_goal_session
from .db import offload, offload_write
from .goal_execution import (
    MAX_COST_MICROS, MAX_ROUNDS, MAX_TIME_SECONDS, MAX_TOKENS,
    goal_execution_view, require_goal_execution_enabled,
    require_goal_owner, start_goal_round, stop_goal_execution,
)
from .models import InputModel
from .security import AppError


class GoalExecutionAuthorization(InputModel):
    goal_id: str = Field(pattern=r"^goal-[a-f0-9-]{36}$")
    goal_revision: int = Field(strict=True, ge=1)
    harness_session_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
    max_rounds: int = Field(strict=True, ge=1, le=MAX_ROUNDS)
    token_budget: int = Field(strict=True, ge=1, le=MAX_TOKENS)
    cost_budget_micros: int = Field(strict=True, ge=1, le=MAX_COST_MICROS)
    time_budget_seconds: int = Field(strict=True, ge=60, le=MAX_TIME_SECONDS)
    confirm: Literal[True]


class GoalRoundStart(InputModel):
    goal_id: str = Field(pattern=r"^goal-[a-f0-9-]{36}$")
    goal_revision: int = Field(strict=True, ge=1)
    confirm: Literal[True]


def create_goal_execution_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/conversations/{conversation_id}/goal/execution")

    @router.get("")
    async def get(conversation_id: str, response: Response,
                  service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_goal_execution_enabled(service)
        return await offload(goal_execution_view, service, conversation_id, user)

    @router.post("")
    async def authorize(conversation_id: str, body: GoalExecutionAuthorization,
                        response: Response, service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_goal_execution_enabled(service)
        row = await offload(require_goal_session, service, conversation_id, user)
        if body.harness_session_id != row["harness_session_id"]:
            raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，请刷新后重新授权")
        # Read the current native goal through the same serialized actor used by
        # state management. A stale id/revision is rejected before any row is
        # written; this call does not run a model turn.
        return await service.manager.goal_state(conversation_id, {"action": "get"}, user,
                                                authorization=body.model_dump())

    @router.post("/rounds")
    async def start_round(conversation_id: str, body: GoalRoundStart,
                          response: Response, service=Depends(svc), user=Depends(authenticated)):
        """Reserve and submit one explicitly confirmed Goal round.

        The native snapshot is read through the same serialized Goal actor as
        state management. The run itself is then created by the normal
        ``Service.prepare_run`` path and submitted to the existing FIFO. This
        call never arms the goal or calls a model.
        """
        response.headers["Cache-Control"] = "no-store"
        require_goal_execution_enabled(service)
        row = await offload(require_goal_session, service, conversation_id, user)
        view = await service.manager.goal_state(conversation_id, {"action": "get"}, user)
        goal = view.get("goal") if isinstance(view, dict) else None
        if not isinstance(goal, dict):
            raise AppError(409, "GOAL_NOT_FOUND", "请先创建目标再启动执行")
        if goal.get("id") != body.goal_id or goal.get("revision") != body.goal_revision:
            raise AppError(409, "GOAL_STALE_REVISION", "目标已变化，请刷新后重新确认")
        result = await offload_write(start_goal_round, service, conversation_id, user,
                                     goal, row["harness_session_id"])
        if not result.get("replayed"):
            result["submitted"] = await service.manager.submit_goal_round(result["run_id"])
        else:
            result["submitted"] = True
        return result

    @router.delete("")
    async def revoke(conversation_id: str, response: Response,
                     service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_goal_execution_enabled(service)
        # Stop must work while a round is running; it only needs ownership.
        await offload(require_goal_owner, service, conversation_id, user)
        # Do not report a successful stop while an outstanding native
        # authorization read can still create a fresh approval. The owner
        # can stop an active run; only the short state maintenance is fenced.
        if conversation_id in service.manager.goal_requests:
            raise AppError(409, "GOAL_SESSION_BUSY", "目标状态正在更新，请稍后撤销")
        await offload_write(stop_goal_execution, service, conversation_id, "user_revoked")
        return await offload(goal_execution_view, service, conversation_id, user)

    return router
