"""Run failures retain public error codes through the actor and terminal event."""

import json

import pytest

from app.security import AppError
from test_api import FakeRuntime, client, configured, conversation, poll, send


@pytest.mark.parametrize("phase,structured", [
    ("connect", True),
    ("execute", True),
    ("execute", False),
])
def test_actor_failure_preserves_error_code_and_retry_clears_it(client, monkeypatch, phase, structured):
    """Exercise submission, runtime failure, durable settlement and same-chat retry."""
    original = getattr(FakeRuntime, phase)
    failed_once = False
    expected_code = "HARNESS_TURN_FAILED" if structured else None
    expected_message = "本次任务未完成，请重试。" if structured else "执行服务暂时不可用，请检查服务配置或稍后重试"

    async def fail_once(runtime, *args):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            if structured:
                raise AppError(502, expected_code, expected_message)
            raise RuntimeError("private-upstream-diagnostic-must-not-be-exposed")
        return await original(runtime, *args)

    monkeypatch.setattr(FakeRuntime, phase, fail_once)
    cid = conversation(client)["id"]
    submitted = send(client, cid, text="查询业务记录", request_id="error-reporting-first")
    failed = poll(client, submitted["run_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == expected_code
    assert failed["error"] == expected_message

    service = client.app.state.service
    stored = service.require("runs", failed["id"])
    assert stored["error_code"] == expected_code
    events = service.db.events(failed["id"], 0)
    terminals = [event for event in events if event["type"] == "run.completed"]
    assert len(terminals) == 1
    terminal = terminals[0]["data"]
    assert terminal["status"] == "failed"
    assert terminal["error_code"] == expected_code
    assert terminal["error"] == expected_message
    assert "private-upstream-diagnostic" not in json.dumps({"run": failed, "events": events})

    retried = send(client, cid, text="查询业务记录", request_id="error-reporting-retry")
    succeeded = poll(client, retried["run_id"])
    assert succeeded["status"] == "succeeded"
    assert succeeded["error_code"] is None
    assert succeeded["error"] is None
    retry_events = service.db.events(succeeded["id"], 0)
    retry_terminal = [event["data"] for event in retry_events if event["type"] == "run.completed"]
    assert len(retry_terminal) == 1
    assert retry_terminal[0]["error_code"] is None
