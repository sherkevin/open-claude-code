import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_api import PASSWORD, login


def _app(tmp_path, *, provider=False):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "dano-v2.1", "models": [
        {"id": "dano-v2.1", "label": "Dano", "upstream_model": "dano", "capabilities": ["industrial", "chinese"], "reasoning_levels": {"default": None}},
        {"id": "vision", "label": "视觉模型", "upstream_model": "vision", "capabilities": ["multimodal", "entity_extraction", "relation_extraction"], "reasoning_levels": {"default": None}},
        {"id": "embedding", "label": "向量模型", "upstream_model": "embedding", "capabilities": ["embedding"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider_path = ""
    if provider:
        provider = tmp_path / "provider.json"
        provider.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "test-only-token", "ANTHROPIC_BASE_URL": "http://provider.test/v1"}}), encoding="utf-8")
        provider_path = str(provider)
    return create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), provider_settings=provider_path, origins=("http://testserver",), model_router_enabled=True, knowledge_vision_model_id="vision", knowledge_vision_base_url=""))


def test_provider_preflight_is_authenticated_and_fail_closed(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/v1/models/provider-preflight").status_code == 401
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        value = client.get("/api/v1/models/provider-preflight").json()
        assert value["schema_version"] == "provider-preflight/v1"
        assert value["provider_attempted"] is False
        assert value["roles"]["vision"]["target_model_id"] == "vision"
        assert value["roles"]["vision"]["status"] == "provider_unconfigured"
        assert value["roles"]["embedding"]["target_model_id"] == "embedding"
        assert value["roles"]["relation_extraction"]["target_model_id"] == "vision"
        assert value["roles"]["vision"]["call_enabled"] is False
        # The browser may inspect readiness, but must not learn the upstream
        # gateway or its internal route classification.
        assert "provider_route" not in value["roles"]["vision"]
        assert "tokenhub_route" not in value["roles"]["vision"]
        assert "tokenhub" not in json.dumps(value, ensure_ascii=False).lower()
        assert "upstream" not in json.dumps(value, ensure_ascii=False).lower()


def test_provider_preflight_reports_explicit_missing_role_without_fallback(tmp_path):
    app = _app(tmp_path)
    # Settings are immutable only by convention; assigning an explicit ID is
    # enough to verify that an unknown role does not reuse the chat model.
    with TestClient(app) as client:
        service = app.state.service
        service.settings.knowledge_vision_model_id = "does-not-exist"
        service.settings.knowledge_entity_model_id = "does-not-exist"
        service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        value = client.get("/api/v1/models/provider-preflight").json()
        assert value["roles"]["vision"]["status"] == "unavailable"
        assert value["roles"]["entity_extraction"]["status"] == "unavailable"
        assert value["roles"]["vision"]["target_model_id"] == "does-not-exist"


def test_default_loopback_endpoint_is_not_provider_authorization(tmp_path):
    """A default local URL alone must never make a role look configured."""
    app = _app(tmp_path)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        value = client.get("/api/v1/models/provider-preflight").json()
        vision = value["roles"]["vision"]
        assert vision["provider_configured"] is False
        assert vision["provider_status"] == "credentials_missing"
        assert vision["status"] == "provider_unconfigured"
        assert vision["call_enabled"] is False
        assert vision["feature_enabled"] is False


def test_feature_gate_does_not_grant_call_permission(tmp_path):
    """Even an authorized, explicitly enabled role remains preflight-only."""
    app = _app(tmp_path, provider=True)
    with TestClient(app) as client:
        service = app.state.service
        service.settings.knowledge_vision_enabled = True
        service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        vision = client.get("/api/v1/models/provider-preflight").json()["roles"]["vision"]
        assert vision["provider_configured"] is True
        assert vision["feature_enabled"] is True
        assert vision["call_enabled"] is False
        assert vision["status"] == "ready"


def test_malformed_provider_settings_fail_closed(tmp_path):
    app = _app(tmp_path)
    path = tmp_path / "broken-provider.json"
    path.write_text("not-json", encoding="utf-8")
    with TestClient(app) as client:
        app.state.service.settings.provider_settings = str(path)
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        response = client.get("/api/v1/models/provider-preflight")
        assert response.status_code == 200
        value = response.json()
        assert value["roles"]["vision"]["provider_configured"] is False
        assert value["roles"]["vision"]["provider_status"] == "provider_invalid"
