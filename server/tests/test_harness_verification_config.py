"""Portable checks for real-Harness fixture admission; no Node or provider calls."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.harness_attachments import require_image_capability
from app.harness_runtime import HarnessRuntime
from app.model_catalog import ModelCatalog
from app.security import AppError
from test_harness_real_runtime import image_fixture_settings


def test_real_image_fixture_explicitly_allows_only_synthetic_route(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER_TEST_MODE", "false")
    settings = image_fixture_settings(tmp_path, "/fixture/node", "/fixture/source", "http://127.0.0.1:9")
    assert settings.provider_test_mode is True
    assert ModelCatalog(settings).public()[0]["available"] is True


def test_real_image_fixture_passes_capability_guard_without_bypassing_it(tmp_path):
    settings = image_fixture_settings(tmp_path, "/fixture/node", "/fixture/source", "http://127.0.0.1:9")
    service = SimpleNamespace(catalog=ModelCatalog(settings))
    require_image_capability(service, "fixture")
    service.catalog.models["fixture"]["capabilities"] = []
    with pytest.raises(AppError) as caught:
        require_image_capability(service, "fixture")
    assert caught.value.code == "VISION_UNAVAILABLE"


class ConfigurationCaptured(Exception):
    pass


def connect_boundary(tmp_path, monkeypatch, provider, *, test_mode=False):
    """Execute the real connect admission up to SDK construction, never spawn."""
    captured = {}

    def profile(**kwargs):
        captured["profile"] = kwargs
        return {"profile": "workspace"}

    def config(**kwargs):
        captured["sdk"] = kwargs
        raise ConfigurationCaptured()

    monkeypatch.setattr("harness.profile.write_profile", profile)
    monkeypatch.setattr("app.harness_runtime.official_client", lambda _source: (object, config))
    monkeypatch.setattr("app.harness_runtime.write_launcher", lambda *_args: tmp_path / "never-launched")
    runtime = object.__new__(HarnessRuntime)
    runtime.service = SimpleNamespace(settings=Settings(
        data_dir=tmp_path / "data", provider_settings="", provider_test_mode=test_mode,
        harness_node_path=sys.executable, harness_source_dir=str(tmp_path / "source")))
    runtime.config = {"runtime_type": "harness", "provider_env": provider,
                      "upstream_model": "fixture", "upstream_effort": None,
                      "public_model_id": "fixture", "public_model_label": "Fixture", "instructions": ""}
    runtime.conversation_id = "fixture-conversation"
    runtime.session_id = "fixture-session"
    runtime.bridge = SimpleNamespace(definitions=lambda: [])
    return runtime, captured


@pytest.mark.parametrize("credentials,expected", [
    ({"ANTHROPIC_API_KEY": "synthetic-api-key"}, "synthetic-api-key"),
    ({"ANTHROPIC_AUTH_TOKEN": "synthetic-auth-token"}, "synthetic-auth-token"),
    ({"ANTHROPIC_API_KEY": "synthetic-api-key", "ANTHROPIC_AUTH_TOKEN": "synthetic-auth-token"}, "synthetic-api-key"),
    ({"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "synthetic-auth-token"}, "synthetic-auth-token"),
])
def test_connect_forwards_selected_credential_only_to_sdk_environment(tmp_path, monkeypatch, credentials, expected):
    runtime, captured = connect_boundary(tmp_path, monkeypatch, {
        "ANTHROPIC_BASE_URL": "https://tokenhub.tencentmaas.com", **credentials})
    with pytest.raises(ConfigurationCaptured):
        asyncio.run(runtime.connect())
    assert captured["sdk"]["env"] == {"WORKSPACE_MODEL_API_KEY": expected}
    assert captured["profile"]["provider"]["apiKeyEnv"] == "WORKSPACE_MODEL_API_KEY"
    assert all(value not in json.dumps(captured["profile"], default=str)
               for value in credentials.values() if value)


@pytest.mark.parametrize("base,credentials,code", [
    ("https://tokenhub.tencentmaas.com", {}, "HARNESS_PROVIDER_NOT_CONFIGURED"),
    ("https://api.deepseek.com/anthropic", {"ANTHROPIC_API_KEY": "synthetic-only"}, "TOKENHUB_ROUTE_REQUIRED"),
    ("http://127.0.0.1:9", {"ANTHROPIC_AUTH_TOKEN": "synthetic-only"}, "TOKENHUB_ROUTE_REQUIRED"),
])
def test_production_connect_rejects_missing_credentials_and_non_tokenhub_routes(tmp_path, monkeypatch, base, credentials, code):
    runtime, captured = connect_boundary(tmp_path, monkeypatch, {"ANTHROPIC_BASE_URL": base, **credentials})
    with pytest.raises(AppError) as caught:
        asyncio.run(runtime.connect())
    assert caught.value.code == code
    assert captured == {}


def test_connect_loopback_requires_explicit_test_mode(tmp_path, monkeypatch):
    runtime, captured = connect_boundary(tmp_path, monkeypatch, {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_API_KEY": "synthetic-only"}, test_mode=True)
    with pytest.raises(ConfigurationCaptured):
        asyncio.run(runtime.connect())
    assert captured["sdk"]["env"] == {"WORKSPACE_MODEL_API_KEY": "synthetic-only"}
