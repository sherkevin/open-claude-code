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
import ipaddress
import json
import logging
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Settings
from .db import Database, dump, now, uid
from .kaiwu_bridge import BridgeError, build_bridge_material, derive_secret
from .kaiwu_native_relay import is_native_connector
from .kaiwu_diagnostics import (
    CATALOG,
    CONNECTOR_ID_RE,
    DEFAULT_ADMIN_EVENTS,
    DEFAULT_DEVICE_EVENTS,
    LEVELS,
    INSTALL_ATTEMPT_ID_RE,
    MAX_BATCH_BYTES,
    MAX_BATCH_EVENTS,
    OPS_PAGE,
    SOURCE_ID_RE,
    TRACE_ID_RE,
    EventBatch,
    insert_rows,
    maybe_prune,
    normalize_event,
    new_event_id,
    new_trace_id,
    ops_page_headers,
    parse_rfc3339,
    query_admin_events,
    query_connector_events,
    source_grants,
    validate_filter,
)
from .security import AppError


ADMIN_PREFIX = "/api/kaiwu"
MAX_BUSINESS_SOURCES = 16
MAX_CONNECTOR_SOURCES = MAX_BUSINESS_SOURCES + 1
MAX_TOOL_COUNT = 64
CLOCK_SKEW_SECONDS = 60
SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{32}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ENROLLMENT_ID_RE = re.compile(r"^enr_[0-9a-f]{32}$")
INTEGRATION_ID_RE = re.compile(r"^int_[0-9a-f]{32}$")
CONNECTOR_ID_RE = re.compile(r"^conn_[0-9a-f]{32}$")
PERSISTENT_CODE_PREFIX = "kwp_"
DEVICE_EXPORT_ACTOR_PREFIX = "device-export:"
ENROLLMENT_TTL_MAX = 7 * 24 * 60 * 60
INTEGRATION_TTL_MAX = 365 * 24 * 60 * 60
log = logging.getLogger(__name__)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnrollmentRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    tenant_name: str = Field(min_length=1, max_length=128)
    ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=60, le=ENROLLMENT_TTL_MAX)
    persistent: bool = Field(default=False, strict=True)
    issued_to: str | None = Field(default=None, min_length=1, max_length=128)

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

    @field_validator("issued_to")
    @classmethod
    def valid_recipient(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("issued_to is invalid")
        return value

    @model_validator(mode="after")
    def valid_persistent_scope(self) -> EnrollmentRequest:
        if self.persistent:
            if not self.issued_to:
                raise ValueError("issued_to is required for a persistent enrollment")
            if "ttl_seconds" in self.model_fields_set:
                raise ValueError("persistent enrollments do not accept ttl_seconds")
        elif self.issued_to is not None:
            raise ValueError("issued_to requires a persistent enrollment")
        return self


class EnrollmentExtendRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128, pattern=TENANT_RE.pattern)
    ttl_seconds: int = Field(ge=60, le=ENROLLMENT_TTL_MAX, strict=True)


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
    source_ids: list[str] = Field(max_length=MAX_CONNECTOR_SOURCES)
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


class IntegrationCreateRequest(StrictModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    connector_id: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    source_ids: list[str] = Field(min_length=1, max_length=MAX_CONNECTOR_SOURCES)
    ttl_seconds: int | None = Field(default=None, ge=60, le=INTEGRATION_TTL_MAX)

    @field_validator("tenant_id")
    @classmethod
    def valid_tenant(cls, value: str) -> str:
        value = value.strip()
        if not TENANT_RE.fullmatch(value):
            raise ValueError("tenant_id contains unsupported characters")
        return value

    @field_validator("connector_id")
    @classmethod
    def valid_connector(cls, value: str) -> str:
        value = value.strip()
        if not CONNECTOR_ID_RE.fullmatch(value):
            raise ValueError("connector_id is invalid")
        return value

    @field_validator("display_name")
    @classmethod
    def valid_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("display_name is invalid")
        return value

    @field_validator("source_ids")
    @classmethod
    def valid_source_ids(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if len(set(normalized)) != len(normalized) or any(
            not SOURCE_ID_RE.fullmatch(value) for value in normalized
        ):
            raise ValueError("source_ids must contain unique opaque source IDs")
        return sorted(normalized)


class IntegrationRotateRequest(StrictModel):
    ttl_seconds: int | None = Field(default=None, ge=60, le=INTEGRATION_TTL_MAX)


class DeviceExportRequest(StrictModel):
    source_id: str = Field(pattern=SOURCE_ID_RE.pattern)


class SelfTestRequest(StrictModel):
    source_id: str = Field(min_length=36, max_length=36)
    nonce: str = Field(min_length=1, max_length=128)

    @field_validator("source_id")
    @classmethod
    def valid_source_id(cls, value: str) -> str:
        if not SOURCE_ID_RE.fullmatch(value):
            raise ValueError("source_id must be an opaque src_ identifier")
        return value

    @field_validator("nonce")
    @classmethod
    def valid_nonce(cls, value: str) -> str:
        if not value or value.strip() != value or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError("nonce must be visible ASCII text without whitespace")
        return value


BUILTIN_DEMO_SOURCE_NAME = "开物内置测试服务（示例数据）"
BUILTIN_DEMO_SOURCE_ID_NAMESPACE = "kaiwu_builtin_demo_v1"
BUILTIN_DEMO_TOOLS = frozenset({
    "kaiwu_demo_inventory_query",
    "kaiwu_demo_order_summary",
    "kaiwu_demo_ping",
})
BUILTIN_DEMO_TOOL = "kaiwu_demo_ping"
BUILTIN_DEMO_MARKER = "KAIWU_BUILTIN_DEMO_V1"
# These released demos have identical tool definitions. Their fingerprints
# differ only because the handshake used the connector package version.
BUILTIN_DEMO_COMPATIBLE_SCHEMA_DIGESTS = frozenset({
    "02b1de23a81613bf6295fc5a937e70eba07032e1b5d064c9a4837f22437e675e",  # 1.4.1
    "b7e05ec9b71d8f583eaed02655592e99938303931e2594969fbc5e60df9b3123",  # 1.4.2 and fixed demo profile
})
SELF_TEST_INTEGRATION_CREATED_BY_PREFIX = "device-self-test:"
SELF_TEST_CLEANUP_ACTION = "integration.self_test_cleanup"
MCP_PROTOCOL_VERSION = "2025-06-18"
MAX_SELF_TEST_RESPONSE_BYTES = 1_000_000
SELF_TEST_CLOCK_SKEW_SECONDS = 300
SELF_TEST_STAGES = (
    "edge_connect",
    "mcp_initialize",
    "tools_list",
    "tools_call",
    "probe_validation",
)


def _builtin_demo_source_id(connector_id: str) -> str:
    digest = hashlib.sha256(
        (BUILTIN_DEMO_SOURCE_ID_NAMESPACE + ":" + connector_id).encode("utf-8")
    ).hexdigest()
    return "src_" + digest[:32]


def _is_builtin_demo(connector_id: str, source: dict) -> bool:
    if (
        source["source_id"] != _builtin_demo_source_id(connector_id)
        or source["transport"] != "streamable_http"
    ):
        return False
    try:
        tools = json.loads(source["allowed_tools"])
    except (TypeError, ValueError):
        return False
    return (
        isinstance(tools, list)
        and len(tools) == len(BUILTIN_DEMO_TOOLS)
        and all(isinstance(tool, str) for tool in tools)
        and set(tools) == BUILTIN_DEMO_TOOLS
    )


@contextmanager
def _report_source_conflict(db: Database, connector: dict, source_id: str):
    """Record a denied registration after rolling back its transaction."""
    try:
        yield
    except AppError as exc:
        if exc.code == "SOURCE_CONFIG_CONFLICT":
            event_code = "connection_source_conflict"
            try:
                spec = CATALOG[event_code]
                insert_rows(db, connector["tenant_id"], connector["id"], [{
                    "event_id": new_event_id(),
                    "occurred_at": now(),
                    "level": spec.level,
                    "component": spec.component,
                    "event_code": event_code,
                    "message_code": event_code,
                    "source_id": source_id,
                    "trace_id": new_trace_id(),
                    "http_status": exc.status,
                }], origin="edge")  # Server-generated event, not a device upload.
                maybe_prune(db)
            except Exception as log_error:  # Log collection must not change the denial.
                log.warning("source conflict diagnostics write failed (%s)", type(log_error).__name__)
        raise


def _assert_business_source_limit(connector_id: str, sources: list[dict]) -> None:
    count = sum(not _is_builtin_demo(connector_id, source) for source in sources)
    if count > MAX_BUSINESS_SOURCES:
        raise AppError(409, "SOURCE_LIMIT_EXCEEDED", "单个连接器最多允许 16 个业务来源")


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
    try:
        return derive_secret(settings, purpose, value)
    except BridgeError as exc:
        raise AppError(503, "CONTROL_PLANE_DISABLED", str(exc)) from None


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


def _optional_time(value: str | None, name: str):
    if not value:
        return None
    try:
        return parse_rfc3339(value)
    except ValueError:
        raise AppError(422, "DIAGNOSTICS_FILTER_INVALID", f"诊断筛选条件无效：{name}") from None


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


def _integration_view(db: Database, row: dict) -> dict:
    sources = [
        item["source_id"]
        for item in db.all(
            "SELECT source_id FROM kaiwu_integration_sources "
            "WHERE integration_id=? ORDER BY source_id",
            (row["id"],),
        )
    ]
    return {
        "integration_id": row["id"],
        "tenant_id": row["tenant_id"],
        "connector_id": row["connector_id"],
        "display_name": row["display_name"],
        "token_prefix": row["token_prefix"],
        "source_ids": sources,
        "expires_at": row["expires_at"],
        "revision": row["revision"],
        "rotated_at": row["rotated_at"],
        "revoked_at": row["revoked_at"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _edge_public_base(settings: Settings) -> str:
    raw = settings.kaiwu_edge_public_base.rstrip("/")
    if not raw:
        raise AppError(503, "EDGE_NOT_CONFIGURED", "云端 Edge 公网地址尚未配置，不能签发平台配置")
    parsed = urlsplit(raw)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AppError(503, "EDGE_NOT_CONFIGURED", "云端 Edge 公网地址格式无效")
    host = (parsed.hostname or "").lower().rstrip(".")
    local_http = parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1", "testserver"}
    if parsed.scheme != "https" and not local_http:
        raise AppError(503, "EDGE_NOT_CONFIGURED", "云端 Edge 必须使用 HTTPS")
    expected_path = settings.kaiwu_edge_mount_path.rstrip("/")
    if parsed.path.rstrip("/") != expected_path:
        raise AppError(
            503,
            "EDGE_NOT_CONFIGURED",
            f"云端 Edge 公网路径必须与挂载路径 {expected_path} 一致",
        )
    return raw


def _self_test_edge_base(settings: Settings) -> str:
    """Return a public HTTPS Edge base, never the local test exception."""
    raw = settings.kaiwu_edge_public_base.rstrip("/")
    if not raw:
        raise AppError(
            503,
            "SELF_TEST_EDGE_HTTPS_REQUIRED",
            "内置测试自检需要受信配置的公网 HTTPS Edge 地址",
        )
    try:
        parsed = urlsplit(raw)
        parsed_port = parsed.port
    except ValueError:
        raise AppError(
            503,
            "SELF_TEST_EDGE_HTTPS_REQUIRED",
            "内置测试自检的受信 Edge 地址格式无效",
        ) from None
    del parsed_port
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or host in {"localhost", "testserver"}
        or host.endswith(".localhost")
    ):
        raise AppError(
            503,
            "SELF_TEST_EDGE_HTTPS_REQUIRED",
            "内置测试自检必须使用受信配置的公网 HTTPS Edge 地址",
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise AppError(
            503,
            "SELF_TEST_EDGE_HTTPS_REQUIRED",
            "内置测试自检不能使用本机或内网 Edge 地址冒充公网 HTTPS",
        )
    expected_path = settings.kaiwu_edge_mount_path.rstrip("/")
    if parsed.path.rstrip("/") != expected_path:
        raise AppError(
            503,
            "SELF_TEST_EDGE_HTTPS_REQUIRED",
            f"内置测试自检的 Edge 路径必须与挂载路径 {expected_path} 一致",
        )
    return raw


def _platform_config(settings: Settings, sources: list[dict], token: str, display_name: str) -> dict:
    base = _edge_public_base(settings)
    servers = {}
    for row in sources:
        source_id = row["source_id"]
        name = display_name if len(sources) == 1 else f"{display_name} ({source_id[-6:]})"
        servers[name] = {
            "type": "http",
            "url": f"{base}/sources/{source_id}/mcp",
            "headers": {"Authorization": f"Bearer {token}"},
        }
    return {"mcpServers": servers}


def _new_integration_token() -> str:
    return "kw_int_" + secrets.token_urlsafe(48)


def _token_prefix(token: str) -> str:
    return token[:15] + "..." + token[-6:]


def _device_export_token(settings: Settings, connector_id: str, integration_id: str) -> str:
    return "kw_int_" + _derive_secret(settings, "device-mcp-export-v1", f"{connector_id}:{integration_id}")


def _issue_platform_integration(
    service_obj, data, sources: list[dict], actor_id: str, *,
    device_export: bool = False, assist_session_id: str | None = None,
) -> dict:
    """Issue within the caller's transaction so authorization and creation are atomic."""
    settings = service_obj.settings
    _assert_business_source_limit(data.connector_id, sources)
    db = service_obj.db
    trace_id = "tr_" + uid()
    integration_id = "int_" + uid()
    if device_export:
        token = _device_export_token(settings, data.connector_id, integration_id)
        created_by = DEVICE_EXPORT_ACTOR_PREFIX + data.connector_id
    elif assist_session_id is not None:
        # The workbench can retry after the control transaction has committed.
        # Reconstruct the integration token without storing it in either log or control DB.
        integration_id = "int_" + _hash_secret("assist-platform-import-v1:" + assist_session_id)[:32]
        token = "kw_int_" + _derive_secret(settings, "assist-platform-import-v1", assist_session_id)
        created_by = actor_id
    else:
        token = _new_integration_token()
        created_by = actor_id
    config = _platform_config(settings, sources, token, data.display_name)
    if assist_session_id is not None and not device_export:
        existing = db.one("SELECT * FROM kaiwu_integrations WHERE id=?", (integration_id,))
        if existing:
            registered = db.all(
                "SELECT connector_id,source_id FROM kaiwu_integration_sources WHERE integration_id=?",
                (integration_id,),
            )
            if (
                existing["tenant_id"] != data.tenant_id
                or existing["connector_id"] != data.connector_id
                or existing["display_name"] != data.display_name
                or existing["created_by"] != created_by
                or existing["revoked_at"] is not None
                or existing["expires_at"] <= now()
                or not hmac.compare_digest(existing["token_hash"], _hash_secret(token))
                or {(item["connector_id"], item["source_id"]) for item in registered}
                != {(data.connector_id, source_id) for source_id in data.source_ids}
            ):
                raise AppError(409, "ASSIST_INTEGRATION_CHANGED", "协助导入授权已更新，请重新发起协助")
            return {"mcp_config": config}
    ttl = data.ttl_seconds or settings.kaiwu_integration_default_ttl_seconds
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
    timestamp = now()
    db.execute(
        "INSERT INTO kaiwu_integrations"
        "(id,tenant_id,connector_id,display_name,token_hash,token_prefix,expires_at,"
        "revision,rotated_at,revoked_at,created_by,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,1,NULL,NULL,?,?,?)",
        (
            integration_id, data.tenant_id, data.connector_id, data.display_name,
            _hash_secret(token), _token_prefix(token), expires_at, created_by, timestamp, timestamp,
        ),
    )
    db.executemany(
        "INSERT INTO kaiwu_integration_sources"
        "(integration_id,connector_id,source_id) VALUES(?,?,?)",
        [(integration_id, data.connector_id, source["source_id"]) for source in sources],
    )
    _audit(
        db, tenant_id=data.tenant_id, connector_id=data.connector_id,
        source_id=sources[0]["source_id"] if device_export else None,
        actor_type="device" if device_export else "admin", actor_id=actor_id, action="integration.created", outcome="allow",
        trace_id=trace_id, detail={"integration_id": integration_id, "source_ids": data.source_ids, "expires_at": expires_at},
    )
    row = db.one("SELECT * FROM kaiwu_integrations WHERE id=?", (integration_id,))
    return {
        "integration": _integration_view(db, row),
        "access_token": token,
        "mcp_config": config,
        "mcp_config_json": json.dumps(config, ensure_ascii=False, indent=2),
        "trace_id": trace_id,
    }


def _available_native_source(request: Request, service, connector, source_id: str) -> dict:
    connector_id = connector["id"]
    relay = getattr(request.app.state, "kaiwu_native_relay", None)
    if (not service.settings.kaiwu_native_relay_enabled or connector["revoked"] or connector["draining"]
            or relay is None or not relay.is_online(connector_id)):
        raise AppError(409, "NATIVE_RELAY_OFFLINE", "Windows 连接器暂未连接，请在本机建立连接后重试")
    if source_id == _builtin_demo_source_id(connector_id):
        raise AppError(403, "BUILTIN_DEMO_HANDOFF_DENIED", "请选择要导入的业务 MCP")
    source = _integration_rows(service.db, connector_id, [source_id])[0]
    if source["transport"] != "streamable_http":
        raise AppError(409, "HANDOFF_TRANSPORT_UNSUPPORTED", "请使用 Streamable HTTP 业务 MCP")
    return source


def _allocate_visitor_port(db: Database, settings: Settings) -> int:
    used = {
        int(row["visitor_port"])
        for row in db.all("SELECT visitor_port FROM kaiwu_edge_routes")
    }
    for port in range(settings.kaiwu_edge_visitor_port_start, settings.kaiwu_edge_visitor_port_end + 1):
        if port not in used:
            return port
    raise AppError(409, "EDGE_ROUTE_EXHAUSTED", "云端 visitor 端口范围已耗尽")


def _integration_rows(db: Database, connector_id: str, source_ids: list[str]) -> list[dict]:
    placeholders = ",".join("?" for _ in source_ids)
    rows = db.all(
        "SELECT s.*, r.visitor_host, r.visitor_port "
        "FROM kaiwu_sources s LEFT JOIN kaiwu_edge_routes r "
        "ON r.connector_id=s.connector_id AND r.source_id=s.source_id "
        f"WHERE s.connector_id=? AND s.source_id IN ({placeholders}) ORDER BY s.source_id",
        (connector_id, *source_ids),
    )
    if {row["source_id"] for row in rows} != set(source_ids):
        raise AppError(403, "INTEGRATION_SOURCE_DENIED", "来源不属于该连接器或尚未登记")
    if not is_native_connector(db, connector_id) and any(row["visitor_port"] is None for row in rows):
        raise AppError(409, "EDGE_ROUTE_NOT_READY", "云端 visitor 路由尚未分配，请先完成来源登记")
    return rows


def _revoke_connector_integrations(db: Database, connector_id: str, actor_type: str, actor_id: str) -> int:
    rows = db.all(
        "SELECT id,tenant_id FROM kaiwu_integrations WHERE connector_id=? AND revoked_at IS NULL",
        (connector_id,),
    )
    timestamp = now()
    for row in rows:
        db.execute(
            "UPDATE kaiwu_integrations SET revoked_at=?,updated_at=?,revision=revision+1 "
            "WHERE id=? AND revoked_at IS NULL",
            (timestamp, timestamp, row["id"]),
        )
        _audit(
            db,
            tenant_id=row["tenant_id"],
            connector_id=connector_id,
            source_id=None,
            actor_type=actor_type,
            actor_id=actor_id,
            action="integration.revoked_by_connector",
            outcome="allow",
            trace_id="tr_" + uid(),
            detail={"integration_id": row["id"]},
        )
    return len(rows)


def _release_connector_routes(db: Database, connector_id: str) -> None:
    """Release visitor ports once a connector can never be resumed."""
    db.execute("DELETE FROM kaiwu_edge_routes WHERE connector_id=?", (connector_id,))


def _revoke_connector_locked(
    db: Database, connector_id: str, timestamp: str, actor_type: str, actor_id: str,
) -> None:
    db.execute(
        "UPDATE kaiwu_connectors SET draining=1,revoked=1,revision=revision+1,updated_at=?,revoked_at=? "
        "WHERE id=? AND revoked=0",
        (timestamp, timestamp, connector_id),
    )
    _revoke_connector_integrations(db, connector_id, actor_type, actor_id)
    _release_connector_routes(db, connector_id)


class _SelfTestFailure(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _stage_status(stages: list[dict], stage_id: str, status: str, message: str) -> None:
    stages.append({"id": stage_id, "status": status, "message": message})


def _finish_self_test_stages(stages: list[dict], current: str, failed_message: str) -> None:
    _stage_status(stages, current, "failed", failed_message)
    passed_or_failed = {item["id"] for item in stages}
    for stage_id in SELF_TEST_STAGES:
        if stage_id not in passed_or_failed:
            _stage_status(stages, stage_id, "skipped", "前序步骤未通过，本步骤未执行")


async def _read_self_test_response(response: httpx.Response) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > MAX_SELF_TEST_RESPONSE_BYTES:
            raise _SelfTestFailure(502, "云端 Edge 返回结果超过自检大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


def _jsonrpc_documents(raw: bytes, content_type: str) -> list:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _SelfTestFailure(502, "云端 Edge 返回了无法解析的字符编码") from None
    if "text/event-stream" not in content_type.lower():
        try:
            value = json.loads(text)
        except ValueError:
            raise _SelfTestFailure(502, "云端 Edge 未返回有效 JSON-RPC 结果") from None
        return value if isinstance(value, list) else [value]

    documents = []
    blocks = re.split(r"\r\n\r\n|\n\n|\r\r", text)
    for block in blocks:
        data_lines = []
        for line in re.split(r"\r\n|\r|\n", block):
            if line.startswith("data:"):
                data_lines.append(line[5:][1:] if line[5:].startswith(" ") else line[5:])
        if not data_lines:
            continue
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            documents.append(json.loads(payload))
        except ValueError:
            raise _SelfTestFailure(502, "云端 Edge 返回了无效的 SSE JSON-RPC 结果") from None
    return documents


def _jsonrpc_result(raw: bytes, content_type: str, request_id: int) -> dict:
    for document in _jsonrpc_documents(raw, content_type):
        if not isinstance(document, dict) or document.get("id") != request_id:
            continue
        error = document.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else ""
            suffix = f"（JSON-RPC {code}）" if isinstance(code, int) and not isinstance(code, bool) else ""
            raise _SelfTestFailure(502, f"内置测试 MCP 返回错误{suffix}")
        result = document.get("result")
        if not isinstance(result, dict):
            raise _SelfTestFailure(502, "内置测试 MCP 未返回可读取的 JSON-RPC 结果")
        return result
    raise _SelfTestFailure(502, "云端 Edge 未返回与请求匹配的 JSON-RPC 结果")


async def _post_self_test(
    client: httpx.AsyncClient,
    edge_url: str,
    token: str,
    body: dict,
    trace_id: str,
    session_id: str = "",
) -> tuple[int, str, str, bytes, str]:
    encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
        "X-Kaiwu-Trace-ID": trace_id,
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        async with client.stream("POST", edge_url, content=encoded, headers=headers) as response:
            observed_trace = response.headers.get("x-kaiwu-trace-id", "").strip()
            if TRACE_ID_RE.fullmatch(observed_trace):
                trace_id = observed_trace
            raw = await _read_self_test_response(response)
            return (
                response.status_code,
                trace_id,
                response.headers.get("content-type", ""),
                raw,
                response.headers.get("mcp-session-id", "").strip(),
            )
    except _SelfTestFailure:
        raise
    except httpx.TimeoutException:
        raise _SelfTestFailure(504, "云端 Edge 请求超时，请检查公网 Edge 和隧道状态") from None
    except httpx.RequestError:
        raise _SelfTestFailure(503, "无法连接受信配置的公网 Edge，请检查地址、证书和网络") from None


def _require_edge_status(status: int) -> None:
    if 200 <= status < 300:
        return
    if status in {401, 403}:
        raise _SelfTestFailure(502, "云端 Edge 拒绝了临时测试凭据，请确认自检接口已部署")
    if status == 404:
        raise _SelfTestFailure(502, "云端 Edge 未找到固定测试来源路径，请先完成来源登记和部署")
    if status == 503:
        raise _SelfTestFailure(503, "云端 Edge 或来源隧道暂时不可用")
    if status == 504:
        raise _SelfTestFailure(504, "云端 Edge 等待内置测试服务超时")
    raise _SelfTestFailure(502, f"云端 Edge 返回 HTTP {status}，自检未通过")


def _probe_payload(result: dict) -> dict:
    if result.get("isError") is True:
        raise _SelfTestFailure(502, "内置测试 MCP 返回了工具错误")
    structured = result.get("structuredContent", result.get("structured_content"))
    if isinstance(structured, dict) and ("demo" in structured or "marker" in structured):
        return structured
    if "marker" in result or "demo" in result:
        return result
    content = result.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise _SelfTestFailure(502, "内置测试 MCP 返回了无法核验的工具结果")


def _validate_probe(result: dict, nonce: str) -> dict:
    payload = _probe_payload(result)
    if payload.get("demo") is not True:
        raise _SelfTestFailure(502, "内置测试 MCP 未返回 demo=true")
    if payload.get("marker") != BUILTIN_DEMO_MARKER:
        raise _SelfTestFailure(502, "内置测试 MCP 的固定标记不匹配")
    if payload.get("nonce") != nonce:
        raise _SelfTestFailure(502, "内置测试 MCP 未原样回显本次 nonce")
    server_time_raw = payload.get("server_time_utc")
    try:
        server_time = parse_rfc3339(server_time_raw)
    except (TypeError, ValueError):
        raise _SelfTestFailure(502, "内置测试 MCP 未返回有效的当前 UTC 时间") from None
    if abs((datetime.now(timezone.utc) - server_time).total_seconds()) > SELF_TEST_CLOCK_SKEW_SECONDS:
        raise _SelfTestFailure(502, "内置测试 MCP 返回的 UTC 时间与当前时间相差过大")
    instance_id = payload.get("instance_id")
    if (
        not isinstance(instance_id, str)
        or not 1 <= len(instance_id) <= 128
        or instance_id.strip() != instance_id
        or any(ord(char) < 33 or ord(char) > 126 for char in instance_id)
    ):
        raise _SelfTestFailure(502, "内置测试 MCP 返回的进程启动标识无效")
    return {
        "demo": True,
        "marker": BUILTIN_DEMO_MARKER,
        "nonce": nonce,
        "server_time_utc": server_time.isoformat(),
        "instance_id": instance_id,
    }


async def _execute_self_test(
    settings: Settings,
    edge_base: str,
    source_id: str,
    nonce: str,
    token: str,
    trace_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int, str, list[dict], dict]:
    stages: list[dict] = []
    current = "edge_connect"
    edge_url = f"{edge_base}/sources/{source_id}/mcp"
    timeout = httpx.Timeout(
        settings.kaiwu_edge_request_timeout_seconds,
        connect=min(10.0, settings.kaiwu_edge_request_timeout_seconds),
    )
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            status, trace_id, content_type, raw, session_id = await _post_self_test(
                client,
                edge_url,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "kaiwu-self-test", "version": "1.3.8"},
                    },
                },
                trace_id,
            )
            _require_edge_status(status)
            _stage_status(stages, "edge_connect", "passed", "已通过受信公网 HTTPS Edge 建立 MCP 请求")

            current = "mcp_initialize"
            initialize_result = _jsonrpc_result(raw, content_type, 1)
            if not isinstance(initialize_result.get("protocolVersion"), str):
                raise _SelfTestFailure(502, "内置测试 MCP 的初始化结果缺少协议版本")
            status, trace_id, _, _, notification_session = await _post_self_test(
                client,
                edge_url,
                token,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                trace_id,
                session_id,
            )
            _require_edge_status(status)
            if notification_session:
                session_id = notification_session
            _stage_status(stages, "mcp_initialize", "passed", "MCP initialize 和 initialized 通知已完成")

            current = "tools_list"
            status, trace_id, content_type, raw, listed_session = await _post_self_test(
                client,
                edge_url,
                token,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                trace_id,
                session_id,
            )
            _require_edge_status(status)
            if listed_session:
                session_id = listed_session
            listed = _jsonrpc_result(raw, content_type, 2).get("tools")
            names = [
                item.get("name")
                for item in listed
                if isinstance(item, dict)
            ] if isinstance(listed, list) else []
            if BUILTIN_DEMO_TOOL not in names:
                raise _SelfTestFailure(502, f"工具目录没有已批准的内置测试工具 {BUILTIN_DEMO_TOOL}")
            _stage_status(stages, "tools_list", "passed", f"工具目录已返回 {BUILTIN_DEMO_TOOL}")

            current = "tools_call"
            status, trace_id, content_type, raw, called_session = await _post_self_test(
                client,
                edge_url,
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": BUILTIN_DEMO_TOOL, "arguments": {"nonce": nonce}},
                },
                trace_id,
                session_id,
            )
            _require_edge_status(status)
            if called_session:
                session_id = called_session
            called_result = _jsonrpc_result(raw, content_type, 3)
            _stage_status(stages, "tools_call", "passed", f"已执行固定工具 {BUILTIN_DEMO_TOOL}")

            current = "probe_validation"
            probe = _validate_probe(called_result, nonce)
            _stage_status(stages, "probe_validation", "passed", "nonce、固定标记、UTC 时间和实例标识均校验通过")
            return 200, trace_id, stages, probe
    except _SelfTestFailure as exc:
        _finish_self_test_stages(stages, current, exc.message)
        return exc.status_code, trace_id, stages, {}
    except Exception:  # noqa: BLE001 - never expose internal/transient details
        _finish_self_test_stages(stages, current, "自检过程中发生未预期错误")
        return 502, trace_id, stages, {}


def _create_self_test_integration(
    db: Database,
    connector: dict,
    source_id: str,
    request_timeout_seconds: float,
) -> tuple[str, str]:
    token = _new_integration_token()
    integration_id = "int_" + uid()
    timestamp = now()
    expires_at = (
        datetime.now(timezone.utc)
        + timedelta(seconds=max(60, int(request_timeout_seconds) + 5))
    ).isoformat()
    with db.transaction():
        db.execute(
            "INSERT INTO kaiwu_integrations"
            "(id,tenant_id,connector_id,display_name,token_hash,token_prefix,expires_at,"
            "revision,rotated_at,revoked_at,created_by,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,1,NULL,NULL,?,?,?)",
            (
                integration_id,
                connector["tenant_id"],
                connector["id"],
                BUILTIN_DEMO_SOURCE_NAME,
                _hash_secret(token),
                "self-test",
                expires_at,
                SELF_TEST_INTEGRATION_CREATED_BY_PREFIX + connector["device_key_id"],
                timestamp,
                timestamp,
            ),
        )
        db.execute(
            "INSERT INTO kaiwu_integration_sources"
            "(integration_id,connector_id,source_id) VALUES(?,?,?)",
            (integration_id, connector["id"], source_id),
        )
    return integration_id, token


def _revoke_self_test_integration(db: Database, integration_id: str) -> bool:
    row = db.one(
        "SELECT created_by FROM kaiwu_integrations WHERE id=?",
        (integration_id,),
    )
    if row and not str(row["created_by"]).startswith(SELF_TEST_INTEGRATION_CREATED_BY_PREFIX):
        raise RuntimeError("refusing to delete a non-self-test integration")
    with db.transaction():
        db.execute(
            "DELETE FROM kaiwu_integration_sources WHERE integration_id=?",
            (integration_id,),
        )
        db.execute("DELETE FROM kaiwu_integrations WHERE id=?", (integration_id,))
    return db.one("SELECT 1 FROM kaiwu_integrations WHERE id=?", (integration_id,)) is None


def _record_self_test_cleanup(
    db: Database,
    connector: dict,
    source_id: str,
    trace_id: str,
    cleanup_succeeded: bool,
) -> None:
    """Record only cleanup outcome in the existing control audit."""
    try:
        _audit(
            db,
            tenant_id=connector["tenant_id"],
            connector_id=connector["id"],
            source_id=source_id,
            actor_type="device",
            actor_id=connector["device_key_id"],
            action=SELF_TEST_CLEANUP_ACTION,
            outcome="allow" if cleanup_succeeded else "deny",
            trace_id=trace_id,
            detail={
                "result": "succeeded" if cleanup_succeeded else "failed",
                "tool_name": BUILTIN_DEMO_TOOL,
            },
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask cleanup
        log.warning("self-test cleanup diagnostics write failed (%s)", type(exc).__name__)


def _control_settings(service_obj) -> Settings:
    settings = service_obj.settings
    if not settings.kaiwu_control_enabled:
        raise AppError(503, "CONTROL_PLANE_DISABLED", "内网连接器控制面尚未启用")
    if not settings.kaiwu_control_token():
        raise AppError(503, "CONTROL_PLANE_DISABLED", "内网连接器控制面缺少独立管理员凭据")
    if not settings.kaiwu_native_relay_enabled and (
        not settings.kaiwu_control_relay_address or settings.kaiwu_control_relay_port <= 0
    ):
        raise AppError(503, "CONTROL_PLANE_DISABLED", "内网连接器控制面缺少已配置的中继端点")
    return settings


def verify_device_request(service_obj, headers, method: str, path: str, body: bytes) -> dict:
    settings = _control_settings(service_obj)
    supplied = headers.get("authorization", "")
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
    if headers.get("x-kaiwu-device-key", "") != connector["device_key_id"]:
        raise AppError(403, "DEVICE_SIGNATURE_INVALID", "设备签名无效")

    timestamp_raw = headers.get("x-kaiwu-timestamp", "")
    nonce = headers.get("x-kaiwu-nonce", "")
    signature_raw = headers.get("x-kaiwu-signature", "")
    try:
        timestamp = int(timestamp_raw)
    except (TypeError, ValueError):
        raise AppError(403, "DEVICE_SIGNATURE_INVALID", "设备签名无效") from None
    if abs(int(time.time()) - timestamp) > CLOCK_SKEW_SECONDS:
        raise AppError(403, "DEVICE_SIGNATURE_EXPIRED", "设备签名已过期")
    if not NONCE_RE.fullmatch(nonce):
        raise AppError(403, "DEVICE_SIGNATURE_INVALID", "设备签名无效")

    message = "\n".join((
        timestamp_raw,
        nonce,
        method.upper(),
        path,
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


def create_kaiwu_control_router(service_getter: Callable) -> APIRouter:
    router = APIRouter(prefix=ADMIN_PREFIX, tags=["kaiwu-control"])

    def service(request: Request):
        return service_getter(request)

    def admin(request: Request, service_obj=Depends(service)) -> dict:
        settings = _control_settings(service_obj)
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
        return verify_device_request(
            service_obj, request.headers, request.method, request.url.path, await request.body(),
        )

    @router.get("/v1/health")
    def health(service_obj=Depends(service)):
        try:
            _control_settings(service_obj)
            enabled = True
        except AppError:
            enabled = False
        return {"status": "enabled" if enabled else "disabled", "version": "control-plane-1"}

    @router.post("/v1/enrollments")
    def create_enrollment(
        data: EnrollmentRequest,
        response: Response,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        db = service_obj.db
        enrollment_id = "enr_" + uid()
        code = (PERSISTENT_CODE_PREFIX if data.persistent else "kw_") + secrets.token_urlsafe(32)
        code_hash = _hash_secret(code)
        timestamp = now()
        expires_at = None
        persistent_fields = {}
        if data.persistent:
            persistent_fields = {"persistent": True, "issued_to": data.issued_to}
        else:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=data.ttl_seconds)).isoformat()
        trace_id = "tr_" + uid()
        with db.transaction():
            if data.persistent:
                db.execute(
                    "INSERT INTO kaiwu_persistent_enrollments"
                    "(id,tenant_id,tenant_name,issued_to,code_hash,created_by,created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (enrollment_id, data.tenant_id, data.tenant_name, data.issued_to, code_hash,
                     admin_ctx["actor_id"], timestamp),
                )
            else:
                db.execute(
                    "INSERT INTO kaiwu_enrollments"
                    "(id,tenant_id,tenant_name,code_hash,expires_at,used_at,connector_id,created_by,created_at)"
                    " VALUES(?,?,?,?,?,NULL,NULL,?,?)",
                    (enrollment_id, data.tenant_id, data.tenant_name, code_hash, expires_at,
                     admin_ctx["actor_id"], timestamp),
                )
            _audit(
                db,
                tenant_id=data.tenant_id,
                connector_id=None,
                source_id=None,
                actor_type="admin",
                actor_id=admin_ctx["actor_id"],
                action="enrollment.created",
                outcome="allow",
                trace_id=trace_id,
                detail={"enrollment_id": enrollment_id, "expires_at": expires_at, **persistent_fields},
            )
        response.headers["Cache-Control"] = "no-store"
        return {
            "enrollment_id": enrollment_id,
            "code": code,
            "expires_at": expires_at,
            "trace_id": trace_id,
            **persistent_fields,
        }

    @router.post("/v1/enrollments/{enrollment_id}/extend")
    def extend_one_time_enrollment(
        enrollment_id: str,
        data: EnrollmentExtendRequest,
        response: Response,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        """Extend an unused, unexpired one-time code from its original creation."""
        if not ENROLLMENT_ID_RE.fullmatch(enrollment_id):
            raise AppError(404, "ENROLLMENT_NOT_FOUND", "一次性接入码不存在")
        db = service_obj.db
        with db.transaction():
            row = db.one(
                "SELECT tenant_id,created_at,expires_at,used_at,connector_id "
                "FROM kaiwu_enrollments WHERE id=? AND tenant_id=?",
                (enrollment_id, data.tenant_id),
            )
            if not row:
                raise AppError(404, "ENROLLMENT_NOT_FOUND", "一次性接入码不存在")
            if row["used_at"] is not None or row["connector_id"] is not None:
                raise AppError(409, "ENROLLMENT_INVALID", "接入码已使用")
            previous = datetime.fromisoformat(row["expires_at"])
            if previous <= datetime.now(timezone.utc):
                raise AppError(409, "ENROLLMENT_EXPIRED", "接入码已过期")
            new_expiry = datetime.fromisoformat(row["created_at"]) + timedelta(seconds=data.ttl_seconds)
            if new_expiry < previous:
                raise AppError(409, "ENROLLMENT_EXTENSION_INVALID", "新有效期不得短于原有效期")
            expires_at = new_expiry.isoformat()
            trace_id = "tr_" + uid()
            if new_expiry > previous:
                db.execute(
                    "UPDATE kaiwu_enrollments SET expires_at=? "
                    "WHERE id=? AND tenant_id=? AND used_at IS NULL AND connector_id IS NULL",
                    (expires_at, enrollment_id, data.tenant_id),
                )
                _audit(
                    db,
                    tenant_id=data.tenant_id,
                    connector_id=None,
                    source_id=None,
                    actor_type="admin",
                    actor_id=admin_ctx["actor_id"],
                    action="enrollment.extended",
                    outcome="allow",
                    trace_id=trace_id,
                    detail={"enrollment_id": enrollment_id,
                            "previous_expires_at": row["expires_at"],
                            "expires_at": expires_at,
                            "ttl_seconds": data.ttl_seconds},
                )
            else:
                trace_id = None
        response.headers["Cache-Control"] = "no-store"
        return {"enrollment_id": enrollment_id, "expires_at": expires_at,
                "trace_id": trace_id}

    @router.get("/v1/enrollments")
    def list_persistent_enrollments(
        response: Response,
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        rows = service_obj.db.all(
            "SELECT e.id AS enrollment_id,e.tenant_id,e.tenant_name,e.issued_to,e.created_by,"
            "e.created_at,e.revoked_at,"
            "(SELECT COUNT(*) FROM kaiwu_persistent_enrollment_devices d WHERE d.enrollment_id=e.id) AS device_count "
            "FROM kaiwu_persistent_enrollments e ORDER BY e.created_at DESC,e.id LIMIT ? OFFSET ?",
            (limit, offset),
        )
        response.headers["Cache-Control"] = "no-store"
        return {"enrollments": [{**row, "expires_at": None} for row in rows]}

    @router.get("/v1/enrollments/{enrollment_id}/devices")
    def list_persistent_enrollment_devices(
        enrollment_id: str,
        response: Response,
        limit: int = Query(default=25, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        db = service_obj.db
        if not ENROLLMENT_ID_RE.fullmatch(enrollment_id) or not db.one(
            "SELECT 1 FROM kaiwu_persistent_enrollments WHERE id=?", (enrollment_id,),
        ):
            raise AppError(404, "ENROLLMENT_NOT_FOUND", "长期接入码不存在")
        rows = db.all(
            "SELECT c.id AS connector_id,c.device_key_id,d.bound_at,c.revoked,c.revoked_at,"
            "h.received_at AS last_heartbeat_at "
            "FROM kaiwu_persistent_enrollment_devices d JOIN kaiwu_connectors c ON c.id=d.connector_id "
            "LEFT JOIN kaiwu_heartbeats h ON h.connector_id=c.id WHERE d.enrollment_id=? "
            "ORDER BY d.bound_at DESC,d.connector_id LIMIT ? OFFSET ?",
            (enrollment_id, limit, offset),
        )
        response.headers["Cache-Control"] = "no-store"
        return {
            "enrollment_id": enrollment_id,
            "devices": [{**row, "revoked": bool(row["revoked"])} for row in rows],
        }

    @router.post("/v1/enrollments/{enrollment_id}/revoke")
    async def revoke_persistent_enrollment(
        enrollment_id: str,
        request: Request,
        response: Response,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        db = service_obj.db
        trace_id = "tr_" + uid()
        with db.transaction():
            enrollment = db.one(
                "SELECT tenant_id,issued_to,revoked_at FROM kaiwu_persistent_enrollments WHERE id=?",
                (enrollment_id,),
            )
            if not enrollment:
                raise AppError(404, "ENROLLMENT_NOT_FOUND", "长期接入码不存在")
            devices = db.all(
                "SELECT c.id,c.revoked FROM kaiwu_persistent_enrollment_devices d "
                "JOIN kaiwu_connectors c ON c.id=d.connector_id WHERE d.enrollment_id=?",
                (enrollment_id,),
            )
            revoked_at = enrollment["revoked_at"]
            if revoked_at is None:
                revoked_at = now()
                db.execute(
                    "UPDATE kaiwu_persistent_enrollments SET revoked_at=? WHERE id=?",
                    (revoked_at, enrollment_id),
                )
                for device_row in devices:
                    if not device_row["revoked"]:
                        _revoke_connector_locked(db, device_row["id"], revoked_at, "admin", admin_ctx["actor_id"])
                _audit(
                    db,
                    tenant_id=enrollment["tenant_id"],
                    connector_id=None,
                    source_id=None,
                    actor_type="admin",
                    actor_id=admin_ctx["actor_id"],
                    action="enrollment.revoked",
                    outcome="allow",
                    trace_id=trace_id,
                    detail={
                        "enrollment_id": enrollment_id,
                        "issued_to": enrollment["issued_to"],
                        "device_count": len(devices),
                    },
                )
        for device_row in devices:
            await request.app.state.kaiwu_native_relay.disconnect(device_row["id"])
        response.headers["Cache-Control"] = "no-store"
        return {
            "enrollment_id": enrollment_id,
            "revoked": True,
            "revoked_at": revoked_at,
            "revoked_device_count": len(devices),
            "trace_id": trace_id,
        }

    @router.post("/v1/enrollments/redeem")
    def redeem(data: RedeemRequest, response: Response, service_obj=Depends(service)):
        _control_settings(service_obj)
        try:
            key_id, _ = _device_public_key(data.device_public_key)
        except ValueError:
            raise AppError(400, "DEVICE_PUBLIC_KEY_INVALID", "设备公钥无效") from None
        settings: Settings = service_obj.settings
        connector_id = "conn_" + uid()
        device_token = "dvc_" + secrets.token_urlsafe(32)
        relay_token = _derive_secret(settings, "relay", connector_id)
        db = service_obj.db
        code_hash = _hash_secret(data.code)
        persistent = data.code.startswith(PERSISTENT_CODE_PREFIX)
        trace_id = "tr_" + uid()
        with db.transaction():
            timestamp = now()
            if persistent:
                enrollment = db.one(
                    "SELECT * FROM kaiwu_persistent_enrollments WHERE code_hash=?",
                    (code_hash,),
                )
                if not enrollment or enrollment["revoked_at"]:
                    raise AppError(409, "ENROLLMENT_INVALID", "接入码无效或已撤销")
                if db.one(
                    "SELECT 1 FROM kaiwu_persistent_enrollment_devices d "
                    "JOIN kaiwu_connectors c ON c.id=d.connector_id "
                    "WHERE d.enrollment_id=? AND c.revoked=0 LIMIT 1",
                    (enrollment["id"],),
                ):
                    raise AppError(409, "ENROLLMENT_DEVICE_ACTIVE", "接入码已绑定设备，请联系管理员撤销旧设备后重试")
            else:
                enrollment = db.one("SELECT * FROM kaiwu_enrollments WHERE code_hash=?", (code_hash,))
                if not enrollment or enrollment["used_at"]:
                    raise AppError(409, "ENROLLMENT_INVALID", "接入码无效或已使用")
                if enrollment["expires_at"] <= timestamp:
                    raise AppError(409, "ENROLLMENT_EXPIRED", "接入码已过期")
                db.execute(
                    "UPDATE kaiwu_enrollments SET used_at=?,connector_id=? WHERE id=? AND used_at IS NULL",
                    (timestamp, connector_id, enrollment["id"]),
                )
            db.execute(
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
                    timestamp,
                    timestamp,
                ),
            )
            detail = {"enrollment_id": enrollment["id"]}
            if persistent:
                db.execute(
                    "INSERT INTO kaiwu_persistent_enrollment_devices(connector_id,enrollment_id,bound_at) "
                    "VALUES(?,?,?)",
                    (connector_id, enrollment["id"], timestamp),
                )
                detail.update(persistent=True, issued_to=enrollment["issued_to"])
            _audit(
                db,
                tenant_id=enrollment["tenant_id"],
                connector_id=connector_id,
                source_id=None,
                actor_type="device",
                actor_id=key_id,
                action="enrollment.redeemed",
                outcome="allow",
                trace_id=trace_id,
                detail=detail,
            )
        response.headers["Cache-Control"] = "no-store"
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
                    "trusted_ca_file": settings.kaiwu_control_relay_tls_client_trusted_ca_file,
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

    @router.get("/v1/connectors/{connector_id}/mcp-export-status")
    def device_mcp_export_status(
        connector_id: str,
        request: Request,
        response: Response,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        """Read-only control-plane diagnosis; this never grants export."""
        db = service_obj.db
        settings = admin_ctx["settings"]
        row = db.one("SELECT id,tenant_id,revoked,draining FROM kaiwu_connectors WHERE id=?", (connector_id,))
        if not row:
            raise AppError(404, "CONNECTOR_NOT_FOUND", "连接器不存在")
        last = db.one(
            "SELECT detail,trace_id,created_at FROM kaiwu_control_audit "
            "WHERE connector_id=? AND tenant_id=? AND action='device.mcp_export.rejected' "
            "ORDER BY id DESC LIMIT 1",
            (connector_id, row["tenant_id"]),
        )
        last_rejection = None
        if last:
            detail = _json_object(last["detail"])
            last_rejection = {
                "reason_code": detail.get("reason_code"), "http_status": detail.get("http_status"),
                "trace_id": last["trace_id"], "occurred_at": last["created_at"],
            }
        relay = getattr(request.app.state, "kaiwu_native_relay", None)
        response.headers["Cache-Control"] = "no-store"
        return {
            "connector_id": connector_id,
            "tenant_id": row["tenant_id"],
            "tenant_export_allowed": row["tenant_id"] in settings.kaiwu_device_export_tenants,
            "revoked": bool(row["revoked"]),
            "draining": bool(row["draining"]),
            "native_relay_enabled": settings.kaiwu_native_relay_enabled,
            "native_relay_online": bool(relay and relay.is_online(connector_id)),
            "public_https_configured": urlsplit(_edge_public_base(settings)).scheme == "https",
            "last_rejection": last_rejection,
        }

    @router.post("/v1/integrations", status_code=201)
    def create_integration(
        data: IntegrationCreateRequest,
        response: Response,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        with service_obj.db.transaction():
            connector = service_obj.db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (data.connector_id,))
            if not connector or connector["revoked"]:
                raise AppError(404, "CONNECTOR_NOT_FOUND", "连接器不存在或已撤销")
            if connector["tenant_id"] != data.tenant_id:
                raise AppError(403, "INTEGRATION_TENANT_DENIED", "连接器不属于该企业")
            sources = _integration_rows(service_obj.db, data.connector_id, data.source_ids)
            issued = _issue_platform_integration(service_obj, data, sources, admin_ctx["actor_id"])
        response.headers["Cache-Control"] = "no-store"
        return issued

    @router.get("/v1/integrations")
    def list_integrations(
        connector_id: str = "",
        tenant_id: str = "",
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        del admin_ctx
        clauses = []
        values: list[str] = []
        if connector_id:
            if not CONNECTOR_ID_RE.fullmatch(connector_id):
                raise AppError(422, "CONNECTOR_ID_INVALID", "连接器编号无效")
            clauses.append("connector_id=?")
            values.append(connector_id)
        if tenant_id:
            if not TENANT_RE.fullmatch(tenant_id):
                raise AppError(422, "TENANT_ID_INVALID", "企业编号无效")
            clauses.append("tenant_id=?")
            values.append(tenant_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = service_obj.db.all(
            "SELECT * FROM kaiwu_integrations" + where + " ORDER BY created_at DESC",
            tuple(values),
        )
        return {"integrations": [_integration_view(service_obj.db, row) for row in rows]}

    @router.get("/v1/integrations/{integration_id}")
    def get_integration(
        integration_id: str,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        del admin_ctx
        if not INTEGRATION_ID_RE.fullmatch(integration_id):
            raise AppError(404, "INTEGRATION_NOT_FOUND", "企业集成不存在")
        row = service_obj.db.one("SELECT * FROM kaiwu_integrations WHERE id=?", (integration_id,))
        if not row:
            raise AppError(404, "INTEGRATION_NOT_FOUND", "企业集成不存在")
        return {"integration": _integration_view(service_obj.db, row)}

    @router.post("/v1/integrations/{integration_id}/rotate")
    def rotate_integration(
        integration_id: str,
        data: IntegrationRotateRequest,
        response: Response,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        settings: Settings = admin_ctx["settings"]
        if not INTEGRATION_ID_RE.fullmatch(integration_id):
            raise AppError(404, "INTEGRATION_NOT_FOUND", "企业集成不存在")
        row = service_obj.db.one("SELECT * FROM kaiwu_integrations WHERE id=?", (integration_id,))
        if not row:
            raise AppError(404, "INTEGRATION_NOT_FOUND", "企业集成不存在")
        if row["revoked_at"]:
            raise AppError(403, "INTEGRATION_REVOKED", "企业集成已撤销")
        connector = service_obj.db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (row["connector_id"],))
        if not connector or connector["revoked"]:
            raise AppError(403, "INTEGRATION_REVOKED", "连接器已撤销")
        source_ids = [
            item["source_id"]
            for item in service_obj.db.all(
                "SELECT source_id FROM kaiwu_integration_sources "
                "WHERE integration_id=? ORDER BY source_id",
                (integration_id,),
            )
        ]
        sources = _integration_rows(service_obj.db, row["connector_id"], source_ids)
        _edge_public_base(settings)
        token = _new_integration_token()
        ttl = data.ttl_seconds or settings.kaiwu_integration_default_ttl_seconds
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
        timestamp = now()
        trace_id = "tr_" + uid()
        with service_obj.db.transaction():
            service_obj.db.execute(
                "UPDATE kaiwu_integrations SET token_hash=?,token_prefix=?,expires_at=?,"
                "revision=revision+1,rotated_at=?,updated_at=? WHERE id=? AND revoked_at IS NULL",
                (_hash_secret(token), _token_prefix(token), expires_at, timestamp, timestamp, integration_id),
            )
            _audit(
                service_obj.db,
                tenant_id=row["tenant_id"],
                connector_id=row["connector_id"],
                source_id=None,
                actor_type="admin",
                actor_id=admin_ctx["actor_id"],
                action="integration.rotated",
                outcome="allow",
                trace_id=trace_id,
                detail={"integration_id": integration_id, "expires_at": expires_at},
            )
        updated = service_obj.db.one("SELECT * FROM kaiwu_integrations WHERE id=?", (integration_id,))
        config = _platform_config(settings, sources, token, row["display_name"])
        response.headers["Cache-Control"] = "no-store"
        return {
            "integration": _integration_view(service_obj.db, updated),
            "access_token": token,
            "mcp_config": config,
            "mcp_config_json": json.dumps(config, ensure_ascii=False, indent=2),
            "trace_id": trace_id,
        }

    @router.post("/v1/integrations/{integration_id}/revoke")
    def revoke_integration(
        integration_id: str,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        if not INTEGRATION_ID_RE.fullmatch(integration_id):
            raise AppError(404, "INTEGRATION_NOT_FOUND", "企业集成不存在")
        row = service_obj.db.one("SELECT * FROM kaiwu_integrations WHERE id=?", (integration_id,))
        if not row:
            raise AppError(404, "INTEGRATION_NOT_FOUND", "企业集成不存在")
        timestamp = now()
        trace_id = "tr_" + uid()
        with service_obj.db.transaction():
            service_obj.db.execute(
                "UPDATE kaiwu_integrations SET revoked_at=COALESCE(revoked_at,?),updated_at=?,"
                "revision=revision+1 WHERE id=?",
                (timestamp, timestamp, integration_id),
            )
            _audit(
                service_obj.db,
                tenant_id=row["tenant_id"],
                connector_id=row["connector_id"],
                source_id=None,
                actor_type="admin",
                actor_id=admin_ctx["actor_id"],
                action="integration.revoked",
                outcome="allow",
                trace_id=trace_id,
                detail={"integration_id": integration_id},
            )
        updated = service_obj.db.one("SELECT * FROM kaiwu_integrations WHERE id=?", (integration_id,))
        return {
            "integration": _integration_view(service_obj.db, updated),
            "revoked": True,
            "trace_id": trace_id,
        }

    @router.get("/v1/connectors/{connector_id}/data-plane")
    def connector_data_plane(
        connector_id: str,
        response: Response,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        if not CONNECTOR_ID_RE.fullmatch(connector_id):
            raise AppError(404, "CONNECTOR_NOT_FOUND", "连接器不存在")
        if is_native_connector(service_obj.db, connector_id):
            raise AppError(409, "NATIVE_RELAY_SELECTED", "该连接器使用开物内置中继")
        try:
            material = build_bridge_material(
                service_obj.db,
                admin_ctx["settings"],
                connector_id,
            )
        except BridgeError as exc:
            raise AppError(409, "DATA_PLANE_NOT_READY", str(exc)) from None
        response.headers["Cache-Control"] = "no-store"
        return {
            "connector_id": connector_id,
            "revision": material.revision,
            "source_ids": list(material.source_ids),
            "frps_toml": material.frps_toml,
            "visitor_toml": material.visitor_toml,
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
                    "trusted_ca_file": settings.kaiwu_control_relay_tls_client_trusted_ca_file,
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
        if is_native_connector(service_obj.db, connector_id):
            raise AppError(409, "NATIVE_RELAY_SELECTED", "该连接器使用开物内置中继")
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
                    "trusted_ca_file": settings.kaiwu_control_relay_tls_client_trusted_ca_file,
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
        with _report_source_conflict(service_obj.db, connector, data.source_id), service_obj.db.transaction():
            native = is_native_connector(service_obj.db, connector["id"])
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
                    # Only the fixed bundled demo can migrate fingerprints. All
                    # customer sources still require review on any such change.
                    stable_binding = hashlib.sha256(
                        (BUILTIN_DEMO_SOURCE_ID_NAMESPACE + ":" + data.source_id).encode("utf-8")
                    ).hexdigest()
                    schema = desired["tool_schema_sha256"]
                    migrated = {"target_binding_sha256": stable_binding, "tool_schema_sha256": schema}
                    same_schema = current["tool_schema_sha256"] == schema
                    if (
                        _is_builtin_demo(connector["id"], existing)
                        and current["display_name"] == BUILTIN_DEMO_SOURCE_NAME
                        and (current | migrated) == desired
                        and (same_schema or {current["tool_schema_sha256"], schema} <= BUILTIN_DEMO_COMPATIBLE_SCHEMA_DIGESTS)
                    ):
                        service_obj.db.execute(
                            "UPDATE kaiwu_sources SET target_binding_sha256=?,tool_schema_sha256=?,updated_at=? "
                            "WHERE connector_id=? AND source_id=?",
                            (stable_binding, schema, now(), connector["id"], data.source_id),
                        )
                        existing.update(migrated)
                        _audit(
                            service_obj.db, tenant_id=connector["tenant_id"], connector_id=connector["id"],
                            source_id=data.source_id, actor_type="device", actor_id=connector["device_key_id"],
                            action="source.builtin_demo_compatibility", outcome="allow", trace_id=trace_id,
                            detail={"schema_changed": not same_schema, "binding_changed": current["target_binding_sha256"] != stable_binding},
                        )
                    else:
                        raise AppError(409, "SOURCE_CONFIG_CONFLICT", "同一来源编号的配置发生变化，请重新审核")
                route = service_obj.db.one(
                    "SELECT 1 FROM kaiwu_edge_routes WHERE connector_id=? AND source_id=?",
                    (connector["id"], data.source_id),
                )
                if not native and not route:
                    timestamp = now()
                    visitor_port = _allocate_visitor_port(service_obj.db, device_ctx["settings"])
                    service_obj.db.execute(
                        "INSERT INTO kaiwu_edge_routes"
                        "(connector_id,source_id,visitor_host,visitor_port,created_at,updated_at)"
                        " VALUES(?,?,?,?,?,?)",
                        (
                            connector["id"],
                            data.source_id,
                            device_ctx["settings"].kaiwu_edge_visitor_host,
                            visitor_port,
                            timestamp,
                            timestamp,
                        ),
                    )
                    _audit(
                        service_obj.db,
                        tenant_id=connector["tenant_id"],
                        connector_id=connector["id"],
                        source_id=data.source_id,
                        actor_type="device",
                        actor_id=connector["device_key_id"],
                        action="source.route_migrated",
                        outcome="allow",
                        trace_id=trace_id,
                        detail={"visitor_port": visitor_port},
                    )
                return {"source": _source_view(existing, device_ctx["settings"]), "revision": connector["revision"], "trace_id": trace_id}
            registered = service_obj.db.all(
                "SELECT source_id, allowed_tools, transport FROM kaiwu_sources WHERE connector_id=?",
                (connector["id"],),
            )
            _assert_business_source_limit(connector["id"], [*registered, {"source_id": data.source_id, **desired}])
            derived = _derive_secret(
                device_ctx["settings"], "stcp", f"{connector['id']}:{data.source_id}:1"
            )
            visitor_port = None if native else _allocate_visitor_port(service_obj.db, device_ctx["settings"])
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
            if visitor_port is not None:
                service_obj.db.execute(
                    "INSERT INTO kaiwu_edge_routes"
                    "(connector_id,source_id,visitor_host,visitor_port,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (
                        connector["id"],
                        data.source_id,
                        device_ctx["settings"].kaiwu_edge_visitor_host,
                        visitor_port,
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
        sources = {
            row["source_id"]: row
            for row in service_obj.db.all(
                "SELECT source_id, allowed_tools, transport FROM kaiwu_sources WHERE connector_id=?",
                (connector_id,),
            )
        }
        if not set(data.source_ids).issubset(sources):
            raise AppError(409, "HEARTBEAT_SOURCE_UNKNOWN", "心跳包含未登记来源")
        _assert_business_source_limit(connector_id, [sources[source_id] for source_id in data.source_ids])
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

    @router.post("/v1/connectors/{connector_id}/mcp-export")
    def export_device_mcp(
        connector_id: str,
        data: DeviceExportRequest,
        request: Request,
        response: Response,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        if device_ctx["connector"]["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        settings: Settings = device_ctx["settings"]
        db = service_obj.db
        try:
            with db.transaction():
                # Device authentication happened before this transaction. Check the
                # current record again to serialize issuance with revocation.
                connector = db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
                if not connector or connector["revoked"]:
                    raise AppError(403, "DEVICE_REVOKED", "设备身份已失效")
                tenant_id = connector["tenant_id"]
                if tenant_id not in settings.kaiwu_device_export_tenants:
                    raise AppError(403, "DEVICE_EXPORT_DENIED", "该企业尚未开通复制 MCP 配置，请联系开物管理员")
                if (
                    not settings.kaiwu_native_relay_enabled or connector["draining"]
                    or not is_native_connector(db, connector_id)
                    or not request.app.state.kaiwu_native_relay.is_online(connector_id)
                ):
                    raise AppError(409, "NATIVE_RELAY_OFFLINE", "请先连接开物，再复制 MCP 配置")
                if data.source_id == _builtin_demo_source_id(connector_id):
                    raise AppError(403, "DEVICE_EXPORT_SOURCE_DENIED", "请选择已登记的业务 MCP 服务")
                source, = _integration_rows(db, connector_id, [data.source_id])
                if source["tenant_id"] != tenant_id or source["transport"] != "streamable_http":
                    raise AppError(403, "DEVICE_EXPORT_SOURCE_DENIED", "请选择本企业的 Streamable HTTP 业务 MCP")
                if urlsplit(_edge_public_base(settings)).scheme != "https":
                    raise AppError(503, "EDGE_NOT_CONFIGURED", "复制 MCP 配置需要配置 HTTPS 公网地址")

                # This namespace is used only by the signed-device endpoint. Never
                # return or rotate an administrator-issued integration token.
                latest = db.one(
                    "SELECT i.* FROM kaiwu_integrations i JOIN kaiwu_integration_sources m ON m.integration_id=i.id "
                    "WHERE i.connector_id=? AND i.tenant_id=? AND i.created_by=? AND m.source_id=? "
                    "ORDER BY i.created_at DESC,i.id DESC LIMIT 1",
                    (connector_id, tenant_id, DEVICE_EXPORT_ACTOR_PREFIX + connector_id, data.source_id),
                )
                if latest and latest["revoked_at"]:
                    raise AppError(403, "DEVICE_EXPORT_REVOKED", "这个 MCP 配置已被管理员停用，请联系开物管理员")
                if latest and latest["expires_at"] > now():
                    token = _device_export_token(settings, connector_id, latest["id"])
                    if not hmac.compare_digest(_hash_secret(token), latest["token_hash"]):
                        raise AppError(409, "DEVICE_EXPORT_TOKEN_CHANGED", "该 MCP 凭据已更新，请联系开物管理员")
                    config = _platform_config(settings, [source], token, latest["display_name"])
                else:
                    integration = IntegrationCreateRequest(
                        tenant_id=tenant_id, connector_id=connector_id, display_name=source["display_name"][:100],
                        source_ids=[data.source_id],
                    )
                    created = _issue_platform_integration(
                        service_obj, integration, [source], connector["device_key_id"], device_export=True,
                    )
                    config = created["mcp_config"]
        except AppError as exc:
            # Record after rollback. Only the authenticated device identity and
            # stable error code are recorded, never source input or credentials.
            try:
                _audit(
                    db, tenant_id=device_ctx["connector"]["tenant_id"],
                    connector_id=connector_id, source_id=None, actor_type="device",
                    actor_id=connector_id, action="device.mcp_export.rejected",
                    outcome="deny", trace_id=new_trace_id(),
                    detail={"reason_code": exc.code, "http_status": exc.status},
                )
            except Exception as audit_error:
                logging.getLogger(__name__).warning(
                    "device MCP export rejection audit failed (%s)", type(audit_error).__name__
                )
            raise
        response.headers["Cache-Control"] = "no-store"
        return {"mcp_config": config}

    @router.post("/v1/connectors/{connector_id}/self-test")
    async def self_test(
        connector_id: str,
        data: SelfTestRequest,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        expected_source_id = _builtin_demo_source_id(connector_id)
        if data.source_id != expected_source_id:
            raise AppError(
                403,
                "SELF_TEST_SOURCE_DENIED",
                "内置测试来源编号与当前连接器不匹配",
            )
        settings: Settings = device_ctx["settings"]
        source = service_obj.db.one(
            "SELECT * FROM kaiwu_sources "
            "WHERE connector_id=? AND source_id=?",
            (connector_id, data.source_id),
        )
        if (
            not source
            or source["tenant_id"] != connector["tenant_id"]
            or source["connector_id"] != connector_id
        ):
            raise AppError(403, "SELF_TEST_SOURCE_DENIED", "该来源不属于当前企业和连接器")
        try:
            allowed_tools = json.loads(source["allowed_tools"])
        except (TypeError, ValueError):
            allowed_tools = []
        if (
            not isinstance(allowed_tools, list)
            or not all(isinstance(tool, str) for tool in allowed_tools)
            or len(allowed_tools) != len(BUILTIN_DEMO_TOOLS)
            or set(allowed_tools) != BUILTIN_DEMO_TOOLS
        ):
            raise AppError(
                403,
                "SELF_TEST_SOURCE_DENIED",
                "内置测试来源必须恰好批准三个固定的 kaiwu_demo_* 只读工具",
            )
        _integration_rows(service_obj.db, connector_id, [data.source_id])
        edge_base = _self_test_edge_base(settings)
        trace_id = new_trace_id()
        integration_id, token = _create_self_test_integration(
            service_obj.db,
            connector,
            data.source_id,
            settings.kaiwu_edge_request_timeout_seconds,
        )
        cleanup_succeeded = False
        try:
            status, trace_id, stages, probe = await _execute_self_test(
                settings,
                edge_base,
                data.source_id,
                data.nonce,
                token,
                trace_id,
            )
        finally:
            try:
                cleanup_succeeded = _revoke_self_test_integration(
                    service_obj.db,
                    integration_id,
                )
            except Exception as exc:  # noqa: BLE001 - cleanup result is reported below
                log.warning(
                    "self-test temporary credential cleanup failed (%s)",
                    type(exc).__name__,
                )
            _record_self_test_cleanup(
                service_obj.db,
                connector,
                data.source_id,
                trace_id,
                cleanup_succeeded,
            )
        if not cleanup_succeeded:
            status = 500
            probe = {}
            _stage_status(stages, "cleanup", "failed", "临时测试凭据清理失败")
        response = JSONResponse(
            status_code=status,
            content={
                "data": {
                    "ok": status == 200 and bool(probe),
                    "trace_id": trace_id,
                    "source_id": data.source_id,
                    "stages": stages,
                    "probe": probe,
                }
            },
            headers={"Cache-Control": "no-store"},
        )
        if TRACE_ID_RE.fullmatch(trace_id):
            response.headers["X-Kaiwu-Trace-ID"] = trace_id
        return response

    def transition(connector_id: str, action: str, admin_ctx, service_obj) -> dict:
        db = service_obj.db
        trace_id = "tr_" + uid()
        with db.transaction():
            connector = db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
            if not connector:
                raise AppError(404, "CONNECTOR_NOT_FOUND", "连接器不存在")
            if connector["revoked"] and action != "revoke":
                raise AppError(403, "DEVICE_REVOKED", "连接器已撤销")
            draining = int(action != "resume")
            revoked = int(action == "revoke")
            changed = connector["draining"] != draining or connector["revoked"] != revoked
            if changed and revoked:
                _revoke_connector_locked(db, connector_id, now(), "admin", admin_ctx["actor_id"])
            elif changed:
                db.execute(
                    "UPDATE kaiwu_connectors SET draining=?,revision=revision+1,updated_at=? WHERE id=?",
                    (draining, now(), connector_id),
                )
            current = db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
            _audit(
                db,
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
    async def drain(connector_id: str, request: Request, admin_ctx=Depends(admin), service_obj=Depends(service)):
        result = transition(connector_id, "drain", admin_ctx, service_obj)
        await request.app.state.kaiwu_native_relay.disconnect(connector_id)
        return result

    @router.post("/v1/connectors/{connector_id}/resume")
    def resume(connector_id: str, admin_ctx=Depends(admin), service_obj=Depends(service)):
        return transition(connector_id, "resume", admin_ctx, service_obj)

    @router.post("/v1/connectors/{connector_id}/revoke")
    async def revoke(connector_id: str, request: Request, admin_ctx=Depends(admin), service_obj=Depends(service)):
        result = transition(connector_id, "revoke", admin_ctx, service_obj)
        await request.app.state.kaiwu_native_relay.disconnect(connector_id)
        return result

    @router.post("/v1/connectors/{connector_id}/self-revoke")
    async def self_revoke(
        connector_id: str,
        request: Request,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        trace_id = "tr_" + uid()
        with service_obj.db.transaction():
            _revoke_connector_locked(
                service_obj.db,
                connector_id,
                now(),
                "device",
                connector["device_key_id"],
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
        await request.app.state.kaiwu_native_relay.disconnect(connector_id)
        return {"connector_id": connector_id, "revoked": True, "trace_id": trace_id}

    @router.post("/v1/connectors/{connector_id}/events")
    async def upload_events(
        connector_id: str,
        data: EventBatch,
        request: Request,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        raw = await request.body()
        if len(raw) > MAX_BATCH_BYTES:
            raise AppError(413, "DIAGNOSTICS_BATCH_TOO_LARGE", "诊断批次正文超过 256 KiB")
        if not data.events:
            raise AppError(422, "DIAGNOSTICS_BATCH_INVALID", "诊断批次不能为空")
        if len(data.events) > MAX_BATCH_EVENTS:
            raise AppError(422, "DIAGNOSTICS_BATCH_INVALID", "单个诊断批次最多 100 条事件")
        db = service_obj.db
        grants = source_grants(db, connector_id)
        rows = [normalize_event(event, grants) for event in data.events]
        accepted = insert_rows(db, connector["tenant_id"], connector_id, rows, origin="connector")
        maybe_prune(service_obj.db)
        return {"accepted": accepted}

    @router.get("/v1/connectors/{connector_id}/platform-events")
    def platform_events(
        connector_id: str,
        limit: int = DEFAULT_DEVICE_EVENTS,
        device_ctx=Depends(device),
        service_obj=Depends(service),
    ):
        connector = device_ctx["connector"]
        if connector["id"] != connector_id:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
        # Scope comes only from the authenticated connector and the path; the
        # unsigned limit query can narrow the page size but never widen scope.
        events = query_connector_events(
            service_obj.db,
            connector_id,
            limit=limit,
            edge_only=True,
        )
        return {"events": events}

    @router.get("/v1/diagnostics/events")
    def admin_events(
        limit: int = DEFAULT_ADMIN_EVENTS,
        cursor: str | None = None,
        connector_id: str | None = None,
        source_id: str | None = None,
        level: str | None = None,
        trace_id: str | None = None,
        event_code: str | None = None,
        install_attempt_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        admin_ctx=Depends(admin),
        service_obj=Depends(service),
    ):
        del admin_ctx
        validate_filter("connector_id", connector_id, CONNECTOR_ID_RE)
        validate_filter("source_id", source_id, SOURCE_ID_RE)
        validate_filter("trace_id", trace_id, TRACE_ID_RE)
        validate_filter("install_attempt_id", install_attempt_id, INSTALL_ATTEMPT_ID_RE)
        if level and level not in LEVELS:
            raise AppError(422, "DIAGNOSTICS_FILTER_INVALID", "诊断筛选条件无效：level")
        if event_code and event_code not in CATALOG:
            raise AppError(422, "DIAGNOSTICS_FILTER_INVALID", "诊断筛选条件无效：event_code")
        cursor_id = None
        if cursor:
            if not cursor.isdigit():
                raise AppError(422, "DIAGNOSTICS_FILTER_INVALID", "诊断筛选条件无效：cursor")
            cursor_id = int(cursor)
        since_value = _optional_time(since, "since")
        until_value = _optional_time(until, "until")
        if since_value and until_value and since_value > until_value:
            raise AppError(422, "DIAGNOSTICS_FILTER_INVALID", "诊断筛选条件无效：时间范围")
        events, next_cursor = query_admin_events(
            service_obj.db,
            limit=limit,
            cursor=cursor_id,
            connector_id=connector_id,
            source_id=source_id,
            level=level,
            trace_id=trace_id,
            event_code=event_code,
            install_attempt_id=install_attempt_id,
            since=since_value,
            until=until_value,
        )
        return {"events": events, "next_cursor": next_cursor}

    @router.get("/ops", response_class=HTMLResponse, include_in_schema=False)
    def ops():
        # Deliberately unauthenticated and data-free: the shell contains no
        # events, and the administrator token is only entered into page memory.
        return HTMLResponse(OPS_PAGE, headers=ops_page_headers())

    from .kaiwu_assist import add_device_assist
    from .kaiwu_platform_export import add_device_handoff

    add_device_assist(router, service, device)
    add_device_handoff(router, service, device)
    return router
