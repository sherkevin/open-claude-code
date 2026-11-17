"""Synthetic acceptance tests for the Kaiwu control plane.

All tenants, keys, sources and heartbeats are generated locally. These tests do
not call relay, Edge, a customer MCP server or any external provider.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


ADMIN_TOKEN = "admin-control-token-" + "a" * 32
BASE = "/api/kaiwu"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def key_material() -> tuple[Ed25519PrivateKey, str, str]:
    key = Ed25519PrivateKey.generate()
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key_id = b64url(hashlib.sha256(public_raw).digest())
    return key, b64url(public_raw), key_id


def signed_headers(
    key: Ed25519PrivateKey,
    key_id: str,
    method: str,
    path: str,
    body: bytes,
    *,
    nonce: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp_text = str(timestamp if timestamp is not None else int(time.time()))
    nonce = nonce or b64url(hashlib.sha256(body + str(time.time_ns()).encode()).digest())
    message = "\n".join((
        timestamp_text,
        nonce,
        method.upper(),
        path,
        hashlib.sha256(body).hexdigest(),
    )).encode()
    return {
        "X-Kaiwu-Device-Key": key_id,
        "X-Kaiwu-Timestamp": timestamp_text,
        "X-Kaiwu-Nonce": nonce,
        "X-Kaiwu-Signature": b64url(key.sign(message)),
    }


def json_body(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + ADMIN_TOKEN}


def settings(tmp_path, *, enabled: bool = True) -> Settings:
    return Settings(
        data_dir=tmp_path / ("enabled" if enabled else "disabled"),
        provider_settings="",
        origins=("http://testserver",),
        kaiwu_control_enabled=enabled,
        kaiwu_control_admin_token=ADMIN_TOKEN if enabled else "",
        kaiwu_control_relay_address="relay.test.invalid",
        kaiwu_control_relay_port=7443,
        kaiwu_control_relay_protocol="wss",
        kaiwu_control_relay_tls_enabled=True,
        kaiwu_control_edge_user="edge-test",
    )


@pytest.fixture
def env(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        yield client, app


def enroll(env, key_material_value) -> dict:
    client, _ = env
    created = client.post(
        BASE + "/v1/enrollments",
        headers=admin_headers(),
        json={"tenant_id": "tenant-a", "tenant_name": "合成企业"},
    )
    assert created.status_code == 200, created.text
    code = created.json()["code"]
    _, public_key, _ = key_material_value
    redeemed = client.post(
        BASE + "/v1/enrollments/redeem",
        json={"code": code, "device_public_key": public_key},
    )
    assert redeemed.status_code == 200, redeemed.text
    return redeemed.json()


def test_control_plane_disabled_fails_closed(tmp_path):
    app = create_app(settings(tmp_path, enabled=False))
    with TestClient(app) as client:
        response = client.get(BASE + "/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "disabled"
        denied = client.post(
            BASE + "/v1/enrollments",
            headers={"Authorization": "Bearer arbitrary"},
            json={"tenant_id": "tenant-a", "tenant_name": "合成企业"},
        )
        assert denied.status_code == 503
        assert denied.json()["error"]["code"] == "CONTROL_PLANE_DISABLED"


def test_enrollment_is_one_time_and_device_signature_is_bound(env):
    client, _ = env
    key, public_key, key_id = key_material()
    assert key_id
    created = client.post(
        BASE + "/v1/enrollments",
        headers=admin_headers(),
        json={"tenant_id": "tenant-a", "tenant_name": "合成企业"},
    )
    code = created.json()["code"]
    redeemed = client.post(
        BASE + "/v1/enrollments/redeem",
        json={"code": code, "device_public_key": public_key},
    )
    assert redeemed.status_code == 200
    identity = redeemed.json()
    assert identity["device_key_id"] == key_id

    reused = client.post(
        BASE + "/v1/enrollments/redeem",
        json={"code": code, "device_public_key": public_key},
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "ENROLLMENT_INVALID"

    path = BASE + f"/v1/connectors/{identity['connector_id']}/desired-state"
    headers = {
        "Authorization": "Bearer " + identity["device_token"],
        **signed_headers(key, key_id, "GET", path, b""),
    }
    desired = client.get(path, headers=headers)
    assert desired.status_code == 200, desired.text
    assert desired.json()["revision"] == 1
    assert desired.json()["sources"] == []

    wrong_key, _, _ = key_material()
    forged = client.get(
        path,
        headers={
            "Authorization": "Bearer " + identity["device_token"],
            **signed_headers(wrong_key, key_id, "GET", path, b""),
        },
    )
    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "DEVICE_SIGNATURE_INVALID"


def test_device_signature_replay_and_clock_skew_are_rejected(env):
    client, _ = env
    material = key_material()
    key, _, key_id = material
    identity = enroll(env, material)
    path = BASE + f"/v1/connectors/{identity['connector_id']}/desired-state"
    nonce = b64url(b"fixed-nonce-" + b"x" * 20)
    headers = {
        "Authorization": "Bearer " + identity["device_token"],
        **signed_headers(key, key_id, "GET", path, b"", nonce=nonce),
    }
    assert client.get(path, headers=headers).status_code == 200
    replay = client.get(path, headers=headers)
    assert replay.status_code == 403
    assert replay.json()["error"]["code"] == "DEVICE_SIGNATURE_REPLAYED"

    expired = client.get(
        path,
        headers={
            "Authorization": "Bearer " + identity["device_token"],
            **signed_headers(key, key_id, "GET", path, b"", timestamp=int(time.time()) - 120),
        },
    )
    assert expired.status_code == 403
    assert expired.json()["error"]["code"] == "DEVICE_SIGNATURE_EXPIRED"


def test_source_registration_is_metadata_only_and_desired_state_is_strict(env):
    client, _ = env
    material = key_material()
    key, _, key_id = material
    identity = enroll(env, material)
    token = identity["device_token"]
    source = {
        "source_id": "src_" + "1" * 32,
        "display_name": "销售只读",
        "allowed_tools": ["query_orders", "manual_search"],
        "tool_schema_sha256": "2" * 64,
        "target_binding_sha256": "3" * 64,
        "transport": "streamable_http",
    }
    body = json_body(source)
    path = BASE + "/v1/sources"
    created = client.post(
        path,
        content=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, body),
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["source"]["allowed_tools"] == ["manual_search", "query_orders"]

    injected = {**source, "local_host": "10.0.0.8", "local_port": 8080}
    injected_body = json_body(injected)
    rejected = client.post(
        path,
        content=injected_body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, injected_body),
        },
    )
    assert rejected.status_code == 422

    desired_path = BASE + f"/v1/connectors/{identity['connector_id']}/desired-state"
    desired = client.get(
        desired_path,
        headers={
            "Authorization": "Bearer " + token,
            **signed_headers(key, key_id, "GET", desired_path, b""),
        },
    )
    assert desired.status_code == 200
    source_view = desired.json()["sources"][0]
    assert source_view["source_id"] == source["source_id"]
    assert "local_host" not in source_view
    assert "local_port" not in source_view
    assert "upstream_url" not in source_view

    secrets_path = BASE + f"/v1/connectors/{identity['connector_id']}/transport-secrets"
    secrets_response = client.get(
        secrets_path,
        headers={
            "Authorization": "Bearer " + token,
            **signed_headers(key, key_id, "GET", secrets_path, b""),
        },
    )
    assert secrets_response.status_code == 200
    assert secrets_response.headers["cache-control"] == "no-store"
    payload = secrets_response.json()
    assert payload["relay"]["token"]
    assert payload["sources"][0]["stcp_secret"]
    assert "device_token" not in secrets_response.text
    assert "Authorization" not in secrets_response.text


def test_source_conflict_credentials_are_separated_and_audit_has_no_secret(env):
    client, app = env
    material = key_material()
    key, _, key_id = material
    identity = enroll(env, material)
    token = identity["device_token"]
    source = {
        "source_id": "src_" + "2" * 32,
        "display_name": "库存只读",
        "allowed_tools": ["read_stock"],
        "tool_schema_sha256": "4" * 64,
        "target_binding_sha256": "5" * 64,
    }
    path = BASE + "/v1/sources"
    body = json_body(source)
    first = client.post(
        path,
        content=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, body),
        },
    )
    assert first.status_code == 200
    conflict_body = json_body({**source, "allowed_tools": ["read_stock", "write_stock"]})
    conflict = client.post(
        path,
        content=conflict_body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, conflict_body),
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "SOURCE_CONFIG_CONFLICT"

    device_as_admin = client.get(BASE + "/v1/connectors", headers={"Authorization": "Bearer " + token})
    assert device_as_admin.status_code == 403
    assert device_as_admin.json()["error"]["code"] == "CREDENTIAL_PLANE_DENIED"
    device_path = BASE + f"/v1/connectors/{identity['connector_id']}/desired-state"
    admin_as_device = client.get(device_path, headers=admin_headers())
    assert admin_as_device.status_code == 403
    assert admin_as_device.json()["error"]["code"] == "CREDENTIAL_PLANE_DENIED"

    audit = app.state.service.db.all("SELECT * FROM kaiwu_control_audit")
    serialized = json.dumps(audit, ensure_ascii=False)
    assert ADMIN_TOKEN not in serialized
    assert token not in serialized
    assert identity["relay"]["token"] not in serialized


def test_heartbeat_drain_resume_and_revoke(env):
    client, app = env
    material = key_material()
    key, _, key_id = material
    identity = enroll(env, material)
    token = identity["device_token"]
    connector_path = BASE + f"/v1/connectors/{identity['connector_id']}"
    heartbeat_path = connector_path + "/heartbeat"
    beat = {
        "state": "READY",
        "version": "1.2.0 test",
        "observed_at": "2026-11-17T12:00:00Z",
        "source_ids": [],
        "last_error": "",
    }
    beat_body = json_body(beat)
    sent = client.post(
        heartbeat_path,
        content=beat_body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", heartbeat_path, beat_body),
        },
    )
    assert sent.status_code == 200, sent.text
    row = app.state.service.db.one(
        "SELECT * FROM kaiwu_heartbeats WHERE connector_id=?",
        (identity["connector_id"],),
    )
    assert row["source_ids"] == "[]"
    assert row["last_error"] == ""

    missing = {
        "state": "READY",
        "version": "1.2.0 test",
        "observed_at": "2026-11-17T12:00:00Z",
        "last_error": "",
    }
    missing_body = json_body(missing)
    invalid = client.post(
        heartbeat_path,
        content=missing_body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", heartbeat_path, missing_body),
        },
    )
    assert invalid.status_code == 422

    drained = client.post(connector_path + "/drain", headers=admin_headers())
    assert drained.status_code == 200
    assert drained.json()["draining"] is True
    assert drained.json()["revision"] == 2
    resumed = client.post(connector_path + "/resume", headers=admin_headers())
    assert resumed.status_code == 200
    assert resumed.json()["draining"] is False
    revoked = client.post(connector_path + "/revoke", headers=admin_headers())
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    desired_path = connector_path + "/desired-state"
    stale = client.get(
        desired_path,
        headers={
            "Authorization": "Bearer " + token,
            **signed_headers(key, key_id, "GET", desired_path, b""),
        },
    )
    assert stale.status_code == 403
    assert stale.json()["error"]["code"] == "DEVICE_REVOKED"
