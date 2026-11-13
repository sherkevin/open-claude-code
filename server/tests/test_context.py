import asyncio
import time

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.db import dump
from app.context import context_snapshot


@pytest.fixture
def native_context(tmp_path, monkeypatch):
    state = {"connects": 0, "prompts": [], "counts": 0, "boundary": True, "count_error": False, "tasks": []}

    class Client:
        def __init__(self, options):
            self.prompt = None
            state["options"] = options
        async def connect(self):
            state["connects"] += 1
            state["tasks"].append(asyncio.current_task())
        async def query(self, prompt):
            if not isinstance(prompt, str):
                messages = [message async for message in prompt]
                assert messages[0]["client_composed"] is True
                prompt = messages[0]["message"]["content"]
            self.prompt = prompt
            state["prompts"].append(prompt)
            state["tasks"].append(asyncio.current_task())
        async def receive_response(self):
            if self.prompt.startswith("/compact"):
                if state["boundary"]:
                    yield SystemMessage("compact_boundary", {"compact_metadata": {"trigger": "manual", "pre_tokens": 100}})
            else:
                yield AssistantMessage([TextBlock("native answer")], "fixture")
            yield ResultMessage("success", 10, 5, False, 1, "native-session", usage={"input_tokens": 7, "output_tokens": 2})
        async def get_context_usage(self):
            state["counts"] += 1
            state["tasks"].append(asyncio.current_task())
            if state["count_error"]:
                raise RuntimeError("must-not-expose-provider-details")
            return {"totalTokens": 100, "maxTokens": 1000, "percentage": 10, "isAutoCompactEnabled": True, "autoCompactThreshold": 900, "model": "fixture", "memoryFiles": [{"path": "/private/should-not-expose"}]}
        async def interrupt(self):
            pass
        async def disconnect(self):
            pass

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)
    settings = Settings(data_dir=tmp_path, model="fixture", provider_settings="", runtime_type="claude")
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "test-only"}
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "admin"})
        assistant = service.save_assistant({"name": "Context", "connections": []})
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Context"}, user)
        client.post("/api/v1/auth/login", json={"username": "owner", "password": "test-password-123"})
        yield client, service, conversation["id"], state


def wait_run(client, run_id):
    for _ in range(200):
        row = client.get(f"/api/v1/runs/{run_id}").json()
        if row["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return row
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def start_chat(client, cid):
    response = client.post(f"/api/v1/conversations/{cid}/messages", json={"text": "hello", "file_ids": [], "client_request_id": "context-seed"})
    assert response.status_code == 202
    row = wait_run(client, response.json()["run_id"])
    assert row["status"] == "succeeded"
    return row


def test_context_is_native_manual_cached_and_owned(native_context):
    client, service, cid, state = native_context
    url = f"/api/v1/conversations/{cid}/context"
    empty = client.get(url).json()
    assert empty["status"] == "not_measured" and empty["total_tokens"] is None
    assert client.post(url + "/refresh").json()["status"] == "unavailable"
    assert state["connects"] == state["counts"] == 0
    start_chat(client, cid)
    service.db.execute("UPDATE runs SET usage=?", (dump({"input_tokens": 9999999, "output_tokens": 9999999}),))
    measured = client.post(url + "/refresh").json()
    assert measured["source"] == "native_runtime" and measured["total_tokens"] == 100 and measured["max_tokens"] == 1000
    assert measured["percentage"] == 10 and measured["auto_compact_enabled"] is True
    assert "private" not in str(measured)
    assert client.post(url + "/refresh").json()["cached"] is True
    assert client.get(url).json()["cached"] is True
    assert state["counts"] == 1 and state["connects"] == 1 and state["prompts"] == ["hello"]
    assert len({id(task) for task in state["tasks"]}) == 1
    other = service.create_user({"username": "other", "display_name": "Other", "password": "test-password-123", "role": "member"})
    client.post("/api/v1/auth/login", json={"username": "other", "password": "test-password-123"})
    assert client.get(url).status_code == 404
    assert client.post(url + "/refresh").status_code == 404


@pytest.mark.parametrize("boundary", [True, False])
def test_compaction_requires_real_boundary_and_preserves_native_session(native_context, boundary):
    client, service, cid, state = native_context
    start_chat(client, cid)
    state["boundary"] = boundary
    response = client.post(f"/api/v1/conversations/{cid}/compact", json={"client_request_id": "compact-request", "instructions": "保留业务编号"})
    assert response.status_code == 202
    run = wait_run(client, response.json()["run_id"])
    assert run["status"] == "succeeded" and run["kind"] == "compact"
    assert run["compaction"]["status"] == ("completed" if boundary else "not_needed")
    assert state["prompts"] == ["hello", "/compact 保留业务编号"]
    assert state["connects"] == 1
    assert service.require("conversations", cid)["sdk_session_id"] == "native-session"
    events = service.db.events(run["id"], 0)
    assert any(item["type"] == "context.compacted" for item in events) is boundary
    latest = client.get(f"/api/v1/conversations/{cid}/context").json()["last_compaction"]
    assert latest["run_id"] == run["id"] and latest["status"] == run["compaction"]["status"]
    retry = client.post(f"/api/v1/conversations/{cid}/compact", json={"client_request_id": "compact-request"})
    assert retry.json()["run_id"] == run["id"] and len(state["prompts"]) == 2


def test_context_failure_is_unknown_and_does_not_fail_compaction(native_context):
    client, _, cid, state = native_context
    assert client.post(f"/api/v1/conversations/{cid}/compact", json={"client_request_id": "empty"}).status_code == 409
    start_chat(client, cid)
    state["count_error"] = True
    value = client.post(f"/api/v1/conversations/{cid}/context/refresh").json()
    assert value["status"] == "unavailable" and value["total_tokens"] is None
    assert "must-not-expose" not in str(value)
    response = client.post(f"/api/v1/conversations/{cid}/compact", json={"client_request_id": "compact-unavailable-count"})
    run = wait_run(client, response.json()["run_id"])
    assert run["status"] == "succeeded" and run["compaction"]["status"] == "completed"
    assert run["compaction"]["after"]["total_tokens"] is None


def test_partial_context_is_unavailable_without_inventing_percentage():
    value = context_snapshot({"totalTokens": 100, "maxTokens": 1000})
    assert value["status"] == "unavailable" and value["total_tokens"] == 100
    assert value["percentage"] is None


def test_memory_edit_invalidates_context_and_reloads_native_session(native_context):
    client, service, cid, state = native_context
    start_chat(client, cid)
    assert client.post(f"/api/v1/conversations/{cid}/context/refresh").json()["status"] == "available"
    conversation = service.require("conversations", cid)
    url = f"/api/v1/conversations/{cid}/memory"
    revision = client.get(url).json()["revision"]
    assert client.put(url + "/file", json={"path": "MEMORY.md", "content": "Native memory edit", "revision": revision}).status_code == 200
    assert client.get(f"/api/v1/conversations/{cid}/context").json()["status"] == "not_measured"
    response = client.post(f"/api/v1/conversations/{cid}/messages", json={"text": "continue", "file_ids": [], "client_request_id": "after-memory-edit"})
    assert wait_run(client, response.json()["run_id"])["status"] == "succeeded"
    assert state["connects"] == 2 and state["options"].resume == "native-session"


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("boundary_observed", [False, True])
def test_compaction_terminal_failures_are_persisted(native_context, status, boundary_observed):
    client, service, cid, _ = native_context
    conversation = service.require("conversations", cid)
    user = service.require("users", conversation["user_id"])
    value, _ = service.prepare_run(cid, {"text": "/compact", "file_ids": [], "client_request_id": "compact-terminal"}, user)
    service.db.execute("UPDATE runs SET kind='compact',compaction=? WHERE id=?", (dump({"status": "started", "trigger": "manual"}), value["run_id"]))
    if boundary_observed:
        service.db.event(value["run_id"], "context.compacted", {"trigger": "manual", "pre_tokens": 100})
    if status == "interrupted":
        service.manager.recover()
    else:
        service.manager.finish(value["run_id"], status, "Safe failure" if status == "failed" else None)
    run = client.get(f"/api/v1/runs/{value['run_id']}").json()
    assert run["status"] == status
    expected = "completed" if boundary_observed else status
    assert run["compaction"]["status"] == expected
    if boundary_observed:
        assert run["compaction"]["followup_status"] == status
        assert run["compaction"]["pre_tokens"] == 100
    latest = client.get(f"/api/v1/conversations/{cid}/context").json()["last_compaction"]
    assert latest["status"] == expected
