"""One-time connection reviews; no production credentials or remote service."""
import json
import pytest
from app.connection_read_review import save_review, current_review, require_session_eligible
from app.db import dump, now
from app.security import AppError
from test_api import client, configured

READ = {"name": "query", "description": "Read synthetic records.", "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}}
WRITE = {"name": "update_record", "description": "Update records.", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}
DISPATCH = {"name": "call", "description": "Invoke action.", "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}}}}
SQL = {"name": "query_sql", "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}}}


def setup(service, user, scope="private"):
    view = service.save_connection({"name": "Synthetic review", "url": "http://localhost:18999/mcp", "scope": scope}, user=user)
    service.db.execute("UPDATE connections SET status='connected',tools=? WHERE id=?", (dump([READ, WRITE, DISPATCH, SQL]), view["id"]))
    return service.require("connections", view["id"])


def payload(row, *names):
    return {"expected_connection_revision": row["revision"], "expected_review_revision": row["read_only_review_revision"],
            "tools": {name: {"connection_revision": row["revision"], "definition_digest": require_session_eligible(row, name),
                             "basis": "Verified synthetic query implementation and contract"} for name in names}}


def user(service, role="member", name="reviewer"):
    return service.create_user({"username": name, "display_name": name, "role": role,
                                "password": "synthetic-only-password"})


def test_private_review_once_scope_revision_revoke_and_run_start(client):
    service = client.app.state.service
    owner = user(service)
    other = user(service, "admin", "other")
    row = setup(service, owner)
    before = now()
    result = save_review(service, row["id"], payload(row, "query"), owner)
    assert result["read_only_review_revision"] == 1
    fresh = service.require("connections", row["id"])
    assert current_review(service, fresh, "query", before) is None
    assert current_review(service, fresh, "query", now())["verified_by"] == owner["id"]
    assert current_review(service, fresh, "call", now()) is None
    with pytest.raises(AppError):
        save_review(service, row["id"], payload(fresh), other)
    with pytest.raises(AppError, match="已变更"):
        save_review(service, row["id"], payload(row), owner)
    revoked = save_review(service, row["id"], payload(fresh), owner)
    assert not revoked["read_only_certifications"]
    assert current_review(service, service.require("connections", row["id"]), "query", now()) is None
    assert service.db.one("SELECT count(*) n FROM connection_read_only_review_audit WHERE connection_id=?", (row["id"],))["n"] == 2


@pytest.mark.parametrize("name", ["update_record", "call", "query_sql"])
def test_no_mutation_or_generic_execution_review_even_if_remote_hint_true(client, name):
    service = client.app.state.service
    owner = user(service)
    row = setup(service, owner)
    with pytest.raises(AppError, match="逐次审批"):
        require_session_eligible(row, name)
    assert not json.loads(service.require("connections", row["id"])["read_only_certifications"])


def test_team_review_requires_admin_and_invalidates_on_definition_or_connection_change(client):
    service = client.app.state.service
    admin = user(service, "admin")
    member = user(service, "member", "member")
    row = setup(service, admin, "team")
    with pytest.raises(AppError):
        save_review(service, row["id"], payload(row, "query"), member)
    result = save_review(service, row["id"], payload(row, "query"), admin)
    assert result["read_only_certifications"]["query"]["verified_by"] == admin["id"]
    changed = {**READ, "description": "Different definition"}
    service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([changed]), row["id"]))
    assert current_review(service, service.require("connections", row["id"]), "query", now()) is None
    service.db.execute("UPDATE connections SET tools=?,revision=revision+1 WHERE id=?", (dump([READ]), row["id"]))
    assert current_review(service, service.require("connections", row["id"]), "query", now()) is None


def test_api_requires_current_digest_and_rejects_unverified_review(client):
    service = client.app.state.service
    admin = service.db.one("SELECT id,role,enabled FROM users WHERE username='admin'")
    row = setup(service, admin, "team")
    body = payload(row, "query")
    body["tools"]["query"]["definition_digest"] = "0" * 64
    r = client.post(f"/api/v1/connections/{row['id']}/read-only-review", json=body)
    assert r.status_code == 409
    r = client.post(f"/api/v1/connections/{row['id']}/read-only-review", json=payload(row, "query"))
    assert r.status_code == 200, r.text
    assert r.json()["read_only_certifications"]["query"]["verified_by"] == admin["id"]
    tools = {t["name"]: t for t in r.json()["tools"]}
    assert tools["query"]["session_read_only_review_eligible"] is True
    assert tools["call"]["session_read_only_review_eligible"] is False
