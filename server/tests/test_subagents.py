import asyncio
import json
import sqlite3
import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.subagents import SubagentOrchestrator
from test_api import FakeRuntime, login


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
        service.subagents.child_timeout_seconds = 30
        service.subagents.create_plan(run["run_id"], user["id"], ["slow", "fast"])
        execute = service.subagents.executor

        async def cancel_during_run():
            started = asyncio.Event()

            async def paused_executor(child, task):
                if task["task"] == "slow":
                    started.set()
                    await asyncio.Future()
                return await execute(child, task)

            service.subagents.executor = paused_executor
            running = asyncio.create_task(service.subagents.run_plan(run["run_id"], user["id"]))
            await asyncio.wait_for(started.wait(), 10)
            await service.subagents.cancel_plan(run["run_id"], user["id"])
            await running

        asyncio.run(cancel_during_run())
        rows = service.db.all("SELECT status FROM subagent_children WHERE parent_run_id=?", (run["run_id"],))
        assert all(row["status"] in {"cancelled", "succeeded"} for row in rows)
        assert any(row["status"] == "cancelled" for row in rows)
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


def test_plan_get_returns_only_allowlisted_titles_and_keeps_prompts_private(tmp_path):
    settings = Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",))
    app = create_app(settings, runtime_factory=FakeRuntime)
    prompts = [
        "请为示例客户 private-customer@fixture.invalid 统计商品总数",
        "为内部交付整理检查清单，参考 https://private.fixture.invalid/checks",
        "【林示例】整理 <script>private-label</script> 的背景信息",
        "审阅 token=private-token-value",
    ]
    with TestClient(app) as client:
        service, _, run = make_parent(app)
        login(client)
        run_id = run["run_id"]
        route = f"/api/v1/runs/{run_id}/subagents"
        service.db.execute("UPDATE messages SET text=? WHERE id=?", ("请整理林示例的背景信息。", run["message"]["id"]))
        created = client.post(route, json={"tasks": prompts})
        assert created.status_code == 202
        assert all("display_title" not in child for child in created.json()["children"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            plan = client.get(route).json()
            if plan["counts"]["succeeded"] + plan["counts"]["failed"] == len(prompts):
                break
            time.sleep(0.02)
        assert plan["counts"]["succeeded"] == 3
        assert plan["counts"]["failed"] == 1
        assert [child.get("display_title") for child in plan["children"]] == ["商品数量统计", "交付检查清单", None, None]

        rows = service.db.all("SELECT * FROM subagent_children WHERE parent_run_id=? ORDER BY rowid", (run_id,))
        assert [row["display_title_key"] for row in rows] == ["product_count", "delivery_checklist", None, None]
        approvals = service.db.all("SELECT * FROM approvals WHERE run_id=?", (run_id,))
        assert [row["tool_name"] for row in approvals] == ["subagent_undeclared_tool"]
        events = service.db.events(run_id, 0)
        assert "display_title" not in json.dumps(events)
        persisted = json.dumps([rows, approvals, events], ensure_ascii=False)
        for private in ("private-customer", "private.fixture.invalid", "private-label", "private-token-value", "林示例"):
            assert private not in persisted
            assert private not in json.dumps(plan, ensure_ascii=False)

        # Old Harness tool events may have included the raw arguments.
        tool = {"tool_name": "mcp__workspace__delegate_tasks", "arguments": {"tasks": prompts}}
        service.db.event(run_id, "tool.started", tool)
        service.db.event(run_id, "approval.required", tool)
        service.manager.finish(run_id, "cancelled")
        events_response = client.get(f"/api/v1/runs/{run_id}/events")
        assert events_response.status_code == 200
        assert events_response.text.count('"tasks":"[redacted]"') == 2
        assert "display_title" not in events_response.text
        assert all(private not in events_response.text for private in ("private-", "private.fixture.invalid", "林示例"))
        service.db.execute("UPDATE subagent_children SET display_title_key=? WHERE child_id=?", ("unrecognized-private-label", rows[2]["child_id"]))

        service.create_user({"username": "subagent-reader", "display_name": "他人", "password": PASSWORD, "role": "member"})
        login(client, "subagent-reader")
        assert client.get(route).status_code == 404

    with TestClient(create_app(settings, runtime_factory=FakeRuntime)) as client:
        login(client)
        titles = [child.get("display_title") for child in client.get(route).json()["children"]]
        assert titles == ["商品数量统计", "交付检查清单", None, None]


def test_existing_children_survive_database_upgrade_without_titles(tmp_path):
    settings = Settings(data_dir=tmp_path, provider_settings="", origins=("http://testserver",))
    app = create_app(settings, runtime_factory=FakeRuntime)
    with TestClient(app):
        service, user, run = make_parent(app)
        plan = service.subagents.create_plan(run["run_id"], user["id"], ["统计商品数量"])
        expected = plan["children"][0]
        asyncio.run(service.subagents.run_plan(run["run_id"], user["id"]))

    with sqlite3.connect(tmp_path / "app.sqlite3") as db:
        db.execute("ALTER TABLE subagent_children DROP COLUMN display_title_key")

    with TestClient(create_app(settings, runtime_factory=FakeRuntime)) as client:
        login(client)
        plan = client.get(f"/api/v1/runs/{run['run_id']}/subagents")
        assert plan.status_code == 200
        child, = plan.json()["children"]
        assert child["child_id"] == expected["child_id"]
        assert child["task_digest"] == expected["task_digest"]
        assert child["status"] == "succeeded"
        assert "display_title" not in child


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
