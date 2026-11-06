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

