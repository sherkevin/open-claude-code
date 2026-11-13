"""End-to-end isolation checks for Harness conversation memory.

These tests use the real memory API, immutable snapshot store, database
generation counter and conversation lifecycle. They do not call a provider.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import uid
from app.main import create_app


PASSWORD = "synthetic-memory-isolation-123"
OLD_SENTINEL = "OLD-MEMORY-SENTINEL-731"
NEW_SENTINEL = "NEW-MEMORY-SENTINEL-827"


@pytest.fixture
def memory_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(
        data_dir=tmp_path,
        provider_settings="",
        model="fixture",
        runtime_type="harness",
        knowledge_vision_enabled=False,
        image_generation_enabled=False,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user(
            {
                "username": "memory-owner",
                "display_name": "Memory owner",
                "password": PASSWORD,
                "role": "admin",
            }
        )
        assistant = service.save_assistant({"name": "Memory isolation"}, user=user)
        conversation = service.create_conversation(
            {"assistant_id": assistant["id"], "title": "Memory isolation"},
            user,
        )
        assert client.post(
            "/api/v1/auth/login",
            json={"username": user["username"], "password": PASSWORD},
        ).status_code == 200
        yield {
            "client": client,
            "service": service,
            "user": user,
            "assistant": assistant,
            "conversation": conversation,
            "url": f"/api/v1/conversations/{conversation['id']}/memory",
        }


def _write(system, content, revision, operation_id):
    response = system["client"].put(
        system["url"] + "/file",
        json={
            "path": "notes.md",
            "content": content,
            "revision": revision,
            "operation_id": operation_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _binding(system):
    return system["service"].memory.snapshot_for_run(
        system["user"]["id"],
        system["conversation"]["id"],
    )


def _index(system, binding):
    return system["service"].memory.snapshots.read(binding)["files"]


def _contents(system, binding):
    return {
        item["path"]: system["service"].memory.snapshots.read(
            binding, item["path"]
        )["content"].encode("utf-8")
        for item in _index(system, binding)
    }


def _install_stale_native_history(system):
    """Model an old native session plus non-memory workspace evidence."""
    service = system["service"]
    conversation_id = system["conversation"]["id"]
    service.db.execute(
        "UPDATE conversations SET harness_session_id='stale-native-session' WHERE id=?",
        (conversation_id,),
    )
    service.db.execute(
        "INSERT INTO messages(id,conversation_id,role,text,file_ids,run_id,created_at) "
        "VALUES(?,?,'user',?,'[]',?,?)",
        (uid(), conversation_id, OLD_SENTINEL, uid(), "2026-11-12T00:00:00+00:00"),
    )
    workspace = service.settings.data_dir / "workspaces" / conversation_id / "uploads"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "old-attachment.txt").write_text(OLD_SENTINEL, encoding="utf-8")


def test_edit_rotates_native_generation_and_excludes_old_memory(memory_client):
    system = memory_client
    initial = system["client"].get(system["url"]).json()
    _write(system, OLD_SENTINEL, initial["revision"], "seed-old")
    old_binding = _binding(system)
    _install_stale_native_history(system)

    edited = _write(system, NEW_SENTINEL, old_binding["revision"], "replace-old")
    new_binding = _binding(system)
    active = _contents(system, new_binding)

    assert edited["context_reset"] is True
    assert new_binding["snapshot_id"] != old_binding["snapshot_id"]
    assert new_binding["revision"] == old_binding["revision"] + 1
    assert new_binding["native_epoch"] == old_binding["native_epoch"] + 1
    assert system["service"].require("conversations", system["conversation"]["id"])[
        "harness_session_id"
    ] is None
    assert "notes.md" in active
    assert OLD_SENTINEL.encode() not in b"".join(active.values())
    assert NEW_SENTINEL.encode() in active["notes.md"]
    store = system["service"].memory.snapshots
    with store.fs.verify(old_binding) as old_tree:
        assert OLD_SENTINEL.encode() in old_tree.files["notes.md"]

    # Application history remains auditable, but it is not part of the active
    # memory snapshot and cannot be resumed through the stale native session.
    history = system["service"].db.all(
        "SELECT text FROM messages WHERE conversation_id=? ORDER BY created_at",
        (system["conversation"]["id"],),
    )
    assert OLD_SENTINEL in json.dumps(history, ensure_ascii=False)


def test_delete_and_clear_do_not_carry_old_content_forward(memory_client):
    system = memory_client
    initial = system["client"].get(system["url"]).json()
    _write(system, OLD_SENTINEL, initial["revision"], "seed-delete")
    seeded = _binding(system)

    deleted = system["client"].delete(
        system["url"] + "/file",
        params={
            "path": "notes.md",
            "revision": seeded["revision"],
            "operation_id": "delete-memory",
        },
    )
    assert deleted.status_code == 200, deleted.text
    after_delete = _binding(system)
    assert _index(system, after_delete) == []
    assert after_delete["snapshot_id"] != seeded["snapshot_id"]
    assert OLD_SENTINEL.encode() not in b"".join(
        _contents(system, after_delete).values()
    )

    _write(system, OLD_SENTINEL, after_delete["revision"], "seed-clear")
    before_clear = _binding(system)
    cleared = system["client"].delete(
        system["url"],
        params={
            "revision": before_clear["revision"],
            "operation_id": "clear-memory",
        },
    )
    assert cleared.status_code == 200, cleared.text
    after_clear = _binding(system)
    assert _index(system, after_clear) == []
    assert after_clear["snapshot_id"] != before_clear["snapshot_id"]
    assert after_clear["native_epoch"] == before_clear["native_epoch"] + 1


def test_stale_revision_and_path_failure_roll_back(memory_client):
    system = memory_client
    initial = system["client"].get(system["url"]).json()
    seeded = _write(system, OLD_SENTINEL, initial["revision"], "seed-conflict")
    binding = _binding(system)

    conflict = system["client"].put(
        system["url"] + "/file",
        json={
            "path": "notes.md",
            "content": NEW_SENTINEL,
            "revision": initial["revision"],
            "operation_id": "stale-revision",
        },
    )
    assert conflict.status_code == 409
    assert _binding(system) == binding
    assert [item["path"] for item in _index(system, binding)] == ["notes.md"]

    denied = system["client"].put(
        system["url"] + "/file",
        json={
            "path": "../escape.md",
            "content": NEW_SENTINEL,
            "revision": seeded["revision"],
            "operation_id": "escape-path",
        },
    )
    assert denied.status_code in {403, 409, 422}
    assert _binding(system) == binding
    active = _contents(system, binding)
    assert OLD_SENTINEL.encode() in active["notes.md"]
    assert NEW_SENTINEL.encode() not in active["notes.md"]


def test_old_tool_result_is_not_part_of_active_memory_binding(memory_client):
    system = memory_client
    initial = system["client"].get(system["url"]).json()
    _write(system, NEW_SENTINEL, initial["revision"], "seed-tool")
    run_id = uid()
    system["service"].db.execute(
        "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,assistant_message_id,status,created_at) "
        "VALUES(?,?,?,?,?,?,'failed',?)",
        (
            run_id,
            system["conversation"]["id"],
            system["user"]["id"],
            "old-tool-result",
            uid(),
            uid(),
            "2026-11-12T00:00:00+00:00",
        ),
    )
    system["service"].db.event(
        run_id,
        "tool.completed",
        {"summary": OLD_SENTINEL, "source": "historical-fixture"},
    )
    active = _contents(system, _binding(system))
    assert OLD_SENTINEL.encode() not in b"".join(active.values())
    assert NEW_SENTINEL.encode() in active["notes.md"]
