"""Import/discovery/real local MCP dispatch for explicitly reusable contracts."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import dump, now
from app.mcp_import import create_import_router
from app.service import connection_view
from app.trusted_mcp_read import connection_identity, trusted_read_decision
from test_harness_mcp_tools import (system, mcp_server, new_run, request, pending_approval,
                                   SYNTHETIC_CREDENTIAL)
from test_trusted_mcp_read import fresh_contract


def anchor(system):
    path, policy, row = fresh_contract(system)
    policy["connections"][row["id"]]["allow_reimport"] = True
    path.write_text(json.dumps(policy))
    return path, policy, row


def import_exact(system, name="Reimport", credential=SYNTHETIC_CREDENTIAL):
    app = FastAPI()
    app.include_router(create_import_router(lambda: system.service, lambda: system.owner))
    config = {"type": "mcp", "server_label": name, "server_url": system.server.url,
              "authorization": credential, "require_approval": "never"}
    with TestClient(app) as client:
        result = client.post("/api/v1/connections/import", json={"config": config})
        assert result.status_code == 201, result.text
        assert credential not in result.text
        assert result.json()["binding_required"] is True
        view = result.json()["connections"][0]
        assert view["status"] == "untested" and not view["tools"]
    return view


def discovered(system, view):
    return asyncio.run(system.service.test_connection(view["id"], user=system.owner))


def auto_names(view):
    return {tool["name"] for tool in view["tools"] if tool.get("platform_read_default")}


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_same_reviewed_bearer_import_discovery_and_dispatch(system, mode):
    path, policy, base = anchor(system)
    before = path.read_bytes()
    imported = import_exact(system)
    row = system.service.require("connections", imported["id"])
    assert row["id"] != base["id"] and row["secret"] != base["secret"]
    assert trusted_read_decision(system.service, row, "read_records", now()) is None
    # Discovery only; it does not perform a business action or modify trust.
    view = discovered(system, imported)
    assert view["status"] == "connected"
    assert auto_names(view) == {"read_records"}
    assert path.read_bytes() == before and not system.server.calls
    assert imported["id"] not in json.loads(system.service.require("assistants", system.assistant["id"])["connections"])[0].values()
    system.service.save_assistant({"connections": [{"connection_id": imported["id"],
        "allowed_tools": ["read_records", "write_record"], "auto_approved_tools": []}]},
        system.assistant["id"], user=system.owner)
    system.connection = imported
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
            run = system.actor.current_run_id
            assert not system.service.db.one("SELECT * FROM approvals WHERE run_id=?", (run,))
            events = [e["data"] for e in system.service.db.events(run, 0)
                      if e["type"] == "approval.session_decided"]
            assert len(events) == 1
            assert events[0]["scope"] == "trusted_read_contract"
            assert events[0]["connection_id"] == imported["id"]
            assert "复用" in events[0]["reason"]
    asyncio.run(scenario())


def test_reuse_never_inferred_from_credentials_name_or_schema_without_opt_in(system):
    path, policy, base = fresh_contract(system)
    imported = import_exact(system)
    assert not auto_names(discovered(system, imported))
    assert path.read_bytes() and not system.server.calls


def test_new_connection_identity_and_definition_boundaries(system):
    path, policy, base = anchor(system)
    view = discovered(system, import_exact(system))
    row = system.service.require("connections", view["id"])
    service = system.service
    assert trusted_read_decision(service, row, "read_records", now())
    for field, value in [
        ("owner_id", "different-owner"), ("scope", "team"), ("url", row["url"] + "/elsewhere"),
        ("auth_type", "oauth"), ("auth_type", "none"), ("secret", service.secrets.encrypt("new-synthetic-bearer")),
        ("enabled", 0), ("status", "failed"), ("deleted", 1), ("read_only_review_revision", 1),
    ]:
        assert not trusted_read_decision(service, {**row, field: value}, "read_records", now()), field
    tools = json.loads(row["tools"])
    tools[0]["description"] += " altered"
    assert not trusted_read_decision(service, {**row, "tools": dump(tools)}, "read_records", now())
    assert not trusted_read_decision(service, row, "write_record", now())
    assert not trusted_read_decision(service, row, "unbound_tool", now())
    public = connection_view(row, system.owner, service)
    assert SYNTHETIC_CREDENTIAL not in json.dumps(public) and row["secret"] not in json.dumps(public)
    assert auto_names(public) == {"read_records"}


@pytest.mark.parametrize("change", ["expired", "revoked", "anchor_changed", "anchor_manual", "target_blocked", "direct_stale", "ambiguous"])
def test_reused_contract_revocation_and_ambiguity_fail_closed(system, change):
    path, policy, base = anchor(system)
    view = discovered(system, import_exact(system))
    row = system.service.require("connections", view["id"])
    service = system.service
    assert trusted_read_decision(service, row, "read_records", now())
    if change == "expired":
        policy["connections"][base["id"]]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    elif change == "revoked":
        policy["connections"].pop(base["id"])
    elif change == "anchor_changed":
        service.db.execute("UPDATE connections SET revision=revision+1 WHERE id=?", (base["id"],))
    elif change == "anchor_manual":
        service.db.execute("UPDATE connections SET read_only_review_revision=1 WHERE id=?", (base["id"],))
    elif change == "target_blocked":
        policy["blocked_reuse"] = [row["id"]]
    elif change == "direct_stale":
        policy["connections"][row["id"]] = {**policy["connections"][base["id"]], "identity": "0" * 64}
    elif change == "ambiguous":
        extra = discovered(system, import_exact(system, "Another reviewed anchor"))
        extra_row = service.require("connections", extra["id"])
        policy["connections"][extra_row["id"]] = {**policy["connections"][base["id"]],
            "identity": connection_identity(extra_row)}
    path.write_text(json.dumps(policy))
    assert not trusted_read_decision(service, row, "read_records", now())
    assert not auto_names(connection_view(row, system.owner, service))
