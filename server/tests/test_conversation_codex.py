"""Default-off product API for the Codex App Server host slice.

Synthetic stdio peer only: no real binary, model, provider or business tool.
"""
from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_api import FakeRuntime, PASSWORD, conversation, login
from test_codex_appserver_host import make_host


def settings_for(tmp_path, *, enabled: bool = True, trial: bool = False):
    return Settings(
        data_dir=tmp_path,
        provider_settings="",
        origins=("http://testserver",),
        mcp_hosts=("localhost", "127.0.0.1"),
        idle_seconds=60,
        approval_seconds=3,
        deployment_mode="trial" if trial else "normal",
        codex_appserver_enabled=enabled,
        codex_appserver_binary=str(tmp_path / "pinned-codex") if enabled else "",
        # The injected factory never calls from_settings; a syntactically valid
        # placeholder keeps the real settings gate exercised in production.
        codex_appserver_binary_sha256="0" * 64 if enabled else "",
    )


def build(tmp_path, *, enabled: bool = True, trial: bool = False):
    settings = settings_for(tmp_path, enabled=enabled, trial=trial)
    hosts = []

    def factory(_settings):
        host, _log = make_host(tmp_path)
        hosts.append(host)
        return host

    app = create_app(settings, runtime_factory=FakeRuntime, codex_host_factory=factory)
    return app, hosts


@pytest.fixture
def codex_env(tmp_path):
    app, hosts = build(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "admin",
                             "password": PASSWORD, "role": "admin"})
        login(client)
        yield client, service, hosts[0]


def assert_steering_denied(client, base, status):
    body = {"client_request_id": "steer-private-01", "turn_id": "turn-other", "text": "只看上个月"}
    assert client.post(base + "/steer", json=body).status_code == status
    assert client.get(base + "/steers").status_code == status


def test_codex_disabled_by_default(tmp_path):
    app, hosts = build(tmp_path, enabled=False)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "admin",
                                       "password": PASSWORD, "role": "admin"})
        login(client)
        cid = conversation(client)["id"]
        assert client.get(f"/api/v1/conversations/{cid}/codex").status_code == 404
        assert client.get(f"/api/v1/conversations/{cid}/codex/history").status_code == 404
        assert_steering_denied(client, f"/api/v1/conversations/{cid}/codex", 404)
        assert "codex_appserver" not in client.get("/api/v1/config").json()
    assert hosts == []


def test_trial_mode_does_not_expose_codex_router(tmp_path):
    app, hosts = build(tmp_path, enabled=True, trial=True)
    with TestClient(app) as client:
        response = client.get("/api/v1/conversations/anything/codex")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FEATURE_DISABLED"
        history = client.get("/api/v1/conversations/anything/codex/history")
        assert history.status_code == 403
        assert history.json()["error"]["code"] == "FEATURE_DISABLED"
        assert_steering_denied(client, "/api/v1/conversations/anything/codex", 403)
    assert hosts == []


def test_owner_thread_turn_and_replay(codex_env):
    client, _service, _host = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/codex"
    assert client.get("/api/v1/config").json()["codex_appserver"] is True

    status = client.get(base)
    assert status.status_code == 200, status.text
    assert status.json()["bound"] is False
    assert status.headers["cache-control"] == "no-store"

    prepared = client.post(base + "/thread")
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["bound"] is True
    thread_id = prepared.json()["thread_id"]

    turn = client.post(base + "/turn", json={
        "client_request_id": "request-0001", "text": "合成请求"})
    assert turn.status_code == 200, turn.text
    receipt = turn.json()
    assert receipt["thread_id"] == thread_id
    assert receipt["status"] == "completed"
    assert receipt["text"] == "合成回复"
    assert receipt["replayed"] is False
    # Reopening the page reads the bounded saved result without a new turn.
    latest = client.get(base).json()["last_turn"]
    assert latest["turn_id"] == receipt["turn_id"] and latest["text"] == receipt["text"]

    replay = client.post(base + "/turn", json={
        "client_request_id": "request-0001", "text": "合成请求"})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert replay.json()["turn_id"] == receipt["turn_id"]

    conflict = client.post(base + "/turn", json={
        "client_request_id": "request-0001", "text": "不同内容"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CODEX_CONFLICT"

    assert client.post(base + "/interrupt", json={}).status_code == 409
    reconciled = client.post(base + "/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["recovery_required"] is False

    extra = client.post(base + "/turn", json={
        "client_request_id": "request-0002", "text": "合成", "model": "not-allowed"})
    assert extra.status_code == 422


def test_other_owner_cannot_read_or_use_the_binding(codex_env):
    client, service, _host = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/codex"
    assert client.post(base + "/turn", json={
        "client_request_id": "owner-private", "text": "合成", "prepare_if_needed": True}).status_code == 200
    service.create_user({"username": "other", "display_name": "other",
                         "password": PASSWORD, "role": "member"})
    login(client, "other")
    assert client.get(base).status_code == 404
    assert_steering_denied(client, base, 404)
    denied = client.get(base + "/history")
    assert denied.status_code == 404
    assert "owner-private" not in denied.text and "合成回复" not in denied.text
    assert client.post(base + "/thread").status_code == 404
    assert client.post(base + "/turn", json={
        "client_request_id": "request-0001", "text": "合成"}).status_code == 404


def test_busy_conversation_and_active_run_are_refused(codex_env):
    client, service, host = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/codex"
    assert client.post(base + "/thread").status_code == 200
    key = hashlib.sha256(
        f"{client.get('/api/v1/auth/me').json()['id']}\x00{cid}".encode("utf-8")).hexdigest()
    with host._turn_lock(key):
        busy = client.post(base + "/turn", json={
            "client_request_id": "request-0001", "text": "合成"})
        assert busy.status_code == 409
        assert busy.json()["error"]["code"] == "CODEX_BUSY"
    service.db.execute("UPDATE conversations SET active_run_id='synthetic-run' WHERE id=?",
                       (cid,))
    assert client.get(base).status_code == 409
    assert client.get(base + "/history").json()["error"]["code"] == "CODEX_SESSION_BUSY"
    assert_steering_denied(client, base, 409)
    service.db.execute("UPDATE conversations SET active_run_id=NULL, archived=1 WHERE id=?",
                       (cid,))
    assert client.get(base).status_code == 409
    assert client.get(base + "/history").json()["error"]["code"] == "CODEX_SESSION_BUSY"
    assert_steering_denied(client, base, 409)


def test_steering_requires_explicit_text_and_original_turn(codex_env):
    client, _, _ = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/codex"
    turn = client.post(base + "/turn", json={
        "client_request_id": "original-001", "text": "原题", "prepare_if_needed": True}).json()
    body = {"client_request_id": "adjustment-01", "turn_id": turn["turn_id"], "text": "只看上个月"}
    for extra in ({"file_ids": ["ordinary-upload"]}, {"prepare_if_needed": True}, {"model": "another-model"}):
        assert client.post(base + "/steer", json={**body, **extra}).status_code == 422
    assert client.post(base + "/steer", json={k: v for k, v in body.items() if k != "turn_id"}).status_code == 422
    assert client.get(base + "/steers").json() == {"receipts": []}
    late = client.post(base + "/steer", json=body)
    assert late.status_code == 200
    expected = {"turn_id": turn["turn_id"], "client_request_id": "adjustment-01", "state": "closed"}
    assert late.json() == expected
    history = client.get(base + "/steers")
    assert history.headers["cache-control"] == late.headers["cache-control"] == "no-store"
    assert history.json() == {"receipts": [expected]}
    assert client.get(base).json()["last_turn"]["turn_id"] == turn["turn_id"]


def test_feature_errors_are_not_leaked(codex_env):
    client, _service, _host = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/codex"
    # A turn before thread preparation is a closed-state conflict, not a
    # server path or binary detail.
    response = client.post(base + "/turn", json={
        "client_request_id": "request-0001", "text": "合成"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CODEX_CONFLICT"
    assert "app-server" not in response.text

def test_ordinary_feature_switch_cannot_launch_unreviewed_real_host(tmp_path):
    with pytest.raises(ValueError, match="explicitly provisioned isolated host"):
        create_app(settings_for(tmp_path), runtime_factory=FakeRuntime)


def test_first_turn_requires_explicit_preparation_consent(codex_env):
    client, _, _ = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/codex"
    body = {"client_request_id": "first-request", "text": "只读请求"}
    assert client.post(base + "/turn", json=body).status_code == 409
    assert not client.get(base).json()["bound"]
    result = client.post(base + "/turn", json={**body, "prepare_if_needed": True})
    assert result.status_code == 200, result.text
    assert client.get(base).json()["last_turn"]["turn_id"] == result.json()["turn_id"]


def test_owner_finds_older_receipt_without_input_or_replay(codex_env, monkeypatch):
    from app import codex_appserver

    client, _, host = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}/codex"
    assert client.get(base + "/requests/first-request").json()["state"] == "not_found"
    monkeypatch.setattr(codex_appserver, "RECENT_REQUESTS", 2)
    for req in ("first-request", "second-request"):
        result = client.post(base + "/turn", json={
            "client_request_id": req, "text": "合成短任务", "prepare_if_needed": True})
        assert result.status_code == 200
        if req == "first-request":
            first = result.json()
    # No native operation is needed to recover a previously completed result.
    with monkeypatch.context() as patch:
        patch.setattr(host, "_verify_executable", lambda: pytest.fail("unexpected native operation"))
        result = client.get(base + "/requests/first-request")
        assert result.headers["cache-control"] == "no-store"
        assert result.json() == {
            "state": "recorded", "request_id": "first-request",
            "turn": {"request_id": "first-request", "turn_id": first["turn_id"],
                     "text": first["text"], "text_truncated": False, "status": "completed",
                     "file_reads": {"succeeded": 0, "failed": 0, "unconfirmed": 0}}}
        assert client.get(base).json()["last_turn"]["request_id"] == "second-request"
        other = conversation(client)["id"]
        assert client.get(f"/api/v1/conversations/{other}/codex/requests/first-request").json()["state"] == "not_found"
    assert client.post(base + "/turn", json={"client_request_id": "third-request", "text": "合成"}).status_code == 200
    assert client.get(base + "/requests/first-request").json() == {
        "state": "admitted_without_receipt", "request_id": "first-request", "turn": None}


def test_history_reopens_without_request_ids_messages_runs_or_native_calls(codex_env, monkeypatch):
    from app import codex_appserver

    client, service, host = codex_env
    cid = conversation(client)["id"]
    base = f"/api/v1/conversations/{cid}"
    expected = []
    for req in ("receipt-alpha", "receipt-omega"):
        response = client.post(base + "/codex/turn", json={
            "client_request_id": req, "text": "合成", "prepare_if_needed": True})
        assert response.status_code == 200, response.text
        turn = response.json()
        expected.insert(0, {"request_id": req, "turn_id": turn["turn_id"], "text": "合成回复",
                            "text_truncated": False, "status": "completed",
                            "file_reads": {"succeeded": 0, "failed": 0, "unconfirmed": 0}})

    other = conversation(client)["id"]
    monkeypatch.setattr(codex_appserver.subprocess, "Popen", lambda *a, **kw: pytest.fail("native launch"))
    for _ in range(2):
        response = client.get(base + "/codex/history")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json() == {"recent_limit": 8, "receipts": expected,
                                   "unavailable_count": 0, "pending": False}
    window = client.get(base + "/messages", params={"limit": 20}).json()
    assert window["messages"] == [] and window["latest_run_id"] is None
    assert service.db.one("SELECT COUNT(*) AS n FROM runs WHERE conversation_id=?", (cid,))["n"] == 0
    assert client.get(f"/api/v1/conversations/{other}/codex/history").json()["receipts"] == []

    def unavailable(*args):
        raise codex_appserver.CodexHostUnavailable("/private/host/path synthetic-private-value")
    monkeypatch.setattr(host, "_load", unavailable)
    failed = client.get(base + "/codex/history")
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "CODEX_UNAVAILABLE"
    assert "/private" not in failed.text and "synthetic-private-value" not in failed.text
