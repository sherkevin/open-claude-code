"""Bounded one-time enrollment expiry; never grant assistance or resurrect codes."""
from datetime import datetime, timedelta, timezone
import json
import pytest

from test_kaiwu_control import BASE, admin_headers, env, key_material

URL = BASE + "/v1/enrollments"
TENANT = {"tenant_id": "tenant-a", "tenant_name": "Synthetic tenant"}
WEEK = 604800


def issue(client, **extra):
    result = client.post(URL, headers=admin_headers(), json={**TENANT, **extra})
    assert result.status_code == 200, result.text
    return result.json()


def extend(client, eid, ttl=WEEK, tenant=TENANT["tenant_id"], headers=None):
    return client.post(URL + f"/{eid}/extend", headers=admin_headers() if headers is None else headers,
                       json={"tenant_id": tenant, "ttl_seconds": ttl})


def test_seven_day_issue_preserves_default_and_cap(env):
    client, app = env
    default = issue(client)
    long = issue(client, ttl_seconds=WEEK)
    db = app.state.service.db
    for created, ttl in ((default, WEEK), (long, WEEK)):
        row = db.one("SELECT created_at,expires_at,used_at FROM kaiwu_enrollments WHERE id=?",
                     (created["enrollment_id"],))
        duration = (datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])).total_seconds()
        assert ttl <= duration < ttl + 1
        assert row["used_at"] is None
    for ttl in (59, WEEK + 1):
        assert client.post(URL, headers=admin_headers(), json={**TENANT, "ttl_seconds": ttl}).status_code == 422


def test_extend_existing_code_and_audit_once(env):
    client, app = env
    current = issue(client, ttl_seconds=24 * 60 * 60)
    eid = current["enrollment_id"]
    db = app.state.service.db
    before = db.one("SELECT * FROM kaiwu_enrollments WHERE id=?", (eid,))
    assert extend(client, eid, headers={}).status_code == 401
    assert extend(client, eid, tenant="tenant-other").status_code == 404
    assert extend(client, eid, WEEK + 1).status_code == 422
    assert extend(client, eid, 900).status_code == 409
    result = extend(client, eid)
    assert result.status_code == 200, result.text
    assert result.headers["Cache-Control"] == "no-store"
    body = result.json()
    assert set(body) == {"enrollment_id", "expires_at", "trace_id"}
    assert body["expires_at"] == (datetime.fromisoformat(before["created_at"]) + timedelta(days=7)).isoformat()
    after = db.one("SELECT * FROM kaiwu_enrollments WHERE id=?", (eid,))
    assert after == {**before, "expires_at": body["expires_at"]}
    audit = db.one("SELECT action,outcome,detail FROM kaiwu_control_audit WHERE trace_id=?", (body["trace_id"],))
    assert audit["action"] == "enrollment.extended" and audit["outcome"] == "allow"
    assert json.loads(audit["detail"])["previous_expires_at"] == before["expires_at"]
    assert extend(client, eid).json()["trace_id"] is None
    _, public_key, _ = key_material()
    redeem = client.post(URL + "/redeem", json={"code": current["code"], "device_public_key": public_key})
    assert redeem.status_code == 200, redeem.text
    assert client.post(URL + "/redeem", json={"code": current["code"], "device_public_key": public_key}).status_code == 409
    assert extend(client, eid).status_code == 409


def test_cannot_revive_expired_or_modify_persistent_codes(env):
    client, app = env
    current = issue(client)
    eid = current["enrollment_id"]
    db = app.state.service.db
    expiry = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    db.execute("UPDATE kaiwu_enrollments SET expires_at=? WHERE id=?", (expiry, eid))
    r = extend(client, eid)
    assert r.status_code == 409 and r.json()["error"]["code"] == "ENROLLMENT_EXPIRED"
    assert db.one("SELECT expires_at FROM kaiwu_enrollments WHERE id=?", (eid,))["expires_at"] == expiry
    persistent = issue(client, persistent=True, issued_to="Synthetic contact")
    assert extend(client, persistent["enrollment_id"]).status_code == 404
    assert extend(client, "enr_" + "0"*32).status_code == 404
