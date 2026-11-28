"""Synthetic checks for the single-account Huawi R3 composition (HW-037e).

Only the local FastAPI app, the committed declarative SKILL.md candidate and
system-owned synthetic fixtures are used.  No model, real MCP server, Huawi
account, credential, customer answer, production deployment or network endpoint
is contacted.  The tests encode the intended composition as executable
assertions; they do not claim a real end-to-end acceptance run.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.security import AppError
from test_api import PASSWORD, client, configured, login, poll  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
COMMITTED_SKILL = ROOT / "examples" / "huawi-parity-readonly" / "SKILL.md"
OPTIONAL_MARKER = "受限通用规则候选"

# Stand-in for the account owner's approved basic rules.  It is deliberately
# distinct from the optional Skill body so the test can prove the two channels
# are not silently copied into one another.
CORE_INSTRUCTIONS = (
    "你是华微只读业务助手（合成账户预演）。\n"
    "核心规则 CORE-SYNTHETIC-01：只使用本助手已绑定的只读工具；"
    "任何业务工具调用都遵循当前人工审批策略，不把建议写成已经执行；"
    "无法核实的数据、范围或权限必须明确说明。"
)
CORE_MARKER = "CORE-SYNTHETIC-01"

SYNTHETIC_TOOLS = [
    {
        "name": "describe",
        "description": "Synthetic metadata discovery; no live schema is claimed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string"},
                "keyword": {"type": "string"},
                "kind": {"type": "string"},
                "page": {"type": "integer"},
                "rows": {"type": "integer"},
            },
        },
    },
    {
        "name": "query",
        "description": "Synthetic read-only query; no live dataset is claimed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset": {"type": "string"},
                "select": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "aggregate": {"type": "string"}, "alias": {"type": "string"}}, "required": ["field"]}},
                "filters": {"type": "array", "items": {"type": "object"}},
                "group_by": {"type": "array", "items": {"type": "string"}},
                "include_total": {"type": "boolean"},
                "order_by": {"type": "array", "items": {"type": "object"}},
                "page": {"type": "integer"},
                "rows": {"type": "integer"},
            },
            "required": ["dataset", "select"],
        },
    },
    {
        "name": "call",
        "description": "Synthetic action entry point; never authorized here.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["action"],
        },
    },
]


async def synthetic_discovery(_row):
    return json.loads(json.dumps(SYNTHETIC_TOOLS))


def member(client, username):
    response = client.post(
        "/api/v1/users",
        json={
            "username": username,
            "password": PASSWORD,
            "display_name": username,
            "role": "member",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def import_committed_skill(client):
    response = client.post(
        "/api/v1/skills/import",
        json={
            "markdown": COMMITTED_SKILL.read_text(encoding="utf-8"),
            "scope": "private",
        },
    )
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["name"] == "huawi-parity-readonly"
    assert skill["required_tools"] == []
    assert skill["scope"] == "private"
    return skill


def private_bundle(client, *, with_skill=False, username="huawi-owner"):
    """Create the same-owner private synthetic bundle through public APIs."""
    member(client, username)
    login(client, username)
    service = client.app.state.service
    connection_response = client.post(
        "/api/v1/connections",
        json={
            "name": "Synthetic private read-only MCP",
            "url": "http://localhost:8932/mcp",
            "auth_type": "bearer",
            "credential": "synthetic-placeholder-not-a-credential",
            "scope": "private",
        },
    )
    assert connection_response.status_code == 201, connection_response.text
    connection = connection_response.json()
    service.discover = synthetic_discovery
    tested = client.post(f"/api/v1/connections/{connection['id']}/test")
    assert tested.status_code == 200, tested.text
    connection = tested.json()
    assert connection["status"] == "connected"

    skill = import_committed_skill(client) if with_skill else None
    assistant_body = {
        "name": "Synthetic Huawi read-only assistant",
        "instructions": CORE_INSTRUCTIONS,
        "scope": "private",
        "connections": [
            {
                "connection_id": connection["id"],
                "allowed_tools": ["describe", "query"],
                "auto_approved_tools": [],
            }
        ],
    }
    if skill is not None:
        assistant_body["skill_ids"] = [skill["id"]]
    assistant_response = client.post("/api/v1/assistants", json=assistant_body)
    assert assistant_response.status_code == 201, assistant_response.text
    assistant = assistant_response.json()

    conversation_response = client.post(
        "/api/v1/conversations", json={"assistant_id": assistant["id"]}
    )
    assert conversation_response.status_code == 201, conversation_response.text
    conversation = conversation_response.json()
    owner = service.db.one("SELECT * FROM users WHERE username=?", (username,))
    return {
        "service": service,
        "owner": owner,
        "connection": connection,
        "skill": skill,
        "assistant": assistant,
        "conversation": conversation,
        "prefix": f"mcp__c{connection['id']}__",
    }


def send_message(client, conversation_id, request_id, text, skill_ids=None):
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "client_request_id": request_id,
            "text": text,
            "file_ids": [],
            "skill_ids": skill_ids or [],
        },
    )
    assert response.status_code == 202, response.text
    result = response.json()
    completed = poll(client, result["run_id"])
    assert completed["status"] == "succeeded", completed
    return result


def run_row(bundle, run_id):
    return bundle["service"].require("runs", run_id)


def test_same_owner_private_connection_is_manual_read_only_without_auto_approval(
    client,
):
    bundle = private_bundle(client)
    connection = bundle["connection"]
    assistant = bundle["assistant"]
    conversation = bundle["conversation"]

    assert connection["scope"] == "private"
    assert connection["owner_id"] == bundle["owner"]["id"]
    assert "synthetic-placeholder-not-a-credential" not in json.dumps(connection)
    tools = {tool["name"]: tool for tool in connection["tools"]}
    assert set(tools) == {"describe", "query", "call"}
    describe_schema = tools["describe"]["inputSchema"]
    assert set(describe_schema["properties"]) == {
        "code",
        "keyword",
        "kind",
        "page",
        "rows",
    }
    assert "required" not in describe_schema
    query_schema = tools["query"]["inputSchema"]
    assert query_schema["required"] == ["dataset", "select"]
    assert set(query_schema["properties"]) == {
        "dataset",
        "select",
        "filters",
        "group_by",
        "include_total",
        "order_by",
        "page",
        "rows",
    }

    binding = assistant["connections"][0]
    assert binding["connection_id"] == connection["id"]
    assert binding["allowed_tools"] == ["describe", "query"]
    assert binding["auto_approved_tools"] == []
    assert binding["argument_constraints"] == {}
    assert assistant["scope"] == "private"
    assert assistant["owner_id"] == bundle["owner"]["id"]
    assert conversation["permission_mode"] == "manual"
    assert "manual" in conversation["permission_modes"]

    service = bundle["service"]
    conversation_id = conversation["id"]
    assert service.authorize_tool(conversation_id, bundle["prefix"] + "describe", {}) is False
    assert service.authorize_tool(conversation_id, bundle["prefix"] + "query", {}) is False
    with pytest.raises(AppError) as denied:
        service.authorize_tool(
            conversation_id, bundle["prefix"] + "call", {"action": "synthetic_noop"}
        )
    assert denied.value.code == "TOOL_DENIED"


def test_core_instructions_are_effective_and_optional_skill_is_never_implicit(client):
    bundle = private_bundle(client, with_skill=True)
    skill = bundle["skill"]
    assistant_id = bundle["assistant"]["id"]
    conversation_id = bundle["conversation"]["id"]

    # Configured skill_ids is availability only, not a per-turn selection.
    available = client.get(f"/api/v1/assistants/{assistant_id}/skills")
    assert available.status_code == 200, available.text
    assert [item["id"] for item in available.json()] == [skill["id"]]
    configured = client.get(f"/api/v1/assistants/{assistant_id}").json()
    assert configured["instructions"] == CORE_INSTRUCTIONS
    assert configured["skill_ids"] == [skill["id"]]

    # A run without an explicit selection has core instructions only.
    first = send_message(client, conversation_id, "hw037e-core-0001", "合成只读问题一")
    first_run = run_row(bundle, first["run_id"])
    first_snapshot = json.loads(first_run["capability_snapshot"])
    assert first_snapshot["instructions"] == CORE_INSTRUCTIONS
    assert json.loads(first_run["skill_snapshots"]) == []
    first_messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    ).json()
    first_user = next(
        item
        for item in first_messages
        if item["run_id"] == first["run_id"] and item["role"] == "user"
    )
    assert first_user["skills"] == []

    # Selecting the Skill adds a per-turn snapshot; it never rewrites core.
    second = send_message(
        client,
        conversation_id,
        "hw037e-core-0002",
        "合成只读问题二",
        skill_ids=[skill["id"]],
    )
    second_run = run_row(bundle, second["run_id"])
    snapshots = json.loads(second_run["skill_snapshots"])
    assert [item["id"] for item in snapshots] == [skill["id"]]
    assert snapshots[0]["name"] == "huawi-parity-readonly"
    assert snapshots[0]["revision"] == 1
    assert OPTIONAL_MARKER in snapshots[0]["instructions"]
    assert CORE_MARKER not in snapshots[0]["instructions"]
    assert json.loads(second_run["capability_snapshot"])["instructions"] == CORE_INSTRUCTIONS
    second_messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    ).json()
    second_user = next(
        item
        for item in second_messages
        if item["run_id"] == second["run_id"] and item["role"] == "user"
    )
    assert second_user["skills"] == [
        {
            "id": skill["id"],
            "name": "huawi-parity-readonly",
            "revision": 1,
        }
    ]
    assert client.get(f"/api/v1/assistants/{assistant_id}").json()[
        "instructions"
    ] == CORE_INSTRUCTIONS


def test_skill_versions_and_revocation_guard_explicit_selection(client):
    bundle = private_bundle(client, with_skill=True)
    skill = bundle["skill"]
    conversation_id = bundle["conversation"]["id"]
    original = skill["instructions"]

    first = send_message(
        client,
        conversation_id,
        "hw037e-version-0001",
        "合成只读问题三",
        skill_ids=[skill["id"]],
    )
    first_run = run_row(bundle, first["run_id"])
    assert json.loads(first_run["skill_snapshots"])[0]["revision"] == 1

    patched_text = original + "\n\n## 合成补充 v2\n仅用于本地版本测试。"
    patched = client.patch(
        f"/api/v1/skills/{skill['id']}", json={"instructions": patched_text}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["revision"] == 2

    versions = client.get(f"/api/v1/skills/{skill['id']}/versions").json()
    assert [item["revision"] for item in versions] == [2, 1]
    version_one = next(item for item in versions if item["revision"] == 1)
    assert version_one["instructions"] == original

    # A finished run keeps the exact version it selected.
    first_run = bundle["service"].require("runs", first["run_id"])
    assert json.loads(first_run["skill_snapshots"])[0]["instructions"] == original

    # Disabling the Skill removes it from availability; explicit selection is
    # refused before any message or run row is written.
    disabled = client.patch(
        f"/api/v1/skills/{skill['id']}", json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    before_messages = client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    ).json()
    refused = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "client_request_id": "hw037e-version-0002",
            "text": "合成只读问题四",
            "file_ids": [],
            "skill_ids": [skill["id"]],
        },
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "SKILL_UNAVAILABLE"
    assert client.get(f"/api/v1/conversations/{conversation_id}/messages").json() == before_messages


def test_cross_owner_private_bundle_is_denied(client):
    # The fixture is logged in as admin here; create the second member first.
    member(client, "huawi-other")
    bundle = private_bundle(client, with_skill=True)
    connection = bundle["connection"]
    skill = bundle["skill"]
    assistant = bundle["assistant"]
    conversation = bundle["conversation"]

    login(client, "huawi-other")
    for kind, item_id in (
        ("connections", connection["id"]),
        ("skills", skill["id"]),
        ("assistants", assistant["id"]),
    ):
        assert item_id not in {row["id"] for row in client.get(f"/api/v1/{kind}").json()}
        assert client.get(f"/api/v1/{kind}/{item_id}").status_code == 404
        assert (
            client.patch(f"/api/v1/{kind}/{item_id}", json={"name": "stolen"}).status_code
            == 404
        )
        assert client.delete(f"/api/v1/{kind}/{item_id}").status_code == 404
    assert client.get(f"/api/v1/skills/{skill['id']}/versions").status_code == 404
    assert (
        client.post(
            "/api/v1/assistants",
            json={"name": "Synthetic stolen binding", "skill_ids": [skill["id"]]},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/conversations", json={"assistant_id": assistant["id"]}
        ).status_code
        == 404
    )
    assert client.get(f"/api/v1/conversations/{conversation['id']}").status_code == 404
    assert (
        client.post(
            f"/api/v1/conversations/{conversation['id']}/messages",
            json={
                "client_request_id": "hw037e-cross-0001",
                "text": "synthetic",
                "file_ids": [],
            },
        ).status_code
        == 404
    )

    # Private resources are owner-only even for the administrator account.
    login(client, "admin")
    assert client.get(f"/api/v1/connections/{connection['id']}").status_code == 404
    assert client.get(f"/api/v1/skills/{skill['id']}").status_code == 404
    assert client.get(f"/api/v1/assistants/{assistant['id']}").status_code == 404

    login(client, "huawi-owner")
    assert client.get(f"/api/v1/connections/{connection['id']}").status_code == 200


def test_assistant_and_connection_revisions_block_conflicts_and_pin_the_run(client):
    bundle = private_bundle(client)
    service = bundle["service"]
    owner = service.db.one("SELECT * FROM users WHERE username='huawi-owner'")
    assistant = bundle["assistant"]
    connection = bundle["connection"]
    conversation_id = bundle["conversation"]["id"]

    prepared, created = service.prepare_run(
        conversation_id,
        {
            "client_request_id": "hw037e-pin-0001",
            "text": "合成只读问题五",
            "file_ids": [],
        },
        owner,
    )
    assert created is True
    run = service.require("runs", prepared["run_id"])
    snapshot = json.loads(run["capability_snapshot"])
    assert snapshot["instructions"] == CORE_INSTRUCTIONS
    assert snapshot["connection_revisions"][connection["id"]] == connection["revision"]
    assert json.loads(run["skill_snapshots"]) == []

    # An in-flight run keeps its submitted instructions.
    config = service.runtime_config(conversation_id)
    assert config["instructions"] == CORE_INSTRUCTIONS
    assert config["skill_snapshots"] == []
    updated = client.patch(
        f"/api/v1/assistants/{assistant['id']}",
        json={
            "instructions": "核心规则 v2（合成）",
            "expected_revision": assistant["revision"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == assistant["revision"] + 1
    assert service.runtime_config(conversation_id)["instructions"] == CORE_INSTRUCTIONS

    # A stale editor cannot silently overwrite the newer revision.
    stale = client.patch(
        f"/api/v1/assistants/{assistant['id']}",
        json={
            "instructions": "过期覆盖（合成）",
            "expected_revision": assistant["revision"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "ASSISTANT_CONFLICT"
    assert (
        client.get(f"/api/v1/assistants/{assistant['id']}").json()["instructions"]
        == "核心规则 v2（合成）"
    )

    # Changing the pinned connection revision invalidates the prepared run.
    service.save_connection(
        {"url": "http://localhost:8933/mcp"}, connection["id"], user=owner
    )
    with pytest.raises(AppError) as changed:
        service.runtime_config(conversation_id)
    assert changed.value.code == "CONFIG_CHANGED"
    service.manager.finish(prepared["run_id"], "cancelled")
