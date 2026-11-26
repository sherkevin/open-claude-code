"""Real pinned Node Harness + local model HTTP + official FastMCP HTTP.

Opt in with HARNESS_TEST_NODE and HARNESS_TEST_SOURCE. Only synthetic localhost
services are used; no runtime, authorization, approval, or MCP call is mocked.
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import PROVIDER_KEYS, Settings
from app.main import create_app
from harness_mcp_fixture import local_mcp_server, local_model_server
from test_api import PASSWORD, login, send
from test_harness_real_runtime import child_request, wait_run


pytestmark = pytest.mark.skipif(
    not os.getenv("HARNESS_TEST_NODE") or not os.getenv("HARNESS_TEST_SOURCE"),
    reason="requires explicitly installed pinned Harness runtime",
)

GRANTED = ["read_assets", "read_failure", "update_asset", "delete_asset"]
READ_ARGUMENTS = {"query": "泵 R-17 \"压力\"", "filters": {
    "asset_ids": ["R-17", "R-18"], "range": {"min": 0, "max": 7.5},
    "include_inactive": False, "metadata": None}, "limit": 2}
WRITE_ARGUMENTS = {"record_id": "R-17", "changes": {"pressure": 6.25,
    "labels": ["试验", "approved"], "enabled": False, "comment": None},
    "options": {"expected_version": 4, "audit": {"reason": "local synthetic test"}}}


@pytest.fixture
def mcp_app(tmp_path, monkeypatch, local_model_server, local_mcp_server, record_property):
    model, mcp = local_model_server, local_mcp_server
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{
        "id": "fixture", "label": "本地 MCP 测试模型", "upstream_model": "fixture",
        "reasoning_levels": {"default": None}, "provider_env": {
            "ANTHROPIC_API_KEY": "synthetic-model-only", "ANTHROPIC_BASE_URL": model.url}}]}))
    settings = Settings(data_dir=tmp_path / "data", runtime_type="harness",
        harness_node_path=os.environ["HARNESS_TEST_NODE"], harness_source_dir=os.environ["HARNESS_TEST_SOURCE"],
        model_catalog=str(catalog), provider_settings="", provider_test_mode=True, default_model_id="",
        origins=("http://testserver",), mcp_hosts=("127.0.0.1",), approval_seconds=60,
        model_router_enabled=False, knowledge_vision_enabled=False, image_generation_enabled=False)
    app = create_app(settings)
    from app.harness_runtime import HarnessRuntime
    original_connect, processes, run_ids = HarnessRuntime.connect, [], []

    async def observe_process(runtime):
        await original_connect(runtime)
        process = runtime.client._proc
        processes.append(process)
        # Inspect only this test's child, without printing any environment.
        assert mcp.token.encode() not in Path(f"/proc/{process.pid}/environ").read_bytes()
        assert Path(f"/proc/{process.pid}/exe").resolve() == Path(settings.harness_node_path).resolve()

    monkeypatch.setattr(HarnessRuntime, "connect", observe_process)
    with TestClient(app) as client:
        service = app.state.service
        owner = service.create_user({"username": "admin", "display_name": "申请人", "password": PASSWORD, "role": "admin"})
        login(client)
        response = client.post("/api/v1/connections", json={"name": "Synthetic business MCP", "scope": "team",
            "url": mcp.url, "auth_type": "bearer", "credential": mcp.token})
        assert response.status_code == 201, response.text
        connection = response.json()
        discovery = client.post(f"/api/v1/connections/{connection['id']}/test")
        assert discovery.status_code == 200 and discovery.json()["status"] == "connected", discovery.text
        assert {item["name"] for item in discovery.json()["tools"]} == {*GRANTED, "hidden_admin"}
        assert not mcp.calls, "discovery must not execute business tools"
        reviewed = {tool["name"]: {"connection_revision": discovery.json()["revision"],
                    "definition_digest": tool["definition_digest"], "basis": "Reviewed synthetic read-only fixture implementation"}
                    for tool in discovery.json()["tools"] if tool["name"] in ("read_assets", "read_failure")}
        assert client.post(f"/api/v1/connections/{connection['id']}/read-only-review", json={
            "expected_connection_revision": discovery.json()["revision"], "expected_review_revision": 0,
            "tools": reviewed}).status_code == 200
        response = client.post("/api/v1/assistants", json={"name": "Harness MCP", "scope": "team", "connections": [{
            "connection_id": connection["id"], "allowed_tools": GRANTED, "auto_approved_tools": ["read_assets", "read_failure"]}]})
        assert response.status_code == 201, response.text
        assistant = response.json()
        response = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"], "permission_mode": "auto"})
        assert response.status_code == 201, response.text
        cid = response.json()["id"]

        def canonical(raw_name):
            return f"mcp__c{connection['id']}__{raw_name}"

        def alias(raw_name):
            return "external_" + hashlib.sha256(canonical(raw_name).encode()).hexdigest()[:32]

        def run(case, tool, arguments):
            model.actions[case] = (alias(tool), arguments)
            run_id = send(client, cid, case, f"real-mcp-{time.monotonic_ns()}")["run_id"]
            run_ids.append(run_id)
            return run_id

        yield SimpleNamespace(client=client, service=service, owner=owner, connection=connection, assistant=assistant,
            cid=cid, run=run, alias=alias, canonical=canonical, mcp=mcp, model=model, settings=settings)
        for run_id in run_ids:
            assert mcp.token not in json.dumps(service.db.events(run_id, 0), ensure_ascii=False)

    assert processes and len({p.pid for p in processes}) == len(processes)
    assert all(p.poll() is not None for p in processes), "real Harness child leaked"
    assert mcp.authenticated and all(mcp.authenticated), "MCP must receive the synthetic bearer only from the host"
    assert mcp.token not in json.dumps(model.requests, ensure_ascii=False)
    private_root = settings.data_dir / "harness"
    assert private_root.exists()
    child_roots = (settings.data_dir / "workspaces" / cid / "subagents").glob("*/runtime")
    for root in [private_root, *child_roots]:
        for path in root.rglob("*"):
            if path.is_file():
                assert mcp.token.encode() not in path.read_bytes(), f"MCP credential leaked into Node file {path.name}"
    record_property("real_harness_processes", len(processes))
    record_property("local_synthetic_model_requests", len(model.requests))
    record_property("real_local_mcp_tool_calls", len(mcp.calls))
    record_property("paid_model_requests", 0)


def tool_events(env, run_id, kind):
    return [event["data"] for event in env.service.db.events(run_id, 0) if event["type"] == kind]


def completed(env, run_id):
    result = wait_run(env.client, run_id)
    assert result["status"] == "succeeded", result
    return result


def approval(env, run_id, name):
    pending = wait_run(env.client, run_id, {"awaiting_approval", "succeeded"})
    assert pending["status"] == "awaiting_approval", pending
    row = pending["approvals"][0]
    assert row["tool_name"] == env.canonical(name)
    return row


def decide(env, row, decision):
    response = env.client.post(f"/api/v1/approvals/{row['id']}/decision", json={"decision": decision})
    assert response.status_code == 200, response.text
    return response.json()


def test_rate_limit_recovery_after_actual_mcp_write_does_not_replay(mcp_app):
    env = mcp_app
    env.model.faults["rate_limit_after_result"] = 2
    run_id = env.run("RATE_LIMIT_WRITE_CASE", "update_asset", WRITE_ARGUMENTS)
    decide(env, approval(env, run_id, "update_asset"), "allow")
    completed(env, run_id)
    assert env.mcp.calls == [{"tool": "update_asset", "arguments": WRITE_ARGUMENTS}]
    retries = tool_events(env, run_id, "model.retry")
    assert [r["retry"] for r in retries] == [1, 2]
    assert all(r["code"] == "MODEL_RATE_LIMITED" and r["max_retries"] == 3 for r in retries)
    assert len(env.model.requests) == 4
    assert all(r["messages"] == env.model.requests[1]["messages"] for r in env.model.requests[2:])
    assert len(tool_events(env, run_id, "tool.dispatched")) == 1


def test_recovery_after_successful_write_does_not_replay(mcp_app):
    env = mcp_app
    env.model.faults["disconnect_after_result"] = True
    text = "RECOVERY_WRITE_CASE"
    first = env.run(text, "update_asset", WRITE_ARGUMENTS)
    decide(env, approval(env, first, "update_asset"), "allow")
    failed = wait_run(env.client, first)
    assert failed["status"] == "failed", failed
    assert failed["recovery"]["mode"] == "answer_only"
    assert failed["recovery"]["operations"] == [{"tool_name": env.canonical("update_asset"), "state": "completed"}]
    assert not tool_events(env, first, "model.retry")
    original = env.service.require("runs", first)
    assert send(env.client, env.cid, text, original["client_request_id"])["run_id"] == first
    endpoint = f"/api/v1/conversations/{env.cid}/messages"
    legacy = env.client.post(endpoint, json={"text": text, "client_request_id": "legacy-retry"})
    assert legacy.status_code == 409 and legacy.json()["error"]["code"] == "RECOVERY_REQUIRED"
    recovery = env.client.post(endpoint, json={"text": text, "client_request_id": "resume-without-tools", "recovery_run_id": first})
    assert recovery.status_code == 202, recovery.text
    resumed = recovery.json()["run_id"]
    completed(env, resumed)
    assert not env.model.requests[-1].get("tools")
    native_messages = env.model.requests[-1]["messages"]
    assert "completed" in json.dumps(native_messages, ensure_ascii=False)
    assert any(block.get("type") == "tool_result" and "R-17" in json.dumps(block) for item in native_messages
               for block in item["content"] if isinstance(item["content"], list))
    assert env.mcp.calls == [{"tool": "update_asset", "arguments": WRITE_ARGUMENTS}]
    assert not tool_events(env, resumed, "tool.dispatched")


def test_recovery_after_automatic_workspace_write_does_not_replay(mcp_app):
    env = mcp_app
    response = env.client.patch(f"/api/v1/conversations/{env.cid}", json={"permission_mode": "full"})
    assert response.status_code == 200, response.text
    env.model.faults["disconnect_after_result"] = True
    text = "RECOVERY_AUTOMATIC_WORKSPACE_WRITE"
    env.model.actions[text] = ("generate_xlsx", {"filename": "once.xlsx", "sheets": [{"name": "test", "rows": [["once"]]}]})
    run = send(env.client, env.cid, text, "recovery-local-write-only")['run_id']
    failed = wait_run(env.client, run)
    assert failed['status'] == 'failed', failed
    assert failed['recovery']['mode'] == 'answer_only'
    assert failed['recovery']['operations'] == [{'tool_name': 'mcp__workspace__generate_xlsx', 'state': 'completed'}]
    assert tool_events(env, run, 'approval.session_decided')
    assert not env.service.db.all('SELECT id FROM approvals WHERE run_id=?', (run,))
    response = env.client.post(f"/api/v1/conversations/{env.cid}/messages", json={
        "client_request_id": "answer-after-automatic-write", "recovery_run_id": run,
    })
    assert response.status_code == 202, response.text
    restored = response.json()['run_id']
    completed(env, restored)
    assert not tool_events(env, restored, 'tool.dispatched')
    assert not env.model.requests[-1].get('tools')
    artifacts = env.settings.data_dir / 'workspaces' / env.cid / 'artifacts'
    assert len(list(artifacts.rglob('*.xlsx'))) == 1


def test_context_overflow_does_not_retry_after_an_external_write(mcp_app):
    env = mcp_app
    historical = "PREVIOUS_HISTORY The P-900 pressure limit is 7 bar. Keep this earlier context. " * 200
    completed(env, send(env.client, env.cid, historical, "prior-compaction-material")['run_id'])
    env.model.faults["overflow_after_result"] = True
    run = env.run("OVERFLOW_AFTER_COMMITTED_MCP_WRITE", "update_asset", WRITE_ARGUMENTS)
    decide(env, approval(env, run, "update_asset"), "allow")
    result = wait_run(env.client, run)
    assert result['status'] == 'failed' and result['error_code'] == 'MODEL_CONTEXT_LIMIT', result
    assert result['compaction'] is None and len(env.model.requests) == 3
    assert not tool_events(env, run, 'context.compaction_result')
    assert len(env.mcp.calls) == 1


def test_real_mcp_granted_tools_full_arguments_sources_and_failure(mcp_app):
    env = mcp_app
    read = env.run("MCP_CASE_READ", "read_assets", READ_ARGUMENTS)
    completed(env, read)
    definitions = {tool["name"]: tool for tool in env.model.requests[0]["tools"]}
    names = set(definitions)
    # This assistant has no knowledge collection binding, so no knowledge
    # retrieval tools may be advertised alongside its explicit MCP grants.
    assert names == {"generate_xlsx", "generate_image", "file_read", "file_write", "file_search", "memory_list", "memory_read", "memory_write",
                     "ask_user", "review_plan", "delegate_tasks", "schedule_create", "schedule_list", "schedule_view", "schedule_edit", *(env.alias(name) for name in GRANTED)}
    assert env.alias("hidden_admin") not in names
    for name in GRANTED:
        provider_description = definitions[env.alias(name)]["description"]
        assert env.connection["name"] in provider_description and name in provider_description
        assert env.mcp.url not in provider_description and env.mcp.token not in provider_description
    assert env.mcp.calls == [{"tool": "read_assets", "arguments": READ_ARGUMENTS}]
    assert not env.service.db.all("SELECT id FROM approvals WHERE run_id=?", (read,))
    started = tool_events(env, read, "tool.started")
    assert len(started) == 1 and started[0]["tool_name"] == env.canonical("read_assets")
    assert started[0]["arguments"] == READ_ARGUMENTS
    sources = env.service.db.all("SELECT * FROM evidence_sources WHERE run_id=?", (read,))
    assert len(sources) == 1 and sources[0]["resource_id"] == env.connection["id"]
    assert sources[0]["tool_name"] in {"read_assets", env.canonical("read_assets")}
    assert json.loads(sources[0]["locator"])["record_keys"] == ["R-17"]
    assert env.client.get(f"/api/v1/sources/{sources[0]['id']}").status_code == 200
    message = env.service.require("messages", env.service.require("runs", read)["assistant_message_id"])
    assert sources[0]["id"] in json.dumps(json.loads(message["citations"]))
    assert tool_events(env, read, "tool.completed")[0]["source_ids"] == [sources[0]["id"]]

    failed = env.run("MCP_CASE_FAILURE", "read_failure", {"query": "synthetic MCP failure"})
    completed(env, failed)
    assert [call["tool"] for call in env.mcp.calls] == ["read_assets", "read_failure"]
    assert not env.service.db.all("SELECT id FROM evidence_sources WHERE run_id=?", (failed,))
    outcome = tool_events(env, failed, "tool.completed")[0]
    assert outcome["success"] is False and outcome["source_ids"] == []
    answer = env.service.require("messages", env.service.require("runs", failed)["assistant_message_id"])
    assert not json.loads(answer["citations"]) and "[[source:" not in answer["text"]

    ungranted = env.run("MCP_CASE_UNGRANTED", "hidden_admin", {"operation": "ungranted"})
    completed(env, ungranted)
    assert len(env.mcp.calls) == 2
    assert not env.service.db.all("SELECT id FROM evidence_sources WHERE run_id=?", (ungranted,))


def test_real_mcp_write_requires_exact_human_allow_and_deny(mcp_app):
    env = mcp_app
    denied = env.run("MCP_CASE_WRITE_DENY", "update_asset", WRITE_ARGUMENTS)
    row = approval(env, denied, "update_asset")
    assert row["arguments"] == WRITE_ARGUMENTS and not env.mcp.calls
    decide(env, row, "deny")
    completed(env, denied)
    assert not env.mcp.calls
    assert tool_events(env, denied, "tool.completed")[0]["success"] is False

    allowed = env.run("MCP_CASE_WRITE_ALLOW", "update_asset", WRITE_ARGUMENTS)
    row = approval(env, allowed, "update_asset")
    assert row["arguments"] == WRITE_ARGUMENTS and not env.mcp.calls
    decide(env, row, "allow")
    completed(env, allowed)
    assert env.mcp.calls == [{"tool": "update_asset", "arguments": WRITE_ARGUMENTS}]
    persisted = env.service.require("approvals", row["id"])
    assert persisted["decided_by"] == env.owner["id"] and persisted["decision_source"] == "user"
    assert json.loads(persisted["arguments"]) == WRITE_ARGUMENTS
    assert tool_events(env, allowed, "tool.completed")[0]["success"] is True


def test_real_mcp_two_distinct_nonrequester_admins_execute_once(mcp_app):
    env = mcp_app
    approvers = [env.service.create_user({"username": name, "display_name": name, "password": PASSWORD, "role": "admin"})
                 for name in ("approver1", "approver2")]
    policy = env.client.put(f"/api/v1/assistants/{env.assistant['id']}/high-risk-approval", json={
        "enabled": True, "allowed_tools": [env.canonical("delete_asset")],
        "admin_allowlist": [user["id"] for user in approvers]})
    assert policy.status_code == 200, policy.text
    arguments = {"record_id": "R-17", "operation": "delete"}
    run_id = env.run("MCP_CASE_DUAL", "delete_asset", arguments)
    row = approval(env, run_id, "delete_asset")
    assert row["approval_mode"] == "dual" and row["required_approvals"] == 2
    assert not env.mcp.calls
    own_vote = env.client.post(f"/api/v1/approvals/{row['id']}/decision", json={"decision": "allow"})
    assert own_vote.status_code == 403
    env.client.post("/api/v1/auth/logout")
    login(env.client, "approver1")
    assert decide(env, row, "allow")["status"] == "pending"
    assert not env.mcp.calls
    assert env.client.post(f"/api/v1/approvals/{row['id']}/decision", json={"decision": "allow"}).status_code == 409
    env.client.post("/api/v1/auth/logout")
    login(env.client, "approver2")
    assert decide(env, row, "allow")["status"] == "allowed"
    env.client.post("/api/v1/auth/logout")
    login(env.client)
    completed(env, run_id)
    assert env.mcp.calls == [{"tool": "delete_asset", "arguments": arguments}]
    votes = env.service.db.all("SELECT voter_id FROM approval_votes WHERE approval_id=?", (row["id"],))
    assert {vote["voter_id"] for vote in votes} == {user["id"] for user in approvers}
    assert env.service.require("approvals", row["id"])["decision_source"] == "dual_admin"


def test_real_mcp_cancel_and_revocation_prevent_dispatch(mcp_app):
    env = mcp_app
    cancelled = env.run("MCP_CASE_CANCEL", "update_asset", WRITE_ARGUMENTS)
    row = approval(env, cancelled, "update_asset")
    assert not env.mcp.calls
    assert env.client.post(f"/api/v1/runs/{cancelled}/cancel").status_code == 200
    assert wait_run(env.client, cancelled)["status"] == "cancelled"
    assert env.client.post(f"/api/v1/approvals/{row['id']}/decision", json={"decision": "allow"}).status_code == 409
    assert not env.mcp.calls

    revoked = env.run("MCP_CASE_REVOKE_CONNECTION", "update_asset", WRITE_ARGUMENTS)
    row = approval(env, revoked, "update_asset")
    assert not env.mcp.calls
    response = env.client.patch(f"/api/v1/connections/{env.connection['id']}", json={"enabled": False})
    assert response.status_code == 200, response.text
    decide(env, row, "allow")
    completed(env, revoked)
    assert not env.mcp.calls
    assert tool_events(env, revoked, "tool.completed")[0]["success"] is False
    assert not env.service.db.all("SELECT id FROM evidence_sources WHERE run_id=?", (revoked,))

    assert env.client.patch(f"/api/v1/connections/{env.connection['id']}", json={"enabled": True}).status_code == 200
    revoked_grant = env.run("MCP_CASE_REVOKE_GRANT", "update_asset", WRITE_ARGUMENTS)
    row = approval(env, revoked_grant, "update_asset")
    response = env.client.patch(f"/api/v1/assistants/{env.assistant['id']}", json={"connections": [{
        "connection_id": env.connection["id"], "allowed_tools": ["read_assets"], "auto_approved_tools": ["read_assets"]}]})
    assert response.status_code == 200, response.text
    decide(env, row, "allow")
    completed(env, revoked_grant)
    assert not env.mcp.calls
    assert tool_events(env, revoked_grant, "tool.completed")[0]["success"] is False


@pytest.mark.parametrize("dispatch", ["automatic", "manual"])
def test_real_harness_subagents_inherit_effective_mcp_permissions(mcp_app, dispatch):
    from app.read_only_tools import definition_digest

    env = mcp_app
    if dispatch == "manual":
        connection = env.service.require("connections", env.connection["id"])
        definition = next(tool for tool in json.loads(connection["tools"]) if tool["name"] == "read_assets")
        assistant = env.client.patch(f"/api/v1/assistants/{env.assistant['id']}", json={"connections": [{
            "connection_id": connection["id"], "allowed_tools": GRANTED, "auto_approved_tools": [],
            "read_only_confirmations": {"read_assets": {"connection_revision": connection["revision"],
                "definition_digest": definition_digest(definition), "basis": "Synthetic query-only test fixture"}},
        }]})
        assert assistant.status_code == 200, assistant.text
        mode = env.client.patch(f"/api/v1/conversations/{env.cid}", json={"permission_mode": "auto"})
        assert mode.status_code == 200, mode.text

    task_read, task_write, task_deny, task_hidden = (
        "CHILD_REAL_READ", "CHILD_REAL_WRITE", "CHILD_REAL_DENIED", "CHILD_REAL_HIDDEN",
    )
    tasks = [task_read, task_write, task_deny, task_hidden]
    env.model.actions.update({
        task_read: (env.alias("read_assets"), READ_ARGUMENTS),
        task_write: (env.alias("update_asset"), WRITE_ARGUMENTS),
        task_deny: (env.alias("update_asset"), {**WRITE_ARGUMENTS, "record_id": "DENY-THIS-RECORD"}),
        task_hidden: (env.alias("hidden_admin"), {"operation": "must never execute"}),
    })

    parent_marker = "PARENT_REAL_MCP_CHILDREN"
    release = threading.Event()
    env.model.actions[parent_marker] = ("delegate_tasks", {"tasks": tasks}) if dispatch == "automatic" else release
    try:
        run = send(env.client, env.cid, parent_marker, f"delegation-{dispatch}")["run_id"]
        path = f"/api/v1/runs/{run}"
        assert wait_run(env.client, run, {"running"})["status"] == "running"
        if dispatch == "manual":
            response = env.client.post(f"{path}/subagents", json={"mode": "harness", "tasks": tasks})
            assert response.status_code == 202, response.text
            # The model may submit its final response while the two children
            # still need an approval. The owning run must stay alive.
            release.set()

        decisions = {}
        deadline = time.monotonic() + 45
        while len(decisions) < 2 and time.monotonic() < deadline:
            info = env.client.get(path).json()
            assert info["status"] == "running", info
            plan = env.client.get(f"{path}/subagents").json()
            child_ids = [item["child_id"] for item in plan["children"]]
            for row in info["approvals"]:
                if row["id"] in decisions:
                    continue
                assert row["tool_name"] == env.canonical("update_asset")
                child_id = row["subagent_child_id"]
                assert child_id in child_ids[1:3]
                should_allow = child_id == child_ids[1]
                assert row["arguments"]["record_id"] == ("R-17" if should_allow else "DENY-THIS-RECORD")
                assert not any(call["arguments"].get("record_id") == "DENY-THIS-RECORD" for call in env.mcp.calls)
                if should_allow:
                    assert not any(call["tool"] == "update_asset" for call in env.mcp.calls)
                decisions[row["id"]] = decide(env, row, "allow" if should_allow else "deny")["status"]
            time.sleep(.05)
        assert set(decisions.values()) == {"allowed", "denied"}
        completed(env, run)
    finally:
        release.set()

    assert sorted(call["tool"] for call in env.mcp.calls) == ["read_assets", "update_asset"]
    assert {"tool": "update_asset", "arguments": WRITE_ARGUMENTS} in env.mcp.calls
    plan = env.client.get(f"{path}/subagents").json()
    child_ids = [item["child_id"] for item in plan["children"]]
    assert plan["counts"]["succeeded"] == len(tasks)
    worker_requests = [body for body in env.model.requests if child_request(body)]
    parent_tools = {tool["name"] for tool in env.model.requests[0]["tools"]}
    assert len(worker_requests) == 2 * len(tasks)
    assert all({tool["name"] for tool in body["tools"]} == parent_tools - {"delegate_tasks"} for body in worker_requests)
    assert all(parent_marker not in json.dumps(body["messages"]) for body in worker_requests)
    trace = env.client.get(f"{path}/trace").json()
    calls = [event["data"] for event in trace["events"] if event["type"] == "tool.completed"
             and event["data"].get("subagent_child_id")]
    assert {item["subagent_child_id"]: item["success"] for item in calls} == dict(zip(child_ids[:3], [True, True, False]))
    assert all("arguments" not in item and "error_message" not in item for item in calls)
    resolved = [event["data"] for event in trace["events"] if event["type"] == "subagent.approval.resolved"]
    assert {(item["child_id"], item["decision"]) for item in resolved} == {(child_ids[1], "allow"), (child_ids[2], "deny")}
    if dispatch == "manual":
        decision = tool_events(env, run, "approval.session_decided")
        assert [(item["subagent_child_id"], item["mode"], item["scope"]) for item in decision] == [
            (child_ids[0], "auto", "certified_read_only_connection"),
        ]


def test_real_harness_subagent_full_mode_stays_in_own_conversation(mcp_app):
    from app.file_tools import WorkspaceFiles

    env = mcp_app
    other = env.service.create_user({"username": "other-user", "display_name": "Other", "password": PASSWORD, "role": "member"})
    foreign = env.service.create_conversation({"assistant_id": env.assistant["id"]}, other)
    WorkspaceFiles(env.service, foreign["id"], other["id"]).write(
        {"path": "private.txt", "content": "FOREIGN_PRIVATE_SENTINEL", "expected_revision": 0})
    foreign_path = env.settings.data_dir / "workspaces" / foreign["id"] / "workbench" / "private.txt"

    setting = env.client.patch(f"/api/v1/conversations/{env.cid}", json={"permission_mode": "full"})
    assert setting.status_code == 200, setting.text
    env.model.actions.update({
        "PARENT_FULL_SCOPE": ("delegate_tasks", {"tasks": ["CHILD_FULL_WRITE", "CHILD_FOREIGN_READ", "CHILD_NESTED_CALL"]}),
        "CHILD_FULL_WRITE": ("file_write", {"path": "child.txt", "content": "scoped result", "expected_revision": 0}),
        "CHILD_FOREIGN_READ": ("file_read", {"path": str(foreign_path)}),
        "CHILD_NESTED_CALL": ("delegate_tasks", {"tasks": ["nested one", "nested two"]}),
    })
    run = send(env.client, env.cid, "PARENT_FULL_SCOPE", "delegation-full-scope")["run_id"]
    completed(env, run)
    assert (env.settings.data_dir / "workspaces" / env.cid / "workbench" / "child.txt").read_text() == "scoped result"
    assert "FOREIGN_PRIVATE_SENTINEL" not in json.dumps(env.model.requests)
    assert not env.service.db.all("SELECT * FROM approvals WHERE run_id=?", (run,))
    plan = env.client.get(f"/api/v1/runs/{run}/subagents").json()
    written, foreign, _nested = [item["child_id"] for item in plan["children"]]
    calls = {item["subagent_child_id"]: item for item in tool_events(env, run, "tool.completed")
             if "subagent_child_id" in item}
    assert calls[written]["success"] is True and calls[foreign]["error_code"] == "PATH_DENIED"
    assert set(calls) == {written, foreign}
    receipts = tool_events(env, run, "approval.session_decided")
    assert [(item["subagent_child_id"], item["mode"]) for item in receipts] == [(written, "full")]
    assert not env.service.db.all("SELECT child_id FROM subagent_children WHERE parent_run_id IN (?, ?, ?)",
                                  tuple(item["child_id"] for item in plan["children"]))
    env.client.post("/api/v1/auth/logout")
    login(env.client, "other-user")
    assert env.client.get(f"/api/v1/runs/{run}/subagents").status_code == 404
    assert env.client.get(f"/api/v1/runs/{run}/trace").status_code == 404


def test_real_harness_subagent_inherits_dual_approval(mcp_app):
    env = mcp_app
    approvers = [env.service.create_user({"username": name, "display_name": name, "password": PASSWORD, "role": "admin"})
                 for name in ("childapprover1", "childapprover2")]
    tool = env.canonical("delete_asset")
    policy = env.client.put(f"/api/v1/assistants/{env.assistant['id']}/high-risk-approval", json={
        "enabled": True, "allowed_tools": [tool], "admin_allowlist": [user["id"] for user in approvers],
    })
    assert policy.status_code == 200, policy.text
    arguments = {"record_id": "R-17", "operation": "delete"}
    release = threading.Event()
    env.model.actions.update({
        "PARENT_DUAL_CHILD": release,
        "CHILD_DUAL_CALL": (env.alias("delete_asset"), arguments),
    })
    run = send(env.client, env.cid, "PARENT_DUAL_CHILD", "delegation-dual")["run_id"]
    try:
        assert wait_run(env.client, run, {"running"})["status"] == "running"
        response = env.client.post(f"/api/v1/runs/{run}/subagents", json={"mode": "harness", "tasks": ["CHILD_DUAL_CALL"]})
        assert response.status_code == 202, response.text
    finally:
        release.set()
    deadline = time.monotonic() + 40
    row = None
    while row is None and time.monotonic() < deadline:
        info = env.client.get(f"/api/v1/runs/{run}").json()
        assert info["status"] in {"queued", "running"}, info
        row = next(iter(info["approvals"]), None)
        if not row:
            time.sleep(.05)
    assert row and row["approval_mode"] == "dual" and row["subagent_child_id"]
    assert not env.mcp.calls
    assert env.client.post(f"/api/v1/approvals/{row['id']}/decision", json={"decision": "allow"}).status_code == 403
    env.client.post("/api/v1/auth/logout")
    login(env.client, "childapprover1")
    assert env.client.get(f"/api/v1/runs/{run}/trace").status_code == 404
    assert decide(env, row, "allow")["status"] == "pending"
    assert not env.mcp.calls
    env.client.post("/api/v1/auth/logout")
    login(env.client, "childapprover2")
    assert decide(env, row, "allow")["status"] == "allowed"
    env.client.post("/api/v1/auth/logout")
    login(env.client)
    completed(env, run)
    assert env.mcp.calls == [{"tool": "delete_asset", "arguments": arguments}]
    resolved = tool_events(env, run, "subagent.approval.resolved")
    assert [(item["child_id"], item["decision_source"]) for item in resolved] == [(row["subagent_child_id"], "dual_admin")]


def test_real_harness_subagent_memory_requires_full_human_review(mcp_app):
    from app.harness_memory import MEMORY_WRITE

    env = mcp_app
    content = "CHILD_PRIVATE_MEMORY_BODY"
    revision = env.service.memory.revision(env.owner["id"], env.cid)["revision"]
    env.model.actions.update({
        "PARENT_CHILD_MEMORY": ("delegate_tasks", {"tasks": ["CHILD_MEMORY_CALL", "Just return a short note"]}),
        "CHILD_MEMORY_CALL": ("memory_write", {"path": "child.md", "content": content, "expected_revision": revision}),
    })
    run = send(env.client, env.cid, "PARENT_CHILD_MEMORY", "delegation-memory")["run_id"]
    deadline = time.monotonic() + 40
    row = None
    while row is None and time.monotonic() < deadline:
        info = env.client.get(f"/api/v1/runs/{run}").json()
        assert info["status"] in {"queued", "running"}, info
        row = next(iter(info["approvals"]), None)
        if not row:
            time.sleep(.05)
    assert row and row["tool_name"] == MEMORY_WRITE and row["subagent_child_id"]
    assert content not in json.dumps(row) + env.client.get(f"/api/v1/runs/{run}/trace").text
    endpoint = f"/api/v1/approvals/{row['id']}"
    assert env.client.post(f"{endpoint}/decision", json={"decision": "allow"}).status_code == 409
    review = env.client.get(f"{endpoint}/memory-content").json()
    assert review["content"] == content
    response = env.client.post(f"{endpoint}/decision", json={
        "decision": "allow", "memory_content_digest": review["content_digest"],
    })
    assert response.status_code == 200, response.text
    completed(env, run)
    assert env.service.memory.read(env.owner, env.cid, "child.md")["content"] == content
    assert tool_events(env, run, "memory.pending")[0]["subagent_child_id"] == row["subagent_child_id"]
    assert tool_events(env, run, "memory.updated")


def test_real_harness_subagent_cannot_dispatch_after_grant_revocation(mcp_app):
    env = mcp_app
    env.model.actions.update({
        "PARENT_REVOKE_SUBAGENT": ("delegate_tasks", {"tasks": ["CHILD_CHANGE_ASSET", "Just return the note"]}),
        "CHILD_CHANGE_ASSET": (env.alias("update_asset"), WRITE_ARGUMENTS),
    })
    run = send(env.client, env.cid, "PARENT_REVOKE_SUBAGENT", "delegation-revoke")["run_id"]
    deadline = time.monotonic() + 40
    row = None
    while row is None and time.monotonic() < deadline:
        info = env.client.get(f"/api/v1/runs/{run}").json()
        assert info["status"] in {"queued", "running"}, info
        row = next(iter(info["approvals"]), None)
        if not row:
            time.sleep(.05)
    assert row and row["subagent_child_id"]
    revoked = env.client.patch(f"/api/v1/assistants/{env.assistant['id']}", json={"connections": [{
        "connection_id": env.connection["id"], "allowed_tools": ["read_assets"], "auto_approved_tools": ["read_assets"],
    }]})
    assert revoked.status_code == 200, revoked.text
    decide(env, row, "allow")
    completed(env, run)
    assert not env.mcp.calls
    assert env.service.require("approvals", row["id"])["status"] == "denied"
    assert not any(item.get("subagent_child_id") == row["subagent_child_id"] for item in tool_events(env, run, "tool.dispatched"))


@pytest.mark.parametrize("trigger",["timer","manual"])
@pytest.mark.parametrize("kind",["direct","child","write"])
def test_real_scheduled_agent_executes_with_existing_approval_boundary(mcp_app,kind,trigger):
    from datetime import datetime,timedelta,timezone
    env=mcp_app
    marker="SCHEDULED_AGENT_"+kind.upper()
    if kind=="child":
        task="SCHEDULED_CHILD_READ"
        env.model.actions[marker]=("delegate_tasks",{"tasks":[task,"SCHEDULED_CHILD_INDEPENDENT_SUMMARY"]})
        env.model.actions[task]=(env.alias("read_assets"),READ_ARGUMENTS)
    elif kind=="write":
        env.model.actions[marker]=(env.alias("update_asset"),WRITE_ARGUMENTS)
    else:
        env.model.actions[marker]=(env.alias("read_assets"),READ_ARGUMENTS)
    stamp=(datetime.now(timezone.utc)+timedelta(seconds=7200 if trigger=="manual" else 2)).isoformat()
    route=f"/api/v1/conversations/{env.cid}/schedules"
    response=env.client.post(route,json={"text":marker,"scheduled_at":stamp,"execution_mode":"agent"})
    assert response.status_code==201,response.text
    identity=response.json()["id"]
    if trigger=="manual":
        row=next(x for x in env.client.get(route).json() if x["id"]==identity)
        body={"request_id":"manual-agent-fixture","expected_token":row["edit_token"]}
        accepted=env.client.post(route+"/"+identity+"/run-now",json=body)
        assert accepted.status_code==202,accepted.text
        assert accepted.json()["occurrence_id"]==identity
        assert env.client.post(route+"/"+identity+"/run-now",json=body).status_code==202
    deadline=time.monotonic()+30
    run=None
    while time.monotonic()<deadline:
        row=env.service.db.one("SELECT id FROM runs WHERE conversation_id=? AND client_request_id=?",(env.cid,identity))
        if row:
            run=row["id"]
            break
        time.sleep(.05)
    assert run,"Scheduled task did not dispatch"
    if kind=="write":
        pending=approval(env,run,"update_asset")
        assert not env.mcp.calls
        decide(env,pending,"deny")
        completed(env,run)
        assert not env.mcp.calls
    else:
        completed(env,run)
        assert [call["tool"] for call in env.mcp.calls]==["read_assets"]
        assert not env.service.db.one("SELECT id FROM approvals WHERE run_id=?",(run,))
        if kind=="child":
            plan=env.client.get(f"/api/v1/runs/{run}/subagents").json()
            assert plan["counts"]["succeeded"]==2
    meta=json.loads(env.service.require("runs",run)["execution_meta"])
    assert meta["schedule_id"]==identity and meta["schedule_execution_mode"]=="agent"
    assert env.service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=? AND client_request_id=?",(env.cid,identity))["n"]==1
