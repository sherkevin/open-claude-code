"""Real Service/SessionActor approval boundaries; no provider or child process."""

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import dump
from app.harness_runtime import HarnessRuntime
from app.runtime import RunManager, SessionActor
from app.security import AppError
from app.service import Service


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="fixture",
                               runtime_type="harness", approval_seconds=2))
    owner = service.create_user({"username": "owner", "display_name": "Owner", "role": "admin",
                                "password": "harness-tools-test-123"})
    kb = service.knowledge.save({"name": "Fixture"}, owner)
    service.knowledge.upload(kb["id"], owner, "pump.md", b"P-900 pressure limit is 7 bar.")
    assistant = service.save_assistant({"name": "Fixture", "knowledge_base_ids": [kb["id"]]}, user=owner)
    cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Fixture"}, owner)["id"]
    result, _ = service.prepare_run(cid, {"text": "Find the pressure limit", "file_ids": [], "client_request_id": "fixture"}, owner)
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (result["run_id"],))
    actor = SessionActor.__new__(SessionActor)
    actor.service, actor.conversation_id = service, cid
    actor.current_run_id, actor.cancelled = result["run_id"], False
    actor.manager = SimpleNamespace(service=service, approvals={}, context_requests={})
    service.manager = actor.manager
    runtime = HarnessRuntime(service, cid, service.runtime_config(cid), actor.approve, actor.emit)
    runtime.current_run = service.require("runs", result["run_id"])
    actor.runtime = runtime
    try:
        yield runtime, actor, owner, assistant, kb
    finally:
        service.db.close()


def payload(runtime, name="knowledge_search", arguments=None, call_id="fixture-call"):
    return {"schema": "workspace-tool/v1", "sessionId": runtime.session_id, "callId": call_id,
            "toolName": name, "arguments": {"query": "P-900"} if arguments is None else arguments}


def xlsx_payload(runtime):
    return payload(runtime, "generate_xlsx", {"filename": "reviewed.xlsx", "sheets": [
        {"name": "Evidence", "columns": ["Name"], "rows": [["P-900"]]},
    ]})


async def pending_approval(actor):
    for _ in range(100):
        rows = actor.service.db.all("SELECT * FROM approvals WHERE run_id=? AND status='pending'", (actor.current_run_id,))
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise AssertionError("Expected a real SessionActor approval")


def artifact_path(runtime):
    return runtime.service.settings.data_dir / "workspaces" / runtime.conversation_id / "artifacts" / "reviewed.xlsx"


def test_harness_real_knowledge_read_has_registered_sources_and_rejects_duplicate_rpc(system):
    runtime, actor, *_ = system

    async def scenario():
        request = payload(runtime)
        result = await runtime.bridge.invoke(request)
        assert not result.get("is_error")
        assert "7 bar" in result["content"][0]["text"]
        events = runtime.service.db.events(actor.current_run_id, 0)
        completed = [event for event in events if event["type"] == "tool.completed"][-1]
        assert completed["data"]["success"] is True and completed["data"]["source_ids"]
        assert not runtime.service.db.all("SELECT * FROM approvals")
        with pytest.raises(AppError) as error:
            await runtime.bridge.invoke(request)
        assert error.value.code == "HARNESS_TOOL_DENIED"

    asyncio.run(scenario())


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_harness_xlsx_waits_for_real_actor_approval(system, decision):
    runtime, actor, owner, *_ = system

    async def scenario():
        task = asyncio.create_task(runtime.bridge.invoke(xlsx_payload(runtime)))
        approval = await pending_approval(actor)
        assert not artifact_path(runtime).exists()
        runtime.service.db.execute("UPDATE approvals SET decided_by=?,decision_source='user' WHERE id=?", (owner["id"], approval["id"]))
        actor.manager.approvals[approval["id"]].set_result(decision)
        result = await asyncio.wait_for(task, 2)
        assert bool(result.get("is_error")) is (decision == "deny")
        assert artifact_path(runtime).exists() is (decision == "allow")
        row = runtime.service.require("approvals", approval["id"])
        assert row["status"] == ("allowed" if decision == "allow" else "denied")
        assert row["decided_by"] == owner["id"]

    asyncio.run(scenario())


def test_harness_expired_approval_cannot_create_artifact(system):
    runtime, actor, *_ = system
    runtime.service.settings.approval_seconds = 0.05

    async def scenario():
        task = asyncio.create_task(runtime.bridge.invoke(xlsx_payload(runtime)))
        approval = await pending_approval(actor)
        result = await asyncio.wait_for(task, 2)
        assert result["is_error"] and not artifact_path(runtime).exists()
        row = runtime.service.require("approvals", approval["id"])
        assert row["status"] == "denied" and row["decision_source"] == "timeout"
        assert approval["id"] not in actor.manager.approvals

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["disable_user", "disable_assistant", "cancel"])
def test_harness_approved_write_never_executes_after_revocation_or_cancel(system, change):
    runtime, actor, owner, assistant, _ = system

    async def scenario():
        task = asyncio.create_task(runtime.bridge.invoke(xlsx_payload(runtime)))
        approval = await pending_approval(actor)
        if change == "cancel":
            await actor.cancel()
        else:
            table, key = ("users", owner["id"]) if change == "disable_user" else ("assistants", assistant["id"])
            runtime.service.db.execute(f"UPDATE {table} SET enabled=0 WHERE id=?", (key,))
            actor.manager.approvals[approval["id"]].set_result("allow")
        result = await asyncio.wait_for(task, 2)
        assert result["is_error"] is True
        assert not artifact_path(runtime).exists()
        completed = [event for event in runtime.service.db.events(actor.current_run_id, 0) if event["type"] == "tool.completed"][-1]
        assert completed["data"]["success"] is False

    asyncio.run(scenario())


def test_harness_discards_read_result_when_scope_is_revoked_during_await(system, monkeypatch):
    runtime, actor, _, assistant, _ = system

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"private": "must-not-return-after-revocation"}

        monkeypatch.setattr(runtime.service.knowledge, "search_for_run", delayed)
        task = asyncio.create_task(runtime.bridge.invoke(payload(runtime)))
        await asyncio.wait_for(started.wait(), 2)
        runtime.service.db.execute("UPDATE assistants SET knowledge_base_ids='[]',revision=revision+1 WHERE id=?", (assistant["id"],))
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result["is_error"] and "must-not-return" not in dump(result)
        completed = [event for event in runtime.service.db.events(actor.current_run_id, 0) if event["type"] == "tool.completed"][-1]
        assert completed["data"]["source_ids"] == []

    asyncio.run(scenario())


def save_approval_policy(system, policy, *, enabled):
    runtime, _, owner, assistant, _ = system
    if policy == "model":
        return runtime.service.save_model_approval_policy(assistant["id"], {
            "enabled": enabled, "model_id": "synthetic-policy-boundary", "allowed_tools": ["Read"],
            "allowed_data_scopes": ["workspace"], "allowed_operations": ["read"], "max_amount": 0,
        }, owner)
    return runtime.service.save_high_risk_approval_policy(assistant["id"], {
        "enabled": enabled, "allowed_tools": [], "admin_allowlist": [],
    }, owner)


@pytest.mark.parametrize("policy", ["model", "dual"])
@pytest.mark.parametrize("initial", ["absent", "disabled"])
def test_harness_discards_read_result_when_approval_policy_changes(system, monkeypatch, policy, initial):
    runtime, actor, *_ = system
    if initial == "disabled":
        save_approval_policy(system, policy, enabled=False)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"content": [{"type": "text", "text": "sensitive-result-before-policy-change"}]}

        monkeypatch.setattr(runtime.service.knowledge, "search_for_run", delayed)
        task = asyncio.create_task(runtime.bridge.invoke(payload(runtime)))
        await asyncio.wait_for(started.wait(), 2)
        save_approval_policy(system, policy, enabled=True)
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result["is_error"] and "sensitive-result" not in dump(result)
        assert "审批策略已变化" in dump(result)
        completed = [event for event in runtime.service.db.events(actor.current_run_id, 0) if event["type"] == "tool.completed"][-1]
        assert completed["data"]["success"] is False and completed["data"]["source_ids"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("policy", ["model", "dual"])
def test_harness_approved_write_rejects_changed_approval_policy(system, policy):
    runtime, actor, owner, *_ = system
    save_approval_policy(system, policy, enabled=False)

    async def scenario():
        task = asyncio.create_task(runtime.bridge.invoke(xlsx_payload(runtime)))
        approval = await pending_approval(actor)
        save_approval_policy(system, policy, enabled=True)
        runtime.service.db.execute("UPDATE approvals SET decided_by=?,decision_source='user' WHERE id=?", (owner["id"], approval["id"]))
        actor.manager.approvals[approval["id"]].set_result("allow")
        result = await asyncio.wait_for(task, 2)
        assert result["is_error"] and "审批策略已变化" in dump(result)
        assert not artifact_path(runtime).exists()
        completed = [event for event in runtime.service.db.events(actor.current_run_id, 0) if event["type"] == "tool.completed"][-1]
        assert completed["data"]["success"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("policy", ["model", "dual"])
def test_harness_cannot_bypass_enabled_policy_for_unsupported_workspace_tool(system, policy, monkeypatch):
    runtime, actor, owner, assistant, _ = system
    service = runtime.service
    if policy == "model":
        service.save_model_approval_policy(assistant["id"], {
            "enabled": True, "model_id": "synthetic-test", "allowed_tools": ["Read"],
            "allowed_data_scopes": ["workspace"], "allowed_operations": ["read"], "max_amount": 0,
        }, owner)
    else:
        service.save_high_risk_approval_policy(assistant["id"], {
            "enabled": True, "allowed_tools": [], "admin_allowlist": [],
        }, owner)

    async def unexpected(*_args, **_kwargs):
        pytest.fail("Policy-denied request reached knowledge retrieval")

    monkeypatch.setattr(service.knowledge, "search_for_run", unexpected)
    result = asyncio.run(runtime.bridge.invoke(payload(runtime)))
    assert result["is_error"] is True
    row = service.db.one("SELECT * FROM approvals WHERE run_id=?", (actor.current_run_id,))
    assert row["status"] == "denied"
    assert row["strategy"] == policy


@pytest.mark.parametrize("change", ["wrong_session", "unknown_tool", "old_runtime", "foreign_active_run"])
def test_harness_tool_identity_and_active_run_must_match(system, change):
    runtime, actor, *_ = system
    request = payload(runtime)
    if change == "wrong_session":
        request["sessionId"] = "another-session"
    elif change == "unknown_tool":
        request["toolName"] = "bash"
    elif change == "old_runtime":
        runtime.current_run = None
    else:
        runtime.service.db.execute("UPDATE conversations SET active_run_id='another-run' WHERE id=?", (runtime.conversation_id,))

    async def scenario():
        if change == "foreign_active_run":
            assert (await runtime.bridge.invoke(request))["is_error"] is True
        else:
            with pytest.raises(AppError):
                await runtime.bridge.invoke(request)
        assert not runtime.service.db.all("SELECT * FROM evidence_sources")

    asyncio.run(scenario())


def test_harness_actor_closes_each_turn_and_restores_only_native_harness_session(system):
    runtime, _, owner, *_ = system
    service, cid = runtime.service, runtime.conversation_id
    first_run = runtime.current_run["id"]
    service.db.execute("UPDATE runs SET status='queued' WHERE id=?", (first_run,))
    instances = []

    class SyntheticHarnessProcess:
        close_after_run = HarnessRuntime.close_after_run

        def __init__(self, _service, _cid, config, _approve, emit):
            self.config, self.emit, self.closed = config, emit, False
            instances.append(self)

        async def connect(self):
            pass

        async def execute(self, run, message):
            assert all(item.closed for item in instances[:-1])
            assert self.config["conversation"]["sdk_session_id"] is None
            service.db.execute("UPDATE conversations SET harness_session_id='durable-harness' WHERE id=?", (cid,))
            await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": "synthetic result"})

        async def close(self):
            self.closed = True

    async def completed(run_id):
        for _ in range(100):
            if service.require("runs", run_id)["status"] == "succeeded":
                return
            await asyncio.sleep(0.01)
        raise AssertionError("Synthetic run did not finish")

    async def scenario():
        manager = RunManager(service, runtime_factory=SyntheticHarnessProcess)
        service.manager = manager
        try:
            manager.submit(first_run)
            await completed(first_run)
            second, _ = service.prepare_run(cid, {"text": "second fixture task", "file_ids": [], "client_request_id": "second"}, owner)
            manager.submit(second["run_id"])
            await completed(second["run_id"])
            assert len(instances) == 2 and all(item.closed for item in instances)
            assert instances[0].config["native_session_id"] is None
            assert instances[1].config["native_session_id"] == "durable-harness"
            assert manager.actors[cid].runtime is None
        finally:
            await manager.close()

    asyncio.run(scenario())
