"""Real pinned Node Harness + local model HTTP + official FastMCP HTTP.

Opt in with HARNESS_TEST_NODE and HARNESS_TEST_SOURCE. Only synthetic localhost
services are used; no runtime, authorization, approval, or MCP call is mocked.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import PROVIDER_KEYS, Settings
from app.main import create_app
from harness_mcp_fixture import local_mcp_server, local_model_server
from test_api import PASSWORD, login, send
from test_harness_real_runtime import wait_run


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
        response = client.post("/api/v1/assistants", json={"name": "Harness MCP", "scope": "team", "connections": [{
            "connection_id": connection["id"], "allowed_tools": GRANTED, "auto_approved_tools": ["read_assets", "read_failure"]}]})
        assert response.status_code == 201, response.text
        assistant = response.json()
        response = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]})
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
    for path in private_root.rglob("*"):
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


def test_real_mcp_granted_tools_full_arguments_sources_and_failure(mcp_app):
    env = mcp_app
    read = env.run("MCP_CASE_READ", "read_assets", READ_ARGUMENTS)
    completed(env, read)
    names = {tool["name"] for tool in env.model.requests[0]["tools"]}
    # This assistant has no knowledge collection binding, so no knowledge
    # retrieval tools may be advertised alongside its explicit MCP grants.
    assert names == {"generate_xlsx", "generate_image", "file_read", "file_write", "file_search", "memory_list", "memory_read", "memory_write", *(env.alias(name) for name in GRANTED)}
    assert env.alias("hidden_admin") not in names
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
