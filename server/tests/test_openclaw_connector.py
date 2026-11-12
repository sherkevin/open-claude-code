"""Synthetic tests for the OpenClaw MCP/Skill/plugin security boundary.

No network MCP server is contacted.  All tools, executors and tenants are
synthetic local fixtures, and every assertion is about the connector's own
whitelist, approval, timeout, cancellation, revocation and audit behavior.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.db import dump, now, uid
from app.openclaw_connector import (
    ConnectorError,
    ConnectorIdentity,
    ConnectorRegistry,
    UnavailableToolExecutor,
)
from app.service import Service

TOOLS = [
    {
        "name": "read_ticket",
        "description": "读取工单",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                        "required": ["id"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "idempotentHint": True},
    },
    {
        "name": "list_tickets",
        "description": "列出工单",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "create_ticket",
        "description": "创建工单",
        "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}},
                        "required": ["title"], "additionalProperties": False},
    },
    {
        "name": "update_ticket",
        "description": "更新工单",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "delete_ticket",
        "description": "删除工单",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "mystery_tool",
        "description": "未分类工具",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "install_plugin",
        "description": "声明为只读的插件安装工具",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "slow_tool",
        "description": "慢速只读工具",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "wait_tool",
        "description": "可取消只读工具",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]
ALLOWED = [item["name"] for item in TOOLS]


class Executor:
    async def call(self, server, tool, arguments, cancelled, request_id):
        if tool.name == "slow_tool":
            await asyncio.sleep(2)
        if tool.name == "large_tool":
            return "x" * 20_001
        if tool.name == "fail_tool":
            raise RuntimeError("synthetic internal detail")
        return {"tool": tool.name, "ok": True}


class WaitingExecutor:
    def __init__(self):
        self.started = asyncio.Event()

    async def call(self, server, tool, arguments, cancelled, request_id):
        self.started.set()
        await cancelled.wait()
        raise asyncio.CancelledError


@dataclass
class Env:
    service: Service
    registry: ConnectorRegistry
    me: ConnectorIdentity
    other: ConnectorIdentity


def register(env: Env, identity: ConnectorIdentity | None = None, allowed: list[str] | None = None,
             tools: list[dict] | None = None, name: str = "tickets") -> dict:
    return env.registry.register_server(identity or env.me, name, tools or TOOLS, allowed or ALLOWED)


def call(env: Env, tool: str, arguments: dict, *, identity: ConnectorIdentity | None = None, **kwargs):
    return asyncio.run(env.registry.call(identity or env.me, tool, arguments, **kwargs))


def error(response) -> tuple[int, str]:
    return response.status, response.code


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        provider_settings="",
        origins=("http://testserver",),
        mcp_hosts=("localhost", "127.0.0.1"),
        idle_seconds=60,
        approval_seconds=3,
    )
    service = Service(settings)
    registry = ConnectorRegistry(service.db, executor=Executor(), timeout_seconds=0.2)
    yield Env(service, registry, ConnectorIdentity("tenant-a", "principal-a"),
              ConnectorIdentity("tenant-b", "principal-b"))
    service.db.close()


# ---------------------------------------------------------------- discovery
def test_discovery_only_exposes_whitelisted_safe_tools(env):
    result = register(env)
    assert result["allowed_tools"] == ["create_ticket", "list_tickets", "read_ticket",
                                       "slow_tool", "update_ticket", "wait_tool"]
    assert set(result["denied_tools"]) == {"delete_ticket", "install_plugin", "mystery_tool"}

    discovered = env.registry.discover(env.me)
    names = [item["name"] for item in discovered]
    assert names == sorted(names)
    assert set(names) == {"create_ticket", "list_tickets", "read_ticket", "slow_tool", "update_ticket", "wait_tool"}
    assert "delete_ticket" not in names
    assert "mystery_tool" not in names
    annotations = {item["name"]: item["annotations"] for item in discovered}
    assert annotations["read_ticket"]["risk"] == "low"
    assert annotations["read_ticket"]["requiresApproval"] is False
    assert annotations["create_ticket"]["risk"] == "medium"
    assert annotations["create_ticket"]["requiresApproval"] is True
    assert annotations["update_ticket"]["risk"] == "medium"

    assert env.registry.discover(env.other) == []
    assert any(event["action"] == "tool.policy_denied" for event in env.registry.audit_events(env.me))


def test_unknown_and_high_risk_tools_are_denied_by_default(env):
    register(env)
    for tool, arguments in (("delete_ticket", {"id": "1"}),
                            ("mystery_tool", {}),
                            ("install_plugin", {})):
        with pytest.raises(ConnectorError) as exc:
            call(env, tool, arguments)
        assert (exc.value.status, exc.value.code) == (403, "TOOL_DENIED")
    with pytest.raises(ConnectorError) as exc:
        call(env, "never_declared", {})
    assert (exc.value.status, exc.value.code) == (404, "TOOL_NOT_FOUND")

    audit = env.registry.audit_events(env.me)
    denied = [event for event in audit if event["action"] == "tool.denied"]
    assert any(event["detail"].get("reason") == "policy_denied" for event in denied)
    assert any(event["detail"].get("reason") == "not_whitelisted" for event in denied)


# --------------------------------------------------------- approval lifecycle
def test_low_risk_read_only_tool_runs_without_approval(env):
    register(env)
    result = call(env, "read_ticket", {"id": "T-1"})
    assert result.risk == "low"
    assert result.grant == "auto"
    mcp = result.as_mcp_result()
    assert mcp["isError"] is False
    assert json.loads(mcp["content"][0]["text"]) == {"tool": "read_ticket", "ok": True}
    assert mcp["risk"] == "low"
    actions = [event["action"] for event in env.registry.audit_events(env.me)]
    assert "tool.started" in actions
    assert "tool.succeeded" in actions


def test_medium_risk_requires_approval_and_approval_is_single_use(env):
    register(env)
    with pytest.raises(ConnectorError) as exc:
        call(env, "create_ticket", {"title": "x"})
    assert (exc.value.status, exc.value.code) == (403, "APPROVAL_REQUIRED")

    approval = env.registry.request_approval(env.me, "create_ticket", {"title": "x"})
    assert approval["status"] == "pending"
    assert approval["risk"] == "medium"
    with pytest.raises(ConnectorError) as exc:
        call(env, "create_ticket", {"title": "x"}, approval_id=approval["id"])
    assert exc.value.code == "APPROVAL_REQUIRED"

    assert env.registry.decide_approval(env.me, approval["id"], True)["status"] == "approved"
    result = call(env, "create_ticket", {"title": "x"}, approval_id=approval["id"])
    assert result.risk == "medium"
    assert result.grant == "approval"
    with pytest.raises(ConnectorError) as exc:
        call(env, "create_ticket", {"title": "x"}, approval_id=approval["id"])
    assert (exc.value.status, exc.value.code) == (409, "APPROVAL_USED")

    mismatch = env.registry.request_approval(env.me, "create_ticket", {"title": "x"})
    env.registry.decide_approval(env.me, mismatch["id"], True)
    with pytest.raises(ConnectorError) as exc:
        call(env, "create_ticket", {"title": "different"}, approval_id=mismatch["id"])
    assert (exc.value.status, exc.value.code) == (409, "APPROVAL_MISMATCH")

    denied = env.registry.request_approval(env.me, "create_ticket", {"title": "x"})
    env.registry.decide_approval(env.me, denied["id"], False)
    with pytest.raises(ConnectorError) as exc:
        call(env, "create_ticket", {"title": "x"}, approval_id=denied["id"])
    assert (exc.value.status, exc.value.code) == (403, "APPROVAL_REQUIRED")

    with pytest.raises(ConnectorError) as exc:
        env.registry.request_approval(env.me, "read_ticket", {"id": "1"})
    assert (exc.value.status, exc.value.code) == (409, "APPROVAL_NOT_REQUIRED")


def test_approval_expiry_and_tenant_isolation(env):
    register(env)
    register(env, identity=env.other, name="other-tickets")
    expired = env.registry.request_approval(env.me, "create_ticket", {"title": "x"})
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    env.service.db.execute("UPDATE openclaw_connector_approvals SET expires_at=? WHERE id=?", (past, expired["id"]))
    with pytest.raises(ConnectorError) as exc:
        env.registry.decide_approval(env.me, expired["id"], True)
    assert (exc.value.status, exc.value.code) == (409, "APPROVAL_EXPIRED")

    approved = env.registry.request_approval(env.me, "create_ticket", {"title": "x"})
    env.registry.decide_approval(env.me, approved["id"], True)
    env.service.db.execute("UPDATE openclaw_connector_approvals SET expires_at=? WHERE id=?", (past, approved["id"]))
    with pytest.raises(ConnectorError) as exc:
        call(env, "create_ticket", {"title": "x"}, approval_id=approved["id"])
    assert (exc.value.status, exc.value.code) == (409, "APPROVAL_EXPIRED")

    isolated = env.registry.request_approval(env.me, "create_ticket", {"title": "x"})
    with pytest.raises(ConnectorError) as exc:
        env.registry.decide_approval(env.other, isolated["id"], True)
    assert (exc.value.status, exc.value.code) == (404, "APPROVAL_NOT_FOUND")
    env.registry.decide_approval(env.me, isolated["id"], True)
    with pytest.raises(ConnectorError) as exc:
        call(env, "create_ticket", {"title": "x"}, identity=env.other, approval_id=isolated["id"])
    assert (exc.value.status, exc.value.code) == (404, "APPROVAL_NOT_FOUND")


# ------------------------------------------------------- timeout / cancellation
def test_tool_timeout_is_bounded_and_audited(env):
    register(env)
    with pytest.raises(ConnectorError) as exc:
        call(env, "slow_tool", {}, timeout_seconds=0.05)
    assert (exc.value.status, exc.value.code) == (504, "TOOL_TIMEOUT")
    rows = env.service.db.all("SELECT * FROM openclaw_connector_calls WHERE tool_name='slow_tool'")
    assert rows and rows[-1]["status"] == "failed"
    assert json.loads(rows[-1]["error_json"])["code"] == "TOOL_TIMEOUT"
    assert any(event["action"] == "tool.timed_out" for event in env.registry.audit_events(env.me))


def test_tool_cancellation_and_cross_tenant_cancel(env):
    async def scenario():
        started = asyncio.Event()

        class Waiting:
            async def call(self, server, tool, arguments, cancelled, request_id):
                started.set()
                await cancelled.wait()
                raise asyncio.CancelledError

        registry = ConnectorRegistry(env.service.db, executor=Waiting(), timeout_seconds=5)
        registry.register_server(env.me, "waiting", TOOLS, ALLOWED)
        task = asyncio.create_task(registry.call(env.me, "wait_tool", {}))
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(100):
            if registry._active:
                break
            await asyncio.sleep(0.01)
        request_id = next(iter(registry._active))
        assert registry.cancel(env.me, request_id)["status"] == "cancelling"
        with pytest.raises(ConnectorError) as exc:
            await task
        assert (exc.value.status, exc.value.code) == (499, "TOOL_CANCELLED")
        row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
        assert row["status"] == "cancelled"
        assert any(event["action"] == "tool.cancelled" for event in registry.audit_events(env.me))

        with pytest.raises(ConnectorError) as exc:
            registry.cancel(env.other, request_id)
        assert (exc.value.status, exc.value.code) == (404, "TOOL_CALL_NOT_FOUND")
        with pytest.raises(ConnectorError) as exc:
            registry.cancel(env.me, "not-a-call")
        assert (exc.value.status, exc.value.code) == (404, "TOOL_CALL_NOT_FOUND")

    asyncio.run(scenario())


# ----------------------------------------------------------- revocation / scope
def test_tool_and_server_revocation_and_tenant_isolation(env):
    register(env)
    server_id = env.registry._servers(env.me)[0]["id"]
    assert env.registry.revoke_tool(env.me, server_id, "read_ticket") is True
    assert "read_ticket" not in {item["name"] for item in env.registry.discover(env.me)}
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", {"id": "1"})
    assert (exc.value.status, exc.value.code) == (403, "TOOL_DENIED")

    assert env.registry.revoke_server(env.me, server_id) is True
    assert env.registry.discover(env.me) == []
    with pytest.raises(ConnectorError) as exc:
        call(env, "list_tickets", {})
    assert (exc.value.status, exc.value.code) == (403, "TOOL_REVOKED")

    assert env.registry.revoke_server(env.other, server_id) is False
    with pytest.raises(ConnectorError) as exc:
        env.registry.revoke_tool(env.other, server_id, "read_ticket")
    assert (exc.value.status, exc.value.code) == (404, "SERVER_NOT_FOUND")

    other_server = env.registry.register_server(env.other, "tickets", TOOLS, ALLOWED)
    assert other_server["id"] != server_id
    assert env.registry.discover(env.me) == []
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", {"id": "1"}, identity=env.me)
    assert (exc.value.status, exc.value.code) == (403, "TOOL_REVOKED")
    other_result = call(env, "read_ticket", {"id": "T-2"}, identity=env.other)
    assert other_result.risk == "low"
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", {"id": "1"}, identity=env.other, request_id=other_result.request_id)
    assert (exc.value.status, exc.value.code) == (409, "TOOL_CALL_EXISTS")


# -------------------------------------------------------- argument validation
def test_argument_and_declaration_bounds(env):
    register(env)
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", ["not", "an", "object"])
    assert (exc.value.status, exc.value.code) == (400, "INVALID_TOOL_ARGUMENTS")
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", {})
    assert (exc.value.status, exc.value.code) == (400, "TOOL_ARGUMENTS_INVALID")
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", {"id": 1})
    assert (exc.value.status, exc.value.code) == (400, "TOOL_ARGUMENTS_INVALID")
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", {"id": "1", "extra": "x"})
    assert (exc.value.status, exc.value.code) == (400, "TOOL_ARGUMENTS_INVALID")
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", {"id": "x" * 20_001})
    assert (exc.value.status, exc.value.code) == (413, "TOOL_ARGUMENTS_TOO_LARGE")
    nested: dict = {}
    cursor = nested
    for _ in range(8):
        cursor["next"] = {}
        cursor = cursor["next"]
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_ticket", nested)
    assert (exc.value.status, exc.value.code) == (413, "TOOL_ARGUMENTS_TOO_DEEP")

    with pytest.raises(ConnectorError) as exc:
        register(env, name="duplicate-declaration", tools=[TOOLS[0], TOOLS[0]])
    assert (exc.value.status, exc.value.code) == (422, "DUPLICATE_TOOL")
    with pytest.raises(ConnectorError) as exc:
        register(env, name="bad-schema", tools=[{"name": "bad", "inputSchema": {"type": "array"}}],
                 allowed=["bad"])
    assert (exc.value.status, exc.value.code) == (422, "INVALID_TOOL_DECLARATION")
    with pytest.raises(ConnectorError) as exc:
        register(env, name="bad-whitelist", allowed=["not-declared"])
    assert (exc.value.status, exc.value.code) == (422, "INVALID_TOOL_WHITELIST")
    with pytest.raises(ConnectorError) as exc:
        register(env, name="dup-whitelist", allowed=["read_ticket", "read_ticket"])
    assert (exc.value.status, exc.value.code) == (422, "INVALID_TOOL_WHITELIST")
    with pytest.raises(ConnectorError) as exc:
        register(env, name="tickets")
    assert (exc.value.status, exc.value.code) == (409, "SERVER_EXISTS")
    with pytest.raises(ConnectorError) as exc:
        register(env, name="conflict", tools=[TOOLS[0]], allowed=["read_ticket"])
    assert (exc.value.status, exc.value.code) == (409, "TOOL_NAME_CONFLICT")
    too_many = [{"name": f"read_{index}", "inputSchema": {"type": "object", "properties": {}},
                 "annotations": {"readOnlyHint": True}} for index in range(65)]
    with pytest.raises(ConnectorError) as exc:
        register(env, name="too-many", tools=too_many, allowed=[item["name"] for item in too_many])
    assert (exc.value.status, exc.value.code) == (422, "INVALID_TOOL_DECLARATION")


# ------------------------------------------------------- output / failures
def test_output_bounds_and_executor_failures(env):
    failing_tools = [
        {"name": "large_tool", "description": "large", "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True}},
        {"name": "fail_tool", "description": "fail", "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True}},
    ]
    register(env, name="failing", tools=failing_tools, allowed=["large_tool", "fail_tool"])
    with pytest.raises(ConnectorError) as exc:
        call(env, "large_tool", {})
    assert (exc.value.status, exc.value.code) == (502, "TOOL_OUTPUT_TOO_LARGE")
    with pytest.raises(ConnectorError) as exc:
        call(env, "fail_tool", {})
    assert (exc.value.status, exc.value.code) == (502, "TOOL_EXECUTION_FAILED")
    assert "synthetic internal detail" not in exc.value.message

    async def unavailable_case():
        registry = ConnectorRegistry(env.service.db, executor=UnavailableToolExecutor())
        registry.register_server(env.other, "unavailable", TOOLS, ALLOWED)
        with pytest.raises(ConnectorError) as exc:
            await registry.call(env.other, "read_ticket", {"id": "1"})
        assert (exc.value.status, exc.value.code) == (503, "TOOL_UNAVAILABLE")

    asyncio.run(unavailable_case())


# ------------------------------------------------------------------- audit
def test_audit_redacts_arguments_and_records_digests(env):
    register(env)
    result = call(env, "read_ticket", {"id": "SECRET-ARGUMENT-XYZ"})
    assert result.risk == "low"
    audit = env.registry.audit_events(env.me)
    text = json.dumps(audit, ensure_ascii=False)
    assert "SECRET-ARGUMENT-XYZ" not in text
    started = [event for event in audit if event["action"] == "tool.started"]
    assert started
    digest = started[-1]["detail"]["arguments_digest"]
    assert len(digest) == 64
    assert started[-1]["detail"]["risk"] == "low"
    assert started[-1]["detail"]["grant"] == "auto"
    assert started[-1]["request_id"] == result.request_id
    assert {"server.registered", "tool.started", "tool.succeeded"} <= {event["action"] for event in audit}
    call_row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (result.request_id,))
    assert call_row["output_digest"] and len(call_row["output_digest"]) == 64
    assert "SECRET-ARGUMENT-XYZ" not in json.dumps(call_row, ensure_ascii=False)
    assert "SECRET-ARGUMENT-XYZ" not in json.dumps(env.registry._servers(env.me), ensure_ascii=False)


def test_recovery_marks_running_calls_as_interrupted(env):
    request_id = uid()
    env.service.db.execute(
        "INSERT INTO openclaw_connector_calls(request_id,tenant_id,principal_id,tool_name,status,created_at) VALUES(?,?,?,?,?,?)",
        (request_id, env.me.tenant_id, env.me.principal_id, "read_ticket", "running", now()),
    )
    registry = ConnectorRegistry(env.service.db, executor=Executor())
    row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
    assert row["status"] == "failed"
    assert row["http_status"] == 503
    assert json.loads(row["error_json"])["code"] == "INTERRUPTED"
    assert any(event["action"] == "tool.interrupted" for event in registry.audit_events(env.me))
