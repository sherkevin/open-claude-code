"""Executor selection, legacy migration and native session isolation; no models."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db import dump, now
from app.models import ConversationCreate, ConversationPatch
from app.runtime import SDKRuntime, configured_runtime
from app.security import AppError
from app.service import Service, conversation_view


@pytest.fixture
def system(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [
        {"id": "fixture", "label": "Fixture", "upstream_model": "fixture-upstream"},
        {"id": "protected", "label": "Protected", "upstream_model": "protected-upstream",
         "requires_new_conversation_on_switch": True},
    ]}), encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data", model_catalog=str(catalog),
                        provider_settings="", model="fixture-upstream", runtime_type="claude")
    service = Service(settings)
    user = service.create_user({"username": "test", "display_name": "Test", "role": "admin",
                                "password": "runtime-test-password-123"})
    assistant = service.save_assistant({"name": "Test", "connections": []})
    try:
        yield service, user, assistant
    finally:
        service.db.close()


def create(service, user, assistant, **extra):
    return service.create_conversation({"assistant_id": assistant["id"], "title": "Test", **extra}, user)


def test_new_default_is_harness_and_explicit_rollback_only_affects_new_sessions(system, monkeypatch):
    service, user, assistant = system
    claude = create(service, user, assistant)
    monkeypatch.delenv("AGENT_RUNTIME_TYPE", raising=False)
    service.settings.runtime_type = Settings(model="fixture-upstream", provider_settings="").runtime_type
    harness = create(service, user, assistant, runtime_type="claude")
    service.memory.snapshot_for_run(user["id"], harness["id"])
    service.db.execute("UPDATE conversations SET harness_session_id='owned-harness' WHERE id=?", (harness["id"],))
    assert harness["runtime_type"] == "harness"
    assert service.runtime_config(claude["id"])["runtime_type"] == "claude"

    monkeypatch.setenv("AGENT_RUNTIME_TYPE", "claude")
    service.settings.runtime_type = Settings(model="fixture-upstream", provider_settings="").runtime_type
    rolled_back = create(service, user, assistant)
    assert rolled_back["runtime_type"] == "claude"
    assert service.runtime_config(harness["id"])["runtime_type"] == "harness"
    assert service.runtime_config(harness["id"])["native_session_id"] == "owned-harness"


@pytest.mark.parametrize("node,source", [("", ""), ("/explicit/node", ""), ("", "/explicit/harness")])
def test_unconfigured_default_harness_fails_without_discovery_or_claude_fallback(system, monkeypatch, node, source):
    from app import harness_runtime, runtime

    service, user, assistant = system
    monkeypatch.delenv("AGENT_RUNTIME_TYPE", raising=False)
    service.settings.runtime_type = Settings(model="fixture-upstream", provider_settings="").runtime_type
    service.settings.harness_node_path, service.settings.harness_source_dir = node, source
    cid = create(service, user, assistant)["id"]
    config = service.runtime_config(cid)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Unconfigured Harness must fail before loading a transport or falling back")

    monkeypatch.setattr(runtime, "SDKRuntime", forbidden)
    monkeypatch.setattr(harness_runtime, "official_client", forbidden)
    monkeypatch.setattr(harness_runtime, "write_launcher", forbidden)
    selected = configured_runtime(service, cid, config, None, None)
    assert isinstance(selected, harness_runtime.HarnessRuntime)
    with pytest.raises(AppError) as error:
        asyncio.run(selected.connect())
    assert error.value.code == "HARNESS_NOT_CONFIGURED"
    assert selected.client is None
    assert not (service.settings.data_dir / "harness").exists()
    assert service.require("conversations", cid)["runtime_type"] == "harness"


def test_factory_keeps_legacy_missing_runtime_claude_and_rejects_unknown(system):
    service, user, assistant = system
    cid = create(service, user, assistant)["id"]
    config = service.runtime_config(cid)
    config.pop("runtime_type")
    assert isinstance(configured_runtime(service, cid, config, None, None), SDKRuntime)
    with pytest.raises(AppError) as error:
        configured_runtime(service, cid, {**config, "runtime_type": "unreviewed"}, None, None)
    assert error.value.code == "RUNTIME_UNAVAILABLE"


def test_legacy_database_upgrade_keeps_claude_identity_and_history(system):
    service, user, assistant = system
    cid = create(service, user, assistant)["id"]
    service.db.execute("UPDATE conversations SET sdk_session_id='old-claude',sdk_cost_total=1.25 WHERE id=?", (cid,))
    service.db.execute("INSERT INTO messages(id,conversation_id,role,text,created_at) VALUES('legacy-message',?,'user','retained history',?)", (cid, now()))
    original_snapshot = service.require("conversations", cid)["model_snapshot"]
    # Reproduce the previous on-disk schema, then upgrade while the deployment
    # default selects Harness. The migration must not retarget existing rows.
    service.db.conn.execute("ALTER TABLE conversations DROP COLUMN runtime_type")
    service.db.conn.execute("ALTER TABLE conversations DROP COLUMN harness_session_id")
    service.db.close()
    service.settings.runtime_type = "harness"
    upgraded = Service(service.settings)
    try:
        row = upgraded.require("conversations", cid)
        assert row["runtime_type"] == "claude"
        assert row["sdk_session_id"] == "old-claude" and row["harness_session_id"] is None
        assert row["sdk_cost_total"] == 1.25 and row["model_snapshot"] == original_snapshot
        assert upgraded.require("messages", "legacy-message")["text"] == "retained history"
        assert upgraded.runtime_config(cid)["native_session_id"] == "old-claude"
        assert create(upgraded, user, assistant)["runtime_type"] == "harness"
    finally:
        upgraded.db.close()
    # A rollback of the deployment default cannot reinterpret Harness sessions.
    service.settings.runtime_type = "claude"
    reopened = Service(service.settings)
    try:
        types = {row["runtime_type"] for row in reopened.db.all("SELECT * FROM conversations")}
        assert types == {"claude", "harness"}
        assert reopened.require("conversations", cid)["sdk_session_id"] == "old-claude"
    finally:
        reopened.db.close()


@pytest.mark.parametrize("setting,value", [
    ("harness_node_path", "/reviewed/node"),
    ("harness_source_dir", "/reviewed/harness"),
    ("harness_max_output_tokens", 4096),
])
def test_default_changes_only_new_conversations_and_launch_changes_only_harness_fingerprint(system, setting, value):
    service, user, assistant = system
    claude = create(service, user, assistant)
    before = service.runtime_config(claude["id"])
    service.settings.runtime_type = "harness"
    harness = create(service, user, assistant, runtime_type="claude")
    harness_before = service.runtime_config(harness["id"])
    assert harness["runtime_type"] == "harness"
    service.patch_conversation(claude["id"], {"title": "Renamed", "runtime_type": "harness"}, user)
    setattr(service.settings, setting, value)
    assert service.runtime_config(claude["id"])["fingerprint"] == before["fingerprint"]
    assert service.runtime_config(harness["id"])["fingerprint"] != harness_before["fingerprint"]
    assert service.require("conversations", claude["id"])["runtime_type"] == "claude"


@pytest.mark.parametrize("runtime_type,own_key,other_key", [
    ("claude", "sdk_session_id", "harness_session_id"),
    ("harness", "harness_session_id", "sdk_session_id"),
])
def test_runtime_config_never_resumes_another_executors_native_session(system, runtime_type, own_key, other_key):
    service, user, assistant = system
    service.settings.runtime_type = runtime_type
    cid = create(service, user, assistant)["id"]
    service.db.execute(f"UPDATE conversations SET {other_key}='foreign-session' WHERE id=?", (cid,))
    config = service.runtime_config(cid)
    assert config["runtime_type"] == runtime_type and config["native_session_id"] is None
    assert config["conversation"][other_key] is None
    assert not conversation_view(service.require("conversations", cid))["has_native_session"]
    service.db.execute(f"UPDATE conversations SET {own_key}='own-session' WHERE id=?", (cid,))
    config = service.runtime_config(cid)
    assert config["native_session_id"] == config["conversation"][own_key] == "own-session"
    assert config["conversation"][other_key] is None
    stored = service.require("conversations", cid)
    assert stored[other_key] == "foreign-session"  # Stored history is not erased.
    view = conversation_view(stored)
    assert view["has_native_session"] and "own-session" not in dump(view)
    assert "foreign-session" not in dump(view)


@pytest.mark.parametrize("entrypoint", ["patch", "request", "automatic"])
def test_harness_native_history_keeps_model_switch_guards(system, entrypoint):
    service, user, assistant = system
    service.settings.runtime_type = "harness"
    cid = create(service, user, assistant)["id"]
    service.memory.snapshot_for_run(user["id"], cid)
    service.db.execute("UPDATE conversations SET harness_session_id='harness-history' WHERE id=?", (cid,))
    payload = {"text": "fixture task", "file_ids": [], "client_request_id": "model-switch"}
    if entrypoint == "automatic":
        payload.update(auto_route=True, _route_recommendation={"model_id": "protected", "reasoning_level": "default", "reason": "fixture"})
        result, _ = service.prepare_run(cid, payload, user)
        metadata = json.loads(service.require("runs", result["run_id"])["execution_meta"])
        assert metadata["selection"]["model_id"] == "fixture"
        assert metadata["model_routing"]["mode"] == "fallback"
    else:
        with pytest.raises(AppError) as error:
            if entrypoint == "patch":
                service.patch_conversation(cid, {"model_id": "protected"}, user)
            else:
                service.prepare_run(cid, {**payload, "model_id": "protected"}, user)
        assert error.value.code == "MODEL_NEW_CONVERSATION_REQUIRED"
        assert not service.db.all("SELECT * FROM runs")
    assert service.require("conversations", cid)["harness_session_id"] == "harness-history"


def test_active_run_pins_runtime_and_refuses_cross_executor_replay(system):
    service, user, assistant = system
    service.settings.runtime_type = "harness"
    cid = create(service, user, assistant)["id"]
    result, _ = service.prepare_run(cid, {"text": "fixture", "file_ids": [], "client_request_id": "runtime-pin"}, user)
    run = service.require("runs", result["run_id"])
    assert json.loads(run["execution_meta"])["runtime_type"] == "harness"
    service.settings.runtime_type = "claude"
    assert service.runtime_config(cid)["runtime_type"] == "harness"
    metadata = json.loads(run["execution_meta"])
    metadata.pop("runtime_type")
    service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump(metadata), run["id"]))
    with pytest.raises(AppError) as error:
        service.runtime_config(cid)
    assert error.value.code == "RUNTIME_TYPE_CHANGED"


@pytest.mark.parametrize("field", ["runtime_type", "sdk_session_id", "harness_session_id", "harness_node_path", "harness_source_dir", "harness_max_output_tokens"])
def test_conversation_api_cannot_choose_executor_or_launch_configuration(field):
    with pytest.raises(ValidationError):
        ConversationCreate(assistant_id="assistant", **{field: "untrusted"})
    with pytest.raises(ValidationError):
        ConversationPatch(**{field: "untrusted"})
