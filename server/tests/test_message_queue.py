"""Persistent drafts, FIFO dispatch and lost-response recovery without paid calls."""
import concurrent.futures
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.message_queue import MessageQueue
from app.runtime import RunManager
from test_api import FakeRuntime, PASSWORD, client, conversation, login, poll, send  # noqa: F401


@pytest.fixture
def configured(tmp_path):
    return Settings(data_dir=tmp_path, provider_settings="", runtime_type="claude", origins=("http://testserver",))


def add(client, cid, identity, text="queued message"):
    response = client.post(f"/api/v1/conversations/{cid}/queue", json={"client_request_id": identity, "text": text})
    assert response.status_code == 201, response.text
    return response.json()


def dispatch(client, cid, item, after=None, resume=False):
    return client.post(f"/api/v1/conversations/{cid}/queue/{item['id']}/dispatch", json={"revision": item["revision"], "after_run_id": after, "resume": resume})


def test_host_advances_fifo_without_a_browser_dispatch(client):
    cid = conversation(client)["id"]
    first = send(client, cid, "approval", "running-before-background-queue")["run_id"]
    running = poll(client, first, {"awaiting_approval"})
    queued = add(client, cid, "background-queued-1", "before edit")
    cancelled = add(client, cid, "background-queued-2", "never dispatch")
    last = add(client, cid, "background-queued-3", "after edited job")
    assert client.patch(f"/api/v1/conversations/{cid}/queue/{queued['id']}", json={"revision": 1, "text": "after edit"}).status_code == 200
    assert client.post(f"/api/v1/conversations/{cid}/queue/{cancelled['id']}/cancel", json={"revision": 1}).status_code == 200
    approval = running["approvals"][0]
    assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"}).status_code == 200
    # The browser may close after approval. No client request drives dispatch.
    service = client.app.state.service
    deadline = time.monotonic() + 8
    while True:
        runs = service.db.all("SELECT client_request_id,status,message_id FROM runs WHERE conversation_id=? ORDER BY rowid", (cid,))
        if len(runs) == 3 and runs[-1]["status"] == "succeeded":
            break
        assert time.monotonic() < deadline, runs
        time.sleep(0.05)
    assert [row["client_request_id"] for row in runs] == ["running-before-background-queue", queued["id"], last["id"]]
    assert service.require("messages", runs[1]["message_id"])["text"] == "after edit"


def test_startup_reconciles_a_pending_head_after_a_durable_success(configured, monkeypatch):
    with monkeypatch.context() as first_process:
        first_process.setattr(RunManager, "start_queue_worker", lambda _manager: None)
        with TestClient(create_app(configured, FakeRuntime)) as client:
            service = client.app.state.service
            service.create_user({"username": "admin", "display_name": "Queue", "password": PASSWORD, "role": "admin"})
            login(client)
            cid = conversation(client)["id"]
            predecessor = send(client, cid, "successful predecessor", "queue-restart-precedent")["run_id"]
            assert poll(client, predecessor)["status"] == "succeeded"
            item = add(client, cid, "queue-restart-pending", "pending between processes")
    with TestClient(create_app(configured, FakeRuntime)) as client:
        service = client.app.state.service
        deadline = time.monotonic() + 6
        while True:
            run = service.db.one("SELECT status FROM runs WHERE conversation_id=? AND client_request_id=?", (cid, item["id"]))
            if run and run["status"] == "succeeded":
                break
            assert time.monotonic() < deadline, run
            time.sleep(0.05)
        assert service.db.one("SELECT status FROM message_queue WHERE conversation_id=? AND id=?", (cid, item["id"])) == {"status": "accepted"}


def test_fifo_edit_cancel_and_duplicate_dispatch(client):
    cid = conversation(client)["id"]
    first, second, third = (add(client, cid, f"queue-test-{i}", str(i)) for i in range(3))
    assert dispatch(client, cid, second).json()["status"] == "changed"
    edited = client.patch(f"/api/v1/conversations/{cid}/queue/{first['id']}", json={"revision": 1, "text": "edited first"})
    assert edited.status_code == 200
    first = edited.json()["items"][0]
    assert first["revision"] == 2
    assert client.patch(f"/api/v1/conversations/{cid}/queue/{first['id']}", json={"revision": 1, "text": "stale overwrite"}).status_code == 409
    assert client.post(f"/api/v1/conversations/{cid}/queue/{second['id']}/cancel", json={"revision": 1}).status_code == 200
    accepted = dispatch(client, cid, first).json()
    assert accepted["status"] == "accepted"
    assert poll(client, accepted["run_id"])["status"] == "succeeded"
    repeated = dispatch(client, cid, first).json()
    assert repeated["run_id"] == accepted["run_id"]
    assert client.app.state.service.db.one("SELECT COUNT(*) n FROM runs WHERE conversation_id=?", (cid,))["n"] == 1
    assert dispatch(client, cid, third).json()["status"] == "waiting"
    assert dispatch(client, cid, third, accepted["run_id"]).json()["status"] == "accepted"


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_failed_or_stopped_predecessor_pauses_until_explicit_continue(client, status):
    cid = conversation(client)["id"]
    first = add(client, cid, "queue-before-failure")
    accepted = dispatch(client, cid, first).json()
    poll(client, accepted["run_id"])
    client.app.state.service.db.execute("UPDATE runs SET status=? WHERE id=?", (status, accepted["run_id"]))
    second = add(client, cid, "queue-after-failure")
    assert dispatch(client, cid, second, accepted["run_id"]).json()["status"] == "paused"
    resumed = dispatch(client, cid, second, accepted["run_id"], resume=True).json()
    assert resumed["status"] == "accepted" and resumed["run_id"] != accepted["run_id"]
    assert dispatch(client, cid, first).json()["run_id"] == accepted["run_id"]


def test_lost_acceptance_and_two_tabs_use_one_run_and_keep_drafts(client):
    cid = conversation(client)["id"]
    item = add(client, cid, "queue-concurrent-tabs")
    assert add(client, cid, item["id"])["id"] == item["id"]
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    def accept():
        return MessageQueue(service).dispatch(cid, item["id"], {"revision": 1, "after_run_id": None, "resume": False}, user)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        left, right = list(executor.map(lambda _: accept(), range(2)))
    assert left["run_id"] == right["run_id"]
    # Simulate interruption between run commit and the queue acceptance write.
    service.db.execute("UPDATE message_queue SET status='pending',run_id=NULL WHERE conversation_id=?", (cid,))
    recovered = client.get(f"/api/v1/conversations/{cid}/queue").json()
    assert recovered["items"] == [] and recovered["latest_run"]["id"] == left["run_id"]
    repeated = dispatch(client, cid, item).json()
    assert repeated["run_id"] == left["run_id"]
    assert poll(client, repeated["run_id"])["status"] == "succeeded"
    assert service.db.one("SELECT COUNT(*) n FROM runs WHERE conversation_id=?", (cid,))["n"] == 1
    assert client.post(f"/api/v1/conversations/{cid}/queue/{item['id']}/cancel", json={"revision": 1}).status_code == 409


@pytest.mark.parametrize("role", ["member", "admin"])
def test_queue_is_private_and_bound_to_conversation(client, role):
    cid = conversation(client)["id"]
    item = add(client, cid, "queue-private-item")
    client.post("/api/v1/users", json={"username": "other-user", "display_name": "Other", "password": PASSWORD, "role": role})
    login(client, "other-user")
    assert client.get(f"/api/v1/conversations/{cid}/queue").status_code == 404
    assert dispatch(client, cid, item).status_code == 404
    assert client.post(f"/api/v1/conversations/{cid}/queue/{item['id']}/cancel", json={"revision": 1}).status_code == 404


def test_cancelled_draft_cannot_be_reintroduced_by_lost_add_response(client):
    cid = conversation(client)["id"]
    item = add(client, cid, "queue-cancelled-item")
    client.post(f"/api/v1/conversations/{cid}/queue/{item['id']}/cancel", json={"revision": 1})
    assert add(client, cid, item["id"])["status"] == "cancelled"
    assert client.get(f"/api/v1/conversations/{cid}/queue").json()["items"] == []


def test_due_schedule_runs_once_and_cancelled_schedule_stays_cancelled(client):
    from datetime import datetime, timedelta, timezone

    cid = conversation(client)["id"]
    route = f"/api/v1/conversations/{cid}/schedules"
    stamp = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    scheduled = client.post(route, json={"text": "scheduled message", "scheduled_at": stamp})
    cancelled = client.post(route, json={"text": "cancelled message", "scheduled_at": stamp})
    assert scheduled.status_code == cancelled.status_code == 201
    identity, cancelled_id = scheduled.json()["id"], cancelled.json()["id"]
    assert client.delete(f"{route}/{cancelled_id}").status_code == 204

    service = client.app.state.service
    service.db.execute("UPDATE conversation_schedules SET scheduled_at=? WHERE conversation_id=?",
                       ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), cid))
    deadline = time.monotonic() + 8
    while True:
        schedules = {item["id"]: item for item in client.get(route).json()}
        if schedules[identity]["status"] == "succeeded":
            break
        assert time.monotonic() < deadline, schedules
        time.sleep(0.05)
    assert schedules[cancelled_id]["status"] == "cancelled"
    runs = service.db.all("SELECT client_request_id,execution_meta FROM runs WHERE conversation_id=?", (cid,))
    assert [run["client_request_id"] for run in runs] == [identity]
    assert json.loads(runs[0]["execution_meta"])["schedule_id"] == identity
