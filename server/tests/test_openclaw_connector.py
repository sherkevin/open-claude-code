"""Synthetic tests for the OpenClaw MCP/Skill/plugin security boundary.

No network MCP server is contacted.  All tools, executors and tenants are
synthetic local fixtures, and every assertion is about the connector's own
whitelist, approval, timeout, cancellation, revocation and audit behavior.
"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app import openclaw_connector as connector_module
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
        "name": "read_slow_tool",
        "description": "慢速只读工具",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "read_wait_tool",
        "description": "可取消只读工具",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
]
ALLOWED = [item["name"] for item in TOOLS]


class Executor:
    async def call(self, server, tool, arguments, cancelled, request_id):
        if tool.name == "read_slow_tool":
            await asyncio.sleep(2)
        if tool.name == "read_large_tool":
            return "x" * 20_001
        if tool.name == "read_fail_tool":
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


@pytest.fixture
def lease_clock(monkeypatch):
    class Clock:
        value = datetime(2040, 1, 1, tzinfo=timezone.utc)

        def now(self):
            return self.value.isoformat()

        def deadline(self, seconds):
            return (self.value + timedelta(seconds=seconds)).isoformat()

        def advance(self, seconds):
            self.value += timedelta(seconds=seconds)

    clock = Clock()
    monkeypatch.setattr(connector_module, "_now_iso", clock.now)
    monkeypatch.setattr(connector_module, "_lease_deadline", clock.deadline)
    return clock


# ---------------------------------------------------------------- discovery
def test_discovery_only_exposes_whitelisted_safe_tools(env):
    result = register(env)
    assert result["allowed_tools"] == ["create_ticket", "list_tickets",
                                       "read_slow_tool", "read_ticket",
                                       "read_wait_tool", "update_ticket"]
    assert set(result["denied_tools"]) == {"delete_ticket", "install_plugin", "mystery_tool"}

    discovered = env.registry.discover(env.me)
    names = [item["name"] for item in discovered]
    assert names == sorted(names)
    assert set(names) == {"create_ticket", "list_tickets", "read_slow_tool",
                          "read_ticket", "update_ticket", "read_wait_tool"}
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
        call(env, "read_slow_tool", {}, timeout_seconds=0.05)
    assert (exc.value.status, exc.value.code) == (504, "TOOL_TIMEOUT")
    rows = env.service.db.all("SELECT * FROM openclaw_connector_calls WHERE tool_name='read_slow_tool'")
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
        task = asyncio.create_task(registry.call(env.me, "read_wait_tool", {}))
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
        {"name": "read_large_tool", "description": "large", "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True}},
        {"name": "read_fail_tool", "description": "fail", "inputSchema": {"type": "object", "properties": {}},
         "annotations": {"readOnlyHint": True}},
    ]
    register(env, name="failing", tools=failing_tools, allowed=["read_large_tool", "read_fail_tool"])
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_large_tool", {})
    assert (exc.value.status, exc.value.code) == (502, "TOOL_OUTPUT_TOO_LARGE")
    with pytest.raises(ConnectorError) as exc:
        call(env, "read_fail_tool", {})
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


# --------------------------------------- readOnlyHint cannot widen risk (F1)
def _declared(name: str, *, read_only: bool | None = None,
              destructive: bool | None = None) -> dict:
    annotations: dict = {}
    if read_only is not None:
        annotations["readOnlyHint"] = read_only
    if destructive is not None:
        annotations["destructiveHint"] = destructive
    return {"name": name, "description": name,
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": annotations}


def test_read_only_hint_cannot_bypass_write_or_unknown_classification(env):
    tools = [
        _declared("write_file", read_only=True),
        _declared("send_email", read_only=True),
        _declared("create_user", read_only=True),
        _declared("update_ticket", read_only=True),
        _declared("mystery_readonly", read_only=True),
        _declared("delete_ticket", read_only=True),
        _declared("read_ticket", read_only=True),
        _declared("read_unverified"),
        _declared("getUser", read_only=True),
        _declared("thread_listener", read_only=True),
    ]
    allowed = [item["name"] for item in tools]
    result = register(env, name="classification", tools=tools, allowed=allowed)
    assert set(result["denied_tools"]) == {"mystery_readonly", "delete_ticket", "thread_listener"}

    discovered = {item["name"]: item["annotations"] for item in env.registry.discover(env.me)}
    for name in ("write_file", "send_email", "create_user", "update_ticket"):
        assert discovered[name]["risk"] == "medium", name
        assert discovered[name]["requiresApproval"] is True, name
    assert discovered["read_unverified"]["risk"] == "medium"
    assert discovered["read_unverified"]["requiresApproval"] is True
    assert discovered["read_ticket"]["risk"] == "low"
    assert discovered["read_ticket"]["requiresApproval"] is False
    assert discovered["getUser"]["risk"] == "low"
    assert "mystery_readonly" not in discovered
    assert "delete_ticket" not in discovered
    assert "thread_listener" not in discovered

    with pytest.raises(ConnectorError) as exc:
        call(env, "write_file", {})
    assert (exc.value.status, exc.value.code) == (403, "APPROVAL_REQUIRED")
    for name in ("mystery_readonly", "delete_ticket", "thread_listener"):
        with pytest.raises(ConnectorError) as exc:
            call(env, name, {})
        assert (exc.value.status, exc.value.code) == (403, "TOOL_DENIED")
    result = call(env, "read_ticket", {"id": "T-1"})
    assert (result.risk, result.grant) == ("low", "auto")


# ------------------------------------ lease ownership on multi-instance start (F3)
def test_second_registry_startup_does_not_recover_another_instances_live_call(env):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        class Blocking:
            async def call(self, server, tool, arguments, cancelled, request_id):
                started.set()
                await release.wait()
                return {"ok": True}

        first = ConnectorRegistry(env.service.db, executor=Blocking(), timeout_seconds=5,
                                  lease_seconds=60)
        first.register_server(env.other, "live", TOOLS, ALLOWED)
        task = asyncio.create_task(first.call(env.other, "read_ticket", {"id": "T-1"}))
        await asyncio.wait_for(started.wait(), 2)
        request_id = next(iter(first._active))
        row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?",
                                 (request_id,))
        assert row["status"] == "running"
        assert row["owner_instance"] == first.instance_id

        second = ConnectorRegistry(env.service.db, executor=Executor(), lease_seconds=60)
        assert second.recover_interrupted() == 0
        row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?",
                                 (request_id,))
        assert row["status"] == "running"
        assert not any(event["action"] == "tool.interrupted"
                       for event in second.audit_events(env.other))

        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.risk == "low"
        row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?",
                                 (request_id,))
        assert row["status"] == "succeeded"
        assert row["lease_expires_at"] is None
        assert not first._renew_call_lease(request_id)
        assert not first._finish_call(request_id, "failed", 502, "LATE_ERROR", None, None)
        assert env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,)) == row

    asyncio.run(scenario())


def test_lease_expiry_and_legacy_rows_drive_recovery(env, lease_clock):
    registry = ConnectorRegistry(env.service.db, executor=Executor())
    past = lease_clock.now()  # Equality is already expired, not a live lease.
    future = lease_clock.deadline(300)
    expired_id, live_id, legacy_id = uid(), uid(), uid()
    env.service.db.execute(
        "INSERT INTO openclaw_connector_calls(request_id,tenant_id,principal_id,tool_name,status,owner_instance,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (expired_id, env.me.tenant_id, env.me.principal_id, "read_ticket", "running",
         "dead-instance", past, now()),
    )
    env.service.db.execute(
        "INSERT INTO openclaw_connector_calls(request_id,tenant_id,principal_id,tool_name,status,owner_instance,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (live_id, env.me.tenant_id, env.me.principal_id, "read_ticket", "running",
         "live-instance", future, now()),
    )
    env.service.db.execute(
        "INSERT INTO openclaw_connector_calls(request_id,tenant_id,principal_id,tool_name,status,created_at) VALUES(?,?,?,?,?,?)",
        (legacy_id, env.me.tenant_id, env.me.principal_id, "read_ticket", "running", now()),
    )
    assert registry.recover_interrupted() == 2
    for request_id in (expired_id, legacy_id):
        row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?",
                                 (request_id,))
        assert row["status"] == "failed"
        assert row["http_status"] == 503
        assert json.loads(row["error_json"])["code"] == "INTERRUPTED"
        assert row["lease_expires_at"] is None
    live = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?",
                              (live_id,))
    assert live["status"] == "running"
    assert live["lease_expires_at"] == future
    assert registry.recover_interrupted() == 0


def test_heartbeat_renews_live_lease_before_second_registry_start(env, lease_clock, monkeypatch):

    async def heartbeat_case():
        started = asyncio.Event()
        release = asyncio.Event()

        class Blocking:
            async def call(self, server, tool, arguments, cancelled, request_id):
                started.set()
                await release.wait()
                return {"ok": True}

        active = ConnectorRegistry(env.service.db, executor=Blocking(), timeout_seconds=5,
                                   lease_seconds=0.3)
        active.register_server(env.other, "heartbeat", TOOLS, ALLOWED)
        task = asyncio.create_task(active.call(env.other, "read_ticket", {"id": "T-1"}))
        await asyncio.wait_for(started.wait(), 2)
        request_id = next(iter(active._active))
        first_lease = env.service.db.one(
            "SELECT lease_expires_at FROM openclaw_connector_calls WHERE request_id=?",
            (request_id,),
        )["lease_expires_at"]
        heartbeat_completed = asyncio.Event()
        renew = active._renew_call_lease

        def observed_renew(call_id):
            renewed = renew(call_id)
            heartbeat_completed.set()
            return renewed

        monkeypatch.setattr(active, "_renew_call_lease", observed_renew)
        lease_clock.advance(0.2)
        # Await the real heartbeat, but let the controlled clock define expiry.
        await asyncio.wait_for(heartbeat_completed.wait(), 2)
        second_lease = env.service.db.one(
            "SELECT lease_expires_at FROM openclaw_connector_calls WHERE request_id=?",
            (request_id,),
        )["lease_expires_at"]
        assert second_lease > first_lease
        second = ConnectorRegistry(env.service.db, lease_seconds=60)
        row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?",
                                 (request_id,))
        assert row["status"] == "running"
        release.set()
        await asyncio.wait_for(task, 2)
        row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?",
                                 (request_id,))
        assert row["status"] == "succeeded"

    asyncio.run(heartbeat_case())


@pytest.mark.parametrize("ownership", ["recovered", "expired", "changed"])
@pytest.mark.parametrize("late_error", [None, "mapped", "generic"])
def test_late_call_result_cannot_revive_or_overwrite_lost_lease(env, lease_clock, ownership, late_error):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        class Blocking:
            async def call(self, server, tool, arguments, cancelled, request_id):
                started.set()
                await release.wait()
                if late_error == "mapped":
                    raise ConnectorError(429, "RATE_LIMITED", "synthetic late error")
                if late_error == "generic":
                    raise RuntimeError("synthetic late error")
                return {"late": True}

        first = ConnectorRegistry(env.service.db, executor=Blocking(), lease_seconds=60)
        first.register_server(env.me, "late", TOOLS, ALLOWED)
        second = ConnectorRegistry(env.service.db)
        task = asyncio.create_task(first.call(env.me, "read_ticket", {"id": "T-1"}))
        await asyncio.wait_for(started.wait(), 2)
        request_id = next(iter(first._active))
        if ownership == "changed":
            env.service.db.execute(
                "UPDATE openclaw_connector_calls SET owner_instance=? WHERE request_id=?",
                (second.instance_id, request_id),
            )
        else:
            lease_clock.advance(60)
            if ownership == "recovered":
                assert second.recover_interrupted() == 1
        before = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
        assert not first._renew_call_lease(request_id)
        release.set()
        with pytest.raises(ConnectorError) as exc:
            await asyncio.wait_for(task, 2)
        if ownership != "expired" or not late_error:
            assert error(exc.value) == (503, "INTERRUPTED")
        after = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
        if ownership == "expired":
            assert after["status"] == "failed"
            assert json.loads(after["error_json"])["code"] == exc.value.code
        else:
            assert after == before
        assert not first._renew_call_lease(request_id)
        assert not first._finish_call(request_id, "failed", 502, "LATE_ERROR", None, None)
        assert env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,)) == after
        assert not any(event["action"] == "tool.succeeded" for event in first.audit_events(env.me))
        if ownership != "expired":
            events = first.audit_events(env.me)
            assert any(event["action"] == "tool.result_discarded" for event in events)
            assert not any(event["action"] == "tool.failed" for event in events)

    asyncio.run(scenario())


def test_heartbeat_stops_executor_when_lease_expires(env, lease_clock):
    async def scenario():
        started, stopped = asyncio.Event(), asyncio.Event()

        class Blocking:
            async def call(self, server, tool, arguments, cancelled, request_id):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    stopped.set()

        registry = ConnectorRegistry(env.service.db, executor=Blocking(), lease_seconds=0.3)
        registry.register_server(env.me, "expires", TOOLS, ALLOWED)
        task = asyncio.create_task(registry.call(env.me, "read_ticket", {"id": "T-1"}))
        await asyncio.wait_for(started.wait(), 2)
        lease_clock.advance(0.3)
        with pytest.raises(ConnectorError) as exc:
            await asyncio.wait_for(task, 2)
        assert error(exc.value) == (503, "INTERRUPTED")
        assert stopped.is_set()

    asyncio.run(scenario())


def test_recovery_rechecks_lease_after_scan(env, lease_clock, monkeypatch):
    request_id = uid()
    env.service.db.execute(
        "INSERT INTO openclaw_connector_calls(request_id,tenant_id,principal_id,tool_name,status,owner_instance,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (request_id, env.me.tenant_id, env.me.principal_id, "read_ticket", "running",
         env.registry.instance_id, lease_clock.now(), lease_clock.now()),
    )
    query = env.service.db.all

    def scan_then_renew(sql, values=()):
        rows = query(sql, values)
        if sql.startswith("SELECT * FROM openclaw_connector_calls WHERE status='running'"):
            # Simulate a concurrent renewal committed after the recovery scan.
            env.service.db.execute(
                "UPDATE openclaw_connector_calls SET lease_expires_at=? WHERE request_id=?",
                (lease_clock.deadline(60), request_id),
            )
        return rows

    monkeypatch.setattr(env.service.db, "all", scan_then_renew)
    assert env.registry.recover_interrupted() == 0
    row = env.service.db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
    assert row["status"] == "running"
    assert row["lease_expires_at"] == lease_clock.deadline(60)


@pytest.mark.parametrize("operation", ["renew", "succeed"])
def test_call_lease_check_uses_clock_after_waiting_for_write_lock(env, lease_clock, monkeypatch, operation):
    request_id = uid()
    db = env.service.db
    db.execute(
        "INSERT INTO openclaw_connector_calls(request_id,tenant_id,principal_id,tool_name,status,owner_instance,lease_expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (request_id, env.me.tenant_id, env.me.principal_id, "read_ticket", "running",
         env.registry.instance_id, lease_clock.deadline(60), lease_clock.now()),
    )
    attempting_write = threading.Event()
    execute, transaction = db.execute, db.transaction

    def observed_execute(sql, values=()):
        if sql.startswith("UPDATE openclaw_connector_calls"):
            attempting_write.set()
        return execute(sql, values)

    @contextmanager
    def observed_transaction():
        attempting_write.set()
        with transaction():
            yield

    monkeypatch.setattr(db, "execute", observed_execute)
    monkeypatch.setattr(db, "transaction", observed_transaction)
    action = (lambda: env.registry._renew_call_lease(request_id)) if operation == "renew" else (
        lambda: env.registry._finish_call(request_id, "succeeded", 200, None, "digest", 1))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with db.lock:
            pending = pool.submit(action)
            assert attempting_write.wait(2)
            lease_clock.advance(60)
        assert pending.result(timeout=2) is False
    row = db.one("SELECT * FROM openclaw_connector_calls WHERE request_id=?", (request_id,))
    assert row["status"] == "running"
    assert row["lease_expires_at"] == lease_clock.now()
