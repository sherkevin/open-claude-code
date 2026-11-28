"""Actual pinned Node Harness + real FastAPI approval + official loopback MCP.

Only synthetic loopback services are used. A normal private member owns the
connection, assistant and conversation; approval is decided through the real
REST route. The Harness runtime executes a single ``query`` tool call whose
tool-reported COUNT / COUNT_DISTINCT cells are projected by the host receipt.
No customer account, production service, paid model or non-loopback host is
contacted. Opt in with HARNESS_TEST_NODE and HARNESS_TEST_SOURCE; the module is
otherwise skipped because the pinned Harness runtime is absent.

No authorization, bridge, dispatch, approval or result callback is patched:
the test observes the app's persisted events and the loopback fixture's calls.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.config import PROVIDER_KEYS, Settings
from app.main import create_app
from harness_mcp_fixture import local_model_server  # noqa: F401 - pytest fixture
from test_api import PASSWORD, login, send
from test_harness_real_runtime import wait_run
from test_huawi_aggregate_runtime import (  # noqa: F401 - shared synthetic contract
    COUNT_QUERY, aggregate_payload)

pytestmark = pytest.mark.skipif(
    not os.getenv("HARNESS_TEST_NODE") or not os.getenv("HARNESS_TEST_SOURCE"),
    reason="requires explicitly installed pinned Harness runtime "
           "(set HARNESS_TEST_NODE and HARNESS_TEST_SOURCE)",
)

# The tool signature makes the optional fields explicit on the wire so the
# approval snapshot, the MCP call and the tool.started event are byte-for-byte
# comparable. Empty filters/group/order lists are the only extension of the
# shared COUNT_QUERY constant and remain valid for the strict receipt.
QUERY_ARGUMENTS = {**copy.deepcopy(COUNT_QUERY),
                   "filters": [], "group_by": [], "order_by": []}

CASES = {
    "allow": aggregate_payload(data=((17, 5),)),
    "zero": aggregate_payload(data=((0, 0),)),
    "empty": aggregate_payload(data=()),
    # Never returned: the approval is denied before dispatch.
    "deny": aggregate_payload(data=((17, 5),)),
}


def expected_receipt(count, kinds):
    return {
        "dataset": "synthetic_table",
        "receipt_kind": "tool_reported_count_cells",
        "returned_aggregate_rows": 1,
        "page_size": 20,
        "scope_verified": False,
        "counts": [
            {"alias": "cnt", "field": "*", "aggregate": "count", "value": count},
            {"alias": "kinds", "field": "item", "aggregate": "count_distinct",
             "value": kinds},
        ],
    }


@pytest.fixture
def aggregate_mcp_server():
    """Official FastMCP over real HTTP; only a synthetic bearer is accepted."""
    calls, authenticated = [], []
    token = "synthetic-huawi-aggregate-loopback-bearer"
    state = SimpleNamespace(payload=None)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    mcp = FastMCP("huawi-aggregate-fixture", host="127.0.0.1", port=port,
                  stateless_http=True)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True,
                                          destructiveHint=False))
    def query(dataset: str, select: list, filters: list | None = None,
              group_by: list | None = None, order_by: list | None = None,
              page: int = 1, rows: int = 20,
              include_total: bool = True) -> dict[str, Any]:
        """Synthetic ungrouped aggregate query returning count cells."""
        calls.append({"tool": "query", "arguments": {
            "dataset": dataset, "select": copy.deepcopy(select),
            "filters": copy.deepcopy(filters), "group_by": copy.deepcopy(group_by),
            "order_by": copy.deepcopy(order_by), "page": page, "rows": rows,
            "include_total": include_total,
        }})
        return copy.deepcopy(state.payload)

    inner = mcp.streamable_http_app()

    async def authenticated_app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            valid = headers.get(b"authorization") == ("Bearer " + token).encode()
            authenticated.append(valid)
            if not valid:
                await send({"type": "http.response.start", "status": 401,
                            "headers": []})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await inner(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(
        authenticated_app, log_config=None, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                              daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            server.should_exit = True
            thread.join(5)
            sock.close()
            pytest.fail("local aggregate FastMCP service did not start")
        time.sleep(0.01)
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp", token=token,
                              calls=calls, authenticated=authenticated,
                              state=state)
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "local aggregate FastMCP service leaked"


@pytest.fixture
def aggregate_harness_app(tmp_path, monkeypatch, local_model_server,
                          aggregate_mcp_server):
    model, mcp = local_model_server, aggregate_mcp_server
    for key in PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{
        "id": "fixture", "label": "Local Harness aggregate probe",
        "upstream_model": "fixture", "reasoning_levels": {"default": None},
        "provider_env": {"ANTHROPIC_API_KEY": "synthetic-model-only",
                         "ANTHROPIC_BASE_URL": model.url}}]}))
    settings = Settings(
        data_dir=tmp_path / "data", runtime_type="harness",
        harness_node_path=os.environ["HARNESS_TEST_NODE"],
        harness_source_dir=os.environ["HARNESS_TEST_SOURCE"],
        model_catalog=str(catalog), provider_settings="", provider_test_mode=True,
        default_model_id="", origins=("http://testserver",),
        mcp_hosts=("127.0.0.1",), approval_seconds=60, model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False)
    app = create_app(settings)
    with TestClient(app) as client:
        service = app.state.service
        member = service.create_user({"username": "aggregate-member",
                                      "display_name": "Synthetic private member",
                                      "password": PASSWORD, "role": "member"})
        login(client, "aggregate-member")
        response = client.post("/api/v1/connections", json={
            "name": "Synthetic private Huawi aggregate MCP", "scope": "private",
            "url": mcp.url, "auth_type": "bearer", "credential": mcp.token})
        assert response.status_code == 201, response.text
        connection = response.json()
        discovery = client.post(f"/api/v1/connections/{connection['id']}/test")
        assert discovery.status_code == 200, discovery.text
        assert discovery.json()["status"] == "connected", discovery.text
        assert {tool["name"] for tool in discovery.json()["tools"]} == {"query"}
        # A typed mapping is required for FastMCP structured output.
        # Do not turn tool text into a host-verified receipt.
        assert not mcp.calls, "discovery must not execute the business tool"

        response = client.post("/api/v1/assistants", json={
            "name": "Synthetic private aggregate fixture", "scope": "private",
            "connections": [{"connection_id": connection["id"],
                             "allowed_tools": ["query"],
                             "auto_approved_tools": []}]})
        assert response.status_code == 201, response.text
        assistant = response.json()
        response = client.post("/api/v1/conversations", json={
            "assistant_id": assistant["id"], "permission_mode": "manual"})
        assert response.status_code == 201, response.text
        cid = response.json()["id"]

        def canonical(name="query"):
            return f"mcp__c{connection['id']}__{name}"

        def alias(name="query"):
            return "external_" + hashlib.sha256(
                canonical(name).encode()).hexdigest()[:32]

        run_ids = []

        def run(marker, arguments):
            model.actions[marker] = (alias(), copy.deepcopy(arguments))
            run_id = send(client, cid, marker,
                          f"huawi-real-aggregate-{time.monotonic_ns()}")["run_id"]
            run_ids.append(run_id)
            return run_id

        yield SimpleNamespace(
            client=client, service=service, member=member, connection=connection,
            assistant=assistant, cid=cid, mcp=mcp, model=model,
            settings=settings, alias=alias, canonical=canonical, run=run,
            run_ids=run_ids)

    # The real Harness child must never receive the MCP bearer. The runtime
    # writes its private session files under data_dir/harness.
    private_root = settings.data_dir / "harness"
    assert private_root.exists()
    for path in private_root.rglob("*"):
        if path.is_file():
            assert mcp.token.encode() not in path.read_bytes(), (
                f"MCP credential leaked into Harness file {path.name}")


def tool_events(env, run_id, kind):
    return [event["data"] for event in env.service.db.events(run_id, 0)
            if event["type"] == kind]


def decide(env, row, decision):
    response = env.client.post(
        f"/api/v1/approvals/{row['id']}/decision",
        json={"decision": decision})
    assert response.status_code == 200, response.text
    return response.json()


def test_real_harness_aggregate_cases_use_one_approved_query(
        aggregate_harness_app, record_property):
    env = aggregate_harness_app
    assert env.member["role"] == "member"
    stored_connection = env.service.require("connections", env.connection["id"])
    stored_assistant = env.service.require("assistants", env.assistant["id"])
    assert stored_connection["scope"] == "private"
    assert stored_connection["owner_id"] == env.member["id"]
    assert stored_assistant["scope"] == "private"
    assert stored_assistant["owner_id"] == env.member["id"]
    conversation = env.client.get(
        f"/api/v1/conversations/{env.cid}").json()
    assert conversation["runtime_type"] == "harness"
    assert Path(env.settings.harness_node_path).is_file()
    assert Path(env.settings.harness_source_dir, "package.json").is_file()

    outcomes = {}
    for case in ("allow", "zero", "empty", "deny"):
        env.mcp.state.payload = copy.deepcopy(CASES[case])
        marker = f"REAL_HUAWI_AGGREGATE_{case.upper()}"
        calls_before = len(env.mcp.calls)
        run_id = env.run(marker, QUERY_ARGUMENTS)

        pending = wait_run(env.client, run_id,
                           {"awaiting_approval", "succeeded"})
        assert pending["status"] == "awaiting_approval", pending
        # Exact approval gate: one pending row, no dispatch and no MCP call.
        assert len(env.mcp.calls) == calls_before
        assert not tool_events(env, run_id, "tool.dispatched")
        assert not tool_events(env, run_id, "tool.completed")
        assert len(pending["approvals"]) == 1, pending
        row = pending["approvals"][0]
        assert row["tool_name"] == env.canonical("query")
        assert row["arguments"] == QUERY_ARGUMENTS
        assert row["approval_mode"] == "single"
        assert row["required_approvals"] == 1
        persisted = env.service.require("approvals", row["id"])
        assert json.loads(persisted["arguments"]) == QUERY_ARGUMENTS
        assert row["argument_digest"] == hashlib.sha256(
            persisted["arguments"].encode("utf-8")).hexdigest()

        decision = "deny" if case == "deny" else "allow"
        decided = decide(env, row, decision)
        assert decided["status"] == ("denied" if decision == "deny" else "allowed")
        settled = wait_run(env.client, run_id, {"succeeded"})
        assert settled["status"] == "succeeded", settled

        started = tool_events(env, run_id, "tool.started")
        dispatched = tool_events(env, run_id, "tool.dispatched")
        completed = tool_events(env, run_id, "tool.completed")
        assert len(started) == 1 and len(completed) == 1
        assert started[0]["tool_name"] == env.canonical("query")
        assert started[0]["arguments"] == QUERY_ARGUMENTS
        assert started[0]["requires_approval"] is True
        outcome = completed[0]
        assert outcome["tool_name"] == env.canonical("query")
        assert outcome["tool_call_id"] == started[0]["tool_call_id"]
        persisted_after = env.service.require("approvals", row["id"])
        assert persisted_after["decided_by"] == env.member["id"]
        assert persisted_after["decision_source"] == "user"

        if decision == "deny":
            assert not dispatched
            assert len(env.mcp.calls) == calls_before
            assert outcome["success"] is False
            assert outcome.get("error_code") == "HARNESS_APPROVAL_DENIED"
            assert "count_receipt" not in outcome
            assert "business_result" not in outcome
            outcomes[case] = {"mcp_calls": 0, "receipt": None,
                              "success": False}
            continue

        assert len(dispatched) == 1, "approved query must dispatch exactly once"
        assert dispatched[0]["tool_call_id"] == outcome["tool_call_id"]
        assert outcome["success"] is True
        assert len(env.mcp.calls) == calls_before + 1
        assert env.mcp.calls[calls_before] == {
            "tool": "query", "arguments": QUERY_ARGUMENTS}
        assert outcome["source_ids"] == []

        if case == "empty":
            # Genuine empty: the call succeeded but reported no count row.
            assert "count_receipt" not in outcome
            assert "business_result" not in outcome
            outcomes[case] = {"mcp_calls": 1, "receipt": None, "success": True}
            continue

        count, kinds = (0, 0) if case == "zero" else (17, 5)
        receipt = outcome.get("count_receipt")
        assert receipt == expected_receipt(count, kinds), receipt
        card = outcome["business_result"]
        assert card["rows"] == [{"cnt": count, "kinds": kinds}]
        assert card["columns"] and [item["key"] for item in card["columns"]] == [
            "cnt", "kinds"]
        assert card["total_rows"] is None and card["complete"] is None
        assert card["source_ids"] == [] and outcome["source_ids"] == []
        outcomes[case] = {"mcp_calls": 1, "receipt": receipt, "success": True}

        # The model was actually offered the granted alias, not a raw or
        # ungranted tool name.
        first_request = next(body for body in env.model.requests
                             if marker in json.dumps(body["messages"],
                                                     ensure_ascii=False))
        exposed = {tool["name"] for tool in first_request.get("tools", [])}
        assert env.alias("query") in exposed
        assert env.canonical("query") not in exposed

    # Genuine 0 is distinct from empty and from a denied dispatch.
    assert outcomes["zero"]["receipt"]["counts"][0]["value"] == 0
    assert outcomes["zero"]["receipt"]["counts"][1]["value"] == 0
    assert outcomes["empty"]["receipt"] is None
    assert outcomes["deny"]["receipt"] is None
    assert sum(item["mcp_calls"] for item in outcomes.values()) == 3
    assert len(env.mcp.calls) == 3
    assert all(call["arguments"] == QUERY_ARGUMENTS for call in env.mcp.calls)

    # Loopback-only, no customer host and no production endpoint is involved.
    assert env.mcp.url == env.connection["url"]
    assert env.mcp.url.startswith("http://127.0.0.1:")
    assert env.model.url.startswith("http://127.0.0.1:")
    assert env.settings.mcp_hosts == ("127.0.0.1",)
    assert env.mcp.authenticated and all(env.mcp.authenticated)
    assert env.mcp.token not in json.dumps(env.model.requests, ensure_ascii=False)
    assert not env.service.db.all("SELECT id FROM evidence_sources")
    for run_id in env.run_ids:
        events = json.dumps(env.service.db.events(run_id, 0), ensure_ascii=False)
        assert env.mcp.token not in events
        trace = env.client.get(f"/api/v1/runs/{run_id}/trace")
        assert trace.status_code == 200, trace.text
        assert env.mcp.token not in trace.text

    record_property("huawi_real_aggregate_cases", 4)
    record_property("huawi_real_aggregate_mcp_tool_calls", len(env.mcp.calls))
    record_property("local_synthetic_model_requests", len(env.model.requests))
    record_property("paid_model_requests", 0)
    record_property("customer_or_production_requests", 0)
