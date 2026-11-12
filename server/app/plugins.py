"""Administrator-reviewed plugin manifest registry.

The registry records source, version, digest, license and declared permissions.
It does not download or execute third-party code. Runtime integrations must
consume an approved manifest and independently enforce each declared scope.
"""
from __future__ import annotations

import json
import re
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import Field

from .db import dump, now, uid
from .models import InputModel
from .security import AppError


Permission = Literal[
    "workspace.files.read",
    "workspace.files.write",
    "knowledge.read",
    "web.read",
    "mcp.connection.read",
    "artifact.write",
    "memory.read",
]
PERMITTED_LICENSES = frozenset({
    "apache-2.0",
    "bsd-2-clause",
    "bsd-3-clause",
    "isc",
    "mit",
})


class PluginRegister(InputModel):
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
    source_url: str = Field(min_length=1, max_length=2048)
    license: str = Field(min_length=1, max_length=64)
    package_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    declared_permissions: list[Permission] = Field(default_factory=list, max_length=32)
    review_note: str = Field(default="", max_length=2000)


class PluginApproval(InputModel):
    review_note: str = Field(default="", max_length=2000)


class PluginRevocation(InputModel):
    review_note: str = Field(default="", max_length=2000)


def _validate_source(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise AppError(422, "PLUGIN_SOURCE_INVALID", "插件来源必须是 HTTPS 地址，不能内嵌凭据")
    return url


def _license_key(value: str) -> str:
    return re.sub(r"\s+", "-", value.strip().lower())


def plugin_view(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "version": row["version"],
        "source_url": row["source_url"],
        "license": row["license"],
        "package_sha256": row["package_sha256"],
        "declared_permissions": json.loads(row["declared_permissions"]),
        "status": row["status"],
        "review_note": row["review_note"],
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "approved_at": row["approved_at"],
        "revoked_at": row["revoked_at"],
    }


def _audit(service, plugin_id: str, action: str, actor_id: str, details: dict) -> None:
    service.db.execute(
        "INSERT INTO plugin_registry_events(plugin_id,action,actor_id,details,created_at) VALUES(?,?,?,?,?)",
        (plugin_id, action, actor_id, dump(details), now()),
    )


def register_plugin(service, body: PluginRegister, user: dict) -> dict:
    source = _validate_source(body.source_url)
    name = body.name.strip()
    if not name:
        raise AppError(422, "PLUGIN_NAME_INVALID", "插件名称不能为空")
    existing = service.db.one(
        "SELECT * FROM plugin_registry WHERE name=? AND version=?",
        (name, body.version),
    )
    if existing:
        if existing["package_sha256"] == body.package_sha256:
            raise AppError(409, "PLUGIN_ALREADY_REGISTERED", "相同版本与摘要的插件已经登记")
        raise AppError(409, "PLUGIN_VERSION_IMMUTABLE", "同一插件版本不能更换来源摘要，请发布新版本")
    plugin_id, stamp = uid(), now()
    service.db.execute(
        "INSERT INTO plugin_registry(id,name,version,source_url,license,package_sha256,declared_permissions,status,review_note,created_by,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            plugin_id,
            name,
            body.version,
            source,
            body.license.strip(),
            body.package_sha256,
            dump(sorted(set(body.declared_permissions))),
            "pending",
            body.review_note,
            user["id"],
            stamp,
            stamp,
        ),
    )
    _audit(service, plugin_id, "registered", user["id"], {
        "version": body.version,
        "package_sha256": body.package_sha256,
        "declared_permissions": sorted(set(body.declared_permissions)),
        "review_note": body.review_note,
    })
    return plugin_view(service.require("plugin_registry", plugin_id))


def approve_plugin(service, plugin_id: str, body: PluginApproval, user: dict) -> dict:
    row = service.require("plugin_registry", plugin_id)
    if row["status"] != "pending":
        raise AppError(409, "PLUGIN_STATE_CONFLICT", "只有待审核插件可以批准")
    if _license_key(row["license"]) not in PERMITTED_LICENSES:
        raise AppError(422, "PLUGIN_LICENSE_DENIED", "许可证不在已批准的宽松许可证清单内")
    if not re.fullmatch(r"[0-9a-f]{64}", row["package_sha256"]):
        raise AppError(422, "PLUGIN_DIGEST_INVALID", "插件摘要无效")
    stamp = now()
    updated = service.db.execute(
        "UPDATE plugin_registry SET status='approved',review_note=?,revision=revision+1,updated_at=?,approved_by=?,approved_at=? "
        "WHERE id=? AND status='pending'",
        (body.review_note, stamp, user["id"], stamp, plugin_id),
    )
    if updated.rowcount != 1:
        raise AppError(409, "PLUGIN_STATE_CONFLICT", "插件状态已经变化")
    _audit(service, plugin_id, "approved", user["id"], {
        "license": row["license"],
        "package_sha256": row["package_sha256"],
        "declared_permissions": json.loads(row["declared_permissions"]),
        "review_note": body.review_note,
    })
    return plugin_view(service.require("plugin_registry", plugin_id))


def revoke_plugin(service, plugin_id: str, body: PluginRevocation, user: dict) -> dict:
    row = service.require("plugin_registry", plugin_id)
    if row["status"] == "revoked":
        return plugin_view(row)
    stamp = now()
    service.db.execute(
        "UPDATE plugin_registry SET status='revoked',review_note=?,revision=revision+1,updated_at=?,revoked_by=?,revoked_at=? WHERE id=?",
        (body.review_note, stamp, user["id"], stamp, plugin_id),
    )
    _audit(service, plugin_id, "revoked", user["id"], {"review_note": body.review_note})
    return plugin_view(service.require("plugin_registry", plugin_id))


def create_plugin_router(service_dependency, admin):
    router = APIRouter(prefix="/api/v1/admin/plugins", tags=["Plugin governance"])

    @router.get("")
    def listing(user=Depends(admin), service=Depends(service_dependency)):
        return [plugin_view(row) for row in service.db.all("SELECT * FROM plugin_registry ORDER BY updated_at DESC,name")]

    @router.post("", status_code=201)
    def register(body: PluginRegister, user=Depends(admin), service=Depends(service_dependency)):
        return register_plugin(service, body, user)

    @router.get("/{item_id}")
    def get(item_id: str, user=Depends(admin), service=Depends(service_dependency)):
        return plugin_view(service.require("plugin_registry", item_id))

    @router.post("/{item_id}/approve")
    def approve(item_id: str, body: PluginApproval, user=Depends(admin), service=Depends(service_dependency)):
        return approve_plugin(service, item_id, body, user)

    @router.post("/{item_id}/revoke")
    def revoke(item_id: str, body: PluginRevocation, user=Depends(admin), service=Depends(service_dependency)):
        return revoke_plugin(service, item_id, body, user)

    @router.get("/{item_id}/events")
    def events(item_id: str, user=Depends(admin), service=Depends(service_dependency)):
        service.require("plugin_registry", item_id)
        return [
            {
                "id": row["id"],
                "action": row["action"],
                "actor_id": row["actor_id"],
                "details": json.loads(row["details"]),
                "created_at": row["created_at"],
            }
            for row in service.db.all("SELECT * FROM plugin_registry_events WHERE plugin_id=? ORDER BY id DESC", (item_id,))
        ]

    return router
