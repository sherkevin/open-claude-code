"""Adapter failure and timing contracts; actual Node is tested separately."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.harness_runtime import HarnessRuntime
from app.security import AppError


@pytest.fixture(autouse=True)
def attachment_boundary(monkeypatch):
    # These tests isolate notification handling; authorization/bytes use real
    # SQLite and uploads in test_harness_attachments.py and real Node tests.
    class Prompt:
        def __init__(self, *_args):
            pass

        def recheck(self, **_kwargs):
            pass

        def dispatch(self, client, session, prompt):
            return client.session_prompt(session, [{"type": "text", "text": prompt}])

    monkeypatch.setattr("app.harness_runtime.AttachmentPrompt", Prompt)


def adapter(finish="completed", finish_observed=True):
    emitted, methods = [], []

    def notification(method, payload):
        return SimpleNamespace(method=method, payload={"sessionId": "owned", **payload})

    frames = iter([
        notification("session.event", {"event": {"type": "agent/inbox/spliced", "data": {"inserted": [{"id": "receipt"}]}}}),
        notification("workspace.stream", {"event": "start", "stepId": "step"}),
        notification("workspace.stream", {"sessionId": "foreign", "event": "text_delta", "stepId": "step", "text": "foreign secret"}),
        notification("workspace.stream", {"event": "text_delta", "stepId": "step", "text": "answer"}),
        notification("session.event", {"event": {"type": "assistant/message", "seq": 2, "data": {"message": {"content": [{"type": "text", "text": "answer"}]}}}}),
        notification("workspace.stream", {"event": "end", "stepId": "step", "finishObserved": finish_observed, "outcome": {"kind": "committed"}}),
        notification("session.event", {"event": {"type": "turn/end", "seq": 3, "data": {"reason": {"kind": finish}}}}),
        notification("session.status", {"status": "idle"}),
    ])

    async def emit(kind, data):
        emitted.append((kind, data))

    class Client:
        def session_prompt(self, session, content):
            methods.append(("session/prompt", session, content))
            return "receipt"

        def next_notification(self):
            return next(frames)

        def request(self, method, params, **kwargs):
            methods.append((method, params))
            return {}

    runtime = object.__new__(HarnessRuntime)
    runtime.service = SimpleNamespace(settings=SimpleNamespace(approval_seconds=600),
        require=lambda *_args: {"created_at": "2026-11-10T00:00:00+00:00", "timings": "{}"},
        db=SimpleNamespace(lock=threading.RLock(), execute=lambda *_args: None, event=lambda *_args: None))
    runtime.conversation_id, runtime.session_id = "conversation", "owned"
    runtime.config = {"fingerprint": "synthetic-transport"}
    runtime.client, runtime.emit, runtime.bridge = Client(), emit, SimpleNamespace(calls=set())
    return runtime, emitted, methods


def execute(runtime):
    return runtime.execute({"id": "run", "assistant_message_id": "answer", "created_at": "2026-11-10T00:00:00+00:00"},
                           {"text": "/permission literal", "file_ids": "[]"})


def test_valid_approval_wait_does_not_expire_at_five_minutes(monkeypatch):
    runtime, emitted, methods = adapter()

    async def virtual_approval_delay(awaitable, timeout):
        # Model an approval that takes 590 seconds without sleeping in a test.
        if timeout < 590:
            awaitable.close()
            raise asyncio.TimeoutError()
        return await awaitable

    monkeypatch.setattr(asyncio, "wait_for", virtual_approval_delay)
    asyncio.run(execute(runtime))
    assert [data["text"] for kind, data in emitted if kind == "message.delta"] == ["answer"]
    assert methods[0] == ("session/prompt", "owned", [{"type": "text", "text": "/permission literal"}])
    assert methods[-1][0] == "session/wait"


@pytest.mark.parametrize("finish,observed,code", [("error", True, "HARNESS_TURN_FAILED"), (None, True, "HARNESS_TURN_FAILED"), ("completed", False, "HARNESS_STREAM_INCOMPLETE")])
def test_idle_or_partial_text_cannot_replace_a_successful_terminal(finish, observed, code):
    runtime, _emitted, methods = adapter(finish, observed)
    with pytest.raises(AppError) as error:
        asyncio.run(execute(runtime))
    assert error.value.code == code
    assert "Harness" not in error.value.message
    assert all(method[0] != "session/wait" for method in methods)
