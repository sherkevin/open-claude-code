import json

from fastapi.testclient import TestClient

from app.main import create_app
from test_api import FakeRuntime, poll, send
from test_model_catalog import catalog_settings


def make_client(tmp_path):
    return TestClient(create_app(catalog_settings(tmp_path), runtime_factory=FakeRuntime))


def setup(client):
    service = client.app.state.service
    service.create_user({"username": "identity", "display_name": "Identity", "password": "identity-password-123", "role": "admin"})
    assert client.post("/api/v1/auth/login", json={"username": "identity", "password": "identity-password-123"}).status_code == 200
    assistant = client.post("/api/v1/assistants", json={"name": "Identity", "connections": [], "knowledge_base_ids": []}).json()
    conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
    return service, conversation["id"]


def test_selected_identity_is_metadata_and_keeps_native_history(tmp_path):
    with make_client(tmp_path) as client:
        service, cid = setup(client)
        # Existing native state and old identity in application history must not
        # control the public model name of a newly prepared metadata request.
        service.db.execute("UPDATE conversations SET sdk_session_id='native-business-history',sdk_cost_total=1.25 WHERE id=?", (cid,))
        before = FakeRuntime.connects
        for index, (model, label) in enumerate([("dano-v2.1", "Dano-v2.1"), ("deepseek-v4.1-flash", "deepseek-v4.1-flash"), ("dano-v2.1", "Dano-v2.1")]):
            assert client.patch(f"/api/v1/conversations/{cid}", json={"model_id": model}).status_code == 200
            sent = send(client, cid, "你是谁？", f"identity-{index}")
            result = poll(client, sent["run_id"])
            assert result["status"] == "succeeded"
            assert result["execution_kind"] == "product_metadata"
            assert result["identity_source"] == "run_selection"
            assert result["model_call_count"] == 0
            assert result["model_id"] == model
            assert result["usage"]["input_tokens"] == result["usage"]["output_tokens"] == 0
            assert result["usage"]["estimated_cost_usd"] == 0
            assert result["usage"]["cost_is_estimate"] is False
            messages = client.get(f"/api/v1/conversations/{cid}/messages").json()
            answer = next(m["text"] for m in messages if m.get("id") == result["assistant_message_id"])
            assert label in answer
            if model != "dano-v2.1":
                assert "Dano" not in answer
            assert "private-" not in answer and "1017" not in answer
            replay = send(client, cid, "你是谁？", f"identity-{index}")
            assert replay["run_id"] == sent["run_id"]
        assert FakeRuntime.connects == before
        assert not service.db.all("SELECT * FROM raw_events")
        row = service.require("conversations", cid)
        assert row["sdk_session_id"] == "native-business-history" and row["sdk_cost_total"] == 1.25
        assert row["active_run_id"] is None
        assert len(client.get(f"/api/v1/conversations/{cid}/messages").json()) == 6


def test_business_and_quoted_identity_are_real_runtime_turns(tmp_path):
    with make_client(tmp_path) as client:
        service, cid = setup(client)
        before = FakeRuntime.connects
        texts = ['你是谁？请同时计算17+25。', '请翻译“你是谁”这句话。', '告诉我你是谁的供应商。']
        for index, text in enumerate(texts):
            sent = send(client, cid, text, f"business-{index}")
            result = poll(client, sent["run_id"])
            assert result["status"] == "succeeded"
            assert result["execution_kind"] == "model" and result["model_call_count"] is None
            answer = service.require("messages", result["assistant_message_id"])["text"]
            assert answer == "回复：" + text
            assert result["usage"]["input_tokens"] == 12
            assert not json.loads(service.require("runs", sent["run_id"])["execution_meta"]).get("identity_source")
        assert FakeRuntime.connects == before + 1
