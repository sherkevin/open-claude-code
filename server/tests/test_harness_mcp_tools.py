"""Harness business-tool boundary against a real local MCP HTTP transport.

The fixture is synthetic; Service, authorization, approval and evidence use the
application implementations. No model or customer connection is contacted.
"""

import asyncio
import copy
import gzip
import hashlib
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import dump
from app.harness_runtime import HarnessRuntime
from app.runtime import SessionActor
from app.security import AppError
from app.service import Service


SYNTHETIC_CREDENTIAL = "synthetic-mcp-boundary-secret-123"
TOOL_DEFINITIONS = [
    {"name": "read_records", "description": "Read local synthetic production records.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "minLength": 1, "maxLength": 120},
         "limit": {"type": "integer", "minimum": 1, "maximum": 3},
     }, "required": ["query"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_record", "description": "Write one local synthetic record.",
     "inputSchema": {"type": "object", "properties": {
         "record_id": {"type": "string", "enum": ["P-900"]},
         "value": {"type": "integer", "minimum": 0, "maximum": 10},
     }, "required": ["record_id", "value"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False}},
    {"name": "unbound_tool", "description": "Discovered but never granted.",
     "inputSchema": {"type": "object", "additionalProperties": False}},
]


@pytest.fixture
def mcp_server():
    state = SimpleNamespace(tools=copy.deepcopy(TOOL_DEFINITIONS), methods=[], calls=[],
                            authenticated=[], delayed=False, echo_credential=False, fail=False,
                            delay_method="tools/call", response_fault=None, response_format="json", redirected=[],
                            unavailable_paths=set(),
                            structured_mode="both", payload_padding=0,
                            started=threading.Event(), released=threading.Event())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def reply(self, status, value=None, headers=None, compressed=False, sse=False):
            body = b"" if value is None else json.dumps(value).encode()
            if sse:
                body = b"event: message\ndata: " + body + b"\n\n"
            if compressed:
                body = gzip.compress(body)
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.reply(405)

        def do_DELETE(self):
            self.reply(200)

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.methods.append(request["method"])
            authorized = self.headers.get("Authorization") == f"Bearer {SYNTHETIC_CREDENTIAL}"
            state.authenticated.append(authorized)
            if not authorized:
                self.reply(401)
                return
            if "id" not in request:
                self.reply(202)
                return
            method, params = request["method"], request.get("params") or {}
            if method == "initialize" and self.path in state.unavailable_paths:
                self.reply(503)
                return
            if self.path == "/redirected":
                state.redirected.append(request)
            if method == "tools/call":
                state.calls.append(params)
                state.started.set()
            if state.delayed and method == state.delay_method:
                state.started.set()
                state.released.wait(15)
            if method == "initialize":
                result = {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "synthetic-harness-business", "version": "1.0"}}
            elif method == "tools/list":
                result = {"tools": state.tools}
            elif method == "tools/call":
                record = {"record_id": "P-900", "pressure": 7}
                source = {"kind": "data", "title": "Synthetic pressure record", "excerpt": "P-900: 7 bar",
                          "locator": {"database": "synthetic", "table": "pressure", "fields": ["record_id", "pressure"],
                                      "time_range": "2026-11-10", "timezone": "UTC", "queried_at": "2026-11-10T00:00:00Z"}}
                if state.echo_credential:
                    record["description"] = f"Remote echo: Bearer {SYNTHETIC_CREDENTIAL}"
                    record["api_token"] = SYNTHETIC_CREDENTIAL
                    source["excerpt"] += f" {SYNTHETIC_CREDENTIAL}"
                body = {"record": record, "sources": [source],
                        "business_domain": "制造运营", "business_process": "库存异常分析",
                        "business_object": "压力记录", "metrics": ["pressure"],
                        "fields": ["record_id", "pressure"],
                        "filters": {"record_id": "P-900"}, "time_range": "2026-11-10",
                        "aggregation": "latest", "row_count": 1, "page": 1,
                        "page_size": 1, "cache_hit": False, "returned_rows": 1,
                        "original_rows": 1}
                if state.payload_padding:
                    body["padding"] = "x" * state.payload_padding
                if hasattr(state, "structured_override"):
                    body = copy.deepcopy(state.structured_override)
                result = {"content": [{"type": "text", "text": dump(body)}], "structuredContent": body,
                          "isError": state.fail}
                if state.structured_mode == "only":
                    result["content"] = []
                elif state.structured_mode == "empty":
                    result["content"], result["structuredContent"] = [], {}
                if state.response_fault == "oversize":
                    result["content"][0]["text"] = "x" * 1_000_001
                if state.response_fault == "redirect":
                    self.reply(307, headers={"Location": "/redirected"})
                    return
                if state.response_fault == "malformed":
                    self.reply(200, {"jsonrpc": "2.0", "id": {"value": SYNTHETIC_CREDENTIAL}, "result": {}})
                    return
            else:
                self.reply(200, {"jsonrpc": "2.0", "id": request["id"],
                                 "error": {"code": -32601, "message": "Unsupported fixture method"}})
                return
            compressed = method == "tools/call" and state.response_fault == "gzip"
            self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": result},
                       headers={"Content-Encoding": "gzip"} if compressed else None,
                       compressed=compressed, sse=method == "tools/call" and state.response_format == "sse")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/mcp"
    try:
        yield state
    finally:
        state.released.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def review_fixture_connection(service, connection_id, owner, names=("read_records",)):
    from app.connection_read_review import save_review
    from app.read_only_tools import definition_digest
    row = service.require("connections", connection_id)
    selected = {tool["name"]: {"connection_revision": row["revision"],
                "definition_digest": definition_digest(tool), "basis": "Inspected synthetic fixture's read-only implementation and contract"}
                for tool in json.loads(row["tools"]) if tool["name"] in names}
    return save_review(service, connection_id, {
        "expected_connection_revision": row["revision"],
        "expected_review_revision": row.get("read_only_review_revision", 0),
        "tools": selected,
    }, owner)


@pytest.fixture
def system(tmp_path, monkeypatch, mcp_server):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="synthetic",
                               runtime_type="harness", approval_seconds=15, mcp_hosts=("127.0.0.1",)))
    owner = service.create_user({"username": "owner", "display_name": "Owner", "role": "admin",
                                "password": "local-mcp-tests-only-123"})
    connection = service.save_connection({"name": "Local synthetic MCP", "url": mcp_server.url,
                                          "auth_type": "bearer", "credential": SYNTHETIC_CREDENTIAL}, user=owner)
    discovered = asyncio.run(service.test_connection(connection["id"], user=owner))
    assert discovered["status"] == "connected", discovered
    assert not mcp_server.calls, "Discovery must never execute a business operation"
    review_fixture_connection(service, connection["id"], owner)
    assistant = service.save_assistant({"name": "Business MCP fixture", "connections": [{
        "connection_id": connection["id"], "allowed_tools": ["read_records", "write_record"],
        "auto_approved_tools": ["read_records"],
    }]}, user=owner)
    cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Fixture", "permission_mode": "auto"}, owner)["id"]
    result, _ = service.prepare_run(cid, {"text": "Read the synthetic pressure", "file_ids": [],
                                         "client_request_id": "fixture"}, owner)
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (result["run_id"],))
    actor = SessionActor.__new__(SessionActor)
    actor.service, actor.conversation_id = service, cid
    actor.current_run_id, actor.cancelled = result["run_id"], False
    actor.manager = SimpleNamespace(service=service, approvals={}, context_requests={}, maintenance_busy=lambda _: False)
    service.manager = actor.manager
    runtime = HarnessRuntime(service, cid, service.runtime_config(cid), actor.approve, actor.emit)
    runtime.current_run = service.require("runs", result["run_id"])
    actor.runtime = runtime
    try:
        yield SimpleNamespace(runtime=runtime, actor=actor, service=service, owner=owner,
                              assistant=assistant, connection=connection, server=mcp_server)
    finally:
        service.db.close()


async def pending_approval(system):
    for _ in range(200):
        row = system.service.db.one("SELECT * FROM approvals WHERE run_id=? AND status='pending'", (system.actor.current_run_id,))
        if row:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("Expected a real SessionActor approval")


def canonical(system, name):
    return f"mcp__c{system.connection['id']}__{name}"


def alias(system, name):
    return "external_" + hashlib.sha256(canonical(system, name).encode()).hexdigest()[:32]


def request(system, name="read_records", arguments=None):
    return {"schema": "workspace-tool/v1", "sessionId": system.runtime.session_id, "callId": "business-test",
            "toolName": alias(system, name), "arguments": {"query": "P-900", "limit": 1} if arguments is None else arguments}


def write_request(system):
    return request(system, "write_record", {"record_id": "P-900", "value": 8})


def completed(system):
    return [event for event in system.service.db.events(system.actor.current_run_id, 0) if event["type"] == "tool.completed"][-1]["data"]


def new_run(system, *, permission_mode=None):
    service, actor, owner = system.service, system.actor, system.owner
    service.db.execute("UPDATE runs SET status='succeeded' WHERE id=?", (actor.current_run_id,))
    service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (actor.conversation_id,))
    if permission_mode is not None:
        service.patch_conversation(actor.conversation_id, {"permission_mode": permission_mode}, owner)
    result, _ = service.prepare_run(actor.conversation_id, {"text": "Read synthetic data", "file_ids": [],
                                                            "client_request_id": "next"}, owner)
    actor.current_run_id = result["run_id"]
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (actor.current_run_id,))
    config = service.runtime_config(actor.conversation_id)
    system.runtime = actor.runtime = HarnessRuntime(service, actor.conversation_id, config, actor.approve, actor.emit)
    system.runtime.current_run = service.require("runs", actor.current_run_id)
    return config


def certify_read(system):
    from app.read_only_tools import definition_digest

    row = system.service.require("connections", system.connection["id"])
    tool = next(tool for tool in json.loads(row["tools"]) if tool["name"] == "read_records")
    binding = {"connection_id": row["id"], "allowed_tools": ["read_records", "write_record"],
               "auto_approved_tools": [], "read_only_confirmations": {"read_records": {
                   "connection_revision": row["revision"], "definition_digest": definition_digest(tool),
                   "basis": "Verified fixture routes, read-only credential and allowed arguments",
               }}}
    system.service.save_assistant({"connections": [binding]}, system.assistant["id"], user=system.owner)


@pytest.mark.parametrize("mode", ["manual", "auto", "full"])
def test_session_approval_modes_use_the_same_certified_read_and_keep_permissions(system, mode):
    certify_read(system)
    new_run(system, permission_mode=mode)

    async def scenario():
        with pytest.raises(AppError):
            await system.runtime.bridge.invoke(request(system, "unbound_tool", {}))
        task = asyncio.create_task(system.runtime.bridge.invoke(request(system)))
        if mode == "manual":
            approval = await pending_approval(system)
            assert not system.server.calls
            system.service.db.execute("UPDATE approvals SET decided_by=? WHERE id=?", (system.owner["id"], approval["id"]))
            system.actor.manager.approvals[approval["id"]].set_result("allow")
        result = await asyncio.wait_for(task, 8)
        assert not result.get("is_error"), result
        assert [call["name"] for call in system.server.calls] == ["read_records"]
        decisions = [event["data"] for event in system.service.db.events(system.actor.current_run_id, 0)
                     if event["type"] == "approval.session_decided"]
        if mode == "manual":
            assert not decisions
        else:
            assert len(decisions) == 1
            assert (decisions[0]["mode"], decisions[0]["tool_name"], decisions[0]["decided_by"], decisions[0]["certified_by"]) == (
                mode, canonical(system, "read_records"), system.owner["id"], system.owner["id"])

    asyncio.run(scenario())
    conversation = system.service.list_conversations(system.owner)[0]
    assert conversation["permission_mode"] == mode
    changes = system.service.db.all("SELECT * FROM conversation_permission_changes WHERE conversation_id=?", (conversation["id"],))
    assert changes[-1]["actor_id"] == system.owner["id"] and changes[-1]["mode"] == mode


def test_automatic_approval_rejects_live_definition_change_before_call(system):
    certify_read(system)
    new_run(system, permission_mode="auto")
    system.server.tools[0]["annotations"]["readOnlyHint"] = False
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert result["is_error"] and not system.server.calls


def test_full_keeps_manual_external_writes_and_rejects_member_selection(system):
    certify_read(system)
    member = system.service.create_user({"username": "member", "display_name": "Member", "role": "member",
                                         "password": "local-mcp-tests-only-123"})
    member_assistant = system.service.save_assistant({"name": "Member assistant"}, user=member)
    with pytest.raises(AppError, match="完全权限需要管理员设置"):
        system.service.create_conversation({"assistant_id": member_assistant["id"], "title": "Member",
                                            "permission_mode": "full"}, member)
    legacy = json.loads(system.service.require("assistants", system.assistant["id"])["connections"])
    legacy[0]["auto_approved_tools"] = ["write_record"]
    system.service.save_assistant({"connections": legacy}, system.assistant["id"], user=system.owner)
    new_run(system, permission_mode="full")

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(write_request(system)))
        approval = await pending_approval(system)
        assert not system.server.calls
        system.actor.manager.approvals[approval["id"]].set_result("deny")
        result = await asyncio.wait_for(task, 8)
        assert result["is_error"] and not system.server.calls

    asyncio.run(scenario())


def test_unavailable_mcp_keeps_healthy_tools_and_rediscovers_without_call_or_user_test(system):
    service = system.service
    healthy = service.save_connection({"name": "Other synthetic MCP", "url": system.server.url + "/healthy",
                                       "auth_type": "bearer", "credential": SYNTHETIC_CREDENTIAL}, user=system.owner)
    assert asyncio.run(service.test_connection(healthy["id"], user=system.owner))["status"] == "connected"
    review_fixture_connection(service, healthy["id"], system.owner)
    binding = {"allowed_tools": ["read_records"], "auto_approved_tools": ["read_records"]}
    service.save_assistant({"connections": [{**binding, "connection_id": connection["id"]}
                                             for connection in (system.connection, healthy)]}, system.assistant["id"], user=system.owner)
    system.server.unavailable_paths.add("/mcp")
    failed = asyncio.run(service.test_connection(system.connection["id"], user=system.owner))
    assert failed["status"] == "failed" and any(tool["name"] == "read_records" for tool in failed["tools"])
    config = new_run(system)
    assert [source["connection_id"] for source in config["mcp_unavailable"]] == [system.connection["id"]]
    assert config["mcp_unavailable"][0]["reason"]
    assert set(config["mcp_servers"]) == {f"c{healthy['id']}"}
    healthy_system = SimpleNamespace(**{**vars(system), "connection": healthy})
    result = asyncio.run(system.runtime.bridge.invoke(request(healthy_system)))
    assert not result.get("is_error"), result
    assert len(system.server.calls) == 1
    system.server.unavailable_paths.clear()
    asyncio.run(service.mcp_availability.refresh(system.actor.conversation_id))
    recovered = service.runtime_config(system.actor.conversation_id)
    assert not recovered["mcp_unavailable"] and len(recovered["mcp_inventory"]) == 2
    assert config["fingerprint"] == recovered["fingerprint"]
    assert len(system.server.calls) == 1, "Background discovery may not invoke a business tool"


async def assert_denied(system, payload):
    try:
        result = await system.runtime.bridge.invoke(payload)
    except AppError:
        return
    assert result.get("is_error"), result


def test_business_definitions_use_granted_stable_aliases_without_credentials(system):
    definitions = system.runtime.bridge.definitions()
    names = {item["name"] for item in definitions}
    assert {alias(system, "read_records"), alias(system, "write_record")} <= names
    assert alias(system, "unbound_tool") not in names
    assert not any(name.startswith("mcp__") for name in names)
    assert SYNTHETIC_CREDENTIAL not in dump(definitions)
    assert system.server.url not in dump(definitions)
    assert not system.server.calls
    assert "initialize" in system.server.methods and "tools/list" in system.server.methods
    assert all(system.server.authenticated)


def test_business_read_uses_real_transport_and_registers_revocable_sources(system):
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert not result.get("is_error"), result
    assert len(system.server.calls) == 1
    assert system.server.calls[0]["name"] == "read_records"
    assert system.server.calls[0]["arguments"] == {"query": "P-900", "limit": 1}
    assert not system.service.db.all("SELECT * FROM approvals")
    sources = system.service.evidence.list(system.actor.current_run_id, system.owner)
    assert sources and f"[[source:{sources[0]['id']}]]" in dump(result)
    assert completed(system)["source_ids"] == [sources[0]["id"]]
    assert completed(system)["tool_name"] == canonical(system, "read_records")
    registered = system.service.db.one("SELECT * FROM evidence_sources WHERE id=?", (sources[0]["id"],))
    assert registered["resource_id"] == system.connection["id"]
    assert registered["tool_name"] == "read_records"
    facts = completed(system)["result_facts"]
    assert facts["business_domain"] == "制造运营"
    assert facts["business_process"] == "库存异常分析"
    assert facts["returned_rows"] == 1 and facts["original_rows"] == 1
    stages = [event["data"].get("stage") for event in system.service.db.events(system.actor.current_run_id, 0)
              if event["type"] == "run.stage"]
    assert stages[-2:] == ["tool", "evidence"]
    assert not {"scoping", "routing"}.intersection(stages)
    system.service.save_assistant({"connections": []}, system.assistant["id"], user=system.owner)
    with pytest.raises(AppError):
        system.service.evidence.get(sources[0]["id"], system.owner)


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_business_write_requires_real_human_approval_before_network_call(system, decision):
    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(write_request(system)))
        approval = await pending_approval(system)
        assert approval["tool_name"] == canonical(system, "write_record")
        assert not system.server.calls
        system.service.db.execute("UPDATE approvals SET decided_by=?,decision_source='user' WHERE id=?", (system.owner["id"], approval["id"]))
        system.actor.manager.approvals[approval["id"]].set_result(decision)
        result = await asyncio.wait_for(task, 10)
        assert bool(result.get("is_error")) is (decision == "deny")
        assert len(system.server.calls) == (1 if decision == "allow" else 0)
        assert completed(system)["success"] is (decision == "allow")
        stages = [event["data"] for event in system.service.db.events(system.actor.current_run_id, 0)
                  if event["type"] == "run.stage"]
        assert [stage["stage"] for stage in stages] == ["tool", "evidence"]
        if decision == "deny":
            assert stages[-1]["status"] == "unknown" and stages[-1]["source_count"] == 0

    asyncio.run(scenario())


def test_business_write_requires_two_real_distinct_nonrequester_admin_votes(system):
    admins = [system.service.create_user({"username": f"approver{index}", "display_name": f"Approver {index}",
              "password": "local-mcp-tests-only-123", "role": "admin"}) for index in (1, 2)]
    system.service.save_high_risk_approval_policy(system.assistant["id"], {
        "enabled": True, "allowed_tools": [canonical(system, "write_record")],
        "admin_allowlist": [admin["id"] for admin in admins],
    }, system.owner)

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(write_request(system)))
        approval = await pending_approval(system)
        assert approval["approval_mode"] == "dual" and not system.server.calls
        with pytest.raises(AppError):
            system.service.high_risk_approval.record_vote(approval, system.owner, "allow")
        first = system.service.high_risk_approval.record_vote(approval, admins[0], "allow")
        assert first["status"] == "pending" and not task.done() and not system.server.calls
        with pytest.raises(AppError) as duplicate:
            system.service.high_risk_approval.record_vote(approval, admins[0], "allow")
        assert duplicate.value.code == "APPROVAL_DUPLICATE_VOTE"
        final = system.service.high_risk_approval.record_vote(system.service.require("approvals", approval["id"]), admins[1], "allow")
        assert final["status"] == "allowed"
        result = await asyncio.wait_for(task, 10)
        assert not result.get("is_error") and len(system.server.calls) == 1
        assert len(system.service.db.all("SELECT * FROM approval_votes WHERE approval_id=?", (approval["id"],))) == 2
        assert system.service.require("approvals", approval["id"])["decision_source"] == "dual_admin"

    asyncio.run(scenario())


def test_model_approval_policy_cannot_auto_approve_business_write(system):
    system.service.save_model_approval_policy(system.assistant["id"], {
        "enabled": True, "model_id": "synthetic-mcp-policy", "allowed_tools": ["Read"],
        "allowed_data_scopes": ["workspace"], "allowed_operations": ["read"], "max_amount": 0,
    }, system.owner)

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(write_request(system)))
        approval = await pending_approval(system)
        assert approval["strategy"] != "model" and not system.server.calls
        system.actor.manager.approvals[approval["id"]].set_result("deny")
        assert (await asyncio.wait_for(task, 10))["is_error"]
        assert not system.server.calls

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["remove_binding", "disable_connection", "change_connection", "enable_policy", "cancel"])
def test_business_approved_write_rechecks_grants_configuration_policy_and_cancellation(system, change):
    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(write_request(system)))
        approval = await pending_approval(system)
        if change == "remove_binding":
            system.service.save_assistant({"connections": []}, system.assistant["id"], user=system.owner)
        elif change == "disable_connection":
            system.service.save_connection({"enabled": False}, system.connection["id"], user=system.owner)
        elif change == "change_connection":
            system.service.save_connection({"name": "Changed during approval"}, system.connection["id"], user=system.owner)
        elif change == "enable_policy":
            system.service.save_high_risk_approval_policy(system.assistant["id"], {
                "enabled": True, "allowed_tools": [], "admin_allowlist": [],
            }, system.owner)
        else:
            await system.actor.cancel()
        future = system.actor.manager.approvals.get(approval["id"])
        if future and not future.done():
            future.set_result("allow")
        assert (await asyncio.wait_for(task, 10))["is_error"]
        assert not system.server.calls and completed(system)["success"] is False

    asyncio.run(scenario())


@pytest.mark.parametrize("arguments", [
    {"query": "P-900", "connection_id": "unrelated"},
    {"query": "P-900", "url": "http://unapproved.invalid/mcp"},
    {"query": "P-900", "limit": "1"},
    {"query": "P-900", "limit": 4},
    {},
])
def test_business_schema_rejects_ungranted_routing_fields_and_invalid_values(system, arguments):
    asyncio.run(assert_denied(system, request(system, arguments=arguments)))
    assert not system.server.calls


def test_business_unbound_discovered_tool_cannot_be_invoked(system):
    asyncio.run(assert_denied(system, request(system, "unbound_tool", {})))
    assert not system.server.calls


@pytest.mark.parametrize("change", ["remote_schema", "saved_tools"])
def test_business_discovery_change_cannot_reuse_a_stale_tool_grant(system, change):
    if change == "remote_schema":
        system.server.tools[0]["inputSchema"]["properties"]["query"]["maxLength"] = 3
    else:
        # test_connection updates tools without incrementing connection revision.
        system.service.db.execute("UPDATE connections SET tools='[]' WHERE id=?", (system.connection["id"],))
    asyncio.run(assert_denied(system, request(system)))
    assert not system.server.calls


@pytest.mark.parametrize("change", ["remove_binding", "enable_policy"])
def test_business_pending_http_result_is_discarded_after_permission_change(system, change):
    system.server.delayed = True

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(request(system)))
        for _ in range(200):
            if system.server.started.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Local MCP handler was not reached")
        if change == "remove_binding":
            system.service.save_assistant({"connections": []}, system.assistant["id"], user=system.owner)
        else:
            system.service.save_high_risk_approval_policy(system.assistant["id"], {
                "enabled": True, "allowed_tools": [], "admin_allowlist": [],
            }, system.owner)
        system.server.released.set()
        result = await asyncio.wait_for(task, 10)
        assert result["is_error"] and "Synthetic pressure record" not in dump(result)
        assert not system.service.db.all("SELECT * FROM evidence_sources")
        assert completed(system)["source_ids"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("response_format", ["json", "sse"])
def test_business_credential_echo_is_removed_before_model_audit_and_evidence(system, caplog, response_format):
    caplog.set_level(logging.DEBUG, logger="mcp")
    system.server.echo_credential = True
    system.server.response_format = response_format
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert not result.get("is_error"), result
    sources = system.service.evidence.list(system.actor.current_run_id, system.owner)
    assert sources
    public = dump({"result": result, "sources": sources, "events": system.service.db.events(system.actor.current_run_id, 0),
                   "definitions": system.runtime.bridge.definitions()})
    assert SYNTHETIC_CREDENTIAL not in public
    assert SYNTHETIC_CREDENTIAL not in caplog.text
    assert all(system.server.authenticated)


def test_business_error_result_never_registers_successful_evidence(system):
    system.server.fail = True
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert result.get("is_error"), result
    assert len(system.server.calls) == 1
    assert not system.service.db.all("SELECT * FROM evidence_sources")
    assert completed(system)["success"] is False and completed(system)["source_ids"] == []
    stages = [event["data"] for event in system.service.db.events(system.actor.current_run_id, 0)
              if event["type"] == "run.stage"]
    assert [stage["stage"] for stage in stages] == ["tool", "evidence"]
    assert stages[-1]["status"] == "unknown" and stages[-1]["source_count"] == 0


@pytest.mark.parametrize("fault", ["gzip", "oversize", "redirect", "malformed"])
def test_business_invalid_transport_response_is_rejected_without_retry_or_evidence(system, fault, caplog, monkeypatch):
    monkeypatch.setattr("app.mcp_tools.MCP_TIMEOUT_SECONDS", 1)
    system.server.response_fault = fault
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert result.get("is_error"), result
    assert len(system.server.calls) == 1, "Failed external operations must never be replayed"
    assert not system.server.redirected, "Redirect targets must not receive credentials or operations"
    assert not system.service.db.all("SELECT * FROM evidence_sources")
    assert completed(system)["success"] is False and completed(system)["source_ids"] == []
    assert SYNTHETIC_CREDENTIAL not in dump(result) + caplog.text


def test_business_transport_total_deadline_aborts_without_retry(system, monkeypatch):
    monkeypatch.setattr("app.mcp_tools.MCP_TIMEOUT_SECONDS", 0.25)
    system.server.delayed = True
    started = time.monotonic()
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    assert time.monotonic() - started < 3
    assert result.get("is_error"), result
    assert len(system.server.calls) == 1
    assert not system.service.db.all("SELECT * FROM evidence_sources")
    assert completed(system)["success"] is False


async def wait_for_http_boundary(system):
    for _ in range(200):
        if system.server.started.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Local MCP transport did not reach the expected boundary")


def test_business_transport_cancellation_propagates_and_never_replays(system):
    system.server.delayed = True

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(request(system)))
        await wait_for_http_boundary(system)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert len(system.server.calls) == 1
        assert not system.service.db.all("SELECT * FROM evidence_sources")
        assert completed(system)["success"] is False and completed(system)["source_ids"] == []

    asyncio.run(scenario())


def test_business_schema_cannot_expose_json_escaped_connection_credential(system):
    from app.mcp_tools import MCPBusinessTool

    credential = 'synthetic-quote"and-backslash\\-secret'
    connection = {**system.connection, "secret": system.service.secrets.encrypt(credential)}
    definition = copy.deepcopy(TOOL_DEFINITIONS[0])
    definition["inputSchema"]["properties"]["query"]["default"] = credential
    with pytest.raises(AppError) as error:
        MCPBusinessTool(system.runtime, connection, definition)
    assert error.value.code == "HARNESS_MCP_SCHEMA_UNSUPPORTED"


@pytest.mark.parametrize("method", ["initialize", "tools/list"])
@pytest.mark.parametrize("change", ["remove_binding", "disable_connection", "enable_policy", "cancel"])
def test_business_revocation_during_http_discovery_prevents_operation(system, method, change):
    system.server.delayed = True
    system.server.delay_method = method

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(request(system)))
        await wait_for_http_boundary(system)
        assert not system.server.calls
        if change == "remove_binding":
            system.service.save_assistant({"connections": []}, system.assistant["id"], user=system.owner)
        elif change == "disable_connection":
            system.service.save_connection({"enabled": False}, system.connection["id"], user=system.owner)
        elif change == "enable_policy":
            system.service.save_high_risk_approval_policy(system.assistant["id"], {
                "enabled": True, "allowed_tools": [], "admin_allowlist": [],
            }, system.owner)
        else:
            await system.actor.cancel()
        system.server.released.set()
        result = await asyncio.wait_for(task, 10)
        assert result.get("is_error"), result
        assert not system.server.calls, "Revoked grants must fail before tools/call reaches the server"
        assert not system.service.db.all("SELECT * FROM evidence_sources")
        assert completed(system)["success"] is False and completed(system)["source_ids"] == []

    asyncio.run(scenario())


def test_transport_log_redaction_is_scoped_to_the_invocation_and_its_children(caplog):
    from app.mcp_tools import transport_logs

    logger = logging.getLogger("mcp.client.streamable_http")
    caplog.set_level(logging.WARNING, logger=logger.name)

    async def scenario():
        entered, released = asyncio.Event(), asyncio.Event()

        async def protected_call():
            with transport_logs():
                logger.warning("private transport message: %s", SYNTHETIC_CREDENTIAL)
                entered.set()
                await released.wait()

                async def child():
                    try:
                        raise ValueError(SYNTHETIC_CREDENTIAL)
                    except ValueError:
                        logger.exception("private child error")

                await asyncio.create_task(child())
            logger.warning("normal diagnostic after context")

        task = asyncio.create_task(protected_call())
        await entered.wait()
        logger.warning("normal concurrent diagnostic")
        released.set()
        await task
        logger.warning("normal diagnostic after task")

    asyncio.run(scenario())
    assert SYNTHETIC_CREDENTIAL not in caplog.text
    assert "normal concurrent diagnostic" in caplog.text
    assert "normal diagnostic after context" in caplog.text
    assert "normal diagnostic after task" in caplog.text
    assert "private child error" not in caplog.text
    assert sum("diagnostic withheld" in record.getMessage() for record in caplog.records) == 2


def test_business_completion_only_attributes_sources_registered_by_this_call(system):
    system.server.delayed = True

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(request(system)))
        await wait_for_http_boundary(system)
        other = system.service.evidence.register(
            system.actor.current_run_id, "data", "Concurrent unrelated source",
            {"database": "synthetic", "table": "temperature", "fields": ["record_id", "temperature"],
             "time_range": "2026-11-10", "timezone": "UTC", "queried_at": "2026-11-10T00:00:00Z"},
            "P-900: 35 degrees", resource_type="connection", resource_id=system.connection["id"],
            tool_name="read_records")
        assert system.service.evidence.get(other["id"], system.owner)["id"] == other["id"]
        system.server.released.set()
        result = await asyncio.wait_for(task, 10)
        assert not result.get("is_error"), result
        sources = system.service.evidence.list(system.actor.current_run_id, system.owner)
        own = next(source for source in sources if source["title"] == "Synthetic pressure record")
        assert len(sources) == 2
        assert completed(system)["source_ids"] == [own["id"]]
        assert other["id"] not in dump(result)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_legacy_unreviewed_read_requires_manual_approval_even_in_auto(system, mode):
    from app.connection_read_review import save_review
    row = system.service.require("connections", system.connection["id"])
    save_review(system.service, row["id"], {
        "expected_connection_revision": row["revision"],
        "expected_review_revision": row["read_only_review_revision"], "tools": {}}, system.owner)
    new_run(system, permission_mode=mode)

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(request(system)))
        approval = await pending_approval(system)
        assert not system.server.calls
        system.actor.manager.approvals[approval["id"]].set_result("deny")
        result = await asyncio.wait_for(task, 8)
        assert result["is_error"] and not system.server.calls

    asyncio.run(scenario())


def test_auto_mode_never_accepts_legacy_write_flag(system):
    binding = json.loads(system.service.require("assistants", system.assistant["id"])["connections"])
    binding[0]["auto_approved_tools"] = ["write_record"]
    system.service.save_assistant({"connections": binding}, system.assistant["id"], user=system.owner)
    new_run(system, permission_mode="auto")

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(write_request(system)))
        approval = await pending_approval(system)
        assert not system.server.calls
        system.actor.manager.approvals[approval["id"]].set_result("deny")
        assert (await asyncio.wait_for(task, 8))["is_error"]
        assert not system.server.calls
    asyncio.run(scenario())
