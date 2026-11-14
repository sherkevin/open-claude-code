"""Offline host-confirmation and workflow transport boundaries; no customer I/O."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.db import dump
from app.models import Binding
from app.read_only_tools import definition_digest, discovered_definition, tool_view
from app.security import AppError
from app.service import connection_view
from app.workflows import _run_mcp_read, _workflow_tool_scope
from test_api import client, configured

DEFINITION = {
    "name": "query_records", "description": "Synthetic lookup with no side effects",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False},
}
ARGUMENTS = {"query": "synthetic evidence"}


def error_code(code, operation):
    with pytest.raises(AppError) as caught:
        operation()
    assert caught.value.code == code
    return caught.value


@pytest.fixture
def env(client):
    service = client.app.state.service
    user = service.db.one("SELECT * FROM users WHERE username='admin'")
    connection = service.save_connection({"name": "Synthetic read-only source", "url": "http://localhost:18999/mcp"}, user=user)
    service.db.execute("UPDATE connections SET status='connected',tools=? WHERE id=?", (dump([DEFINITION]), connection["id"]))
    binding = {"connection_id": connection["id"], "allowed_tools": [DEFINITION["name"]], "auto_approved_tools": [DEFINITION["name"]]}
    assistant = service.save_assistant({"name": "Synthetic readonly host", "connections": [binding]}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Synthetic readonly"}, user)
    tool = f"mcp__c{connection['id']}__{DEFINITION['name']}"
    node = {"id": "read", "type": "mcp_read", "label": "合成读取", "config": {"mode": "live", "tool_name": tool, "arguments_json": dump(ARGUMENTS)}}
    return SimpleNamespace(client=client, service=service, user=user, connection=connection, assistant=assistant,
                           binding=binding, conversation=conversation, node=node, tool=tool)


def connection_row(env):
    return env.service.require("connections", env.connection["id"])


def save_binding(env, binding):
    result = env.service.save_assistant({"connections": [binding]}, env.assistant["id"], user=env.user)
    env.binding = copy.deepcopy(binding)
    return result


def record(env, definition=DEFINITION):
    return {"connection_revision": connection_row(env)["revision"], "definition_digest": definition_digest(definition),
            "basis": "Host reviewed the synthetic query-only implementation and readonly account"}


def confirm(env, definition=DEFINITION):
    return save_binding(env, {**env.binding, "read_only_confirmations": {DEFINITION["name"]: record(env, definition)}})


def scope(env):
    assistant = env.service.require("assistants", env.assistant["id"])
    return _workflow_tool_scope(env.service, assistant, env.user, env.conversation, [env.node])


def call(env, tool_scope):
    return asyncio.run(_run_mcp_read(env.service, env.user, env.conversation, env.node, tool_scope, {}, {}))


def fake_transport(monkeypatch, *, definition=DEFINITION, initialize=None, listing=None, after_call=None, response=None, failure=None):
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
            if initialize:
                initialize()
        async def list_tools(self, cursor=None):
            if listing:
                listing()
            return SimpleNamespace(tools=copy.deepcopy(definition if isinstance(definition, list) else [definition]), nextCursor=None)
        async def call_tool(self, name, arguments, **kwargs):
            calls.append((name, copy.deepcopy(arguments)))
            if failure:
                raise failure
            if after_call:
                after_call()
            body = response or {"content": [{"type": "text", "text": "synthetic result"}]}
            return SimpleNamespace(isError=False, model_dump=lambda mode: copy.deepcopy(body))
    monkeypatch.setattr("mcp.ClientSession", Session)
    monkeypatch.setattr("mcp.client.streamable_http.streamable_http_client", lambda *a, **k: Streams())
    return calls


def write_definition(env, definition):
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump(definition if isinstance(definition, list) else [definition]), env.connection["id"]))


@pytest.mark.parametrize("annotations", [None, {"readOnlyHint": True}, {"readOnlyHint": False}])
def test_auto_approval_or_remote_hint_never_substitutes_for_host_confirmation(env, monkeypatch, annotations):
    definition = {**DEFINITION, "annotations": annotations}
    write_definition(env, definition)
    calls = fake_transport(monkeypatch, definition=definition)
    error_code("WORKFLOW_READ_ONLY_UNCONFIRMED", lambda: scope(env))
    assert calls == []
    assert env.service.authorize_tool(env.conversation["id"], env.tool, ARGUMENTS) is True, "Normal permission semantics remain separate"


def test_explicit_host_confirmation_without_hint_is_allowed_but_never_grants_auto_approval(env, monkeypatch):
    definition = {key: value for key, value in DEFINITION.items() if key != "annotations"}
    write_definition(env, definition)
    confirm(env, definition)
    calls = fake_transport(monkeypatch, definition=definition)
    result, _, _ = call(env, scope(env))
    assert result["status"] == "completed" and calls == [(DEFINITION["name"], ARGUMENTS)]
    save_binding(env, {**env.binding, "auto_approved_tools": []})
    error_code("WORKFLOW_TOOL_NOT_AUTO_APPROVED", lambda: scope(env))
    assert env.service.authorize_tool(env.conversation["id"], env.tool, ARGUMENTS) is False


@pytest.mark.parametrize("annotations", [{"readOnlyHint": False}, {"destructiveHint": True}, {"readOnlyHint": True, "destructiveHint": True}])
def test_conflicting_remote_declarations_cannot_be_confirmed(env, annotations):
    definition = {**DEFINITION, "annotations": annotations}
    write_definition(env, definition)
    error_code("READ_ONLY_DECLARATION_CONFLICT", lambda: confirm(env, definition))
    assert tool_view(definition)["read_only_confirmation_eligible"] is False


@pytest.mark.parametrize("value", [1, "true", "false", None, [], {}])
def test_stored_non_boolean_annotations_are_unverified_not_positive_claims(value):
    # SDK parsing can coerce wire scalars. This checks stored unknown data, not
    # raw-wire guarantees. A hint is never the grant, even when SDK says True.
    item = {**DEFINITION, "annotations": {"readOnlyHint": value}, "definition_digest": "a" * 64}
    view = tool_view(item)
    assert view["read_only_confirmation_eligible"] is False and view["definition_digest"] is None


@pytest.mark.parametrize("definition", [
    {"name": DEFINITION["name"]}, {**DEFINITION, "inputSchema": {}},
    {**DEFINITION, "inputSchema": {"type": "object", "$ref": "https://example.invalid/unsafe"}},
    {**DEFINITION, "description": 123},
])
def test_missing_or_unverified_tool_definition_cannot_produce_confirmation_digest(definition):
    error_code("READ_ONLY_DEFINITION_UNVERIFIED", lambda: definition_digest(definition))


@pytest.mark.parametrize("tools", [[], [DEFINITION, DEFINITION]])
def test_unknown_or_duplicate_tool_never_executes(env, tools, monkeypatch):
    confirm(env)
    write_definition(env, tools)
    calls = fake_transport(monkeypatch)
    error_code("READ_ONLY_DEFINITION_UNVERIFIED", lambda: scope(env))
    assert calls == []


@pytest.mark.parametrize("field,value", [("connection_revision", True), ("connection_revision", 0), ("basis", " "), ("basis", "a" * 501), ("basis", " " * 501 + "x"), ("definition_digest", "unknown")])
def test_confirmation_input_is_strict_and_not_silently_coerced(env, field, value):
    confirmation = {**record(env), field: value}
    binding = {**env.binding, "read_only_confirmations": {DEFINITION["name"]: confirmation}}
    error_code("READ_ONLY_CONFIRMATION_INVALID", lambda: save_binding(env, binding))
    if not (field == "basis" and value == " "):
        with pytest.raises(ValidationError):
            Binding(**binding)


def test_confirmation_cannot_bind_unallowed_tool_and_unrelated_edits_preserve_it(env):
    invalid = {**env.binding, "read_only_confirmations": {"write_something": record(env)}}
    error_code("READ_ONLY_CONFIRMATION_INVALID", lambda: save_binding(env, invalid))
    confirm(env)
    before = copy.deepcopy(env.binding)
    result = env.service.save_assistant({"description": "Unrelated edit"}, env.assistant["id"], user=env.user)
    assert result["connections"][0] == before
    assert Binding(**before).model_dump()["read_only_confirmations"] == before["read_only_confirmations"]


@pytest.mark.parametrize("mutation", ["schema", "annotations", "description", "revision"])
def test_stale_page_confirmation_save_fails_and_existing_confirmation_is_inactive(env, mutation):
    old_record = record(env)
    confirm(env)
    definition = copy.deepcopy(DEFINITION)
    if mutation == "schema":
        definition["inputSchema"]["properties"]["query"]["maxLength"] = 100
    elif mutation == "annotations":
        definition["annotations"]["idempotentHint"] = True
    elif mutation == "description":
        definition["description"] += " changed"
    else:
        env.service.db.execute("UPDATE connections SET revision=revision+1 WHERE id=?", (env.connection["id"],))
    write_definition(env, definition)
    error_code("READ_ONLY_CONFIRMATION_STALE", lambda: scope(env))
    # Unchanged old records may be displayed/preserved; this never activates them.
    env.service.save_assistant({"description": "Still reviewing"}, env.assistant["id"], user=env.user)
    error_code("READ_ONLY_CONFIRMATION_STALE", lambda: scope(env))
    new_assistant = {"name": "Stale copied page", "connections": [{**env.binding, "read_only_confirmations": {DEFINITION["name"]: old_record}}]}
    error_code("READ_ONLY_CONFIRMATION_STALE", lambda: env.service.save_assistant(new_assistant, user=env.user))
    confirm(env, definition)
    assert scope(env)["read"]["definition_digest"] == definition_digest(definition)


@pytest.mark.parametrize("status", ["untested", "failed", "needs_auth"])
def test_unconnected_source_is_not_eligible_for_save_or_execution(env, status):
    env.service.db.execute("UPDATE connections SET status=? WHERE id=?", (status, env.connection["id"]))
    error_code("READ_ONLY_CONNECTION_UNVERIFIED", lambda: confirm(env))


@pytest.mark.parametrize("change", ["remove", "basis", "add"])
def test_active_run_cannot_reuse_changed_host_confirmation(env, change):
    if change != "add":
        confirm(env)
    env.service.prepare_run(env.conversation["id"], {"text": "Synthetic run", "file_ids": [], "client_request_id": "readonly-snapshot"}, env.user)
    if change == "remove":
        save_binding(env, {**env.binding, "read_only_confirmations": {}})
    else:
        confirm(env)
        if change == "basis":
            changed = copy.deepcopy(env.binding)
            changed["read_only_confirmations"][DEFINITION["name"]]["basis"] = "Different host rationale"
            save_binding(env, changed)
    error_code("READ_ONLY_CONFIRMATION_CHANGED", lambda: scope(env))


@pytest.mark.parametrize("moment", ["before", "initialize", "list_tools", "after_call"])
@pytest.mark.parametrize("change", ["remove", "schema", "revision", "archived"])
def test_async_boundaries_recheck_current_confirmation_and_connection(env, monkeypatch, moment, change):
    confirm(env)
    tool_scope = scope(env)
    def mutate():
        if change == "remove":
            save_binding(env, {**env.binding, "read_only_confirmations": {}})
        elif change == "schema":
            write_definition(env, {**DEFINITION, "description": "Definition changed in flight"})
        elif change == "revision":
            env.service.db.execute("UPDATE connections SET revision=revision+1 WHERE id=?", (env.connection["id"],))
        else:
            env.service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (env.conversation["id"],))
    calls = fake_transport(monkeypatch, initialize=mutate if moment == "initialize" else None,
                           listing=mutate if moment == "list_tools" else None, after_call=mutate if moment == "after_call" else None)
    if moment == "before":
        mutate()
    code = {"remove": "WORKFLOW_READ_ONLY_UNCONFIRMED", "schema": "READ_ONLY_CONFIRMATION_STALE", "revision": "READ_ONLY_CONFIRMATION_STALE", "archived": "WORKFLOW_SESSION_UNAVAILABLE"}[change]
    error_code(code, lambda: call(env, tool_scope))
    assert len(calls) == (1 if moment == "after_call" else 0), "No execution after a pre-call revocation, no result after a post-call revocation"


@pytest.mark.parametrize("change", ["schema", "annotations", "duplicate", "missing"])
def test_fresh_remote_definition_is_verified_before_call_tool(env, monkeypatch, change):
    confirm(env)
    definition = copy.deepcopy(DEFINITION)
    if change == "schema":
        definition["inputSchema"]["properties"]["query"]["maxLength"] = 100
    elif change == "annotations":
        definition["annotations"]["readOnlyHint"] = False
    else:
        definition = [definition, definition] if change == "duplicate" else []
    calls = fake_transport(monkeypatch, definition=definition)
    code = "READ_ONLY_DECLARATION_CONFLICT" if change == "annotations" else "READ_ONLY_CONFIRMATION_STALE"
    error_code(code, lambda: call(env, scope(env)))
    assert calls == []


def test_scope_cannot_be_reused_by_other_conversation_and_cancelled_task_does_not_call(env, monkeypatch):
    confirm(env)
    tool_scope = scope(env)
    other = env.service.create_conversation({"assistant_id": env.assistant["id"], "title": "Other"}, env.user)
    calls = fake_transport(monkeypatch)
    original = env.conversation
    env.conversation = other
    error_code("WORKFLOW_TOOL_SCOPE_INVALID", lambda: call(env, tool_scope))
    env.conversation = original
    def cancelled():
        raise asyncio.CancelledError()
    calls = fake_transport(monkeypatch, initialize=cancelled)
    with pytest.raises(asyncio.CancelledError):
        call(env, tool_scope)
    assert calls == []


def test_completed_active_task_cannot_continue_workflow(env, monkeypatch):
    confirm(env)
    env.service.prepare_run(env.conversation["id"], {"text": "Synthetic run", "file_ids": [], "client_request_id": "readonly-cancelled"}, env.user)
    tool_scope = scope(env)
    def cancel():
        env.service.db.execute("UPDATE runs SET status='cancelled' WHERE conversation_id=?", (env.conversation["id"],))
    calls = fake_transport(monkeypatch, initialize=cancel)
    error_code("WORKFLOW_SESSION_UNAVAILABLE", lambda: call(env, tool_scope))
    assert calls == []


def workflow(env, approval_first=False):
    nodes = [env.node]
    edges = []
    if approval_first:
        nodes = [{"id": "approval", "type": "approval", "label": "人工确认", "config": {}}, env.node]
        edges = [{"source": "approval", "target": "read"}]
    response = env.client.post(f"/api/v1/assistants/{env.assistant['id']}/workflows", json={"name": "Synthetic host confirmation", "graph": {"nodes": nodes, "edges": edges}})
    assert response.status_code == 201, response.text
    result = response.json()
    assert env.client.post(f"/api/v1/workflows/{result['id']}/publish").status_code == 200
    return result


@pytest.mark.parametrize("change", ["definition", "confirmation", "revision"])
def test_approval_resume_rejects_changed_definition_even_after_host_reconfirms(env, monkeypatch, change):
    confirm(env)
    wf = workflow(env, approval_first=True)
    response = env.client.post(f"/api/v1/workflows/{wf['id']}/runs", json={"conversation_id": env.conversation["id"]})
    assert response.status_code == 201, response.text
    run = response.json()
    assert run["status"] == "awaiting_approval"
    assert run["permission_snapshot"]["tools"][0]["read_only_confirmation_digest"]
    if change == "definition":
        definition = {**DEFINITION, "description": "Changed while approval pending"}
        write_definition(env, definition)
        # Reconfirmation may permit a NEW run; it cannot approve an OLD one.
        confirm(env, definition)
    elif change == "confirmation":
        changed = copy.deepcopy(env.binding)
        changed["read_only_confirmations"][DEFINITION["name"]]["basis"] = "New review by host"
        save_binding(env, changed)
    else:
        env.service.db.execute("UPDATE connections SET revision=revision+1 WHERE id=?", (env.connection["id"],))
        confirm(env)
    calls = fake_transport(monkeypatch)
    response = env.client.post(f"/api/v1/workflow-runs/{run['id']}/approvals/approval/decision", json={"decision": "approve"})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "WORKFLOW_PERMISSION_CHANGED"
    assert calls == []


def test_known_credential_is_removed_before_preview_truncation_sources_and_persistence(env, monkeypatch):
    secret = 'synthetic-secret-"quoted"-no-real-credential'
    env.service.db.execute("UPDATE connections SET auth_type='bearer',secret=? WHERE id=?", (env.service.secrets.encrypt(secret), env.connection["id"]))
    confirm(env)
    body = {"structuredContent": {"rows": [{"id": "R-1", "note": f"Bearer {secret}"}], "sources": [{"title": "Synthetic record", "excerpt": "Reviewed " + secret, "locator": {"table": "records"}}]},
            "content": [{"type": "text", "text": "x" * 11990 + secret}, {"type": "text", "text": json.dumps({"quoted": secret})}]}
    calls = fake_transport(monkeypatch, response=body)
    wf = workflow(env)
    response = env.client.post(f"/api/v1/workflows/{wf['id']}/runs", json={"conversation_id": env.conversation["id"]})
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "succeeded" and len(calls) == 1
    encoded = json.dumps(response.json(), ensure_ascii=False)
    assert secret not in encoded and "synthetic-secret-" not in encoded and "[redacted]" in encoded
    sources = response.json()["output"]["tool_evidence"]["read"]
    assert sources and sources[0]["title"] == "Synthetic record" and sources[0]["locator"]["table"] == "records"
    rows = env.service.db.all("SELECT * FROM workflow_tool_evidence WHERE workflow_run_id=?", (response.json()["id"],))
    persisted = dump(rows + [env.service.db.one("SELECT * FROM workflow_runs WHERE id=?", (response.json()["id"],))])
    assert secret not in persisted and "synthetic-secret-" not in persisted


def test_remote_exception_never_propagates_transport_secret(env, monkeypatch):
    confirm(env)
    secret = "synthetic-secret-do-not-propagate"
    calls = fake_transport(monkeypatch, failure=RuntimeError(f"Bearer {secret}"))
    error = error_code("WORKFLOW_TOOL_FAILED", lambda: call(env, scope(env)))
    assert len(calls) == 1 and secret not in str(error) and error.__suppress_context__ is True


def test_digest_excludes_generated_fields_and_discovery_round_trip_preserves_annotations():
    from mcp.types import Tool
    parsed = Tool(**DEFINITION)
    saved = discovered_definition(parsed)
    view = tool_view(saved)
    assert saved["annotations"] == DEFINITION["annotations"]
    assert definition_digest(parsed) == definition_digest(view) == definition_digest(saved)


def test_connection_view_exposes_current_fingerprint_but_no_implicit_confirmation(env):
    view = connection_view(connection_row(env), env.user)
    item = view["tools"][0]
    assert item["definition_digest"] == definition_digest(DEFINITION)
    assert item["read_only_confirmation_eligible"] is True
    assert not env.binding.get("read_only_confirmations")


def test_cancel_that_clears_active_run_pointer_invalidates_inspected_scope(env, monkeypatch):
    confirm(env)
    env.service.prepare_run(env.conversation["id"], {"text": "Synthetic run", "file_ids": [], "client_request_id": "readonly-clear-active"}, env.user)
    tool_scope = scope(env)
    def cancel():
        env.service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (env.conversation["id"],))
    calls = fake_transport(monkeypatch, initialize=cancel)
    error_code("WORKFLOW_SESSION_UNAVAILABLE", lambda: call(env, tool_scope))
    assert calls == []


@pytest.mark.parametrize("mode", ["duplicate_cross_page", "too_many_pages", "too_many_tools", "revoked_between_pages"])
def test_live_paginated_discovery_is_bounded_and_rechecks_each_page(mode):
    from app.read_only_tools import verify_live_definition
    seen, revoked = [], False
    class Session:
        async def list_tools(self, cursor=None):
            nonlocal revoked
            seen.append(cursor)
            if mode == "duplicate_cross_page":
                return SimpleNamespace(tools=[DEFINITION], nextCursor="second" if cursor is None else None)
            if mode == "too_many_tools":
                return SimpleNamespace(tools=[DEFINITION] * 1001, nextCursor=None)
            if mode == "revoked_between_pages" and cursor:
                revoked = True
            return SimpleNamespace(tools=[DEFINITION] if cursor is None else [], nextCursor="next")
    def recheck():
        if revoked:
            raise AppError(403, "TOOL_DENIED", "revoked")
    expected = {"duplicate_cross_page": "READ_ONLY_CONFIRMATION_STALE", "too_many_pages": "READ_ONLY_CONFIRMATION_STALE", "too_many_tools": "READ_ONLY_DEFINITION_UNVERIFIED", "revoked_between_pages": "TOOL_DENIED"}[mode]
    error_code(expected, lambda: asyncio.run(verify_live_definition(Session(), DEFINITION["name"], definition_digest(DEFINITION), recheck)))
    assert len(seen) <= 20


def test_confirmation_basis_cannot_persist_known_transport_credentials(env):
    secret = "synthetic-confirmation-secret"
    env.service.db.execute("UPDATE connections SET auth_type='bearer',secret=? WHERE id=?", (env.service.secrets.encrypt(secret), env.connection["id"]))
    binding = {**env.binding, "read_only_confirmations": {DEFINITION["name"]: {**record(env), "basis": "Checked Bearer " + secret}}}
    error_code("READ_ONLY_CONFIRMATION_INVALID", lambda: save_binding(env, binding))
    assert secret not in env.service.require("assistants", env.assistant["id"])["connections"]


def test_definition_cache_is_bounded_hash_only_and_mutation_changes_key():
    from app.read_only_tools import _checked_digests, _digest_lock
    with _digest_lock:
        _checked_digests.clear()
    for index in range(70):
        definition_digest({**DEFINITION, "description": f"synthetic cache sample {index}"})
    with _digest_lock:
        assert len(_checked_digests) == 64
        assert all(len(key) == 64 and value is None for key, value in _checked_digests.items())
    assert definition_digest(DEFINITION) != definition_digest({**DEFINITION, "description": "changed"})


@pytest.mark.parametrize("mutation,code", [
    ("allowed", "TOOL_DENIED"), ("automatic", "WORKFLOW_TOOL_NOT_AUTO_APPROVED"),
    ("credential", "READ_ONLY_CONNECTION_UNVERIFIED"), ("connection_status", "READ_ONLY_CONNECTION_UNVERIFIED"),
    ("user_disabled", "USER_DISABLED"),
])
def test_initialized_session_cannot_outlive_current_permission_or_credential(env, monkeypatch, mutation, code):
    confirm(env)
    tool_scope = scope(env)
    def mutate():
        if mutation == "allowed":
            save_binding(env, {**env.binding, "allowed_tools": [], "auto_approved_tools": [], "read_only_confirmations": {}})
        elif mutation == "automatic":
            save_binding(env, {**env.binding, "auto_approved_tools": []})
        elif mutation == "credential":
            env.service.save_connection({"auth_type": "bearer", "credential": "synthetic-rotated-credential"}, env.connection["id"], user=env.user)
        elif mutation == "connection_status":
            env.service.db.execute("UPDATE connections SET status='failed' WHERE id=?", (env.connection["id"],))
        else:
            env.service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (env.user["id"],))
    calls = fake_transport(monkeypatch, initialize=mutate)
    error_code(code, lambda: call(env, tool_scope))
    assert calls == []
