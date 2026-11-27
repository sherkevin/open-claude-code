"""Opt-in Codex startup preparation crosses the real app lifespan and stdio boundary."""
import fcntl
import hashlib
import multiprocessing
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.codex_appserver import CodexHostUnavailable, _Session
from app.main import create_app
from test_api import FakeRuntime, PASSWORD, conversation, login
from test_codex_appserver_host import log_events, make_host, requests
from test_conversation_codex import settings_for


FLAG = "AGENT_CODEX_APPSERVER_PREPARE_ON_STARTUP"
BASE = "/api/v1/conversations"
REQUEST = {"client_request_id": "explicit-only", "text": "合成问题", "prepare_if_needed": True}


def application(path, *, enabled=True, trial=False, digest=None):
    expected = digest if digest is not None else hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
    host, log = make_host(path, expected_sha256=expected)
    settings = settings_for(path, enabled=enabled, trial=trial)
    app = create_app(settings, runtime_factory=FakeRuntime, codex_host_factory=lambda _: host)
    return app, host, log


def authenticate(client, username, role="member"):
    client.app.state.service.create_user({"username": username, "display_name": username,
                                          "password": PASSWORD, "role": role})
    login(client, username)


def methods(log):
    return [row["method"] for row in log_events(log) if row["event"] == "request"]


def test_startup_prepares_runtime_once_without_an_owner_or_turn(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-parent-only")
    app, _, log = application(tmp_path)
    assert not log.exists()
    with TestClient(app) as client:
        before = log_events(log)
        assert methods(log) == ["initialize", "initialized"]
        boot, = [row for row in before if row["event"] == "startup"]
        assert any(row["event"] == "stdin_closed" for row in before)
        layout = tmp_path / "codex-appserver"
        assert Path(boot["env"]["CODEX_HOME"]) == layout / "codex-home"
        assert not boot["disallowed_present"]
        assert not Path(boot["cwd"]).exists()
        assert not list((layout / "state").glob("*.json"))
        assert not list((layout / "workspaces").iterdir())

        authenticate(client, "owner")
        cid = conversation(client)["id"]
        detail = f"{BASE}/{cid}/codex"
        assert client.get(detail).json()["bound"] is False
        assert client.get("/api/v1/workspace/codex-phases", params={"conversation_id": cid}).json() == {"phases": [None]}
        assert client.get(detail).json()["bound"] is False
        assert log_events(log) == before

        authenticate(client, "outsider")
        assert client.post(detail + "/turn", json=REQUEST).status_code == 404
        assert log_events(log) == before
        login(client, "owner")
        sent = client.post(detail + "/turn", json=REQUEST)
        assert sent.status_code == 200, sent.text
        receipt = sent.json()
        assert receipt["status"] == "completed"
        replay = client.post(detail + "/turn", json=REQUEST).json()
        assert replay["replayed"] and replay["turn_id"] == receipt["turn_id"]
        assert len(requests(log_events(log), "turn/start")) == 1
        assert len(requests(log_events(log), "initialize")) == 2
        turn_boot = [row for row in log_events(log) if row["event"] == "startup"][-1]
        assert turn_boot["env"] == boot["env"]
        assert turn_boot["cwd"] != boot["cwd"]


@pytest.mark.parametrize("flag,enabled,trial", [
    (None, True, False), ("false", True, False), ("true", False, False), ("true", True, True),
])
def test_startup_gates_leave_the_native_process_unstarted(tmp_path, monkeypatch, flag, enabled, trial):
    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    app, _, log = application(tmp_path, enabled=enabled, trial=trial)
    with TestClient(app) as client:
        authenticate(client, "admin", "admin")
        assert client.get("/api/v1/config").status_code == 200
        if enabled and not trial:
            cid = conversation(client)["id"]
            assert client.get(f"{BASE}/{cid}/codex").status_code == 200
    assert not log.exists()
    assert not (tmp_path / "codex-appserver").exists()


def test_prepare_flag_alone_cannot_supply_a_production_host(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    with pytest.raises(ValueError, match="explicitly provisioned isolated host"):
        create_app(settings_for(tmp_path), runtime_factory=FakeRuntime)


def test_failed_preparation_is_cleaned_and_an_explicit_turn_still_works(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    app, _, log = application(tmp_path)
    initialize = _Session.initialize
    private = "synthetic-sensitive-diagnostic"

    def fail_once(session):
        monkeypatch.setattr(_Session, "initialize", initialize)
        raise CodexHostUnavailable(private)

    monkeypatch.setattr(_Session, "initialize", fail_once)
    with TestClient(app) as client:
        boot, = [row for row in log_events(log) if row["event"] == "startup"]
        assert not Path(boot["cwd"]).exists()
        assert not methods(log)
        assert not list((tmp_path / "codex-appserver" / "state").glob("*.json"))
        assert "CodexHostUnavailable" in caplog.text
        assert private not in caplog.text
        authenticate(client, "owner")
        cid = conversation(client)["id"]
        result = client.post(f"{BASE}/{cid}/codex/turn", json=REQUEST)
        assert result.status_code == 200 and result.json()["status"] == "completed"
        assert methods(log).count("turn/start") == 1


def test_restart_prepares_once_and_preserves_durable_owner_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    first, _, first_log = application(tmp_path)
    with TestClient(first) as client:
        authenticate(client, "owner")
        cid = conversation(client)["id"]
        detail = f"{BASE}/{cid}/codex"
        response = client.post(detail + "/turn", json=REQUEST)
        assert response.status_code == 200
        receipt = response.json()
    assert methods(first_log).count("initialize") == 2

    restarted, _, log = application(tmp_path)
    with TestClient(restarted) as client:
        assert methods(log) == ["initialize", "initialized"]
        login(client, "owner")
        stored = client.get(detail).json()
        assert stored["thread_id"] == receipt["thread_id"]
        assert stored["last_turn"]["turn_id"] == receipt["turn_id"]
        replay = client.post(detail + "/turn", json=REQUEST).json()
        assert replay["replayed"] and replay["turn_id"] == receipt["turn_id"]
        assert methods(log) == ["initialize", "initialized"]


def test_invalid_binary_digest_prevents_native_bootstrap_and_falls_back_closed(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    app, host, log = application(tmp_path, digest="0" * 64)
    with TestClient(app) as client:
        assert "CodexHostUnavailable" in caplog.text
        assert not log.exists()
        authenticate(client, "owner")
        cid = conversation(client)["id"]
        detail = f"{BASE}/{cid}/codex"
        assert client.get(detail).json()["bound"] is False
        denied = client.post(detail + "/turn", json=REQUEST)
        assert denied.status_code == 503 and not log.exists()
        # Repair only the isolated fixture's pinned digest, then retry the unchanged request.
        valid_digest = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
        host.config = replace(host.config, expected_executable_sha256=valid_digest)
        result = client.post(detail + "/turn", json=REQUEST)
        assert result.status_code == 200 and methods(log).count("turn/start") == 1

def test_missing_digest_suppresses_only_operator_preparation(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(FLAG, "true")
    app, _, log = application(tmp_path, digest="")
    with TestClient(app) as client:
        assert not log.exists()
        assert "CodexHostUnavailable" in caplog.text
        authenticate(client, "owner")
        cid = conversation(client)["id"]
        # The pre-existing injected test host allows explicitly requested turns without a pin.
        result = client.post(f"{BASE}/{cid}/codex/turn", json=REQUEST)
        assert result.status_code == 200
        assert methods(log).count("initialize") == methods(log).count("turn/start") == 1

def run_worker(path, first, initialized, contended, release, ready):
    app, _, _ = application(path)
    initialize = _Session.initialize
    flock = fcntl.flock

    def hold_initialize(session):
        initialize(session)
        initialized.set()
        assert release.wait(15)

    def notice_contention(fd, flags):
        try:
            return flock(fd, flags)
        except BlockingIOError:
            if flags == fcntl.LOCK_EX | fcntl.LOCK_NB:
                contended.set()
            raise

    observer = (patch.object(_Session, "initialize", hold_initialize) if first
                else patch.object(fcntl, "flock", notice_contention))
    with observer, TestClient(app):
        ready.set()


def test_two_service_workers_wait_for_one_native_preparation(tmp_path, monkeypatch):
    monkeypatch.setenv(FLAG, "true")
    ctx = multiprocessing.get_context("fork")
    initialized, contended, release, first_ready, second_ready = (ctx.Event() for _ in range(5))
    common = (initialized, contended, release)
    first = ctx.Process(target=run_worker, args=(tmp_path, True, *common, first_ready))
    second = ctx.Process(target=run_worker, args=(tmp_path, False, *common, second_ready))
    first.start()
    try:
        assert initialized.wait(10)
        second.start()
        assert contended.wait(10)
        assert not first_ready.is_set() and not second_ready.is_set()
    finally:
        release.set()
        for child in (first, second):
            if child.pid is not None:
                child.join(15)
                if child.is_alive():
                    child.kill()
                    child.join(5)
    assert first.exitcode == second.exitcode == 0
    assert first_ready.is_set() and second_ready.is_set()
    log, = tmp_path.glob("peer-*.jsonl")
    assert methods(log) == ["initialize", "initialized"]
