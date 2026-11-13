"""Immutable Harness memory; synthetic users/data only, no model requests."""
import hashlib
import json
import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database, dump, now, uid
from app.memory import NativeMemory, create_memory_router
from app.memory_snapshots import MAX_FILE, MAX_TOTAL, MAX_MANIFEST, SnapshotFiles, VerifiedTree, canonical, digest
from app.security import AppError
from app.service import Service


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="synthetic", runtime_type="harness"))
    user = service.create_user({"username": "owner", "display_name": "Owner", "password": "synthetic-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Memory fixture"}, user=user)
    cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Synthetic memory"}, user)["id"]
    try:
        yield service, user, cid
    finally:
        service.db.close()


def state(store):
    service, user, cid = store
    return service.memory.snapshot_for_run(user["id"], cid)


def edit(store, revision, path="MEMORY.md", content="synthetic", key=None):
    service, user, cid = store
    return service.memory.edit(user, cid, revision, path, content, key or uid())


def make_run(store):
    service, user, cid = store
    binding = state(store)
    rid = uid()
    service.db.execute("INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at,execution_meta) VALUES(?,?,?,?,?,'running',?,?)", (rid, cid, user["id"], uid(), uid(), now(), dump({"memory_binding": binding, "native_epoch": binding["native_epoch"]})))
    service.db.execute("UPDATE conversations SET active_run_id=? WHERE id=?", (rid, cid))
    return rid, binding


def approve(store, op):
    service, user, _ = store
    approval = uid()
    policy = '{"fixture":"human-only"}'
    arguments = {"path": op["path"], "content": op["content"], "expected_revision": op["expected_revision"],
                 "operation_id": op["operation_id"], "native_epoch": op["binding"]["native_epoch"], "policy_binding": policy}
    service.db.execute("INSERT INTO approvals(id,run_id,user_id,tool_name,arguments,status,created_at,expires_at,decided_at,decided_by,decision_source) VALUES(?,?,?,?,?,'allowed',?,?,?,?,'user')", (approval, op["run_id"], user["id"], "memory_write", dump(arguments), now(), (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), now(), user["id"]))
    return service.memory.approve_stage(op["operation_id"], approval, policy)


def finish(service, cid, rid):
    def final(result):
        service.db.event(rid, "memory.updated", {k: result[k] for k in ("revision", "native_epoch", "context_reset")})
        service.db.execute("UPDATE runs SET status='succeeded',completed_at=? WHERE id=?", (now(), rid))
        service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (cid,))
    return final


def test_management_epoch_revision_idempotence_and_history(store):
    service, user, cid = store
    empty = state(store)
    assert empty["revision"] == empty["native_epoch"] == 0
    service.db.execute("UPDATE conversations SET harness_session_id='old-native',context_usage='{}' WHERE id=?", (cid,))
    saved = edit(store, 0, key="save-once")
    assert saved["revision"] == saved["native_epoch"] == 1 and saved["context_reset"]
    assert edit(store, 0, key="save-once") == saved
    unchanged = edit(store, 1, key="same-content")
    assert unchanged["mutation_status"] == "unchanged" and unchanged["native_epoch"] == 1
    deleted = edit(store, 1, content=None, key="delete-once")
    assert deleted["revision"] == deleted["native_epoch"] == 2
    cleared = edit(store, 2, path=None, content=None, key="clear-empty")
    assert cleared["revision"] == 2 and cleared["native_epoch"] == 3
    assert edit(store, 2, path=None, content=None, key="clear-empty") == cleared
    assert service.require("conversations", cid)["harness_session_id"] is None
    assert service.db.one("SELECT COUNT(*) n FROM memory_snapshot_records WHERE state='published'")["n"] == 3
    assert service.memory.snapshots.fs.path(user["id"], cid, empty["snapshot_id"]).is_dir()


@pytest.mark.parametrize("change", ["content", "path", "revision", "kind"])
def test_idempotency_key_cannot_be_rebound(store, change):
    edit(store, 0, key="once")
    kwargs = {"revision": 0, "path": "MEMORY.md", "content": "synthetic", "key": "once"}
    kwargs[{"content": "content", "path": "path", "revision": "revision", "kind": "content"}[change]] = {"content": "other", "path": "other.md", "revision": 1, "kind": None}[change]
    with pytest.raises(AppError):
        edit(store, **kwargs)


@pytest.mark.parametrize("path", ["/absolute.md", "../escape.md", "a//b.md", ".private.md", "a/./b.md", "a/../b.md", "a\\b.md", "a:b.md", "a\nb.md", "a.json", "a/b/c/d/e/f.md", "a" * 240 + ".md"])
def test_paths_fail_closed(store, path):
    state(store)
    with pytest.raises(AppError):
        edit(store, 0, path=path)
    assert state(store)["revision"] == 0


def test_capacity_uses_utf8_bytes_and_candidate_total(store):
    exact = "a" * MAX_FILE
    assert edit(store, 0, content=exact)["revision"] == 1
    with pytest.raises(AppError):
        edit(store, 1, content="汉" * (MAX_FILE // 3 + 1))
    for index in range(1, 8):
        edit(store, index, path=f"{index}.md", content=exact)
    assert sum(i["size"] for i in store[0].memory.view(store[1], store[2])["files"]) == MAX_TOTAL
    with pytest.raises(AppError):
        edit(store, 8, path="overflow.md", content="x")


@pytest.mark.parametrize("mutation", ["content", "symlink", "hardlink", "fifo", "file_mode", "root_mode", "ancestor_mode", "unknown", "manifest", "missing", "directory_swap"])
def test_published_tree_tampering_is_never_accepted_as_new_revision(store, mutation):
    service, user, cid = store
    edit(store, 0)
    binding = state(store)
    root = service.memory.root(user["id"], cid)
    target = root / "MEMORY.md"
    if mutation == "content":
        target.write_text("tampered")
    elif mutation == "symlink":
        target.unlink(); target.symlink_to(root / ".manifest.json")
    elif mutation == "hardlink":
        os.link(target, root / "other.md")
    elif mutation == "fifo":
        target.unlink(); os.mkfifo(target, 0o600)
    elif mutation == "file_mode":
        target.chmod(0o644)
    elif mutation == "root_mode":
        root.chmod(0o755)
    elif mutation == "ancestor_mode":
        root.parent.chmod(0o755)
    elif mutation == "unknown":
        (root / "unknown.txt").write_text("not indexed")
    elif mutation == "manifest":
        (root / ".manifest.json").write_text("{}")
    elif mutation == "missing":
        target.unlink()
    else:
        root.rename(root.with_name("replaced")); root.symlink_to(root.with_name("replaced"), target_is_directory=True)
    with pytest.raises(AppError):
        service.memory.refresh(user["id"], cid)
    row = service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
    assert row["revision"] == binding["revision"] and row["active_snapshot_id"] == binding["snapshot_id"]


def test_open_descriptor_rechecks_file_and_directory_identity(store):
    service, user, cid = store
    edit(store, 0)
    binding = state(store)
    root = service.memory.root(user["id"], cid)
    with service.memory.snapshots.fs.verify(binding) as tree:
        (root / "MEMORY.md").write_text("mutated")
        with pytest.raises(AppError):
            tree.assert_unchanged()


def test_stage_read_old_then_atomic_success_and_event(store):
    service, user, cid = store
    edit(store, 0, content="old")
    rid, binding = make_run(store)
    op = service.memory.stage_for_run(binding, rid, "tool-once", "MEMORY.md", "new", 1)
    assert op["status"] == "proposed"
    assert service.memory.read_for_run(binding, "MEMORY.md", 1)["content"] == "old"
    pending = approve(store, op)
    assert pending["status"] == "approved_pending_commit"
    assert service.memory.read_for_run(binding, "MEMORY.md", 1)["content"] == "old"
    checked = []
    def authorize():
        checked.append(service.memory.snapshot_for_run(user["id"], cid, rid))
        assert service.memory.pending_for_run(rid)["status"] == "prepared"
    result = service.memory.commit_for_run(rid, authorize=authorize, finalize=finish(service, cid, rid))
    assert checked == [binding] and result["revision"] == 2
    assert service.require("runs", rid)["status"] == "succeeded"
    assert service.memory.read(user, cid, "MEMORY.md")["content"] == "new"
    assert [e["type"] for e in service.db.events(rid, 0)] == ["memory.updated"]


@pytest.mark.parametrize("failure", ["unapproved", "cancelled", "expired", "changed_content", "model_approval", "disabled", "authorization", "finalize"])
def test_failed_commit_keeps_old_pointer_epoch_and_run_terminal_atomic(store, failure):
    service, user, cid = store
    rid, binding = make_run(store)
    op = service.memory.stage_for_run(binding, rid, uid(), "MEMORY.md", "not published", 0)
    pending = op if failure == "unapproved" else approve(store, op)
    if failure == "cancelled":
        service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (rid,))
    if failure == "expired":
        service.db.execute("UPDATE approvals SET expires_at=? WHERE id=?", ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), pending["approval_id"]))
    if failure == "changed_content":
        service.db.execute("UPDATE approvals SET arguments='{}' WHERE id=?", (pending["approval_id"],))
    if failure == "model_approval":
        service.db.execute("UPDATE approvals SET decision_source='model' WHERE id=?", (pending["approval_id"],))
    if failure == "disabled":
        service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    def authorize():
        if failure == "authorization":
            raise AppError(403, "DENIED", "synthetic")
    def final(result):
        finish(service, cid, rid)(result)
        if failure == "finalize":
            raise RuntimeError("synthetic final transaction failure")
    with pytest.raises((AppError, RuntimeError)):
        service.memory.commit_for_run(rid, authorize=authorize, finalize=final)
    scope = service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
    assert scope["active_snapshot_id"] == binding["snapshot_id"] and scope["revision"] == 0
    assert service.require("conversations", cid)["native_epoch"] == 0
    assert service.require("runs", rid)["status"] != "succeeded"
    assert service.db.events(rid, 0) == []


def test_one_proposal_per_run_and_discard_not_readable(store):
    service, _, cid = store
    rid, binding = make_run(store)
    first = service.memory.stage_for_run(binding, rid, "same", "MEMORY.md", "candidate", 0)
    assert service.memory.stage_for_run(binding, rid, "same", "MEMORY.md", "candidate", 0)["operation_id"] == first["operation_id"]
    with pytest.raises(AppError):
        service.memory.stage_for_run(binding, rid, "different", "other.md", "candidate", 0)
    service.memory.discard_for_run(rid)
    assert service.memory.pending_for_run(rid)["status"] == "aborted"
    assert service.memory.list_for_run(binding)["files"] == []


@pytest.mark.parametrize("fault", ["build", "verify", "sql"])
def test_management_failures_leave_old_pointer_and_preserve_orphan_budget(store, monkeypatch, fault):
    service, user, cid = store
    binding = state(store)
    original = service.memory.snapshots.fs.build
    def broken(*args, **kwargs):
        result = original(*args, **kwargs)
        if fault == "build":
            raise OSError("synthetic after fsync")
        if fault == "verify":
            (service.memory.snapshots.fs.path(user["id"], cid, result["snapshot_id"]) / "MEMORY.md").write_text("tampered")
        return result
    monkeypatch.setattr(service.memory.snapshots.fs, "build", broken)
    execute = service.db.execute
    def fail_sql(query, values=()):
        if fault == "sql" and query.startswith("UPDATE conversations SET native_epoch"):
            raise RuntimeError("synthetic SQL failure")
        return execute(query, values)
    monkeypatch.setattr(service.db, "execute", fail_sql)
    with pytest.raises((AppError, OSError, RuntimeError)):
        edit(store, 0)
    assert state(store) == binding
    assert service.db.one("SELECT COUNT(*) n FROM memory_snapshot_records")["n"] == 2
    assert service.db.one("SELECT COUNT(*) n FROM memory_operations WHERE status='aborted'")["n"] == 1


def test_harness_management_http_requires_stable_key_and_works_archived(store):
    service, user, cid = store
    app = FastAPI()
    @app.exception_handler(AppError)
    async def error(_request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=exc.status, content={"code": exc.code})
    app.include_router(create_memory_router(lambda: service, lambda: user))
    url = f"/api/v1/conversations/{cid}/memory"
    with TestClient(app) as client:
        assert client.get(url).json()["native_epoch"] == 0
        assert client.put(url + "/file", json={"path": "MEMORY.md", "content": "new", "revision": 0}).status_code == 422
        service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (cid,))
        saved = client.put(url + "/file", json={"path": "MEMORY.md", "content": "new", "revision": 0, "operation_id": "http-save"})
        assert saved.status_code == 200 and saved.json()["context_reset"]
        assert client.get(url + "/file", params={"path": "MEMORY.md"}).json()["content"] == "new"
        deleted = client.delete(url + "/file", params={"path": "MEMORY.md", "revision": 1, "operation_id": "http-delete"})
        assert deleted.status_code == 200 and deleted.json()["revision"] == 2
        assert client.delete(url, params={"revision": 2, "operation_id": "http-clear"}).json()["native_epoch"] == 3


def test_migration_preserves_source_and_resets_only_existing_harness_history(store):
    service, user, cid = store
    old = service.settings.data_dir / "conversation-memory" / user["id"] / cid
    old.mkdir(parents=True, mode=0o700)
    (old / "MEMORY.md").write_text("legacy fixture")
    (old / "MEMORY.md").chmod(0o600)
    service.db.execute("UPDATE conversations SET harness_session_id='retired-native' WHERE id=?", (cid,))
    binding = state(store)
    assert binding["native_epoch"] == 1 and binding["revision"] == 0
    assert service.memory.read(user, cid, "MEMORY.md")["content"] == "legacy fixture"
    assert (old / "MEMORY.md").read_text() == "legacy fixture"
    assert service.require("conversations", cid)["harness_session_id"] is None
    assert state(store) == binding


def test_recovery_never_adopts_uncommitted_tree_or_releases_live_owner(store):
    service, user, cid = store
    binding = state(store)
    snapshots = service.memory.snapshots
    with snapshots.transaction():
        op = snapshots._new_operation(binding, "pending-manual", "upsert", "MEMORY.md", "orphan")
        snapshots._gate(op)
    snapshots._prepare(op)
    service.memory.recover()
    assert service.db.one("SELECT mutation_id FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))["mutation_id"] == op["id"]
    service.db.execute("UPDATE memory_operations SET owner_start='not-this-process' WHERE id=?", (op["id"],))
    service.memory.recover()
    assert state(store) == binding
    assert service.db.one("SELECT status FROM memory_operations WHERE id=?", (op["id"],))["status"] == "aborted"
    assert service.db.one("SELECT COUNT(*) n FROM memory_snapshot_records")["n"] == 2


def test_retention_budget_denies_without_deleting_published_history(store, monkeypatch):
    service, user, cid = store
    state(store)
    monkeypatch.setattr("app.memory_snapshots.MAX_SNAPSHOTS", 2)
    edit(store, 0)
    with pytest.raises(AppError) as caught:
        edit(store, 1, content="third")
    assert caught.value.code == "MEMORY_STORAGE_FULL"
    assert service.memory.read(user, cid, "MEMORY.md")["content"] == "synthetic"
    assert service.db.one("SELECT COUNT(*) n FROM memory_snapshot_records")["n"] == 2


def test_candidates_and_aborted_content_still_count_towards_module_budget(store, monkeypatch):
    service, _, _ = store
    rid, binding = make_run(store)
    op = service.memory.stage_for_run(binding, rid, uid(), "MEMORY.md", "x" * MAX_FILE, 0)
    service.memory.discard_for_run(rid)
    reserved = service.db.one("SELECT reserved_bytes FROM memory_operations WHERE id=?", (op["id"],))["reserved_bytes"]
    assert reserved >= MAX_FILE
    monkeypatch.setattr("app.memory_snapshots.MAX_PROJECT", reserved)
    with service.memory.snapshots.transaction(), pytest.raises(AppError):
        service.memory.snapshots._new_operation(binding, uid(), "clear")


def test_cross_user_and_version_reads_fail_without_path_disclosure(store):
    service, user, cid = store
    binding = state(store)
    other = service.create_user({"username": "other", "display_name": "Other", "password": "synthetic-password-123", "role": "member"})
    with pytest.raises(AppError):
        service.memory.snapshot_for_run(other["id"], cid)
    edit(store, 0)
    with pytest.raises(AppError):
        service.memory.snapshots.read(binding)
    user["enabled"] = True
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    with pytest.raises(AppError):
        edit(store, 1)


def test_operation_status_recovery_is_owner_bound_and_contains_no_body(store):
    service, user, cid = store
    assert service.memory.operation_status(user, cid, "not-yet")["status"] == "not_found"
    edit(store, 0, content="private candidate", key="browser-recovery")
    value = service.memory.operation_status(user, cid, "browser-recovery")
    assert value == {"operation_id": "browser-recovery", "status": "committed", "kind": "upsert", "path": "MEMORY.md",
                     "result": {"revision": 1, "native_epoch": 1, "context_reset": True, "mutation_status": "committed"}}
    assert "private candidate" not in dump(value) and "snapshot" not in dump(value)
    other = service.create_user({"username": "other", "display_name": "Other", "password": "synthetic-password-123", "role": "member"})
    with pytest.raises(AppError):
        service.memory.operation_status(other, cid, "browser-recovery")
    rid, binding = make_run(store)
    op = service.memory.stage_for_run(binding, rid, "tool-only", "MEMORY.md", "private tool", 1)
    assert service.memory.operation_status(user, cid, "tool-only")["status"] == "not_found"
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    with pytest.raises(AppError):
        service.memory.operation_status(user, cid, "browser-recovery")


def test_held_snapshot_avoids_reading_bytes_under_final_transaction(store, monkeypatch):
    service, user, cid = store
    edit(store, 0)
    with service.memory.verified_for_run(user["id"], cid) as binding:
        monkeypatch.setattr(os, "read", lambda *_: pytest.fail("must not reread bytes inside transaction"))
        with service.db.transaction():
            assert service.memory.snapshot_for_run(user["id"], cid) == binding


def test_unknown_process_owner_remains_blocked(store, monkeypatch):
    service, _, cid = store
    binding = state(store)
    snapshots = service.memory.snapshots
    with snapshots.transaction():
        op = snapshots._new_operation(binding, "uncertain", "clear")
        snapshots._gate(op)
    monkeypatch.setattr("app.memory_snapshots.process_start", lambda _pid: "unverifiable")
    service.memory.recover()
    assert service.db.one("SELECT mutation_id FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))["mutation_id"] == op["id"]


def _crash_writer(data_dir, user_id, cid, fault):
    service = Service(Settings(data_dir=Path(data_dir), provider_settings="", model="synthetic", runtime_type="harness"))
    original = service.memory.snapshots.fs.build
    def build(*args, **kwargs):
        if fault == "reserved":
            os._exit(31)
        result = original(*args, **kwargs)
        if fault == "tree_synced":
            os._exit(32)
        return result
    service.memory.snapshots.fs.build = build
    execute = service.db.execute
    def sql(query, values=()):
        result = execute(query, values)
        if fault == "sql_uncommitted" and query.startswith("UPDATE conversations SET native_epoch"):
            os._exit(33)
        return result
    service.db.execute = sql
    service.memory.edit(service.require("users", user_id), cid, 0, "MEMORY.md", "crash candidate", "crash-operation")
    os._exit(34)


@pytest.mark.parametrize("fault", ["reserved", "tree_synced", "sql_uncommitted", "response_lost"])
def test_real_process_crash_recovery_never_mixes_pointer_and_epoch(store, fault):
    service, user, cid = store
    before = state(store)
    worker = multiprocessing.get_context("fork").Process(target=_crash_writer, args=(str(service.settings.data_dir), user["id"], cid, fault))
    worker.start()
    worker.join(120)
    try:
        assert not worker.is_alive()
        assert worker.exitcode in {31, 32, 33, 34}
    finally:
        if worker.is_alive():
            worker.kill(); worker.join(5)
    service.memory.recover()
    after = state(store)
    if fault == "response_lost":
        assert after["revision"] == after["native_epoch"] == 1
        replay = edit(store, 0, content="crash candidate", key="crash-operation")
        assert replay["native_epoch"] == 1 and state(store) == after
    else:
        assert after == before
        assert service.memory.operation_status(user, cid, "crash-operation")["status"] == "aborted"
