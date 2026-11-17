"""Fail-closed control plane for the Kaiwu intranet MCP connector.

This module implements the synthetic-testable portion of the cloud control
plane. It deliberately does not implement Edge, relay/visitor forwarding,
Token Exchange, grants or identity mapping: those remain separate acceptance
gates and must never be inferred from a successful enrollment or heartbeat.

Security properties enforced here:

* the whole surface is disabled by default;
* administrator and device credentials use different authentication paths;
* device requests require an Ed25519 signature over the exact method, path,
  body digest, timestamp and one-time nonce;
* enrollment codes and device tokens are stored only as hashes;
* source registration accepts only opaque metadata and never a customer URL,
  host, port, query string or credential;
* drain and revoke increment a persisted revision and invalidate device
  requests immediately.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings
from .db import Database, dump, now, uid
from .security import AppError


ADMIN_PREFIX = "/api/kaiwu"
MAX_SOURCES = 16
MAX_TOOL_COUNT = 64
CLOCK_SKEW_SECONDS = 60
SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{32}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollmentRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    tenant_name: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator("tenant_id")
    @classmethod
    def valid_tenant_id(cls, value: str) -> str:
        value = value.strip()
        if not TENANT_RE.fullmatch(value):
            raise ValueError("tenant_id contains unsupported characters")
        return value

    @field_validator("tenant_name")
    @classmethod
    def valid_tenant_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("tenant_name is invalid")
        return value


class RedeemRequest(StrictModel):
    code: str = Field(min_length=16, max_length=256)
    device_public_key: str = Field(min_length=40, max_length=128)


class SourceRegistration(StrictModel):
    source_id: str = Field(min_length=36, max_length=36)
    display_name: str = Field(min_length=1, max_length=128)
    allowed_tools: list[str] = Field(min_length=1, max_length=MAX_TOOL_COUNT)
    tool_schema_sha256: str = Field(min_length=64, max_length=64)
    target_binding_sha256: str = Field(min_length=64, max_length=64)
    transport: str = Field(default="streamable_http", max_length=32)

    @field_validator("source_id")
    @classmethod
    def valid_source_id(cls, value: str) -> str:
        if not SOURCE_ID_RE.fullmatch(value):
            raise ValueError("source_id must be an opaque src_ identifier")
        return value

    @field_validator("display_name")
    @classmethod
    def valid_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("display_name is invalid")
        return value

    @field_validator("allowed_tools")
    @classmethod
    def valid_tools(cls, values: list[str]) -> list[str]:
        normalized = sorted({str(value).strip() for value in values})
        if not normalized or any(not TOOL_NAME_RE.fullmatch(value) for value in normalized):
            raise ValueError("allowed_tools contains an invalid name")
        return normalized

    @field_validator("tool_schema_sha256", "target_binding_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        value = value.lower()
        if not HASH_RE.fullmatch(value):
            raise ValueError("digest must be lowercase SHA-256 hex")
        return value

    @field_validator("transport")
    @classmethod
    def valid_transport(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"streamable_http", "legacy_sse"}:
            raise ValueError("transport is not supported")
        return value


class HeartbeatRequest(StrictModel):
    state: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=256)
    observed_at: datetime
    source_ids: list[str] = Field(max_length=MAX_SOURCES)
    last_error: str = Field(max_length=1000)

    @field_validator("state")
    @classmethod
    def valid_state(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {
            "UNPAIRED", "CONFIGURING", "CONNECTING", "READY", "PAUSED",
            "DEGRADED_RELAY", "DEGRADED_UPSTREAM", "REVOKED",
        }:
            raise ValueError("state is not supported")
        return value

    @field_validator("source_ids")
    @classmethod
    def valid_source_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values) or any(not SOURCE_ID_RE.fullmatch(value) for value in values):
            raise ValueError("source_ids must contain unique opaque source IDs")
        return values


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    value = value.strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _device_public_key(value: str) -> tuple[str, Ed25519PublicKey]:
    raw = _decode_base64url(value)
    if len(raw) != 32:
        raise ValueError("device public key must be a raw Ed25519 key")
    key_id = _base64url(hashlib.sha256(raw).digest())
    return key_id, Ed25519PublicKey.from_public_bytes(raw)


def _derive_secret(settings: Settings, purpose: str, value: str) -> str:
    """Derive a trial relay/STCP secret without persisting plaintext.

    The dedicated administrator token is an operator-held root secret. It is
    never returned or logged. This keeps testable trial material deterministic
    while database dumps contain only derivation hashes.
    """
    root = settings.kaiwu_control_token()
    if not root:
        raise AppError(503, "CONTROL_PLANE_DISABLED", "内网连接器控制面尚未启用")
    digest = hmac.new(root.encode("utf-8"), f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).digest()
    return _base64url(digest)


def _sanitize_last_error(value: str) -> str:
    value = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    patterns = (
        (r"(?i)(authorization\s*:\s*bearer)\s+\S+", r"\1 [REDACTED]"),
        (r"(?i)(bearer)\s+[A-Za-z0-9._~+/=-]+", r"\1 [REDACTED]"),
        (r"(?i)((?:token|secret|password|hmac)\s*[=:]\s*)\S+", r"\1[REDACTED]"),
        (r"-----BEGIN [^-]+-----", "[REDACTED]"),
    )
    for pattern, replacement in patterns:
        value = re.sub(pattern, replacement, value)
    return value[:1000]


def _json_object(value: str) -> dict:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _audit(
    db: Database,
    *,
    tenant_id: str,
    connector_id: str | None,
    source_id: str | None,
    actor_type: str,
    actor_id: str,
    action: str,
    outcome: str,
    trace_id: str,
    detail: dict | None = None,
) -> None:
    db.execute(
        "INSERT INTO kaiwu_control_audit"
        "(tenant_id,connector_id,source_id,actor_type,actor_id,action,outcome,detail,trace_id,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            tenant_id,
            connector_id,
            source_id,
            actor_type,
            actor_id,
            action,
            outcome,
            dump(detail or {}),
            trace_id,
            now(),
        ),
    )


def _connector_view(row: dict) -> dict:
    return {
        "connector_id": row["id"],
        "tenant_id": row["tenant_id"],
        "tenant_name": row["tenant_name"],
        "revision": row["revision"],
        "draining": bool(row["draining"]),
        "revoked": bool(row["revoked"]),
        "device_key_id": row["device_key_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "revoked_at": row["revoked_at"],
    }


def _source_view(row: dict, settings: Settings) -> dict:
    return {
        "source_id": row["source_id"],
        "display_name": row["display_name"],
        "allowed_tools": json.loads(row["allowed_tools"]),
        "tool_schema_sha256": row["tool_schema_sha256"],
        "target_binding_sha256": row["target_binding_sha256"],
        "transport": row["transport"],
        "allow_users": [settings.kaiwu_control_edge_user],
        "revision": row["revision"],
    }


def create_kaiwu_control_router(service_getter: Callable) -> APIRouter:
    router = APIRouter(prefix=ADMIN_PREFIX, tags=["kaiwu-control"])

    def service(request: Request):
        return service_getter(request)

    def settings_for(service_obj) -> Settings:
        return service_obj.settings

    def guard(service_obj) -> Settings:
        settings = settings_for(service_obj)
        if not settings.kaiwu_control_enabled:
            raise AppError(503, "CONTROL_PLANE_DISABLED", "内网连接器控制面尚未启用")
        if not settings.kaiwu_control_token():
            raise AppError(503, "CONTROL_PLANE_DISABLED", "内网连接器控制面缺少独立管理员凭据")
        if not settings.kaiwu_control_relay_address or settings.kaiwu_control_relay_port <= 0:
            raise AppError(503, "CONTROL_PLANE_DISABLED", "内网连接器控制面缺少已配置的中继端点")
        return settings

    def admin(request: Request, service_obj=Depends(service)) -> dict:
        settings = guard(service_obj)
        supplied = request.headers.get("authorization", "")
        scheme, _, token = supplied.partition(" ")
        if scheme.lower() != "bearer" or not token or not hmac.compare_digest(token, settings.kaiwu_control_token()):
            if token and service_obj.db.one(
                "SELECT 1 FROM kaiwu_connectors WHERE device_token_hash=?",
                (_hash_secret(token),),
            ):
                raise AppError(403, "CREDENTIAL_PLANE_DENIED", "设备凭据不能调用管理员接口")
            raise AppError(401, "ADMIN_UNAUTHENTICATED", "控制面管理员凭据无效")
        return {"actor_id": "control-admin", "settings": settings}

    async def device(request: Request, service_obj=Depends(service)) -> dict:
        settings = guard(service_obj)
        supplied = request.headers.get("authorization", "")
        scheme, _, token = supplied.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise AppError(401, "DEVICE_UNAUTHENTICATED", "设备身份无效")
        if hmac.compare_digest(token, settings.kaiwu_control_token()):
            raise AppError(403, "CREDENTIAL_PLANE_DENIED", "管理员凭据不能调用设备接口")
        connector = service_obj.db.one(
            "SELECT * FROM kaiwu_connectors WHERE device_token_hash=?",
            (_hash_secret(token),),
        )
        if not connector or connector["revoked"]:
            raise AppError(403, "DEVICE_REVOKED", "设备身份已失效")
        if request.headers.get("x-kaiwu-device-key", "") != connector["device_key_id"]:
            raise AppError(403, "DEVICE_SIGNATURE_INVALID", "设备签名无效")

        timestamp_raw = request.headers.get("x-kaiwu-timestamp", "")
        nonce = request.headers.get("x-kaiwu-nonce", "")
        signature_raw = request.headers.get("x-kaiwu-signature", "")
        try:
            timestamp = int(timestamp_raw)
        except (TypeError, ValueError):
            raise AppError(403, "DEVICE_SIGNATURE_INVALID", "设备签名无效") from None
        if abs(int(time.time()) - timestamp) > CLOCK_SKEW_SECONDS:
            raise AppError(403, "DEVICE_SIGNATURE_EXPIRED", "设备签名已过期")
        if not NONCE_RE.fullmatch(nonce):
            raise AppError(403, "DEVICE_SIGNATURE_INVALID", "设备签名无效")

        body = await request.body()
        message = "\n".join((
            timestamp_raw,
            nonce,
            request.method.upper(),
            request.url.path,
            hashlib.sha256(body).hexdigest(),
        )).encode("utf-8")
        try:
            signature = _decode_base64url(signature_raw)
            public_key = _device_public_key(connector["device_public_key"])[1]
            public_key.verify(signature, message)
        except (ValueError, InvalidSignature):
            raise AppError(403, "DEVICE_SIGNATURE_INVALID", "设备签名无效") from None

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=CLOCK_SKEW_SECONDS + 5)).isoformat()
        service_obj.db.execute("DELETE FROM kaiwu_device_nonces WHERE expires_at<=?", (now(),))
        try:
            service_obj.db.execute(
                "INSERT INTO kaiwu_device_nonces(connector_id,nonce,expires_at,created_at) VALUES(?,?,?,?)",
                (connector["id"], nonce, expires_at, now()),
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise AppError(403, "DEVICE_SIGNATURE_REPLAYED", "设备签名不可重复使用") from None
            raise
        return {"connector": connector, "settings": settings}

    @router.get("/v1/health")
    def health(service_obj=Depends(service)):
        enabled = bool(
            service_obj.settings.kaiwu_control_enabled
            and service_obj.settings.kaiwu_control_token()
            and service_obj.settings.kaiwu_control_relay_address
            and service_obj.settings.kaiwu_control_relay_port > 0
        )
        return {"status": "enabled" if enabled else "disabled", "version": "control-plane-1"}

    @router.post("/v1/enrollments")
    def create_enrollment(
        data: EnrollmentRequest,
        request: Request,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        settings: Settings = admin_ctx["settings"]
        enrollment_id = "enr_" + uid()
        code = "kw_" + secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=data.ttl_seconds)).isoformat()
        service_obj.db.execute(
            "INSERT INTO kaiwu_enrollments"
            "(id,tenant_id,tenant_name,code_hash,expires_at,used_at,connector_id,created_by,created_at)"
            " VALUES(?,?,?,?,?,NULL,NULL,?,?)",
            (
                enrollment_id,
                data.tenant_id,
                data.tenant_name,
                _hash_secret(code),
                expires_at,
                admin_ctx["actor_id"],
                now(),
            ),
        )
        trace_id = "tr_" + uid()
        _audit(
            service_obj.db,
            tenant_id=data.tenant_id,
            connector_id=None,
            source_id=None,
            actor_type="admin",
            actor_id=admin_ctx["actor_id"],
            action="enrollment.created",
            outcome="allow",
            trace_id=trace_id,
            detail={"enrollment_id": enrollment_id, "expires_at": expires_at},
        )
        return {
            "enrollment_id": enrollment_id,
            "code": code,
            "expires_at": expires_at,
            "trace_id": trace_id,
        }

    @router.post("/v1/enrollments/redeem")
    def redeem(data: RedeemRequest, service_obj=Depends(service)):
        guard(service_obj)
        try:
            key_id, _ = _device_public_key(data.device_public_key)
        except ValueError:
            raise AppError(400, "DEVICE_PUBLIC_KEY_INVALID", "设备公钥无效") from None
        settings: Settings = service_obj.settings
        connector_id = "conn_" + uid()
        device_token = "dvc_" + secrets.token_urlsafe(32)
        relay_token = _derive_secret(settings, "relay", connector_id)
        trace_id = "tr_" + uid()
        with service_obj.db.transaction():
            enrollment = service_obj.db.one(
                "SELECT * FROM kaiwu_enrollments WHERE code_hash=?",
                (_hash_secret(data.code),),
            )
            if not enrollment or enrollment["used_at"]:
                raise AppError(409, "ENROLLMENT_INVALID", "接入码无效或已使用")
            if enrollment["expires_at"] <= now():
                raise AppError(409, "ENROLLMENT_EXPIRED", "接入码已过期")
            service_obj.db.execute(
                "UPDATE kaiwu_enrollments SET used_at=?,connector_id=? WHERE id=? AND used_at IS NULL",
                (now(), connector_id, enrollment["id"]),
            )
            current = service_obj.db.one("SELECT used_at,connector_id FROM kaiwu_enrollments WHERE id=?", (enrollment["id"],))
            if current["connector_id"] != connector_id:
                raise AppError(409, "ENROLLMENT_INVALID", "接入码无效或已使用")
            service_obj.db.execute(
                "INSERT INTO kaiwu_connectors"
                "(id,tenant_id,tenant_name,device_public_key,device_key_id,device_token_hash,"
                "relay_token_hash,revision,draining,revoked,created_at,updated_at,revoked_at)"
                " VALUES(?,?,?,?,?,?,?,1,0,0,?,?,NULL)",
                (
                    connector_id,
                    enrollment["tenant_id"],
                    enrollment["tenant_name"],
                    data.device_public_key,
                    key_id,
                    _hash_secret(device_token),
                    _hash_secret(relay_token),
                    now(),
                    now(),
                ),
            )
            _audit(
                service_obj.db,
                tenant_id=enrollment["tenant_id"],
                connector_id=connector_id,
                source_id=None,
                actor_type="device",
                actor_id=key_id,
                action="enrollment.redeemed",
                outcome="allow",
                trace_id=trace_id,
                detail={"enrollment_id": enrollment["id"]},
            )
        return {
            "connector_id": connector_id,
            "tenant_id": enrollment["tenant_id"],
            "tenant_name": enrollment["tenant_name"],
            "device_token": device_token,
            "device_key_id": key_id,
            "relay": {
                "address": settings.kaiwu_control_relay_address,
                "port": settings.kaiwu_control_relay_port,
                "user": connector_id,
                "token": relay_token,
                "protocol": settings.kaiwu_control_relay_protocol,
                "tls": {
                    "enabled": settings.kaiwu_control_relay_tls_enabled,
                    "server_name": settings.kaiwu_control_relay_tls_server_name,
                },
            },
            "trace_id": trace_id,
        }

    @router.get("/v1/connectors")
    def list_connectors(admin_ctx=Depends(admin), service_obj=Depends(service)):
        del admin_ctx
        return {
            "connectors": [
                _connector_view(row)
                for row in service_obj.db.all("SELECT * FROM kaiwu_connectors ORDER BY created_at")
            ]
        }

    @router.get("/v1/connectors/{connector_id}/desired-state")
    def desired_state(
        connector_id: str,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        settings: Settings = device_ctx["settings"]
        sources = [
            _source_view(row, settings)
            for row in service_obj.db.all(
                "SELECT * FROM kaiwu_sources WHERE connector_id=? ORDER BY source_id",
                (connector_id,),
            )
        ]
        return {
            "revision": connector["revision"],
            "relay": {
                "address": settings.kaiwu_control_relay_address,
                "port": settings.kaiwu_control_relay_port,
                "user": connector_id,
                "protocol": settings.kaiwu_control_relay_protocol,
                "tls": {
                    "enabled": settings.kaiwu_control_relay_tls_enabled,
                    "server_name": settings.kaiwu_control_relay_tls_server_name,
                },
            },
            "sources": sources,
            "revoked": bool(connector["revoked"]),
            "draining": bool(connector["draining"]),
        }

    @router.get("/v1/connectors/{connector_id}/transport-secrets")
    def transport_secrets(
        connector_id: str,
        response: Response,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        settings: Settings = device_ctx["settings"]
        sources = []
        for row in service_obj.db.all(
            "SELECT * FROM kaiwu_sources WHERE connector_id=? ORDER BY source_id",
            (connector_id,),
        ):
            source = _source_view(row, settings)
            source["stcp_secret"] = _derive_secret(
                settings, "stcp", f"{connector_id}:{row['source_id']}:{row['revision']}"
            )
            sources.append(source)
        response.headers["Cache-Control"] = "no-store"
        return {
            "revision": connector["revision"],
            "relay": {
                "address": settings.kaiwu_control_relay_address,
                "port": settings.kaiwu_control_relay_port,
                "user": connector_id,
                "token": _derive_secret(settings, "relay", connector_id),
                "protocol": settings.kaiwu_control_relay_protocol,
                "tls": {
                    "enabled": settings.kaiwu_control_relay_tls_enabled,
                    "server_name": settings.kaiwu_control_relay_tls_server_name,
                },
            },
            "sources": sources,
            "revoked": False,
            "draining": bool(connector["draining"]),
        }

    @router.post("/v1/sources")
    def register_source(
        data: SourceRegistration,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        existing = service_obj.db.one(
            "SELECT * FROM kaiwu_sources WHERE connector_id=? AND source_id=?",
            (connector["id"], data.source_id),
        )
        desired = {
            "display_name": data.display_name,
            "allowed_tools": dump(data.allowed_tools),
            "tool_schema_sha256": data.tool_schema_sha256,
            "target_binding_sha256": data.target_binding_sha256,
            "transport": data.transport,
        }
        trace_id = "tr_" + uid()
        if existing:
            current = {key: existing[key] for key in desired}
            if current != desired:
                raise AppError(409, "SOURCE_CONFIG_CONFLICT", "同一来源编号的配置发生变化，请重新审核")
            return {"source": _source_view(existing, device_ctx["settings"]), "revision": connector["revision"], "trace_id": trace_id}
        count = service_obj.db.one(
            "SELECT COUNT(*) AS n FROM kaiwu_sources WHERE connector_id=?",
            (connector["id"],),
        )["n"]
        if count >= MAX_SOURCES:
            raise AppError(409, "SOURCE_LIMIT_EXCEEDED", "单个连接器最多允许 16 个来源")
        derived = _derive_secret(
            device_ctx["settings"], "stcp", f"{connector['id']}:{data.source_id}:1"
        )
        with service_obj.db.transaction():
            service_obj.db.execute(
                "INSERT INTO kaiwu_sources"
                "(connector_id,tenant_id,source_id,display_name,allowed_tools,tool_schema_sha256,"
                "target_binding_sha256,transport,stcp_secret,revision,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,1,?,?)",
                (
                    connector["id"],
                    connector["tenant_id"],
                    data.source_id,
                    data.display_name,
                    dump(data.allowed_tools),
                    data.tool_schema_sha256,
                    data.target_binding_sha256,
                    data.transport,
                    _hash_secret(derived),
                    now(),
                    now(),
                ),
            )
            service_obj.db.execute(
                "UPDATE kaiwu_connectors SET revision=revision+1,updated_at=? WHERE id=?",
                (now(), connector["id"]),
            )
            _audit(
                service_obj.db,
                tenant_id=connector["tenant_id"],
                connector_id=connector["id"],
                source_id=data.source_id,
                actor_type="device",
                actor_id=connector["device_key_id"],
                action="source.registered",
                outcome="allow",
                trace_id=trace_id,
                detail={
                    "allowed_tools": data.allowed_tools,
                    "tool_schema_sha256": data.tool_schema_sha256,
                    "target_binding_sha256": data.target_binding_sha256,
                },
            )
        row = service_obj.db.one(
            "SELECT * FROM kaiwu_sources WHERE connector_id=? AND source_id=?",
            (connector["id"], data.source_id),
        )
        revision = service_obj.db.one("SELECT revision FROM kaiwu_connectors WHERE id=?", (connector["id"],))["revision"]
        return {"source": _source_view(row, device_ctx["settings"]), "revision": revision, "trace_id": trace_id}

    @router.put("/v1/sources/{source_id}/registration")
    def update_source(
        source_id: str,
        data: SourceRegistration,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        if source_id != data.source_id:
            raise AppError(409, "SOURCE_ID_MISMATCH", "来源编号与请求体不一致")
        existing = service_obj.db.one(
            "SELECT * FROM kaiwu_sources WHERE connector_id=? AND source_id=?",
            (device_ctx["connector"]["id"], source_id),
        )
        if not existing:
            raise AppError(404, "SOURCE_NOT_FOUND", "来源尚未登记")
        desired = {
            "display_name": data.display_name,
            "allowed_tools": dump(data.allowed_tools),
            "tool_schema_sha256": data.tool_schema_sha256,
            "target_binding_sha256": data.target_binding_sha256,
            "transport": data.transport,
        }
        if {key: existing[key] for key in desired} == desired:
            return {
                "source": _source_view(existing, device_ctx["settings"]),
                "revision": device_ctx["connector"]["revision"],
            }
        raise AppError(409, "SOURCE_CONFIG_CONFLICT", "来源配置变化必须重新审核，不能原地覆盖")

    @router.post("/v1/connectors/{connector_id}/heartbeat")
    def heartbeat(
        connector_id: str,
        data: HeartbeatRequest,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        known = {
            row["source_id"]
            for row in service_obj.db.all(
                "SELECT source_id FROM kaiwu_sources WHERE connector_id=?",
                (connector_id,),
            )
        }
        if not set(data.source_ids).issubset(known):
            raise AppError(409, "HEARTBEAT_SOURCE_UNKNOWN", "心跳包含未登记来源")
        observed = data.observed_at
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        observed_at = observed.astimezone(timezone.utc).isoformat()
        error = _sanitize_last_error(data.last_error)
        service_obj.db.execute(
            "INSERT INTO kaiwu_heartbeats"
            "(connector_id,state,connector_version,observed_at,source_ids,last_error,received_at)"
            " VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(connector_id) DO UPDATE SET"
            " state=excluded.state,connector_version=excluded.connector_version,"
            " observed_at=excluded.observed_at,source_ids=excluded.source_ids,"
            " last_error=excluded.last_error,received_at=excluded.received_at",
            (
                connector_id,
                data.state,
                data.version[:256],
                observed_at,
                dump(data.source_ids),
                error,
                now(),
            ),
        )
        return {"ok": True, "received_at": now()}

    def transition(connector_id: str, action: str, admin_ctx, service_obj) -> dict:
        connector = service_obj.db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
        if not connector:
            raise AppError(404, "CONNECTOR_NOT_FOUND", "连接器不存在")
        if connector["revoked"] and action != "revoked":
            raise AppError(403, "DEVICE_REVOKED", "连接器已撤销")
        if action == "drain":
            draining, revoked = 1, 0
        elif action == "resume":
            draining, revoked = 0, 0
        else:
            draining, revoked = 1, 1
        changed = bool(connector["draining"]) != bool(draining) or bool(connector["revoked"]) != bool(revoked)
        if changed:
            service_obj.db.execute(
                "UPDATE kaiwu_connectors SET draining=?,revoked=?,revision=revision+1,updated_at=?,revoked_at=? WHERE id=?",
                (draining, revoked, now(), now() if revoked else connector["revoked_at"], connector_id),
            )
        current = service_obj.db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
        trace_id = "tr_" + uid()
        _audit(
            service_obj.db,
            tenant_id=current["tenant_id"],
            connector_id=connector_id,
            source_id=None,
            actor_type="admin",
            actor_id=admin_ctx["actor_id"],
            action="connector." + action,
            outcome="allow",
            trace_id=trace_id,
            detail={"revision": current["revision"], "changed": changed},
        )
        return {**_connector_view(current), "trace_id": trace_id}

    @router.post("/v1/connectors/{connector_id}/drain")
    def drain(connector_id: str, admin_ctx=Depends(admin), service_obj=Depends(service)):
        return transition(connector_id, "drain", admin_ctx, service_obj)

    @router.post("/v1/connectors/{connector_id}/resume")
    def resume(connector_id: str, admin_ctx=Depends(admin), service_obj=Depends(service)):
        return transition(connector_id, "resume", admin_ctx, service_obj)

    @router.post("/v1/connectors/{connector_id}/revoke")
    def revoke(connector_id: str, admin_ctx=Depends(admin), service_obj=Depends(service)):
        return transition(connector_id, "revoke", admin_ctx, service_obj)

    @router.post("/v1/connectors/{connector_id}/self-revoke")
    def self_revoke(
        connector_id: str,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        trace_id = "tr_" + uid()
        service_obj.db.execute(
            "UPDATE kaiwu_connectors SET draining=1,revoked=1,revision=revision+1,updated_at=?,revoked_at=? WHERE id=?",
            (now(), now(), connector_id),
        )
        _audit(
            service_obj.db,
            tenant_id=connector["tenant_id"],
            connector_id=connector_id,
            source_id=None,
            actor_type="device",
            actor_id=connector["device_key_id"],
            action="connector.self_revoked",
            outcome="allow",
            trace_id=trace_id,
            detail={},
        )
        return {"connector_id": connector_id, "revoked": True, "trace_id": trace_id}

    return router
