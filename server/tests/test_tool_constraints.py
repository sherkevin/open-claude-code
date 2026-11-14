"""Real service boundaries with synthetic tool schemas; no customer/network I/O."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import dump
from app.harness_runtime import HarnessRuntime
from app.models import AssistantCreate
from app.read_only_tools import definition_digest
from app.runtime import SDKRuntime
from app.security import AppError
from app.service import Service
from app.workflows import _run_mcp_read, _workflow_tool_scope
from test_api import client, configured


REMOTE = {
    "type": "object",
    "properties": {
        "fields": {"type": "array", "items": {"type": "string"}},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        "factory": {"type": "string"}, "organization": {"type": "string"},
        "date_from": {"type": "string"}, "date_to": {"type": "string"},
        "aggregation": {"type": "string", "enum": ["count", "sum", "none"]},
    },
    "additionalProperties": False,
}
CONSTRAINT = {
    "type": "object",
    "required": ["fields", "limit", "factory", "organization", "date_from", "date_to", "aggregation"],
    "properties": {
        "fields": {"type": "array", "items": {"type": "string", "enum": ["id", "status"]}, "minItems": 1, "maxItems": 2, "uniqueItems": True},
        "limit": {"type": "integer", "maximum": 10},
        "factory": {"const": "SYNTHETIC-F01"},
        "organization": {"enum": ["SYNTHETIC-ORG"]},
        "date_from": {"format": "date", "const": "2026-11-01"},
        "date_to": {"format": "date", "const": "2026-11-14"},
        "aggregation": {"enum": ["count", "sum"]},
    },
    "additionalProperties": False,
}
VALID = {"fields": ["id", "status"], "limit": 5, "factory": "SYNTHETIC-F01",
         "organization": "SYNTHETIC-ORG", "date_from": "2026-11-01", "date_to": "2026-11-14", "aggregation": "count"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="synthetic", runtime_type="harness"))
    user = service.create_user({"username": "owner", "display_name": "Owner", "role": "admin", "password": "synthetic-only-password"})
    connection = service.save_connection({"name": "Synthetic MCP", "url": "http://localhost:18999/mcp"}, user=user)
    service.db.execute("UPDATE connections SET status='connected',tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": REMOTE}]), connection["id"]))
    binding = {"connection_id": connection["id"], "allowed_tools": ["read_orders"], "auto_approved_tools": ["read_orders"],
               "argument_constraints": {"read_orders": copy.deepcopy(CONSTRAINT)},
               "read_only_confirmations": {"read_orders": {"connection_revision": connection["revision"], "definition_digest": definition_digest({"name": "read_orders", "inputSchema": REMOTE}), "basis": "Synthetic query-only fixture reviewed by the host"}}}
    assistant = service.save_assistant({"name": "Synthetic assistant", "connections": [binding]}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Synthetic query"}, user)
    yield SimpleNamespace(service=service, user=user, connection=connection, binding=binding, assistant=assistant,
                          conversation=conversation, tool=f"mcp__c{connection['id']}__read_orders")
    service.db.close()


def prepare(env):
    value, _ = env.service.prepare_run(env.conversation["id"], {"text": "Synthetic query", "file_ids": [], "client_request_id": "constraint-test"}, env.user)
    return value


def patch_binding(env, binding):
    return env.service.save_assistant({"connections": [binding]}, env.assistant["id"], user=env.user)


def assert_error(code, action):
    with pytest.raises(AppError) as error:
        action()
    assert error.value.code == code
    return error.value


def test_request_model_and_service_preserve_constraints_on_unrelated_patch(env):
    parsed = AssistantCreate(name="Fixture", connections=[env.binding]).model_dump()
    assert parsed["connections"][0]["argument_constraints"] == {"read_orders": CONSTRAINT}
    changed = env.service.save_assistant({"description": "Only the description changed"}, env.assistant["id"], user=env.user)
    assert changed["connections"] == env.assistant["connections"]
    before = copy.deepcopy(VALID)
    assert env.service.authorize_tool(env.conversation["id"], env.tool, VALID) is True
    assert VALID == before, "Constraints must never fill defaults or rewrite input"


@pytest.mark.parametrize("change", [
    {"fields": ["id", "private_cost"]}, {"limit": 11}, {"limit": "5"},
    {"factory": "OTHER"}, {"organization": "OTHER"}, {"date_to": "2026-12-14"},
    {"aggregation": "none"}, {"unknown": True},
])
def test_fields_range_count_and_remote_schema_are_enforced(env, change):
    args = {**VALID, **change}
    assert_error("TOOL_ARGUMENT_CONSTRAINT_VIOLATION", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, args))


@pytest.mark.parametrize("field", ["fields", "limit", "factory", "organization", "date_from", "date_to"])
def test_required_scope_cannot_be_omitted(env, field):
    args = {key: value for key, value in VALID.items() if key != field}
    assert_error("TOOL_ARGUMENT_CONSTRAINT_VIOLATION", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, args))


@pytest.mark.parametrize("schema", [
    {}, {"type": "array"}, {"type": "object", "$ref": "https://example.invalid/schema"},
    {"type": "object", "$ref": "#"}, {"type": "object", "default": {}},
    {"type": "object", "properties": {"factory": {"pattern": "(a+)+$"}}},
    {"type": "object", "required": "factory"},
    {"type": "object", "properties": {"factory": {"format": "uri"}}},
    {"type": "object", "description": "x" * 33000},
])
def test_invalid_or_unbounded_policy_is_rejected_without_saving(env, schema):
    binding = {**env.binding, "argument_constraints": {"read_orders": schema}}
    assert_error("TOOL_CONSTRAINT_INVALID", lambda: patch_binding(env, binding))
    assert env.service.require("assistants", env.assistant["id"])["revision"] == env.assistant["revision"]


def test_unbound_and_unknown_parameters_cannot_claim_control(env):
    binding = {**env.binding, "argument_constraints": {"not_bound": CONSTRAINT}}
    assert_error("TOOL_CONSTRAINT_UNBOUND", lambda: patch_binding(env, binding))
    binding["argument_constraints"] = {"read_orders": {"type": "object", "required": ["guessed_factory"]}}
    assert_error("TOOL_CONSTRAINT_PARAMETER_UNKNOWN", lambda: patch_binding(env, binding))


@pytest.mark.parametrize("remote", [None, {}, {"type": "object", "$ref": "https://example.invalid/schema"}])
def test_unverified_remote_schema_cannot_enable_policy(env, remote):
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": remote}]), env.connection["id"]))
    assert_error("TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", lambda: patch_binding(env, env.binding))


@pytest.mark.parametrize("status", ["untested", "failed", "needs_auth"])
def test_unconnected_source_cannot_save_or_execute_constraint(env, status):
    env.service.db.execute("UPDATE connections SET status=? WHERE id=?", (status, env.connection["id"]))
    assert_error("TOOL_CONSTRAINT_CONNECTION_UNVERIFIED", lambda: patch_binding(env, env.binding))
    assert_error("TOOL_CONSTRAINT_CONNECTION_UNVERIFIED", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, VALID))


@pytest.mark.parametrize("location", ["policy", "remote"])
def test_transport_secret_cannot_enter_constraint_schema(env, location):
    secret = "synthetic-connection-secret-please-redact"
    env.service.db.execute("UPDATE connections SET secret=? WHERE id=?", (env.service.secrets.encrypt(secret), env.connection["id"]))
    binding = copy.deepcopy(env.binding)
    if location == "policy":
        binding["argument_constraints"]["read_orders"]["properties"]["factory"] = {"const": secret}
    else:
        remote = {**REMOTE, "description": secret}
        env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": remote}]), env.connection["id"]))
    error = assert_error("TOOL_CONSTRAINT_SECRET", lambda: patch_binding(env, binding))
    assert secret not in str(error) and secret not in error.message


@pytest.mark.parametrize("change", ["add", "remove", "loosen", "tighten"])
def test_existing_run_cannot_reuse_changed_constraint_or_prior_approval(env, change):
    binding = copy.deepcopy(env.binding)
    if change == "add":
        patch_binding(env, {**binding, "argument_constraints": {}})
    prepare(env)
    if change == "remove":
        binding["argument_constraints"] = {}
    elif change == "loosen":
        binding["argument_constraints"]["read_orders"]["properties"]["limit"]["maximum"] = 100
    elif change == "tighten":
        binding["argument_constraints"]["read_orders"]["properties"]["limit"]["maximum"] = 3
    patch_binding(env, binding)
    assert_error("TOOL_CONSTRAINT_CHANGED", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, VALID))


def test_discovery_schema_change_invalidates_pinned_run_even_without_revision_change(env):
    prepare(env)
    remote = copy.deepcopy(REMOTE)
    remote["properties"]["limit"]["maximum"] = 500
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": remote}]), env.connection["id"]))
    assert_error("TOOL_CONSTRAINT_SCHEMA_CHANGED", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, VALID))


@pytest.mark.parametrize("runtime_cls,runtime_type", [(SDKRuntime, "claude"), (HarnessRuntime, "harness")])
def test_both_runtime_guards_use_shared_constraint_boundary(env, runtime_cls, runtime_type):
    env.service.db.execute("UPDATE conversations SET runtime_type=? WHERE id=?", (runtime_type, env.conversation["id"]))
    prepare(env)
    runtime = runtime_cls(env.service, env.conversation["id"], env.service.runtime_config(env.conversation["id"]), None, None)
    assert runtime.check_tool(env.tool, VALID) is True
    assert_error("TOOL_ARGUMENT_CONSTRAINT_VIOLATION", lambda: runtime.check_tool(env.tool, {**VALID, "limit": 999}))


def test_unconstrained_legacy_bindings_keep_existing_behavior(env):
    binding = {key: value for key, value in env.binding.items() if key != "argument_constraints"}
    patch_binding(env, binding)
    prepare(env)
    assert env.service.authorize_tool(env.conversation["id"], env.tool, {"legacy": "not rewritten"}) is True


def node(env, args):
    return {"id": "read", "type": "mcp_read", "config": {"mode": "live", "tool_name": env.tool, "arguments_json": dump(args)}}


def workflow_scope(env, item):
    assistant = env.service.require("assistants", env.assistant["id"])
    return _workflow_tool_scope(env.service, assistant, env.user, env.conversation, [item])


def test_workflow_permission_inspection_allows_required_fields_but_execution_rejects_bad_arguments(env):
    item = node(env, {**VALID, "limit": 99})
    scope = workflow_scope(env, item)
    assert "read" in scope, "Permission inspection must not validate a fake empty request"
    with pytest.raises(AppError) as error:
        asyncio.run(_run_mcp_read(env.service, env.user, env.conversation, item, scope, {}, {}))
    assert error.value.code == "TOOL_ARGUMENT_CONSTRAINT_VIOLATION"


def fake_transport(monkeypatch, on_initialize=None):
    calls = []

    class Streams:
        async def __aenter__(self):
            return (None, None)

        async def __aexit__(self, *_):
            return False

    class Session(Streams):
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def initialize(self):
            if on_initialize:
                on_initialize()

        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[{"name": "read_orders", "inputSchema": REMOTE}], nextCursor=None)

        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, copy.deepcopy(arguments)))
            return SimpleNamespace(isError=False, model_dump=lambda mode: {"content": [{"type": "text", "text": "synthetic result"}]})

    monkeypatch.setattr("mcp.ClientSession", Session)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", lambda *a, **k: Streams())
    return calls


def test_valid_workflow_call_keeps_exact_arguments_and_no_live_transport(monkeypatch, env):
    item = node(env, VALID)
    scope = workflow_scope(env, item)
    calls = fake_transport(monkeypatch)
    result, _, _ = asyncio.run(_run_mcp_read(env.service, env.user, env.conversation, item, scope, {}, {}))
    assert calls == [("read_orders", VALID)] and result["status"] == "completed"


@pytest.mark.parametrize("moment", ["before", "during_initialize"])
def test_workflow_refuses_policy_relaxation_after_inspection(monkeypatch, env, moment):
    item = node(env, VALID)
    scope = workflow_scope(env, item)
    change = lambda: patch_binding(env, {**env.binding, "argument_constraints": {}})
    calls = fake_transport(monkeypatch, change if moment == "during_initialize" else None)
    if moment == "before":
        change()
    with pytest.raises(AppError) as error:
        asyncio.run(_run_mcp_read(env.service, env.user, env.conversation, item, scope, {}, {}))
    assert error.value.code == "TOOL_CONSTRAINT_CHANGED"
    assert calls == []


@pytest.mark.parametrize("literal", [{"guessed_factory": "F01"}, ["F01"]])
@pytest.mark.parametrize("keyword", ["const", "enum"])
def test_structured_literals_cannot_bypass_declared_parameter_check(env, literal, keyword):
    schema = {"type": "object", keyword: [literal] if keyword == "enum" else literal}
    assert_error("TOOL_CONSTRAINT_INVALID", lambda: patch_binding(env, {**env.binding, "argument_constraints": {"read_orders": schema}}))


def test_unique_items_requires_bounded_array_and_large_requests_fail_before_schema_validation(env, monkeypatch):
    schema = copy.deepcopy(CONSTRAINT)
    schema["properties"]["fields"].pop("maxItems")
    assert_error("TOOL_CONSTRAINT_INVALID", lambda: patch_binding(env, {**env.binding, "argument_constraints": {"read_orders": schema}}))

    from app.tool_constraints import _compiled_schemas
    # This request is only ~40 KiB, but unrestricted uniqueItems on objects is
    # quadratic. The structural budget must reject it before either validator.
    remote = {"type": "object", "properties": {"records": {"type": "array", "items": {"type": "object"}, "uniqueItems": True}}}
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": remote}]), env.connection["id"]))
    schema = {"type": "object", "properties": {"records": {"type": "array"}}}
    patch_binding(env, {**env.binding, "argument_constraints": {"read_orders": schema}})
    # All configs are already compiled. An unexpected runtime validation would
    # fail loudly rather than spending seconds checking the hostile collection.
    compiled = _compiled_schemas(json.dumps(schema, sort_keys=True, separators=(",", ":")), json.dumps(remote, sort_keys=True, separators=(",", ":")))
    reached = []
    def should_not_validate(*args, **kwargs):
        reached.append(True)
        raise AssertionError("large request reached a JSON schema validator")
    monkeypatch.setattr(type(compiled[0]), "validate", should_not_validate)
    args = {"records": [{"id": index} for index in range(3000)]}
    assert_error("TOOL_ARGUMENT_CONSTRAINT_VIOLATION", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, args))
    assert reached == []


@pytest.mark.parametrize("remote", [
    {"type": "object", "properties": {"query": {"type": "string", "pattern": "(a+)+$"}}},
    {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {"query": {"type": "string"}}, "dependencies": {"trigger": {"properties": {"query": {"pattern": "(a+)+$"}}}}},
    {"type": "object", "properties": {"query": {"type": "string"}}, "dependencies": {"trigger": {"properties": {"query": {"pattern": "(a+)+$"}}}}},
])
def test_remote_regex_or_unknown_dialect_is_not_executed_by_local_constraint_validation(env, remote):
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": remote}]), env.connection["id"]))
    assert_error("TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", lambda: patch_binding(env, {**env.binding, "argument_constraints": {"read_orders": {"type": "object"}}}))


def test_binding_total_size_is_bounded_even_when_each_schema_is_small(env):
    names = [f"tool_{index}" for index in range(40)]
    schema = {"type": "object", "description": "x" * 4000}
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": name, "inputSchema": REMOTE} for name in names]), env.connection["id"]))
    binding = {**env.binding, "allowed_tools": names, "auto_approved_tools": [], "argument_constraints": {name: schema for name in names}}
    assert_error("TOOL_CONSTRAINT_INVALID", lambda: patch_binding(env, binding))


def test_binding_constraint_count_is_bounded_even_with_tiny_schemas(env):
    names = [f"tool_{index}" for index in range(65)]
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": name, "inputSchema": REMOTE} for name in names]), env.connection["id"]))
    binding = {**env.binding, "allowed_tools": names, "auto_approved_tools": [], "argument_constraints": {name: {"type": "object"} for name in names}}
    assert_error("TOOL_CONSTRAINT_INVALID", lambda: patch_binding(env, binding))


def test_schema_cache_reuses_compilation_but_never_current_authority(env):
    from app.tool_constraints import _compiled_schemas
    _compiled_schemas.cache_clear()
    assert env.service.authorize_tool(env.conversation["id"], env.tool, VALID)
    first = _compiled_schemas.cache_info()
    assert env.service.authorize_tool(env.conversation["id"], env.tool, VALID)
    second = _compiled_schemas.cache_info()
    assert first.misses == second.misses == 1 and second.hits > first.hits
    changed = copy.deepcopy(env.binding)
    changed["argument_constraints"]["read_orders"]["properties"]["limit"]["maximum"] = 3
    patch_binding(env, changed)
    assert_error("TOOL_ARGUMENT_CONSTRAINT_VIOLATION", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, VALID))
    assert _compiled_schemas.cache_info().misses == 2
    env.service.db.execute("UPDATE connections SET status='failed' WHERE id=?", (env.connection["id"],))
    assert_error("TOOL_CONSTRAINT_CONNECTION_UNVERIFIED", lambda: env.service.authorize_tool(env.conversation["id"], env.tool, VALID))


def test_workflow_approval_resume_rejects_discovery_change_without_connection_revision(client):
    service = client.app.state.service
    connection = client.post("/api/v1/connections", json={"name": "Synthetic orders", "url": "http://localhost:18999/mcp"}).json()
    service.db.execute("UPDATE connections SET status='connected',tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": REMOTE}]), connection["id"]))
    binding = {"connection_id": connection["id"], "allowed_tools": ["read_orders"], "auto_approved_tools": ["read_orders"], "argument_constraints": {"read_orders": CONSTRAINT}, "read_only_confirmations": {"read_orders": {"connection_revision": connection["revision"], "definition_digest": definition_digest({"name": "read_orders", "inputSchema": REMOTE}), "basis": "Synthetic read-only fixture"}}}
    response = client.post("/api/v1/assistants", json={"name": "Synthetic workflow", "connections": [binding]})
    assert response.status_code == 201, response.text
    assistant = response.json()
    conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
    tool = f"mcp__c{connection['id']}__read_orders"
    graph = {"nodes": [
        {"id": "approval", "type": "approval", "label": "先确认", "config": {}},
        {"id": "read", "type": "mcp_read", "label": "读取", "config": {"mode": "live", "tool_name": tool, "arguments_json": dump(VALID)}},
    ], "edges": [{"source": "approval", "target": "read"}]}
    response = client.post(f"/api/v1/assistants/{assistant['id']}/workflows", json={"name": "Policy snapshot", "graph": graph})
    assert response.status_code == 201, response.text
    workflow = response.json()
    assert client.post(f"/api/v1/workflows/{workflow['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/workflows/{workflow['id']}/runs", json={"conversation_id": conversation["id"]})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "awaiting_approval"
    assert run["permission_snapshot"]["tools"][0]["argument_constraints_digest"]
    before_revision = service.require("connections", connection["id"])["revision"]
    remote = copy.deepcopy(REMOTE)
    remote["properties"]["limit"]["maximum"] = 500
    service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": "read_orders", "inputSchema": remote}]), connection["id"]))
    assert service.require("connections", connection["id"])["revision"] == before_revision
    response = client.post(f"/api/v1/workflow-runs/{run['id']}/approvals/approval/decision", json={"decision": "approve"})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "READ_ONLY_CONFIRMATION_STALE"
