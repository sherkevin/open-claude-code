"""Acceptance tests for assigned, reusable connection codes."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import hashlib

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.kaiwu_native_relay import NATIVE_RELAY_PATH
from app.main import create_app
from test_kaiwu_enterprise import BASE, json_body, key_material, settings, signed_headers
from test_kaiwu_native_relay import ADMIN, issue_config, register_source


ENROLLMENTS = BASE + "/v1/enrollments"
TENANT = {"tenant_id": "pilot-company", "tenant_name": "合成试点企业"}


@pytest.fixture
def pilot(tmp_path):
    app = create_app(replace(
        settings(tmp_path),
        kaiwu_native_relay_enabled=True,
        kaiwu_control_relay_address="",
        kaiwu_control_relay_port=0,
    ))
    with TestClient(app) as client:
        yield client, app


def issue_persistent_code(client):
    response = client.post(
        ENROLLMENTS, headers=ADMIN,
        json={**TENANT, "persistent": True, "issued_to": "试点联系人"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return response.json()


def redeem(client, code, keys):
    _, public_key, _ = keys
    return client.post(ENROLLMENTS + "/redeem", json={"code": code, "device_public_key": public_key})


def sign_as(identity, keys):
    key, _, fingerprint = keys

    def sign(method, path, body=b""):
        return {
            "Authorization": "Bearer " + identity["device_token"],
            **signed_headers(key, fingerprint, method, path, body),
        }

    return sign


def test_persistent_codes_have_no_expiry_and_audit_device_usage(pilot):
    client, app = pilot
    for fields in (
        {"persistent": True},
        {"persistent": True, "issued_to": "  "},
        {"persistent": True, "issued_to": "试点联系人", "ttl_seconds": 900},
        {"issued_to": "试点联系人"},
    ):
        invalid = client.post(ENROLLMENTS, headers=ADMIN, json={**TENANT, **fields})
        assert invalid.status_code == 422

    enrollment = issue_persistent_code(client)
    code = enrollment["code"]
    enrollment_id = enrollment["enrollment_id"]
    assert enrollment["expires_at"] is None
    assert enrollment["issued_to"] == "试点联系人"
    assert enrollment["persistent"] is True
    db = app.state.service.db
    stored = db.one("SELECT code_hash FROM kaiwu_persistent_enrollments WHERE id=?", (enrollment_id,))
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    assert stored["code_hash"] == code_hash
    db.execute(
        "UPDATE kaiwu_persistent_enrollments SET created_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", enrollment_id),
    )

    short = client.post(ENROLLMENTS, headers=ADMIN, json=TENANT).json()
    assert set(short) == {"enrollment_id", "code", "expires_at", "trace_id"}
    assert datetime.fromisoformat(short["expires_at"]) > datetime.now(timezone.utc)
    legacy_identity = redeem(client, short["code"], key_material())
    assert legacy_identity.status_code == 200, legacy_identity.text
    assert redeem(client, short["code"], key_material()).status_code == 409

    keys = key_material()
    result = redeem(client, code, keys)
    assert result.status_code == 200, result.text
    identity = result.json()
    assert set(identity) == set(legacy_identity.json())
    connector_id = identity["connector_id"]
    sign = sign_as(identity, keys)

    assert client.get(ENROLLMENTS).status_code == 401
    devices_path = ENROLLMENTS + f"/{enrollment_id}/devices"
    assert client.get(devices_path, headers=sign("GET", devices_path)).status_code == 403
    assert client.get(ENROLLMENTS + "?limit=101", headers=ADMIN).status_code == 422
    listing = client.get(ENROLLMENTS, headers=ADMIN)
    entry, = listing.json()["enrollments"]
    assert entry["enrollment_id"] == enrollment_id
    assert entry["device_count"] == 1
    assert entry["issued_to"] == "试点联系人"
    assert entry["revoked_at"] is None
    assert client.get(ENROLLMENTS + "?offset=1", headers=ADMIN).json()["enrollments"] == []
    assert code not in listing.text
    assert code_hash not in listing.text

    device, = client.get(devices_path, headers=ADMIN).json()["devices"]
    assert device["connector_id"] == connector_id
    assert device["device_key_id"] == keys[2]
    assert device["last_heartbeat_at"] is None
    assert device["revoked"] is False

    heartbeat_path = BASE + f"/v1/connectors/{connector_id}/heartbeat"
    body = json_body({
        "state": "READY",
        "version": "1.4.1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source_ids": [],
        "last_error": "",
    })
    heartbeat = client.post(
        heartbeat_path, content=body,
        headers={**sign("POST", heartbeat_path, body), "Content-Type": "application/json"},
    )
    assert heartbeat.status_code == 200, heartbeat.text
    devices = client.get(devices_path, headers=ADMIN)
    assert devices.json()["devices"][0]["last_heartbeat_at"]
    assert code not in devices.text
    assert code_hash not in devices.text
    audit = db.one(
        "SELECT actor_id,detail FROM kaiwu_control_audit WHERE action='enrollment.redeemed' AND connector_id=?",
        (connector_id,),
    )
    assert audit["actor_id"] == keys[2]
    assert enrollment_id in audit["detail"]
    assert code not in audit["detail"]
    assert code_hash not in audit["detail"]


def test_persistent_code_allows_one_active_device_and_reuse_after_revocation(pilot):
    client, _ = pilot
    enrollment = issue_persistent_code(client)
    code = enrollment["code"]
    first_keys, second_keys = key_material(), key_material()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_attempt = executor.submit(redeem, client, code, first_keys)
        second_attempt = executor.submit(redeem, client, code, second_keys)
        first, second = first_attempt.result(), second_attempt.result()
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    granted, denied, granted_keys = (first, second, first_keys) if first.status_code == 200 else (second, first, second_keys)
    assert denied.json()["error"]["code"] == "ENROLLMENT_DEVICE_ACTIVE"
    assert redeem(client, code, granted_keys).json()["error"]["code"] == "ENROLLMENT_DEVICE_ACTIVE"
    identity = granted.json()
    connector_path = BASE + f"/v1/connectors/{identity['connector_id']}"
    desired = connector_path + "/desired-state"
    assert client.get(desired, headers=sign_as(identity, granted_keys)("GET", desired)).status_code == 200
    assert client.post(connector_path + "/drain", headers=ADMIN).status_code == 200
    assert redeem(client, code, key_material()).status_code == 409
    assert client.post(connector_path + "/revoke", headers=ADMIN).status_code == 200
    assert client.get(desired, headers=sign_as(identity, granted_keys)("GET", desired)).status_code == 403

    replacement = redeem(client, code, key_material())
    assert replacement.status_code == 200, replacement.text
    new_identity = replacement.json()
    entry_id = enrollment["enrollment_id"]
    history = client.get(ENROLLMENTS + f"/{entry_id}/devices", headers=ADMIN).json()["devices"]
    assert [(row["connector_id"], row["revoked"]) for row in history] == [
        (new_identity["connector_id"], False), (identity["connector_id"], True),
    ]


def test_revoking_persistent_code_disables_device_integration_and_relay(pilot):
    client, app = pilot
    enrollment = issue_persistent_code(client)
    keys = key_material()
    identity = redeem(client, enrollment["code"], keys).json()
    other_enrollment = issue_persistent_code(client)
    other_keys = key_material()
    other_device = redeem(client, other_enrollment["code"], other_keys).json()
    connector_id = identity["connector_id"]
    path = NATIVE_RELAY_PATH.format(connector_id=connector_id)
    sign = sign_as(identity, keys)
    revoke_path = ENROLLMENTS + f"/{enrollment['enrollment_id']}/revoke"
    with client.websocket_connect("wss://testserver" + path, headers=sign("GET", path)) as relay:
        assert relay.receive_json() == {"type": "ready"}
        register_source(client, sign)
        integration = issue_config(client, connector_id, TENANT["tenant_id"])

        response = client.post(revoke_path, headers=ADMIN)
        assert response.status_code == 200, response.text
        revoked = response.json()
        assert revoked["revoked"] is True
        assert revoked["revoked_device_count"] == 1
        with pytest.raises(WebSocketDisconnect):
            relay.receive_json()
        edge = client.post(integration["url"], headers=integration["headers"], json={
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        assert edge.status_code == 401

    assert client.post(BASE + f"/v1/connectors/{connector_id}/resume", headers=ADMIN).status_code == 403
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver" + path, headers=sign("GET", path)):
            pytest.fail("revoked device connected")
    assert redeem(client, enrollment["code"], key_material()).json()["error"]["code"] == "ENROLLMENT_INVALID"
    other_path = BASE + f"/v1/connectors/{other_device['connector_id']}/desired-state"
    assert client.get(other_path, headers=sign_as(other_device, other_keys)("GET", other_path)).status_code == 200

    repeated = client.post(revoke_path, headers=ADMIN).json()
    assert repeated["revoked_at"] == revoked["revoked_at"]
    audit = app.state.service.db.all("SELECT * FROM kaiwu_control_audit WHERE action='enrollment.revoked'")
    assert len(audit) == 1
    assert enrollment["enrollment_id"] in audit[0]["detail"]
