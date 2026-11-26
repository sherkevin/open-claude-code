"""Feature-gated owner API for the native Team task metadata lifecycle.

Every route here is mounted only when the independent task switch is on. None
of them prompt a model, spawn a subagent, call a business tool or change a
permission. The official ``agentTeams`` board is mutated only through the
pinned Node seam; this router records approval, session binding and history.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import Field

from .db import offload, offload_write
from .models import InputModel
from .security import AppError
from .team_tasks import (
    MAX_DESCRIPTION_CHARS, MAX_EXPIRES_SECONDS, MAX_OPERATIONS,
    MAX_SUBJECT_CHARS, MIN_EXPIRES_SECONDS, TASK_ID,
    acknowledge_team_operation, authorize_team_tasks, begin_team_operation,
    reject_team_operation, require_team_tasks_enabled,
    require_team_tasks_owner, stop_team_tasks, team_collaboration_view,
    uncertain_team_operation,
)

SESSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
REQUEST_PATTERN = r"^[A-Za-z0-9_-]{8,128}$"


class TeamAuthorization(InputModel):
    harness_session_id: str = Field(pattern=SESSION_PATTERN)
    max_operations: int = Field(strict=True, ge=1, le=MAX_OPERATIONS)
    expires_in_seconds: int = Field(strict=True, ge=MIN_EXPIRES_SECONDS,
                                    le=MAX_EXPIRES_SECONDS)
    confirm: Literal[True]


class TeamBoardChange(InputModel):
    harness_session_id: str = Field(pattern=SESSION_PATTERN)
    client_request_id: str = Field(pattern=REQUEST_PATTERN)
    action: Literal["enable", "disable"]
    confirm: Literal[True]


class TeamTaskCreate(InputModel):
    harness_session_id: str = Field(pattern=SESSION_PATTERN)
    client_request_id: str = Field(pattern=REQUEST_PATTERN)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    blocked_by: list[str] = Field(default_factory=list, max_length=64)
    write_scopes: list[str] = Field(default_factory=list, max_length=64)


class TeamTaskTransition(InputModel):
    harness_session_id: str = Field(pattern=SESSION_PATTERN)
    client_request_id: str = Field(pattern=REQUEST_PATTERN)
    expected_revision: int = Field(strict=True, ge=1, le=10**9)


def _task_id(value: str) -> str:
    if not isinstance(value, str) or TASK_ID.fullmatch(value) is None:
        raise AppError(422, "TEAM_REQUEST_INVALID", "任务标识无效")
    return value


def _task_summary(operation: dict) -> dict | None:
    if not operation.get("task_id"):
        return None
    return {"id": operation["task_id"], "revision": operation["task_revision"],
            "status": operation["task_status"]}


def _response(operation: dict, board=None, task=None, replayed=False) -> dict:
    return {"schema": "team-task-response/v1", "operation": operation,
            "board": board, "task": task,
            "execution_enabled": False, "replayed": replayed}


def create_team_tasks_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/conversations/{conversation_id}/team")

    @router.get("/collaboration")
    async def collaboration(conversation_id: str, response: Response,
                            service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        return await offload(team_collaboration_view, service, conversation_id, user)

    @router.post("/authorization")
    async def authorize(conversation_id: str, body: TeamAuthorization,
                        response: Response, service=Depends(svc),
                        user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        return await offload_write(authorize_team_tasks, service, conversation_id,
                                   user, body.model_dump())

    @router.delete("/authorization")
    async def revoke(conversation_id: str, response: Response,
                     service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        require_team_tasks_enabled(service)
        # Stopping must work while the conversation is busy or archived; it
        # only needs ownership. A metadata RPC in flight is fenced instead of
        # racing a successful native mutation.
        await offload(require_team_tasks_owner, service, conversation_id, user)
        manager = getattr(service, "manager", None)
        if manager is not None and conversation_id in manager.goal_requests:
            raise AppError(409, "TEAM_SESSION_BUSY", "任务板操作正在进行，请稍后撤销授权")
        await offload_write(stop_team_tasks, service, conversation_id, "user_revoked")
        return await offload(team_collaboration_view, service, conversation_id, user)

    @router.post("/board")
    async def change_board(conversation_id: str, body: TeamBoardChange,
                           response: Response, service=Depends(svc),
                           user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        request = {"action": body.action, **body.model_dump(exclude={"action"})}
        return await _mutate(service, conversation_id, user, request)

    @router.post("/tasks")
    async def create_task(conversation_id: str, body: TeamTaskCreate,
                          response: Response, service=Depends(svc),
                          user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        request = {"action": "create", **body.model_dump()}
        return await _mutate(service, conversation_id, user, request)

    @router.post("/tasks/{task_id}/claim")
    async def claim_task(conversation_id: str, task_id: str, body: TeamTaskTransition,
                         response: Response, service=Depends(svc),
                         user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        return await _mutate(service, conversation_id, user,
                             {"action": "claim", "task_id": _task_id(task_id),
                              **body.model_dump()})

    @router.post("/tasks/{task_id}/complete")
    async def complete_task(conversation_id: str, task_id: str,
                            body: TeamTaskTransition, response: Response,
                            service=Depends(svc), user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        return await _mutate(service, conversation_id, user,
                             {"action": "complete", "task_id": _task_id(task_id),
                              **body.model_dump()})

    @router.post("/operations/{operation_id}/acknowledge")
    async def acknowledge(conversation_id: str, operation_id: str,
                          response: Response, service=Depends(svc),
                          user=Depends(authenticated)):
        response.headers["Cache-Control"] = "no-store"
        operation = await offload_write(acknowledge_team_operation, service,
                                        conversation_id, user, operation_id)
        return {"schema": "team-operation-response/v1", "operation": operation,
                "execution_enabled": False}

    return router


async def _mutate(service, conversation_id: str, user, request: dict) -> dict:
    """Pending audit -> serialized native RPC -> confirmed/uncertain receipt."""
    operation = await offload_write(begin_team_operation, service,
                                    conversation_id, user, request)
    if operation.get("replay"):
        view = operation["operation"]
        return _response(view, task=_task_summary(view), replayed=True)
    try:
        value = await service.manager.team_task(
            conversation_id, user, operation["native"], operation["id"])
    except AppError as error:
        # Only a still-pending row is downgraded. The actor may already have
        # confirmed or fenced it after the native RPC returned.
        await offload_write(reject_team_operation, service, operation["id"], error.code)
        raise
    except Exception:
        await offload_write(uncertain_team_operation, service, operation["id"],
                            "TEAM_TASK_UNCERTAIN")
        raise AppError(503, "TEAM_TASK_UNCERTAIN",
                       "任务板操作结果未确认，请刷新核对后再操作") from None
    return _response(value["operation"], board=value.get("board"),
                     task=value.get("task"), replayed=False)
