import time

import pytest

from test_api import PASSWORD, configured, login, poll, send
from app.main import create_app
from app.security import AppError
from app.config import Settings
from fastapi.testclient import TestClient
from test_api import FakeRuntime


class DualRuntime(FakeRuntime):
    async def execute(self, run, message):
        allowed = await self.approve("mcp__danger__delete", {"operation": "delete", "data_scope": "workspace", "secret": "do-not-show"})
        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "已执行" if allowed else "已拒绝"})


def setup_app(tmp_path):
    # Keep the approval window above the full-suite host scheduling jitter;
    # expiry behavior is covered explicitly through manager recovery below.
    settings = Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",), mcp_hosts=("localhost", "127.0.0.1"), idle_seconds=60, approval_seconds=30)
    app = create_app(settings, runtime_factory=DualRuntime)
    client = TestClient(app)
    client.__enter__()
    service = app.state.service
    admin1 = service.create_user({"username": "admin", "display_name": "管理员一", "password": PASSWORD, "role": "admin"})
    admin2 = service.create_user({"username": "admin2", "display_name": "管理员二", "password": PASSWORD, "role": "admin"})
    member = service.create_user({"username": "member", "display_name": "员工", "password": PASSWORD, "role": "member"})
    login(client, "admin")
    assistant = client.post("/api/v1/assistants", json={"name": "High risk", "scope": "team", "connections": []}).json()
    policy = client.put(f"/api/v1/assistants/{assistant['id']}/high-risk-approval", json={"enabled": True, "allowed_tools": ["mcp__danger__delete"], "admin_allowlist": [admin1["id"], admin2["id"]]}).json()
    assert policy["required_approvals"] == 2
    client.post("/api/v1/auth/logout")
    login(client, "member")
    conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
    return client, service, admin1, admin2, member, conversation


def test_high_risk_requires_two_distinct_admin_votes(tmp_path):
    client, service, admin1, admin2, member, conversation = setup_app(tmp_path)
    try:
        run = send(client, conversation["id"], "delete", "high-risk-request")
        waiting = poll(client, run["run_id"], {"awaiting_approval"})
        approval = waiting["approvals"][0]
        assert approval["approval_mode"] == "dual"
        assert approval["required_approvals"] == 2
        assert approval["approver_ids"] == []
        client.post("/api/v1/auth/logout")
        login(client, "admin")
        pending = client.get("/api/v1/approvals/pending")
        assert pending.status_code == 200 and any(item["id"] == approval["id"] for item in pending.json())
        first = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
        assert first.status_code == 200 and first.json()["status"] == "pending"
        assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"}).status_code == 409
        client.post("/api/v1/auth/logout")
        login(client, "admin2")
        second = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
        assert second.status_code == 200 and second.json()["status"] == "allowed"
        client.post("/api/v1/auth/logout")
        login(client, "member")
        assert poll(client, run["run_id"])["status"] == "succeeded"
        row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],))
        assert row["decision_source"] == "dual_admin"
        assert len(service.db.all("SELECT * FROM approval_votes WHERE approval_id=?", (approval["id"],))) == 2
        events = service.db.events(run["run_id"], 0)
        assert any(event["type"] == "approval.dual_vote" for event in events)
        assert all("do-not-show" not in str(event["data"]) for event in events)
    finally:
        client.__exit__(None, None, None)


def test_member_cannot_approve_dual_and_restart_expires(tmp_path):
    client, service, admin1, admin2, member, conversation = setup_app(tmp_path)
    try:
        run = send(client, conversation["id"], "delete", "high-risk-restart")
        approval = poll(client, run["run_id"], {"awaiting_approval"})["approvals"][0]
        denied = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
        assert denied.status_code == 403
        service.manager.recover()
        row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],))
        assert row["status"] == "expired" and row["decision_source"] == "restart"
        assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"}).status_code == 409
    finally:
        client.__exit__(None, None, None)


def test_high_risk_queue_is_allowlisted_and_policy_rejects_non_high_tools(tmp_path):
    client, service, admin1, admin2, member, conversation = setup_app(tmp_path)
    try:
        assistant_id = conversation["assistant_id"]
        client.post("/api/v1/auth/logout")
        login(client, "admin")
        invalid = client.put(
            f"/api/v1/assistants/{assistant_id}/high-risk-approval",
            json={"enabled": True, "allowed_tools": ["Read"], "admin_allowlist": [admin1["id"], admin2["id"]]},
        )
        assert invalid.status_code == 422
        unknown = client.put(
            f"/api/v1/assistants/{assistant_id}/high-risk-approval",
            json={"enabled": True, "allowed_tools": ["new_unknown_tool"], "admin_allowlist": [admin1["id"], admin2["id"]]},
        )
        assert unknown.status_code == 422
        one_admin = client.put(
            f"/api/v1/assistants/{assistant_id}/high-risk-approval",
            json={"enabled": True, "allowed_tools": ["mcp__danger__delete"], "admin_allowlist": [admin1["id"]]},
        )
        assert one_admin.status_code == 422
        assert one_admin.json()["error"]["code"] == "HIGH_RISK_ADMIN_ALLOWLIST_REQUIRED"
        client.post("/api/v1/auth/logout")
        login(client, "member")
        run = send(client, conversation["id"], "delete", "queue-visibility")
        approval = poll(client, run["run_id"], {"awaiting_approval"})["approvals"][0]
        assert client.get("/api/v1/approvals/pending").status_code == 403
        client.post("/api/v1/auth/logout")
        login(client, "admin")
        queue = client.get("/api/v1/approvals/pending")
        assert queue.status_code == 200 and [item["id"] for item in queue.json()] == [approval["id"]]
        assert "do-not-show" not in queue.text
    finally:
        client.__exit__(None, None, None)


def test_policy_revision_invalidates_existing_dual_request(tmp_path):
    client, service, admin1, admin2, member, conversation = setup_app(tmp_path)
    try:
        run = send(client, conversation["id"], "delete", "revision-change")
        approval = poll(client, run["run_id"], {"awaiting_approval"})["approvals"][0]
        client.post("/api/v1/auth/logout")
        login(client, "admin")
        assert client.put(
            f"/api/v1/assistants/{conversation['assistant_id']}/high-risk-approval",
            json={"enabled": True, "allowed_tools": ["mcp__danger__delete"], "admin_allowlist": [admin1["id"], admin2["id"]]},
        ).status_code == 200
        # The queue must not show an approval whose policy snapshot was
        # invalidated, even before an administrator attempts to vote.
        assert client.get("/api/v1/approvals/pending").json() == []
        decision = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
        assert decision.status_code == 409 and decision.json()["error"]["code"] == "APPROVAL_POLICY_CHANGED"
    finally:
        client.__exit__(None, None, None)


def test_snapshot_current_rejects_policy_change_after_snapshot(tmp_path):
    client, service, admin1, admin2, member, conversation = setup_app(tmp_path)
    try:
        run = send(client, conversation["id"], "delete", "post-vote-recheck")
        approval = poll(client, run["run_id"], {"awaiting_approval"})["approvals"][0]
        run_row = service.db.one("SELECT * FROM runs WHERE id=?", (run["run_id"],))
        arguments = {"operation": "delete", "data_scope": "workspace", "secret": "do-not-show"}
        snapshot = service.high_risk_approval.request_snapshot(run_row, approval["tool_name"], arguments)
        assert snapshot and service.high_risk_approval.snapshot_current(run_row, approval["tool_name"], arguments, snapshot)
        service.save_high_risk_approval_policy(conversation["assistant_id"], {
            "enabled": True,
            "allowed_tools": ["mcp__danger__delete"],
            "admin_allowlist": [admin1["id"], admin2["id"]],
        }, admin1)
        assert not service.high_risk_approval.snapshot_current(run_row, approval["tool_name"], arguments, snapshot)
    finally:
        client.__exit__(None, None, None)


def test_record_vote_rejects_unknown_decision(tmp_path):
    client, service, admin1, admin2, member, conversation = setup_app(tmp_path)
    try:
        run = send(client, conversation["id"], "delete", "invalid-decision")
        approval = poll(client, run["run_id"], {"awaiting_approval"})["approvals"][0]
        client.post("/api/v1/auth/logout")
        login(client, "admin")
        with pytest.raises(AppError) as error:
            service.high_risk_approval.record_vote(approval, admin1, "other")
        assert error.value.code == "INVALID_APPROVAL_DECISION"
    finally:
        client.__exit__(None, None, None)
