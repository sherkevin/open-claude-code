import json
import time

import pytest

from app.db import Database, dump
from app.memory import MAX_FILE
from app.security import AppError
from test_api import client, configured, send, poll


def sessions(client):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    assistant = client.post("/api/v1/assistants", json={"name": "Concurrent assistant"}).json()
    values = [client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "title": title}).json() for title in ("A", "B")]
    return service, user, assistant, values[0], values[1]


def test_same_assistant_sessions_run_in_parallel_and_approvals_cancel_independently(client):
    service, _, _, a, b = sessions(client)
    service.settings.approval_seconds = 30
    first = send(client, a["id"], "approval", "parallel-a")
    first_approval = poll(client, first["run_id"], {"awaiting_approval"})["approvals"][0]
    # B completes an ordinary task while A is still awaiting approval.
    second = send(client, b["id"], "business task", "parallel-b")
    assert poll(client, second["run_id"])["status"] == "succeeded"
    assert client.get(f"/api/v1/runs/{first['run_id']}").json()["status"] == "awaiting_approval"
    held = send(client, b["id"], "approval", "parallel-b-held")
    second_approval = poll(client, held["run_id"], {"awaiting_approval"})["approvals"][0]
    assert first_approval["id"] != second_approval["id"]
    duplicate = client.post(f"/api/v1/conversations/{b['id']}/messages", json={"text": "duplicate", "client_request_id": "distinct-duplicate"})
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "CONVERSATION_BUSY"
    client.post(f"/api/v1/runs/{first['run_id']}/cancel")
    assert poll(client, first["run_id"])["status"] == "cancelled"
    still_waiting = client.get(f"/api/v1/runs/{held['run_id']}").json()
    assert still_waiting["status"] == "awaiting_approval"
    assert still_waiting["approvals"][0]["status"] == "pending"
    assert client.post(f"/api/v1/approvals/{second_approval['id']}/decision", json={"decision": "allow"}).status_code == 200
    assert poll(client, held["run_id"])["status"] == "succeeded"
    assert client.post(f"/api/v1/approvals/{first_approval['id']}/decision", json={"decision": "allow"}).status_code == 409
    a_messages = client.get(f"/api/v1/conversations/{a['id']}/messages").json()
    b_messages = client.get(f"/api/v1/conversations/{b['id']}/messages").json()
    assert all(row["conversation_id"] == a["id"] for row in a_messages)
    assert any(row["text"] == "已执行" for row in b_messages)
    assert not any(row["text"] == "已执行" for row in a_messages)


def test_session_memory_revision_fingerprint_context_and_context_busy_are_isolated(client):
    service, user, _, a, b = sessions(client)
    urls = [f"/api/v1/conversations/{value['id']}/memory" for value in (a, b)]
    for key, url, text in zip(("a-initial", "b-initial"), urls, ("Only session A", "Only session B")):
        assert client.put(url + "/file", json={"path": "MEMORY.md", "content": text, "revision": 0,
                                              "operation_id": key}).status_code == 200
    first_config, second_config = [service.runtime_config(value["id"]) for value in (a, b)]
    assert first_config["memory_dir"] != second_config["memory_dir"]
    assert first_config["fingerprint"] != second_config["fingerprint"]
    for value in (a, b):
        service.db.execute("UPDATE conversations SET context_usage=? WHERE id=?", (dump({"session": value["id"]}), value["id"]))
    assert client.put(urls[0] + "/file", json={"path": "MEMORY.md", "content": "A revised", "revision": 1,
                                              "operation_id": "a-revised"}).status_code == 200
    assert service.require("conversations", a["id"])["context_usage"] is None
    assert json.loads(service.require("conversations", b["id"])["context_usage"])["session"] == b["id"]
    assert service.runtime_config(b["id"])["fingerprint"] == second_config["fingerprint"]
    assert client.get(urls[1] + "/file", params={"path": "MEMORY.md"}).json()["content"] == "Only session B"
    service.manager.context_requests[a["id"]] = None
    try:
        blocked = client.delete(urls[0], params={"revision": 2, "operation_id": "a-clear-blocked"})
        assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "MEMORY_BUSY"
        assert client.put(urls[1] + "/file", json={"path": "MEMORY.md", "content": "B while A measures", "revision": 1,
                                                  "operation_id": "b-while-a-measures"}).status_code == 200
        with pytest.raises(AppError) as busy:
            service.prepare_run(a["id"], {"text": "same session", "file_ids": [], "client_request_id": "context-conflict"}, user)
        assert busy.value.code == "CONTEXT_BUSY"
        run = send(client, b["id"], "B while A measures", "context-parallel")
        assert poll(client, run["run_id"])["status"] == "succeeded"
    finally:
        service.manager.context_requests.pop(a["id"], None)
    # A running task no longer blocks context refresh of another conversation.
    run = send(client, a["id"], "wait", "a-busy-run")
    poll(client, run["run_id"], {"running"})
    service.db.execute("UPDATE conversations SET context_usage=NULL WHERE id=?", (b["id"],))
    assert client.post(f"/api/v1/conversations/{b['id']}/context/refresh").status_code == 200
    assert client.post(f"/api/v1/conversations/{a['id']}/context/refresh").status_code == 409
    client.post(f"/api/v1/runs/{run['run_id']}/cancel")
    poll(client, run["run_id"])


def test_legacy_snapshot_migration_is_once_per_existing_session_and_new_sessions_start_empty(client):
    service, user, assistant, a, b = sessions(client)
    legacy = service.memory.legacy_root(user["id"], assistant["id"])
    legacy.mkdir(parents=True)
    (legacy / "MEMORY.md").write_text("Preserved shared original", encoding="utf-8")
    # Emulate the schema before 0.4.2, then execute the real startup backfill.
    service.db.execute("DELETE FROM conversation_memory_scopes")
    reopened = Database(service.settings.data_dir / "app.sqlite3")
    assert {row["origin"] for row in reopened.all("SELECT * FROM conversation_memory_scopes")} == {"legacy_pending"}
    reopened.close()
    for value in (a, b):
        view = service.memory.view(user, value["id"])
        assert view["origin"] == "legacy_snapshot"
        assert service.memory.read(user, value["id"], "MEMORY.md")["content"] == "Preserved shared original"
    service.memory.edit(user, a["id"], 0, "MEMORY.md", "A only", operation_id="legacy-a-upsert")
    assert service.memory.read(user, b["id"], "MEMORY.md")["content"] == "Preserved shared original"
    assert (legacy / "MEMORY.md").read_text(encoding="utf-8") == "Preserved shared original"
    # Recovery after directory publication but before marker commit reuses the
    # existing snapshot, instead of overwriting it from the shared original.
    service.db.execute("UPDATE conversation_memory_scopes SET origin='legacy_pending' WHERE conversation_id=?", (a["id"],))
    assert service.memory.read(user, a["id"], "MEMORY.md")["content"] == "A only"
    service.memory.edit(user, a["id"], service.memory.view(user, a["id"])["revision"],
                        operation_id="legacy-a-clear")
    new = service.create_conversation({"assistant_id": assistant["id"], "title": "New empty"}, user)
    reopened = Database(service.settings.data_dir / "app.sqlite3")
    reopened.close()
    assert service.memory.view(user, a["id"])["files"] == []
    assert service.memory.view(user, new["id"])["files"] == []
    assert service.memory.view(user, new["id"])["origin"] == "empty"
    legacy_url = f"/api/v1/assistants/{assistant['id']}/memory"
    assert client.get(legacy_url).json()["read_only"] is True
    assert client.get(legacy_url + "/file", params={"path": "MEMORY.md"}).json()["content"] == "Preserved shared original"
    for response in (client.put(legacy_url + "/file", json={"path": "MEMORY.md", "content": "overwrite", "revision": 0}), client.delete(legacy_url, params={"revision": 0})):
        assert response.status_code == 403 and response.json()["error"]["code"] == "MEMORY_LEGACY_READ_ONLY"
    assert (legacy / "MEMORY.md").read_text(encoding="utf-8") == "Preserved shared original"


def test_invalid_legacy_memory_preserves_original_and_retries_only_that_session(client):
    service, user, assistant, a, b = sessions(client)
    legacy = service.memory.legacy_root(user["id"], assistant["id"])
    legacy.mkdir(parents=True)
    source = legacy / "MEMORY.md"
    source.write_bytes(b"x" * (MAX_FILE + 1))
    service.db.execute("UPDATE conversation_memory_scopes SET origin='legacy_pending' WHERE conversation_id=?", (a["id"],))
    with pytest.raises(AppError) as too_big:
        service.memory.view(user, a["id"])
    assert too_big.value.status == 413
    assert source.stat().st_size == MAX_FILE + 1
    scope = service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (a["id"],))
    assert scope["storage_version"] == 0 and scope["active_snapshot_id"] is None
    assert not (service.settings.data_dir / "conversation-memory-snapshots" / user["id"] / a["id"]).exists()
    assert service.memory.view(user, b["id"])["files"] == []
    source.write_text("Recovered source", encoding="utf-8")
    assert service.memory.read(user, a["id"], "MEMORY.md")["content"] == "Recovered source"


@pytest.mark.parametrize("text", ["你是谁？", "Read an ordinary business record"])
def test_queued_run_rechecks_disabled_owner_before_identity_or_model_execution(client, text):
    service, user, _, a, _ = sessions(client)
    created, _ = service.prepare_run(a["id"], {"text": text, "file_ids": [], "client_request_id": "disabled-owner-queued"}, user)
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    with pytest.raises(AppError) as disabled:
        service.runtime_config(a["id"])
    assert disabled.value.code == "USER_DISABLED"
    client.portal.call(service.manager.submit, created["run_id"])
    for _ in range(200):
        run = service.require("runs", created["run_id"])
        if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            break
        time.sleep(0.01)
    assert run["status"] == "failed" and run["error"] == "账号已停用"
    assert service.require("messages", run["assistant_message_id"])["text"] == ""
    assert service.manager.actors[a["id"]].runtime is None
