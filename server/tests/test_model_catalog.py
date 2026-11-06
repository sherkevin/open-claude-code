import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.context import cached_context, save_context
from app.db import dump
from app.main import create_app
from app.model_catalog import ModelCatalog
from app.runtime import SDKRuntime
from app.security import AppError
from app.service import Service
from test_api import FakeRuntime, poll, send


def catalog_settings(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "dano-v2.1", "models": [
        {"id": "deepseek-v4.1-flash", "label": "deepseek-v4.1-flash", "upstream_model": "deepseek-v4.1-flash", "reasoning_levels": {"default": None}},
        {"id": "glm-5.3", "label": "GLM-5.3", "upstream_model": "", "enabled": False},
        {"id": "dano-v2.1", "label": "Dano-v2.1", "upstream_model": "private-deployment-1017", "reasoning_levels": {"light": "low", "high": "high", "max": "max"}, "default_reasoning_level": "high",
         "provider_env": {"ANTHROPIC_BASE_URL": "https://private-provider.invalid", "ANTHROPIC_API_KEY": "private-key"}},
    ]}), encoding="utf-8")
    return Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), model="previous-model", provider_settings="", origins=("http://testserver",))


@pytest.fixture
def catalog_service(tmp_path):
    service = Service(catalog_settings(tmp_path))
    user = service.create_user({"username": "test", "display_name": "Test", "password": "long-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Test", "connections": []})
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Test"}, user)
    try:
        yield service, user, assistant, conversation
    finally:
        service.db.close()


def test_private_catalog_defaults_validation_and_effort_mapping(tmp_path):
    catalog = ModelCatalog(catalog_settings(tmp_path))
    assert [model["id"] for model in catalog.public()] == ["deepseek-v4.1-flash", "glm-5.3", "dano-v2.1"]
    public = dump(catalog.public())
    assert "private-" not in public and "1017" not in public
    assert catalog.defaults == {"default_model_id": "dano-v2.1", "default_reasoning_level": "high", "model_label": "Dano-v2.1"}
    for level, upstream in [("light", "low"), ("high", "high"), ("max", "max")]:
        assert catalog.select("dano-v2.1", level)["upstream_effort"] == upstream
    assert catalog.select("deepseek-v4.1-flash")["upstream_effort"] is None
    for model_id, level, code in [("missing", None, "UNKNOWN_MODEL"), ("glm-5.3", None, "MODEL_UNAVAILABLE"), ("dano-v2.1", "low", "UNSUPPORTED_REASONING_LEVEL")]:
        with pytest.raises(AppError) as failure:
            catalog.select(model_id, level)
        assert failure.value.code == code


def test_selection_switch_resets_context_preserves_native_session(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    assert conversation["model_id"] == "dano-v2.1" and conversation["reasoning_level"] == "high"
    service.db.execute("UPDATE conversations SET sdk_session_id='native-session',context_usage='{}' WHERE id=?", (cid,))
    old = service.runtime_config(cid)
    changed = service.patch_conversation(cid, {"model_id": "deepseek-v4.1-flash"}, user)
    assert changed["reasoning_level"] == "default"
    new = service.runtime_config(cid)
    assert old["fingerprint"] != new["fingerprint"]
    assert new["conversation"]["sdk_session_id"] == "native-session"
    assert new["conversation"]["context_usage"] is None
    changed = service.patch_conversation(cid, {"model_id": "dano-v2.1", "reasoning_level": "light"}, user)
    assert changed["reasoning_level"] == "light"


def test_active_run_snapshot_and_context_switch_barrier(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    service.manager = SimpleNamespace(context_requests={cid: True})
    with pytest.raises(AppError, match="上下文"):
        service.patch_conversation(cid, {"model_id": "deepseek-v4.1-flash"}, user)
    service.manager.context_requests.clear()
    result, _ = service.prepare_run(cid, {"text": "test", "file_ids": [], "client_request_id": "test-snapshot"}, user)
    with pytest.raises(AppError) as error:
        service.patch_conversation(cid, {"reasoning_level": "max"}, user)
    assert error.value.code == "CONVERSATION_BUSY"
    run = service.require("runs", result["run_id"])
    metadata = json.loads(run["execution_meta"])
    assert metadata["model"] == "private-deployment-1017" and metadata["provider"] == "private-provider.invalid"
    assert metadata["selection"]["reasoning_level"] == "high"
    # Even an internal accidental row edit cannot alter an already prepared run.
    service._save_model_selection(cid, service.catalog.select("deepseek-v4.1-flash"))
    assert service.runtime_config(cid)["upstream_model"] == "private-deployment-1017"


def test_queued_run_rejects_catalog_retargeting(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    service.catalog.models["dano-v2.1"]["upstream_model"] = "changed-route"
    with pytest.raises(AppError) as error:
        service.prepare_run(cid, {"text": "test", "file_ids": [], "client_request_id": "test-repin"}, user)
    assert error.value.code == "MODEL_SELECTION_REQUIRED"
    assert not service.db.all("SELECT * FROM runs")
    service.patch_conversation(cid, {"model_id": "dano-v2.1"}, user)
    assert service.runtime_config(cid)["upstream_model"] == "changed-route"


def test_existing_conversation_migration_keeps_recorded_route(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    result, _ = service.prepare_run(cid, {"text": "test", "file_ids": [], "client_request_id": "legacy-run"}, user)
    service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump({"model": "previous-model", "provider": "private-host"}), result["run_id"]))
    service.db.execute("UPDATE conversations SET model_snapshot=NULL,model_id=NULL,active_run_id=NULL WHERE id=?", (cid,))
    service._migrate_model_selections()
    row = service.require("conversations", cid)
    assert row["model_id"] == "legacy-model" and row["model_label"] == "历史模型"
    assert json.loads(row["model_snapshot"])["upstream_model"] == "previous-model"
    assert service.runtime_config(cid)["upstream_model"] == "previous-model"
    assert service.public_run_model(service.require("runs", result["run_id"]))["model"] == "历史模型"


def test_context_public_metadata_never_reports_provider_model(catalog_service):
    service, _, _, conversation = catalog_service
    cid = conversation["id"]
    save_context(service, cid, {"model": "private-deployment-1017", "measured_at": None})
    assert cached_context(service, cid)["model"] == "Dano-v2.1"
    service.db.execute("UPDATE conversations SET context_usage=? WHERE id=?", (dump({"model": "private-historical-route"}), cid))
    assert cached_context(service, cid)["model"] == "Dano-v2.1"


@pytest.mark.parametrize("level,effort", [("light", "low"), ("high", "high"), ("max", "max")])
def test_sdk_receives_selection_and_all_model_aliases(catalog_service, monkeypatch, level, effort):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    service.patch_conversation(cid, {"reasoning_level": level}, user)
    service.settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "old-key", "ANTHROPIC_MODEL": "old-model", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "old-small-model"}
    captured = {}
    class Client:
        def __init__(self, options):
            captured["options"] = options
        async def connect(self):
            pass
        async def disconnect(self):
            pass
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)
    async def scenario():
        runtime = SDKRuntime(service, cid, service.runtime_config(cid), None, None)
        await runtime.connect()
        await runtime.close()
    asyncio.run(scenario())
    options = captured["options"]
    assert options.model == "private-deployment-1017" and options.effort == effort
    assert options.env["ANTHROPIC_BASE_URL"] == "https://private-provider.invalid"
    for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
        assert options.env[key] == options.model
    assert options.system_prompt["type"] == "custom" and options.system_prompt["snapshot"] is False
    assert "private-deployment-1017" not in options.system_prompt["prompt"]


def test_public_api_catalog_conversation_trace_and_switch_rebuild(tmp_path):
    settings = catalog_settings(tmp_path)
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "test", "display_name": "Test", "password": "long-test-password-123", "role": "admin"})
        assert client.get("/api/v1/models").status_code == 401
        client.post("/api/v1/auth/login", json={"username": "test", "password": "long-test-password-123"})
        config = client.get("/api/v1/config").json()
        assert config["default_model_id"] == "dano-v2.1"
        assert client.get("/api/v1/models").json()[1]["available"] is False
        assistant = service.save_assistant({"name": "Test", "connections": []})
        new = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "model_id": "dano-v2.1", "reasoning_level": "light"})
        assert new.status_code == 201
        cid = new.json()["id"]
        before = FakeRuntime.connects
        first = send(client, cid, request_id="catalog-run-0001")
        assert poll(client, first["run_id"])["status"] == "succeeded"
        service.db.event(first["run_id"], "context.compaction_result", {"before": {"model": "private-historical-route"}})
        trace = client.get(f"/api/v1/runs/{first['run_id']}/trace")
        # IDs and timestamps can coincidentally contain these four digits.
        assert "private-" not in trace.text and "deepseek-v4.1-flash-1017" not in trace.text
        assert trace.json()["run"]["model"] == "Dano-v2.1"
        assert trace.json()["run"]["reasoning_level"] == "light"
        assert "private-historical-route" not in client.get(f"/api/v1/runs/{first['run_id']}/events").text
        failed = client.patch(f"/api/v1/conversations/{cid}", json={"model_id": "glm-5.3"})
        assert failed.status_code == 503 and client.get(f"/api/v1/conversations/{cid}").json()["model_id"] == "dano-v2.1"
        changed = client.patch(f"/api/v1/conversations/{cid}", json={"model_id": "deepseek-v4.1-flash"})
        assert changed.status_code == 200 and changed.json()["reasoning_level"] == "default"
        second = send(client, cid, request_id="catalog-run-0002")
        assert poll(client, second["run_id"])["status"] == "succeeded"
        assert FakeRuntime.connects == before + 2
        assert service.public_run_model(service.require("runs", first["run_id"]))["model"] == "Dano-v2.1"


@pytest.mark.parametrize("source", ["global", "model"])
@pytest.mark.parametrize("field", ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"])
def test_provider_changes_reject_prepared_runs_without_persisting_credentials(catalog_service, source, field):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    initial = {"ANTHROPIC_BASE_URL": "https://old-provider.invalid", "ANTHROPIC_API_KEY": "old-private-key"}
    if source == "global":
        current = dict(initial)
        service.settings.provider_env = lambda: dict(current)
        model_id = "deepseek-v4.1-flash"
    else:
        current = service.catalog.models["dano-v2.1"]["provider_env"]
        current.update(initial)
        model_id = "dano-v2.1"
    service.patch_conversation(cid, {"model_id": model_id}, user)
    before = service.runtime_config(cid)
    first, _ = service.prepare_run(cid, {"text": "test", "file_ids": [], "client_request_id": "provider-pin-0001"}, user)
    row = service.require("runs", first["run_id"])
    assert "old-private-key" not in row["execution_meta"]
    assert "old-private-key" not in service.require("conversations", cid)["model_snapshot"]
    assert json.loads(row["execution_meta"])["provider"] == "old-provider.invalid"
    current[field] = "https://new-provider.invalid" if field.endswith("URL") else "new-private-key"
    with pytest.raises(AppError) as failure:
        service.runtime_config(cid)
    assert failure.value.code == "MODEL_SELECTION_REQUIRED"
    service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (cid,))
    service.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (first["run_id"],))
    with pytest.raises(AppError) as failure:
        service.prepare_run(cid, {"text": "test", "file_ids": [], "client_request_id": "provider-pin-0002"}, user)
    assert failure.value.code == "MODEL_SELECTION_REQUIRED"
    service.patch_conversation(cid, {"model_id": model_id}, user)
    after = service.runtime_config(cid)
    assert after["fingerprint"] != before["fingerprint"]
    assert after["provider_env"][field] == current[field]
    assert "new-private-key" not in service.require("conversations", cid)["model_snapshot"]


def test_runtime_connect_uses_validated_memory_environment(catalog_service, monkeypatch):
    service, _, _, conversation = catalog_service
    config = service.runtime_config(conversation["id"])
    def changed_provider():
        raise AssertionError("connect must not read provider configuration again")
    service.settings.provider_env = changed_provider
    captured = {}
    class Client:
        def __init__(self, options):
            captured["env"] = options.env
        async def connect(self):
            pass
        async def disconnect(self):
            pass
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)
    async def scenario():
        runtime = SDKRuntime(service, conversation["id"], config, None, None)
        await runtime.connect()
        await runtime.close()
    asyncio.run(scenario())
    assert captured["env"]["ANTHROPIC_BASE_URL"] == config["provider_env"]["ANTHROPIC_BASE_URL"]
    assert captured["env"]["ANTHROPIC_API_KEY"] == config["provider_env"]["ANTHROPIC_API_KEY"]


def test_provider_digest_upgrade_is_idempotent_and_does_not_change_history(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    result, _ = service.prepare_run(cid, {"text": "test", "file_ids": [], "client_request_id": "provider-old-run"}, user)
    snapshot = json.loads(service.require("conversations", cid)["model_snapshot"])
    snapshot.pop("provider_fingerprint")
    service.db.execute("UPDATE conversations SET model_snapshot=? WHERE id=?", (dump(snapshot), cid))
    historical_metadata = dump({"model": "private-deployment-1017", "provider": "historical-provider.invalid", "selection": snapshot})
    service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (historical_metadata, result["run_id"]))
    service._migrate_model_selections()
    updated = service.require("conversations", cid)["model_snapshot"]
    assert len(json.loads(updated)["provider_fingerprint"]) == 64
    service._migrate_model_selections()
    assert service.require("conversations", cid)["model_snapshot"] == updated
    assert service.require("runs", result["run_id"])["execution_meta"] == historical_metadata


def test_provider_reselection_rebuilds_warm_actor(tmp_path):
    settings = catalog_settings(tmp_path)
    current = {"ANTHROPIC_BASE_URL": "https://first.invalid", "ANTHROPIC_API_KEY": "first-key"}
    settings.provider_env = lambda: dict(current)
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "test", "display_name": "Test", "password": "long-test-password-123", "role": "admin"})
        client.post("/api/v1/auth/login", json={"username": "test", "password": "long-test-password-123"})
        assistant = service.save_assistant({"name": "Test", "connections": []})
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "model_id": "deepseek-v4.1-flash"}).json()["id"]
        before = FakeRuntime.connects
        first = send(client, cid, request_id="provider-actor-0001")
        assert poll(client, first["run_id"])["status"] == "succeeded"
        current["ANTHROPIC_BASE_URL"] = "https://second.invalid"
        assert client.post(f"/api/v1/conversations/{cid}/messages", json={"text": "test", "client_request_id": "provider-actor-0002"}).status_code == 409
        assert client.patch(f"/api/v1/conversations/{cid}", json={"model_id": "deepseek-v4.1-flash"}).status_code == 200
        second = send(client, cid, request_id="provider-actor-0002")
        assert poll(client, second["run_id"])["status"] == "succeeded"
        assert FakeRuntime.connects == before + 2
        assert json.loads(service.require("runs", first["run_id"])["execution_meta"])["provider"] == "first.invalid"
        assert json.loads(service.require("runs", second["run_id"])["execution_meta"])["provider"] == "second.invalid"


def test_native_actor_switch_rebuilds_system_prompt_and_resumes_same_session(tmp_path, monkeypatch):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
    settings = catalog_settings(tmp_path)
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "test-only"}
    captured, inputs = [], []
    class Client:
        def __init__(self, options):
            captured.append(options)
        async def connect(self):
            pass
        async def query(self, prompt):
            inputs.extend([message async for message in prompt])
        async def receive_response(self):
            yield AssistantMessage([TextBlock("native fixture answer")], "fixture")
            yield ResultMessage("success", 10, 5, False, 1, "same-native-session", usage={"input_tokens": 7, "output_tokens": 2})
        async def disconnect(self):
            pass
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        service.catalog.models["glm-5.3"].update(upstream_model="glm-5.3", enabled=True)
        service.create_user({"username": "test", "display_name": "Test", "password": "long-test-password-123", "role": "admin"})
        client.post("/api/v1/auth/login", json={"username": "test", "password": "long-test-password-123"})
        assistant = service.save_assistant({"name": "Test", "connections": []})
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        selections = [("dano-v2.1", "Dano-v2.1", "private-deployment-1017"), ("deepseek-v4.1-flash", "deepseek-v4.1-flash", "deepseek-v4.1-flash"), ("glm-5.3", "GLM-5.3", "glm-5.3")]
        for index, (identity, label, upstream) in enumerate(selections):
            if index:
                assert client.patch(f"/api/v1/conversations/{cid}", json={"model_id": identity}).status_code == 200
            run = send(client, cid, request_id=f"identity-switch-{index:04d}")
            assert poll(client, run["run_id"])["status"] == "succeeded"
            options = captured[-1]
            assert options.model == upstream
            assert options.system_prompt["type"] == "custom" and options.system_prompt["snapshot"] is False
            if index:
                assert options.resume == "same-native-session"
                assert f"当前公开模型选项为 {label}" in options.system_prompt["prompt"]
                assert "你是 Dano-v2.1" not in options.system_prompt["prompt"]
            else:
                assert options.resume is None and "你是 Dano-v2.1" in options.system_prompt["prompt"]
        assert len(captured) == len(inputs) == 3
        assert all(message["client_composed"] is True for message in inputs)


def test_protected_model_switch_preserves_native_history_and_context(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    service.catalog.models["glm-5.3"].update(upstream_model="glm-5.3", enabled=True, requires_new_conversation_on_switch=True)
    result, _ = service.prepare_run(cid, {"text": "retain this history", "file_ids": [], "client_request_id": "switch-history-0001"}, user)
    service.db.execute("UPDATE runs SET status='succeeded' WHERE id=?", (result["run_id"],))
    service.db.execute("UPDATE conversations SET active_run_id=NULL,sdk_session_id='native-history',context_usage=? WHERE id=?", (dump({"total_tokens": 123}), cid))
    before = service.require("conversations", cid)
    messages = service.db.all("SELECT * FROM messages WHERE conversation_id=?", (cid,))
    with pytest.raises(AppError) as failure:
        service.patch_conversation(cid, {"model_id": "glm-5.3", "title": "must not change"}, user)
    assert failure.value.code == "MODEL_NEW_CONVERSATION_REQUIRED"
    assert failure.value.message == "请新建对话使用此模型；原对话和记忆保留"
    assert service.require("conversations", cid) == before
    assert service.db.all("SELECT * FROM messages WHERE conversation_id=?", (cid,)) == messages
    changed = service.patch_conversation(cid, {"model_id": "deepseek-v4.1-flash"}, user)
    assert changed["has_native_session"] and changed["model_id"] == "deepseek-v4.1-flash"


def test_protected_model_allows_empty_chat_and_same_model_effort(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    service.catalog.models["glm-5.3"].update(upstream_model="glm-5.3", enabled=True, requires_new_conversation_on_switch=True)
    option = next(item for item in service.catalog.public() if item["id"] == "glm-5.3")
    assert option["requires_new_conversation_on_switch"] is True
    assert not conversation["has_native_session"]
    changed = service.patch_conversation(cid, {"model_id": "glm-5.3"}, user)
    assert changed["model_id"] == "glm-5.3" and not changed["has_native_session"]
    service.db.execute("UPDATE conversations SET sdk_session_id='glm-history' WHERE id=?", (cid,))
    same = service.patch_conversation(cid, {"reasoning_level": "default"}, user)
    assert same["has_native_session"] and same["model_id"] == "glm-5.3"
    with pytest.raises(AppError) as failure:
        service.patch_conversation(cid, {"model_id": "deepseek-v4.1-flash"}, user)
    assert failure.value.code == "MODEL_NEW_CONVERSATION_REQUIRED"


def test_legacy_upstream_alias_cannot_bypass_protected_switch(catalog_service):
    service, user, _, conversation = catalog_service
    cid = conversation["id"]
    service.catalog.models["glm-5.3"].update(upstream_model="glm-5.3", enabled=True, requires_new_conversation_on_switch=True)
    legacy = {"model_id": "legacy-model", "model_label": "历史模型", "reasoning_level": "medium", "upstream_model": "glm-5.3", "upstream_effort": "medium", "legacy": True}
    service._save_model_selection(cid, legacy)
    service.db.execute("UPDATE conversations SET sdk_session_id='legacy-glm-history' WHERE id=?", (cid,))
    before = service.require("conversations", cid)
    with pytest.raises(AppError) as failure:
        service.patch_conversation(cid, {"model_id": "dano-v2.1"}, user)
    assert failure.value.code == "MODEL_NEW_CONVERSATION_REQUIRED"
    assert service.require("conversations", cid) == before


def test_protected_switch_api_returns_capability_and_preserves_chat(tmp_path):
    app = create_app(catalog_settings(tmp_path), runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.catalog.models["glm-5.3"].update(upstream_model="glm-5.3", enabled=True, requires_new_conversation_on_switch=True)
        service.create_user({"username": "test", "display_name": "Test", "password": "long-test-password-123", "role": "admin"})
        client.post("/api/v1/auth/login", json={"username": "test", "password": "long-test-password-123"})
        assistant = service.save_assistant({"name": "Test", "connections": []})
        created = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        assert created["has_native_session"] is False
        option = next(item for item in client.get("/api/v1/models").json() if item["id"] == "glm-5.3")
        assert option["requires_new_conversation_on_switch"] is True
        service.db.execute("UPDATE conversations SET sdk_session_id='preserved-native-session' WHERE id=?", (created["id"],))
        before = client.get(f"/api/v1/conversations/{created['id']}").json()
        assert before["has_native_session"] is True
        response = client.patch(f"/api/v1/conversations/{created['id']}", json={"model_id": "glm-5.3"})
        assert response.status_code == 409 and response.json()["error"]["code"] == "MODEL_NEW_CONVERSATION_REQUIRED"
        assert client.get(f"/api/v1/conversations/{created['id']}").json() == before
        fresh = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "model_id": "glm-5.3"})
        assert fresh.status_code == 201 and fresh.json()["model_id"] == "glm-5.3"
