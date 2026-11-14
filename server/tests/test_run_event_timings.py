"""Event timing must describe observed boundaries, not business correctness."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.config import Settings
from app.db import dump
from app.runtime import SessionActor, save_timing
from app.service import Service


@pytest.fixture
def system(tmp_path):
    service = Service(Settings(data_dir=tmp_path))
    user = service.create_user({"username": "timing-test", "display_name": "Test", "password": "safe-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Timing test", "connections": []})
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "test"}, user)
    response, _ = service.prepare_run(conversation["id"], {"text": "read", "file_ids": [], "client_request_id": "timing"}, user)
    actor = object.__new__(SessionActor)
    actor.service = service
    run = service.require("runs", response["run_id"])
    try:
        yield service, actor, run
    finally:
        service.db.close()


def timings(system):
    service, _, run = system
    return json.loads(service.require("runs", run["id"])["timings"])


def test_first_observations_are_retained_without_inventing_business_metrics(system, monkeypatch):
    service, actor, run = system
    values = iter([10, 20, 30, 40, 50, 60])
    monkeypatch.setattr("app.runtime.elapsed_ms", lambda _: next(values))
    for kind, data in [
        ("run.stage", {"stage": "initializing"}),
        ("run.stage", {"stage": "scoping"}),
        ("tool.started", {"requires_approval": True}),
        ("tool.completed", {"success": False}),
        ("tool.completed", {"success": True}),
        ("tool.completed", {"success": True}),
    ]:
        actor._emit(run["id"], kind, data)
    assert timings(system) == {"first_server_progress_ms": 10, "first_tool_attempt_ms": 30, "first_tool_success_ms": 50}
    assert len([e for e in service.db.events(run["id"], 0) if e["type"] == "timing.updated"]) == 3


@pytest.mark.parametrize("data", [{}, {"success": None}, {"success": False}, {"success": 0}, {"success": 1}, {"success": "true"}])
def test_unknown_or_failed_tool_result_does_not_count_as_success(system, data):
    _, actor, run = system
    actor._emit(run["id"], "tool.completed", data)
    assert timings(system) == {}


def test_denied_approval_records_attempt_only(system):
    _, actor, run = system
    actor._emit(run["id"], "tool.started", {"requires_approval": True})
    actor._emit(run["id"], "approval.resolved", {"decision": "deny"})
    actor._emit(run["id"], "tool.completed", {"success": False})
    assert set(timings(system)) == {"first_tool_attempt_ms"}


def test_text_only_run_records_server_text_without_claiming_tool_or_business_result(system, monkeypatch):
    service, actor, run = system
    monkeypatch.setattr("app.runtime.elapsed_ms", lambda _: 20)
    for text in ["", " "]:
        actor._emit(run["id"], "message.delta", {"message_id": run["assistant_message_id"], "text": text})
    assert timings(system) == {}
    actor._emit(run["id"], "message.delta", {"message_id": run["assistant_message_id"], "text": "Answer"})
    assert timings(system) == {"first_server_progress_ms": 20, "first_text_ms": 20}
    assert service.require("messages", run["assistant_message_id"])["text"] == " Answer"


def test_triggering_event_failure_rolls_back_timing_and_text(system, monkeypatch):
    service, actor, run = system
    original_event = service.db.event
    def fail_trigger(run_id, kind, data):
        if kind == "message.delta":
            raise RuntimeError("simulated write failure")
        return original_event(run_id, kind, data)
    monkeypatch.setattr(service.db, "event", fail_trigger)
    with pytest.raises(RuntimeError, match="simulated write failure"):
        actor._emit(run["id"], "message.delta", {"message_id": run["assistant_message_id"], "text": "Answer"})
    assert timings(system) == {}
    assert service.db.events(run["id"], 0) == []
    assert service.require("messages", run["assistant_message_id"])["text"] == ""


def test_concurrent_tool_callbacks_preserve_each_boundary(system):
    service, actor, run = system
    barrier = Barrier(4)
    def emit(kind, data):
        barrier.wait(timeout=5)
        actor._emit(run["id"], kind, data)
    def provider_timing():
        barrier.wait(timeout=5)
        save_timing(service, run["id"], "runtime_ready_ms", 5)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(emit, kind, data) for kind, data in [
            ("run.stage", {"stage": "initializing"}), ("tool.started", {}), ("tool.completed", {"success": True}),
        ]]
        futures.append(pool.submit(provider_timing))
        for future in futures:
            future.result(timeout=5)
    assert set(timings(system)) == {"first_server_progress_ms", "first_tool_attempt_ms", "first_tool_success_ms", "runtime_ready_ms"}


def test_legacy_timings_are_preserved_but_not_reused_as_new_metrics(system, monkeypatch):
    service, actor, run = system
    legacy = {"first_feedback_ms": 1, "first_structure_ms": 2, "first_valid_result_ms": 3}
    service.db.execute("UPDATE runs SET timings=? WHERE id=?", (dump(legacy), run["id"]))
    monkeypatch.setattr("app.runtime.elapsed_ms", lambda _: 20)
    actor._emit(run["id"], "run.stage", {"stage": "initializing"})
    assert timings(system) == {**legacy, "first_server_progress_ms": 20}
