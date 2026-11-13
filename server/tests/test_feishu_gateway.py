import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.test_api import FakeRuntime, PASSWORD


def _poll(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = client.get(f"/api/v1/runs/{run_id}").json()
        if value["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return value
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_feishu_gateway_maps_chat_and_is_idempotent(tmp_path):
    settings = Settings(
        data_dir=tmp_path,
        provider_settings="",
        origins=("http://testserver",),
        feishu_bridge_token="test-bridge-token",
        feishu_username="admin",
        feishu_allowed_open_ids=("ou-owner",),
    )
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        user = app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
        assistant = client.post("/api/v1/assistants", json={"name": "飞书助手", "instructions": "回答测试任务"}).json()
        settings.feishu_assistant_id = assistant["id"]
        headers = {"X-Agent-Feishu-Bridge-Token": "test-bridge-token"}
        denied = client.post("/internal/feishu/messages", headers=headers, json={"event_id": "evt-denied", "message_id": "m-denied", "chat_id": "oc-1", "chat_type": "p2p", "open_id": "ou-other", "text": "hello"})
        assert denied.status_code == 403
        payload = {"event_id": "evt-1", "message_id": "m-1", "chat_id": "oc-1", "chat_type": "p2p", "open_id": "ou-owner", "text": "hello"}
        response = client.post("/internal/feishu/messages", headers=headers, json=payload)
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        assert _poll(client, run_id)["status"] == "succeeded"
        duplicate = client.post("/internal/feishu/messages", headers=headers, json=payload)
        assert duplicate.status_code == 202 and duplicate.json()["duplicate"] is True
        assert duplicate.json()["run_id"] == run_id
        streamed = client.get(f"/internal/feishu/runs/{run_id}/events", headers=headers).json()
        assert streamed["assistant_message"]["text"] == "回复：hello"
        assert any(item["type"] == "run.completed" for item in streamed["events"])
        binding = app.state.service.db.one("SELECT * FROM feishu_chat_bindings WHERE chat_id=?", ("oc-1",))
        assert binding and binding["user_id"] == user["id"]


def test_feishu_gateway_requires_token_and_rejects_cross_chat_binding(tmp_path):
    settings = Settings(data_dir=tmp_path, provider_settings="", feishu_bridge_token="secret", feishu_username="admin", feishu_allowed_open_ids=("ou-owner",))
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        app.state.service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
        assistant = client.post("/api/v1/assistants", json={"name": "助手"}).json()
        settings.feishu_assistant_id = assistant["id"]
        payload = {"event_id": "evt-1", "message_id": "m-1", "chat_id": "oc-1", "chat_type": "p2p", "open_id": "ou-owner", "text": "hello"}
        assert client.post("/internal/feishu/messages", json=payload).status_code == 401
        headers = {"X-Agent-Feishu-Bridge-Token": "secret"}
        assert client.post("/internal/feishu/messages", headers=headers, json=payload).status_code == 202
        conflict = {**payload, "event_id": "evt-2", "message_id": "m-2", "open_id": "ou-other"}
        assert client.post("/internal/feishu/messages", headers=headers, json=conflict).status_code == 403
