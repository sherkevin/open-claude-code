"""Persistent drafts, FIFO dispatch and lost-response recovery without paid calls."""
import concurrent.futures

import pytest

from app.config import Settings
from app.message_queue import MessageQueue
from test_api import PASSWORD, client, conversation, login, poll  # noqa: F401


@pytest.fixture
def configured(tmp_path):
    return Settings(data_dir=tmp_path, provider_settings="", runtime_type="claude", origins=("http://testserver",))


def add(client, cid, identity, text="queued message"):
    response = client.post(f"/api/v1/conversations/{cid}/queue", json={"client_request_id": identity, "text": text})
    assert response.status_code == 201, response.text
    return response.json()


def dispatch(client, cid, item, after=None, resume=False):
    return client.post(f"/api/v1/conversations/{cid}/queue/{item['id']}/dispatch", json={"revision": item["revision"], "after_run_id": after, "resume": resume})


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
