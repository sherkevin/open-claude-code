"""Trusted contract admission uses synthetic local MCP, never customer data."""
import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import dump, now
from app.read_only_tools import definition_digest
from app.trusted_mcp_read import connection_identity, trusted_read_decision, POLICY_FILENAME
from test_harness_mcp_tools import (system, mcp_server, new_run, request,
                                   pending_approval, completed, canonical)


def enroll(service, connection, names=("read_records",)):
    row = service.require("connections", connection["id"])
    tools = {t["name"]: definition_digest(t) for t in json.loads(row["tools"]) if t["name"] in names}
    policy = {"version": 1, "connections": {row["id"]: {
        "identity": connection_identity(row), "policy_id": "synthetic-reviewed-contract-v1",
        "registered_at": now(), "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "tools": tools}}}
    path = service.settings.data_dir / POLICY_FILENAME
    path.write_text(json.dumps(policy))
    path.chmod(0o600)
    return path, policy, row


def fresh_contract(system, names=("read_records",)):
    service = system.service
    other = service.save_connection({"name": "Trusted synthetic MCP", "url": system.server.url,
        "auth_type": "none"}, user=system.owner)
    # Synthetic transport expects the existing fixture credential. Only copy
    # fixture-local encrypted data; no production credentials or connections.
    original = service.require("connections", system.connection["id"])
    service.db.execute("UPDATE connections SET status='connected',tools=?,auth_type=?,secret=? WHERE id=?",
                       (original["tools"], original["auth_type"], original["secret"], other["id"]))
    service.save_assistant({"connections": [{"connection_id": other["id"],
        "allowed_tools": ["read_records", "write_record"], "auto_approved_tools": ["write_record"]}]},
        system.assistant["id"], user=system.owner)
    system.connection = other
    return enroll(service, other, names)


def test_trusted_contract_identity_definition_permission_and_revocation(system):
    path, policy, row = fresh_contract(system, ("read_records", "write_record"))
    assert trusted_read_decision(system.service, row, "read_records", now())["scope"] == "trusted_read_contract"
    # A positive policy entry cannot authorize a declared write.
    assert trusted_read_decision(system.service, row, "write_record", now()) is None
    for field, value in [("id", "different"), ("revision", row["revision"] + 1),
                         ("owner_id", "different"), ("url", row["url"] + "/other"),
                         ("secret", "different"), ("auth_type", "none"),
                         ("status", "failed"), ("enabled", 0), ("read_only_review_revision", 1)]:
        assert trusted_read_decision(system.service, {**row, field: value}, "read_records", now()) is None
    tools = json.loads(row["tools"])
    tools[0]["description"] += " changed"
    assert trusted_read_decision(system.service, {**row, "tools": dump(tools)}, "read_records", now()) is None
    path.chmod(0o644)
    assert trusted_read_decision(system.service, row, "read_records", now()) is None
    path.chmod(0o600)
    path.write_text("{}")
    assert trusted_read_decision(system.service, row, "read_records", now()) is None


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_trusted_read_contract_controls_actual_mcp_dispatch(system, mode):
    path, policy, row = fresh_contract(system)
    new_run(system, permission_mode=mode)

    async def scenario():
        task = asyncio.create_task(system.runtime.bridge.invoke(request(system)))
        if mode == "manual":
            approval = await pending_approval(system)
            assert not system.server.calls
            system.actor.manager.approvals[approval["id"]].set_result("deny")
            assert (await asyncio.wait_for(task, 8))["is_error"]
            assert not system.server.calls
        else:
            result = await asyncio.wait_for(task, 8)
            assert not result.get("is_error"), result
            assert [call["name"] for call in system.server.calls] == ["read_records"]
            events = system.service.db.events(system.actor.current_run_id, 0)
            decisions = [e["data"] for e in events if e["type"] == "approval.session_decided"]
            assert len(decisions) == 1 and decisions[0]["scope"] == "trusted_read_contract"
            assert not system.service.db.one("SELECT * FROM approvals WHERE run_id=?", (system.actor.current_run_id,))

    asyncio.run(scenario())


def test_unregistered_or_expired_contract_does_not_auto_approve(system):
    path, policy, row = fresh_contract(system)
    entry = policy["connections"][row["id"]]
    entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(policy))
    assert trusted_read_decision(system.service, row, "read_records", now()) is None
    path.unlink()
    assert trusted_read_decision(system.service, row, "read_records", now()) is None
