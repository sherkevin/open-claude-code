"""Synthetic OpenClaw MCP/Skill/plugin connector security boundary.

This is deliberately a *boundary*, not a production MCP client.  It accepts
standard MCP ``tools/list`` declarations, applies an explicit per-tenant tool
whitelist, classifies risk, and executes through an injected executor.  The
default executor is unavailable, and no network or customer MCP server is
contacted in this slice.

Rules implemented here:

* advertised tools are only those explicitly whitelisted for the tenant
  principal and not classified high risk;
* read-only low-risk tools may run without approval;
* recognized write tools require a synthetic approval;
* destructive, administrative, execution and unknown tools are denied by
  default, even if the caller whitelists them;
* calls are bounded by timeout, cancellable, revocable and audited without
  storing raw tool arguments.

Approval here is a synthetic control-plane decision for tests and integration
wiring; a product approval UI and the real MCP transport are explicitly still
open work.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

from .db import dump, now, uid
from .openclaw_compat import MAX_TOOL_CHARS, OpenClawError

MAX_TOOLS_PER_SERVER = 64
MAX_SERVERS = 64
MAX_ARGUMENT_DEPTH = 6
MAX_OUTPUT_CHARS = 20_000
DEFAULT_TOOL_TIMEOUT_SECONDS = 10.0
APPROVAL_TTL_SECONDS = 300

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

HIGH_RISK_MARKERS = (
    "delete", "remove", "drop", "destroy", "purge", "truncate",
    "exec", "execute", "shell", "command", "run_command", "spawn",
    "admin", "sudo", "install", "uninstall", "config", "credential",
    "secret", "token", "payment", "transfer", "withdraw", "revoke",
    "grant", "disable", "system", "deploy", "publish",
)
MEDIUM_RISK_MARKERS = (
    "write", "create", "update", "upsert", "insert", "upload", "send",
    "post", "patch", "put", "set", "append", "move", "rename", "submit",
    "modify", "edit", "save", "import", "sync",
)
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS openclaw_connector_servers(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    name TEXT NOT NULL,
    tools_json TEXT NOT NULL,
    allowed_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(tenant_id, principal_id, name)
);
CREATE TABLE IF NOT EXISTS openclaw_connector_approvals(
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS openclaw_connector_approvals_scope
    ON openclaw_connector_approvals(tenant_id, principal_id, status);
CREATE TABLE IF NOT EXISTS openclaw_connector_calls(
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    http_status INTEGER,
    error_json TEXT,
    output_digest TEXT,
    output_bytes INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS openclaw_connector_calls_scope
    ON openclaw_connector_calls(tenant_id, principal_id, created_at);
CREATE TABLE IF NOT EXISTS openclaw_connector_audit(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    server_id TEXT,
    tool_name TEXT,
    request_id TEXT,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS openclaw_connector_audit_scope
    ON openclaw_connector_audit(tenant_id, principal_id, id);
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return now()


def _until(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _detail(detail: Mapping[str, Any] | None) -> str:
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


@dataclass(frozen=True)
class ConnectorIdentity:
    tenant_id: str
    principal_id: str

    def __post_init__(self):
        for name, value in (("tenant_id", self.tenant_id), ("principal_id", self.principal_id)):
            if not isinstance(value, str) or not (1 <= len(value) <= 200) or any(ord(char) < 32 for char in value):
                raise ConnectorError(400, "INVALID_IDENTITY", f"{name} 无效")


@dataclass(frozen=True)
class DeclaredTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only_hint: bool
    destructive_hint: bool
    idempotent_hint: bool | None
    open_world_hint: bool | None
    risk: str
    grant: str


@dataclass(frozen=True)
class ToolCallResult:
    request_id: str
    tool_name: str
    risk: str
    grant: str
    output: Any
    duration_ms: int

    def as_mcp_result(self) -> dict[str, Any]:
        text = self.output if isinstance(self.output, str) else json.dumps(self.output, ensure_ascii=False, separators=(",", ":"))
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "requestId": self.request_id,
            "risk": self.risk,
            "grant": self.grant,
        }


class ConnectorError(OpenClawError):
    """Connector errors reuse the OpenAI-compatible public error envelope."""


class ToolExecutor(Protocol):
    async def call(self, server: Mapping[str, Any], tool: DeclaredTool, arguments: dict[str, Any],
                   cancelled: asyncio.Event, request_id: str) -> Any: ...


class UnavailableToolExecutor:
    async def call(self, server, tool, arguments, cancelled, request_id):
        raise ConnectorError(503, "TOOL_UNAVAILABLE", "连接器已接入，但本轮尚未绑定经批准的工具执行器")


@dataclass
class ActiveCall:
    request_id: str
    identity: ConnectorIdentity
    tool_name: str
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


def _classify(name: str, read_only: bool, destructive: bool) -> tuple[str, str]:
    """Return ``(risk, grant)`` for one whitelisted MCP tool.

    ``grant`` is ``auto``, ``approval`` or ``denied``.  Unknown tools are high
    risk and denied; the safe default never widens because a name was not
    recognized.
    """
    lowered = name.lower()
    if destructive:
        return RISK_HIGH, "denied"
    if any(marker in lowered for marker in HIGH_RISK_MARKERS):
        return RISK_HIGH, "denied"
    if read_only:
        return RISK_LOW, "auto"
    if any(marker in lowered for marker in MEDIUM_RISK_MARKERS):
        return RISK_MEDIUM, "approval"
    return RISK_HIGH, "denied"


def _validate_tool(item: Any, index: int) -> DeclaredTool:
    if not isinstance(item, dict):
        raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}] 必须是对象")
    name = item.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
        raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}].name 无效")
    description = item.get("description", "")
    if not isinstance(description, str) or len(description) > 2000:
        raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}].description 无效")
    schema = item.get("inputSchema", {"type": "object", "properties": {}})
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}].inputSchema 必须描述对象")
    if not isinstance(schema.get("properties", {}), dict):
        raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}].inputSchema.properties 无效")
    try:
        size = len(json.dumps(schema, ensure_ascii=False, separators=(",", ":")))
    except (TypeError, ValueError, RecursionError):
        raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}].inputSchema 无效") from None
    if size > MAX_TOOL_CHARS:
        raise ConnectorError(413, "TOOL_DECLARATION_TOO_LARGE", f"tools[{index}].inputSchema 过大")
    annotations = item.get("annotations", {})
    if annotations is None:
        annotations = {}
    if not isinstance(annotations, dict):
        raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}].annotations 无效")
    flags: dict[str, bool | None] = {}
    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
        value = annotations.get(key)
        if value is not None and not isinstance(value, bool):
            raise ConnectorError(422, "INVALID_TOOL_DECLARATION", f"tools[{index}].annotations.{key} 必须是布尔值")
        flags[key] = value
    risk, grant = _classify(name, bool(flags["readOnlyHint"]), bool(flags["destructiveHint"]))
    return DeclaredTool(name, description, schema, bool(flags["readOnlyHint"]), bool(flags["destructiveHint"]),
                        flags["idempotentHint"], flags["openWorldHint"], risk, grant)


def _bounded_arguments(schema: Mapping[str, Any], arguments: Any) -> tuple[dict[str, Any], str]:
    if not isinstance(arguments, dict):
        raise ConnectorError(400, "INVALID_TOOL_ARGUMENTS", "工具参数必须是 JSON 对象")
    try:
        encoded = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        digest_source = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        raise ConnectorError(400, "INVALID_TOOL_ARGUMENTS", "工具参数包含不可序列化内容") from None
    if len(encoded) > MAX_TOOL_CHARS:
        raise ConnectorError(413, "TOOL_ARGUMENTS_TOO_LARGE", "工具参数超过长度限制")
    budget = {"chars": 0}

    def walk(value: Any, depth: int) -> None:
        if depth > MAX_ARGUMENT_DEPTH:
            raise ConnectorError(413, "TOOL_ARGUMENTS_TOO_DEEP", "工具参数嵌套过深")
        if isinstance(value, str):
            budget["chars"] += len(value)
        elif isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or len(key) > 128:
                    raise ConnectorError(400, "INVALID_TOOL_ARGUMENTS", "工具参数键无效")
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ConnectorError(400, "INVALID_TOOL_ARGUMENTS", "工具参数包含不支持的值")
        if budget["chars"] > MAX_TOOL_CHARS:
            raise ConnectorError(413, "TOOL_ARGUMENTS_TOO_LARGE", "工具参数字符串总长度超过限制")

    walk(arguments, 0)
    required = schema.get("required", [])
    if required is not None:
        if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
            raise ConnectorError(422, "INVALID_TOOL_DECLARATION", "inputSchema.required 无效")
        missing = [key for key in required if key not in arguments]
        if missing:
            raise ConnectorError(400, "TOOL_ARGUMENTS_INVALID", "工具参数缺少必填字段", param=missing[0])
    properties = schema.get("properties", {})
    if isinstance(properties, dict) and schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            raise ConnectorError(400, "TOOL_ARGUMENTS_INVALID", "工具参数包含未声明字段", param=unknown[0])
    type_map = {
        "string": str,
        "boolean": bool,
        "object": dict,
        "array": list,
        "integer": int,
        "number": (int, float),
        "null": type(None),
    }
    if isinstance(properties, dict):
        for key, item in arguments.items():
            declaration = properties.get(key)
            if not isinstance(declaration, dict) or "type" not in declaration:
                continue
            expected = declaration["type"]
            if expected not in type_map:
                continue
            python_type = type_map[expected]
            if expected in ("integer", "number") and isinstance(item, bool):
                valid = False
            else:
                valid = isinstance(item, python_type)
            if not valid:
                raise ConnectorError(400, "TOOL_ARGUMENTS_INVALID", f"工具参数 {key} 类型无效", param=key)
    return arguments, _digest(digest_source)


class ConnectorRegistry:
    """Per-tenant tool whitelist, approval, call, cancel and revocation state."""

    def __init__(self, db, executor: ToolExecutor | None = None, *,
                 timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS):
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        self.db = db
        with self.db.lock:
            self.db.conn.executescript(_SCHEMA)
        self.executor = executor or UnavailableToolExecutor()
        self.timeout_seconds = float(timeout_seconds)
        self._active: dict[str, ActiveCall] = {}
        self._lock = threading.Lock()
        self.recover_interrupted()

    def _audit(self, identity: ConnectorIdentity, action: str, outcome: str, *,
               server_id: str | None = None, tool_name: str | None = None,
               request_id: str | None = None, detail: Mapping[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO openclaw_connector_audit(tenant_id,principal_id,server_id,tool_name,request_id,action,outcome,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (identity.tenant_id, identity.principal_id, server_id, tool_name, request_id,
             action, outcome, _detail(detail), _now_iso()),
        )

    def recover_interrupted(self) -> int:
        rows = self.db.all("SELECT * FROM openclaw_connector_calls WHERE status='running'")
        for row in rows:
            self.db.execute(
                "UPDATE openclaw_connector_calls SET status='failed',http_status=503,error_json=?,completed_at=? WHERE request_id=? AND status='running'",
                (dump({"code": "INTERRUPTED", "message": "连接器重启，未完成的工具调用已终止"}),
                 _now_iso(), row["request_id"]),
            )
            identity = ConnectorIdentity(row["tenant_id"], row["principal_id"])
            self._audit(identity, "tool.interrupted", "failed", request_id=row["request_id"],
                        tool_name=row["tool_name"])
        return len(rows)

    def audit_events(self, identity: ConnectorIdentity) -> list[dict[str, Any]]:
        rows = self.db.all(
            "SELECT * FROM openclaw_connector_audit WHERE tenant_id=? AND principal_id=? ORDER BY id",
            (identity.tenant_id, identity.principal_id),
        )
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    # ---------------------------------------------------------------- registry
    def _servers(self, identity: ConnectorIdentity, *, include_revoked: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM openclaw_connector_servers WHERE tenant_id=? AND principal_id=?"
        if not include_revoked:
            query += " AND revoked_at IS NULL"
        return self.db.all(query + " ORDER BY created_at", (identity.tenant_id, identity.principal_id))

    def _tool_map(self, identity: ConnectorIdentity) -> dict[str, tuple[dict[str, Any], DeclaredTool]]:
        result: dict[str, tuple[dict[str, Any], DeclaredTool]] = {}
        for server in self._servers(identity):
            policy = json.loads(server["policy_json"])
            tools = {item["name"]: item for item in json.loads(server["tools_json"])}
            for name, item in policy.items():
                if item["grant"] == "denied":
                    continue
                result[name] = (server, DeclaredTool(
                    name, tools[name]["description"], tools[name]["inputSchema"],
                    tools[name]["readOnlyHint"], tools[name]["destructiveHint"],
                    tools[name]["idempotentHint"], tools[name]["openWorldHint"],
                    item["risk"], item["grant"],
                ))
        return result

    def register_server(self, identity: ConnectorIdentity, name: str, tools: list[Any],
                        allowed_tools: list[str]) -> dict[str, Any]:
        if not isinstance(name, str) or not (1 <= len(name) <= 100):
            raise ConnectorError(422, "INVALID_SERVER", "连接器名称无效")
        if len(self._servers(identity, include_revoked=True)) >= MAX_SERVERS:
            raise ConnectorError(422, "TOO_MANY_SERVERS", "连接器数量超过限制")
        if self.db.one(
            "SELECT id FROM openclaw_connector_servers WHERE tenant_id=? AND principal_id=? AND name=?",
            (identity.tenant_id, identity.principal_id, name),
        ):
            raise ConnectorError(409, "SERVER_EXISTS", "同名连接器已登记")
        if not isinstance(tools, list) or not (1 <= len(tools) <= MAX_TOOLS_PER_SERVER):
            raise ConnectorError(422, "INVALID_TOOL_DECLARATION", "工具声明数量无效")
        declared: dict[str, DeclaredTool] = {}
        for index, item in enumerate(tools):
            tool = _validate_tool(item, index)
            if tool.name in declared:
                raise ConnectorError(422, "DUPLICATE_TOOL", f"重复的工具名称：{tool.name}")
            declared[tool.name] = tool
        if not isinstance(allowed_tools, list) or not (1 <= len(allowed_tools) <= MAX_TOOLS_PER_SERVER):
            raise ConnectorError(422, "INVALID_TOOL_WHITELIST", "工具白名单数量无效")
        if len(allowed_tools) != len(set(allowed_tools)):
            raise ConnectorError(422, "INVALID_TOOL_WHITELIST", "工具白名单包含重复名称")
        unknown = [tool_name for tool_name in allowed_tools if tool_name not in declared]
        if unknown:
            raise ConnectorError(422, "INVALID_TOOL_WHITELIST", f"白名单引用未声明的工具：{unknown[0]}")
        conflicts = sorted(set(allowed_tools) & set(self._tool_map(identity)))
        if conflicts:
            # Tool names are the routing key for an agent.  Allowing two
            # servers to expose the same name would make risk decisions
            # ambiguous, so require the operator to disambiguate first.
            raise ConnectorError(409, "TOOL_NAME_CONFLICT", f"工具名称已被其他连接器占用：{conflicts[0]}")
        server_id = uid()
        policy = {
            tool_name: {"risk": declared[tool_name].risk, "grant": declared[tool_name].grant}
            for tool_name in allowed_tools
        }
        stored_tools = [
            {"name": tool.name, "description": tool.description, "inputSchema": tool.input_schema,
             "readOnlyHint": tool.read_only_hint, "destructiveHint": tool.destructive_hint,
             "idempotentHint": tool.idempotent_hint, "openWorldHint": tool.open_world_hint}
            for tool in declared.values()
        ]
        self.db.execute(
            "INSERT INTO openclaw_connector_servers(id,tenant_id,principal_id,name,tools_json,allowed_json,policy_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (server_id, identity.tenant_id, identity.principal_id, name, dump(stored_tools),
             dump(list(allowed_tools)), dump(policy), _now_iso()),
        )
        denied = sorted(tool_name for tool_name, item in policy.items() if item["grant"] == "denied")
        self._audit(identity, "server.registered", "allowed", server_id=server_id,
                    detail={"name": name, "allowed": sorted(policy), "denied": denied})
        if denied:
            self._audit(identity, "tool.policy_denied", "denied", server_id=server_id,
                        detail={"tools": denied, "reason": "high_or_unknown_risk"})
        return {
            "id": server_id,
            "name": name,
            "allowed_tools": sorted(tool for tool, item in policy.items() if item["grant"] != "denied"),
            "denied_tools": denied,
        }

    def revoke_server(self, identity: ConnectorIdentity, server_id: str) -> bool:
        changed = self.db.execute(
            "UPDATE openclaw_connector_servers SET revoked_at=? WHERE id=? AND tenant_id=? AND principal_id=? AND revoked_at IS NULL",
            (_now_iso(), server_id, identity.tenant_id, identity.principal_id),
        ).rowcount
        if changed:
            self._audit(identity, "server.revoked", "allowed", server_id=server_id)
        return bool(changed)

    def revoke_tool(self, identity: ConnectorIdentity, server_id: str, tool_name: str) -> bool:
        server = self.db.one(
            "SELECT * FROM openclaw_connector_servers WHERE id=? AND tenant_id=? AND principal_id=? AND revoked_at IS NULL",
            (server_id, identity.tenant_id, identity.principal_id),
        )
        if not server:
            raise ConnectorError(404, "SERVER_NOT_FOUND", "连接器不存在或已撤销")
        policy = json.loads(server["policy_json"])
        if tool_name not in policy:
            raise ConnectorError(404, "TOOL_NOT_FOUND", "工具不在该连接器的白名单")
        policy[tool_name] = {"risk": RISK_HIGH, "grant": "denied"}
        self.db.execute("UPDATE openclaw_connector_servers SET policy_json=? WHERE id=?", (dump(policy), server_id))
        self._audit(identity, "tool.revoked", "allowed", server_id=server_id, tool_name=tool_name)
        return True

    def discover(self, identity: ConnectorIdentity) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for name, (server, tool) in self._tool_map(identity).items():
            items.append({
                "name": name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "annotations": {
                    "readOnlyHint": tool.read_only_hint,
                    "destructiveHint": tool.destructive_hint,
                    "idempotentHint": tool.idempotent_hint,
                    "openWorldHint": tool.open_world_hint,
                    "risk": tool.risk,
                    "requiresApproval": tool.grant == "approval",
                },
                "x-openclaw-server": server["name"],
            })
        return sorted(items, key=lambda item: item["name"])

    def _resolve_tool(self, identity: ConnectorIdentity, tool_name: str) -> tuple[dict[str, Any], DeclaredTool]:
        if not isinstance(tool_name, str) or not tool_name:
            raise ConnectorError(400, "INVALID_TOOL_NAME", "工具名称无效")
        available = self._tool_map(identity)
        if tool_name in available:
            return available[tool_name]
        # Distinguish a known but denied/revoked tool from an unknown name in
        # the audit trail without revealing another tenant's inventory.
        for server in self._servers(identity, include_revoked=True):
            policy = json.loads(server["policy_json"])
            if tool_name in policy:
                if server["revoked_at"]:
                    self._audit(identity, "tool.denied", "denied", server_id=server["id"], tool_name=tool_name,
                                detail={"reason": "server_revoked"})
                    raise ConnectorError(403, "TOOL_REVOKED", "连接器已撤销")
                self._audit(identity, "tool.denied", "denied", server_id=server["id"], tool_name=tool_name,
                            detail={"reason": "policy_denied", "risk": policy[tool_name]["risk"]})
                raise ConnectorError(403, "TOOL_DENIED", "工具被安全策略默认拒绝")
        self._audit(identity, "tool.denied", "denied", tool_name=tool_name[:128],
                    detail={"reason": "not_whitelisted"})
        raise ConnectorError(404, "TOOL_NOT_FOUND", "工具不存在或未被授权")

    # --------------------------------------------------------------- approvals
    def request_approval(self, identity: ConnectorIdentity, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        server, tool = self._resolve_tool(identity, tool_name)
        if tool.grant == "auto":
            raise ConnectorError(409, "APPROVAL_NOT_REQUIRED", "该工具属于低风险只读工具，不需要审批")
        arguments, digest = _bounded_arguments(tool.input_schema, arguments)
        approval_id = uid()
        self.db.execute(
            "INSERT INTO openclaw_connector_approvals(id,tenant_id,principal_id,tool_name,arguments_digest,status,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (approval_id, identity.tenant_id, identity.principal_id, tool.name, digest, "pending",
             _now_iso(), _until(APPROVAL_TTL_SECONDS)),
        )
        self._audit(identity, "approval.requested", "pending", server_id=server["id"], tool_name=tool.name,
                    detail={"approval_id": approval_id, "risk": tool.risk, "arguments_digest": digest})
        return {"id": approval_id, "tool_name": tool.name, "status": "pending", "risk": tool.risk,
                "expires_at": _until(APPROVAL_TTL_SECONDS), "arguments_digest": digest}

    def decide_approval(self, identity: ConnectorIdentity, approval_id: str, approved: bool) -> dict[str, Any]:
        row = self.db.one(
            "SELECT * FROM openclaw_connector_approvals WHERE id=? AND tenant_id=? AND principal_id=?",
            (approval_id, identity.tenant_id, identity.principal_id),
        )
        if not row:
            raise ConnectorError(404, "APPROVAL_NOT_FOUND", "审批不存在或无权操作")
        if row["status"] != "pending":
            raise ConnectorError(409, "APPROVAL_FINALIZED", "审批已处理")
        if row["expires_at"] <= _now_iso():
            self.db.execute("UPDATE openclaw_connector_approvals SET status='expired',decided_at=? WHERE id=?",
                            (_now_iso(), approval_id))
            raise ConnectorError(409, "APPROVAL_EXPIRED", "审批已过期")
        status = "approved" if approved else "denied"
        self.db.execute("UPDATE openclaw_connector_approvals SET status=?,decided_at=? WHERE id=?",
                        (status, _now_iso(), approval_id))
        self._audit(identity, "approval.approved" if approved else "approval.denied", status,
                    tool_name=row["tool_name"], detail={"approval_id": approval_id})
        return {"id": approval_id, "tool_name": row["tool_name"], "status": status}

    # -------------------------------------------------------------------- calls
    async def call(self, identity: ConnectorIdentity, tool_name: str, arguments: dict[str, Any],
                   *, approval_id: str | None = None, request_id: str | None = None,
                   timeout_seconds: float | None = None) -> ToolCallResult:
        server, tool = self._resolve_tool(identity, tool_name)
        arguments, digest = _bounded_arguments(tool.input_schema, arguments)
        if tool.grant == "approval":
            if approval_id is None:
                raise ConnectorError(403, "APPROVAL_REQUIRED", "该工具需要审批，请先申请并取得批准")
            approval = self.db.one(
                "SELECT * FROM openclaw_connector_approvals WHERE id=? AND tenant_id=? AND principal_id=?",
                (approval_id, identity.tenant_id, identity.principal_id),
            )
            if not approval:
                raise ConnectorError(404, "APPROVAL_NOT_FOUND", "审批不存在或无权使用")
            if approval["status"] != "approved":
                raise ConnectorError(403, "APPROVAL_REQUIRED", "审批尚未通过")
            if approval["used_at"]:
                raise ConnectorError(409, "APPROVAL_USED", "审批已使用，不能重复调用")
            if approval["expires_at"] <= _now_iso():
                raise ConnectorError(409, "APPROVAL_EXPIRED", "审批已过期")
            if approval["tool_name"] != tool.name or approval["arguments_digest"] != digest:
                raise ConnectorError(409, "APPROVAL_MISMATCH", "审批与当前工具或参数不一致")
        if timeout_seconds is None:
            timeout_seconds = self.timeout_seconds
        if not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 300:
            raise ConnectorError(400, "INVALID_TIMEOUT", "工具超时时间无效")
        request_id = request_id or uid()
        if not isinstance(request_id, str) or not (1 <= len(request_id) <= 200):
            raise ConnectorError(400, "INVALID_REQUEST_ID", "工具调用编号无效")
        existing = self.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
        if existing:
            if existing["tenant_id"] != identity.tenant_id or existing["principal_id"] != identity.principal_id:
                raise ConnectorError(404, "TOOL_CALL_NOT_FOUND", "工具调用不存在或无权操作")
            raise ConnectorError(409, "TOOL_CALL_EXISTS", "工具调用编号已存在")
        started = _now_iso()
        self.db.execute(
            "INSERT INTO openclaw_connector_calls(request_id,tenant_id,principal_id,tool_name,status,created_at) VALUES(?,?,?,?,?,?)",
            (request_id, identity.tenant_id, identity.principal_id, tool.name, "running", started),
        )
        active = ActiveCall(request_id, identity, tool.name)
        with self._lock:
            self._active[request_id] = active
        self._audit(identity, "tool.started", "allowed", server_id=server["id"], tool_name=tool.name,
                    request_id=request_id,
                    detail={"risk": tool.risk, "grant": tool.grant, "arguments_digest": digest,
                            "approval_id": approval_id})
        if tool.grant == "approval":
            used = self.db.execute(
                "UPDATE openclaw_connector_approvals SET used_at=? WHERE id=? AND used_at IS NULL",
                (_now_iso(), approval_id),
            ).rowcount
            if used != 1:
                self._finish_call(request_id, "denied", 409, "APPROVAL_USED", None, None)
                with self._lock:
                    self._active.pop(request_id, None)
                raise ConnectorError(409, "APPROVAL_USED", "审批已使用，不能重复调用")
        started_at = datetime.now(timezone.utc)
        try:
            result = await self._run_executor(server, tool, arguments, active, request_id, float(timeout_seconds))
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            encoded = text.encode("utf-8")
            if len(encoded) > MAX_OUTPUT_CHARS:
                raise ConnectorError(502, "TOOL_OUTPUT_TOO_LARGE", "工具输出超过长度限制，已拒绝返回")
            duration = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
            self._finish_call(request_id, "succeeded", 200, None, _digest(text), len(encoded))
            self._audit(identity, "tool.succeeded", "allowed", server_id=server["id"], tool_name=tool.name,
                        request_id=request_id,
                        detail={"risk": tool.risk, "output_digest": _digest(text), "output_bytes": len(encoded),
                                "duration_ms": duration})
            return ToolCallResult(request_id, tool.name, tool.risk, tool.grant, result, duration)
        except asyncio.CancelledError:
            self._finish_call(request_id, "cancelled", 499, "TOOL_CANCELLED", None, None)
            self._audit(identity, "tool.cancelled", "cancelled", server_id=server["id"], tool_name=tool.name,
                        request_id=request_id, detail={"reason": "caller_cancelled"})
            raise
        except ConnectorError as error:
            status = "cancelled" if error.code == "TOOL_CANCELLED" else "failed"
            self._finish_call(request_id, status, error.status, error.code, None, None)
            action = {"TOOL_TIMEOUT": "tool.timed_out", "TOOL_CANCELLED": "tool.cancelled"}.get(
                error.code, "tool.failed")
            self._audit(identity, action, status, server_id=server["id"], tool_name=tool.name,
                        request_id=request_id, detail={"code": error.code, "status": error.status})
            raise
        except Exception:
            self._finish_call(request_id, "failed", 502, "TOOL_EXECUTION_FAILED", None, None)
            self._audit(identity, "tool.failed", "failed", server_id=server["id"], tool_name=tool.name,
                        request_id=request_id, detail={"code": "TOOL_EXECUTION_FAILED"})
            raise ConnectorError(502, "TOOL_EXECUTION_FAILED", "工具执行失败") from None
        finally:
            with self._lock:
                self._active.pop(request_id, None)

    async def _run_executor(self, server, tool: DeclaredTool, arguments, active: ActiveCall,
                            request_id: str, timeout: float) -> Any:
        task = asyncio.create_task(self.executor.call(server, tool, arguments, active.cancelled, request_id))
        cancel_wait = asyncio.create_task(active.cancelled.wait())
        try:
            done, _ = await asyncio.wait({task, cancel_wait}, timeout=timeout,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                task.cancel()
                await _discard(task)
                raise ConnectorError(504, "TOOL_TIMEOUT", "工具执行超时")
            if cancel_wait in done:
                task.cancel()
                await _discard(task)
                raise ConnectorError(499, "TOOL_CANCELLED", "工具调用已取消")
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

    def _finish_call(self, request_id: str, status: str, http_status: int, code: str | None,
                     output_digest: str | None, output_bytes: int | None) -> None:
        error_json = dump({"code": code, "message": "工具调用未完成"}) if code else None
        self.db.execute(
            "UPDATE openclaw_connector_calls SET status=?,http_status=?,error_json=?,output_digest=?,output_bytes=?,completed_at=? WHERE request_id=? AND status='running'",
            (status, http_status, error_json, output_digest, output_bytes, _now_iso(), request_id),
        )

    def cancel(self, identity: ConnectorIdentity, request_id: str) -> dict[str, Any]:
        with self._lock:
            active = self._active.get(request_id)
        if active and active.identity == identity:
            active.cancelled.set()
            self._audit(identity, "tool.cancel", "allowed", request_id=request_id, tool_name=active.tool_name)
            return {"request_id": request_id, "status": "cancelling"}
        row = self.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
        if not row or row["tenant_id"] != identity.tenant_id or row["principal_id"] != identity.principal_id:
            raise ConnectorError(404, "TOOL_CALL_NOT_FOUND", "工具调用不存在或无权操作")
        self._audit(identity, "tool.cancel", "allowed", request_id=request_id, tool_name=row["tool_name"],
                    detail={"status": row["status"], "terminal": True})
        return {"request_id": request_id, "status": row["status"]}


async def _discard(task: asyncio.Task) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


__all__ = [
    "APPROVAL_TTL_SECONDS",
    "ConnectorError",
    "ConnectorIdentity",
    "ConnectorRegistry",
    "DeclaredTool",
    "RISK_HIGH",
    "RISK_LOW",
    "RISK_MEDIUM",
    "ToolCallResult",
    "ToolExecutor",
    "UnavailableToolExecutor",
]
