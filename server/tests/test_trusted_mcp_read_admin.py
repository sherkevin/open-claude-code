"""Operator registry tests: synthetic SQLite records, no customer/network calls."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from app.db import now
from app.read_only_tools import definition_digest
from app.trusted_mcp_read import trusted_read_decision

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/kaiwu_read_contract.py"
_spec = importlib.util.spec_from_file_location("kaiwu_read_contract_ops", _SCRIPT)
ops = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ops)


READ = {"name": "query", "description": "Read synthetic records.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}},
                        "additionalProperties": False}}
WRITE = {"name": "write_record", "description": "Change a synthetic record.",
         "inputSchema": {"type": "object", "additionalProperties": False},
         "annotations": {"readOnlyHint": True}}
ACTION = {"name": "call", "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}}}
SECRET = "synthetic-encrypted-fixture-value-never-output"


@pytest.fixture
def registry(tmp_path):
    d = sqlite3.connect(tmp_path / "app.sqlite3")
    d.execute("CREATE TABLE connections(id TEXT PRIMARY KEY,owner_id TEXT,scope TEXT,revision INTEGER,url TEXT,auth_type TEXT,secret TEXT,enabled INTEGER,status TEXT,deleted INTEGER,tools TEXT,read_only_review_revision INTEGER)")
    for cid in ("source-a", "source-b"):
        d.execute("INSERT INTO connections VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, "owner-"+cid, "private", 1, "https://synthetic.example.invalid/"+cid,
                   "bearer", SECRET, 1, "connected", 0, json.dumps([READ, WRITE, ACTION]), 0))
    d.commit()
    d.close()
    return ops.Registry(tmp_path)


def manifest(registry, cid="source-a"):
    state = registry.inspect(cid)
    stamp = ops.utc_now()
    return {"version": 1, "connection_id": cid,
            "expected_identity": state["connection_identity"],
            "expected_policy_sha256": state["policy_sha256"],
            "policy_id": "synthetic-review-v1", "review_reference": "review/ticket-123",
            "expires_at": (stamp + timedelta(days=2)).isoformat(),
            "authorization_expires_at": (stamp + timedelta(days=3)).isoformat(),
            "tools": {"query": definition_digest(READ)}}


def update(registry, cid, field, value):
    with sqlite3.connect(registry.directory / "app.sqlite3") as d:
        assert field in {"revision", "tools", "secret", "enabled", "deleted", "status", "read_only_review_revision"}
        d.execute(f"UPDATE connections SET {field}=? WHERE id=?", (value, cid))


def test_inspect_no_policy_write_or_secret(registry):
    state = registry.inspect("source-a")
    assert state["registry_status"] == "not_registered"
    assert state["policy_sha256"] == "absent"
    assert state["platform_auto_count"] == 0
    assert {t["name"]: t["eligible_for_review"] for t in state["tools"]} == {
        "query": True, "write_record": False, "call": False}
    assert SECRET not in json.dumps(state) and "synthetic.example.invalid" not in json.dumps(state)
    assert not registry.path.exists()


def test_dry_run_apply_and_current_runtime_approval(registry):
    m = manifest(registry)
    plan = registry.apply(m)
    assert plan["committed"] is False and not registry.path.exists()
    committed = registry.apply(m, commit=True)
    assert committed["committed"] and committed["changed"]
    assert registry.path.stat().st_mode & 0o777 == 0o600
    state = registry.inspect("source-a")
    assert state["platform_auto_count"] == 1
    row = registry.connection("source-a")
    assert trusted_read_decision(registry.service, row, "query", now())
    assert not trusted_read_decision(registry.service, row, "call", now())
    assert not trusted_read_decision(registry.service, row, "write_record", now())
    assert registry.inspect("source-b")["platform_auto_count"] == 0


def test_separate_sources_preserved_and_stale_operator_cas_rejected(registry):
    a, b = manifest(registry), manifest(registry, "source-b")
    registry.apply(a, commit=True)
    previous = registry.path.read_bytes()
    with pytest.raises(ops.ContractError, match="POLICY_CHANGED"):
        registry.apply(b, commit=True)
    assert registry.path.read_bytes() == previous
    b["expected_policy_sha256"] = registry.inspect("source-b")["policy_sha256"]
    registry.apply(b, commit=True)
    assert registry.inspect("source-a")["platform_auto_count"] == 1
    assert registry.inspect("source-b")["platform_auto_count"] == 1


@pytest.mark.parametrize("field,value", [
    ("revision", 2), ("secret", "rotated-encrypted-value"), ("enabled", 0),
    ("status", "failed"), ("read_only_review_revision", 1), ("deleted", 1),
])
def test_changed_connection_is_never_silently_reenrolled(registry, field, value):
    m = manifest(registry)
    update(registry, "source-a", field, value)
    with pytest.raises(ops.ContractError):
        registry.apply(m, commit=True)
    assert not registry.path.exists()


def test_definition_changed_or_unsafe_selection_rejected(registry):
    m = manifest(registry)
    update(registry, "source-a", "tools", json.dumps([{**READ, "description": "changed"}, WRITE, ACTION]))
    with pytest.raises(ops.ContractError, match="TOOL_DEFINITION_CHANGED"):
        registry.apply(m, commit=True)
    m = manifest(registry)
    m["tools"] = {"write_record": definition_digest(WRITE)}
    with pytest.raises(ops.ContractError, match="TOOL_NOT_ELIGIBLE"):
        registry.apply(m, commit=True)
    m["tools"] = {"call": definition_digest(ACTION)}
    with pytest.raises(ops.ContractError, match="TOOL_NOT_ELIGIBLE"):
        registry.apply(m, commit=True)
    assert not registry.path.exists()


def test_expiry_bound_enforced(registry):
    m = manifest(registry)
    m["expires_at"] = (ops.utc_now() + timedelta(days=4)).isoformat()
    with pytest.raises(ops.ContractError, match="EXPIRY_OUTSIDE"):
        registry.apply(m, commit=True)
    m["expires_at"] = (ops.utc_now() - timedelta(seconds=1)).isoformat()
    with pytest.raises(ops.ContractError, match="EXPIRY_OUTSIDE"):
        registry.apply(m, commit=True)


def test_idempotent_apply_does_not_invalidate_running_approval(registry):
    m = manifest(registry)
    registry.apply(m, commit=True)
    previous = registry.path.read_bytes()
    m["expected_policy_sha256"] = registry.inspect("source-a")["policy_sha256"]
    result = registry.apply(m, commit=True)
    assert result["changed"] is False
    assert registry.path.read_bytes() == previous


def test_revoke_only_target_and_stale_deleted_connection(registry):
    registry.apply(manifest(registry), commit=True)
    registry.apply(manifest(registry, "source-b"), commit=True)
    sha = registry.inspect("source-a")["policy_sha256"]
    plan = registry.revoke("source-a", sha)
    assert not plan["committed"] and registry.inspect("source-a")["platform_auto_count"] == 1
    update(registry, "source-a", "deleted", 1)
    assert registry.revoke("source-a", sha, commit=True)["committed"]
    assert "source-a" not in registry.load()[0]["connections"]
    assert registry.inspect("source-b")["platform_auto_count"] == 1


def test_corrupt_or_public_policy_not_overwritten(registry):
    m = manifest(registry)
    registry.path.write_text("{")
    registry.path.chmod(0o600)
    with pytest.raises(ops.ContractError, match="POLICY_INVALID"):
        registry.apply(m, commit=True)
    assert registry.path.read_text() == "{"
    registry.path.write_text('{"version":1,"connections":{}}')
    registry.path.chmod(0o644)
    with pytest.raises(ops.ContractError, match="POLICY_INVALID"):
        registry.apply(m, commit=True)
    assert registry.path.stat().st_mode & 0o777 == 0o644


def test_manifest_private_duplicate_keys_and_no_symlink(registry):
    p = registry.directory / "manifest.json"
    p.write_text(json.dumps(manifest(registry)))
    p.chmod(0o600)
    assert ops.read_manifest(p)["connection_id"] == "source-a"
    p.chmod(0o644)
    with pytest.raises(ops.ContractError, match="MANIFEST_NOT_PRIVATE"):
        ops.read_manifest(p)
    p.chmod(0o600)
    p.write_text('{"version":1,"version":2}')
    with pytest.raises(ops.ContractError, match="DUPLICATE_JSON_KEY"):
        ops.read_manifest(p)
    link = registry.directory / "link.json"
    link.symlink_to(p)
    with pytest.raises(OSError):
        ops.read_manifest(link)


def test_registered_state_expiry_and_override(registry):
    registry.apply(manifest(registry), commit=True)
    row = registry.connection("source-a")
    assert registry.inspect("source-a")["expires_within_7_days"]
    update(registry, "source-a", "read_only_review_revision", 1)
    status = registry.inspect("source-a")
    assert status["registry_status"] == "manual_override" and status["platform_auto_count"] == 0
    assert not trusted_read_decision(registry.service, registry.connection("source-a"), "query", now())


def test_cli_returns_safe_json(registry, capsys):
    assert ops.main(["--data-dir", str(registry.directory), "inspect", "--connection-id", "source-a"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["platform_auto_count"] == 0
    assert SECRET not in output
    assert ops.main(["--data-dir", str(registry.directory), "inspect", "--connection-id", "missing"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "CONNECTION_NOT_FOUND"


def test_inspection_explains_changed_or_removed_reviewed_definition(registry):
    registry.apply(manifest(registry), commit=True)
    update(registry, "source-a", "tools", json.dumps([{**READ, "description": "new contract"}, WRITE, ACTION]))
    state = registry.inspect("source-a")
    assert state["platform_auto_count"] == 0 and state["registered_tool_count"] == 1
    assert next(t for t in state["tools"] if t["name"] == "query")["reason"] == "definition_changed"
    update(registry, "source-a", "tools", json.dumps([WRITE, ACTION]))
    assert registry.inspect("source-a")["missing_reviewed_tool_count"] == 1


def test_explicit_reimport_opt_in_and_target_revocation(registry):
    m = manifest(registry)
    m["allow_reimport"] = True
    registry.apply(m, commit=True)
    assert registry.inspect("source-a")["allow_reimport"] is True
    assert registry.inspect("source-b")["registry_status"] == "runtime_match_required"
    sha = registry.inspect("source-a")["policy_sha256"]
    registry.revoke("source-b", sha, commit=True)
    assert registry.inspect("source-b")["registry_status"] == "reuse_revoked"
    assert registry.inspect("source-a")["platform_auto_count"] == 1
    registry.apply(manifest(registry, "source-b"), commit=True)
    assert registry.inspect("source-b")["platform_auto_count"] == 1
    assert "source-b" not in registry.load()[0].get("blocked_reuse", [])


def test_reimport_flag_is_strict_and_needs_bearer(registry):
    m = manifest(registry)
    m["allow_reimport"] = "true"
    with pytest.raises(ops.ContractError, match="INVALID_REIMPORT_SETTING"):
        registry.apply(m, commit=True)
    with sqlite3.connect(registry.directory / "app.sqlite3") as db:
        db.execute("UPDATE connections SET auth_type='none',secret=NULL WHERE id='source-a'")
    m = manifest(registry)
    m["allow_reimport"] = True
    with pytest.raises(ops.ContractError, match="REIMPORT_REQUIRES_REVIEWED_BEARER"):
        registry.apply(m, commit=True)
    assert not registry.path.exists()
