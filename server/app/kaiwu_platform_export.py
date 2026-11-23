"""One-time handoff from a connected device to a signed-in platform admin."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, field_validator

from .db import now
from .kaiwu_control import (
    SOURCE_ID_RE,
    IntegrationCreateRequest,
    StrictModel,
    _available_native_source,
    _hash_secret,
    _issue_platform_integration,
)
from .mcp_import import ImportRequest, parse_mcp_config, plan_mcp_import
from .security import AppError
from .service import connection_view


class SourceRequest(StrictModel):
    source_id: str = Field(pattern=SOURCE_ID_RE.pattern)


class HandoffRequest(StrictModel):
    handoff_code: str = Field(pattern=r"^kw_link_[A-Za-z0-9_-]{43}$")


class RedeemRequest(HandoffRequest):
    action: Literal["copy", "import"]
    scope: Literal["private", "team"] = "private"
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def valid_name(cls, name: str) -> str:
        return IntegrationCreateRequest.valid_display_name(name)


def add_device_handoff(router: APIRouter, service_dependency, device):
    @router.post("/v1/connectors/{connector_id}/platform-handoff", status_code=201)
    def create_handoff(
        connector_id: str,
        data: SourceRequest,
        request: Request,
        response: Response,
        device_ctx=Depends(device),
        service=Depends(service_dependency),
    ):
        if device_ctx["connector"]["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        handoff_code = "kw_link_" + secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with service.db.transaction():
            connector = service.db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
            if not connector:
                raise AppError(403, "DEVICE_REVOKED", "设备身份已失效")
            source = _available_native_source(request, service, connector, data.source_id)
            service.db.execute("DELETE FROM kaiwu_platform_handoffs WHERE expires_at<=?", (now(),))
            service.db.execute(
                "DELETE FROM kaiwu_platform_handoffs WHERE connector_id=? AND source_id=? AND redeemed_by IS NULL",
                (connector_id, data.source_id),
            )
            service.db.execute(
                "INSERT INTO kaiwu_platform_handoffs(ticket_hash,tenant_id,connector_id,source_id,source_revision,expires_at) VALUES(?,?,?,?,?,?)",
                (_hash_secret(handoff_code), connector["tenant_id"], connector_id, data.source_id, source["revision"], expires_at),
            )
        response.headers["Cache-Control"] = "no-store"
        return {"handoff_code": handoff_code}


def _lookup_handoff(service, code: str) -> dict:
    ticket = service.db.one("SELECT * FROM kaiwu_platform_handoffs WHERE ticket_hash=?", (_hash_secret(code),))
    if not ticket or ticket["expires_at"] <= now():
        raise AppError(404, "HANDOFF_EXPIRED", "此导入链接已过期，请在连接器上重新生成")
    return ticket


def _current_handoff(request, service, ticket):
    connector = service.db.one("SELECT * FROM kaiwu_connectors WHERE id=? AND tenant_id=?", (ticket["connector_id"], ticket["tenant_id"]))
    if not connector:
        raise AppError(404, "HANDOFF_EXPIRED", "此导入链接已失效，请在连接器上重新生成")
    source = _available_native_source(request, service, connector, ticket["source_id"])
    if ticket["source_revision"] != source["revision"]:
        raise AppError(409, "HANDOFF_SOURCE_CHANGED", "业务来源已更新，请在连接器上重新生成导入链接")
    return connector, source


def _already_redeemed() -> AppError:
    return AppError(409, "HANDOFF_REDEEMED", "此导入链接已使用，请在连接器上重新生成")


def create_kaiwu_platform_export_router(service_dependency, admin):
    router = APIRouter(prefix="/api/v1/connections/kaiwu-handoff", tags=["MCP configuration"])

    @router.post("/preview")
    def preview(data: HandoffRequest, request: Request, service=Depends(service_dependency), user=Depends(admin)):
        ticket = _lookup_handoff(service, data.handoff_code)
        if ticket["redeemed_by"]:
            if ticket["redeemed_by"] == user["id"] and ticket["imported_connection_id"]:
                row = service.require_resource("connections", ticket["imported_connection_id"], user, edit=True)
                return {"connection": connection_view(row, user)}
            raise _already_redeemed()
        connector, source = _current_handoff(request, service, ticket)
        return {
            "tenant_name": connector["tenant_name"],
            "name": source["display_name"][:100],
            "allowed_tools": json.loads(source["allowed_tools"]),
        }

    @router.post("/redeem")
    def redeem(data: RedeemRequest, request: Request, response: Response, service=Depends(service_dependency), user=Depends(admin)):
        response.headers["Cache-Control"] = "no-store"
        with service.db.transaction():
            ticket = _lookup_handoff(service, data.handoff_code)
            if ticket["redeemed_by"]:
                if data.action == "import" and ticket["redeemed_by"] == user["id"] and ticket["imported_connection_id"]:
                    row = service.require_resource("connections", ticket["imported_connection_id"], user, edit=True)
                    return {"connection": connection_view(row, user)}
                raise _already_redeemed()
            connector, source = _current_handoff(request, service, ticket)
            # The platform parser enforces the same connection name and allowed
            # public host as a pasted MCP configuration.
            integration = IntegrationCreateRequest(
                tenant_id=ticket["tenant_id"], connector_id=connector["id"],
                display_name=data.name, source_ids=[source["source_id"]],
            )
            issued = _issue_platform_integration(service, integration, [source], user["id"])
            connection = None
            if data.action == "import":
                plan = plan_mcp_import(ImportRequest(config=issued["mcp_config"], scope=data.scope), service, user)
                connection = service.save_connection({**plan.connections[0].create_fields(), "scope": data.scope}, user=user)
            else:
                parse_mcp_config(issued["mcp_config"], service.settings.mcp_hosts)
            service.db.execute(
                "UPDATE kaiwu_platform_handoffs SET redeemed_by=?,imported_connection_id=? WHERE ticket_hash=?",
                (user["id"], connection["id"] if connection else None, ticket["ticket_hash"]),
            )
            return {"connection": connection} if connection else {"mcp_config_json": issued["mcp_config_json"]}

    return router
