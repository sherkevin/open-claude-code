import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from test_api import PASSWORD, login


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
        assert value["router"]["model_id"] == "deepseek-v4.1-flash"
        assert value["router"]["mode"] == "bounded_local"
        assert value["router"]["input_chars"] <= 2000
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


def test_task_submit_auto_routes_without_provider_call_and_allows_manual_override(tmp_path):
    catalog = tmp_path / "models.json"
    catalog.write_text(json.dumps({"default_model_id": "deepseek-v4.1-flash", "models": [
        {"id": "deepseek-v4.1-flash", "label": "DeepSeek", "upstream_model": "deepseek", "capabilities": ["reasoning"], "reasoning_levels": {"default": None}},
        {"id": "vision", "label": "Vision", "upstream_model": "vision", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}},
    ]}), encoding="utf-8")
    app = create_app(Settings(data_dir=tmp_path / "data", model_catalog=str(catalog), origins=("http://testserver",), provider_settings=""))
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

