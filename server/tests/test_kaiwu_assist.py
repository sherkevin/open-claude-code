"""A signed synthetic device exercises the assisted MCP onboarding routes."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.db import now
from app.main import create_app
from app.security import password_hash
from test_kaiwu_enterprise import ADMIN_TOKEN, BASE, SOURCE_ID, json_body, key_material, settings, signed_headers

PORTAL = "/api/v1/connections/kaiwu-assist"
PASSWORD = "synthetic-assist-test-password"
TOKEN = "Synthetic-test-credential-42"
TARGET = "https://mcp.example.invalid/SysDevCore/mcp/sales_contract"


@pytest.fixture
def env(tmp_path):
    config = replace(settings(tmp_path), kaiwu_native_relay_enabled=True,
                     kaiwu_assist_tenants=("tenant-assist",), mcp_hosts=("testserver",))
    app = create_app(config)
    with TestClient(app, base_url="https://testserver") as client:
        db = app.state.service.db
        db.execute(
            "INSERT INTO users(id,username,display_name,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?,1,?)",
            ("owner", "owner", "owner", password_hash(PASSWORD), "admin", now()),
        )

        def enroll(tenant):
            key, public_key, key_id = key_material()
            enrollment = client.post(BASE + "/v1/enrollments", headers={"Authorization": "Bearer " + ADMIN_TOKEN},
                                     json={"tenant_id": tenant, "tenant_name": "合成企业"})
            assert enrollment.status_code == 200
            redeem = client.post(BASE + "/v1/enrollments/redeem",
                                 json={"code": enrollment.json()["code"], "device_public_key": public_key})
            assert redeem.status_code == 200
            return redeem.json(), key, key_id

        primary = enroll("tenant-assist")
        outsider = enroll("another-company")
        connector_id = primary[0]["connector_id"]
        path = BASE + "/v1/connectors/" + connector_id

        def identity_headers(endpoint, method, body, credentials=primary):
            identity, key, key_id = credentials
            return {"Authorization": "Bearer " + identity["device_token"],
                    **signed_headers(key, key_id, method, endpoint, body)}

        def device(action, payload=None, credentials=primary):
            endpoint = action if action.startswith("/") else path + "/assist/" + action
            method = "POST" if payload is not None else "GET"
            body = json_body(payload) if payload is not None else b""
            return client.request(method, endpoint, content=body,
                                  headers={"Content-Type": "application/json", **identity_headers(endpoint, method, body, credentials)})

        def connect():
            ws_path = path + "/native-relay"
            return client.websocket_connect("wss://testserver" + ws_path,
                                            headers=identity_headers(ws_path, "GET", b""))

        yield app, client, db, device, connect, connector_id, outsider


def ask(client, connector_id, url=TARGET):
    return client.post(PORTAL + "/sessions", json={"connector_id": connector_id, "name": "销售合同", "url": url})


def request_assist(env):
    _, client, _, device, _, connector_id, outsider = env
    assert client.get(PORTAL + "/devices").status_code == 401
    login = client.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})
    assert login.status_code == 200
    assert [x["id"] for x in client.get(PORTAL + "/devices").json()["devices"]] == [connector_id]
    assert ask(client, outsider[0]["connector_id"]).status_code == 404
    assert ask(client, connector_id, "http://mcp.example.invalid/mcp?token=secret").status_code == 422
    response = ask(client, connector_id)
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["url"] == TARGET and session["status"] == "pending"
    local = {"session_id": session["id"]}
    route = PORTAL + "/sessions/" + session["id"]
    assert ask(client, connector_id).status_code == 409
    assert client.post(route + "/jobs", json={"action": "discover"}).status_code == 409
    assert device("poll", local).json() == {"active": False, "job": None}
    assert device("offer").json()["offer"]["id"] == session["id"]
    assert device("start", local, credentials=outsider).status_code == 403
    assert device("start", local).status_code == 200
    return local, route


def test_credential_delivery_once_stop_and_expiry(env):
    app, client, db, device, *_ = env
    local, route = request_assist(env)
    malformed = client.post(route + "/jobs", json={"action": "discover", "bearer_token": "Bearer do-not-reflect"})
    assert malformed.status_code == 422 and "do-not-reflect" not in malformed.text
    request = {"action": "discover", "bearer_token": TOKEN}
    submitted = client.post(route + "/jobs", json=request)
    assert submitted.status_code == 200 and TOKEN not in submitted.text
    latest = submitted.json()["latest_job"]
    sealed = db.one("SELECT sealed_bearer FROM kaiwu_assist_jobs WHERE id=?", (latest["id"],))["sealed_bearer"]
    assert sealed and TOKEN not in sealed
    assert device("poll", local).json() == {
        "active": True, "job": {"id": latest["id"], "action": "discover", "bearer_token": TOKEN},
    }
    assert device("poll", local).json() == {"active": True, "job": None}
    assert db.one("SELECT sealed_bearer FROM kaiwu_assist_jobs WHERE id=?", (latest["id"],))["sealed_bearer"] is None
    result = {**local, "job_id": latest["id"], "status": "failed", "code": "upstream_failed",
              "stage": "initialize", "http_status": 401, "authorization": True}
    assert device("result", {**result, "raw_headers": {"Authorization": TOKEN}}).status_code == 422
    assert device("result", result).status_code == 200
    assert device("result", result).status_code == 200
    assert device("result", {**result, "http_status": 404}).status_code == 409
    report = client.get(route).json()["latest_job"]
    assert report["http_status"] == 401 and report["authorization"] is True
    queued = client.post(route + "/jobs", json=request).json()["latest_job"]
    db.execute("UPDATE kaiwu_assist_jobs SET expires_at='2020-01-01' WHERE id=?", (queued["id"],))
    assert device("poll", local).json() == {"active": True, "job": None}
    assert db.one("SELECT status,sealed_bearer FROM kaiwu_assist_jobs WHERE id=?", (queued["id"],)) == {
        "status": "failed", "sealed_bearer": None,
    }
    queued = client.post(route + "/jobs", json=request).json()["latest_job"]
    assert device("stop", local).status_code == 200
    assert device("poll", local).json() == {"active": False, "job": None}
    assert db.one("SELECT sealed_bearer FROM kaiwu_assist_jobs WHERE id=?", (queued["id"],))["sealed_bearer"] is None
    assert client.post(route + "/jobs", json=request).status_code == 409
    assert db.one("SELECT actor_id FROM kaiwu_control_audit WHERE action='assist.requested'")["actor_id"] == "owner"


def test_import_uses_discovered_selection_and_same_native_revision(env):
    _, client, db, device, connect, connector_id, _ = env
    local, route = request_assist(env)
    selected = ["lookup_order"]
    connect_request = {"action": "connect", "tools": selected}
    assert client.post(route + "/jobs", json=connect_request).status_code == 422
    discover = client.post(route + "/jobs", json={"action": "discover"}).json()["latest_job"]
    assert device("poll", local).json()["job"]["id"] == discover["id"]
    assert device("result", {**local, "job_id": discover["id"], "status": "succeeded", "code": "tools_loaded",
                            "stage": "tools/list", "authorization": True, "tools": [*selected, "other_tool"]}).status_code == 200
    assert client.post(route + "/jobs", json={"action": "connect", "tools": ["unseen"]}).status_code == 422
    job = client.post(route + "/jobs", json=connect_request).json()["latest_job"]
    assert device("poll", local).json()["job"] == {"id": job["id"], "action": "connect", "tools": selected}
    success = {**local, "job_id": job["id"], "status": "succeeded", "code": "connected",
               "stage": "connect", "source_id": SOURCE_ID}
    assert device("result", success).status_code == 409
    source = {"source_id": SOURCE_ID, "display_name": "销售合同", "allowed_tools": selected,
              "tool_schema_sha256": "b" * 64, "target_binding_sha256": "c" * 64, "transport": "streamable_http"}
    assert device(BASE + "/v1/sources", source).status_code == 200
    assert device("result", success).status_code == 200
    assert client.post(route + "/import", json={"scope": "team"}).status_code == 409
    with connect() as relay:
        assert relay.receive_json() == {"type": "ready"}
        assert client.get(PORTAL + "/devices").json()["devices"][0]["online"] is True
        db.execute("UPDATE kaiwu_sources SET revision=2 WHERE connector_id=? AND source_id=?", (connector_id, SOURCE_ID))
        changed = client.post(route + "/import", json={"scope": "team"})
        assert changed.status_code == 409 and changed.json()["error"]["code"] == "ASSIST_SOURCE_CHANGED"
        db.execute("UPDATE kaiwu_sources SET revision=1 WHERE connector_id=? AND source_id=?", (connector_id, SOURCE_ID))
        db.execute("UPDATE kaiwu_assist_sessions SET last_poll_at='2020-01-01' WHERE id=?", (local["session_id"],))
        assert client.post(route + "/import", json={"scope": "team"}).status_code == 409
        assert device("poll", local).json() == {"active": True, "job": None}
        imported = client.post(route + "/import", json={"scope": "team"})
        assert imported.status_code == 200, imported.text
        assert "kw_int_" not in imported.text
        assert imported.json()["connection"]["scope"] == "team"
        assert client.post(route + "/import", json={"scope": "team"}).json() == imported.json()
        assert db.one("SELECT count(*) AS n FROM kaiwu_integrations")["n"] == 1
        assert db.one("SELECT count(*) AS n FROM connections")["n"] == 1


def test_restart_with_assistance_disabled_cancels_and_erases_pending_credentials(env):
    app, client, db, device, *_ = env
    local, route = request_assist(env)
    queued = client.post(route + "/jobs", json={"action": "discover", "bearer_token": TOKEN}).json()["latest_job"]
    config = replace(app.state.service.settings, kaiwu_assist_tenants=())
    with TestClient(create_app(config), base_url="https://testserver"):
        assert db.one("SELECT status FROM kaiwu_assist_sessions WHERE id=?", (local["session_id"],))["status"] == "expired"
        assert db.one("SELECT status,sealed_bearer FROM kaiwu_assist_jobs WHERE id=?", (queued["id"],)) == {
            "status": "failed", "sealed_bearer": None,
        }
