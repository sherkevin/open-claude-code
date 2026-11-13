import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.model_catalog import ModelCatalog
from app.provider_policy import classify_provider_url
from app.security import AppError
from app.service import Service


@pytest.mark.parametrize(
    ("url", "kind", "production_ready"),
    [
        ("https://tokenhub.tencentmaas.com", "tokenhub", True),
        ("https://tokenhub.tencentmaas.com/v1", "tokenhub", True),
        ("http://127.0.0.1:3425/v1", "local_proxy", False),
        ("http://provider.test/v1", "synthetic", False),
        ("https://api.deepseek.com/anthropic", "rejected", False),
        ("https://attacker.example/v1", "synthetic", False),
        ("https://tokenhub.tencentmaas.com/v1?token=hidden", "rejected", False),
    ],
)
def test_provider_route_classification_is_fail_closed(url, kind, production_ready):
    route = classify_provider_url(url)
    assert route.kind == kind
    assert route.production_ready is production_ready


def test_credentialed_non_tokenhub_provider_is_rejected_before_run(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "default_model_id": "model",
                "models": [
                    {
                        "id": "model",
                        "label": "Model",
                        "upstream_model": "model-upstream",
                        "reasoning_levels": {"default": None},
                        "provider_env": {
                            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
                            "ANTHROPIC_API_KEY": "test-only-key",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = Service(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), provider_settings="", provider_test_mode=False))
    try:
        selection = service.pin_provider(service.catalog.select("model"))
        with pytest.raises(AppError) as failure:
            service.validated_provider(selection)
        assert failure.value.code == "TOKENHUB_ROUTE_REQUIRED"
    finally:
        service.db.close()


def test_external_catalog_route_is_not_publicly_available_without_credentials(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "default_model_id": "model",
                "models": [
                    {
                        "id": "model",
                        "label": "Model",
                        "upstream_model": "model-upstream",
                        "reasoning_levels": {"default": None},
                        "provider_env": {"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), provider_settings="", provider_test_mode=False)
    entry = ModelCatalog(settings).public()[0]
    assert entry["available"] is False
    assert entry["unavailable_reason"] == "模型服务线路未满足安全部署策略"


def test_health_readiness_never_reports_synthetic_route_as_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://provider.test/v1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-key")
    app = create_app(Settings(data_dir=tmp_path / "data", provider_settings="", provider_test_mode=True))
    with TestClient(app) as client:
        value = client.get("/api/v1/health/ready")
    assert value.status_code == 200
    body = value.json()
    assert body["model_route_ready"] is False
    assert "provider_route" not in body
    assert "tokenhub_route" not in body
    assert body["model_configured"] is False
