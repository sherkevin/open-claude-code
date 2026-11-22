"""MCP Edge and the signed connector WebSocket share one native relay."""
from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.kaiwu_native_relay import NATIVE_RELAY_PATH
from app.main import create_app
from test_kaiwu_enterprise import ADMIN_TOKEN, BASE, SOURCE_ID, json_body, key_material, settings, signed_headers


ADMIN = {"Authorization": "Bearer " + ADMIN_TOKEN}
SOURCE = {
    "source_id": SOURCE_ID,
    "display_name": "订单查询",
    "allowed_tools": ["lookup_order"],
    "tool_schema_sha256": "2" * 64,
    "target_binding_sha256": "3" * 64,
    "transport": "streamable_http",
}


@pytest.fixture
def native(tmp_path):
    app = create_app(replace(
        settings(tmp_path),
        kaiwu_native_relay_enabled=True,
        kaiwu_control_relay_address="",
        kaiwu_control_relay_port=0,
    ))
    with TestClient(app) as client:
        def enroll(tenant_id):
            key, public_key, key_id = key_material()
            enrollment = client.post(
                BASE + "/v1/enrollments", headers=ADMIN,
                json={"tenant_id": tenant_id, "tenant_name": "合成企业"},
            )
            assert enrollment.status_code == 200, enrollment.text
            redeemed = client.post(BASE + "/v1/enrollments/redeem", json={
                "code": enrollment.json()["code"], "device_public_key": public_key,
            })
            assert redeemed.status_code == 200, redeemed.text
            identity = redeemed.json()
            path = NATIVE_RELAY_PATH.format(connector_id=identity["connector_id"])

            def sign(method, resource, body=b""):
                return {
                    "Authorization": "Bearer " + identity["device_token"],
                    **signed_headers(key, key_id, method, resource, body),
                }

            return identity["connector_id"], path, sign

        yield client, app, enroll


def register_source(client, sign):
    path = BASE + "/v1/sources"
    body = json_body(SOURCE)
    registered = client.post(
        path, content=body,
        headers={"Content-Type": "application/json", **sign("POST", path, body)},
    )
    assert registered.status_code == 200, registered.text


def issue_config(client, connector_id, tenant_id):
    response = client.post(BASE + "/v1/integrations", headers=ADMIN, json={
        "tenant_id": tenant_id, "connector_id": connector_id,
        "display_name": "订单查询", "source_ids": [SOURCE_ID],
    })
    assert response.status_code == 201, response.text
    return response.json()["mcp_config"]["mcpServers"]["订单查询"]


def send_result(ws, request, body, response_headers):
    exchange_id = request["id"]
    ws.send_json({
        "type": "response", "id": exchange_id, "status": 200, "headers": response_headers,
    })
    for chunk in body:
        ws.send_json({
            "type": "chunk", "id": exchange_id, "body": base64.b64encode(chunk).decode(),
        })
    ws.send_json({"type": "end", "id": exchange_id})


def test_native_relay_uses_same_json_and_preserves_session_and_sse(native, monkeypatch):
    client, app, enroll = native
    connector_id, path, sign = enroll("tenant-a")
    with client.websocket_connect("wss://testserver" + path, headers=sign("GET", path)) as ws:
        assert ws.receive_json() == {"type": "ready"}
        register_source(client, sign)
        assert not app.state.service.db.all("SELECT * FROM kaiwu_edge_routes")
        assert client.get(
            BASE + f"/v1/connectors/{connector_id}/data-plane", headers=ADMIN,
        ).json()["error"]["code"] == "NATIVE_RELAY_SELECTED"
        config = issue_config(client, connector_id, "tenant-a")
        assert config["url"].endswith(f"/sources/{SOURCE_ID}/mcp")

        def call(method, request_id, headers=None):
            return client.post(
                config["url"], headers={**config["headers"], **(headers or {})},
                json={"jsonrpc": "2.0", "method": method, "id": request_id},
            )

        with ThreadPoolExecutor() as executor:
            response = executor.submit(call, "initialize", 1)
            initialize = ws.receive_json()
            assert initialize["source_id"] == SOURCE_ID
            assert not {"Authorization", "url", "method"} & set(initialize["headers"])
            assert "url" not in initialize
            assert base64.b64decode(initialize["body"]).startswith(b'{"jsonrpc":"2.0"')
            send_result(
                ws, initialize, [b'{"jsonrpc":"2.0","id":1,"result":{}}'],
                {"Content-Type": "application/json", "Mcp-Session-Id": "internal-session"},
            )
            initialized = response.result(4)
            assert initialized.status_code == 200, initialized.text
            session = initialized.headers["Mcp-Session-Id"]
            assert session.startswith("edge_")

            response = executor.submit(call, "tools/list", 2, {"Mcp-Session-Id": session})
            list_tools = ws.receive_json()
            assert list_tools["headers"]["Mcp-Session-Id"] == "internal-session"
            sse = [b'event: message\ndata: {"jsonrpc":"2.0","id":2,', b'"result":{"tools":[]}}\n\n']
            send_result(ws, list_tools, sse, {"Content-Type": "text/event-stream"})
            listed = response.result(4)
            assert listed.headers["Content-Type"] == "text/event-stream"
            assert listed.content == b"".join(sse)

    # Even a stale visitor route may not take over when the WSS is offline.
    app.state.service.db.execute(
        "INSERT INTO kaiwu_edge_routes VALUES(?,?,?,?,?,?)",
        (connector_id, SOURCE_ID, "127.0.0.1", 37100, "unused", "unused"),
    )

    def unexpected_legacy_client(*args, **kwargs):
        pytest.fail("offline native connector cannot fall back to a legacy visitor")

    monkeypatch.setattr("app.kaiwu_edge.httpx.AsyncClient", unexpected_legacy_client)
    offline = call("tools/list", 3)
    assert offline.status_code == 503
    assert offline.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_native_relay_routes_concurrent_requests_by_connector_and_exchange(native):
    client, _, enroll = native
    first, path_a, sign_a = enroll("tenant-a")
    second, path_b, sign_b = enroll("tenant-b")
    with (
        client.websocket_connect("wss://testserver" + path_a, headers=sign_a("GET", path_a)) as ws_a,
        client.websocket_connect("wss://testserver" + path_b, headers=sign_b("GET", path_b)) as ws_b,
    ):
        assert ws_a.receive_json() == ws_b.receive_json() == {"type": "ready"}
        register_source(client, sign_a)
        register_source(client, sign_b)
        config_a = issue_config(client, first, "tenant-a")
        config_b = issue_config(client, second, "tenant-b")

        def call(config, request_id):
            return client.post(config["url"], headers=config["headers"], json={
                "jsonrpc": "2.0", "method": "tools/call", "id": request_id,
                "params": {"name": "lookup_order", "arguments": {}},
            })

        with ThreadPoolExecutor() as executor:
            jobs = [executor.submit(call, config_a, 1), executor.submit(call, config_a, 2),
                    executor.submit(call, config_b, 3)]
            exchanges_a = [ws_a.receive_json(), ws_a.receive_json()]
            exchange_b = ws_b.receive_json()
            assert len({exchange["id"] for exchange in [*exchanges_a, exchange_b]}) == 3
            for ws, label, request in (
                (ws_b, "b", exchange_b),
                (ws_a, "a", exchanges_a[1]),
                (ws_a, "a", exchanges_a[0]),
            ):
                request_id = json.loads(base64.b64decode(request["body"]))["id"]
                send_result(ws, request, [json_body({
                    "jsonrpc": "2.0", "id": request_id, "result": {"label": label},
                })], {"Content-Type": "application/json"})
            assert [(response.json()["id"], response.json()["result"]["label"])
                    for response in (job.result(4) for job in jobs)] == [(1, "a"), (2, "a"), (3, "b")]


def test_native_relay_rejects_unsigned_replay_and_revocation(native):
    client, app, enroll = native
    connector_id, path, sign = enroll("tenant-a")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver" + path):
            pytest.fail("unsigned connector was accepted")
    assert not app.state.service.db.all("SELECT * FROM kaiwu_native_relay_devices")
    headers = sign("GET", path)
    with client.websocket_connect("wss://testserver" + path, headers=headers) as ws:
        assert ws.receive_json() == {"type": "ready"}
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("wss://testserver" + path, headers=headers):
                pytest.fail("signed request was replayed")
        register_source(client, sign)
        config = issue_config(client, connector_id, "tenant-a")
        assert client.post(BASE + f"/v1/connectors/{connector_id}/revoke", headers=ADMIN).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
        result = client.post(config["url"], headers=config["headers"], json={
            "jsonrpc": "2.0", "method": "tools/list", "id": 1,
        })
        assert result.status_code == 401
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("wss://testserver" + path, headers=sign("GET", path)):
            pytest.fail("revoked connector was accepted")


def test_native_relay_cancels_timed_out_request_without_replay(native):
    client, app, enroll = native
    app.state.service.settings.kaiwu_edge_request_timeout_seconds = 1
    connector_id, path, sign = enroll("tenant-a")
    with client.websocket_connect("wss://testserver" + path, headers=sign("GET", path)) as ws:
        assert ws.receive_json() == {"type": "ready"}
        register_source(client, sign)
        config = issue_config(client, connector_id, "tenant-a")

        def list_tools(request_id):
            return client.post(config["url"], headers=config["headers"], json={
                "jsonrpc": "2.0", "method": "tools/list", "id": request_id,
            })

        with ThreadPoolExecutor() as executor:
            pending = executor.submit(list_tools, 1)
            expired = ws.receive_json()
            assert pending.result(4).status_code == 504
            assert ws.receive_json() == {"type": "cancel", "id": expired["id"]}
            subsequent = executor.submit(list_tools, 2)
            request = ws.receive_json()
            assert json.loads(base64.b64decode(request["body"]))["id"] == 2
            send_result(ws, request, [b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}'],
                        {"Content-Type": "application/json"})
            assert subsequent.result(4).status_code == 200
