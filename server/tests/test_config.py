import json

import pytest

from app.config import DEFAULT_MODEL, PROVIDER_KEYS, Settings


@pytest.mark.parametrize("document,external,expected", [
    ({"model": "file-top", "env": {"ANTHROPIC_MODEL": "file-env"}},
     {"AGENT_MODEL": "explicit", "ANTHROPIC_MODEL": "external-provider"}, "explicit"),
    ({"model": "file-top", "env": {"ANTHROPIC_MODEL": "file-env"}},
     {"ANTHROPIC_MODEL": "external-provider"}, "external-provider"),
    ({"model": "file-top", "env": {"ANTHROPIC_MODEL": "file-env"}}, {}, "file-env"),
    ({"model": "file-top"}, {}, "file-top"),
    ({}, {}, DEFAULT_MODEL),
])
def test_model_configuration_precedence(tmp_path, monkeypatch, document, external, expected):
    for key in PROVIDER_KEYS | {"AGENT_MODEL"}:
        monkeypatch.delenv(key, raising=False)
    for key, value in external.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "provider.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    settings = Settings(provider_settings=str(path))
    assert settings.model == expected


def test_external_provider_environment_overrides_file(tmp_path, monkeypatch):
    for key in PROVIDER_KEYS | {"AGENT_MODEL"}:
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "provider.json"
    path.write_text(json.dumps({"model": "file-model", "env": {
        "ANTHROPIC_BASE_URL": "https://file.example",
        "ANTHROPIC_AUTH_TOKEN": "file-test-token",
    }, "hooks": {"SessionStart": "must-not-load"}}), encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://external.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "external-test-token")
    settings = Settings(provider_settings=str(path))
    assert settings.model == "file-model"
    assert settings.provider_env() == {
        "ANTHROPIC_MODEL": "file-model",
        "ANTHROPIC_BASE_URL": "https://external.example",
        "ANTHROPIC_AUTH_TOKEN": "external-test-token",
    }
