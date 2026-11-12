"""Approval matrix evidence: three risk levels, four strategies, dual votes.

Every scenario runs against a local SQLite database, synthetic runtime and
synthetic tools. No model, provider, MCP network or credential is touched.
"""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security import tool_risk_profile
from test_api import PASSWORD, login, send


def wait_run(client, run_id, statuses=None, timeout=20):
    statuses = statuses or {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/runs/{run_id}").json()
        if result["status"] in statuses:
            return result
        time.sleep(0.02)
    raise AssertionError(f"run did not reach {statuses}: {result}")


class MatrixRuntime:
    """Synthetic runtime that asks approval for the tool named by the prompt."""

    def __init__(self, service, conversation_id, config, approve, emit):
        self.service, self.conversation_id, self.approve, self.emit = service, conversation_id, approve, emit

    async def connect(self):
        return None

    async def execute(self, run, message):
        text = message["text"]
        tool = text.split(":", 1)[1].strip() if text.startswith("tool:") else "Read"
        if tool in {"Read", "Glob", "Grep"}:
            arguments = {"operation": "read", "data_scope": "workspace", "file_path": "report.txt"}
        elif tool in {"Write", "Edit"}:
            arguments = {"operation": "write", "data_scope": "workspace", "file_path": "report.txt", "content": "synthetic", "expected_revision": 0}
        else:
            arguments = {"operation": "delete", "data_scope": "workspace", "file_path": "report.txt"}
        allowed = await self.approve(tool, arguments)
        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "已执行" if allowed else "已拒绝"})

    async def interrupt(self):
        return None

    async def close(self):
        return None


@pytest.fixture(scope="module")
def matrix_service(tmp_path_factory):
    root = tmp_path_factory.mktemp("approval-matrix")
    settings = Settings(
        data_dir=root, provider_settings="", origins=("http://testserver",),
        mcp_hosts=("localhost", "127.0.0.1"), idle_seconds=60, approval_seconds=30,
    )
    app = create_app(settings, runtime_factory=MatrixRuntime)
    client = TestClient(app)
    client.__enter__()
    service = app.state.service
    admin1 = service.create_user({"username": "admin", "display_name": "管理员一", "password": PASSWORD, "role": "admin"})
    admin2 = service.create_user({"username": "admin2", "display_name": "管理员二", "password": PASSWORD, "role": "admin"})
    member = service.create_user({"username": "member", "display_name": "员工", "password": PASSWORD, "role": "member"})
    try:
        yield client, service, admin1, admin2, member
    finally:
        client.__exit__(None, None, None)


@pytest.fixture
def matrix_app(matrix_service):
    # A fresh assistant isolates each policy and each conversation isolates its
    # runs, while the expensive app/service construction is shared.
    client, service, admin1, admin2, member = matrix_service
    assistant = service.save_assistant({"name": "审批矩阵", "instructions": "", "scope": "team", "connections": []}, user=admin1)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "审批矩阵"}, member)
    client.post("/api/v1/auth/logout")
    login(client, "member")
    return client, service, admin1, admin2, member, assistant, conversation


def _relogin(client, username):
    client.post("/api/v1/auth/logout")
    login(client, username)


def _model_policy(client, assistant_id, **overrides):
    value = {
        "enabled": True,
        "reviewer_mode": "synthetic",
        "model_id": "synthetic-matrix-v1",
        "allowed_tools": ["Read"],
        "allowed_data_scopes": ["workspace"],
        "max_amount": 100,
        "allowed_operations": ["read"],
    }
    value.update(overrides)
    response = client.put(f"/api/v1/assistants/{assistant_id}/model-approval", json=value)
    assert response.status_code == 200, response.text
    return response.json()


def _dual_policy(client, assistant_id, admin_ids, tools=("mcp__danger__delete",)):
    response = client.put(
        f"/api/v1/assistants/{assistant_id}/high-risk-approval",
        json={"enabled": True, "allowed_tools": list(tools), "admin_allowlist": list(admin_ids)},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("tool,level,strategy", [
    ("Read", "low", "safe_read"),
    ("Glob", "low", "safe_read"),
    ("Grep", "low", "safe_read"),
    ("Write", "medium", "user_confirmation"),
    ("Edit", "medium", "user_confirmation"),
    ("mcp__cfixture__publish", "high", "connection_policy"),
    ("mcp__danger__delete", "high", "connection_policy"),
    ("new_unknown_tool", "high", "deny_by_default"),
    ("mcp__workspace__generate_image", "high", "deny_by_default"),
])
def test_three_risk_levels_map_to_conservative_profiles(tool, level, strategy):
    profile = tool_risk_profile(tool)
    assert profile["risk_level"] == level
    assert profile["approval_policy"] == strategy


def test_safe_read_is_auto_allowed_but_medium_write_is_not(matrix_app):
    _client, service, _admin1, _admin2, _member, _assistant, conversation = matrix_app
    assert service.authorize_tool(conversation["id"], "Read", {"file_path": "report.txt"}) is True
    assert service.authorize_tool(conversation["id"], "Glob", {"path": "report.txt"}) is True
    assert service.authorize_tool(conversation["id"], "Write", {"file_path": "report.txt", "content": "x", "expected_revision": 0}) is False
    assert service.authorize_tool(conversation["id"], "Edit", {"file_path": "report.txt", "content": "x"}) is False


def test_medium_risk_uses_single_user_confirmation(matrix_app):
    client, service, _admin1, _admin2, _member, _assistant, conversation = matrix_app
    run = send(client, conversation["id"], "tool:Write", "matrix-medium")
    waiting = wait_run(client, run["run_id"], {"awaiting_approval"})
    approval = waiting["approvals"][0]
    assert approval["approval_mode"] == "single"
    assert approval["required_approvals"] == 1
    assert approval["risk_level"] == "medium"
    decided = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
    assert decided.status_code == 200
    assert wait_run(client, run["run_id"])["status"] == "succeeded"
    row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],))
    assert row["strategy"] == "user" and row["decision_source"] == "user"


def test_low_risk_model_approval_applies_and_medium_falls_through(matrix_app):
    client, service, admin1, _admin2, _member, assistant, conversation = matrix_app
    _relogin(client, "admin")
    _model_policy(client, assistant["id"])

    async def reviewer(_request):
        return {"decision": "allow", "reason": "合成矩阵通过", "confidence": 0.99, "uncertain": False}

    service.model_approval.reviewer = reviewer
    _relogin(client, "member")

    run = send(client, conversation["id"], "tool:Read", "matrix-model-low")
    result = wait_run(client, run["run_id"])
    assert result["status"] == "succeeded"
    row = service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["run_id"],))
    assert row["strategy"] == "model" and row["status"] == "allowed"
    assert row["decision_source"] == "model"
    assert row["review_model_id"] == "synthetic-matrix-v1"
    assert row["review_policy_revision"] == 1

    # Write is medium risk; it must never be decided by the model reviewer.
    run = send(client, conversation["id"], "tool:Write", "matrix-model-medium")
    waiting = wait_run(client, run["run_id"], {"awaiting_approval"})
    approval = waiting["approvals"][0]
    assert approval["approval_mode"] == "single" and approval["strategy"] == "user"
    assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"}).status_code == 200
    assert wait_run(client, run["run_id"])["status"] == "succeeded"
    assert service.db.one("SELECT strategy FROM approvals WHERE id=?", (approval["id"],))["strategy"] == "user"


def test_model_review_failure_and_out_of_scope_fail_closed(matrix_app):
    client, service, _admin1, _admin2, _member, assistant, conversation = matrix_app
    _relogin(client, "admin")

    async def broken(_request):
        raise RuntimeError("synthetic reviewer fault")

    service.model_approval.reviewer = broken
    _model_policy(client, assistant["id"])
    _relogin(client, "member")
    run = send(client, conversation["id"], "tool:Read", "matrix-model-error")
    assert wait_run(client, run["run_id"])["status"] == "succeeded"
    approval = service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["run_id"],))
    assert approval["status"] == "denied"
    assert approval["decision_source"] == "model"
    assert "reviewer_error" in approval["review_result"]

    # An operation outside the administrator allowlist is denied before the
    # reviewer is ever called.
    _relogin(client, "admin")
    called = []
    _model_policy(client, assistant["id"], allowed_operations=["edit"])

    async def must_not_run(_request):
        called.append(True)
        return {"decision": "allow", "reason": "must not run", "confidence": 1.0, "uncertain": False}

    service.model_approval.reviewer = must_not_run
    _relogin(client, "member")
    run = send(client, conversation["id"], "tool:Read", "matrix-model-scope")
    assert wait_run(client, run["run_id"])["status"] == "succeeded"
    approval = service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["run_id"],))
    assert approval["status"] == "denied"
    assert approval["review_reason"] == "操作范围不在管理员代审白名单"
    assert called == []


def test_model_review_never_decides_high_risk(matrix_app):
    client, service, _admin1, _admin2, _member, assistant, conversation = matrix_app
    _relogin(client, "admin")
    _model_policy(client, assistant["id"])

    async def always_allow(_request):
        return {"decision": "allow", "reason": "must never be used", "confidence": 1.0, "uncertain": False}

    service.model_approval.reviewer = always_allow
    run_row = {"id": "synthetic", "conversation_id": conversation["id"]}
    result = asyncio.run(service.model_approval.review(  # driver: runtime falls through to human/dual
        run_row, "mcp__danger__delete", {"operation": "delete", "data_scope": "workspace"}
    ))
    assert result is None
    assert service.high_risk_approval.is_high_risk("mcp__danger__delete", {"operation": "delete"}) is True


def test_high_risk_requires_two_distinct_admins_and_requester_cannot_self_approve(matrix_app):
    client, service, admin1, admin2, member, assistant, conversation = matrix_app
    _relogin(client, "admin")
    policy = _dual_policy(client, assistant["id"], [admin1["id"], admin2["id"]])
    assert policy["required_approvals"] == 2
    _relogin(client, "member")

    run = send(client, conversation["id"], "tool:mcp__danger__delete", "matrix-dual")
    waiting = wait_run(client, run["run_id"], {"awaiting_approval"})
    approval = waiting["approvals"][0]
    assert approval["approval_mode"] == "dual"
    assert approval["required_approvals"] == 2
    assert approval["approver_ids"] == []
    assert approval["strategy"] == "dual"

    # The requester may not approve their own high-risk request.
    denied = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "REQUESTER_CANNOT_APPROVE"

    _relogin(client, "admin")
    first = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
    assert first.status_code == 200 and first.json()["status"] == "pending"
    duplicate = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
    assert duplicate.status_code == 409

    _relogin(client, "admin2")
    second = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"})
    assert second.status_code == 200 and second.json()["status"] == "allowed"

    _relogin(client, "member")
    assert wait_run(client, run["run_id"])["status"] == "succeeded"
    row = service.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],))
    assert row["decision_source"] == "dual_admin"
    assert len(json.loads(row["approver_ids"])) == 2
    assert row["approval_policy_id"] and row["approval_policy_revision"] and row["approval_policy_digest"]
    assert row["approval_request_digest"]
    votes = service.db.all("SELECT * FROM approval_votes WHERE approval_id=?", (approval["id"],))
    assert len(votes) == 2 and len({vote["voter_id"] for vote in votes}) == 2


def test_high_risk_policy_rejects_unsafe_configuration(matrix_app):
    client, _service, admin1, admin2, _member, assistant, _conversation = matrix_app
    _relogin(client, "admin")
    invalid_tool = client.put(
        f"/api/v1/assistants/{assistant['id']}/high-risk-approval",
        json={"enabled": True, "allowed_tools": ["Read"], "admin_allowlist": [admin1["id"], admin2["id"]]},
    )
    assert invalid_tool.status_code == 422 and invalid_tool.json()["error"]["code"] == "HIGH_RISK_TOOL_REQUIRED"
    one_admin = client.put(
        f"/api/v1/assistants/{assistant['id']}/high-risk-approval",
        json={"enabled": True, "allowed_tools": ["mcp__danger__delete"], "admin_allowlist": [admin1["id"]]},
    )
    assert one_admin.status_code == 422 and one_admin.json()["error"]["code"] == "HIGH_RISK_ADMIN_ALLOWLIST_REQUIRED"
    non_admin = client.put(
        f"/api/v1/assistants/{assistant['id']}/high-risk-approval",
        json={"enabled": True, "allowed_tools": ["mcp__danger__delete"], "admin_allowlist": [_service.db.one("SELECT id FROM users WHERE role='member'")["id"], admin1["id"]]},
    )
    assert non_admin.status_code == 422
    missing_admin = client.put(
        f"/api/v1/assistants/{assistant['id']}/high-risk-approval",
        json={"enabled": True, "allowed_tools": ["mcp__danger__delete"], "admin_allowlist": ["missing", admin1["id"]]},
    )
    assert missing_admin.status_code == 422


def test_high_risk_empty_allowlist_defaults_to_deny(matrix_app):
    client, service, admin1, admin2, _member, assistant, conversation = matrix_app
    _relogin(client, "admin")
    # Enabled but empty tool allowlist: not a wildcard, and not a single-user
    # fallback. The snapshot resolves to a policy denial.
    response = client.put(
        f"/api/v1/assistants/{assistant['id']}/high-risk-approval",
        json={"enabled": True, "allowed_tools": [], "admin_allowlist": [admin1["id"], admin2["id"]]},
    )
    assert response.status_code == 200
    _relogin(client, "member")
    run = send(client, conversation["id"], "tool:mcp__danger__delete", "matrix-emptytools")
    waiting = wait_run(client, run["run_id"])
    approval = service.db.one("SELECT * FROM approvals WHERE run_id=?", (run["run_id"],))
    assert approval["status"] == "denied" and approval["decision_source"] == "policy"
    assert approval["approval_mode"] == "dual"
    assert not service.db.all("SELECT * FROM approval_votes WHERE approval_id=?", (approval["id"],))


def test_subagent_high_risk_cascade_denies_without_policy_and_inherits_dual(matrix_app):
    _client, service, admin1, admin2, member, assistant, _conversation = matrix_app
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "子任务矩阵"}, member)

    # No parent high-risk policy: default deny with an audit row, no votes.
    run, _ = service.prepare_run(conversation["id"], {"client_request_id": "matrix-child-deny", "text": "parent", "file_ids": []}, member)
    service.subagents.create_plan(run["run_id"], member["id"], [{
        "task": "delete fixture", "tool": "mcp__danger__delete", "arguments": {"secret": "synthetic-secret"},
    }])
    result = asyncio.run(service.subagents.run_plan(run["run_id"], member["id"]))
    assert result["counts"]["failed"] == 1
    approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run["run_id"],))
    assert approval["status"] == "denied" and approval["decision_source"] == "policy"
    assert not service.db.all("SELECT * FROM approval_votes WHERE approval_id=?", (approval["id"],))

    # With a parent dual policy, the child waits for two distinct admins.
    service.save_high_risk_approval_policy(assistant["id"], {
        "enabled": True, "allowed_tools": ["mcp__danger__delete"],
        "admin_allowlist": [admin1["id"], admin2["id"]],
    }, admin1)
    conversation2 = service.create_conversation({"assistant_id": assistant["id"], "title": "子任务矩阵二"}, member)
    run2, _ = service.prepare_run(conversation2["id"], {"client_request_id": "matrix-child-dual", "text": "parent", "file_ids": []}, member)
    service.subagents.create_plan(run2["run_id"], member["id"], [{
        "task": "delete fixture", "tool": "mcp__danger__delete", "arguments": {"operation": "delete"},
    }])

    async def execute_and_vote():
        running = asyncio.create_task(service.subagents.run_plan(run2["run_id"], member["id"]))
        child_approval = None
        for _ in range(100):
            child_approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run2["run_id"],))
            if child_approval and child_approval["status"] == "pending":
                break
            await asyncio.sleep(0.01)
        assert child_approval and child_approval["approval_mode"] == "dual"
        assert service.high_risk_approval.record_vote(child_approval, admin1, "allow")["terminal"] is False
        second = service.high_risk_approval.record_vote(
            service.db.one("SELECT * FROM approvals WHERE id=?", (child_approval["id"],)), admin2, "allow"
        )
        assert second["status"] == "allowed"
        await running

    asyncio.run(execute_and_vote())
    child = service.db.one("SELECT status,stop_reason FROM subagent_children WHERE parent_run_id=?", (run2["run_id"],))
    assert child["status"] == "succeeded" and child["stop_reason"] == "completed"
    child_approval = service.db.one("SELECT * FROM approvals WHERE run_id=? AND subagent_child_id IS NOT NULL", (run2["run_id"],))
    assert child_approval["decision_source"] == "dual_admin"
    events = service.db.events(run2["run_id"], 0)
    assert any(event["type"] == "subagent.approval.required" for event in events)
    assert "synthetic-secret" not in str(events)
