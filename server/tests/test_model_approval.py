import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


PASSWORD = "Correct-test-password-123"


class ApprovalRuntime:
    def __init__(self, service, conversation_id, config, approve, emit):
        self.service, self.conversation_id, self.approve, self.emit = service, conversation_id, approve, emit

    async def connect(self):
        return None

    async def execute(self, run, message):
        root = self.service.settings.data_dir / "workspaces" / self.conversation_id
        allowed = await self.approve("Read", {"file_path": str(root / "report.txt"), "operation": "read", "amount": 10, "content": "private prompt", "api_token": "secret"})
        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "允许" if allowed else "拒绝"})

    async def interrupt(self):
        return None

    async def close(self):
        return None


@pytest.fixture
def app_and_client(tmp_path):
    settings = Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",), approval_seconds=2)
    app = create_app(settings, runtime_factory=ApprovalRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        response = client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD})
        assert response.status_code == 200
        yield app, client


def _assistant_and_conversation(client):
    assistant = client.post("/api/v1/assistants", json={"name": "审批测试", "instructions": ""}).json()
    conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
    return assistant, conversation


def _wait(client, run_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = client.get(f"/api/v1/runs/{run_id}").json()
        if value["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return value
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def _enable_policy(client, assistant_id, **overrides):
    value = {
        "enabled": True,
        "reviewer_mode": "synthetic",
        "model_id": "synthetic-review-v1",
        "allowed_tools": ["Read"],
        "allowed_data_scopes": ["workspace"],
        "max_amount": 100,
        "allowed_operations": ["read"],
    }
    value.update(overrides)
    response = client.put(f"/api/v1/assistants/{assistant_id}/model-approval", json=value)
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_policy_and_injected_reviewer_are_audited(app_and_client):
    app, client = app_and_client
    assistant, conversation = _assistant_and_conversation(client)
    policy = _enable_policy(client, assistant["id"])
    assert policy["revision"] == 1 and policy["reviewer_mode"] == "synthetic"
    observed = []

    async def reviewer(request):
        observed.append(request)
        return {"decision": "allow", "reason": "合成评估通过", "confidence": 0.99, "uncertain": False, "result": {"fixture": True}}

    app.state.service.model_approval.reviewer = reviewer
    response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"client_request_id": "model-review-1", "text": "run"})
    assert response.status_code == 202
    run = _wait(client, response.json()["run_id"])
    assert run["status"] == "succeeded"
    assert observed and observed[0]["model_id"] == "synthetic-review-v1"
    assert "content" not in observed[0]["arguments"]
    assert "secret" not in str(observed[0]["arguments"])
    approval = app.state.service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["id"],))
    assert approval["status"] == "allowed"
    assert approval["strategy"] == "model"
    assert approval["decision_source"] == "model"
    assert approval["review_reason"] == "合成评估通过"
    assert approval["review_policy_revision"] == 1
    assert client.get(f"/api/v1/runs/{run['id']}/events").text.count("event: approval.model_decided") == 1
    projected = client.get(f"/api/v1/runs/{run['id']}").json()
    assert projected["approvals"] == []


def test_uncertain_or_extra_reviewer_result_is_denied(app_and_client):
    app, client = app_and_client
    assistant, conversation = _assistant_and_conversation(client)
    _enable_policy(client, assistant["id"])

    async def uncertain(_request):
        return {"decision": "allow", "reason": "不确定", "confidence": 0.4, "uncertain": True}

    app.state.service.model_approval.reviewer = uncertain
    response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"client_request_id": "model-review-uncertain", "text": "run"})
    run = _wait(client, response.json()["run_id"])
    approval = app.state.service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["id"],))
    assert approval["status"] == "denied" and approval["review_result"]
    assert "uncertain" in approval["review_result"]


def test_policy_denies_outside_scope_before_reviewer_and_fails_closed(app_and_client):
    app, client = app_and_client
    assistant, conversation = _assistant_and_conversation(client)
    _enable_policy(client, assistant["id"], allowed_operations=["edit"])
    called = False

    async def reviewer(_request):
        nonlocal called
        called = True
        return {"decision": "allow", "reason": "must never be used", "confidence": 0.99, "uncertain": False}

    app.state.service.model_approval.reviewer = reviewer
    response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"client_request_id": "model-review-2", "text": "run"})
    run = _wait(client, response.json()["run_id"])
    assert run["status"] == "succeeded" and called is False
    approval = app.state.service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["id"],))
    assert approval["status"] == "denied"
    assert approval["review_reason"] == "操作范围不在管理员代审白名单"
    assert approval["decision_source"] == "model"


def test_high_risk_and_reviewer_errors_are_denied(app_and_client):
    app, client = app_and_client
    assistant, conversation = _assistant_and_conversation(client)
    rejected = client.put(f"/api/v1/assistants/{assistant['id']}/model-approval", json={"enabled": True, "allowed_tools": ["mcp__cfixture__publish"], "allowed_data_scopes": ["workspace"], "allowed_operations": ["publish"]})
    assert rejected.status_code == 422
    _enable_policy(client, assistant["id"], allowed_tools=["Read"], allowed_data_scopes=["workspace"], allowed_operations=["read"])

    async def reviewer(_request):
        return {"decision": "allow", "reason": "must never override high risk", "confidence": 0.99, "uncertain": False}

    app.state.service.model_approval.reviewer = reviewer
    run_row = {"id": "run", "conversation_id": conversation["id"]}
    result = asyncio.run(app.state.service.model_approval.review(run_row, "mcp__cfixture__publish", {"operation": "publish", "data_scope": "workspace"}))
    # High risk is never model-decided; it falls through to human/admin
    # approval instead of producing a model denial.
    assert result is None

    async def broken(_request):
        raise RuntimeError("synthetic reviewer fault")

    app.state.service.model_approval.reviewer = broken
    result = asyncio.run(app.state.service.model_approval.review(run_row, "Read", {"operation": "read", "data_scope": "workspace", "file_path": "report.txt"}))
    assert result.decision == "deny" and result.result["status"] == "reviewer_error"


def test_model_policy_rejects_non_synthetic_model_ids(app_and_client):
    _app, client = app_and_client
    assistant, _conversation = _assistant_and_conversation(client)
    response = client.put(
        f"/api/v1/assistants/{assistant['id']}/model-approval",
        json={"enabled": True, "model_id": "paid-provider-model", "allowed_tools": ["Read"]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MODEL_REVIEW_MODEL_NOT_ALLOWED"


def test_model_approval_policy_is_admin_only(app_and_client):
    _app, client = app_and_client
    assistant, _conversation = _assistant_and_conversation(client)
    client.post("/api/v1/users", json={"username": "member", "display_name": "员工", "password": PASSWORD, "role": "member"})
    client.post("/api/v1/auth/logout")
    assert client.post("/api/v1/auth/login", json={"username": "member", "password": PASSWORD}).status_code == 200
    assert client.get(f"/api/v1/assistants/{assistant['id']}/model-approval").status_code == 403
    assert client.put(f"/api/v1/assistants/{assistant['id']}/model-approval", json={}).status_code == 403


def test_policy_change_after_reviewer_allow_is_fail_closed(app_and_client):
    app, client = app_and_client
    assistant, conversation = _assistant_and_conversation(client)
    _enable_policy(client, assistant["id"])
    admin = app.state.service.db.one("SELECT * FROM users WHERE username='admin'")

    async def reviewer(_request):
        # Simulate an administrator revoking the policy while the reviewer is
        # running. The runtime must not execute the stale allow decision.
        app.state.service.save_model_approval_policy(
            assistant["id"],
            {"enabled": False, "reviewer_mode": "synthetic", "model_id": "synthetic-review-v1"},
            dict(admin),
        )
        return {"decision": "allow", "reason": "stale", "confidence": 0.99, "uncertain": False}

    app.state.service.model_approval.reviewer = reviewer
    response = client.post(
        f"/api/v1/conversations/{conversation['id']}/messages",
        json={"client_request_id": "model-review-stale", "text": "run"},
    )
    run = _wait(client, response.json()["run_id"])
    approval = app.state.service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["id"],))
    assert run["status"] == "succeeded"
    assert approval["status"] == "denied"
    assert approval["review_result"] and "stale_authorization" in approval["review_result"]


def test_model_review_rejects_non_granting_authorize_tool_result(app_and_client, monkeypatch):
    app, client = app_and_client
    assistant, conversation = _assistant_and_conversation(client)
    _enable_policy(client, assistant["id"])
    run_row = {"id": "run", "conversation_id": conversation["id"]}

    async def reviewer(_request):
        return {"decision": "allow", "reason": "synthetic", "confidence": 1.0, "uncertain": False}

    app.state.service.model_approval.reviewer = reviewer
    monkeypatch.setattr(app.state.service, "authorize_tool", lambda *_args, **_kwargs: False)
    result = asyncio.run(app.state.service.model_approval.review(run_row, "Read", {"operation": "read", "data_scope": "workspace"}))
    assert result.decision == "deny"
    assert result.result["status"] == "authorization_denied"
