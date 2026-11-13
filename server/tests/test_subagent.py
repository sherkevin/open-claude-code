import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import AppError, workspace_path
from app.subagents import SubagentOrchestrator
from test_api import FakeRuntime


PASSWORD = "Correct-test-password-123"


def make_parent(app):
    service = app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    if not user:
        user = service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
    assistant = service.save_assistant({"name": "Synthetic", "instructions": "readonly", "connections": []})
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Synthetic parent"}, user)
    run, _ = service.prepare_run(conversation["id"], {"client_request_id": "subagent-parent-1", "text": "parent", "file_ids": []}, user)
    return service, user, run


def test_synthetic_children_are_isolated_and_replayable(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app):
        service, user, run = make_parent(app)
        plan = service.subagents.create_plan(run["run_id"], user["id"], ["设备温度", {"task": "压力趋势", "result": {"trend": "stable"}}])
        assert plan["counts"]["queued"] == 2
        result = asyncio.run(service.subagents.run_plan(run["run_id"], user["id"]))
        assert result["counts"]["succeeded"] == 2
        children = service.subagents.list_children(run["run_id"], user["id"])
        assert len({child["workspace_id"] for child in children}) == 2
        assert len({child["memory_scope_id"] for child in children}) == 2
        conversation_id = service.require("runs", run["run_id"])["conversation_id"]
        child_root = service.settings.data_dir / "workspaces" / conversation_id
        for child in children:
            assert child["isolation"] == "dedicated_workspace_and_memory"
            assert (child_root / child["workspace_id"]).is_dir()
            assert (child_root / child["memory_scope_id"]).is_dir()
            with pytest.raises(AppError):
                workspace_path(child_root / child["workspace_id"], "../sibling")
        assert all(child["capability_snapshot"]["writes"] is False for child in children)
        assert all(child["capability_snapshot"]["network"] is False for child in children)
        events = service.db.events(run["run_id"], 0)
        assert [event["id"] for event in events] == list(range(1, len(events) + 1))
        payload = "".join(str(event["data"]) for event in events)
        assert "设备温度" not in payload and "压力趋势" not in payload
        assert any(event["type"] == "subagent.plan.completed" for event in events)


def test_failure_isolated_and_bounds_are_enforced(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app):
        service, user, run = make_parent(app)
        plan = service.subagents.create_plan(run["run_id"], user["id"], [{"task": "good"}, {"task": "bad", "behavior": "fail"}])
        result = asyncio.run(service.subagents.run_plan(run["run_id"], user["id"]))
        statuses = {child["status"] for child in result["children"]}
        assert statuses == {"succeeded", "failed"}
        assert plan["children"][0]["task_digest"] != plan["children"][1]["task_digest"]
        try:
            service.subagents.create_plan(run["run_id"], user["id"], ["x"] * 5)
        except Exception as exc:
            assert getattr(exc, "code", None) == "SUBAGENT_CHILD_LIMIT"


def test_parent_cancel_cascades_and_recovery_does_not_replay(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app):
        service, user, run = make_parent(app)
        service.subagents.child_timeout_seconds = 1
        service.subagents.create_plan(run["run_id"], user["id"], [{"task": "slow", "behavior": "delay"}, "fast"])

        async def cancel_during_run():
            running = asyncio.create_task(service.subagents.run_plan(run["run_id"], user["id"]))
            await asyncio.sleep(0.03)
            await service.subagents.cancel_plan(run["run_id"], user["id"])
            await running

        asyncio.run(cancel_during_run())
        rows = service.db.all("SELECT status FROM subagent_children WHERE parent_run_id=?", (run["run_id"],))
        assert all(row["status"] in {"cancelled", "succeeded"} for row in rows)
        before = service.db.all("SELECT child_id,status FROM subagent_children WHERE parent_run_id=?", (run["run_id"],))
        event_count = len(service.db.events(run["run_id"], 0))
        asyncio.run(service.subagents.cancel_plan(run["run_id"], user["id"]))
        after = service.db.all("SELECT child_id,status FROM subagent_children WHERE parent_run_id=?", (run["run_id"],))
        events = service.db.events(run["run_id"], 0)
        assert before == after and len(events) == event_count
        assert sum(event["type"] == "subagent.plan.completed" for event in events) == 1

        # Terminal parents retain read-only child history for audit and SSE
        # replay; only creating or mutating a new plan is blocked.
        service.manager.finish(run["run_id"], "succeeded")
        history = service.subagents.plan_view(run["run_id"], user["id"])
        assert history["counts"]["cancelled"] + history["counts"]["succeeded"] == 2

        _, _, recovery_run = make_parent(app)
        service.subagents.create_plan(recovery_run["run_id"], user["id"], ["queued"])
        service.subagents.recover()
        row = service.db.one("SELECT status,stop_reason FROM subagent_children WHERE parent_run_id=?", (recovery_run["run_id"],))
        assert row == {"status": "interrupted", "stop_reason": "recovered_after_restart"}
        assert any(event["type"] == "subagent.plan.completed" and event["data"]["status"] == "interrupted" for event in service.db.events(recovery_run["run_id"], 0))


def test_protected_http_contract_is_synthetic_only(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app) as client:
        service = app.state.service
        service.create_user({"username": "admin", "display_name": "管理员", "password": PASSWORD, "role": "admin"})
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
        assistant = client.post("/api/v1/assistants", json={"name": "Synthetic", "connections": []}).json()
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        response = client.post(f"/api/v1/conversations/{conversation['id']}/messages", json={"client_request_id": "subagent-http-1", "text": "wait"})
        run_id = response.json()["run_id"]
        response = client.post(f"/api/v1/runs/{run_id}/subagents", json={"mode": "model", "tasks": ["not allowed"]})
        assert response.status_code == 422
        response = client.post(f"/api/v1/runs/{run_id}/subagents", json={"tasks": ["local fixture"]})
        assert response.status_code == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            value = client.get(f"/api/v1/runs/{run_id}/subagents").json()
            if value["counts"].get("succeeded") == 1:
                break
            time.sleep(0.02)
        assert value["counts"]["succeeded"] == 1


def _dual_parent(app):
    """Create a member-owned parent with an explicit two-admin policy."""
    service, admin1, source_run = make_parent(app)
    source_row = service.db.one("SELECT * FROM runs WHERE id=?", (source_run["run_id"],))
    conversation = service.require("conversations", source_row["conversation_id"])
    member = service.create_user({"username": "subagent-member", "display_name": "子任务发起人", "password": PASSWORD, "role": "member"})
    admin2 = service.create_user({"username": "subagent-admin2", "display_name": "审批管理员二", "password": PASSWORD, "role": "admin"})
    member_conversation = service.create_conversation({"assistant_id": conversation["assistant_id"], "title": "Dual child parent"}, member)
    run, _ = service.prepare_run(member_conversation["id"], {"client_request_id": "subagent-dual-parent", "text": "parent", "file_ids": []}, member)
    service.save_high_risk_approval_policy(conversation["assistant_id"], {
        "enabled": True,
        "allowed_tools": ["mcp__danger__delete"],
        "admin_allowlist": [admin1["id"], admin2["id"],],
    }, admin1)
    return service, member, admin1, admin2, run


def test_high_risk_child_inherits_parent_dual_approval(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app):
        service, member, admin1, admin2, run = _dual_parent(app)
        service.subagents.create_plan(run["run_id"], member["id"], [{
            "task": "delete fixture", "tool": "mcp__danger__delete", "arguments": {"operation": "delete", "secret": "secret-value"},
        }])

        async def execute_and_vote():
            running = asyncio.create_task(service.subagents.run_plan(run["run_id"], member["id"]))
            approval = None
            for _ in range(100):
                approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
                if approval and approval["status"] == "pending":
                    break
                await asyncio.sleep(0.01)
            assert approval and approval["approval_mode"] == "dual"
            assert approval["subagent_child_id"]
            first = service.high_risk_approval.record_vote(approval, admin1, "allow")
            assert first["terminal"] is False
            second = service.high_risk_approval.record_vote(service.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],)), admin2, "allow")
            assert second["status"] == "allowed"
            await running

        asyncio.run(execute_and_vote())
        child = service.db.one("SELECT status,stop_reason FROM subagent_children WHERE parent_run_id=?", (run["run_id"],))
        approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
        assert child == {"status": "succeeded", "stop_reason": "completed"}
        assert approval["decision_source"] == "dual_admin"
        events = service.db.events(run["run_id"], 0)
        assert any(event["type"] == "subagent.approval.required" for event in events)
        assert any(event["type"] == "subagent.approval.resolved" and event["data"]["decision"] == "allow" for event in events)
        assert "secret-value" not in str(events)


def test_high_risk_child_without_parent_policy_is_default_denied(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app):
        service, user, run = make_parent(app)
        service.subagents.create_plan(run["run_id"], user["id"], [{
            "task": "delete fixture", "tool": "mcp__danger__delete", "arguments": {"secret": "secret-value"},
        }])
        result = asyncio.run(service.subagents.run_plan(run["run_id"], user["id"]))
        assert result["counts"]["failed"] == 1
        approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
        assert approval["status"] == "denied" and approval["decision_source"] == "policy"
        assert approval["approval_decision_reason"]
        assert not service.db.all("SELECT * FROM approval_votes WHERE approval_id=?", (approval["id"],))


def test_parent_cancel_expires_waiting_child_approval(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app):
        service, member, admin1, admin2, run = _dual_parent(app)
        service.subagents.create_plan(run["run_id"], member["id"], [{
            "task": "delete fixture", "tool": "mcp__danger__delete", "arguments": {"operation": "delete"},
        }])

        async def cancel_while_waiting():
            running = asyncio.create_task(service.subagents.run_plan(run["run_id"], member["id"]))
            for _ in range(100):
                approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
                if approval and approval["status"] == "pending":
                    break
                await asyncio.sleep(0.01)
            assert approval and approval["status"] == "pending"
            await service.subagents.cancel_plan(run["run_id"], member["id"])
            await running

        asyncio.run(cancel_while_waiting())
        child = service.db.one("SELECT status,stop_reason FROM subagent_children WHERE parent_run_id=?", (run["run_id"],))
        approval = service.db.one("SELECT status,decision_source FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
        assert child["status"] == "cancelled" and child["stop_reason"] == "parent_cancelled"
        assert approval == {"status": "expired", "decision_source": "parent_cancelled"}


def test_child_final_vote_rechecks_parent_policy(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",)), runtime_factory=FakeRuntime)
    with TestClient(app):
        service, member, admin1, admin2, run = _dual_parent(app)
        service.subagents.create_plan(run["run_id"], member["id"], [{
            "task": "delete fixture", "tool": "mcp__danger__delete", "arguments": {"operation": "delete"},
        }])
        # Simulate a policy revocation racing with the final vote wake-up.
        service.high_risk_approval.snapshot_current = lambda *_args, **_kwargs: False

        async def execute_and_vote():
            running = asyncio.create_task(service.subagents.run_plan(run["run_id"], member["id"]))
            for _ in range(100):
                approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
                if approval and approval["status"] == "pending":
                    break
                await asyncio.sleep(0.01)
            assert approval and approval["status"] == "pending"
            assert service.high_risk_approval.record_vote(approval, admin1, "allow")["terminal"] is False
            assert service.high_risk_approval.record_vote(service.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],)), admin2, "allow")["status"] == "allowed"
            await running

        asyncio.run(execute_and_vote())
        child = service.db.one("SELECT status,stop_reason FROM subagent_children WHERE parent_run_id=?", (run["run_id"],))
        approval = service.db.one("SELECT status,decision_source FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
        assert child == {"status": "failed", "stop_reason": "approval_denied"}
        assert approval == {"status": "denied", "decision_source": "policy"}
