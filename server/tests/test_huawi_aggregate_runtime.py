"""Synthetic runtime and real loopback MCP aggregate receipt checks.

The direct SDK callback is text-only. The SDK host bridge retains the original
structured result before that flattening; the Harness bridge also sees the
actual structured result. A receipt is limited to the exact approved query
contract and does not grant authority or certify the underlying scope.
"""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import dump
from app.harness_runtime import HarnessRuntime
from app.huawi_aggregate_receipt import (
    count_receipt_metadata, project_count_receipt)
from app.mcp_tools import sdk_business_result
from app.runtime import SessionActor, result_fact_metadata
from app.service import Service
from test_harness_mcp_tools import (  # noqa: F401 - mcp_server is a pytest fixture
    SYNTHETIC_CREDENTIAL, alias, canonical, completed, mcp_server,
    pending_approval, review_fixture_connection)

COUNT_SCHEMA = {
    "type": "object",
    "properties": {
        "dataset": {"type": "string", "minLength": 1, "maxLength": 100},
        "select": {
            "type": "array", "minItems": 1, "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "minLength": 1, "maxLength": 100},
                    "aggregate": {"type": "string", "enum": ["count", "count_distinct"]},
                    "alias": {"type": "string", "minLength": 1, "maxLength": 100},
                },
                "required": ["field", "aggregate", "alias"],
                "additionalProperties": False,
            },
        },
        "filters": {"type": "array", "maxItems": 0},
        "group_by": {"type": "array", "maxItems": 0},
        "order_by": {"type": "array", "maxItems": 0},
        "page": {"type": "integer", "minimum": 1, "maximum": 1},
        "rows": {"type": "integer", "minimum": 1, "maximum": 1000},
        "include_total": {"type": "boolean"},
    },
    "required": ["dataset", "select"],
    "additionalProperties": False,
}

COUNT_QUERY = {
    "dataset": "synthetic_table",
    "select": [
        {"field": "*", "aggregate": "count", "alias": "cnt"},
        {"field": "item", "aggregate": "count_distinct", "alias": "kinds"},
    ],
    "page": 1,
    "rows": 20,
    "include_total": True,
}

COUNT_TOOL = {
    "name": "query",
    "description": "Synthetic dataset aggregate query.",
    "inputSchema": copy.deepcopy(COUNT_SCHEMA),
    "annotations": {"readOnlyHint": True},
}


def aggregate_payload(data=((17, 5),), *, total=None):
    value = {
        "dataset": "synthetic_table",
        "columns": [
            {"name": "cnt", "label": "Synthetic count label", "type": "int"},
            {"name": "kinds", "label": "Synthetic distinct label", "type": "int"},
        ],
        "data": [list(row) for row in data],
        "rows": 20,
        "page": 1,
        "has_more": False,
    }
    if total is not None:
        value["total"] = total
    return value


def envelope(value):
    return {"content": [], "structuredContent": copy.deepcopy(value)}


def test_real_sdk_string_response_is_content_only():
    # The controller's real SDK run observed exactly this: a str tool_response
    # with no structuredContent. Normalization must not fabricate one.
    normalized = sdk_business_result(json.dumps(aggregate_payload(total=1)))
    assert set(normalized) == {"content"}
    assert "structuredContent" not in normalized
    assert project_count_receipt(COUNT_QUERY, normalized) is None


def test_sdk_text_cannot_be_promoted_even_with_trusted_arguments():
    # Even when the approved query arguments are available, a text-only result
    # must never be parsed into a receipt. Only a real structured result from
    # the trusted host receive point qualifies.
    normalized = sdk_business_result(json.dumps(aggregate_payload(total=1)))
    metadata = result_fact_metadata(True, normalized, [], COUNT_QUERY)
    assert "count_receipt" not in metadata and "business_result" not in metadata
    assert sdk_business_result("[]")["content"][0]["text"] == "[]"


def test_trusted_structured_result_projects_when_the_host_passes_arguments():
    # This is the pure contract used by the Harness bridge; it is not a claim
    # that the SDK PostToolUse hook receives structuredContent.
    metadata = result_fact_metadata(True, envelope(aggregate_payload(total=1)),
                                    [], COUNT_QUERY)
    assert [item["value"] for item in metadata["count_receipt"]["counts"]] == [17, 5]
    assert metadata["business_result"]["rows"] == [{"cnt": 17, "kinds": 5}]


def test_zero_is_reported_and_empty_is_not_a_receipt():
    zero = count_receipt_metadata(COUNT_QUERY, envelope(aggregate_payload(data=((0, 0),))), [])
    assert [item["value"] for item in zero["count_receipt"]["counts"]] == [0, 0]
    assert count_receipt_metadata(COUNT_QUERY, envelope(aggregate_payload(data=())), []) is None


def _build_count_system(tmp_path, monkeypatch, mcp_server, *, automatic, reviewed=True, tool_name="query"):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    mcp_server.tools = [dict(copy.deepcopy(COUNT_TOOL), name=tool_name)]
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="synthetic",
                               runtime_type="harness", approval_seconds=15,
                               mcp_hosts=("127.0.0.1",)))
    owner = service.create_user({"username": "count-owner", "display_name": "Owner",
                                 "role": "admin", "password": "local-count-tests-only-123"})
    connection = service.save_connection({"name": "Synthetic aggregate MCP",
                                          "url": mcp_server.url, "auth_type": "bearer",
                                          "credential": SYNTHETIC_CREDENTIAL}, user=owner)
    discovered = asyncio.run(service.test_connection(connection["id"], user=owner))
    assert discovered["status"] == "connected", discovered
    if reviewed:
        review_fixture_connection(service, connection["id"], owner, names=(tool_name,))
    binding = {"connection_id": connection["id"], "allowed_tools": [tool_name],
               "auto_approved_tools": [tool_name] if automatic else []}
    assistant = service.save_assistant({"name": "Synthetic aggregate fixture",
                                        "connections": [binding]}, user=owner)
    cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Aggregate",
                                       "permission_mode": "auto" if automatic else "manual"},
                                      owner)["id"]
    result, _ = service.prepare_run(cid, {"text": "Synthetic aggregate query", "file_ids": [],
                                          "client_request_id": "count-fixture"}, owner)
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (result["run_id"],))
    actor = SessionActor.__new__(SessionActor)
    actor.service, actor.conversation_id = service, cid
    actor.current_run_id, actor.cancelled = result["run_id"], False
    actor.manager = SimpleNamespace(service=service, approvals={}, context_requests={},
                                    maintenance_busy=lambda _: False)
    service.manager = actor.manager
    runtime = HarnessRuntime(service, cid, service.runtime_config(cid), actor.approve, actor.emit)
    runtime.current_run = service.require("runs", result["run_id"])
    actor.runtime = runtime
    return SimpleNamespace(runtime=runtime, actor=actor, service=service, owner=owner,
                           assistant=assistant, connection=connection, server=mcp_server, tool_name=tool_name)


@pytest.fixture
def count_system(tmp_path, monkeypatch, mcp_server):  # noqa: F811 - pytest fixture
    system = _build_count_system(tmp_path, monkeypatch, mcp_server, automatic=True)
    try:
        yield system
    finally:
        system.service.db.close()


@pytest.fixture
def manual_count_system(tmp_path, monkeypatch, mcp_server):  # noqa: F811 - pytest fixture
    system = _build_count_system(tmp_path, monkeypatch, mcp_server, automatic=False)
    try:
        yield system
    finally:
        system.service.db.close()


@pytest.fixture
def plain_count_system(tmp_path, monkeypatch, mcp_server):  # noqa: F811 - pytest fixture
    system = _build_count_system(tmp_path, monkeypatch, mcp_server,
                                 automatic=False, reviewed=False)
    try:
        yield system
    finally:
        system.service.db.close()


def count_request(system, arguments=None):
    return {"schema": "workspace-tool/v1", "sessionId": system.runtime.session_id,
            "callId": "aggregate-test", "toolName": alias(system, system.tool_name),
            "arguments": copy.deepcopy(COUNT_QUERY if arguments is None else arguments)}


def test_harness_aggregate_result_gets_card_and_receipt(count_system):
    system = count_system
    system.server.structured_override = aggregate_payload(total=1)
    result = asyncio.run(system.runtime.bridge.invoke(count_request(system)))
    event = completed(system)
    assert event["success"] is True
    assert [item["value"] for item in event["count_receipt"]["counts"]] == [17, 5]
    card = event["business_result"]
    assert card["rows"] == [{"cnt": 17, "kinds": 5}]
    assert card["total_rows"] is None and card["complete"] is None
    assert event["source_ids"] == [] and card["source_ids"] == []
    text = "\n".join(block["text"] for block in result["content"])
    assert "工具报告聚合值" in text
    assert "Synthetic count label" not in dump(event)
    assert not system.service.evidence.list(system.actor.current_run_id, system.owner)


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_harness_manual_approval_gates_the_count_receipt(manual_count_system, decision):
    system = manual_count_system
    system.server.structured_override = aggregate_payload(total=1)

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(count_request(system)))
        approval = await pending_approval(system)
        assert approval["tool_name"] == canonical(system, system.tool_name)
        assert not system.server.calls
        system.service.db.execute(
            "UPDATE approvals SET decided_by=?,decision_source='user' WHERE id=?",
            (system.owner["id"], approval["id"]))
        system.actor.manager.approvals[approval["id"]].set_result(decision)
        return await asyncio.wait_for(task, 10)

    result = asyncio.run(scenario())
    event = completed(system)
    assert len(system.server.calls) == (1 if decision == "allow" else 0)
    assert event["success"] is (decision == "allow")
    if decision == "allow":
        assert not result.get("is_error")
        assert [item["value"] for item in event["count_receipt"]["counts"]] == [17, 5]
    else:
        assert result.get("is_error") is True
        assert "count_receipt" not in event and "business_result" not in event


def test_harness_manual_query_receipt_does_not_certify_scope(plain_count_system):
    # Explicit approval permits one exact query. Its structured result can
    # be reported, but the receipt is not a read-only certification or proof
    # of the underlying data scope.
    system = plain_count_system
    system.server.structured_override = aggregate_payload(total=1)

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(count_request(system)))
        approval = await pending_approval(system)
        system.service.db.execute(
            "UPDATE approvals SET decided_by=?,decision_source='user' WHERE id=?",
            (system.owner["id"], approval["id"]))
        system.actor.manager.approvals[approval["id"]].set_result("allow")
        return await asyncio.wait_for(task, 10)

    asyncio.run(scenario())
    event = completed(system)
    assert event["success"] is True
    assert len(system.server.calls) == 1
    assert event["count_receipt"]["scope_verified"] is False
    assert [cell["value"] for cell in event["count_receipt"]["counts"]] == [17, 5]


def test_non_query_tool_cannot_produce_count_receipt(tmp_path, monkeypatch, mcp_server):
    system = _build_count_system(tmp_path, monkeypatch, mcp_server,
                                 automatic=True, tool_name="query_dataset")
    try:
        system.server.structured_override = aggregate_payload(total=1)
        asyncio.run(system.runtime.bridge.invoke(count_request(system)))
        event = completed(system)
        assert event["success"] is True
        assert len(system.server.calls) == 1
        assert "count_receipt" not in event and "business_result" not in event
    finally:
        system.service.db.close()


def test_harness_canonical_card_is_not_reclassified(count_system):
    from test_business_results import payload
    system = count_system
    system.server.structured_override = payload()
    asyncio.run(system.runtime.bridge.invoke(count_request(system)))
    event = completed(system)
    assert event["success"] is True
    assert "count_receipt" not in event
    assert event["business_result"]["rows"] == payload()["rows"]


def test_harness_empty_structured_result_has_no_receipt(count_system):
    system = count_system
    system.server.structured_mode = "empty"
    asyncio.run(system.runtime.bridge.invoke(count_request(system)))
    event = completed(system)
    assert event["success"] is True
    assert "count_receipt" not in event and "business_result" not in event
