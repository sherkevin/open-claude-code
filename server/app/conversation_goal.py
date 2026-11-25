"""Feature-gated, owner-scoped native Goal state. No autonomous execution yet."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Response
from pydantic import Field, model_validator

from .db import offload
from .models import InputModel
from .security import AppError


class GoalRef(InputModel):
    id: str = Field(pattern=r"^goal-[a-f0-9-]{36}$")
    revision: int = Field(strict=True, ge=1)


class GoalChange(InputModel):
    action: Literal["create", "edit", "pause", "resume", "complete", "clear"]
    ref: GoalRef | None = None
    objective: str | None = Field(default=None, max_length=8000)
    maxGoalRounds: int | None = Field(default=None, strict=True, ge=1, le=32)

    @model_validator(mode="after")
    def check_contract(self):
        fields = self.model_fields_set
        if self.objective is not None and (not self.objective.strip()
                or len(self.objective.encode("utf-8")) > 24000
                or any(ord(c) < 32 and c not in "\n\r\t" or ord(c) == 127 for c in self.objective)):
            raise ValueError("invalid objective")
        if self.action == "create":
            if fields != {"action","objective","maxGoalRounds"} or self.objective is None or self.maxGoalRounds is None:
                raise ValueError("create requires objective and maxGoalRounds")
        elif self.action == "edit":
            if self.ref is None or fields-{"action","ref","objective","maxGoalRounds"} or (
                    self.objective is None and self.maxGoalRounds is None) or (
                    "objective" in fields and self.objective is None or
                    "maxGoalRounds" in fields and self.maxGoalRounds is None):
                raise ValueError("edit requires exact ref and changed fields")
        elif fields != {"action","ref"} or self.ref is None:
            raise ValueError("transition requires exact ref")
        return self


def require_goal_session(service, cid, user, expected_session=None):
    # This is a product capability gate; native state has no agent tools or
    # round driver and cannot grant business permissions.
    if service.policy.is_trial or not service.settings.harness_goal_state_enabled:
        raise AppError(404,"GOAL_DISABLED","持续目标试点尚未开启")
    row = service.require("conversations",cid,user)
    if not service.require("users",user["id"])["enabled"]:
        raise AppError(403,"USER_DISABLED","账号已停用")
    if row.get("runtime_type")!="harness" or not row.get("harness_session_id"):
        raise AppError(409,"GOAL_SESSION_NOT_READY","请先完成一次 Harness 对话")
    if row["archived"] or row["active_run_id"]:
        raise AppError(409,"GOAL_SESSION_BUSY","目标管理仅支持未归档的空闲会话")
    if expected_session is not None and row["harness_session_id"]!=expected_session:
        raise AppError(409,"GOAL_SESSION_CHANGED","原生会话已变化，请刷新后检查目标")
    if service.db.one("SELECT conversation_id FROM conversation_memory_scopes WHERE conversation_id=? AND mutation_id IS NOT NULL",(cid,)):
        raise AppError(409,"MEMORY_BUSY","记忆正在更新，请稍后重试")
    service.usable_assistant(row["assistant_id"],user)
    return row


def create_goal_router(svc, authenticated):
    router=APIRouter(prefix="/api/v1/conversations/{conversation_id}/goal")

    @router.get("")
    async def get(conversation_id:str,response:Response,service=Depends(svc),user=Depends(authenticated)):
        response.headers["Cache-Control"]="no-store"
        return await service.manager.goal_state(conversation_id,{"action":"get"},user)

    @router.post("")
    async def change(conversation_id:str,body:GoalChange,response:Response,service=Depends(svc),user=Depends(authenticated)):
        response.headers["Cache-Control"]="no-store"
        request=body.model_dump(exclude_none=True)
        prompt_safety=getattr(service,"prompt_safety",None)
        if request.get("objective") is not None and prompt_safety is not None and prompt_safety.enabled:
            await offload(require_goal_session,service,conversation_id,user)
            await offload(prompt_safety.require_allowed,request["objective"],user["id"])
        return await service.manager.goal_state(conversation_id,request,user)

    return router
