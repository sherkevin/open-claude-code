import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import AppError


@pytest.fixture
def memory_app(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="test"))
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "admin"})
        assistant = service.save_assistant({"name": "Memory", "connections": [], "access": {"all_members": True, "user_ids": []}})
        assert client.post("/api/v1/auth/login", json={"username": "owner", "password": "test-password-123"}).status_code == 200
        yield client, service, user, assistant


def test_memory_edit_revision_clear_and_history_preservation(memory_app):
    client, service, user, assistant = memory_app
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "History remains"}, user)
    url = f"/api/v1/conversations/{conversation['id']}/memory"
    initial = client.get(url).json()
    assert initial["files"] == [] and initial["revision"] == 0
    assert initial["scope"] == "conversation" and initial["conversation_id"] == conversation["id"]
    saved = client.put(url + "/file", json={"path": "MEMORY.md", "content": "项目代号：青松", "revision": 0})
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert client.get(url + "/file", params={"path": "MEMORY.md"}).json()["content"] == "项目代号：青松"
    stale = client.put(url + "/file", json={"path": "MEMORY.md", "content": "stale", "revision": 0})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "MEMORY_REVISION_CONFLICT"
    clear = client.delete(url, params={"revision": 1})
    assert clear.status_code == 200 and clear.json()["files"] == [] and clear.json()["revision"] == 2
    assert client.get(f"/api/v1/conversations/{conversation['id']}").status_code == 200


def test_concurrent_first_memory_reads_share_one_revision(memory_app, monkeypatch):
    _, service, user, assistant = memory_app
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Concurrent reads"}, user)
    root = service.memory.root(user["id"], conversation["id"])
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text("Native update", encoding="utf-8")
    original = service.db.one
    first_selected = threading.Event()
    second_started = threading.Event()
    first_thread = [None]

    def delayed_select(query, values=()):
        row = original(query, values)
        if query.startswith("SELECT * FROM conversation_memory_scopes") and row["revision"] == 0 and threading.get_ident() == first_thread[0]:
            first_selected.set()
            assert second_started.wait(2)
            # Only one reader should account for the native file change.
            time.sleep(0.1)
        return row

    monkeypatch.setattr(service.db, "one", delayed_select)

    def first():
        first_thread[0] = threading.get_ident()
        return service.memory.refresh(user["id"], conversation["id"])

    def second():
        assert first_selected.wait(2)
        second_started.set()
        return service.memory.refresh(user["id"], conversation["id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first)
        two = pool.submit(second)
        assert one.result(timeout=5)["revision"] == two.result(timeout=5)["revision"] == 1
    assert len(service.db.all("SELECT * FROM conversation_memory_scopes")) == 1


def test_memory_user_assistant_isolation_and_scope_guards(memory_app):
    client, service, user, assistant = memory_app
    other = service.create_user({"username": "other", "display_name": "Other", "password": "test-password-123", "role": "member"})
    second = service.save_assistant({"name": "Second", "connections": []})
    a = service.create_conversation({"assistant_id": assistant["id"], "title": "A"}, user)
    b = service.create_conversation({"assistant_id": assistant["id"], "title": "B"}, user)
    own = service.memory.root(user["id"], a["id"])
    own.mkdir(parents=True)
    (own / "MEMORY.md").write_text("owner-only", encoding="utf-8")
    assert service.authorize_tool(a["id"], "Read", {"file_path": str(own / "MEMORY.md")})
    assert service.authorize_tool(a["id"], "Glob", {"path": str(own), "pattern": "*.md"})
    with pytest.raises(AppError):
        service.authorize_tool(b["id"], "Read", {"file_path": str(own / "MEMORY.md")})
    for path in (service.memory.root(other["id"], a["id"]) / "MEMORY.md", service.memory.root(user["id"], b["id"]) / "MEMORY.md", service.memory.legacy_root(user["id"], assistant["id"]) / "MEMORY.md", own / ".hidden.md", service.settings.data_dir / "sdk-config" / a["id"] / "settings.json", service.settings.data_dir / "workspaces" / b["id"] / "secret.txt"):
        with pytest.raises(AppError):
            service.authorize_tool(a["id"], "Read", {"file_path": str(path)})
    client.post("/api/v1/auth/login", json={"username": "other", "password": "test-password-123"})
    url = f"/api/v1/conversations/{a['id']}/memory"
    assert client.get(url).status_code == 404
    assert client.get(url + "/file", params={"path": "MEMORY.md"}).status_code == 404
    assert client.get(url + "/file", params={"path": str(own / "MEMORY.md")}).status_code == 404


def test_active_scope_blocks_mutation_and_refreshes_after_native_write(memory_app):
    client, service, user, assistant = memory_app
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "A"}, user)
    other = service.create_conversation({"assistant_id": assistant["id"], "title": "B"}, user)
    created, _ = service.prepare_run(conversation["id"], {"text": "native turn", "file_ids": [], "client_request_id": "native-memory"}, user)
    before = service.runtime_config(conversation["id"])["fingerprint"]
    root = service.memory.root(user["id"], conversation["id"])
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text("Native note", encoding="utf-8")
    url = f"/api/v1/conversations/{conversation['id']}/memory"
    view = client.get(url).json()
    assert view["busy"] and view["revision"] == 0
    assert service.runtime_config(conversation["id"])["fingerprint"] == before
    blocked = client.delete(url, params={"revision": 0})
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "MEMORY_BUSY"
    parallel, _ = service.prepare_run(other["id"], {"text": "concurrent", "file_ids": [], "client_request_id": "parallel"}, user)
    assert parallel["run_id"] != created["run_id"]
    with pytest.raises(AppError) as caught:
        service.prepare_run(conversation["id"], {"text": "duplicate", "file_ids": [], "client_request_id": "conflict"}, user)
    assert caught.value.code == "CONVERSATION_BUSY"
    service.manager.finish(parallel["run_id"], "succeeded")
    service.manager.finish(created["run_id"], "succeeded")
    assert service.runtime_config(conversation["id"])["fingerprint"] != before
    assert client.get(url).json()["revision"] == 1


@pytest.mark.parametrize("path", ["../escape.md", ".private.md", "notes/../../escape.md", "notes/data.json", "notes/.private/secret.md"])
def test_memory_file_api_rejects_unsafe_paths(memory_app, path):
    client, service, user, assistant = memory_app
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Paths"}, user)
    response = client.put(f"/api/v1/conversations/{conversation['id']}/memory/file", json={"path": path, "content": "blocked", "revision": 0})
    assert response.status_code == 403


def test_memory_size_and_symlink_guards(memory_app):
    client, service, user, assistant = memory_app
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Size"}, user)
    url = f"/api/v1/conversations/{conversation['id']}/memory/file"
    assert client.put(url, json={"path": "MEMORY.md", "content": "汉" * 50000, "revision": 0}).status_code == 413
    root = service.memory.root(user["id"], conversation["id"])
    root.mkdir(parents=True)
    target = service.settings.data_dir / "outside.md"
    target.write_text("private", encoding="utf-8")
    try:
        (root / "linked.md").symlink_to(target)
    except OSError:
        pytest.skip("OS does not permit this test user to create symbolic links")
    assert client.get(url, params={"path": "linked.md"}).status_code == 403
