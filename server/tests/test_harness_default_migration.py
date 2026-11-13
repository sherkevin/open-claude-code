"""New-executor defaults must not rewrite pre-catalog conversation choices."""
import json

import pytest

from app.config import Settings
from app.db import dump
from app.model_catalog import ModelCatalog
from app.service import Service


@pytest.mark.parametrize("explicit", [None, "low", "high", "max"])
def test_historical_effort_is_separate_from_new_harness_default(tmp_path, monkeypatch, explicit):
    monkeypatch.delenv("AGENT_EFFORT", raising=False)
    options = {} if explicit is None else {"effort": explicit}
    settings = Settings(data_dir=tmp_path, model="fixture", provider_settings="",
                        model_catalog="", runtime_type="harness", **options)
    catalog = ModelCatalog(settings)
    assert catalog.select()["upstream_effort"] == (explicit or "high")
    assert catalog.historical("fixture")["upstream_effort"] == (explicit or "medium")
    assert catalog.historical("no-longer-configured")["upstream_effort"] == (explicit or "medium")


def test_default_harness_upgrade_preserves_legacy_effort_and_native_identity(tmp_path, monkeypatch):
    for key in ("AGENT_RUNTIME_TYPE", "AGENT_EFFORT", "AGENT_DEFAULT_MODEL_ID"):
        monkeypatch.delenv(key, raising=False)
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{
        "id": "fixture", "label": "Fixture", "upstream_model": "fixture",
        "reasoning_levels": {"medium": "medium", "high": "high"},
        "default_reasoning_level": "high",
    }]}))
    options = dict(data_dir=tmp_path / "data", model="fixture", provider_settings="",
                   model_catalog=str(catalog))
    old = Service(Settings(runtime_type="claude", **options))
    user = old.create_user({"username": "owner", "display_name": "Owner", "role": "admin",
                            "password": "synthetic-migration-password-123"})
    assistant = old.save_assistant({"name": "Fixture", "connections": []})
    cid = old.create_conversation({"assistant_id": assistant["id"], "title": "Migration fixture"}, user)["id"]
    old.db.execute("UPDATE conversations SET model_snapshot=NULL,sdk_session_id='legacy-native' WHERE id=?", (cid,))
    old.db.close()
    upgraded = Service(Settings(**options))
    try:
        row = upgraded.require("conversations", cid)
        assert row["runtime_type"] == "claude"
        assert row["sdk_session_id"] == "legacy-native" and row["harness_session_id"] is None
        snapshot = json.loads(row["model_snapshot"])
        assert snapshot["reasoning_level"] == snapshot["upstream_effort"] == "medium"
        assert upgraded.runtime_config(cid)["native_session_id"] == "legacy-native"
        new = upgraded.create_conversation({"assistant_id": assistant["id"], "title": "Migration fixture"}, user)
        assert new["runtime_type"] == "harness" and new["reasoning_level"] == "high"
        assert not new["has_native_session"]
    finally:
        upgraded.db.close()


def test_single_model_snapshots_survive_default_upgrade_and_rollback(tmp_path, monkeypatch):
    for key in ("AGENT_RUNTIME_TYPE", "AGENT_EFFORT", "AGENT_DEFAULT_MODEL_ID"):
        monkeypatch.delenv(key, raising=False)
    options = dict(data_dir=tmp_path, model="fixture", provider_settings="", model_catalog="")
    old = Service(Settings(runtime_type="claude", **options))
    user = old.create_user({"username": "owner", "display_name": "Owner", "role": "admin",
                            "password": "synthetic-migration-password-123"})
    assistant = old.save_assistant({"name": "Fixture", "connections": []})
    before = old.create_conversation({"assistant_id": assistant["id"], "title": "Migration fixture"}, user)
    snapshot = old.require("conversations", before["id"])["model_snapshot"]
    old.db.close()
    upgraded = Service(Settings(**options))
    try:
        assert upgraded.runtime_config(before["id"])["upstream_effort"] == "medium"
        assert upgraded.require("conversations", before["id"])["model_snapshot"] == snapshot
        created = upgraded.create_conversation({"assistant_id": assistant["id"], "title": "Migration fixture"}, user)
        assert created["runtime_type"] == "harness" and created["reasoning_level"] == "high"
        new_snapshot = upgraded.require("conversations", created["id"])["model_snapshot"]
    finally:
        upgraded.db.close()
    rollback = Service(Settings(runtime_type="claude", **options))
    try:
        for conversation, effort in ((before, "medium"), (created, "high")):
            config = rollback.runtime_config(conversation["id"])
            assert config["runtime_type"] == conversation["runtime_type"]
            assert config["upstream_effort"] == effort
        assert rollback.require("conversations", created["id"])["model_snapshot"] == new_snapshot
    finally:
        rollback.db.close()


@pytest.mark.parametrize("effort", [None, "low", "high", "max"])
def test_recorded_effort_takes_precedence_over_migration_default(tmp_path, monkeypatch, effort):
    monkeypatch.delenv("AGENT_EFFORT", raising=False)
    service = Service(Settings(data_dir=tmp_path, model="fixture", provider_settings="", model_catalog=""))
    try:
        user = service.create_user({"username": "owner", "display_name": "Owner", "role": "admin",
                                    "password": "synthetic-migration-password-123"})
        assistant = service.save_assistant({"name": "Fixture", "connections": []})
        cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Migration fixture"}, user)["id"]
        result, _ = service.prepare_run(cid, {"text": "fixture", "file_ids": [],
                                              "client_request_id": "migration-123"}, user)
        service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump({"model": "fixture", "effort": effort}), result["run_id"]))
        service.db.execute("UPDATE conversations SET model_snapshot=NULL,active_run_id=NULL,runtime_type='claude' WHERE id=?", (cid,))
        service._migrate_model_selections()
        assert service.runtime_config(cid)["upstream_effort"] == effort
    finally:
        service.db.close()


@pytest.mark.parametrize("effort", ["unknown", "", True, {}, []])
def test_invalid_recorded_effort_is_not_guessed(tmp_path, effort):
    catalog = ModelCatalog(Settings(data_dir=tmp_path, provider_settings="", model_catalog=""))
    with pytest.raises(ValueError, match="historical reasoning effort"):
        catalog.historical("fixture", effort=effort)
