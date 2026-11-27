"""Cancellation/revocation at host event and final RPC boundaries."""
import asyncio
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.harness_runtime import HarnessRuntime
from app.security import AppError
from app.runtime import SessionActor
from test_tool_constraints import env, prepare, VALID


@pytest.fixture
def captured_client():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], stdin=subprocess.PIPE, text=True)
    received = []
    client = SimpleNamespace(_proc=process, respond=lambda identity, value: received.append(value))
    try:
        yield client, received
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        process.stdin.close()


def configured(env, mutate=None):
    prepared = prepare(env)
    service, run_id = env.service, prepared["run_id"]
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
    actor = SessionActor.__new__(SessionActor)
    actor.service = service
    async def emit(kind, data):
        if mutate:
            mutate(kind, data, service, run_id)
        actor._emit(run_id, kind, data)
    async def approve(tool_name, arguments, **_options):
        assert tool_name == env.tool and arguments == VALID
        return True

    runtime = HarnessRuntime(service, env.conversation["id"], service.runtime_config(env.conversation["id"]), approve, emit)
    actor.runtime = runtime
    runtime.current_run = service.require("runs", run_id)
    alias, tool = next(iter(runtime.bridge.business_tools.items()))
    async def response(_arguments, _recheck):
        return {"content": [{"type": "text", "text": "synthetic-business-data"}]}
    tool.invoke = response
    payload = {"schema": "workspace-tool/v1", "sessionId": runtime.session_id, "callId": "handoff-test",
               "toolName": alias, "arguments": dict(VALID)}
    return runtime, payload, run_id


@pytest.mark.parametrize("boundary", ["evidence", "completed"])
@pytest.mark.parametrize("mutation", ["cancel", "revoke", "old_run"])
def test_changed_scope_during_completion_never_reaches_model(env, boundary, mutation):
    def change(kind, data, service, run_id):
        matched = kind == "tool.completed" if boundary == "completed" else kind == "run.stage" and data.get("stage") == "evidence"
        if matched:
            if mutation == "cancel":
                service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run_id,))
            elif mutation == "revoke":
                service.db.execute("UPDATE connections SET enabled=0 WHERE id=?", (env.connection["id"],))
            else:
                service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (env.conversation["id"],))
    runtime, payload, run_id = configured(env, change)
    result = asyncio.run(runtime.bridge.invoke(payload))
    assert result.get("is_error") is True
    assert "synthetic-business-data" not in str(result)
    events = env.service.db.events(run_id, 0)
    assert not any(event["type"] == "tool.completed" and event["data"].get("success") for event in events)
    assert any(event["type"] == "tool.discarded" or event["type"] == "tool.completed" and event["data"].get("success") is False for event in events)
    assert not any("_tool_result_guard" in event["data"] for event in events)
    assert not any("_expected_tool_call_id" in event["data"] for event in events)


@pytest.mark.parametrize("mutation", ["cancel", "revoke", "old_run"])
def test_final_rpc_rechecks_after_bridge_has_returned(env, mutation, captured_client):
    runtime, payload, run_id = configured(env)
    result = asyncio.run(runtime.bridge.invoke(payload))
    assert "synthetic-business-data" in str(result)
    if mutation == "cancel":
        env.service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (run_id,))
    elif mutation == "revoke":
        env.service.db.execute("UPDATE connections SET enabled=0 WHERE id=?", (env.connection["id"],))
    else:
        runtime.current_run = None
    client, received = captured_client
    runtime.bridge.respond(client, "rpc-1", payload, result, run_id)
    if mutation == "cancel":
        assert received == []
    else:
        assert received[0].get("is_error") is True
    assert "synthetic-business-data" not in str(received)
    assert not runtime.bridge.response_guards


def test_successful_final_rpc_preserves_result_and_cleans_guard(env, captured_client):
    runtime, payload, run_id = configured(env)
    result = asyncio.run(runtime.bridge.invoke(payload))
    client, received = captured_client
    runtime.bridge.respond(client, "rpc-1", payload, result, run_id)
    assert received == [result]
    assert not runtime.bridge.response_guards


@pytest.mark.parametrize("blocked_by", ["stdin", "writer_lock"])
def test_nonreading_child_cannot_hold_the_database_lock_indefinitely(env, monkeypatch, blocked_by):
    import app.harness_tools as harness_tools
    monkeypatch.setattr(harness_tools, "RESULT_DELIVERY_TIMEOUT_SECONDS", .1)
    runtime, payload, run_id = configured(env)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], stdin=subprocess.PIPE, text=True)
    writer_lock, writer_entered = threading.Lock(), threading.Event()
    def write(_identity, value):
        with writer_lock:
            writer_entered.set()
            process.stdin.write(json.dumps(value) + "\n")
            process.stdin.flush()
    writer = None
    if blocked_by == "writer_lock":
        def occupy_writer():
            try:
                write("earlier-rpc", "x" * 262144)
            except (BrokenPipeError, OSError):
                pass
        writer = threading.Thread(target=occupy_writer)
        writer.start()
        assert writer_entered.wait(1)
    client = SimpleNamespace(_proc=process, respond=write)
    started = time.monotonic()
    try:
        with pytest.raises(AppError) as error:
            runtime.bridge.respond(client, "blocked-rpc", payload, {"content": [{"type": "text", "text": "x" * 262144}]}, run_id)
        assert error.value.code == "HARNESS_RESULT_DELIVERY_TIMEOUT"
        process.wait(timeout=2)
        if writer:
            writer.join(1)
            assert not writer.is_alive()
        assert time.monotonic() - started < 3
        acquired = []
        def check_db():
            with env.service.db.lock:
                acquired.append(True)
        checker = threading.Thread(target=check_db)
        checker.start()
        checker.join(1)
        assert acquired == [True]
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        if writer:
            writer.join(1)
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass


@pytest.mark.parametrize("small_budget", [False, True])
def test_display_receipt_preserves_rows_and_counts_toward_model_budget(env, monkeypatch, small_budget):
    import copy
    import app.mcp_tools as mcp_tools
    runtime, payload, run_id = configured(env)
    original = {"content": [{"type": "text", "text": '{"rows":[{"id":1}]}'}],
                "structuredContent": {"rows": [{"id": 1}]}}
    async def response(_arguments, _recheck):
        return copy.deepcopy(original)
    next(iter(runtime.bridge.business_tools.values())).invoke = response
    if small_budget:
        monkeypatch.setattr(mcp_tools, "MAX_MODEL_RESULT_BYTES", 200)
    result = asyncio.run(runtime.bridge.invoke(payload))
    event = next(event["data"] for event in env.service.db.events(run_id, 0) if event["type"] == "tool.completed")
    if small_budget:
        assert result.get("is_error") is True and event["error_code"] == "MCP_RESULT_TOO_LARGE"
        assert "business_result" not in event
    else:
        assert result["structuredContent"] == original["structuredContent"]
        assert "工作台显示回执" in result["content"][-1]["text"]
        assert event["model_input_bytes"] == len("\n".join(block["text"] for block in result["content"]).encode("utf-8"))
        assert event["business_result"]["rows"] == [{"id": 1}]
