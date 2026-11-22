import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.model_catalog import ModelCatalog
from app.model_recommendation import _tab_target, provider_completion, provider_router, router_preflight
from app.main import create_app
from test_api import FakeRuntime, PASSWORD, login


def _router_service(tmp_path):
    """Build the smallest provider-router harness without starting a server."""
    catalog_path = tmp_path / "models.json"
    catalog_path.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash-1007", "models": [
        {"id": "deepseek-v4.1-flash-1007", "label": "DeepSeek", "upstream_model": "deepseek-v4.1-flash-1007", "reasoning_levels": {"default": None}},
        {"id": "vision", "label": "Vision", "upstream_model": "vision", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider_path = tmp_path / "provider.json"
    provider_path.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "test-only-token", "ANTHROPIC_BASE_URL": "http://provider.test/v1"}}), encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data", model_catalog=str(catalog_path), provider_settings=str(provider_path), model_router_enabled=True, model_router_model_id="deepseek-v4.1-flash-1007", tab_completion_model_id="deepseek-v4.1-flash-1007")
    catalog = ModelCatalog(settings)
    service = SimpleNamespace(settings=settings, catalog=catalog)
    service.effective_provider = lambda selection: settings.provider_env()
    return service


def test_bounded_recommendation_is_advisory_and_filters_unavailable(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "hy4", "models": [
        {"id": "deepseek-v4.1-flash", "label": "deepseek-v4.1-flash", "upstream_model": "deepseek-v4.1-flash", "capabilities": ["general", "reasoning"], "reasoning_levels": {"default": None}},
        {"id": "hy4", "label": "混元 4", "upstream_model": "hy4", "capabilities": ["multimodal", "chinese"], "reasoning_levels": {"default": None}},
        {"id": "kimi-k3.1-pro", "label": "Kimi 3.1 PRO", "upstream_model": "", "enabled": False, "capabilities": ["long_context"], "reasoning_levels": {"default": None}, "unavailable_reason": "管理员尚未配置此模型"},
    ]}), encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), model="legacy")
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        models = client.get("/api/v1/models").json()
        assert next(item for item in models if item["id"] == "hy4")["label"] == "混元 4"
        kimi = next(item for item in models if item["id"] == "kimi-k3.1-pro")
        assert kimi["available"] is False and kimi["unavailable_reason"] == "管理员尚未配置此模型"
        response = client.post("/api/v1/models/recommend", json={
            "task_summary": "请分析一张中文图片并说明质量问题。密码=should-not-leave",
            "candidate_model_ids": ["hy4", "kimi-k3.1-pro"],
        })
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["model_id"] == "hy4" and value["applied"] is False
        assert value["router"]["model_id"] == "deepseek-v4.1-flash-1007"
        assert value["router"]["mode"] == "bounded_local"
        assert value["router"]["input_chars"] <= 2000
        evidence = value["router"]["evidence"]
        assert evidence["schema_version"] == "model-route-evidence/v1"
        assert evidence["policy_version"] == "bounded-router-v1"
        assert evidence["selected_model_id"] == "hy4"
        assert evidence["candidate_model_ids"] == ["hy4"]
        assert len(evidence["input_digest"]) == 64
        assert "密码" not in json.dumps(evidence, ensure_ascii=False)
        assert client.app.state.service.db.all("SELECT * FROM conversations") == []


def test_recommendation_requires_short_summary(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash", "models": [
        {"id": "deepseek-v4.1-flash", "label": "deepseek-v4.1-flash", "upstream_model": "deepseek-v4.1-flash", "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",)))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        assert client.post("/api/v1/models/recommend", json={"task_summary": ""}).status_code == 422


def test_tab_completion_never_uses_hardcoded_text_when_exact_router_target_is_missing(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash", "models": [
        {"id": "deepseek-v4.1-flash", "label": "DeepSeek", "upstream_model": "deepseek", "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), model_router_enabled=True, model_router_model_id="deepseek-v4.1-flash-1007", tab_completion_model_id="deepseek-v4.1-flash-1007", provider_settings=""))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        response = client.post("/api/v1/models/complete", json={"text": "分析质量报告"})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["suggestion"] is None
        assert value["router"]["target_model_id"] == "deepseek-v4.1-flash-1007"
        assert value["router"]["target_status"] == "unavailable"
        assert value["router"]["evidence"]["target_status"] == "unavailable"
        assert value["router"]["evidence"]["decision_mode"] == "unavailable"


def test_task_submit_auto_routes_without_provider_call_and_allows_manual_override(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash", "models": [
        {"id": "deepseek-v4.1-flash", "label": "DeepSeek", "upstream_model": "deepseek", "capabilities": ["reasoning"], "reasoning_levels": {"default": None}},
        {"id": "vision", "label": "Vision", "upstream_model": "vision", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), provider_settings=""), runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        assistant = service.save_assistant({"name": "QA", "instructions": "", "connections": []}, user=user)
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "新对话", "auto_model_routing": True}, user)
        result, created = service.prepare_run(conversation["id"], {"client_request_id": "request-auto-1", "text": "请分析一张图片", "file_ids": [], "skill_ids": []}, user)
        assert created and result["run_id"]
        metadata = json.loads(service.require("runs", result["run_id"])["execution_meta"])
        assert metadata["model_routing"]["enabled"] is True
        assert metadata["model_routing"]["mode"] == "bounded_local"
        assert metadata["model_routing"]["model_id"] == "vision"
        assert metadata["model_routing"]["reason"]
        assert service.require("conversations", conversation["id"])["model_id"] == "vision"

        # A one-shot model_id is explicit and bypasses automatic routing.
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "手动", "auto_model_routing": True}, user)
        result, _ = service.prepare_run(conversation["id"], {"client_request_id": "request-manual-1", "text": "请分析一张图片", "file_ids": [], "skill_ids": [], "model_id": "deepseek-v4.1-flash"}, user)
        metadata = json.loads(service.require("runs", result["run_id"])["execution_meta"])
        assert metadata["model_routing"]["mode"] == "manual"
        assert metadata["selection"]["model_id"] == "deepseek-v4.1-flash"
        # A request-level override is intentionally ephemeral; durable model
        # changes still use PATCH /conversations/{id}.
        assert service.require("conversations", conversation["id"])["model_id"] == "deepseek-v4.1-flash"


def test_message_submit_uses_provider_router_result_and_records_route_mode(tmp_path, monkeypatch):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash", "models": [
        {"id": "deepseek-v4.1-flash", "label": "DeepSeek", "upstream_model": "deepseek", "capabilities": ["reasoning"], "reasoning_levels": {"default": None}},
        {"id": "vision", "label": "Vision", "upstream_model": "vision", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), provider_settings=""), runtime_factory=FakeRuntime)

    async def fake_router(service, task_summary, candidate_model_ids=None, *, user_id=None):
        assert user_id is not None
        return {"model_id": "vision", "model_label": "Vision", "reasoning_level": "default", "reason": "provider test", "applied": False,
                "router": {"model_id": "deepseek-v4.1-flash", "mode": "provider", "input_chars": len(task_summary), "candidate_count": 2, "max_output_chars": 240, "estimated_cost_usd": None}}

    monkeypatch.setattr("app.model_recommendation.provider_router", fake_router)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "QA", "instructions": "", "connections": []}, user=user)
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "路由", "auto_model_routing": True}, user)
        service.manager.submit = lambda run_id: None
        response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"client_request_id": "provider-route-1", "text": "分析图片", "auto_route": True})
        assert response.status_code == 202, response.text
        metadata = json.loads(service.require("runs", response.json()["run_id"])["execution_meta"])
        assert metadata["model_routing"]["mode"] == "provider"
        assert metadata["model_routing"]["router"]["model_id"] == "deepseek-v4.1-flash"
        assert service.require("conversations", conversation["id"])["model_id"] == "vision"


def test_completion_reports_missing_provider_authorization_without_calling_other_model(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash-1007", "models": [
        {"id": "deepseek-v4.1-flash-1007", "label": "DeepSeek V4.1 Flash 1007", "upstream_model": "deepseek-v4.1-flash-1007", "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(
        data_dir=tmp_path / "data",
        model_catalog=str(catalog),
        origins=("http://testserver",),
        model_router_enabled=True,
        model_router_model_id="deepseek-v4.1-flash-1007",
        tab_completion_model_id="deepseek-v4.1-flash-1007",
        provider_settings="",
    ))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        response = client.post("/api/v1/models/complete", json={"text": "分析质量报告"})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["suggestion"] is None
        assert value["router"]["target_model_id"] == "deepseek-v4.1-flash-1007"
        assert value["router"]["target_status"] == "available"
        assert value["router"]["provider_configured"] is False
        assert value["router"]["provider_status"] == "credentials_missing"
        assert value["router"]["provider_attempted"] is False
        assert value["router"]["mode"] == "provider_unconfigured"


def test_provider_router_and_tab_completion_use_only_authorized_target_with_local_fake_gateway(tmp_path, monkeypatch):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "vision", "models": [
        {"id": "deepseek-v4.1-flash-1007", "label": "DeepSeek V4.1 Flash 1007", "upstream_model": "deepseek-v4.1-flash-1007", "reasoning_levels": {"default": None}},
        {"id": "vision", "label": "视觉模型", "upstream_model": "vision-upstream", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "test-only-token", "ANTHROPIC_BASE_URL": "http://tokenhub.test/v1"}}), encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        model_catalog=str(catalog),
        origins=("http://testserver",),
        model_router_enabled=True,
        model_router_model_id="deepseek-v4.1-flash-1007",
        tab_completion_model_id="deepseek-v4.1-flash-1007",
        provider_settings=str(provider),
    )
    app = create_app(settings)
    calls = []

    class FakeResponse:
        status_code = 200
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, json):
            calls.append({"endpoint": endpoint, "headers": self.kwargs["headers"], "payload": json})
            if '"completion"' in json["messages"][0]["content"]:
                body = {"content": [{"text": '{"completion":"分析质量报告并列出风险"}'}]}
            else:
                body = {"content": [{"text": '{"model_id":"vision","reason":"需要图片理解"}'}]}
            return FakeResponse(body)

    monkeypatch.setattr("app.model_recommendation.httpx.AsyncClient", FakeAsyncClient)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        recommendation = client.post("/api/v1/models/recommend", json={"task_summary": "分析图片质量"})
        assert recommendation.status_code == 200, recommendation.text
        route = recommendation.json()
        assert route["model_id"] == "vision"
        assert route["router"]["mode"] == "authorized"
        assert "provider_route" not in route["router"]
        assert "tokenhub_route" not in route["router"]
        assert route["router"]["target_model_id"] == "deepseek-v4.1-flash-1007"
        assert route["router"]["provider_configured"] is True
        assert route["router"]["provider_attempted"] is True
        completion = client.post("/api/v1/models/complete", json={"text": "分析质量"})
        assert completion.status_code == 200, completion.text
        value = completion.json()
        assert value["suggestion"] == "分析质量报告并列出风险"
        assert value["router"]["mode"] == "authorized"
        assert "provider_route" not in value["router"]
        assert "tokenhub_route" not in value["router"]
        assert value["router"]["target_model_id"] == "deepseek-v4.1-flash-1007"
        assert value["router"]["provider_attempted"] is True
    assert len(calls) == 2
    assert all(call["payload"]["model"] == "deepseek-v4.1-flash-1007" for call in calls)
    assert all("test-only-token" not in str(call["payload"]) for call in calls)
    assert calls[0]["endpoint"] == "http://tokenhub.test/v1/messages"
    assert "thinking" not in calls[0]["payload"]
    assert calls[1]["payload"]["thinking"] == {"type": "disabled"}


def test_tab_completion_uses_independent_default_1017_target(tmp_path, monkeypatch):
    """Fitness remains pinned to 1007 while Tab uses the authorized 1017 route."""
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash-1007", "models": [
        {"id": "deepseek-v4.1-flash-1007", "label": "Fitness 1007", "upstream_model": "deepseek-v4.1-flash-1007", "reasoning_levels": {"default": None}},
        {"id": "deepseek-v4.1-flash-1017", "label": "Tab 1017", "upstream_model": "deepseek-v4.1-flash-1017", "reasoning_levels": {"default": None}},
        {"id": "vision", "label": "视觉模型", "upstream_model": "vision-upstream", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "test-only-token", "ANTHROPIC_BASE_URL": "http://tokenhub.test/v1"}}), encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / "data",
        model_catalog=str(catalog),
        origins=("http://testserver",),
        model_router_enabled=True,
        model_router_model_id="deepseek-v4.1-flash-1007",
        provider_settings=str(provider),
    )
    app = create_app(settings)
    calls = []

    class FakeResponse:
        status_code = 200
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, json):
            calls.append(json)
            if '"completion"' in json["messages"][0]["content"]:
                return FakeResponse({"content": [{"text": '{"completion":"分析质量并列出风险"}'}]})
            return FakeResponse({"content": [{"text": '{"model_id":"vision","reason":"需要图片理解"}'}]})

    monkeypatch.setattr("app.model_recommendation.httpx.AsyncClient", FakeAsyncClient)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        recommendation = client.post("/api/v1/models/recommend", json={"task_summary": "分析图片质量"})
        assert recommendation.status_code == 200, recommendation.text
        assert recommendation.json()["router"]["target_model_id"] == "deepseek-v4.1-flash-1007"
        assert "provider_route" not in recommendation.json()["router"]
        assert "tokenhub_route" not in recommendation.json()["router"]
        completion = client.post("/api/v1/models/complete", json={"text": "分析质量"})
        assert completion.status_code == 200, completion.text
        value = completion.json()
        assert value["suggestion"] == "分析质量并列出风险"
        assert value["router"]["target_model_id"] == "deepseek-v4.1-flash-1017"
        assert value["router"]["mode"] == "authorized"
        assert "provider_route" not in value["router"]
        assert "tokenhub_route" not in value["router"]
    assert [call["model"] for call in calls] == ["deepseek-v4.1-flash-1007", "deepseek-v4.1-flash-1017"]


@pytest.mark.parametrize("upstream", ["deepseek-v4.1-flash-1017", "tencent-cloud/deepseek-v4.1-flash-1017", "tencent-cloud/deepseek-v4.1-flash-1017[1m]"])
def test_tab_completion_accepts_explicit_branded_catalog_alias_for_1017(tmp_path, monkeypatch, upstream):
    """A Dano public entry may pin the approved 1017 upstream route."""
    catalog_path = tmp_path / "models.json"
    catalog_path.write_text(json.dumps({"default_model_id": "dano-v2.1", "models": [
        {"id": "dano-v2.1", "label": "Dano-v2.1", "upstream_model": upstream, "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider_path = tmp_path / "provider.json"
    provider_path.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "test-only-token", "ANTHROPIC_BASE_URL": "http://tokenhub.test/v1"}}), encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data", model_catalog=str(catalog_path), provider_settings=str(provider_path), model_router_enabled=True)
    service = SimpleNamespace(settings=settings, catalog=ModelCatalog(settings))
    service.effective_provider = lambda selection: settings.provider_env()
    calls = []

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None

        def json(self):
            return {"content": [{"text": '{"completion":"分析质量并列出风险"}'}]}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, json):
            calls.append(json)
            return FakeResponse()

    monkeypatch.setattr("app.model_recommendation.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(provider_completion(service, "分析质量"))
    assert result["suggestion"] == "分析质量并列出风险"
    assert result["router"]["target_model_id"] == "deepseek-v4.1-flash-1017"
    assert result["router"]["target_status"] == "available"
    assert calls[0]["model"] == upstream
    assert calls[0]["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("upstream", [
    "deepseek-v4.1-flash-10170", "deepseek-v4.1-flash-1017-other",
    "other-deepseek-v4.1-flash-1017", "deepseek-v4.1-flash-1017/other",
])
def test_tab_alias_rejects_similar_unapproved_routes(tmp_path, upstream):
    service = _router_service(tmp_path)
    service.settings.tab_completion_model_id = "deepseek-v4.1-flash-1017"
    service.catalog.models["vision"]["upstream_model"] = upstream
    assert _tab_target(service)[1] is None


def test_tab_disabled_exact_target_cannot_be_bypassed_by_alias(tmp_path):
    service = _router_service(tmp_path)
    service.catalog.models["deepseek-v4.1-flash-1007"]["enabled"] = False
    service.catalog.models["vision"]["upstream_model"] = "deepseek-v4.1-flash-1007"
    assert _tab_target(service)[1] is None


@pytest.mark.parametrize("draft,completion,expected", [
    ("  分析质量\n  按车间\t汇总  ", "分析质量 按车间 汇总并列出来源", "  分析质量\n  按车间\t汇总  并列出来源"),
    ("分析质量", "分析质量", None),
    ("分析质量", "分析质量 token=generated-secret", None),
])
def test_tab_preserves_original_draft_and_rejects_empty_or_credential_suffix(tmp_path, monkeypatch, draft, completion, expected):
    service = _router_service(tmp_path)

    async def fake_post(*args, **kwargs):
        import httpx
        return httpx.Response(200, request=httpx.Request("POST", "http://provider.test/v1/messages"),
                              json={"content": [{"text": json.dumps({"completion": completion})}]})

    monkeypatch.setattr("app.model_recommendation.metered_post", fake_post)
    value = asyncio.run(provider_completion(service, draft))
    assert value["suggestion"] == expected


def test_tab_overlong_draft_is_not_truncated_or_sent_to_provider(tmp_path, monkeypatch):
    service = _router_service(tmp_path)

    async def unexpected_post(*args, **kwargs):
        pytest.fail("overlong draft must not spend a provider request")

    monkeypatch.setattr("app.model_recommendation.metered_post", unexpected_post)
    value = asyncio.run(provider_completion(service, "质" * 501))
    assert value["suggestion"] is None
    assert value["router"]["mode"] == "input_too_long"
    assert value["router"]["provider_attempted"] is False


def test_router_status_is_fail_closed_when_exact_target_is_missing(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash", "models": [
        {"id": "deepseek-v4.1-flash", "label": "DeepSeek", "upstream_model": "deepseek", "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), model_router_enabled=True, tab_completion_model_id="deepseek-v4.1-flash-1007"))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        response = client.get("/api/v1/models/router-status")
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["schema_version"] == "model-router-preflight/v1"
        assert value["target_model_id"] == "deepseek-v4.1-flash-1007"
        assert value["target_status"] == "unavailable"
        assert value["provider_status"] == "target_unavailable"
        assert value["ready"] is False
        assert value["tab_completion_mode"] == "unavailable"
        assert value["provider_attempted"] is False
        assert "deepseek" in json.dumps(value, ensure_ascii=False)


def test_router_status_distinguishes_disabled_from_missing_provider(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash-1007", "models": [
        {"id": "deepseek-v4.1-flash-1007", "label": "DeepSeek V4.1 Flash 1007", "upstream_model": "deepseek-v4.1-flash-1007", "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), model_router_enabled=False, tab_completion_enabled=False, tab_completion_model_id="deepseek-v4.1-flash-1007", provider_settings=""))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        value = client.get("/api/v1/models/router-status").json()
        assert value["target_status"] == "available"
        assert value["provider_status"] == "credentials_missing"
        assert value["enabled"] is False and value["budget_enabled"] is True
        assert value["ready"] is False
        assert value["tab_completion_mode"] == "disabled"


def test_router_status_reports_ready_without_calling_provider(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash-1007", "models": [
        {"id": "deepseek-v4.1-flash-1007", "label": "DeepSeek V4.1 Flash 1007", "upstream_model": "deepseek-v4.1-flash-1007", "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "test-only-token", "ANTHROPIC_BASE_URL": "http://provider.test/v1"}}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), model_router_enabled=True, tab_completion_enabled=True, tab_completion_model_id="deepseek-v4.1-flash-1007", provider_settings=str(provider)))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        value = client.get("/api/v1/models/router-status").json()
        assert value["target_status"] == "available"
        assert value["provider_status"] == "credentials_present"
        assert value["ready"] is True
        assert value["tab_completion_mode"] == "ready"
        assert value["provider_attempted"] is False
        assert "test-only-token" not in json.dumps(value)


def test_provider_router_rejects_non_object_gateway_envelope_without_500(tmp_path, monkeypatch):
    service = _router_service(tmp_path)

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, json):
            return FakeResponse()

    monkeypatch.setattr("app.model_recommendation.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(provider_router(service, "分析设备质量", ["vision"]))
    assert result["model_id"] == "vision"
    assert result["router"]["mode"] == "bounded_local"
    assert result["router"]["provider_attempted"] is True
    assert result["router"]["fallback_reason"] == "provider_response_rejected"
    assert result["router"]["evidence"]["fallback_reason"] == "provider_response_rejected"


def test_provider_completion_rejects_non_object_gateway_envelope_without_500(tmp_path, monkeypatch):
    service = _router_service(tmp_path)

    class FakeResponse:
        status_code = 200
        def raise_for_status(self):
            return None

        def json(self):
            return None

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, endpoint, json):
            return FakeResponse()

    monkeypatch.setattr("app.model_recommendation.httpx.AsyncClient", FakeAsyncClient)
    result = asyncio.run(provider_completion(service, "分析质量"))
    assert result["suggestion"] is None
    assert result["router"]["mode"] == "provider_response_rejected"
    assert result["router"]["rejection_reason"] == "provider_response_invalid"
    assert result["router"]["evidence"]["fallback_reason"] == "provider_response_invalid"


def test_provider_router_never_constructs_request_for_non_tokenhub_route(tmp_path, monkeypatch):
    service = _router_service(tmp_path)
    service.effective_provider = lambda _selection: {
        "ANTHROPIC_API_KEY": "test-only-token",
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    }

    class NeverClient:
        def __init__(self, **_kwargs):
            pytest.fail("non-TokenHub provider must be rejected before HTTP client creation")

    monkeypatch.setattr("app.model_recommendation.httpx.AsyncClient", NeverClient)
    result = asyncio.run(provider_router(service, "分析设备质量", ["vision"]))
    assert result["router"]["provider_attempted"] is False
    assert result["router"]["provider_status"] == "non_tokenhub_route"
    assert result["router"]["fallback_reason"] == "non_tokenhub_route"


def test_router_preflight_requires_tokenhub_in_production_mode(tmp_path):
    service = _router_service(tmp_path)
    service.settings.provider_test_mode = False
    value = router_preflight(service)
    assert value["provider_route"] == "synthetic"
    assert value["tokenhub_route"] is False
    assert value["ready"] is False
    assert value["tab_completion_ready"] is False


def test_router_status_api_is_brand_neutral_for_authenticated_user(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "fitness", "models": [
        {"id": "fitness", "label": "Fitness model", "upstream_model": "private-upstream-deployment", "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"env": {
        "ANTHROPIC_AUTH_TOKEN": "test-only-token",
        "ANTHROPIC_BASE_URL": "https://tokenhub.tencentmaas.com/v1",
    }}), encoding="utf-8")
    app = create_app(Settings(
        data_dir=tmp_path / "data",
        model_catalog=str(catalog),
        provider_settings=str(provider),
        origins=("http://testserver",),
        model_router_enabled=True,
        model_router_model_id="fitness",
        tab_completion_model_id="fitness",
    ))
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "Admin", "password": PASSWORD, "role": "admin"})
        login(client)
        response = client.get("/api/v1/models/router-status")
        assert response.status_code == 200, response.text
        value = response.json()
        encoded = json.dumps(value, ensure_ascii=False).lower()
        assert "tokenhub" not in encoded
        assert "private-upstream-deployment" not in encoded
        assert "provider_route" not in value
        assert "tokenhub_route" not in value
        assert "tab_provider_route" not in value
        assert "tab_tokenhub_route" not in value
        assert value["ready"] is True
        assert value["recommendation_mode"] == "authorized"


def test_provider_completion_never_constructs_request_for_non_tokenhub_route(tmp_path, monkeypatch):
    service = _router_service(tmp_path)
    service.effective_provider = lambda _selection: {
        "ANTHROPIC_AUTH_TOKEN": "test-only-token",
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    }

    class NeverClient:
        def __init__(self, **_kwargs):
            pytest.fail("non-TokenHub provider must be rejected before HTTP client creation")

    monkeypatch.setattr("app.model_recommendation.httpx.AsyncClient", NeverClient)
    result = asyncio.run(provider_completion(service, "分析质量"))
    assert result["suggestion"] is None
    assert result["router"]["provider_attempted"] is False
    assert result["router"]["mode"] == "provider_invalid"
    assert result["router"]["rejection_reason"] == "non_tokenhub_route"

