"""Synthetic stdio contracts for the default-off Codex App Server host.

No test here starts the real Codex binary, reads a real login, selects a model
or sends an external model request.  The peer is the repository's synthetic
JSON-RPC fixture; the pinned real binary contract remains the separate
``scripts/oss/codex_appserver_stdio_probe.py`` evidence.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import pytest

from app.codex_appserver import (
    CodexHost, CodexHostBusy, CodexHostConfig, CodexHostConflict,
    CodexHostDisabled, CodexHostError, CodexHostRejected, CodexHostUnavailable,
)

FAKE = (Path(__file__).resolve().parents[2] / "scripts" / "oss"
        / "codex_appserver_fake_peer.py")
USER = "user-0000000000000001"
OTHER = "user-0000000000000002"
CID = "conv-0000000000000001"


def make_host(tmp_path: Path, scenario: str = "happy", *, turn_id: str = "",
              thread_id: str = "", expected_sha256: str = "",
              response_timeout: float = 2.0, turn_timeout: float = 5.0,
              reconcile_timeout: float = 3.0, max_input_chars: int = 20000,
              executable: str = sys.executable):
    log = tmp_path / f"peer-{scenario}-{time.time_ns()}.jsonl"
    command = (executable, str(FAKE), "app-server", "--stdio",
               "--scenario", scenario, "--log", str(log))
    if turn_id:
        command += ("--turn-id", turn_id)
    if thread_id:
        command += ("--thread-id", thread_id)
    host = CodexHost(CodexHostConfig(
        enabled=True,
        command=command,
        data_dir=tmp_path,
        expected_executable_sha256=expected_sha256,
        response_timeout_seconds=response_timeout,
        turn_timeout_seconds=turn_timeout,
        reconcile_timeout_seconds=reconcile_timeout,
        max_input_chars=max_input_chars,
    ))
    return host, log


def log_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            result.append(json.loads(line))
        except ValueError:
            continue
    return result


def wait_for_log(path: Path, predicate, timeout: float = 5.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = log_events(path)
        if predicate(events):
            return events
        time.sleep(0.02)
    raise AssertionError("synthetic Codex peer did not reach the expected event")


def requests(events, method):
    return [event for event in events
            if event.get("event") == "request" and event.get("method") == method]


def state_path(tmp_path: Path, user: str, conversation: str) -> Path:
    key = hashlib.sha256(f"{user}\x00{conversation}".encode("utf-8")).hexdigest()
    return tmp_path / "codex-appserver" / "state" / f"{key}.json"


def test_disabled_host_does_nothing(tmp_path):
    host = CodexHost(CodexHostConfig(enabled=False, data_dir=tmp_path))
    with pytest.raises(CodexHostDisabled):
        host.status(USER, CID)
    with pytest.raises(CodexHostDisabled):
        host.ensure_thread(USER, CID)
    assert not (tmp_path / "codex-appserver").exists()


def test_thread_start_is_owner_scoped_and_read_only(tmp_path):
    host, log = make_host(tmp_path)
    view = host.ensure_thread(USER, CID)
    assert view["bound"] is True
    assert view["recovery_required"] is False
    thread_id = view["thread_id"]

    events = wait_for_log(log, lambda items: len(requests(items, "thread/start")) == 1)
    startup = next(event for event in events if event.get("event") == "startup")
    root = tmp_path / "codex-appserver"
    assert Path(startup["env"]["HOME"]) == root / "home"
    assert Path(startup["env"]["CODEX_HOME"]) == root / "codex-home"
    assert startup["disallowed_present"] == []
    started = requests(events, "thread/start")[0]["params"]
    expected_cwd = (root / "workspaces" / hashlib.sha256(USER.encode()).hexdigest()
                    / hashlib.sha256(CID.encode()).hexdigest())
    assert started["cwd"] == str(expected_cwd)
    assert started["sandbox"] == "read-only"
    assert started["approvalPolicy"] == "never"
    assert started["ephemeral"] is False
    assert "model" not in started
    named = requests(log_events(log), "thread/name/set")
    assert [event["params"] for event in named] == [{"threadId": thread_id, "name": "开物会话"}]
    assert requests(log_events(log), "turn/start") == []

    # A different owner never observes the first owner's binding.
    other, _ = make_host(tmp_path)
    assert other.status(OTHER, CID) == {
        "bound": False, "thread_id": None, "active_turn": None,
        "recovery_required": False, "last_turn": None,
    }
    other_view = other.ensure_thread(OTHER, CID)
    assert other_view["thread_id"] != thread_id


def test_prepare_does_not_publish_unpersisted_thread(tmp_path):
    host, log = make_host(tmp_path, "name_rejected")
    with pytest.raises(CodexHostRejected):
        host.ensure_thread(USER, CID)
    assert host.status(USER, CID)["bound"] is False
    assert requests(log_events(log), "turn/start") == []


def test_turn_receipt_identity_and_idempotent_replay(tmp_path):
    host, log = make_host(tmp_path)
    thread_id = host.ensure_thread(USER, CID)["thread_id"]
    receipt = host.run_turn(USER, CID, "request-0001", "合成请求")
    assert receipt["thread_id"] == thread_id
    assert receipt["status"] == "completed"
    assert receipt["text"] == "合成回复"
    assert receipt["replayed"] is False
    assert receipt["turn_id"]

    events = wait_for_log(log, lambda items: len(requests(items, "turn/start")) == 1)
    started = requests(events, "turn/start")[0]["params"]
    assert started["threadId"] == thread_id
    assert started["input"] == [{"type": "text", "text": "合成请求"}]
    assert started["approvalPolicy"] == "never"
    assert "model" not in started

    replay = host.run_turn(USER, CID, "request-0001", "合成请求")
    assert replay == {**receipt, "replayed": True}
    assert len(requests(log_events(log), "turn/start")) == 1

    with pytest.raises(CodexHostConflict):
        host.run_turn(USER, CID, "request-0001", "不同内容")

    second = host.run_turn(USER, CID, "request-0002", "第二轮")
    assert second["replayed"] is False
    assert second["turn_id"] != receipt["turn_id"]
    resumed = requests(log_events(log), "thread/resume")
    assert resumed and resumed[0]["params"]["threadId"] == thread_id

    # A bounded recent-request history preserves idempotency beyond the last
    # turn instead of silently starting a duplicate.
    older = host.run_turn(USER, CID, "request-0001", "合成请求")
    assert older["replayed"] is True
    assert older["turn_id"] == receipt["turn_id"]
    assert len(requests(log_events(log), "turn/start")) == 2


def test_server_requests_are_always_rejected(tmp_path):
    for scenario, request_id in (("approval", 900), ("unknown_request", 901)):
        root = tmp_path / scenario
        root.mkdir()
        host, log = make_host(root, scenario)
        host.ensure_thread(USER, CID)
        receipt = host.run_turn(USER, CID, "request-0001", "合成请求")
        assert receipt["status"] == "completed"
        assert receipt["rejected_server_requests"] >= 1
        events = wait_for_log(
            log, lambda items: any(event.get("event") == "server_request_response"
                                   and event.get("id") == request_id for event in items))
        response = next(event for event in events
                        if event.get("event") == "server_request_response"
                        and event.get("id") == request_id)
        assert response["body"]["error"]["code"] == -32601
        assert "result" not in response["body"]
        assert response["body"]["error"]["message"] in {
            "host approval unavailable", "unknown server request rejected"}


def test_mismatched_notification_fails_closed(tmp_path):
    host, _ = make_host(tmp_path, "mismatch")
    host.ensure_thread(USER, CID)
    with pytest.raises(CodexHostUnavailable):
        host.run_turn(USER, CID, "request-0001", "合成请求")
    view = host.status(USER, CID)
    assert view["recovery_required"] is True
    assert view["active_turn"]["turn_id"]
    with pytest.raises(CodexHostConflict):
        host.ensure_thread(USER, CID)
    with pytest.raises(CodexHostConflict):
        host.run_turn(USER, CID, "request-0002", "不能重放")


def test_turn_timeout_leaves_uncertain_state(tmp_path):
    host, _ = make_host(tmp_path, "hang", turn_timeout=0.4)
    host.ensure_thread(USER, CID)
    with pytest.raises(CodexHostUnavailable):
        host.run_turn(USER, CID, "request-0001", "合成请求")
    assert host.status(USER, CID)["recovery_required"] is True


def test_crash_then_reconcile_terminal_turn(tmp_path):
    crashing, _ = make_host(tmp_path, "exit")
    thread_id = crashing.ensure_thread(USER, CID)["thread_id"]
    with pytest.raises(CodexHostError):
        crashing.run_turn(USER, CID, "request-0001", "合成请求")
    crashed = crashing.status(USER, CID)
    assert crashed["recovery_required"] is True
    active = crashed["active_turn"]["turn_id"]

    recovered, log = make_host(tmp_path, "reconcile_terminal", turn_id=active)
    view = recovered.reconcile(USER, CID)
    assert view["recovery_required"] is False
    assert view["last_turn"]["turn_id"] == active
    assert view["last_turn"]["status"] == "completed"
    events = wait_for_log(log, lambda items: len(requests(items, "thread/read")) >= 1)
    assert all(event.get("method") != "turn/start"
               for event in events if event.get("event") == "request")
    assert requests(events, "thread/resume")[0]["params"]["threadId"] == thread_id
    assert requests(events, "thread/read")[0]["params"]["includeTurns"] is True
    replay = recovered.run_turn(USER, CID, "request-0001", "合成请求")
    assert replay["replayed"] is True and replay["turn_id"] == active
    assert replay["text"] == "" and replay["text_truncated"] is True
    assert replay["rejected_server_requests"] is None
    assert requests(log_events(log), "turn/start") == []


def test_reconcile_in_progress_interrupts_exact_turn(tmp_path):
    crashing, _ = make_host(tmp_path, "exit")
    crashing.ensure_thread(USER, CID)
    with pytest.raises(CodexHostError):
        crashing.run_turn(USER, CID, "request-0001", "合成请求")
    crashed = crashing.status(USER, CID)
    assert crashed["recovery_required"] is True
    active = crashed["active_turn"]["turn_id"]

    recovered, log = make_host(tmp_path, "reconcile_interrupt", turn_id=active)
    view = recovered.reconcile(USER, CID)
    assert view["recovery_required"] is False
    assert view["last_turn"]["status"] == "interrupted"
    events = wait_for_log(log, lambda items: any(
        event.get("event") == "interrupt" for event in items))
    interrupt = next(event for event in events if event.get("event") == "interrupt")
    assert interrupt["turnId"] == active


def test_restart_resumes_the_same_thread(tmp_path):
    first, first_log = make_host(tmp_path)
    thread_id = first.ensure_thread(USER, CID)["thread_id"]
    first.run_turn(USER, CID, "request-0001", "第一轮")

    second, second_log = make_host(tmp_path)
    assert second.ensure_thread(USER, CID)["thread_id"] == thread_id
    events = wait_for_log(second_log, lambda items: len(requests(items, "thread/resume")) == 1)
    assert requests(events, "thread/resume")[0]["params"]["threadId"] == thread_id
    assert requests(events, "thread/start") == []
    assert len(requests(log_events(first_log), "thread/start")) == 1


def test_pinned_digest_rejects_before_launch(tmp_path):
    resolved = str(Path(sys.executable).resolve())
    wrong, wrong_log = make_host(tmp_path, executable=resolved,
                                 expected_sha256="0" * 64)
    with pytest.raises(CodexHostUnavailable):
        wrong.ensure_thread(USER, CID)
    assert not wrong_log.exists()

    pinned, _ = make_host(tmp_path, executable=resolved,
                          expected_sha256=hashlib.sha256(
                              Path(resolved).read_bytes()).hexdigest())
    assert pinned.ensure_thread(USER, CID)["bound"] is True


def test_owner_binding_tamper_fails_closed(tmp_path):
    host, _ = make_host(tmp_path)
    host.ensure_thread(USER, CID)
    path = state_path(tmp_path, USER, CID)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["user_id"] = OTHER
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(CodexHostUnavailable):
        host.status(USER, CID)


def test_input_and_request_budgets_are_enforced(tmp_path):
    host, log = make_host(tmp_path, max_input_chars=4)
    with pytest.raises(CodexHostRejected):
        host.run_turn(USER, CID, "request-0001", "输入内容过长")
    with pytest.raises(CodexHostRejected):
        host.run_turn(USER, CID, "short", "abcd")
    with pytest.raises(CodexHostRejected):
        host.run_turn(USER, CID, "request-0002", "   ")
    assert not log.exists()


def test_turn_fencing_and_owned_interrupt(tmp_path):
    host, log = make_host(tmp_path, "interrupt")
    host.ensure_thread(USER, CID)
    key = hashlib.sha256(f"{USER}\x00{CID}".encode("utf-8")).hexdigest()
    with host._turn_lock(key):
        with pytest.raises(CodexHostBusy):
            host.run_turn(USER, CID, "request-0001", "合成请求")

    outcome: dict = {}

    def run() -> None:
        try:
            outcome["receipt"] = host.run_turn(USER, CID, "request-0001", "合成请求")
        except BaseException as exc:  # pragma: no cover - asserted below
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    wait_for_log(log, lambda items: len(requests(items, "turn/start")) == 1)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        view = host.status(USER, CID)
        if view["active_turn"]:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("turn was not durably admitted")
    active = view["active_turn"]["turn_id"]
    assert view["active_turn"]["can_interrupt"] is True
    other_process, _ = make_host(tmp_path)
    assert other_process.status(USER, CID)["active_turn"]["can_interrupt"] is False
    with pytest.raises(CodexHostConflict):
        host.interrupt(USER, CID, "turn-not-the-active-one")
    interrupted = host.interrupt(USER, CID, active)
    assert interrupted["status"] == "interrupt_requested"
    assert interrupted["turn_id"] == active
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["receipt"]["status"] == "interrupted"
    events = wait_for_log(log, lambda items: any(
        event.get("event") == "interrupt" for event in items))
    assert next(event for event in events
                if event.get("event") == "interrupt")["turnId"] == active
    with pytest.raises(CodexHostConflict):
        host.interrupt(USER, CID)


def test_config_requires_absolute_pinned_launch(tmp_path):
    with pytest.raises(ValueError):
        CodexHostConfig(enabled=True, command=("codex", "app-server", "--stdio"),
                        data_dir=tmp_path)
    with pytest.raises(ValueError):
        CodexHostConfig(enabled=True, command=(str(FAKE),), data_dir=tmp_path,
                        expected_executable_sha256="ABC")
    with pytest.raises(ValueError):
        CodexHostConfig(enabled=False, data_dir="relative")

def test_start_receipt_lost_is_fenced_before_send(tmp_path, monkeypatch):
    from app.codex_appserver import _Session
    host, log = make_host(tmp_path)
    host.ensure_thread(USER, CID)
    original = _Session.start_turn
    def lose_receipt(session, thread_id, text):
        # The actual peer receives the request, but the host dies before
        # installing a known turn ID. The admission must already be durable.
        original(session, thread_id, text)
        raise SystemExit("synthetic hard failure after send")
    monkeypatch.setattr(_Session, "start_turn", lose_receipt)
    with pytest.raises(SystemExit):
        host.run_turn(USER, CID, "request-0001", "合成请求")
    restarted, restarted_log = make_host(tmp_path)
    assert restarted.status(USER, CID)["recovery_required"] is True
    with pytest.raises(CodexHostConflict):
        restarted.reconcile(USER, CID)
    with pytest.raises(CodexHostConflict):
        restarted.run_turn(USER, CID, "request-0001", "合成请求")
    assert not requests(log_events(restarted_log), "turn/start")
    assert len(requests(log_events(log), "turn/start")) == 1


def test_expired_receipt_cannot_repeat_admitted_request(tmp_path, monkeypatch):
    import app.codex_appserver as module
    monkeypatch.setattr(module, "RECENT_REQUESTS", 2)
    monkeypatch.setattr(module, "MAX_ADMITTED_REQUESTS", 3)
    host, log = make_host(tmp_path)
    host.ensure_thread(USER, CID)
    for i in range(1, 4):
        host.run_turn(USER, CID, f"request-000{i}", f"合成 {i}")
    with pytest.raises(CodexHostConflict):
        host.run_turn(USER, CID, "request-0001", "合成 1")
    with pytest.raises(CodexHostConflict):
        host.run_turn(USER, CID, "request-0004", "合成 4")
    assert len(requests(log_events(log), "turn/start")) == 3


def test_first_explicit_request_has_one_process_and_live_owner_feedback(tmp_path, monkeypatch):
    from app.codex_appserver import _Session

    host, log = make_host(tmp_path)
    reader, _ = make_host(tmp_path)
    initializing, resume = threading.Event(), threading.Event()
    displaying, finish = threading.Event(), threading.Event()
    initialize = _Session.initialize
    wait = _Session.wait_turn

    def pause_initialize(session):
        initializing.set()
        assert resume.wait(10)
        return initialize(session)

    def hold_first_output(session, thread_id, turn_id, *, first_output):
        def display(text):
            first_output(text)
            displaying.set()
            assert finish.wait(10)
        return wait(session, thread_id, turn_id, first_output=display)

    monkeypatch.setattr(_Session, "initialize", pause_initialize)
    monkeypatch.setattr(_Session, "wait_turn", hold_first_output)
    outcomes = []
    run = threading.Thread(target=lambda: outcomes.append(
        host.run_turn(USER, CID, "first-request", "只读请求", True)), daemon=True)
    run.start()
    try:
        assert initializing.wait(10)
        initial = reader.status(USER, CID)
        assert not initial["bound"]
        assert initial["operation"] == {"request_id": "first-request", "phase": "preparing"}
        assert reader.status(OTHER, CID).get("operation") is None
        with pytest.raises(CodexHostBusy):
            reader.run_turn(USER, CID, "first-request", "只读请求", True)
        resume.set()
        assert displaying.wait(10)
        live = reader.status(USER, CID)
        assert live["bound"] and live["active_turn"]["request_id"] == "first-request"
        assert live["operation"]["phase"] == "output" and live["operation"]["text"]
    finally:
        resume.set()
        finish.set()
        run.join(10)
    assert len(outcomes) == 1 and outcomes[0]["status"] == "completed"
    saved = reader.status(USER, CID)
    assert saved.get("operation") is None and saved["last_turn"]["request_id"] == "first-request"
    assert saved["last_turn"]["turn_id"] == live["active_turn"]["turn_id"]
    assert reader.run_turn(USER, CID, "first-request", "只读请求", True)["replayed"]
    records = log_events(log)
    assert len(requests(records, "initialize")) == len(requests(records, "turn/start")) == 1
    assert not requests(records, "thread/resume")
