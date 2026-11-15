"""Tenant-scoped OpenClaw compatibility facade (first auditable slice).

This module adds an independent ``/v1`` sidecar boundary next to the employee
Cookie API.  It deliberately does not read ``zhixing_session`` cookies and it
does not forward an OpenClaw Gateway management token: callers must present a
tenant-scoped bearer token issued by :class:`OpenClawTenantStore`.  Execution
is delegated to an injected executor.  The default executor is unavailable, so
mounting the sidecar cannot accidentally make an unapproved paid model call.

The external contract covers Chat Completions and OpenResponses non-streaming
responses, SSE, function-tool round trips, session continuation, idempotency,
model/agent whitelisting, bounded errors and cancellation.  Gateway WebSocket
RPC, embeddings, image input and a real Harness executor are explicitly out of
scope for this slice; see ``docs/design/OPENCLAW_AGENT_HARNESS_V2_CONTRACT.md``.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Literal, Mapping, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .db import dump, now, uid
from .middleware import BodyLimitMiddleware
from .openai_compat import (
    MAX_MESSAGES,
    MAX_OUTPUT_TOKENS,
    MAX_TEXT_CHARS,
    MAX_TOOL_CHARS,
    MAX_TOOLS,
    NormalizedResult,
    NormalizedUsage,
    NormalizedTool,
)

COMPAT_VERSION = "openclaw-compat/2026-11-12.2"
AGENT_HARNESS_V2_CONTRACT_VERSION = "zhixing-openclaw-agent-harness-v2/2026-11-12.2"
CANDIDATE_OPENCLAW_VERSION = "v2026.11.3"
TOKEN_PREFIX = "zxoc_"
MAX_BODY_BYTES = 1_048_576
MAX_SESSION_KEY_CHARS = 200
MAX_IDEMPOTENCY_KEY_CHARS = 200
MAX_TOTAL_TEXT_CHARS = 200_000
MAX_METADATA_KEYS = 20
DEFAULT_EXECUTOR_TIMEOUT_SECONDS = 60.0
DEFAULT_LEASE_SECONDS = 120.0
RESPONSE_TTL_SECONDS = 24 * 60 * 60

_ALIAS = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}$")
_SESSION_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_RESERVED_SESSION_PREFIXES = (
    "admin",
    "system",
    "internal",
    "operator",
    "openclaw-admin",
    "openclaw-operator",
)
# Server-derived end-user sessions use ``user:<digest>``.  Callers may not
# write into that namespace: otherwise one tenant principal could guess
# another end-user's alias and continue that session by passing the derived
# key explicitly.
_RESERVED_SESSION_NAMESPACES = ("user:",)

AGENT_HARNESS_V2_CONTRACT: dict[str, Any] = {
    # The OpenClaw release page showed v2026.11.3 when the design was written.
    # This is a *candidate* lock; the customer instance version is still an
    # external input and must be confirmed before a version-level adapter is
    # claimed as compatible.
    "contract": "zhixing-openclaw-agent-harness-v2",
    "contract_version": AGENT_HARNESS_V2_CONTRACT_VERSION,
    "locked_openclaw_version": CANDIDATE_OPENCLAW_VERSION,
    "lock_state": "candidate_pending_customer_version_confirmation",
    "slice_scope": "synthetic_first_slice_no_real_harness_executor_or_mcp_transport",
    "harness_kind": "single_turn_executor_with_native_session_and_events",
    # ``supported`` is strictly what this slice implements.  The aspirational
    # contract surface lives in ``target_only`` so evidence and clients cannot
    # mistake a target capability for a delivered one.
    "supported": (
        "turn.execute",
        "turn.cancel",
        "session.continue",
        "tool.call",
        "tool.cancel",
        "approval.request",
    ),
    "capability_scope": {
        "turn.execute": "facade_protocol_boundary_with_injected_executor",
        "turn.cancel": "in_process_active_run_cancel_only",
        "session.continue": "binding_only_no_history_replay",
        "tool.call": "synthetic_connector_boundary_no_real_mcp_transport",
        "tool.cancel": "synthetic_connector_active_call_cancel",
        "approval.request": "synthetic_connector_local_approval_not_wired_to_facade_or_events",
    },
    "target_only": (
        "context.summary",
        "memory.read",
        "memory.propose_write",
        "approval.resume",
        "event.run.stage",
        "event.tool",
        "event.approval",
        "event.message",
        "event.usage",
        "event.run.completed",
    ),
    "unsupported": (
        "gateway.websocket_rpc",
        "gateway.control_plane",
        "channel.send",
        "node.control",
        "device.pairing",
        "plugin.install",
        "plugin.auto_update",
        "config.write",
        "admin.rpc",
        "skill.auto_install",
        "provider.model_passthrough",
        "embeddings",
        "image.input",
        "audio.input",
        "gateway.history_replay",
    ),
    "deny_by_default": (
        "unknown_tool",
        "high_risk_tool",
        "plugin_install",
        "config_write",
        "cross_tenant_session",
        "unapproved_model_alias",
        "openclaw_management_token",
        "employee_cookie_session",
    ),
    "required_turn_fields": (
        "turn_id",
        "session_id",
        "agent_id",
        "context_digest",
        "messages",
        "tool_policy_digest",
        "event_stream_id",
    ),
    # Per-field delivery status against ExecutionRequest/ExecutionContext.
    # ``request_digest`` is database idempotency metadata, not a context digest
    # handed to the executor, so ``context_digest`` is explicitly not delivered.
    "turn_field_status": {
        "turn_id": "delivered_to_executor",
        "session_id": "delivered_to_executor",
        "agent_id": "delivered_to_executor",
        "context_digest": "not_delivered_request_digest_is_database_idempotency_only",
        "messages": "delivered_to_executor",
        "tool_policy_digest": "not_delivered_tools_list_passed_without_digest",
        "event_stream_id": "not_delivered_buffered_sse_has_no_persistent_stream_id",
    },
    "turn_fields_not_yet_delivered": (
        "context_digest",
        "tool_policy_digest",
        "event_stream_id",
    ),
    "recovery_model": "owner_instance_plus_lease_expiry_heartbeat",
    "terminal_states": ("completed", "cancelled", "failed", "interrupted"),
}


def harness_contract_manifest() -> dict[str, Any]:
    """Return a copy so callers cannot mutate the locked contract in place."""
    return json.loads(json.dumps(AGENT_HARNESS_V2_CONTRACT))


class OpenClawError(Exception):
    """Bounded public error with an OpenAI-compatible envelope."""

    def __init__(self, status: int, code: str, message: str, *, param: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.param = param

    def as_error(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error" if self.status < 500 else "server_error",
                "param": self.param,
                "code": self.code,
            }
        }


def _error_for_status(status: int) -> OpenClawError:
    if status in (401, 403):
        return OpenClawError(401, "AUTHENTICATION_FAILED", "兼容入口认证失败，请检查租户访问令牌")
    if status == 404:
        return OpenClawError(404, "RESOURCE_NOT_FOUND", "请求的模型、助手或会话不存在")
    if status in (408, 504):
        return OpenClawError(504, "MODEL_TIMEOUT", "模型执行超时，请稍后重试")
    if status == 429:
        return OpenClawError(429, "MODEL_RATE_LIMITED", "模型服务请求过于频繁或已达到用量限制")
    if status == 402:
        return OpenClawError(402, "MODEL_INSUFFICIENT_BALANCE", "模型服务额度不足，请联系管理员")
    if 500 <= status <= 599:
        return OpenClawError(503, "MODEL_SERVICE_UNAVAILABLE", "模型服务暂时不可用，请稍后重试")
    return OpenClawError(502, "MODEL_EXECUTION_FAILED", "模型执行未完成，请联系管理员检查配置")


def _sha256(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _now_iso() -> str:
    return now()


def _header(headers: Mapping[str, Any] | None, name: str) -> str | None:
    if headers is None:
        return None
    target = name.lower()
    # Plain dicts are compared case-insensitively; Starlette Headers already
    # lowercases its lookup keys.
    if hasattr(headers, "get"):
        value = headers.get(target)
        if value is not None:
            return str(value)
        for key, item in getattr(headers, "items", lambda: [])():
            if str(key).lower() == target:
                return str(item)
    return None


def _bounded_text(value: Any, field_name: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 必须是字符串", param=field_name)
    if len(value) > limit:
        raise OpenClawError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制", param=field_name)
    return value


def _bounded_json(value: Any, field_name: str, *, limit: int = MAX_TOOL_CHARS) -> Any:
    if isinstance(value, (dict, list)):
        try:
            size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError, RecursionError):
            raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 格式无效", param=field_name) from None
        if size > limit:
            raise OpenClawError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制", param=field_name)
        return value
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > limit:
            raise OpenClawError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制", param=field_name)
        return value
    raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 格式无效", param=field_name)


def _valid_alias(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _ALIAS.fullmatch(value):
        raise OpenClawError(400, "INVALID_AGENT_ALIAS", f"{field_name} 格式无效", param=field_name)
    return value


def _valid_session_key(value: Any) -> str:
    if not isinstance(value, str) or not _SESSION_KEY.fullmatch(value) or len(value) > MAX_SESSION_KEY_CHARS:
        raise OpenClawError(400, "INVALID_SESSION_KEY", "x-openclaw-session-key 格式无效")
    lowered = value.lower()
    if (any(lowered.startswith(prefix) for prefix in _RESERVED_SESSION_PREFIXES)
            or any(lowered.startswith(namespace) for namespace in _RESERVED_SESSION_NAMESPACES)):
        raise OpenClawError(400, "RESERVED_SESSION_KEY", "该会话命名空间由服务端保留")
    return value


def _valid_user_alias(value: Any) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 200):
        raise OpenClawError(400, "INVALID_REQUEST", "user 必须是 1–200 个字符的字符串", param="user")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise OpenClawError(400, "INVALID_REQUEST", "user 包含无效控制字符", param="user")
    return value


def _valid_idempotency_key(value: Any) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_IDEMPOTENCY_KEY_CHARS):
        raise OpenClawError(400, "INVALID_IDEMPOTENCY_KEY", "幂等键必须是 1–200 个字符的字符串")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise OpenClawError(400, "INVALID_IDEMPOTENCY_KEY", "幂等键包含无效控制字符")
    return value


def _parse_json_arguments(value: Any, field_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return _bounded_json(value, field_name)
    if isinstance(value, str):
        if len(value) > MAX_TOOL_CHARS:
            raise OpenClawError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制", param=field_name)
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError, RecursionError):
            raise OpenClawError(400, "TOOL_ARGUMENTS_INVALID", f"{field_name} 必须是合法 JSON 对象", param=field_name) from None
        if isinstance(parsed, dict):
            return parsed
    raise OpenClawError(400, "TOOL_ARGUMENTS_INVALID", f"{field_name} 必须是 JSON 对象", param=field_name)


@dataclass(frozen=True)
class ExternalToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    arguments_json: str


@dataclass(frozen=True)
class ExternalMessage:
    role: str
    text: str | None
    tool_calls: tuple[ExternalToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class TenantPrincipal:
    tenant_id: str
    tenant_name: str
    user_id: str
    token_id: str
    allowed_agents: tuple[str, ...]
    assistant_aliases: Mapping[str, str]

    @property
    def principal_id(self) -> str:
        # Stable service identity, not the rotating credential.  Token
        # rotation must not orphan sessions, while a different tenant with the
        # same internal user still maps to a different namespace.
        return f"tenant:{self.tenant_id}:user:{self.user_id}"

    @property
    def allowed_models(self) -> frozenset[str]:
        values = {"openclaw"}
        for agent in self.allowed_agents:
            values.add("openclaw/default" if agent == "default" else f"openclaw/{agent}")
        return frozenset(values)


@dataclass(frozen=True)
class SessionBinding:
    id: str
    tenant_id: str
    principal_id: str
    agent_id: str
    assistant_id: str
    session_key: str
    conversation_id: str
    user_digest: str | None


@dataclass(frozen=True)
class ValidatedRequest:
    protocol: Literal["chat.completions", "responses"]
    model_alias: str
    messages: tuple[ExternalMessage, ...]
    tools: tuple[NormalizedTool, ...]
    tool_choice: str | dict[str, Any]
    max_output_tokens: int | None
    temperature: float | None
    stream: bool
    instructions: str | None
    metadata: Mapping[str, str]
    metadata_digest: str
    store: bool
    previous_response_id: str | None
    user_alias: str | None
    message_channel: str | None
    required_tool_call_ids: tuple[str, ...]
    provided_tool_call_ids: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionRequest:
    protocol: Literal["chat.completions", "responses"]
    model_alias: str
    agent_id: str
    messages: tuple[ExternalMessage, ...]
    tools: tuple[NormalizedTool, ...]
    tool_choice: str | dict[str, Any]
    max_output_tokens: int | None
    temperature: float | None
    stream: bool
    instructions: str | None
    metadata_digest: str
    request_id: str
    message_channel: str | None


@dataclass(frozen=True)
class ExecutionContext:
    tenant_id: str
    principal_id: str
    session_id: str
    conversation_id: str
    agent_id: str
    model_alias: str
    run_id: str
    turn_id: str


class OpenClawExecutor(Protocol):
    """Injected turn executor.  It never receives the bearer token."""

    async def execute(self, request: ExecutionRequest, context: ExecutionContext, cancelled: asyncio.Event) -> Any: ...


class UnavailableExecutor:
    """Fail closed until an approved native executor is bound."""

    async def execute(self, request: ExecutionRequest, context: ExecutionContext, cancelled: asyncio.Event) -> Any:
        raise OpenClawError(
            503,
            "MODEL_UNAVAILABLE",
            "OpenClaw 兼容入口已接入，但本轮尚未绑定经批准的模型执行器",
        )


@dataclass
class ActiveRun:
    run_id: str
    turn_id: str
    tenant_id: str
    principal_id: str
    session_id: str
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    lease_lost: bool = False


@dataclass(frozen=True)
class TurnOutcome:
    stream: bool
    body: dict[str, Any] | None
    events: tuple[dict[str, Any], ...]
    response_id: str
    run_id: str
    session_id: str

    def headers(self) -> dict[str, str]:
        return {
            "X-OpenClaw-Compat-Version": COMPAT_VERSION,
            "X-OpenClaw-Session-Id": self.session_id,
            "X-OpenClaw-Run-Id": self.run_id,
            "X-OpenClaw-Response-Id": self.response_id,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS openclaw_tenants(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    assistant_aliases TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    disabled_at TEXT
);
CREATE TABLE IF NOT EXISTS openclaw_tenant_tokens(
    token_hash TEXT PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL REFERENCES openclaw_tenants(id),
    label TEXT NOT NULL,
    prefix TEXT NOT NULL,
    allowed_agents TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS openclaw_tokens_tenant ON openclaw_tenant_tokens(tenant_id);
CREATE TABLE IF NOT EXISTS openclaw_sessions(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    user_digest TEXT,
    assistant_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(tenant_id, principal_id, agent_id, session_key)
);
CREATE INDEX IF NOT EXISTS openclaw_sessions_conversation ON openclaw_sessions(conversation_id);
CREATE TABLE IF NOT EXISTS openclaw_turns(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES openclaw_sessions(id),
    conversation_id TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    protocol TEXT NOT NULL,
    model_alias TEXT NOT NULL,
    idempotency_key TEXT,
    request_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    stream INTEGER NOT NULL DEFAULT 0,
    response_json TEXT,
    sse_json TEXT,
    error_json TEXT,
    owner_instance TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(tenant_id, principal_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS openclaw_turns_session ON openclaw_turns(session_id, created_at);
CREATE TABLE IF NOT EXISTS openclaw_responses(
    response_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES openclaw_turns(id),
    session_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS openclaw_responses_session ON openclaw_responses(session_id, created_at);
CREATE TABLE IF NOT EXISTS openclaw_tool_calls(
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, call_id)
);
CREATE TABLE IF NOT EXISTS openclaw_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT,
    principal_id TEXT,
    session_id TEXT,
    run_id TEXT,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS openclaw_audit_tenant ON openclaw_audit(tenant_id, id);
"""


def _audit_detail(detail: Mapping[str, Any] | None) -> str:
    """Keep audit metadata bounded and free of content or credentials."""
    if not detail:
        return "{}"
    cleaned: dict[str, Any] = {}
    for key, value in list(detail.items())[:30]:
        if isinstance(value, (bool, int, float)) or value is None:
            cleaned[str(key)[:64]] = value
        elif isinstance(value, str):
            cleaned[str(key)[:64]] = value[:200]
        elif isinstance(value, (list, tuple)):
            cleaned[str(key)[:64]] = [str(item)[:64] for item in list(value)[:20]]
    try:
        return dump(cleaned)[:4000]
    except (TypeError, ValueError):
        return "{}"


def _lease_deadline(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _ensure_columns(db, table: str, columns: Mapping[str, str]) -> None:
    """Add lease columns to databases created by the first OC-T12 slice."""
    existing = {row["name"] for row in db.all(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


class OpenClawTenantStore:
    """Tenant tokens and session bindings in the product database.

    Tokens are stored only as SHA-256 hashes.  A token is a *tenant service
    credential*; employee Cookie sessions are rejected explicitly so the two
    credential systems cannot be confused.
    """

    def __init__(self, db, *, session_cookie_name="zhixing_session"):
        self.db = db
        self.employee_cookie_names = frozenset({"zhixing_session", session_cookie_name})
        with self.db.lock:
            self.db.conn.executescript(_SCHEMA)
        _ensure_columns(self.db, "openclaw_turns",
                        {"owner_instance": "TEXT", "lease_expires_at": "TEXT"})
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS openclaw_turns_lease "
            "ON openclaw_turns(status,lease_expires_at)"
        )

    def _audit(self, *, tenant_id: str | None, principal_id: str | None, action: str, outcome: str,
               detail: Mapping[str, Any] | None = None, session_id: str | None = None,
               run_id: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO openclaw_audit(tenant_id,principal_id,session_id,run_id,action,outcome,detail,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (tenant_id, principal_id, session_id, run_id, action, outcome, _audit_detail(detail), _now_iso()),
        )

    def create_tenant(self, name: str, user_id: str, assistant_aliases: Mapping[str, str]) -> dict[str, Any]:
        if not isinstance(name, str) or not (1 <= len(name) <= 100):
            raise OpenClawError(422, "INVALID_TENANT", "租户名称无效")
        if not isinstance(assistant_aliases, Mapping) or not assistant_aliases:
            raise OpenClawError(422, "INVALID_TENANT", "租户必须至少绑定一个助手别名")
        aliases: dict[str, str] = {}
        for alias, assistant_id in assistant_aliases.items():
            aliases[_valid_alias(alias, "assistant_alias")] = assistant_id
        if "default" not in aliases:
            raise OpenClawError(422, "INVALID_TENANT", "租户必须包含 default 助手别名")
        user = self.db.one("SELECT * FROM users WHERE id=? AND enabled=1", (user_id,))
        if not user:
            raise OpenClawError(422, "INVALID_TENANT", "租户服务主体不存在或已停用")
        for assistant_id in aliases.values():
            if not isinstance(assistant_id, str) or not self.db.one(
                "SELECT id FROM assistants WHERE id=? AND deleted=0 AND enabled=1", (assistant_id,)
            ):
                raise OpenClawError(422, "INVALID_TENANT", "租户绑定的助手不存在或已停用")
        tenant_id = uid()
        self.db.execute(
            "INSERT INTO openclaw_tenants(id,name,user_id,assistant_aliases,enabled,created_at) VALUES(?,?,?,?,1,?)",
            (tenant_id, name, user_id, dump(aliases), _now_iso()),
        )
        self._audit(tenant_id=tenant_id, principal_id=None, action="tenant.created", outcome="allowed",
                    detail={"aliases": sorted(aliases)})
        return {"id": tenant_id, "name": name, "user_id": user_id, "assistant_aliases": aliases, "enabled": True}

    def issue_token(self, tenant_id: str, label: str = "", *, allowed_agents: list[str] | tuple[str, ...] | None = None,
                    ttl_seconds: int | None = None) -> dict[str, Any]:
        tenant = self.db.one("SELECT * FROM openclaw_tenants WHERE id=? AND enabled=1", (tenant_id,))
        if not tenant:
            raise OpenClawError(404, "TENANT_NOT_FOUND", "租户不存在或已停用")
        aliases = json.loads(tenant["assistant_aliases"])
        agents = list(aliases) if allowed_agents is None else list(allowed_agents)
        if not agents or len(agents) != len(set(agents)) or any(agent not in aliases for agent in agents):
            raise OpenClawError(422, "INVALID_TOKEN_SCOPE", "令牌允许的助手别名无效")
        if not isinstance(label, str) or len(label) > 100:
            raise OpenClawError(422, "INVALID_TOKEN_LABEL", "令牌标签无效")
        if ttl_seconds is not None and (type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 31_536_000):
            raise OpenClawError(422, "INVALID_TOKEN_TTL", "令牌有效期无效")
        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = uid()
        expires_at = None
        if ttl_seconds is not None:
            expires_at = datetime.fromtimestamp(time.time() + ttl_seconds, timezone.utc).isoformat()
        self.db.execute(
            "INSERT INTO openclaw_tenant_tokens(token_hash,id,tenant_id,label,prefix,allowed_agents,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (_sha256(plaintext), token_id, tenant_id, label, plaintext[:12], dump(agents), expires_at, _now_iso()),
        )
        self._audit(tenant_id=tenant_id, principal_id=token_id, action="token.issued", outcome="allowed",
                    detail={"label": label, "agents": agents, "expires_at": expires_at})
        return {"token": plaintext, "token_id": token_id, "tenant_id": tenant_id, "expires_at": expires_at}

    def revoke_token(self, token_id: str, *, tenant_id: str | None = None) -> bool:
        query = "UPDATE openclaw_tenant_tokens SET revoked_at=? WHERE id=? AND revoked_at IS NULL"
        values: tuple[Any, ...] = (_now_iso(), token_id)
        if tenant_id:
            query += " AND tenant_id=?"
            values += (tenant_id,)
        changed = self.db.execute(query, values).rowcount
        if changed:
            row = self.db.one("SELECT tenant_id FROM openclaw_tenant_tokens WHERE id=?", (token_id,))
            self._audit(tenant_id=row["tenant_id"] if row else tenant_id, principal_id=token_id,
                        action="token.revoked", outcome="allowed")
        return bool(changed)

    def disable_tenant(self, tenant_id: str) -> bool:
        changed = self.db.execute(
            "UPDATE openclaw_tenants SET enabled=0,disabled_at=? WHERE id=? AND enabled=1", (_now_iso(), tenant_id)
        ).rowcount
        if changed:
            self._audit(tenant_id=tenant_id, principal_id=None, action="tenant.disabled", outcome="allowed")
        return bool(changed)

    def authenticate(self, headers: Mapping[str, Any] | None) -> TenantPrincipal:
        cookie = _header(headers, "cookie")
        if cookie and any(name + "=" in cookie for name in self.employee_cookie_names):
            self._audit(tenant_id=None, principal_id=None, action="auth.failed", outcome="denied",
                        detail={"reason": "employee_cookie_not_accepted"})
            raise OpenClawError(401, "EMPLOYEE_SESSION_NOT_ALLOWED", "兼容入口不接受员工网页会话，请使用租户访问令牌")
        authorization = _header(headers, "authorization") or ""
        scheme, _, value = authorization.partition(" ")
        token = value.strip()
        if scheme.lower() != "bearer" or not token:
            self._audit(tenant_id=None, principal_id=None, action="auth.failed", outcome="denied",
                        detail={"reason": "bearer_required"})
            raise OpenClawError(401, "AUTHENTICATION_FAILED", "请使用 Bearer 租户访问令牌")
        if not token.startswith(TOKEN_PREFIX):
            self._audit(tenant_id=None, principal_id=None, action="auth.failed", outcome="denied",
                        detail={"reason": "non_tenant_token"})
            raise OpenClawError(401, "AUTHENTICATION_FAILED", "请使用知行签发的租户访问令牌")
        row = self.db.one(
            """SELECT t.*, tenants.name tenant_name, tenants.user_id, tenants.assistant_aliases,
                      tenants.enabled tenant_enabled, users.enabled user_enabled
               FROM openclaw_tenant_tokens t
               JOIN openclaw_tenants tenants ON tenants.id=t.tenant_id
               JOIN users ON users.id=tenants.user_id
               WHERE t.token_hash=?""",
            (_sha256(token),),
        )
        if not row:
            self._audit(tenant_id=None, principal_id=None, action="auth.failed", outcome="denied",
                        detail={"reason": "unknown_token"})
            raise OpenClawError(401, "AUTHENTICATION_FAILED", "请使用知行签发的租户访问令牌")
        if row["revoked_at"]:
            self._audit(tenant_id=row["tenant_id"], principal_id=row["id"], action="auth.failed", outcome="denied",
                        detail={"reason": "token_revoked"})
            raise OpenClawError(401, "TOKEN_REVOKED", "租户访问令牌已撤销")
        if row["expires_at"] and row["expires_at"] <= _now_iso():
            self._audit(tenant_id=row["tenant_id"], principal_id=row["id"], action="auth.failed", outcome="denied",
                        detail={"reason": "token_expired"})
            raise OpenClawError(401, "TOKEN_EXPIRED", "租户访问令牌已过期")
        if not row["tenant_enabled"] or not row["user_enabled"]:
            self._audit(tenant_id=row["tenant_id"], principal_id=row["id"], action="auth.failed", outcome="denied",
                        detail={"reason": "tenant_or_principal_disabled"})
            raise OpenClawError(401, "TENANT_DISABLED", "租户或服务主体已停用")
        aliases = json.loads(row["assistant_aliases"])
        agents = tuple(json.loads(row["allowed_agents"]))
        if not agents or any(agent not in aliases for agent in agents):
            self._audit(tenant_id=row["tenant_id"], principal_id=row["id"], action="auth.failed", outcome="denied",
                        detail={"reason": "token_scope_invalid"})
            raise OpenClawError(401, "AUTHENTICATION_FAILED", "租户访问令牌范围无效")
        principal = TenantPrincipal(
            tenant_id=row["tenant_id"],
            tenant_name=row["tenant_name"],
            user_id=row["user_id"],
            token_id=row["id"],
            allowed_agents=agents,
            assistant_aliases={agent: aliases[agent] for agent in agents},
        )
        self._audit(tenant_id=principal.tenant_id, principal_id=principal.principal_id,
                    action="auth.succeeded", outcome="allowed", detail={"token_id": principal.token_id})
        return principal

    def audit(self, *, tenant_id: str | None, principal_id: str | None, action: str, outcome: str,
              detail: Mapping[str, Any] | None = None, session_id: str | None = None,
              run_id: str | None = None) -> None:
        self._audit(tenant_id=tenant_id, principal_id=principal_id, action=action, outcome=outcome,
                    detail=detail, session_id=session_id, run_id=run_id)


def _model_alias_for(agent_id: str) -> str:
    return "openclaw/default" if agent_id == "default" else f"openclaw/{agent_id}"


def _agent_from_model(model: Any) -> str:
    if not isinstance(model, str) or not (1 <= len(model) <= 200):
        raise OpenClawError(400, "INVALID_REQUEST", "model 必须是非空字符串", param="model")
    if model in {"openclaw", "openclaw/default"}:
        return "default"
    if model.startswith("openclaw/"):
        return _valid_alias(model[len("openclaw/"):], "model")
    raise OpenClawError(404, "MODEL_NOT_ALLOWED", "请求的模型未被租户授权", param="model")


def _parse_content(value: Any, field_name: str, *, allow_none: bool = False) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 不能为空", param=field_name)
    if isinstance(value, str):
        return _bounded_text(value, field_name)
    if not isinstance(value, list) or len(value) > 64:
        raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 格式无效", param=field_name)
    parts: list[str] = []
    for index, block in enumerate(value):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise OpenClawError(400, "INVALID_REQUEST", f"{field_name}[{index}] 格式无效", param=field_name)
        kind = block["type"]
        if kind in {"text", "input_text", "output_text"}:
            parts.append(_bounded_text(block.get("text"), f"{field_name}[{index}].text"))
        elif kind in {"image_url", "input_image"}:
            raise OpenClawError(400, "UNSUPPORTED_CONTENT", "本阶段兼容入口不支持图片输入", param=field_name)
        else:
            raise OpenClawError(400, "UNSUPPORTED_CONTENT", f"不支持的内容类型：{kind}", param=field_name)
    if not parts and not allow_none:
        raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 不能为空", param=field_name)
    return "".join(parts)


def _parse_tool_calls(value: Any, field_name: str) -> tuple[ExternalToolCall, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not (1 <= len(value) <= MAX_TOOLS):
        raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 数量无效", param=field_name)
    result: list[ExternalToolCall] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise OpenClawError(400, "INVALID_REQUEST", f"{field_name}[{index}] 格式无效", param=field_name)
        if item.get("type") not in (None, "function"):
            raise OpenClawError(400, "INVALID_REQUEST", f"{field_name}[{index}] 仅支持 function", param=field_name)
        function = item.get("function")
        if not isinstance(function, dict):
            # Responses style: name/arguments sit on the item itself.
            function = item
        call_id = item.get("id", item.get("call_id"))
        if not isinstance(call_id, str) or not (1 <= len(call_id) <= 128):
            raise OpenClawError(400, "INVALID_REQUEST", f"{field_name}[{index}].id 无效", param=field_name)
        name = function.get("name")
        if not isinstance(name, str) or not (1 <= len(name) <= 128):
            raise OpenClawError(400, "INVALID_REQUEST", f"{field_name}[{index}].name 无效", param=field_name)
        arguments = _parse_json_arguments(function.get("arguments", {}), f"{field_name}[{index}].arguments")
        result.append(ExternalToolCall(call_id, name, arguments,
                                       json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))))
    ids = [item.id for item in result]
    if len(ids) != len(set(ids)):
        raise OpenClawError(400, "INVALID_REQUEST", f"{field_name} 包含重复调用编号", param=field_name)
    return tuple(result)


def _parse_tools(value: Any) -> tuple[NormalizedTool, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_TOOLS:
        raise OpenClawError(400, "INVALID_REQUEST", "tools 数量超出限制", param="tools")
    result: list[NormalizedTool] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or item.get("type") not in (None, "function"):
            raise OpenClawError(400, "INVALID_REQUEST", f"tools[{index}] 仅支持 function", param="tools")
        function = item.get("function")
        if not isinstance(function, dict):
            function = item
        name = function.get("name")
        if not isinstance(name, str) or not (1 <= len(name) <= 128):
            raise OpenClawError(400, "INVALID_REQUEST", f"tools[{index}].name 无效", param="tools")
        description = function.get("description", "")
        if not isinstance(description, str) or len(description) > 2000:
            raise OpenClawError(400, "INVALID_REQUEST", f"tools[{index}].description 无效", param="tools")
        parameters = function.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(parameters, dict):
            raise OpenClawError(400, "INVALID_REQUEST", f"tools[{index}].parameters 无效", param="tools")
        _bounded_json(parameters, f"tools[{index}].parameters")
        result.append(NormalizedTool(name, description, parameters))
    names = [item.name for item in result]
    if len(names) != len(set(names)):
        raise OpenClawError(400, "INVALID_REQUEST", "tools 不能包含重复名称", param="tools")
    return tuple(result)


def _parse_tool_choice(value: Any, tools: tuple[NormalizedTool, ...]) -> str | dict[str, Any]:
    if value is None:
        return "auto"
    if isinstance(value, str) and value in {"auto", "none", "required"}:
        return value
    if isinstance(value, dict):
        function = value.get("function") if isinstance(value.get("function"), dict) else value
        name = function.get("name") if isinstance(function, dict) else None
        if isinstance(name, str) and name in {tool.name for tool in tools}:
            return {"type": "function", "name": name}
    raise OpenClawError(400, "INVALID_TOOL_CHOICE", "tool_choice 无效或引用了未声明的工具", param="tool_choice")


def _parse_metadata(value: Any) -> tuple[dict[str, str], str]:
    if value is None:
        return {}, _sha256("{}")
    if not isinstance(value, dict) or len(value) > MAX_METADATA_KEYS:
        raise OpenClawError(400, "INVALID_REQUEST", "metadata 必须是有限的键值对象", param="metadata")
    cleaned: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not (1 <= len(key) <= 64) or not isinstance(item, str) or len(item) > 200:
            raise OpenClawError(400, "INVALID_REQUEST", "metadata 仅支持 64 字符键和 200 字符字符串值", param="metadata")
        cleaned[key] = item
    canonical = json.dumps(cleaned, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return cleaned, _sha256(canonical)


_CHAT_FIELDS = {
    "model", "messages", "tools", "tool_choice", "stream", "stream_options", "max_tokens",
    "max_completion_tokens", "max_output_tokens", "temperature", "user", "metadata",
}
_RESPONSES_FIELDS = {
    "model", "input", "instructions", "tools", "tool_choice", "stream", "max_output_tokens",
    "temperature", "user", "previous_response_id", "metadata", "store", "parallel_tool_calls",
}
_EXPLICITLY_UNSUPPORTED = {
    "n", "stop", "presence_penalty", "frequency_penalty", "top_p", "logprobs", "top_logprobs",
    "response_format", "seed", "logit_bias", "audio", "modalities", "reasoning", "text",
    "truncation", "include", "background", "service_tier", "prompt_cache_key", "safety_identifier",
}


def _reject_unknown_fields(payload: dict[str, Any], allowed: set[str]) -> None:
    for key in payload:
        if key in allowed:
            continue
        if key in _EXPLICITLY_UNSUPPORTED:
            raise OpenClawError(400, "UNSUPPORTED_PARAMETER", f"本阶段不支持参数：{key}", param=key)
        raise OpenClawError(400, "UNSUPPORTED_PARAMETER", f"未知参数：{key}", param=key)


def _validate_chat_messages(payload: dict[str, Any]) -> tuple[tuple[ExternalMessage, ...], tuple[str, ...], tuple[str, ...]]:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list) or not (1 <= len(raw_messages) <= MAX_MESSAGES):
        raise OpenClawError(400, "INVALID_REQUEST", "messages 数量无效", param="messages")
    messages: list[ExternalMessage] = []
    provided: list[str] = []
    required: list[str] = []
    total = 0
    for index, item in enumerate(raw_messages):
        if isinstance(item, str):
            item = {"role": "user", "content": item}
        if not isinstance(item, dict):
            raise OpenClawError(400, "INVALID_REQUEST", f"messages[{index}] 格式无效", param="messages")
        role = item.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise OpenClawError(400, "INVALID_REQUEST", f"messages[{index}].role 无效", param="messages")
        tool_calls = _parse_tool_calls(item.get("tool_calls"), f"messages[{index}].tool_calls")
        text = _parse_content(item.get("content"), f"messages[{index}].content",
                              allow_none=(role == "assistant" and bool(tool_calls)))
        tool_call_id = item.get("tool_call_id")
        if role == "tool":
            if not isinstance(tool_call_id, str) or not (1 <= len(tool_call_id) <= 128):
                raise OpenClawError(400, "INVALID_REQUEST", f"messages[{index}].tool_call_id 缺失", param="messages")
            if tool_calls:
                raise OpenClawError(400, "INVALID_REQUEST", f"messages[{index}] 工具结果不能包含 tool_calls", param="messages")
            required.append(tool_call_id)
        elif tool_call_id is not None:
            raise OpenClawError(400, "INVALID_REQUEST", f"messages[{index}] 非 tool 消息不能带 tool_call_id", param="messages")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise OpenClawError(400, "INVALID_REQUEST", f"messages[{index}].name 无效", param="messages")
        provided.extend(call.id for call in tool_calls)
        total += len(text or "") + sum(len(call.arguments_json) for call in tool_calls)
        if total > MAX_TOTAL_TEXT_CHARS:
            raise OpenClawError(413, "REQUEST_TOO_LARGE", "消息内容超过累计长度限制", param="messages")
        messages.append(ExternalMessage(role, text, tool_calls, tool_call_id, name))
    return tuple(messages), tuple(provided), tuple(required)


def _validate_responses_input(payload: dict[str, Any]) -> tuple[tuple[ExternalMessage, ...], tuple[str, ...], tuple[str, ...]]:
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        raw_input = [raw_input]
    if not isinstance(raw_input, list) or not (1 <= len(raw_input) <= MAX_MESSAGES):
        raise OpenClawError(400, "INVALID_REQUEST", "input 数量无效", param="input")
    messages: list[ExternalMessage] = []
    provided: list[str] = []
    required: list[str] = []
    total = 0
    for index, item in enumerate(raw_input):
        if isinstance(item, str):
            item = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": item}]}
        if not isinstance(item, dict):
            raise OpenClawError(400, "INVALID_REQUEST", f"input[{index}] 格式无效", param="input")
        kind = item.get("type")
        if kind == "function_call":
            calls = _parse_tool_calls(
                [{**item, "type": "function", "id": item.get("call_id", item.get("id"))}],
                f"input[{index}]",
            )
            provided.extend(call.id for call in calls)
            total += sum(len(call.arguments_json) for call in calls)
            messages.append(ExternalMessage("assistant", None, calls))
        elif kind == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not (1 <= len(call_id) <= 128):
                raise OpenClawError(400, "INVALID_REQUEST", f"input[{index}].call_id 缺失", param="input")
            output = item.get("output")
            if isinstance(output, list):
                output = _parse_content(output, f"input[{index}].output")
            elif not isinstance(output, str):
                raise OpenClawError(400, "INVALID_REQUEST", f"input[{index}].output 无效", param="input")
            output = _bounded_text(output, f"input[{index}].output")
            required.append(call_id)
            total += len(output)
            messages.append(ExternalMessage("tool", output, tool_call_id=call_id))
        elif kind in (None, "message"):
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise OpenClawError(400, "INVALID_REQUEST", f"input[{index}].role 无效", param="input")
            text = _parse_content(item.get("content"), f"input[{index}].content", allow_none=False)
            total += len(text or "")
            messages.append(ExternalMessage(role, text))
        else:
            raise OpenClawError(400, "UNSUPPORTED_CONTENT", f"不支持的 input 类型：{kind}", param="input")
        if total > MAX_TOTAL_TEXT_CHARS:
            raise OpenClawError(413, "REQUEST_TOO_LARGE", "输入内容超过累计长度限制", param="input")
    return tuple(messages), tuple(provided), tuple(required)


def validate_request(protocol: Literal["chat.completions", "responses"], payload: Any,
                     principal: TenantPrincipal, headers: Mapping[str, Any] | None) -> ValidatedRequest:
    if not isinstance(payload, dict):
        raise OpenClawError(400, "INVALID_REQUEST", "请求体必须是 JSON 对象")
    _reject_unknown_fields(payload, _CHAT_FIELDS if protocol == "chat.completions" else _RESPONSES_FIELDS)
    model = payload.get("model")
    preliminary_agent = _agent_from_model(model)
    override = _header(headers, "x-openclaw-model")
    effective_model = model
    if override is not None:
        if override not in principal.allowed_models:
            raise OpenClawError(404, "MODEL_NOT_ALLOWED", "x-openclaw-model 指定的模型未被租户授权")
        if preliminary_agent != "default" and override != model:
            raise OpenClawError(400, "MODEL_OVERRIDE_CONFLICT", "不支持用 x-openclaw-model 覆盖已指定助手的模型")
        effective_model = override
    agent_from_model = _agent_from_model(effective_model)
    header_agent = _header(headers, "x-openclaw-agent-id")
    if header_agent is not None:
        header_agent = _valid_alias(header_agent, "x-openclaw-agent-id")
        if header_agent not in principal.allowed_agents:
            raise OpenClawError(404, "AGENT_NOT_ALLOWED", "请求的助手未被租户令牌授权")
        if agent_from_model != "default" and agent_from_model != header_agent:
            raise OpenClawError(400, "AGENT_CONFLICT", "模型别名与 x-openclaw-agent-id 指向不同助手")
        agent = header_agent
    else:
        agent = agent_from_model
    if agent not in principal.allowed_agents:
        raise OpenClawError(404, "AGENT_NOT_ALLOWED", "请求的助手未被租户令牌授权")
    if protocol == "chat.completions":
        messages, provided, required = _validate_chat_messages(payload)
        previous_response_id = None
        instructions = None
    else:
        messages, provided, required = _validate_responses_input(payload)
        raw_instructions = payload.get("instructions")
        instructions = None
        if raw_instructions is not None:
            instructions = _bounded_text(raw_instructions, "instructions")
            messages = (ExternalMessage("system", instructions),) + messages
        previous_response_id = payload.get("previous_response_id")
        if previous_response_id is not None:
            if not isinstance(previous_response_id, str) or not (1 <= len(previous_response_id) <= 200):
                raise OpenClawError(400, "INVALID_REQUEST", "previous_response_id 无效", param="previous_response_id")
    tools = _parse_tools(payload.get("tools"))
    tool_choice = _parse_tool_choice(payload.get("tool_choice"), tools)
    raw_max = payload.get("max_tokens", payload.get("max_completion_tokens", payload.get("max_output_tokens")))
    if raw_max is not None and (type(raw_max) is not int or not 1 <= raw_max <= MAX_OUTPUT_TOKENS):
        raise OpenClawError(400, "INVALID_REQUEST", "max_tokens 超出限制", param="max_tokens")
    temperature = payload.get("temperature")
    if temperature is not None and (type(temperature) not in (int, float) or not 0 <= temperature <= 2):
        raise OpenClawError(400, "INVALID_REQUEST", "temperature 超出限制", param="temperature")
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise OpenClawError(400, "INVALID_REQUEST", "stream 必须是布尔值", param="stream")
    stream_options = payload.get("stream_options")
    if stream_options is not None:
        if not isinstance(stream_options, dict) or set(stream_options) - {"include_usage"} or not isinstance(stream_options.get("include_usage", False), bool):
            raise OpenClawError(400, "INVALID_REQUEST", "stream_options 仅支持 include_usage", param="stream_options")
    metadata, metadata_digest = _parse_metadata(payload.get("metadata"))
    store = payload.get("store", False)
    if not isinstance(store, bool):
        raise OpenClawError(400, "INVALID_REQUEST", "store 必须是布尔值", param="store")
    parallel = payload.get("parallel_tool_calls")
    if parallel is not None and not isinstance(parallel, bool):
        raise OpenClawError(400, "INVALID_REQUEST", "parallel_tool_calls 必须是布尔值", param="parallel_tool_calls")
    raw_user = payload.get("user")
    user_alias = _valid_user_alias(raw_user) if raw_user is not None else None
    channel = _header(headers, "x-openclaw-message-channel")
    if channel is not None and len(channel) > 100:
        raise OpenClawError(400, "INVALID_REQUEST", "x-openclaw-message-channel 过长")
    return ValidatedRequest(
        protocol=protocol,
        model_alias=effective_model,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
        max_output_tokens=raw_max,
        temperature=float(temperature) if temperature is not None else None,
        stream=stream,
        instructions=instructions,
        metadata=metadata,
        metadata_digest=metadata_digest,
        store=store,
        previous_response_id=previous_response_id,
        user_alias=user_alias,
        message_channel=channel,
        required_tool_call_ids=required,
        provided_tool_call_ids=provided,
    )


def _canonical_request(request: ValidatedRequest, *, agent_id: str, session_id: str) -> str:
    document = {
        "protocol": request.protocol,
        "model": request.model_alias,
        "agent": agent_id,
        "session": session_id,
        "messages": [
            {"role": message.role, "text": message.text, "tool_call_id": message.tool_call_id, "name": message.name,
             "tool_calls": [{"id": call.id, "name": call.name, "arguments": call.arguments_json}
                            for call in message.tool_calls]}
            for message in request.messages
        ],
        "tools": [{"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                  for tool in request.tools],
        "tool_choice": request.tool_choice,
        "max_output_tokens": request.max_output_tokens,
        "temperature": request.temperature,
        "stream": request.stream,
        "instructions": request.instructions,
        "metadata": request.metadata_digest,
        "store": request.store,
        "previous_response_id": request.previous_response_id,
    }
    return _sha256(json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def _idempotency_key(headers: Mapping[str, Any] | None) -> str | None:
    first = _header(headers, "idempotency-key")
    second = _header(headers, "x-openclaw-idempotency-key")
    if first is not None and second is not None and first != second:
        raise OpenClawError(400, "INVALID_IDEMPOTENCY_KEY", "两个幂等键不一致")
    value = first if first is not None else second
    return _valid_idempotency_key(value) if value is not None else None


def _chat_usage(usage: NormalizedUsage) -> dict[str, int]:
    values = usage.public()
    if {"input_tokens", "output_tokens", "total_tokens"} <= values.keys():
        return {"prompt_tokens": values["input_tokens"], "completion_tokens": values["output_tokens"],
                "total_tokens": values["total_tokens"]}
    return {}


def _responses_usage(usage: NormalizedUsage) -> dict[str, int]:
    values = usage.public()
    output: dict[str, int] = {}
    for source, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                           ("total_tokens", "total_tokens"), ("cache_read_input_tokens", "input_cached_tokens")):
        if source in values:
            output[target] = values[source]
    return output


def _coerce_result(value: Any) -> NormalizedResult:
    if isinstance(value, NormalizedResult):
        return value
    if isinstance(value, dict):
        try:
            return NormalizedResult(
                text=value.get("text", "") if isinstance(value.get("text", ""), str) else "",
                tool_calls=tuple(value.get("tool_calls", ()) or ()),
                finish_reason=value.get("finish_reason", "stop"),
                usage=value.get("usage") if isinstance(value.get("usage"), NormalizedUsage) else NormalizedUsage(),
                chunks=tuple(value.get("chunks", ()) or ()),
            )
        except (TypeError, ValueError):
            pass
    raise OpenClawError(502, "MODEL_EXECUTION_FAILED", "模型执行器返回了无效结果")


def _chunk_text(result: NormalizedResult) -> tuple[str, ...]:
    if result.chunks:
        return tuple(result.chunks)
    text = result.text or ""
    return tuple(text[index:index + 256] for index in range(0, len(text), 256)) if text else ()


def _finish_reason(result: NormalizedResult) -> str:
    if result.finish_reason in {"stop", "length", "tool_calls", "content_filter"}:
        return result.finish_reason
    return "tool_calls" if result.tool_calls else "stop"


def _chat_completion_body(response_id: str, request: ValidatedRequest, result: NormalizedResult) -> dict[str, Any]:
    if result.text:
        content: str | None = result.text
    else:
        content = None if result.tool_calls else ""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if result.tool_calls:
        message["tool_calls"] = [
            {"id": call.id, "type": "function",
             "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))}}
            for call in result.tool_calls
        ]
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model_alias,
        "choices": [{"index": 0, "message": message, "finish_reason": _finish_reason(result)}],
        "usage": _chat_usage(result.usage),
        "system_fingerprint": "openclaw-compat",
    }


def _chat_stream_events(response_id: str, request: ValidatedRequest, result: NormalizedResult) -> tuple[dict[str, Any], ...]:
    created = int(time.time())
    events: list[dict[str, Any]] = []
    chunks = _chunk_text(result)
    for index, chunk in enumerate(chunks):
        delta: dict[str, Any] = {"content": chunk}
        if index == 0:
            delta["role"] = "assistant"
        events.append({"id": response_id, "object": "chat.completion.chunk", "created": created,
                       "model": request.model_alias,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
    if not chunks:
        events.append({"id": response_id, "object": "chat.completion.chunk", "created": created,
                       "model": request.model_alias,
                       "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})
    for index, call in enumerate(result.tool_calls):
        events.append({"id": response_id, "object": "chat.completion.chunk", "created": created,
                       "model": request.model_alias,
                       "choices": [{"index": 0, "delta": {"tool_calls": [{
                           "index": index, "id": call.id, "type": "function",
                           "function": {"name": call.name,
                                        "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))},
                       }]}, "finish_reason": None}]})
    events.append({"id": response_id, "object": "chat.completion.chunk", "created": created,
                   "model": request.model_alias,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": _finish_reason(result)}],
                   "usage": _chat_usage(result.usage)})
    return tuple(events)


def _responses_body(response_id: str, request: ValidatedRequest, result: NormalizedResult) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    if result.text:
        output.append({"id": f"msg_{response_id[5:]}", "type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": result.text, "annotations": []}]})
    for call in result.tool_calls:
        output.append({"id": call.id, "type": "function_call", "call_id": call.id, "name": call.name,
                       "arguments": json.dumps(call.arguments, ensure_ascii=False, separators=(",", ":")),
                       "status": "completed"})
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": request.model_alias,
        "output": output,
        "output_text": result.text,
        "usage": _responses_usage(result.usage),
        "previous_response_id": request.previous_response_id,
        "metadata": dict(request.metadata),
        "incomplete_details": None if _finish_reason(result) == "stop" else {"reason": _finish_reason(result)},
        "error": None,
        "parallel_tool_calls": False,
    }


def _responses_stream_events(response_id: str, request: ValidatedRequest, result: NormalizedResult) -> tuple[dict[str, Any], ...]:
    body = _responses_body(response_id, request, result)
    events: list[dict[str, Any]] = [{
        "type": "response.created",
        "response": {"id": response_id, "object": "response", "model": request.model_alias, "status": "in_progress"},
    }]
    message_id = f"msg_{response_id[5:]}"
    if result.text:
        events.append({"type": "response.output_item.added", "output_index": 0, "item": {
            "id": message_id, "type": "message", "role": "assistant", "status": "in_progress", "content": [],
        }})
    for chunk in _chunk_text(result):
        events.append({"type": "response.output_text.delta", "item_id": message_id,
                       "output_index": 0, "content_index": 0, "delta": chunk})
    if result.text:
        events.append({"type": "response.output_text.done", "item_id": message_id,
                       "output_index": 0, "content_index": 0, "text": result.text})
    for index, item in enumerate(body["output"]):
        if item["type"] == "function_call":
            events.append({"type": "response.output_item.added", "output_index": index, "item": item})
            events.append({"type": "response.function_call_arguments.done", "item_id": item["id"],
                           "output_index": index, "arguments": item["arguments"]})
    events.append({"type": "response.completed", "response": body})
    return tuple(events)


class OpenClawFacade:
    """Protocol facade, identity mapping, idempotency and audit boundary."""

    def __init__(self, service, executor: OpenClawExecutor | None = None, *,
                 executor_timeout_seconds: float = DEFAULT_EXECUTOR_TIMEOUT_SECONDS,
                 lease_seconds: float = DEFAULT_LEASE_SECONDS):
        if not isinstance(executor_timeout_seconds, (int, float)) or not 0 < executor_timeout_seconds <= 600:
            raise ValueError("executor_timeout_seconds must be between 0 and 600")
        if not isinstance(lease_seconds, (int, float)) or not 0 < lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 0 and 3600")
        self.service = service
        self.store = OpenClawTenantStore(service.db, session_cookie_name=service.settings.session_cookie_name)
        self.executor = executor or UnavailableExecutor()
        self.executor_timeout_seconds = float(executor_timeout_seconds)
        self.lease_seconds = float(lease_seconds)
        self.instance_id = f"pid:{os.getpid()}:{uid()}"
        self._active: dict[str, ActiveRun] = {}
        self._lock = threading.Lock()
        self._session_lock = threading.Lock()
        self.recover_interrupted()

    # ------------------------------------------------------------------ audit
    def _audit(self, principal: TenantPrincipal | None, action: str, outcome: str, *,
               session_id: str | None = None, run_id: str | None = None,
               detail: Mapping[str, Any] | None = None) -> None:
        self.store.audit(
            tenant_id=principal.tenant_id if principal else None,
            principal_id=principal.principal_id if principal else None,
            session_id=session_id, run_id=run_id, action=action, outcome=outcome, detail=detail,
        )

    def recover_interrupted(self) -> int:
        """Recover only turns whose owner stopped renewing its lease.

        Constructing a second facade must not mark another live instance's
        turns failed.  Each running turn records ``owner_instance`` and a
        heartbeat-renewed ``lease_expires_at``; only rows whose lease is
        missing (pre-repair data) or expired are recovered.
        """
        now_iso = _now_iso()
        rows = self.service.db.all(
            "SELECT * FROM openclaw_turns WHERE status='running' "
            "AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
            (now_iso,),
        )
        recovered = 0
        for row in rows:
            changed = self.service.db.execute(
                "UPDATE openclaw_turns SET status='failed',http_status=503,error_json=?,"
                "lease_expires_at=NULL,completed_at=? WHERE id=? AND status='running' "
                "AND (lease_expires_at IS NULL OR lease_expires_at<=?)",
                (dump({"code": "INTERRUPTED", "message": "兼容侧车实例租约过期，未完成的执行已终止"}),
                 _now_iso(), row["id"], now_iso),
            ).rowcount
            if not changed:
                continue
            recovered += 1
            self._audit(None, "turn.interrupted", "failed", session_id=row["session_id"], run_id=row["run_id"],
                        detail={"turn_id": row["id"], "owner_instance": row.get("owner_instance"),
                                "lease_expired": True})
        return recovered

    def audit_events(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        if tenant_id is None:
            rows = self.service.db.all("SELECT * FROM openclaw_audit ORDER BY id")
        else:
            rows = self.service.db.all("SELECT * FROM openclaw_audit WHERE tenant_id=? ORDER BY id", (tenant_id,))
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    # ----------------------------------------------------------------- models
    @staticmethod
    def list_models(principal: TenantPrincipal) -> list[dict[str, Any]]:
        models = []
        for agent in principal.allowed_agents:
            alias = _model_alias_for(agent)
            models.append({
                "id": alias, "object": "model", "created": 0, "owned_by": "zhixing-workbench",
                "agent_id": agent, "permission": [],
            })
        return models

    @staticmethod
    def get_model(principal: TenantPrincipal, model_id: str) -> dict[str, Any]:
        requested = "openclaw/default" if model_id == "openclaw" else model_id
        for item in OpenClawFacade.list_models(principal):
            if item["id"] == requested:
                return item
        raise OpenClawError(404, "MODEL_NOT_ALLOWED", "请求的模型未被租户授权")

    # --------------------------------------------------------------- sessions
    def _session_row_by_id(self, principal: TenantPrincipal, session_id: str, agent_id: str) -> dict[str, Any]:
        row = self.service.db.one("SELECT * FROM openclaw_sessions WHERE id=?", (session_id,))
        if (not row or row["tenant_id"] != principal.tenant_id or row["principal_id"] != principal.principal_id
                or row["agent_id"] != agent_id):
            raise OpenClawError(404, "SESSION_NOT_FOUND", "会话不存在或无权继续")
        return row

    def _binding(self, row: Mapping[str, Any]) -> SessionBinding:
        return SessionBinding(row["id"], row["tenant_id"], row["principal_id"], row["agent_id"],
                              row["assistant_id"], row["session_key"], row["conversation_id"],
                              row.get("user_digest"))

    def _response_binding(self, principal: TenantPrincipal, previous_response_id: str,
                          agent_id: str) -> dict[str, Any]:
        row = self.service.db.one(
            """SELECT responses.*, sessions.session_key, sessions.conversation_id, sessions.assistant_id,
                      sessions.user_digest
               FROM openclaw_responses responses
               JOIN openclaw_sessions sessions ON sessions.id=responses.session_id
               WHERE responses.response_id=?""",
            (previous_response_id,),
        )
        if (not row or row["tenant_id"] != principal.tenant_id
                or row["principal_id"] != principal.principal_id or row["agent_id"] != agent_id):
            raise OpenClawError(404, "RESPONSE_NOT_FOUND", "previous_response_id 不存在、已过期或无权继续")
        try:
            created = datetime.fromisoformat(row["created_at"])
        except (TypeError, ValueError):
            raise OpenClawError(404, "RESPONSE_NOT_FOUND", "previous_response_id 不存在、已过期或无权继续") from None
        age = (datetime.now(timezone.utc) - created).total_seconds()
        if age > RESPONSE_TTL_SECONDS or age < -60:
            raise OpenClawError(404, "RESPONSE_NOT_FOUND", "previous_response_id 已过期，请显式新建会话")
        row["id"] = row["session_id"]
        return row

    def _enforce_user_binding(self, row: Mapping[str, Any], user_alias: str | None) -> None:
        """Reject continuing a session under a different ``user`` alias.

        Explicit session keys are tenant-principal scoped.  On first use with a
        ``user`` the binding is stamped with that alias digest (so an explicit
        key cannot later be used to reach that end-user's session from another
        alias), and every later request must present the same alias.
        """
        if user_alias is None:
            return
        digest = _sha256(user_alias)
        stored = row.get("user_digest")
        if stored is None:
            changed = self.service.db.execute(
                "UPDATE openclaw_sessions SET user_digest=?,updated_at=? "
                "WHERE id=? AND user_digest IS NULL",
                (digest, _now_iso(), row["id"]),
            ).rowcount
            if changed:
                return
            refreshed = self.service.db.one(
                "SELECT user_digest FROM openclaw_sessions WHERE id=?", (row["id"],)
            )
            if not refreshed:
                raise OpenClawError(404, "SESSION_NOT_FOUND", "会话不存在或无权继续")
            stored = refreshed["user_digest"]
        if stored != digest:
            raise OpenClawError(409, "SESSION_CONFLICT", "会话已绑定其他 user，不能跨用户继续")

    def _get_or_create_session(self, principal: TenantPrincipal, agent_id: str, assistant_id: str,
                               session_key: str, user_alias: str | None) -> SessionBinding:
        with self._session_lock:
            row = self.service.db.one(
                "SELECT * FROM openclaw_sessions WHERE tenant_id=? AND principal_id=? AND agent_id=? AND session_key=?",
                (principal.tenant_id, principal.principal_id, agent_id, session_key),
            )
            if row:
                self._enforce_user_binding(row, user_alias)
                return self._binding(row)
            user = self.service.db.one("SELECT * FROM users WHERE id=? AND enabled=1", (principal.user_id,))
            if not user:
                raise OpenClawError(401, "TENANT_DISABLED", "租户服务主体已停用")
            title = f"OpenClaw · {agent_id} · {_sha256(session_key)[:8]}"[:120]
            try:
                conversation = self.service.create_conversation(
                    {"assistant_id": assistant_id, "title": title, "auto_model_routing": False}, user
                )
            except OpenClawError:
                raise
            except Exception as error:
                status = getattr(error, "status", None)
                if status in (403, 404):
                    raise OpenClawError(404, "AGENT_NOT_ALLOWED", "租户绑定的助手不存在或无权使用") from None
                raise
            user_digest = _sha256(user_alias) if user_alias else None
            session_id = uid()
            try:
                self.service.db.execute(
                    "INSERT INTO openclaw_sessions(id,tenant_id,principal_id,agent_id,session_key,user_digest,assistant_id,conversation_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (session_id, principal.tenant_id, principal.principal_id, agent_id, session_key,
                     user_digest, assistant_id, conversation["id"], _now_iso(), _now_iso()),
                )
            except sqlite3.IntegrityError:
                # Another sidecar process won the same binding race.
                row = self.service.db.one(
                    "SELECT * FROM openclaw_sessions WHERE tenant_id=? AND principal_id=? AND agent_id=? AND session_key=?",
                    (principal.tenant_id, principal.principal_id, agent_id, session_key),
                )
                if row:
                    self._enforce_user_binding(row, user_alias)
                    return self._binding(row)
                raise
            self._audit(principal, "session.created", "allowed", session_id=session_id,
                        detail={"agent": agent_id, "conversation_id": conversation["id"],
                                "session_key_digest": _sha256(session_key)})
            return SessionBinding(session_id, principal.tenant_id, principal.principal_id, agent_id,
                                  assistant_id, session_key, conversation["id"], user_digest)

    def _resolve_session(self, principal: TenantPrincipal, agent_id: str, assistant_id: str,
                         request: ValidatedRequest, headers: Mapping[str, Any] | None) -> SessionBinding:
        session_id_header = _header(headers, "x-openclaw-session-id")
        session_key_header = _header(headers, "x-openclaw-session-key")
        if request.previous_response_id:
            row = self._response_binding(principal, request.previous_response_id, agent_id)
            if session_id_header is not None and session_id_header != row["session_id"]:
                raise OpenClawError(400, "SESSION_CONFLICT", "previous_response_id 与显式会话不一致")
            if session_key_header is not None and _valid_session_key(session_key_header) != row["session_key"]:
                raise OpenClawError(400, "SESSION_CONFLICT", "previous_response_id 与显式会话键不一致")
            self._enforce_user_binding(row, request.user_alias)
            return self._binding(row)
        if session_id_header is not None:
            row = self._session_row_by_id(principal, session_id_header, agent_id)
            if session_key_header is not None and _valid_session_key(session_key_header) != row["session_key"]:
                raise OpenClawError(400, "SESSION_CONFLICT", "会话标识与会话键不一致")
            self._enforce_user_binding(row, request.user_alias)
            return self._binding(row)
        if session_key_header is not None:
            return self._get_or_create_session(principal, agent_id, assistant_id,
                                               _valid_session_key(session_key_header), request.user_alias)
        if request.user_alias is not None:
            derived = "user:" + _sha256(request.user_alias)[:40]
            return self._get_or_create_session(principal, agent_id, assistant_id, derived, request.user_alias)
        raise OpenClawError(400, "SESSION_KEY_REQUIRED",
                            "请提供 user、x-openclaw-session-key、x-openclaw-session-id 或 previous_response_id")

    def _tool_references_ok(self, session_id: str, required: tuple[str, ...]) -> None:
        for call_id in required:
            # Only ids this facade previously issued for this session count.
            # A caller cannot manufacture an assistant tool_call in the same
            # request and use it to smuggle an unissued result through.
            row = self.service.db.one("SELECT call_id FROM openclaw_tool_calls WHERE session_id=? AND call_id=?",
                                      (session_id, call_id))
            if not row:
                raise OpenClawError(400, "TOOL_CALL_UNKNOWN", "工具结果引用了未声明的调用编号", param="tool_call_id")

    def _remember_tool_calls(self, session_id: str, calls) -> None:
        for call in calls:
            call_id = getattr(call, "id", None)
            name = getattr(call, "name", None)
            if isinstance(call_id, str) and isinstance(name, str):
                self.service.db.execute(
                    "INSERT OR IGNORE INTO openclaw_tool_calls(session_id,call_id,name,created_at) VALUES(?,?,?,?)",
                    (session_id, call_id, name, _now_iso()),
                )

    # ---------------------------------------------------------------- execute
    async def execute(self, principal: TenantPrincipal, protocol: Literal["chat.completions", "responses"],
                      payload: Any, headers: Mapping[str, Any] | None) -> TurnOutcome:
        request = validate_request(protocol, payload, principal, headers)
        agent_id = _agent_from_model(request.model_alias)
        header_agent = _header(headers, "x-openclaw-agent-id")
        if header_agent is not None:
            agent_id = _valid_alias(header_agent, "x-openclaw-agent-id")
        assistant_id = principal.assistant_aliases[agent_id]
        session = self._resolve_session(principal, agent_id, assistant_id, request, headers)
        self._tool_references_ok(session.id, request.required_tool_call_ids)
        digest = _canonical_request(request, agent_id=agent_id, session_id=session.id)
        idem = _idempotency_key(headers)

        if idem is not None:
            existing = self.service.db.one(
                "SELECT * FROM openclaw_turns WHERE tenant_id=? AND principal_id=? AND idempotency_key=?",
                (principal.tenant_id, principal.principal_id, idem),
            )
            if existing:
                return self._replay(principal, existing, digest, session.id)

        turn_id, run_id = uid(), uid()
        try:
            self.service.db.execute(
                "INSERT INTO openclaw_turns(id,tenant_id,principal_id,session_id,conversation_id,run_id,protocol,model_alias,idempotency_key,request_digest,status,owner_instance,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (turn_id, principal.tenant_id, principal.principal_id, session.id, session.conversation_id, run_id,
                 protocol, request.model_alias, idem, digest, "running", self.instance_id,
                 _lease_deadline(self.lease_seconds), _now_iso()),
            )
        except Exception as error:
            # Concurrent identical retries lose the unique insert race.
            if idem is not None:
                existing = self.service.db.one(
                    "SELECT * FROM openclaw_turns WHERE tenant_id=? AND principal_id=? AND idempotency_key=?",
                    (principal.tenant_id, principal.principal_id, idem),
                )
                if existing:
                    return self._replay(principal, existing, digest, session.id)
            raise error
        active = ActiveRun(run_id, turn_id, principal.tenant_id, principal.principal_id, session.id)
        with self._lock:
            self._active[run_id] = active
        self._audit(principal, "turn.started", "allowed", session_id=session.id, run_id=run_id,
                    detail={"turn_id": turn_id, "protocol": protocol, "model": request.model_alias,
                            "agent": agent_id, "idempotency": idem is not None,
                            "messages": len(request.messages), "tools": len(request.tools)})
        execution = ExecutionRequest(
            protocol=protocol, model_alias=request.model_alias, agent_id=agent_id,
            messages=request.messages, tools=request.tools, tool_choice=request.tool_choice,
            max_output_tokens=request.max_output_tokens, temperature=request.temperature,
            stream=request.stream, instructions=request.instructions,
            metadata_digest=request.metadata_digest, request_id=turn_id,
            message_channel=request.message_channel,
        )
        context = ExecutionContext(principal.tenant_id, principal.principal_id, session.id,
                                   session.conversation_id, agent_id, request.model_alias, run_id, turn_id)
        heartbeat = asyncio.create_task(self._heartbeat_turn(turn_id, active))
        try:
            result = await self._run_executor(execution, context, active)
            result = _coerce_result(result)
            response_id = (f"chatcmpl_{secrets.token_hex(12)}" if protocol == "chat.completions"
                           else f"resp_{secrets.token_hex(12)}")
            if protocol == "chat.completions":
                body = _chat_completion_body(response_id, request, result)
                events = _chat_stream_events(response_id, request, result)
            else:
                body = _responses_body(response_id, request, result)
                events = _responses_stream_events(response_id, request, result)
            # Publish session references only with the successful ownership CAS.
            with self.service.db.transaction():
                self._complete_turn(turn_id, body, events, request.stream)
                self._remember_tool_calls(session.id, result.tool_calls)
            self._audit(principal, "turn.completed", "allowed", session_id=session.id, run_id=run_id,
                        detail={"turn_id": turn_id, "finish_reason": _finish_reason(result),
                                "tool_calls": len(result.tool_calls), "usage": result.usage.public()})
            return TurnOutcome(request.stream, body, events, response_id, run_id, session.id)
        except asyncio.CancelledError:
            recorded = self._fail_turn(turn_id, "cancelled", 499, "REQUEST_CANCELLED", "客户端取消或断开连接")
            self._audit(principal, "turn.cancelled" if recorded else "turn.result_discarded",
                        "cancelled" if recorded else "denied", session_id=session.id, run_id=run_id,
                        detail={"turn_id": turn_id, "reason": "client_disconnect"})
            raise
        except OpenClawError as error:
            status = "cancelled" if error.code == "REQUEST_CANCELLED" else "failed"
            recorded = self._fail_turn(turn_id, status, error.status, error.code, error.message)
            action = "turn.cancelled" if status == "cancelled" else "turn.failed"
            if not recorded:
                error = OpenClawError(503, "INTERRUPTED", "请求租约已失效，迟到结果已丢弃")
                action, status = "turn.result_discarded", "denied"
            self._audit(principal, action, status,
                        session_id=session.id, run_id=run_id,
                        detail={"turn_id": turn_id, "code": error.code, "status": error.status})
            raise error
        except Exception as error:
            mapped = _error_for_status(getattr(error, "status_code", 0) if type(getattr(error, "status_code", None)) is int else 502)
            recorded = self._fail_turn(turn_id, "failed", mapped.status, mapped.code, mapped.message)
            if not recorded:
                mapped = OpenClawError(503, "INTERRUPTED", "请求租约已失效，迟到结果已丢弃")
            self._audit(principal, "turn.failed" if recorded else "turn.result_discarded",
                        "failed" if recorded else "denied", session_id=session.id, run_id=run_id,
                        detail={"turn_id": turn_id, "code": mapped.code})
            raise mapped from None
        finally:
            heartbeat.cancel()
            await _discard(heartbeat)
            with self._lock:
                self._active.pop(run_id, None)

    async def _heartbeat_turn(self, turn_id: str, active: ActiveRun) -> None:
        """Renew only this owner's live lease; stop execution after ownership loss."""
        interval = max(self.lease_seconds / 3.0, 0.01)
        while True:
            await asyncio.sleep(interval)
            if not self._renew_turn_lease(turn_id):
                active.lease_lost = True
                active.cancelled.set()
                return

    def _renew_turn_lease(self, turn_id: str) -> bool:
        with self.service.db.transaction():
            return self.service.db.execute(
                "UPDATE openclaw_turns SET lease_expires_at=? "
                "WHERE id=? AND owner_instance=? AND status='running' AND lease_expires_at>?",
                (_lease_deadline(self.lease_seconds), turn_id, self.instance_id, _now_iso()),
            ).rowcount == 1

    async def _run_executor(self, request: ExecutionRequest, context: ExecutionContext,
                            active: ActiveRun) -> Any:
        task = asyncio.create_task(self.executor.execute(request, context, active.cancelled))
        cancel_wait = asyncio.create_task(active.cancelled.wait())
        try:
            done, _ = await asyncio.wait({task, cancel_wait}, timeout=self.executor_timeout_seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                task.cancel()
                await _discard(task)
                raise OpenClawError(504, "MODEL_TIMEOUT", "模型执行超时，请稍后重试")
            if cancel_wait in done:
                task.cancel()
                await _discard(task)
                if active.lease_lost:
                    raise OpenClawError(503, "INTERRUPTED", "请求租约已失效，执行已中止")
                raise OpenClawError(499, "REQUEST_CANCELLED", "请求已取消")
            cancel_wait.cancel()
            await _discard(cancel_wait)
            error = task.exception()
            if error is not None:
                raise error
            return task.result()
        finally:
            if not cancel_wait.done():
                cancel_wait.cancel()
                await _discard(cancel_wait)

    def _replay(self, principal: TenantPrincipal, row: Mapping[str, Any], digest: str,
                session_id: str) -> TurnOutcome:
        if row["session_id"] != session_id or row["request_digest"] != digest:
            raise OpenClawError(409, "IDEMPOTENCY_CONFLICT", "相同幂等键已用于不同的请求内容")
        if row["status"] == "running":
            raise OpenClawError(409, "REQUEST_IN_PROGRESS", "相同幂等请求仍在执行，请稍后重试")
        self._audit(principal, "turn.replayed", "allowed", session_id=row["session_id"], run_id=row["run_id"],
                    detail={"turn_id": row["id"], "status": row["status"]})
        if row["status"] == "succeeded" and row["response_json"]:
            body = json.loads(row["response_json"])
            events = tuple(json.loads(row["sse_json"] or "[]"))
            return TurnOutcome(bool(row["stream"]), body, events, body.get("id", ""), row["run_id"],
                               row["session_id"])
        error = json.loads(row["error_json"] or "{}")
        raise OpenClawError(int(row["http_status"] or 502), error.get("code", "MODEL_EXECUTION_FAILED"),
                            error.get("message", "请求未完成，请重试"))

    def _complete_turn(self, turn_id: str, body: dict[str, Any], events: tuple[dict[str, Any], ...],
                       stream: bool) -> None:
        finished_at = _now_iso()
        changed = self.service.db.execute(
            "UPDATE openclaw_turns SET status='succeeded',http_status=200,response_json=?,sse_json=?,stream=?,lease_expires_at=NULL,completed_at=? "
            "WHERE id=? AND owner_instance=? AND status='running' AND lease_expires_at>?",
            (dump(body), dump(list(events)), 1 if stream else 0, finished_at,
             turn_id, self.instance_id, finished_at),
        ).rowcount
        if changed != 1:
            raise OpenClawError(503, "INTERRUPTED", "请求租约已失效，不能接受迟到结果")
        row = self.service.db.one(
            """SELECT turns.*, sessions.agent_id AS session_agent
               FROM openclaw_turns turns JOIN openclaw_sessions sessions ON sessions.id=turns.session_id
               WHERE turns.id=?""",
            (turn_id,),
        )
        if row and row["protocol"] == "responses":
            self.service.db.execute(
                "INSERT OR REPLACE INTO openclaw_responses(response_id,turn_id,session_id,tenant_id,principal_id,agent_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (body["id"], turn_id, row["session_id"], row["tenant_id"], row["principal_id"],
                 row["session_agent"], _now_iso()),
            )

    def _fail_turn(self, turn_id: str, status: str, http_status: int, code: str, message: str) -> bool:
        return self.service.db.execute(
            "UPDATE openclaw_turns SET status=?,http_status=?,error_json=?,lease_expires_at=NULL,completed_at=? "
            "WHERE id=? AND owner_instance=? AND status='running'",
            (status, http_status, dump({"code": code, "message": message}), _now_iso(),
             turn_id, self.instance_id),
        ).rowcount == 1

    # ----------------------------------------------------------- cancellation
    def cancel(self, principal: TenantPrincipal, run_id: str) -> dict[str, Any]:
        with self._lock:
            active = self._active.get(run_id)
        if active and active.tenant_id == principal.tenant_id and active.principal_id == principal.principal_id:
            active.cancelled.set()
            self._audit(principal, "run.cancel", "allowed", session_id=active.session_id, run_id=run_id,
                        detail={"turn_id": active.turn_id})
            return {"id": run_id, "object": "openclaw.run", "status": "cancelling"}
        row = self.service.db.one("SELECT * FROM openclaw_turns WHERE run_id=?", (run_id,))
        if (not row or row["tenant_id"] != principal.tenant_id
                or row["principal_id"] != principal.principal_id):
            raise OpenClawError(404, "RUN_NOT_FOUND", "运行不存在或无权操作")
        self._audit(principal, "run.cancel", "allowed", session_id=row["session_id"], run_id=run_id,
                    detail={"turn_id": row["id"], "status": row["status"], "terminal": True})
        return {"id": run_id, "object": "openclaw.run", "status": row["status"]}

    def cancel_response(self, principal: TenantPrincipal, response_id: str) -> dict[str, Any]:
        row = self.service.db.one(
            """SELECT turns.run_id FROM openclaw_responses responses
               JOIN openclaw_turns turns ON turns.id=responses.turn_id
               WHERE responses.response_id=? AND responses.tenant_id=? AND responses.principal_id=?""",
            (response_id, principal.tenant_id, principal.principal_id),
        )
        if not row:
            raise OpenClawError(404, "RESPONSE_NOT_FOUND", "响应不存在或无权操作")
        return self.cancel(principal, row["run_id"])


async def _discard(task: asyncio.Task) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def _sse_chat(outcome: TurnOutcome) -> AsyncIterator[str]:
    for event in outcome.events:
        yield "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
    yield "data: [DONE]\n\n"


def _sse_responses(outcome: TurnOutcome) -> AsyncIterator[str]:
    for event in outcome.events:
        yield f"event: {event.get('type', 'message')}\n"
        yield "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"


async def _read_json(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception:
        raise OpenClawError(400, "INVALID_JSON", "请求体必须是合法 JSON") from None
    if not isinstance(payload, dict):
        raise OpenClawError(400, "INVALID_REQUEST", "请求体必须是 JSON 对象")
    return payload


def _app_error(error: Any) -> OpenClawError:
    status = getattr(error, "status", 500)
    code = getattr(error, "code", "INTERNAL_ERROR")
    message = getattr(error, "message", "服务暂时不可用")
    if status == 403:
        return OpenClawError(403, "AGENT_NOT_ALLOWED", message)
    if status == 404:
        return OpenClawError(404, "AGENT_NOT_FOUND", message)
    if status == 409:
        return OpenClawError(409, "SESSION_BUSY", message)
    if status == 413:
        return OpenClawError(413, "REQUEST_TOO_LARGE", message)
    if 400 <= status < 500:
        return OpenClawError(status, code, message)
    return OpenClawError(503, "SERVICE_UNAVAILABLE", "服务暂时不可用")


def create_openclaw_sidecar_app(facade: OpenClawFacade) -> FastAPI:
    """Build the independent ``/v1`` sidecar.

    The employee application's ``create_app`` is intentionally not modified or
    wrapped.  Deployment mounts this ASGI app on a separate listener (or routes
    the ``/v1`` prefix to it), so a Cookie session cannot reach these routes.
    """
    app = FastAPI(
        title="知行 OpenClaw 兼容侧车",
        version=COMPAT_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.openclaw_facade = facade
    app.add_middleware(BodyLimitMiddleware, maximum=MAX_BODY_BYTES)

    @app.exception_handler(OpenClawError)
    async def openclaw_error(_request: Request, exc: OpenClawError):
        return JSONResponse(status_code=exc.status, content=exc.as_error())

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception):
        from .security import AppError

        if isinstance(exc, AppError):
            mapped = _app_error(exc)
            return JSONResponse(status_code=mapped.status, content=mapped.as_error())
        return JSONResponse(status_code=500, content=OpenClawError(500, "INTERNAL_ERROR", "服务暂时不可用").as_error())

    @app.middleware("http")
    async def safety_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not (response.headers.get("content-type") or "").startswith("text/event-stream"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def current(request: Request) -> TenantPrincipal:
        return facade.store.authenticate(request.headers)

    @app.get("/v1/models")
    async def models(request: Request):
        return {"object": "list", "data": facade.list_models(current(request))}

    @app.get("/v1/models/{model_id:path}")
    async def model_get(model_id: str, request: Request):
        return facade.get_model(current(request), model_id)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        principal = current(request)
        payload = await _read_json(request)
        outcome = await facade.execute(principal, "chat.completions", payload, request.headers)
        headers = outcome.headers()
        if outcome.stream:
            headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            return StreamingResponse(_sse_chat(outcome), media_type="text/event-stream", headers=headers)
        return JSONResponse(status_code=200, content=outcome.body, headers=headers)

    @app.post("/v1/responses")
    async def responses(request: Request):
        principal = current(request)
        payload = await _read_json(request)
        outcome = await facade.execute(principal, "responses", payload, request.headers)
        headers = outcome.headers()
        if outcome.stream:
            headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            return StreamingResponse(_sse_responses(outcome), media_type="text/event-stream", headers=headers)
        return JSONResponse(status_code=200, content=outcome.body, headers=headers)

    @app.post("/v1/runs/{run_id}/cancel")
    async def run_cancel(run_id: str, request: Request):
        return facade.cancel(current(request), run_id)

    @app.post("/v1/responses/{response_id}/cancel")
    async def response_cancel(response_id: str, request: Request):
        return facade.cancel_response(current(request), response_id)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        current(request)
        raise OpenClawError(501, "EMBEDDINGS_UNSUPPORTED", "本阶段兼容入口不提供 embeddings")

    return app


__all__ = [
    "AGENT_HARNESS_V2_CONTRACT",
    "AGENT_HARNESS_V2_CONTRACT_VERSION",
    "COMPAT_VERSION",
    "ExecutionContext",
    "ExecutionRequest",
    "ExternalMessage",
    "ExternalToolCall",
    "OpenClawError",
    "OpenClawExecutor",
    "OpenClawFacade",
    "OpenClawTenantStore",
    "SessionBinding",
    "TenantPrincipal",
    "TurnOutcome",
    "UnavailableExecutor",
    "create_openclaw_sidecar_app",
    "harness_contract_manifest",
    "validate_request",
]
