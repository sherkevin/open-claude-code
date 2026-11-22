"""Native connector handoff into the existing MCP import surface."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.db import now
from app.main import create_app
from app.security import password_hash

from test_kaiwu_enterprise import ADMIN_TOKEN, BASE, SOURCE_ID, json_body, key_material, settings, signed_headers


PORTAL = "/api/v1/connections/kaiwu-handoff"
PASSWORD = "synthetic-handoff-test-password"


@pytest.fixture
def handoff_env(tmp_path):
    config = replace(settings(tmp_path), kaiwu_native_relay_enabled=True, mcp_hosts=("testserver",))
    app = create_app(config)
    with TestClient(app, base_url="https://testserver") as client:
        db = app.state.service.db
        db.executemany(
            "INSERT INTO users(id,username,display_name,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
            [(role, role, role, password_hash(PASSWORD), role, now()) for role in ("admin", "member")],
        )
        key, public_key, key_id = key_material()
        enrollment = client.post(
            BASE + "/v1/enrollments", headers={"Authorization": "Bearer " + ADMIN_TOKEN},
            json={"tenant_id": "tenant-handoff", "tenant_name": "合成华微"},
        )
        assert enrollment.status_code == 200
        redeemed = client.post(BASE + "/v1/enrollments/redeem", json={"code": enrollment.json()["code"], "device_public_key": public_key})
        assert redeemed.status_code == 200
        identity = redeemed.json()

        def device_headers(path, body=b"", method="POST"):
            return {"Authorization": "Bearer " + identity["device_token"], **signed_headers(key, key_id, method, path, body)}

        def device_post(path, payload):
            body = json_body(payload)
            return client.post(path, content=body, headers={"Content-Type": "application/json", **device_headers(path, body)})

        source = {"source_id": SOURCE_ID, "display_name": "订单查询", "allowed_tools": ["lookup_order"],
                  "tool_schema_sha256": "b" * 64, "target_binding_sha256": "c" * 64, "transport": "streamable_http"}
        assert device_post(BASE + "/v1/sources", source).status_code == 200
        connector = BASE + "/v1/connectors/" + identity["connector_id"]

        def connect():
            path = connector + "/native-relay"
            return client.websocket_connect("wss://testserver" + path, headers=device_headers(path, method="GET"))

        def handoff():
            return device_post(connector + "/platform-handoff", {"source_id": SOURCE_ID})

        yield client, db, connect, handoff


def login(client, role):
    response = client.post("/api/v1/auth/login", json={"username": role, "password": PASSWORD})
    assert response.status_code == 200, response.text


def test_handoff_import_requires_admin_and_reuses_existing_connection(handoff_env):
    client, db, connect, handoff = handoff_env
    assert handoff().status_code == 409
    with connect() as relay:
        assert relay.receive_json() == {"type": "ready"}
        created = handoff()
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        assert set(created.json()) == {"handoff_code"}
        code = created.json()["handoff_code"]
        request = {"handoff_code": code}
        assert client.post(PORTAL + "/preview", json=request).status_code == 401
        login(client, "member")
        assert client.post(PORTAL + "/preview", json=request).status_code == 403
        login(client, "admin")
        preview = client.post(PORTAL + "/preview", json=request)
        assert preview.json() == {"tenant_name": "合成华微", "name": "订单查询", "allowed_tools": ["lookup_order"]}
        payload = {**request, "action": "import", "scope": "team", "name": "华微订单查询"}
        imported = client.post(PORTAL + "/redeem", json=payload)
        assert imported.status_code == 200, imported.text
        assert "kw_int_" not in imported.text
        connection = imported.json()["connection"]
        assert connection["status"] == "untested"
        assert connection["scope"] == "team"
        assert client.post(PORTAL + "/redeem", json=payload).json() == {"connection": connection}
        assert db.one("SELECT count(*) AS n FROM kaiwu_integrations")["n"] == 1
        assert db.one("SELECT count(*) AS n FROM connections")["n"] == 1
        assert client.post(PORTAL + "/redeem", json={**payload, "action": "copy"}).status_code == 409


def test_copy_is_one_time_and_uses_existing_format(handoff_env):
    client, db, connect, handoff = handoff_env
    login(client, "admin")
    with connect() as relay:
        assert relay.receive_json() == {"type": "ready"}
        request = {"handoff_code": handoff().json()["handoff_code"], "action": "copy", "name": "复制给华微"}
        copied = client.post(PORTAL + "/redeem", json=request)
        assert copied.status_code == 200, copied.text
        assert copied.headers["cache-control"] == "no-store"
        config = json.loads(copied.json()["mcp_config_json"])
        entry = config["mcpServers"]["复制给华微"]
        assert entry["type"] == "http"
        assert entry["url"].endswith(f"/sources/{SOURCE_ID}/mcp")
        assert entry["headers"]["Authorization"].startswith("Bearer kw_int_")
        assert entry["headers"]["Authorization"][7:] not in str(db.all("SELECT * FROM kaiwu_integrations"))
        assert client.post(PORTAL + "/redeem", json=request).status_code == 409
        imported = client.post("/api/v1/connections/import", json={"scope": "team", "config": config})
        assert imported.status_code == 201, imported.text


def test_source_changes_invalidate_pending_code(handoff_env):
    client, db, connect, handoff = handoff_env
    login(client, "admin")
    with connect() as relay:
        assert relay.receive_json() == {"type": "ready"}
        code = handoff().json()["handoff_code"]
        # Devices may only retry the same source registration. Simulate a
        # revision change from an operator-approved update of the database.
        db.execute("UPDATE kaiwu_sources SET revision=revision+1 WHERE source_id=?", (SOURCE_ID,))
        preview = client.post(PORTAL + "/preview", json={"handoff_code": code})
        assert preview.status_code == 409
        assert preview.json()["error"]["code"] == "HANDOFF_SOURCE_CHANGED"
