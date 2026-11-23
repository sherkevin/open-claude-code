"""Route signed-in workbench operators to a separate local connector control service."""

from __future__ import annotations

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Path

from .kaiwu_assist import SESSION_ID_RE, ImportInput, JobInput, OfferInput
from .mcp_import import ImportRequest, plan_mcp_import
from .security import AppError
from .service import connection_view

SessionID = Annotated[str, Path(pattern=SESSION_ID_RE)]
BRIDGE_PATH = "/internal/kaiwu-assist/v1"
SOURCE_ERRORS = {"NATIVE_RELAY_OFFLINE", "BUILTIN_DEMO_HANDOFF_DENIED",
                 "HANDOFF_TRANSPORT_UNSUPPORTED", "INTEGRATION_SOURCE_DENIED"}


def _forward(service, operator, method, path, payload=None):
    settings = service.settings
    key = settings.kaiwu_assist_bridge_secret()
    if not key:
        raise AppError(503, "ASSIST_BRIDGE_UNAVAILABLE", "协助后台尚未配置")
    headers = {"Authorization": f"Bearer {key}", "X-Kaiwu-Operator": operator}
    try:
        with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
            response = client.request(
                method, settings.kaiwu_assist_control_origin + BRIDGE_PATH + path, headers=headers, json=payload,
            )
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise AppError(502, "ASSIST_BRIDGE_UNAVAILABLE", "暂时无法连接设备协助后台") from None
    if isinstance(data, dict) and 400 <= response.status_code < 500:
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            code = str(error.get("code", ""))
            if code.startswith("ASSIST_") or code in SOURCE_ERRORS:
                raise AppError(response.status_code, code, error["message"])
    if not response.is_success or not isinstance(data, dict):
        raise AppError(502, "ASSIST_BRIDGE_UNAVAILABLE", "设备协助后台暂时无法处理请求")
    return data


def _imported_id(service, session_id, user):
    row = service.db.one(
        "SELECT connection_id FROM kaiwu_assist_workbench_imports WHERE session_id=? AND owner_id=?",
        (session_id, user["id"]),
    )
    return row["connection_id"] if row else None


def _connection(service, connection_id, user):
    return connection_view(service.require_resource("connections", connection_id, user, edit=True), user)


def create_kaiwu_assist_workbench_router(service_dependency, admin):
    router = APIRouter(prefix="/api/v1/connections/kaiwu-assist", tags=["MCP assistance"])

    @router.get("/devices")
    def devices(service=Depends(service_dependency), user=Depends(admin)):
        return _forward(service, user["id"], "GET", "/devices")

    @router.post("/sessions", status_code=201)
    def offer(data: OfferInput, service=Depends(service_dependency), user=Depends(admin)):
        return _forward(service, user["id"], "POST", "/sessions", data.model_dump())

    @router.get("/sessions/{session_id}")
    def status(session_id: SessionID, service=Depends(service_dependency), user=Depends(admin)):
        result = _forward(service, user["id"], "GET", f"/sessions/{session_id}")
        connection_id = _imported_id(service, session_id, user)
        if connection_id:
            result["imported_connection_id"] = connection_id
        return result

    @router.post("/sessions/{session_id}/claim")
    def claim(session_id: SessionID, service=Depends(service_dependency), user=Depends(admin)):
        result = _forward(service, user["id"], "POST", f"/sessions/{session_id}/claim")
        connection_id = _imported_id(service, session_id, user)
        if connection_id:
            result["imported_connection_id"] = connection_id
        return result

    @router.post("/sessions/{session_id}/jobs")
    def queue(session_id: SessionID, data: JobInput, service=Depends(service_dependency), user=Depends(admin)):
        return _forward(service, user["id"], "POST", f"/sessions/{session_id}/jobs", data.model_dump(exclude_none=True))

    @router.post("/sessions/{session_id}/stop")
    def end(session_id: SessionID, service=Depends(service_dependency), user=Depends(admin)):
        return _forward(service, user["id"], "POST", f"/sessions/{session_id}/stop")

    @router.post("/sessions/{session_id}/import")
    def import_source(session_id: SessionID, data: ImportInput, service=Depends(service_dependency), user=Depends(admin)):
        connection_id = _imported_id(service, session_id, user)
        if connection_id:
            return {"connection": _connection(service, connection_id, user)}
        issued = _forward(service, user["id"], "POST", f"/sessions/{session_id}/import", data.model_dump())
        config = issued.get("mcp_config")
        if not isinstance(config, dict):
            raise AppError(502, "ASSIST_BRIDGE_UNAVAILABLE", "设备协助后台未返回可用的 MCP 配置")
        with service.db.transaction():
            connection_id = _imported_id(service, session_id, user)
            if connection_id:
                return {"connection": _connection(service, connection_id, user)}
            plan = plan_mcp_import(ImportRequest(config=config, scope=data.scope), service, user)
            if len(plan.connections) != 1:
                raise AppError(502, "ASSIST_BRIDGE_UNAVAILABLE", "设备协助后台未返回单个 MCP 来源")
            connection = service.save_connection({**plan.connections[0].create_fields(), "scope": data.scope}, user=user)
            service.db.execute(
                "INSERT INTO kaiwu_assist_workbench_imports(session_id,owner_id,connection_id) VALUES(?,?,?)",
                (session_id, user["id"], connection["id"]),
            )
        return {"connection": connection}

    return router
