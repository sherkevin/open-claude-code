"""Real SDKRuntime callbacks with a captured CLI boundary, not a provider run."""
import asyncio
import json

import pytest

from app.db import dump
from app.runtime import SDKRuntime, SessionActor
from test_business_results import envelope, payload
from test_tool_constraints import VALID, env, prepare  # noqa: F401
from test_tool_constraints_runtime import capture_sdk


def exercise(env, monkeypatch, response, *, failure=False, revoke=False, terminal=None, replaced=False):
    env.service.db.execute("UPDATE conversations SET runtime_type='claude' WHERE id=?", (env.conversation["id"],))
    secret = "synthetic-sdk-response-secret"
    env.service.db.execute("UPDATE connections SET auth_type='bearer',secret=? WHERE id=?",
                           (env.service.secrets.encrypt(secret), env.connection["id"]))
    prepared = prepare(env)
    env.service.db.execute("UPDATE runs SET status='running' WHERE id=?", (prepared["run_id"],))
    captured = capture_sdk(monkeypatch)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def approve(*args, **kwargs):
        raise AssertionError("Post-call handling must not authorize a new operation")

    async def scenario():
        config = env.service.runtime_config(env.conversation["id"])
        config["provider_env"] = {"ANTHROPIC_API_KEY": "synthetic-callback-key", "ANTHROPIC_BASE_URL": "https://gateway.example"}
        runtime = SDKRuntime(env.service, env.conversation["id"], config, approve, emit)
        runtime.current_run = env.service.require("runs", prepared["run_id"])
        runtime.current_run_id = prepared["run_id"]
        await runtime.connect()
        await captured["options"].hooks["PreToolUse"][0].hooks[0](
            {"tool_name": env.tool, "tool_input": VALID}, "sdk-result-test", {})
        if revoke:
            env.service.db.execute("UPDATE connections SET enabled=0 WHERE id=?", (env.connection["id"],))
        if terminal:
            env.service.db.execute("UPDATE runs SET status=? WHERE id=?", (terminal, prepared["run_id"]))
        if replaced:
            runtime.current_run = {"id": "new-run-must-not-receive-old-results"}
        hook = captured["options"].hooks["PostToolUseFailure" if failure else "PostToolUse"][0].hooks[0]
        result = await hook({"tool_name": env.tool, "tool_input": VALID, "tool_response": response,
                             "hook_event_name": "PostToolUseFailure" if failure else "PostToolUse"}, "sdk-result-test", {})
        await runtime.close()
        return result

    output = asyncio.run(scenario())
    completions = [data for kind, data in events if kind == "tool.completed"]
    discarded = [event["data"] for event in env.service.db.events(prepared["run_id"], 0) if event["type"] == "tool.discarded"]
    return output, (discarded or completions)[-1]


def test_sdk_callback_replaces_oversize_output_instead_of_merely_showing_error(env, monkeypatch):
    original = envelope(payload(rows=[{"customer": "x" * 32000, "quantity": 1}]))
    result, event = exercise(env, monkeypatch, original)
    replacement = result["hookSpecificOutput"]["updatedToolOutput"]
    assert "32000" in replacement and "x" * 100 not in replacement
    assert event["success"] is False and event["error_code"] == "MCP_RESULT_TOO_LARGE"
    assert "business_result" not in event and event["source_ids"] == []


def test_sdk_callback_keeps_one_text_copy_and_uses_cleaned_structured_data(env, monkeypatch):
    value = payload(rows=[{"customer": "synthetic-sdk-response-secret", "quantity": 18}])
    body = {"content": [{"type": "text", "text": dump(value)}] * 2, "structuredContent": value}
    result, event = exercise(env, monkeypatch, body)
    replacement = result["hookSpecificOutput"]["updatedToolOutput"]
    assert json.loads(replacement)["rows"][0]["customer"] == "[redacted]"
    assert "synthetic-sdk-response-secret" not in dump(result) + dump(event)
    assert event["success"] is True
    assert event["business_result"]["rows"][0]["quantity"] == 18
    assert event["model_input_bytes"] == len(replacement.encode())


def test_sdk_callback_rechecks_revocation_before_publishing_result(env, monkeypatch):
    result, event = exercise(env, monkeypatch, envelope(payload()), revoke=True)
    assert event["success"] is False and "business_result" not in event
    assert "rows" not in result["hookSpecificOutput"]["updatedToolOutput"]


def test_sdk_failure_hook_does_not_make_a_result_or_claim_output_replacement(env, monkeypatch):
    result, event = exercise(env, monkeypatch, envelope(payload()), failure=True)
    assert result == {} and event["success"] is False
    assert "business_result" not in event


def test_sdk_source_adapter_failure_still_replaces_raw_output(env, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic-sdk-response-secret")
    monkeypatch.setattr(env.service.evidence, "register_mcp", broken)
    result, event = exercise(env, monkeypatch, envelope(payload()))
    assert event["success"] is False and event["error_code"] == "MCP_EVIDENCE_FAILED"
    assert "business_result" not in event
    assert "synthetic-sdk-response-secret" not in dump(result) + dump(event)
    assert "来源登记失败" in result["hookSpecificOutput"]["updatedToolOutput"]


@pytest.mark.parametrize("terminal", ["cancelling", "cancelled", "succeeded", "interrupted"])
def test_sdk_late_results_never_reach_model_or_sources(env, monkeypatch, terminal):
    result, event = exercise(env, monkeypatch, envelope(payload()), terminal=terminal)
    assert event["error_code"] == "MCP_RESULT_STALE" and event["success"] is False
    assert "rows" not in result["hookSpecificOutput"]["updatedToolOutput"]
    assert not env.service.db.all("SELECT * FROM evidence_sources")


def test_sdk_old_run_callback_is_discarded_without_new_run_event(env, monkeypatch):
    result, event = exercise(env, monkeypatch, envelope(payload()), replaced=True)
    assert event["error_code"] == "MCP_RESULT_STALE"
    assert "rows" not in result["hookSpecificOutput"]["updatedToolOutput"]
    assert not env.service.db.all("SELECT * FROM events WHERE run_id='new-run-must-not-receive-old-results'")


def test_sdk_cancellation_during_evidence_await_still_discards_model_output(env, monkeypatch):
    original = env.service.evidence.register_mcp
    def cancelling(run_id, *args):
        result = original(run_id, *args)
        env.service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run_id,))
        return result
    monkeypatch.setattr(env.service.evidence, "register_mcp", cancelling)
    result, event = exercise(env, monkeypatch, envelope(payload()))
    assert event["error_code"] == "MCP_RESULT_STALE"
    assert "rows" not in result["hookSpecificOutput"]["updatedToolOutput"]


def test_actor_commit_guard_rejects_cancel_between_callback_check_and_emit(env):
    prepared = prepare(env)
    run_id = prepared["run_id"]
    env.service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run_id,))
    actor = SessionActor.__new__(SessionActor)
    actor.service = env.service
    actor._emit(run_id, "tool.completed", {"_expected_run_id": run_id, "tool_call_id": "late",
                "tool_name": env.tool, "success": True, "business_result": payload()})
    events = env.service.db.events(run_id, 0)
    assert not any(event["type"] == "tool.completed" for event in events)
    assert events[-1]["type"] == "tool.discarded"
    assert "business_result" not in events[-1]["data"]


def test_sdk_cancel_during_final_permission_check_cannot_return_business_rows(env, monkeypatch):
    original = SDKRuntime.check_tool
    calls = 0
    def cancel_at_final_check(runtime, tool, arguments):
        nonlocal calls
        calls += 1
        permitted = original(runtime, tool, arguments)
        if calls == 4:
            runtime.service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?",
                                       (runtime.current_run["id"],))
        return permitted
    monkeypatch.setattr(SDKRuntime, "check_tool", cancel_at_final_check)
    result, event = exercise(env, monkeypatch, envelope(payload()))
    assert calls == 4
    assert "rows" not in result["hookSpecificOutput"]["updatedToolOutput"]
    assert event["success"] is False and event["error_code"] == "MCP_RESULT_STALE"


@pytest.mark.parametrize("raw", ["hello", [{"type": "text", "text": "hello"}]])
def test_sdk_text_shapes_remain_supported_without_fabricating_a_table(env, monkeypatch, raw):
    result, event = exercise(env, monkeypatch, raw)
    assert result["hookSpecificOutput"]["updatedToolOutput"] == "hello"
    assert event["success"] is True and "business_result" not in event
