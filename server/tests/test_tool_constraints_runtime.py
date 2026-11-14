"""Offline replay of application callbacks and the actual Harness tool bridge.

SDKRuntime.connect creates the production PreToolUse/permission callbacks; a
capturing SDK shell replaces the CLI transport. Harness invokes the real bridge
with a synthetic terminal business handler. No provider or MCP network is used,
and these tests do not prove SDK event delivery or remote schema rediscovery.
"""

import asyncio
import copy
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.harness_runtime import HarnessRuntime
from app.mcp_tools import external_tool_name
from app.runtime import SDKRuntime
from test_tool_constraints import VALID, env, prepare  # noqa: F401 - shared fixture


INVALID = [
    {**VALID, "limit": 99},
    {**VALID, "fields": ["id", "private_cost"]},
    {key: value for key, value in VALID.items() if key != "factory"},
]


def capture_sdk(monkeypatch):
    captured = {}
    sdk, types = ModuleType("claude_agent_sdk"), ModuleType("claude_agent_sdk.types")

    class Client:
        def __init__(self, *, options):
            captured["options"] = options

        async def connect(self):
            captured["connected"] = True

        async def disconnect(self):
            pass

    class PermissionResultAllow(SimpleNamespace):
        behavior = "allow"

    class PermissionResultDeny(SimpleNamespace):
        behavior = "deny"

    sdk.ClaudeAgentOptions = SimpleNamespace
    sdk.HookMatcher = SimpleNamespace
    sdk.ClaudeSDKClient = Client
    types.PermissionResultAllow = PermissionResultAllow
    types.PermissionResultDeny = PermissionResultDeny
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", types)
    monkeypatch.setattr("app.capabilities.create_workspace_tools", lambda runtime: None)
    return captured


@pytest.mark.parametrize("invalid", INVALID, ids=["excess-count", "unapproved-field", "missing-scope"])
def test_real_sdk_pretool_and_permission_callbacks_deny_out_of_scope(env, monkeypatch, invalid):
    env.service.db.execute("UPDATE conversations SET runtime_type='claude' WHERE id=?", (env.conversation["id"],))
    prepared = prepare(env)
    captured = capture_sdk(monkeypatch)
    events, approvals = [], []

    async def emit(kind, data):
        events.append((kind, data))

    async def approve(*args, **kwargs):
        approvals.append((args, kwargs))
        return True  # Human approval must not rescue a scope violation.

    async def scenario():
        config = env.service.runtime_config(env.conversation["id"])
        # Only the capturing SDK shell receives this reserved fake route;
        # persisted provider selection and real authorization stay unchanged.
        config["provider_env"] = {
            "ANTHROPIC_API_KEY": "synthetic-sdk-callback-key",
            "ANTHROPIC_BASE_URL": "https://gateway.example",
        }
        runtime = SDKRuntime(env.service, env.conversation["id"], config, approve, emit)
        runtime.current_run = env.service.require("runs", prepared["run_id"])
        runtime.current_run_id = prepared["run_id"]
        await runtime.connect()
        options = captured["options"]
        hook = options.hooks["PreToolUse"][0]
        assert hook.on_failure == "block"
        valid = await hook.hooks[0]({"tool_name": env.tool, "tool_input": copy.deepcopy(VALID)}, "valid", {})
        assert valid["hookSpecificOutput"]["permissionDecision"] == "allow"
        before = len(events)
        denied = await hook.hooks[0]({"tool_name": env.tool, "tool_input": copy.deepcopy(invalid)}, "invalid", {})
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert len(events) == before, "Rejected scope must not emit a tool-started event"
        permission = await options.can_use_tool(env.tool, copy.deepcopy(invalid), {})
        assert permission.behavior == "deny"
        assert approvals == []
        await runtime.close()

    asyncio.run(scenario())
    assert captured["connected"] is True
    assert [data["arguments"] for kind, data in events if kind == "tool.started"] == [VALID]


@pytest.mark.parametrize("invalid", INVALID, ids=["excess-count", "unapproved-field", "missing-scope"])
def test_actual_harness_bridge_refuses_scope_before_terminal_handler(env, monkeypatch, invalid):
    prepared = prepare(env)
    env.service.db.execute("UPDATE runs SET status='running' WHERE id=?", (prepared["run_id"],))
    calls, events, approvals = [], [], []

    async def emit(kind, data):
        events.append((kind, data))

    async def approve(*args, **kwargs):
        approvals.append((args, kwargs))
        return True

    runtime = HarnessRuntime(env.service, env.conversation["id"], env.service.runtime_config(env.conversation["id"]), approve, emit)
    runtime.current_run = env.service.require("runs", prepared["run_id"])
    name = external_tool_name(env.tool)
    business = runtime.bridge.business_tools[name]

    async def terminal_handler(arguments, recheck):
        recheck()
        calls.append(copy.deepcopy(arguments))
        return {"content": [{"type": "text", "text": "Synthetic authorized result"}]}

    monkeypatch.setattr(business, "invoke", terminal_handler)

    def request(call_id, arguments):
        return {"schema": "workspace-tool/v1", "sessionId": runtime.session_id,
                "callId": call_id, "toolName": name, "arguments": copy.deepcopy(arguments)}

    async def scenario():
        denied = await runtime.bridge.invoke(request("invalid", invalid))
        assert denied["is_error"] is True
        assert calls == [] and approvals == []
        assert not [item for item in events if item[0] == "tool.started"]
        failed = [data for kind, data in events if kind == "tool.completed"]
        assert len(failed) == 1 and failed[0]["success"] is False
        assert failed[0]["source_ids"] == []
        accepted = await runtime.bridge.invoke(request("valid", VALID))
        assert not accepted.get("is_error")
        assert calls == [VALID] and approvals == []

    asyncio.run(scenario())
