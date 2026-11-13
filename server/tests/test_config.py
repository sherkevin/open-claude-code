import json
from pathlib import Path

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


def test_router_defaults_to_exact_target_and_can_only_be_overridden_explicitly(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL_ROUTER_MODEL_ID", raising=False)
    monkeypatch.delenv("AGENT_TAB_COMPLETION_MODEL_ID", raising=False)
    monkeypatch.delenv("AGENT_TAB_COMPLETION_ENABLED", raising=False)
    settings = Settings(provider_settings="")
    assert settings.model_router_model_id == "deepseek-v4.1-flash-1007"
    assert settings.tab_completion_model_id == "deepseek-v4.1-flash-1017"
    assert settings.tab_completion_enabled is True
    monkeypatch.setenv("AGENT_MODEL_ROUTER_MODEL_ID", "approved-test-route")
    monkeypatch.setenv("AGENT_TAB_COMPLETION_MODEL_ID", "approved-tab-route")
    monkeypatch.setenv("AGENT_TAB_COMPLETION_ENABLED", "false")
    overridden = Settings(provider_settings="")
    assert overridden.model_router_model_id == "approved-test-route"
    assert overridden.tab_completion_model_id == "approved-tab-route"
    assert overridden.tab_completion_enabled is False


def test_runtime_default_and_harness_paths_are_explicit_server_configuration(monkeypatch):
    for key in ("AGENT_RUNTIME_TYPE", "AGENT_EFFORT", "AGENT_HARNESS_NODE_PATH", "AGENT_HARNESS_SOURCE_DIR", "AGENT_HARNESS_MAX_OUTPUT_TOKENS", "AGENT_HARNESS_COMPACTION_TIMEOUT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(provider_settings="")
    assert settings.runtime_type == "harness"
    assert settings.effort == "high" and settings.legacy_effort == "medium"
    assert settings.harness_node_path == settings.harness_source_dir == ""
    assert settings.harness_max_output_tokens == 8192
    assert settings.harness_compaction_timeout_seconds == 600
    monkeypatch.setenv("AGENT_RUNTIME_TYPE", "harness")
    monkeypatch.setenv("AGENT_HARNESS_NODE_PATH", "/isolated/node")
    monkeypatch.setenv("AGENT_HARNESS_SOURCE_DIR", "/isolated/harness")
    settings = Settings(provider_settings="")
    assert settings.runtime_type == "harness"
    assert settings.harness_node_path == "/isolated/node"
    assert settings.harness_source_dir == "/isolated/harness"


@pytest.mark.parametrize("runtime_type", ["claude", " CLAUDE "])
def test_claude_rollback_requires_explicit_server_configuration(monkeypatch, runtime_type):
    monkeypatch.delenv("AGENT_EFFORT", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_TYPE", runtime_type)
    settings = Settings(provider_settings="")
    assert settings.runtime_type == "claude"
    assert settings.effort == settings.legacy_effort == "medium"
    monkeypatch.setenv("AGENT_RUNTIME_TYPE", "harness")
    overridden = Settings(runtime_type="claude", provider_settings="")
    assert overridden.runtime_type == "claude"
    assert overridden.effort == overridden.legacy_effort == "medium"


@pytest.mark.parametrize("runtime_type", ["harness", "claude"])
@pytest.mark.parametrize("effort", ["medium", "low", "high"])
def test_explicit_effort_is_preserved_for_new_and_legacy_sessions(monkeypatch, runtime_type, effort):
    monkeypatch.setenv("AGENT_RUNTIME_TYPE", runtime_type)
    monkeypatch.setenv("AGENT_EFFORT", effort)
    settings = Settings(provider_settings="")
    assert settings.effort == settings.legacy_effort == effort


def test_constructor_effort_overrides_environment_and_is_not_downgraded(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_TYPE", "harness")
    monkeypatch.setenv("AGENT_EFFORT", "low")
    settings = Settings(provider_settings="", effort="medium")
    assert settings.effort == settings.legacy_effort == "medium"


def test_default_runtime_does_not_discover_paths_or_read_provider_files(monkeypatch):
    for key in PROVIDER_KEYS | {"AGENT_MODEL", "AGENT_PROVIDER_SETTINGS", "AGENT_RUNTIME_TYPE",
                                "AGENT_HARNESS_NODE_PATH", "AGENT_HARNESS_SOURCE_DIR"}:
        monkeypatch.delenv(key, raising=False)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Default configuration must not read files or discover an installation")

    for method in ("read_text", "read_bytes", "glob", "rglob", "iterdir"):
        monkeypatch.setattr(Path, method, forbidden)
    settings = Settings()
    assert settings.runtime_type == "harness"
    assert settings.model == DEFAULT_MODEL
    assert settings.provider_settings == ""
    assert settings.harness_node_path == settings.harness_source_dir == ""


@pytest.mark.parametrize("relative", [".env.example", "server/.env.example"])
def test_example_environments_use_harness_without_implicit_installation(relative):
    root = Path(__file__).resolve().parents[2]
    assignments = dict(line.strip().split("=", 1) for line in (root / relative).read_text().splitlines()
                       if "=" in line and not line.lstrip().startswith("#"))
    assert assignments["AGENT_RUNTIME_TYPE"] == "harness"
    assert assignments["AGENT_EFFORT"] == "high"
    assert not assignments.get("AGENT_HARNESS_NODE_PATH")
    assert not assignments.get("AGENT_HARNESS_SOURCE_DIR")


@pytest.mark.parametrize("runtime_type", ["unknown", "", "node /tmp/unreviewed.js", None])
def test_runtime_configuration_rejects_unknown_executors(runtime_type):
    with pytest.raises(ValueError, match="AGENT_RUNTIME_TYPE"):
        Settings(runtime_type=runtime_type, provider_settings="")


@pytest.mark.parametrize("value", [1, 131072])
def test_harness_output_limit_accepts_environment_boundaries(monkeypatch, value):
    monkeypatch.setenv("AGENT_HARNESS_MAX_OUTPUT_TOKENS", str(value))
    assert Settings(provider_settings="").harness_max_output_tokens == value


@pytest.mark.parametrize("value", [0, -1, 131073, True, 1.5, "8192", None])
def test_harness_output_limit_rejects_invalid_configuration(value):
    with pytest.raises(ValueError, match="AGENT_HARNESS_MAX_OUTPUT_TOKENS"):
        Settings(provider_settings="", harness_max_output_tokens=value)


@pytest.mark.parametrize("value", [1, 3600])
def test_harness_compaction_timeout_accepts_environment_boundaries(monkeypatch, value):
    monkeypatch.setenv("AGENT_HARNESS_COMPACTION_TIMEOUT_SECONDS", str(value))
    assert Settings(provider_settings="").harness_compaction_timeout_seconds == value


@pytest.mark.parametrize("value", [0, -1, 3601, True, 1.5, "600", None])
def test_harness_compaction_timeout_rejects_invalid_configuration(value):
    with pytest.raises(ValueError, match="AGENT_HARNESS_COMPACTION_TIMEOUT_SECONDS"):
        Settings(provider_settings="", harness_compaction_timeout_seconds=value)


def test_sqlite_durability_accepts_full_and_diagnostic_normal_only(monkeypatch):
    monkeypatch.delenv("AGENT_SQLITE_SYNCHRONOUS", raising=False)
    assert Settings(provider_settings="").sqlite_synchronous == "FULL"
    assert Settings(provider_settings="", sqlite_synchronous="normal").sqlite_synchronous == "NORMAL"
    with pytest.raises(ValueError, match="AGENT_SQLITE_SYNCHRONOUS"):
        Settings(provider_settings="", sqlite_synchronous="OFF")
