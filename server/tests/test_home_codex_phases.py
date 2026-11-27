"""Home progress must describe the same authorized turn as the engineering panel."""
import json
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.codex_appserver import CodexHostError, _Session
from test_api import PASSWORD, conversation, login
from test_codex_appserver_host import log_events, make_host, requests, state_path
from test_conversation_codex import build, codex_env


HOME = "/api/v1/workspace/codex-phases"
CONVERSATIONS = "/api/v1/conversations"


def phases(client, *ids):
    response = client.get(HOME, params=[("conversation_id", cid) for cid in ids])
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    assert set(response.json()) == {"phases"}
    return response.json()["phases"]


def member(client, service, name):
    user = service.create_user({"username": name, "display_name": name,
                                "password": PASSWORD, "role": "member"})
    login(client, name)
    return user["id"]


def submit(client, cid, request):
    return client.post(f"{CONVERSATIONS}/{cid}/codex/turn", json={
        "client_request_id": request, "text": "合成工程输入", "prepare_if_needed": True,
    })


def test_home_follows_the_current_turn_through_cleanup(codex_env, tmp_path, monkeypatch):
    client, service, host = codex_env
    member(client, service, "engineer")
    cid = conversation(client)["id"]
    old, current = "earlier-request", "current-request"
    assert submit(client, cid, old).status_code == 200

    # Hold the real producer immediately after each publication, including the
    # window after the receipt reaches disk but before the turn lock is released.
    stages = ["preparing", "thread_ready", "running", "output", "completed"]
    entered = {phase: Event() for phase in stages}
    advance = {phase: Event() for phase in stages}
    original_lock = host._turn_lock

    def hold(phase):
        entered[phase].set()
        assert advance[phase].wait(10), phase

    @contextmanager
    def observed_lock(*args, **kwargs):
        with original_lock(*args, **kwargs) as publish:
            hold("preparing")

            def observe(phase, text=None):
                publish(phase, text)
                hold(phase)

            yield observe
            hold("completed")

    monkeypatch.setattr(host, "_turn_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(submit, client, cid, current)
        try:
            for stage in stages:
                assert entered[stage].wait(10), stage
                detail = client.get(f"{CONVERSATIONS}/{cid}/codex").json()
                assert detail["operation"]["request_id"] == current
                assert detail["operation"]["phase"] == ("output" if stage == "completed" else stage)
                assert detail["last_turn"]["request_id"] == (current if stage == "completed" else old)
                if stage == "output":
                    assert detail["operation"]["text"]
                assert phases(client, cid) == [stage]
                advance[stage].set()
        finally:
            for release in advance.values():
                release.set()
        assert future.result(timeout=10).status_code == 200
    assert phases(client, cid) == ["completed"]
    events = [event for path in tmp_path.glob("peer-*.jsonl") for event in log_events(path)]
    # Two user POSTs account for every process and turn despite repeated GETs.
    assert len(requests(events, "initialize")) == len(requests(events, "turn/start")) == 2


def test_home_rechecks_owner_assistant_and_conversation_access(codex_env, tmp_path):
    client, service, host = codex_env
    response = client.post("/api/v1/assistants", json={
        "name": "共享工程", "instructions": "合成测试", "connections": [], "scope": "team",
    })
    assert response.status_code == 201, response.text
    aid = response.json()["id"]
    owner = member(client, service, "member-owner")
    created = client.post(CONVERSATIONS, json={"assistant_id": aid})
    assert created.status_code == 201, created.text
    cid = created.json()["id"]
    idle = conversation(client)["id"]
    detail = f"{CONVERSATIONS}/{cid}/codex"

    with host._turn_lock(host._key(owner, cid), "ongoing-request", preparing=True):
        assert phases(client, idle, cid) == [None, "preparing"]
        member(client, service, "observer")
        assert phases(client, cid, "0" * 32) == [None, None]
        login(client, "admin")
        assert phases(client, cid) == [None]
        login(client, "member-owner")
        assert phases(client, cid) == ["preparing"]
        service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (cid,))
        assert client.get(detail).status_code == 409
        assert phases(client, cid) == [None]
        service.db.execute("UPDATE conversations SET archived=0,active_run_id='ordinary-run' WHERE id=?", (cid,))
        assert phases(client, cid) == [None]
        service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (cid,))
        service.db.execute("UPDATE assistants SET access=? WHERE id=?",
                           (json.dumps({"all_members": False, "user_ids": []}), aid))
        assert client.get(detail).status_code == 403
        assert phases(client, cid) == [None]
        service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (owner,))
        assert client.get(HOME, params={"conversation_id": cid}).status_code in (401, 403)
    assert not list(tmp_path.glob("peer-*.jsonl"))


def test_previous_process_exit_and_stale_lock_are_not_live(codex_env, tmp_path):
    client, _, host = codex_env
    owner = client.get("/api/v1/auth/me").json()["id"]
    crashed, idle = (conversation(client)["id"] for _ in range(2))
    departed, log = make_host(tmp_path, "exit")
    with pytest.raises(CodexHostError):
        departed.run_turn(owner, crashed, "orphaned-request", "合成", True)
    detail = client.get(f"{CONVERSATIONS}/{crashed}/codex").json()
    assert detail["active_turn"] and not detail["active_turn"]["can_interrupt"]
    assert detail["recovery_required"] and not detail.get("operation")
    key = host._key(owner, idle)
    with host._turn_lock(key, "stale-request", preparing=True):
        pass
    # Emulate a crash leaving advisory bytes on disk with no flock owner.
    host._lock_path(key, "turn").write_text(
        json.dumps({"phase": "output", "request_id": "stale-request", "text": "旧正文"}))
    prior = log.read_bytes()
    assert phases(client, idle, crashed) == [None, "recovery_required"]
    assert phases(client, crashed, idle) == ["recovery_required", None]
    assert log.read_bytes() == prior
    assert list(tmp_path.glob("peer-*.jsonl")) == [log]


def test_unreadable_owner_state_does_not_hide_a_healthy_row(codex_env, tmp_path):
    client, _, _ = codex_env
    owner = client.get("/api/v1/auth/me").json()["id"]
    broken, healthy = (conversation(client)["id"] for _ in range(2))
    assert submit(client, broken, "broken-request").status_code == 200
    assert submit(client, healthy, "normal-request").status_code == 200
    binding = state_path(tmp_path, owner, broken)
    binding.write_text("{")
    assert phases(client, broken, healthy) == ["unavailable", "completed"]
    assert binding.read_text() == "{"


@pytest.mark.parametrize("trial", [False, True])
def test_disabled_or_trial_home_never_starts_host(tmp_path, trial):
    app, hosts = build(tmp_path, enabled=trial, trial=trial)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "admin",
                                       "password": PASSWORD, "role": "admin"})
        login(client)
        assert client.get(HOME, params={"conversation_id": "test-session"}).status_code in (403, 404)
    assert hosts == []

def test_user_interrupt_is_visible_until_the_turn_stops(codex_env, tmp_path, monkeypatch):
    client, _, host = codex_env
    cid = conversation(client)["id"]
    peer, log = make_host(tmp_path, "interrupt")
    monkeypatch.setattr(host, "config", peer.config)
    entered, finish = Event(), Event()
    wait_turn = _Session.wait_turn

    def hold_before_receipt(session, *args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return wait_turn(session, *args, **kwargs)

    monkeypatch.setattr(_Session, "wait_turn", hold_before_receipt)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(submit, client, cid, "stop-request")
        try:
            assert entered.wait(10)
            assert phases(client, cid) == ["running"]
            assert client.post(f"{CONVERSATIONS}/{cid}/codex/interrupt", json={}).status_code == 200
            assert phases(client, cid) == ["interrupting"]
        finally:
            finish.set()
        assert future.result(timeout=10).json()["status"] == "interrupted"
    assert phases(client, cid) == ["interrupted"]
    assert len(requests(log_events(log), "turn/interrupt")) == 1


def test_batch_limit_and_signed_out_reads(codex_env, tmp_path):
    client, _, _ = codex_env
    cid = conversation(client)["id"]
    assert client.get(HOME).status_code == 422
    assert client.get(HOME, params=[("conversation_id", cid)] * 6).status_code == 422
    assert phases(client, cid) == [None]
    client.cookies.clear()
    assert client.get(HOME, params={"conversation_id": cid}).status_code == 401
    assert not list(tmp_path.glob("peer-*.jsonl"))

def test_finishing_during_a_home_read_does_not_claim_recovery(codex_env, monkeypatch):
    client, _, host = codex_env
    cid = conversation(client)["id"]
    assert phases(client, cid) == [None]
    running, finish, probe, resume_probe = (Event() for _ in range(4))
    wait_turn, operation = _Session.wait_turn, host._operation

    def hold_before_receipt(session, *args, **kwargs):
        running.set()
        assert finish.wait(10)
        return wait_turn(session, *args, **kwargs)

    def hold_probe(key):
        probe.set()
        assert resume_probe.wait(10)
        return operation(key)

    monkeypatch.setattr(_Session, "wait_turn", hold_before_receipt)
    monkeypatch.setattr(host, "_operation", hold_probe)
    with ThreadPoolExecutor(max_workers=2) as workers:
        turn = workers.submit(submit, client, cid, "finishing-now")
        try:
            assert running.wait(10)
            view = workers.submit(phases, client, cid)
            assert probe.wait(10)
            finish.set()
            # Let the turn finish between binding and operation reads if the
            # implementation permits that interleaving. A single locked snapshot
            # may legitimately finish the GET first and still report live work.
            wait([turn], timeout=1.5)
        finally:
            resume_probe.set()
            finish.set()
        assert view.result(timeout=10)[0] in {"running", "output", "completed"}
        assert turn.result(timeout=10).status_code == 200
    assert phases(client, cid) == ["completed"]
