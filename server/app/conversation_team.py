"""Owner-scoped read-only native Team board; default off."""
from fastapi import APIRouter, Depends, Response
from .security import AppError


def require_team_session(service,cid,user,expected_session=None):
    if service.policy.is_trial or not service.settings.harness_team_state_enabled:
        raise AppError(404,"TEAM_DISABLED","任务板试点尚未开启")
    row=service.require("conversations",cid,user)
    if not service.require("users",user["id"])["enabled"]:
        raise AppError(403,"USER_DISABLED","账号已停用")
    if row.get("runtime_type")!="harness" or not row.get("harness_session_id"):
        raise AppError(409,"TEAM_SESSION_NOT_READY","请先完成一次 Harness 对话")
    if row["archived"] or row["active_run_id"]:
        raise AppError(409,"TEAM_SESSION_BUSY","任务板仅支持未归档的空闲会话")
    if expected_session is not None and row["harness_session_id"]!=expected_session:
        raise AppError(409,"TEAM_SESSION_CHANGED","原生会话已变化，请刷新任务板")
    if service.db.one("SELECT conversation_id FROM conversation_memory_scopes WHERE conversation_id=? AND mutation_id IS NOT NULL",(cid,)):
        raise AppError(409,"MEMORY_BUSY","记忆正在更新，请稍后重试")
    service.usable_assistant(row["assistant_id"],user)
    return row


def create_team_router(svc,authenticated):
    router=APIRouter(prefix="/api/v1/conversations/{conversation_id}/team")

    @router.get("")
    async def get(conversation_id:str,response:Response,service=Depends(svc),user=Depends(authenticated)):
        response.headers["Cache-Control"]="no-store"
        return await service.manager.team_state(conversation_id,user)

    return router
