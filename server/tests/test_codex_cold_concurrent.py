"""Actual worker and peer processes; model responses are synthesized locally."""
import fcntl
import hashlib
import json
import multiprocessing
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.codex_appserver import CodexHost, CodexHostConfig
from app.main import create_app
from test_api import FakeRuntime, PASSWORD, conversation, login
from test_codex_appserver_host import FAKE, log_events
from test_conversation_codex import settings_for


def wait_for(predicate, label):
    until = time.monotonic() + 18
    while not predicate():
        if time.monotonic() > until:
            pytest.fail(label)
        time.sleep(0.02)


def application(root):
    wrapper = Path(__file__).with_name("fixtures") / "codex_cold_peer.py"
    host = CodexHost(CodexHostConfig(
        enabled=True, data_dir=root,
        command=(sys.executable, str(wrapper), str(FAKE), str(root / "fixture"), "app-server", "--stdio"),
        expected_executable_sha256=hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
        response_timeout_seconds=30, turn_timeout_seconds=30,
    ))
    return create_app(settings_for(root), runtime_factory=FakeRuntime, codex_host_factory=lambda _: host)


@pytest.fixture
def owners(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_CODEX_APPSERVER_PREPARE_ON_STARTUP", raising=False)
    control = tmp_path / "fixture"
    control.mkdir()
    app = application(tmp_path)
    with TestClient(app) as first:
        second = TestClient(app)  # Independent cookies, one server lifespan.
        try:
            rows = []
            for client, name in [(first, "member-a"), (second, "member-b")]:
                app.state.service.create_user({"username": name, "display_name": name,
                                               "password": PASSWORD, "role": "member"})
                login(client, name)
                cid = conversation(client)["id"]
                rows.append((client, f"/api/v1/conversations/{cid}/codex"))
            yield control, *rows
        finally:
            second.close()


def send(owner, request_id):
    client, endpoint = owner
    return client.post(endpoint + "/turn", json={
        "client_request_id": request_id, "text": request_id, "prepare_if_needed": True,
    })


def get(owner):
    client, endpoint = owner
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    return response.json()


def events(control):
    return [row for path in sorted(control.glob("peer-*.jsonl")) for row in log_events(path)]


def submitted(control):
    return [row["params"]["input"][0]["text"] for row in events(control)
            if row.get("method") == "turn/start"]


def send_from_worker(root, username, endpoint, request_id):
    control = root / "fixture"
    state = root / "codex-appserver" / "state"
    flock = fcntl.flock

    def observe_guard(fd, flags):
        # Distinguish an actual waiter from a second worker that simply has
        # not run yet. A contended blocking acquisition still blocks as usual.
        if not flags & fcntl.LOCK_EX or not state.exists() or not os.path.samestat(os.fstat(fd), state.stat()):
            return flock(fd, flags)
        try:
            return flock(fd, flags | fcntl.LOCK_NB)
        except BlockingIOError:
            (control / "waiting-for-first-initialize").touch()
            if flags & fcntl.LOCK_NB:
                raise
            return flock(fd, flags)

    with TestClient(application(root)) as client:
        login(client, username)
        with patch.object(fcntl, "flock", observe_guard):
            result = send((client, endpoint), request_id)
        (control / f"{username}-response.json").write_text(json.dumps({
            "http": result.status_code, "status": result.json().get("status"),
            "error": result.json().get("error", {}).get("code"),
        }))


def confirm_turns_in_flight(control, a, b, request_ids):
    wait_for(lambda: len(list(control.glob("turn-in-flight-*"))) == 2, "two native turns did not overlap")
    for owner, request_id in zip((a, b), request_ids):
        wait_for(lambda: (get(owner)["active_turn"] or {}).get("request_id") == request_id,
                 "owner GET did not confirm the in-flight turn")


def test_two_workers_get_past_first_initialization_without_retrying_or_serializing_turns(owners):
    control, a, b = owners
    assert not get(a)["bound"] and not get(b)["bound"]
    assert not list(control.glob("peer-*.jsonl"))
    ctx = multiprocessing.get_context("spawn")
    ids = ("cold-request-a", "cold-request-b")
    first = ctx.Process(target=send_from_worker, args=(control.parent, "member-a", a[1], ids[0]))
    second = ctx.Process(target=send_from_worker, args=(control.parent, "member-b", b[1], ids[1]))
    (control / "hold-turns").touch()
    first.start()
    try:
        wait_for((control / "cold-entered").exists, "first cold handshake missing")
        second.start()
        wait_for(lambda: (control / "waiting-for-first-initialize").exists() or (control / "conflict-seen").exists(),
                 "second worker neither waited nor collided at native initialize")
        (control / "release-initialize").touch()
        if not (control / "conflict-seen").exists():
            confirm_turns_in_flight(control, a, b, ids)
    finally:
        (control / "release-initialize").touch()
        (control / "release-turns").touch()
        for worker in (first, second):
            if worker.pid is not None:
                worker.join(20)
                if worker.is_alive():
                    worker.kill()
                    worker.join(5)
    assert first.exitcode == second.exitcode == 0
    results = [json.loads(path.read_text()) for path in sorted(control.glob("member-*-response.json"))]
    assert [item["http"] for item in results] == [200, 200], results
    assert not (control / "conflict-seen").exists()
    assert sorted(submitted(control)) == sorted(ids)
    assert len(list(control.glob("peer-*.jsonl"))) == 2


def test_warm_worker_keeps_two_turns_live_without_the_cold_lock(owners):
    control, a, b = owners
    (control / "release-initialize").touch()
    assert send(a, "seed-first-request").status_code == 200
    (control / "hold-turns").touch()
    ids = ("parallel-request-a", "parallel-request-b")
    state_fd = os.open(control.parent / "codex-appserver" / "state", os.O_RDONLY | os.O_DIRECTORY)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            fcntl.flock(state_fd, fcntl.LOCK_EX)
            one = pool.submit(send, a, ids[0])
            two = pool.submit(send, b, ids[1])
            try:
                confirm_turns_in_flight(control, a, b, ids)
                assert not one.done() and not two.done()
            finally:
                fcntl.flock(state_fd, fcntl.LOCK_UN)
                (control / "release-turns").touch()
            assert one.result(timeout=15).status_code == two.result(timeout=15).status_code == 200
    finally:
        os.close(state_fd)
    assert sorted(submitted(control)) == sorted(["seed-first-request", *ids])
    assert len(list(control.glob("peer-*.jsonl"))) == 3


def test_failed_cold_handshake_recovers_only_on_another_explicit_send(owners):
    control, a, b = owners
    (control / "fail-one-initialize").touch()
    (control / "release-initialize").touch()
    assert send(a, "one-cold-failure").status_code == 503
    assert submitted(control) == []
    assert len(list(control.glob("peer-*.jsonl"))) == 1
    before = events(control)
    assert not get(a)["bound"] and not get(b)["bound"]
    assert events(control) == before
    success = send(b, "another-explicit-send")
    assert success.status_code == 200, success.text
    replay = send(b, "another-explicit-send").json()
    assert replay["replayed"] and replay["turn_id"] == success.json()["turn_id"]
    assert submitted(control) == ["another-explicit-send"]
    assert len(list(control.glob("peer-*.jsonl"))) == 2
