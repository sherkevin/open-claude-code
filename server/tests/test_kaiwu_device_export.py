"""A pilot connector can copy a source-scoped configuration over its signed API."""

from concurrent.futures import ThreadPoolExecutor

from app.kaiwu_control import _builtin_demo_source_id
from app.mcp_import import parse_mcp_config

from test_kaiwu_enterprise import BASE, SOURCE_ID, json_body
from test_kaiwu_native_relay import ADMIN, SOURCE, issue_config, native, send_result


OTHER_SOURCE = "src_" + "b" * 32
LEGACY_SOURCE = "src_" + "c" * 32
EDGE = "https://testserver/projects/kaiwu/mcp"


def device_post(client, sign, path, payload):
    body = json_body(payload)
    return client.post(path, content=body, headers={
        "Content-Type": "application/json", **sign("POST", path, body),
    })


def export_path(connector_id):
    return f"{BASE}/v1/connectors/{connector_id}/mcp-export"


def register(client, sign, source_id, **fields):
    result = device_post(client, sign, BASE + "/v1/sources", {**SOURCE, "source_id": source_id, **fields})
    assert result.status_code == 200, result.text


def test_requires_signed_online_opted_in_owner_and_business_source(native):
    client, app, enroll = native
    settings = app.state.service.settings
    settings.kaiwu_device_export_tenants = ()
    settings.kaiwu_edge_public_base = EDGE
    connector, relay, sign = enroll("tenant-a")
    other, other_relay, other_sign = enroll("tenant-b")
    path = export_path(connector)
    payload = {"source_id": SOURCE_ID}
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, json=payload, headers=ADMIN).json()["error"]["code"] == "CREDENTIAL_PLANE_DENIED"
    assert device_post(client, sign, export_path(other), payload).json()["error"]["code"] == "DEVICE_SCOPE_DENIED"
    assert device_post(client, sign, path, payload).json()["error"]["code"] == "DEVICE_EXPORT_DENIED"

    settings.kaiwu_device_export_tenants = ("tenant-a",)
    assert device_post(client, sign, path, payload).json()["error"]["code"] == "NATIVE_RELAY_OFFLINE"
    with (
        client.websocket_connect("wss://testserver" + relay, headers=sign("GET", relay)) as ws,
        client.websocket_connect("wss://testserver" + other_relay, headers=other_sign("GET", other_relay)) as other_ws,
    ):
        assert ws.receive_json() == other_ws.receive_json() == {"type": "ready"}
        register(client, other_sign, OTHER_SOURCE)
        register(client, sign, SOURCE_ID)
        register(client, sign, LEGACY_SOURCE, transport="legacy_sse")
        assert device_post(client, sign, path, {"source_id": OTHER_SOURCE}).json()["error"]["code"] == "INTEGRATION_SOURCE_DENIED"
        assert device_post(client, other_sign, export_path(other), {"source_id": OTHER_SOURCE}).json()["error"]["code"] == "DEVICE_EXPORT_DENIED"
        assert device_post(client, sign, path, {"source_id": _builtin_demo_source_id(connector)}).json()["error"]["code"] == "DEVICE_EXPORT_SOURCE_DENIED"
        assert device_post(client, sign, path, {"source_id": LEGACY_SOURCE}).json()["error"]["code"] == "DEVICE_EXPORT_SOURCE_DENIED"
        settings.kaiwu_edge_public_base = EDGE.replace("https://", "http://")
        assert device_post(client, sign, path, payload).json()["error"]["code"] == "EDGE_NOT_CONFIGURED"
        assert not app.state.service.db.all("SELECT * FROM kaiwu_integrations")
        assert client.post(f"{BASE}/v1/connectors/{connector}/drain", headers=ADMIN).status_code == 200
        assert device_post(client, sign, path, payload).json()["error"]["code"] == "NATIVE_RELAY_OFFLINE"


def test_copy_reuses_credential_and_config_can_call_through_protected_edge(native):
    client, app, enroll = native
    settings = app.state.service.settings
    settings.kaiwu_device_export_tenants = ("tenant-a",)
    settings.kaiwu_edge_public_base = EDGE
    connector, relay, sign = enroll("tenant-a")
    path = export_path(connector)
    with client.websocket_connect("wss://testserver" + relay, headers=sign("GET", relay)) as ws:
        assert ws.receive_json() == {"type": "ready"}
        register(client, sign, SOURCE_ID)
        register(client, sign, OTHER_SOURCE)
        admin_config = issue_config(client, connector, "tenant-a")
        with ThreadPoolExecutor() as executor:
            def copy():
                return device_post(client, sign, path, {"source_id": SOURCE_ID})

            exports = [job.result(5) for job in [executor.submit(copy) for _ in range(5)]]
            assert all(result.status_code == 200 and result.headers["cache-control"] == "no-store" for result in exports)
            assert all(result.json() == exports[0].json() for result in exports)
            assert set(exports[0].json()) == {"mcp_config"}
            config = exports[0].json()["mcp_config"]
            imported, = parse_mcp_config(config, ("testserver",)).connections
            entry, = config["mcpServers"].values()
            assert set(entry) == {"type", "url", "headers"}
            assert imported.url == f"{EDGE}/sources/{SOURCE_ID}/mcp"
            assert set(entry["headers"]) == {"Authorization"}
            assert entry["headers"]["Authorization"] != admin_config["headers"]["Authorization"]

            db = app.state.service.db
            rows = db.all("SELECT * FROM kaiwu_integrations ORDER BY created_at")
            assert len(rows) == 2
            assert imported.credential not in str(rows)
            assert rows[0]["created_by"] == "control-admin" and rows[0]["revoked_at"] is None
            assert db.one("SELECT actor_type,source_id FROM kaiwu_control_audit WHERE action='integration.created' ORDER BY id DESC LIMIT 1") == {
                "actor_type": "device", "source_id": SOURCE_ID,
            }

            def edge(method, request_id, url=entry["url"], **kwargs):
                session = kwargs.pop("session", "")
                headers = dict(entry["headers"])
                if session:
                    headers["Mcp-Session-Id"] = session
                return client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": request_id, "method": method, **kwargs})

            response = executor.submit(edge, "initialize", 1)
            forwarded = ws.receive_json()
            assert forwarded["source_id"] == SOURCE_ID and "Authorization" not in forwarded["headers"]
            send_result(ws, forwarded, [b'{"jsonrpc":"2.0","id":1,"result":{}}'], {
                "Content-Type": "application/json", "Mcp-Session-Id": "internal-session",
            })
            initialized = response.result(4)
            assert initialized.status_code == 200, initialized.text
            session = initialized.headers["Mcp-Session-Id"]
            assert edge("initialize", 2, entry["url"].replace(SOURCE_ID, OTHER_SOURCE)).status_code == 403

            response = executor.submit(edge, "tools/call", 3, session=session, params={"name": "lookup_order"})
            forwarded = ws.receive_json()
            send_result(ws, forwarded, [b'{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"synthetic-order"}]}}'], {
                "Content-Type": "application/json",
            })
            assert response.result(4).json()["result"]["content"][0]["text"] == "synthetic-order"

        integration = rows[1]["id"]
        assert client.post(f"{BASE}/v1/integrations/{integration}/revoke", headers=ADMIN).status_code == 200
        assert edge("initialize", 4).status_code == 401
        assert copy().json()["error"]["code"] == "DEVICE_EXPORT_REVOKED"


def test_expiration_and_rotation_do_not_modify_existing_integration(native):
    client, app, enroll = native
    settings = app.state.service.settings
    settings.kaiwu_device_export_tenants = ("tenant-a",)
    settings.kaiwu_edge_public_base = EDGE
    connector, relay, sign = enroll("tenant-a")
    path = export_path(connector)
    with client.websocket_connect("wss://testserver" + relay, headers=sign("GET", relay)) as ws:
        assert ws.receive_json() == {"type": "ready"}
        register(client, sign, SOURCE_ID)
        first = device_post(client, sign, path, {"source_id": SOURCE_ID})
        assert first.status_code == 200
        db = app.state.service.db
        db.execute("UPDATE kaiwu_integrations SET expires_at='2000-01-01T00:00:00+00:00'")
        second = device_post(client, sign, path, {"source_id": SOURCE_ID})
        assert second.status_code == 200 and second.json() != first.json()
        latest = db.one("SELECT id FROM kaiwu_integrations ORDER BY created_at DESC LIMIT 1")["id"]
        rotated = client.post(f"{BASE}/v1/integrations/{latest}/rotate", headers=ADMIN, json={})
        assert rotated.status_code == 200 and rotated.json()["mcp_config"] != second.json()["mcp_config"]
        assert device_post(client, sign, path, {"source_id": SOURCE_ID}).json()["error"]["code"] == "DEVICE_EXPORT_TOKEN_CHANGED"
        assert db.one("SELECT count(*) AS n FROM kaiwu_integrations")["n"] == 2


def test_export_status_explains_rejection_without_granting_or_leaking(native):
    client, app, enroll = native
    settings = app.state.service.settings
    settings.kaiwu_device_export_tenants = ()
    settings.kaiwu_edge_public_base = EDGE
    connector, _, sign = enroll("tenant-a")
    other, _, other_sign = enroll("tenant-b")
    status_path = export_path(connector) + "-status"
    assert client.get(status_path).status_code == 401
    assert client.get(status_path, headers=sign("GET", status_path)).status_code == 403
    assert client.get(status_path, headers=other_sign("GET", status_path)).status_code == 403
    before = client.get(status_path, headers=ADMIN)
    assert before.status_code == 200
    assert before.json()["tenant_export_allowed"] is False
    assert before.json()["last_rejection"] is None
    assert device_post(client, sign, export_path(connector), {"source_id": SOURCE_ID}).status_code == 403
    after = client.get(status_path, headers=ADMIN)
    assert after.headers["cache-control"] == "no-store"
    result = after.json()
    assert result["tenant_id"] == "tenant-a"
    assert result["last_rejection"]["reason_code"] == "DEVICE_EXPORT_DENIED"
    assert result["last_rejection"]["http_status"] == 403
    assert set(result["last_rejection"]) == {"reason_code", "http_status", "trace_id", "occurred_at"}
    assert SOURCE_ID not in after.text and "mcp_config" not in after.text
    assert client.get(export_path(other) + "-status", headers=ADMIN).json()["last_rejection"] is None
    assert not app.state.service.db.all("SELECT * FROM kaiwu_integrations")
    settings.kaiwu_device_export_tenants = ("tenant-a",)
    assert client.get(status_path, headers=ADMIN).json()["tenant_export_allowed"] is True
    assert client.get(status_path, headers=ADMIN).json()["last_rejection"]["reason_code"] == "DEVICE_EXPORT_DENIED"
