import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest

from app.db import Database
from app.security import AppError
from test_api import client, configured, login, PASSWORD, send, poll


def member(client, username):
    result = client.post("/api/v1/users", json={"username": username, "password": PASSWORD, "display_name": username})
    assert result.status_code == 201
    return result.json()


def test_member_owns_resources_admin_does_not_inherit_private_access(client):
    member(client, "alice")
    member(client, "bob")
    login(client, "alice")
    connection = client.post("/api/v1/connections", json={"name": "Alice database", "url": "http://localhost/mcp", "credential": "alice-secret", "auth_type": "bearer"}).json()
    assert connection["scope"] == "private" and connection["owner_id"] == client.app.state.service.db.one("SELECT id FROM users WHERE username='alice'")["id"]
    skill = client.post("/api/v1/skills", json={"name": "Alice process", "instructions": "Read quality records."}).json()
    assistant = client.post("/api/v1/assistants", json={"name": "Alice assistant", "skill_ids": [skill["id"]], "connections": [{"connection_id": connection["id"]}]}).json()
    assert assistant["can_edit"] and assistant["scope"] == "private"
    assert connection["owner_id"] == assistant["owner_id"]
    assert client.post("/api/v1/assistants", json={"name": "Forbidden shared", "scope": "team"}).status_code == 403
    # Members can create private connections, but team connections remain
    # administrator-managed and must be rejected at the API boundary.
    forbidden_team = client.post("/api/v1/connections", json={"name": "Forbidden shared connection", "url": "http://localhost/mcp", "scope": "team"})
    assert forbidden_team.status_code == 403
    assert forbidden_team.json()["error"]["code"] == "ADMIN_REQUIRED"
    for other in ("bob", "admin"):
        login(client, other)
        for kind, obj in (("connections", connection), ("skills", skill), ("assistants", assistant)):
            assert obj["id"] not in {r["id"] for r in client.get(f"/api/v1/{kind}").json()}
            assert client.get(f"/api/v1/{kind}/{obj['id']}").status_code == 404
            assert client.patch(f"/api/v1/{kind}/{obj['id']}", json={"name": "stolen"}).status_code == 404
        assert client.post("/api/v1/assistants", json={"name": "Steal skill", "skill_ids": [skill["id"]]}).status_code == 404
    login(client, "alice")
    assert client.patch(f"/api/v1/assistants/{assistant['id']}", json={"enabled": False}).status_code == 200
    assert not client.get("/api/v1/assistants").json()
    assert client.get("/api/v1/assistants?manage=true").json()[0]["can_edit"]


def test_shared_configuration_can_be_used_but_not_edited_and_cannot_capture_private(client):
    member(client, "alice")
    connection = client.post("/api/v1/connections", json={"name": "Team DB", "url": "http://localhost/mcp", "scope": "team"}).json()
    assistant = client.post("/api/v1/assistants", json={"name": "Team assistant", "scope": "team", "connections": [{"connection_id": connection["id"]}]}).json()
    private = client.post("/api/v1/skills", json={"name": "Private", "instructions": "private instructions"}).json()
    assert client.patch(f"/api/v1/assistants/{assistant['id']}", json={"skill_ids": [private["id"]]}).json()["error"]["code"] == "PRIVATE_DEPENDENCY"
    assert client.patch(f"/api/v1/connections/{connection['id']}", json={"scope": "private"}).status_code == 409
    login(client, "alice")
    assert client.get(f"/api/v1/connections/{connection['id']}").json()["can_edit"] is False
    assert client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).status_code == 201
    for kind, obj in (("connections", connection), ("assistants", assistant)):
        assert client.patch(f"/api/v1/{kind}/{obj['id']}", json={"name": "overwrite"}).status_code == 403
        assert client.delete(f"/api/v1/{kind}/{obj['id']}").status_code == 403
    personal = client.post("/api/v1/assistants", json={"name": "Personal team-reader", "connections": [{"connection_id": connection["id"]}]}).json()
    assert personal["scope"] == "private"


def test_session_search_classification_rename_archive_restore_and_immutable_binding(client):
    assistant = client.post("/api/v1/assistants", json={"name": "QA"}).json()
    a = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "title": "January", "category": "质量", "tags": ["客户A", "客户A"]}).json()
    b = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "title": "February"}).json()
    assert a["tags"] == ["客户A"]
    assert [row["id"] for row in client.get("/api/v1/conversations", params={"q": "客户A"}).json()] == [a["id"]]
    run = send(client, a["id"], "batch-732 tolerance 12%")
    assert poll(client, run["run_id"])["status"] == "succeeded"
    assert [row["id"] for row in client.get("/api/v1/conversations", params={"q": "batch-732", "assistant_id": assistant["id"], "category": "质量"}).json()] == [a["id"]]
    assert client.get("/api/v1/conversations", params={"q": "missing%"}).json() == []
    updated = client.patch(f"/api/v1/conversations/{a['id']}", json={"title": "审核完成", "archived": True, "category": "已完成", "tags": ["完成"]}).json()
    assert updated["archived"] and updated["title"] == "审核完成"
    assert [row["id"] for row in client.get("/api/v1/conversations?archived=true").json()] == [a["id"]]
    assert client.patch(f"/api/v1/conversations/{a['id']}", json={"archived": False}).json()["archived"] is False
    assert len(client.get(f"/api/v1/conversations/{a['id']}/messages").json()) == 2
    assert client.patch(f"/api/v1/conversations/{a['id']}", json={"assistant_id": "replacement"}).status_code == 422
    assert a["id"] != b["id"]
    member(client, "outsider")
    login(client, "outsider")
    assert client.get("/api/v1/conversations", params={"q": "batch-732"}).json() == []


def test_skills_are_versioned_and_snapshotted_at_submission_not_permission_escalation(client):
    imported = client.post("/api/v1/skills/import", json={"markdown": "---\nname: quality-report\ndescription: >\n  汇总并核对来源\nallowed-tools: [Read, Bash]\n---\n# Report\nUse available quality records."})
    assert imported.status_code == 201, imported.text
    skill = imported.json()
    assistant = client.post("/api/v1/assistants", json={"name": "QA", "skill_ids": [skill["id"]]}).json()
    convo = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
    assert skill["required_tools"] == ["Read", "Bash"]
    with pytest.raises(AppError) as rejected:
        client.app.state.service.authorize_tool(convo["id"], "Bash", {"command": "echo expanded-permissions"})
    assert rejected.value.code == "TOOL_DENIED"
    response = client.post(f"/api/v1/conversations/{convo['id']}/messages", json={"text": "wait", "client_request_id": "skill-snapshot", "skill_ids": [skill["id"]]})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert response.json()["message"]["skills"] == [{"id": skill["id"], "name": skill["name"], "revision": 1}]
    poll(client, run_id, {"running"})
    changed = client.patch(f"/api/v1/skills/{skill['id']}", json={"instructions": "Version two"}).json()
    assert changed["revision"] == 2
    snapshots = json.loads(client.app.state.service.require("runs", run_id)["skill_snapshots"])
    assert snapshots[0]["revision"] == 1 and "Version two" not in snapshots[0]["instructions"]
    assert [r["revision"] for r in client.get(f"/api/v1/skills/{skill['id']}/versions").json()] == [2, 1]
    assert client.get(f"/api/v1/assistants/{assistant['id']}/skills").json()[0]["revision"] == 2
    client.patch(f"/api/v1/assistants/{assistant['id']}", json={"skill_ids": []})
    with pytest.raises(AppError) as revoked:
        client.app.state.service.runtime_config(convo["id"])
    assert revoked.value.code == "SKILL_UNAVAILABLE"
    client.patch(f"/api/v1/assistants/{assistant['id']}", json={"skill_ids": [skill["id"]]})
    client.post(f"/api/v1/runs/{run_id}/cancel")
    poll(client, run_id)
    client.patch(f"/api/v1/skills/{skill['id']}", json={"enabled": False})
    denied = client.post(f"/api/v1/conversations/{convo['id']}/messages", json={"text": "run", "client_request_id": "skill-disabled", "skill_ids": [skill["id"]]})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "SKILL_UNAVAILABLE"
    assert len(client.get(f"/api/v1/conversations/{convo['id']}/messages").json()) == 2


def test_mcp_import_is_private_and_name_collision_does_not_leak_other_users(client):
    member(client, "alice")
    member(client, "bob")
    body = {"config": {"mcpServers": {"my source": {"url": "http://localhost/mcp"}}}}
    login(client, "alice")
    first = client.post("/api/v1/connections/import", json=body)
    assert first.status_code == 201
    assert client.post("/api/v1/connections/import", json=body).status_code == 409
    login(client, "bob")
    second = client.post("/api/v1/connections/import", json=body)
    assert second.status_code == 201
    assert first.json()["connections"][0]["owner_id"] != second.json()["connections"][0]["owner_id"]


def test_legacy_schema_migration_preserves_team_configs_and_history(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
CREATE TABLE connections(id TEXT PRIMARY KEY,name TEXT,url TEXT,auth_type TEXT,secret TEXT,enabled INTEGER,status TEXT,tools TEXT DEFAULT '[]',last_error TEXT,last_tested_at TEXT,revision INTEGER DEFAULT 1,deleted INTEGER DEFAULT 0);
CREATE TABLE assistants(id TEXT PRIMARY KEY,name TEXT,description TEXT,instructions TEXT,enabled INTEGER,access TEXT,connections TEXT,revision INTEGER DEFAULT 1,deleted INTEGER DEFAULT 0);
INSERT INTO connections(id,name,url,auth_type,enabled,status) VALUES('source','Legacy','http://localhost/mcp','none',1,'connected');
INSERT INTO assistants(id,name,description,instructions,enabled,access,connections) VALUES('a','Legacy','','retain instructions',1,'{"all_members":true,"user_ids":[]}','[]');
""")
    conn.close()
    db = Database(path)
    assert db.one("SELECT * FROM connections")["scope"] == "team"
    assert db.one("SELECT * FROM assistants")["instructions"] == "retain instructions"
    assert db.one("SELECT * FROM assistants")["knowledge_base_ids"] == "[]"
    db.close()
    again = Database(path)
    assert len(again.all("SELECT * FROM assistants")) == 1
    again.close()


def test_assistant_defaults_include_only_explicit_company_base_and_pin_original_version(client):
    company = client.post("/api/v1/knowledge-bases", json={"name": "Better AI", "scope": "team", "default_for_new_assistants": True}).json()
    business = client.post("/api/v1/knowledge-bases", json={"name": "Unassigned business", "scope": "team"}).json()
    upload = client.post(f"/api/v1/knowledge-bases/{company['id']}/documents", files={"file": ("company.md", "Better AI 提供工业智能助手。".encode(), "text/markdown")})
    assert upload.status_code == 201, upload.text
    doc = upload.json()
    model = client.get("/api/v1/models").json()[0]
    assistant = client.post("/api/v1/assistants", json={"name": "Configured", "default_model_id": model["id"]}).json()
    assert assistant["knowledge_base_ids"] == [company["id"]]
    assert business["id"] not in assistant["knowledge_base_ids"]
    optout = client.post("/api/v1/assistants", json={"name": "No KB", "knowledge_base_ids": []}).json()
    assert optout["knowledge_base_ids"] == []
    convo = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
    assert convo["model_id"] == model["id"]
    run = send(client, convo["id"], "wait", "pin-document-version")
    poll(client, run["run_id"], {"running"})
    version = client.post(f"/api/v1/knowledge-bases/{company['id']}/documents/{doc['id']}/versions", files={"file": ("company.md", b"Version two replaces company content", "text/markdown")})
    assert version.status_code == 201, version.text
    service = client.app.state.service
    pinned = service.db.one("SELECT * FROM knowledge_run_documents WHERE run_id=? AND document_id=?", (run["run_id"], doc["id"]))
    assert pinned["version"] == 1
    client.post(f"/api/v1/runs/{run['run_id']}/cancel")
    poll(client, run["run_id"])


def test_prepared_run_pins_mcp_revision_without_recording_credentials(client):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    connection = service.save_connection({"name": "Source", "url": "http://localhost/mcp", "auth_type": "bearer", "credential": "not-in-snapshot"}, user=user)
    assistant = service.save_assistant({"name": "Reader", "connections": [{"connection_id": connection["id"], "allowed_tools": [], "auto_approved_tools": []}]}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "queued"}, user)
    result, _ = service.prepare_run(conversation["id"], {"client_request_id": "pinned-connection-config", "text": "read", "file_ids": []}, user)
    row = service.require("runs", result["run_id"])
    assert "not-in-snapshot" not in row["capability_snapshot"]
    assert json.loads(row["capability_snapshot"])["connection_revisions"][connection["id"]] == 1
    service.save_connection({"url": "http://localhost/changed", "credential": "new-secret"}, connection["id"], user=user)
    with pytest.raises(AppError) as changed:
        service.runtime_config(conversation["id"])
    assert changed.value.code == "CONFIG_CHANGED"
    service.manager.finish(result["run_id"], "cancelled")


def test_assistant_patch_preserves_omitted_fields_and_rejects_stale_revision(client):
    connection = client.post("/api/v1/connections", json={"name": "Source", "url": "http://localhost/mcp"}).json()
    knowledge = client.post("/api/v1/knowledge-bases", json={"name": "Knowledge"}).json()
    skill = client.post("/api/v1/skills", json={"name": "Procedure", "instructions": "Read the authorized source."}).json()
    model = client.get("/api/v1/models").json()[0]
    original = client.post("/api/v1/assistants", json={
        "name": "Original", "description": "Keep description", "instructions": "Keep instructions",
        "connections": [{"connection_id": connection["id"]}],
        "knowledge_base_ids": [knowledge["id"]], "skill_ids": [skill["id"]],
        "web_search_enabled": True, "default_model_id": model["id"],
        "default_reasoning_level": model["default_reasoning_level"],
    }).json()
    endpoint = f"/api/v1/assistants/{original['id']}"
    renamed = client.patch(endpoint, json={"name": "Renamed", "expected_revision": original["revision"]})
    assert renamed.status_code == 200, renamed.text
    renamed = renamed.json()
    assert renamed["revision"] == original["revision"] + 1
    for field in ("description", "instructions", "connections", "knowledge_base_ids", "skill_ids", "web_search_enabled", "default_model_id", "default_reasoning_level", "access"):
        assert renamed[field] == original[field]
    stale = client.patch(endpoint, json={"instructions": "Stale overwrite", "expected_revision": original["revision"]})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "ASSISTANT_CONFLICT"
    assert client.get(endpoint).json() == renamed
    assert client.patch(endpoint, json={"name": "Invalid", "expected_revision": 0}).status_code == 422
    # Existing callers may omit expected_revision; null model defaults and
    # explicit empty capability lists retain their distinct PATCH meanings.
    legacy = client.patch(endpoint, json={"description": "Legacy edit", "default_model_id": None, "default_reasoning_level": None, "skill_ids": []})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["default_model_id"] is None
    assert legacy.json()["default_reasoning_level"] is None
    assert legacy.json()["skill_ids"] == []
    assert legacy.json()["knowledge_base_ids"] == original["knowledge_base_ids"]
    assert legacy.json()["name"] == "Renamed"


def test_competing_assistant_patches_cannot_silently_lose_updates(client, monkeypatch):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    original = service.save_assistant({"name": "Original", "description": "Original description"}, user=user)
    execute = service.db.execute
    writers_ready = threading.Barrier(2)

    def competing_execute(query, values=()):
        if query.startswith("UPDATE assistants SET name="):
            # Both requests have already read and merged the same revision.
            writers_ready.wait(timeout=5)
        return execute(query, values)

    monkeypatch.setattr(service.db, "execute", competing_execute)

    def update(data):
        try:
            service.save_assistant(data, original["id"], user=user)
            return "saved"
        except AppError as exc:
            assert exc.status == 409
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [{"name": "Updated name"}, {"description": "Updated description"}]))
    assert sorted(results) == ["ASSISTANT_CONFLICT", "saved"]
    final = service.require("assistants", original["id"])
    assert final["revision"] == original["revision"] + 1
    assert (final["name"], final["description"]) in {
        ("Updated name", "Original description"),
        ("Original", "Updated description"),
    }


def test_assistant_deleted_between_read_and_update_is_not_changed(client, monkeypatch):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    original = service.save_assistant({"name": "Original"}, user=user)
    execute = service.db.execute

    def deleted_before_update(query, values=()):
        if query.startswith("UPDATE assistants SET name="):
            execute("UPDATE assistants SET deleted=1,enabled=0,revision=revision+1 WHERE id=?", (original["id"],))
        return execute(query, values)

    monkeypatch.setattr(service.db, "execute", deleted_before_update)
    with pytest.raises(AppError) as conflict:
        service.save_assistant({"name": "Stale name"}, original["id"], user=user)
    assert conflict.value.status == 409
    assert conflict.value.code == "ASSISTANT_CONFLICT"
    final = service.db.one("SELECT * FROM assistants WHERE id=?", (original["id"],))
    assert final["deleted"] and final["name"] == "Original"


def test_assistant_save_response_never_reads_a_later_private_revision(client, monkeypatch):
    service = client.app.state.service
    editor = service.db.one("SELECT * FROM users WHERE username='admin'")
    owner = service.create_user({"username": "owner", "password": PASSWORD, "display_name": "Owner", "role": "admin"})
    original = service.save_assistant({"name": "Team assistant", "scope": "team", "instructions": "Shared instructions"}, user=owner)
    execute = service.db.execute

    def privatized_after_update(query, values=()):
        result = execute(query, values)
        if query.startswith("UPDATE assistants SET name="):
            # The other owner changes the next revision after this editor's
            # successful write, before its response is assembled.
            execute("UPDATE assistants SET scope='private',instructions='Owner private instructions',revision=revision+1 WHERE id=?", (original["id"],))
        return result

    monkeypatch.setattr(service.db, "execute", privatized_after_update)
    saved = service.save_assistant({"name": "Editor update", "expected_revision": original["revision"]}, original["id"], user=editor)
    assert saved["revision"] == original["revision"] + 1
    assert saved["scope"] == "team"
    assert saved["name"] == "Editor update"
    assert saved["instructions"] == "Shared instructions"
    final = service.db.one("SELECT * FROM assistants WHERE id=?", (original["id"],))
    assert final["revision"] == original["revision"] + 2
    assert final["scope"] == "private" and final["instructions"] == "Owner private instructions"
    with pytest.raises(AppError) as denied:
        service.require_resource("assistants", original["id"], editor)
    assert denied.value.status == 404
