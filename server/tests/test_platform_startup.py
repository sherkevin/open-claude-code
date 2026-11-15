"""Portable API startup never weakens the POSIX-only memory snapshot boundary."""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import memory_snapshots as snapshots
from app.config import Settings
from app.main import create_app
from app.security import AppError


BINDING = {"user_id": "a" * 32, "conversation_id": "b" * 32, "snapshot_id": "c" * 32}


def test_application_imports_without_posix_snapshot_flags(tmp_path):
    # A new interpreter catches import-time failures even on Linux CI; deleting
    # the flags in this process would interfere with pytest's own filesystem IO.
    code = """
import os
for key in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_CLOEXEC'):
    if hasattr(os, key):
        delattr(os, key)
from app import memory_snapshots
from app.main import create_app
from app.usage_accounting import REPORT_TIMEZONE
assert not memory_snapshots.SNAPSHOT_FILES_SUPPORTED
assert str(REPORT_TIMEZONE) == 'Asia/Shanghai'
assert create_app().title
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
           "AGENT_DATA_DIR": str(tmp_path), "AGENT_PROVIDER_SETTINGS": "",
           "AGENT_MODEL_CATALOG": "", "AGENT_RUNTIME_TYPE": "claude"}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("entry", ["absolute", "child", "verify", "build", "write", "tree", "check", "unchanged"])
def test_unsupported_snapshot_operations_fail_before_filesystem_access(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(snapshots, "SNAPSHOT_FILES_SUPPORTED", False)
    store = snapshots.SnapshotFiles(tmp_path)
    tree = snapshots.VerifiedTree(tmp_path)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Unsupported snapshot operation reached the filesystem")

    with monkeypatch.context() as guard:
        guard.setattr(snapshots.os, "open", forbidden)
        guard.setattr(snapshots.os, "mkdir", forbidden)
        guard.setattr(snapshots.os, "fstat", forbidden)
        with pytest.raises(AppError) as caught:
            if entry == "absolute":
                with snapshots.absolute_dir(tmp_path):
                    pytest.fail("Unsupported directory opened")
            elif entry == "child":
                with snapshots.child_dir(123, "new", create=True):
                    pytest.fail("Unsupported child directory opened")
            elif entry == "verify":
                with store.verify(BINDING):
                    pytest.fail("Unsupported snapshot verified")
            elif entry == "build":
                store.build(BINDING, {"notes.md": b"private content"})
            elif entry == "write":
                store._write(123, ["notes.md"], b"private content")
            elif entry == "tree":
                with tree:
                    pytest.fail("Unsupported tree opened")
            elif entry == "check":
                snapshots.check_node(123)
            else:
                tree.assert_unchanged()
    assert caught.value.status == 501
    assert caught.value.code == "MEMORY_PLATFORM_UNSUPPORTED"
    assert list(tmp_path.iterdir()) == []
    assert snapshots.process_start(os.getpid()) == "unverifiable"


def test_local_api_starts_and_harness_memory_refuses_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshots, "SNAPSHOT_FILES_SUPPORTED", False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(data_dir=tmp_path, provider_settings="", runtime_type="claude", model="fixture")
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/v1/health/live").json() == {"status": "ok"}
        assert client.get("/api/v1/health/ready").status_code == 200
        service = app.state.service
        user = service.create_user({"username": "platform-owner", "display_name": "Platform owner",
                                    "role": "admin", "password": "local-platform-fixture-123"})
        assert client.post("/api/v1/auth/login", json={"username": user["username"],
                           "password": "local-platform-fixture-123"}).status_code == 200
        assert client.get("/api/v1/auth/me").json()["id"] == user["id"]
        assert client.get("/api/v1/usage?date_from=2026-11-15&date_to=2026-11-15").status_code == 200

        assistant = service.save_assistant({"name": "Platform fixture"}, user=user)
        claude = service.create_conversation({"assistant_id": assistant["id"], "title": "Local API fixture"}, user)
        assert claude["runtime_type"] == "claude"
        assert client.get(f"/api/v1/conversations/{claude['id']}/memory").status_code == 200

        service.settings.runtime_type = "harness"
        harness = service.create_conversation({"assistant_id": assistant["id"], "title": "Unsupported snapshot fixture"}, user)
        before = service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (harness["id"],))
        response = client.get(f"/api/v1/conversations/{harness['id']}/memory")
        assert response.status_code == 501, response.text
        assert response.json()["error"]["code"] == "MEMORY_PLATFORM_UNSUPPORTED"
        after = service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (harness["id"],))
        assert after == before
        assert service.db.all("SELECT * FROM memory_operations") == []
        assert service.db.all("SELECT * FROM memory_snapshot_records") == []
        assert not (tmp_path / "conversation-memory-snapshots").exists()


@pytest.mark.skipif(not snapshots.SNAPSHOT_FILES_SUPPORTED, reason="Requires real POSIX snapshot security primitives")
def test_supported_snapshot_roundtrip_and_symlink_refusal(tmp_path):
    tmp_path.chmod(0o700)
    store = snapshots.SnapshotFiles(tmp_path)
    binding = store.build(BINDING, {"notes.md": b"verified content"})
    with store.verify(binding) as tree:
        assert tree.files == {"notes.md": b"verified content"}
    note = store.path(**BINDING) / "notes.md"
    note.unlink()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"untrusted content")
    outside.chmod(0o600)
    note.symlink_to(outside)
    with pytest.raises(AppError) as caught:
        with store.verify(binding):
            pytest.fail("Symlink accepted as a verified snapshot file")
    assert caught.value.code == "MEMORY_INTEGRITY"
