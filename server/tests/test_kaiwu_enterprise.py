"""Synthetic enterprise integration and Edge acceptance tests."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.config import Settings
from app.kaiwu_bridge import derive_secret
from app.main import create_app
from app.mcp_import import parse_mcp_config


ADMIN_TOKEN = "admin-control-token-" + "b" * 32
BASE = "/api/kaiwu"
SOURCE_ID = "src_" + "a" * 32


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def key_material():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return key, b64url(public), b64url(hashlib.sha256(public).digest())


def json_body(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def signed_headers(key, key_id: str, method: str, path: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    nonce = b64url(hashlib.sha256(body + str(time.time_ns()).encode()).digest())
    message = "\n".join((
        timestamp,
        nonce,
        method.upper(),
        path,
        hashlib.sha256(body).hexdigest(),
    )).encode()
    return {
        "X-Kaiwu-Device-Key": key_id,
        "X-Kaiwu-Timestamp": timestamp,
        "X-Kaiwu-Nonce": nonce,
        "X-Kaiwu-Signature": b64url(key.sign(message)),
    }


def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        provider_settings="",
        origins=("http://testserver",),
        kaiwu_control_enabled=True,
        kaiwu_control_admin_token=ADMIN_TOKEN,
        kaiwu_control_relay_address="127.0.0.1",
        kaiwu_control_relay_port=37000,
        kaiwu_control_relay_protocol="tcp",
        kaiwu_control_relay_tls_enabled=False,
        kaiwu_control_relay_bind_address="127.0.0.1",
        kaiwu_control_edge_user="edge-synthetic",
        kaiwu_edge_public_base="http://testserver/projects/kaiwu/mcp",
        kaiwu_edge_mount_path="/projects/kaiwu/mcp",
        kaiwu_edge_visitor_host="127.0.0.1",
        kaiwu_edge_visitor_port_start=37100,
        kaiwu_edge_visitor_port_end=37120,
    )


@pytest.fixture
def env(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        key, public_key, key_id = key_material()
        enrollment = client.post(
            BASE + "/v1/enrollments",
            headers={"Authorization": "Bearer " + ADMIN_TOKEN},
            json={"tenant_id": "tenant-a", "tenant_name": "合成企业"},
        )
        assert enrollment.status_code == 200, enrollment.text
        redeemed = client.post(
            BASE + "/v1/enrollments/redeem",
            json={"code": enrollment.json()["code"], "device_public_key": public_key},
        )
        assert redeemed.status_code == 200, redeemed.text
        identity = redeemed.json()
        source = {
            "source_id": SOURCE_ID,
            "display_name": "订单只读",
            "allowed_tools": ["lookup_order"],
            "tool_schema_sha256": "2" * 64,
            "target_binding_sha256": "3" * 64,
            "transport": "streamable_http",
        }
        path = BASE + "/v1/sources"
        body = json_body(source)
        registered = client.post(
            path,
            content=body,
            headers={
                "Authorization": "Bearer " + identity["device_token"],
                "Content-Type": "application/json",
                **signed_headers(key, key_id, "POST", path, body),
            },
        )
        assert registered.status_code == 200, registered.text
        yield client, app, identity, key, key_id


def create_integration(client, identity, *, name="华微业务查询", source_ids=None):
    return client.post(
        BASE + "/v1/integrations",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={
            "tenant_id": identity["tenant_id"],
            "connector_id": identity["connector_id"],
            "display_name": name,
            "source_ids": source_ids or [SOURCE_ID],
            "ttl_seconds": 3600,
        },
    )


def test_enterprise_integration_lifecycle_and_connector_revoke(env):
    client, app, identity, _, _ = env
    forbidden = client.post(
        BASE + "/v1/integrations",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={
            "tenant_id": "tenant-other",
            "connector_id": identity["connector_id"],
            "display_name": "错误企业",
            "source_ids": [SOURCE_ID],
        },
    )
    assert forbidden.status_code == 403

    created = create_integration(client, identity)
    assert created.status_code == 201, created.text
    first = created.json()
    token = first["access_token"]
    config = first["mcp_config"]
    assert config["mcpServers"]["华微业务查询"]["url"].endswith(
        f"/sources/{SOURCE_ID}/mcp"
    )
    assert config["mcpServers"]["华微业务查询"]["headers"]["Authorization"] == "Bearer " + token
    parsed = parse_mcp_config(config, ("testserver",))
    assert parsed.connections[0].credential == token
    assert parsed.connections[0].url.endswith(f"/sources/{SOURCE_ID}/mcp")
    assert token not in json.dumps(app.state.service.db.all("SELECT * FROM kaiwu_integrations"))
    assert "lookup_order" not in json.dumps(config)

    listed = client.get(
        BASE + "/v1/integrations",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )
    assert listed.status_code == 200
    assert token not in listed.text
    assert listed.json()["integrations"][0]["token_prefix"].endswith(token[-6:])

    rotated = client.post(
        BASE + f"/v1/integrations/{first['integration']['integration_id']}/rotate",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={"ttl_seconds": 7200},
    )
    assert rotated.status_code == 200, rotated.text
    new_token = rotated.json()["access_token"]
    assert new_token != token
    assert new_token not in json.dumps(app.state.service.db.all("SELECT * FROM kaiwu_integrations"))

    revoked = client.post(
        BASE + f"/v1/integrations/{first['integration']['integration_id']}/revoke",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True

    second = create_integration(client, identity, name="第二个集成")
    assert second.status_code == 201
    assert app.state.service.db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_edge_routes WHERE connector_id=?",
        (identity["connector_id"],),
    )["n"] == 1
    connector = client.post(
        BASE + f"/v1/connectors/{identity['connector_id']}/revoke",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )
    assert connector.status_code == 200
    rows = app.state.service.db.all("SELECT * FROM kaiwu_integrations")
    assert all(row["revoked_at"] for row in rows)
    assert app.state.service.db.all(
        "SELECT * FROM kaiwu_edge_routes WHERE connector_id=?",
        (identity["connector_id"],),
    ) == []


def test_integration_is_not_created_when_edge_base_is_missing(env):
    client, app, identity, _, _ = env
    app.state.service.settings.kaiwu_edge_public_base = ""
    rejected = create_integration(client, identity)
    assert rejected.status_code == 503
    assert app.state.service.db.all("SELECT * FROM kaiwu_integrations") == []


class _MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        request = json.loads(body)
        self.requests.append({
            "authorization": self.headers.get("Authorization", ""),
            "session": self.headers.get("Mcp-Session-Id", ""),
            "method": request.get("method"),
        })
        method = request.get("method")
        if method == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "synthetic", "version": "1"},
                },
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "upstream-session-1")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        payload = {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": {"tools": [{"name": "lookup_order", "inputSchema": {"type": "object"}}]},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def mcp_http():
    _MCPHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], _MCPHandler.requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_edge_authentication_rotation_isolation_and_session_binding(env, mcp_http):
    client, app, identity, _, _ = env
    port, requests = mcp_http
    app.state.service.db.execute(
        "UPDATE kaiwu_edge_routes SET visitor_port=? WHERE connector_id=? AND source_id=?",
        (port, identity["connector_id"], SOURCE_ID),
    )
    first = create_integration(client, identity).json()
    token = first["access_token"]
    endpoint = f"/projects/kaiwu/mcp/sources/{SOURCE_ID}/mcp"

    invalid = client.post(
        endpoint,
        headers={"Authorization": "Bearer invalid"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert invalid.status_code == 401

    initialized = client.post(
        endpoint,
        headers={"Authorization": "Bearer " + token},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert initialized.status_code == 200, initialized.text
    edge_session = initialized.headers["mcp-session-id"]
    assert edge_session.startswith("edge_") and edge_session != "upstream-session-1"
    assert requests[-1]["authorization"] == ""

    listed = client.post(
        endpoint,
        headers={
            "Authorization": "Bearer " + token,
            "Mcp-Session-Id": edge_session,
        },
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    assert requests[-1]["session"] == "upstream-session-1"

    raw_upstream = client.post(
        endpoint,
        headers={
            "Authorization": "Bearer " + token,
            "Mcp-Session-Id": "upstream-session-1",
        },
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
    )
    assert raw_upstream.status_code == 404

    second = create_integration(client, identity, name="另一集成").json()
    cross_integration = client.post(
        endpoint,
        headers={
            "Authorization": "Bearer " + second["access_token"],
            "Mcp-Session-Id": edge_session,
        },
        json={"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
    )
    assert cross_integration.status_code == 404

    rotated = client.post(
        BASE + f"/v1/integrations/{first['integration']['integration_id']}/rotate",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={},
    )
    assert rotated.status_code == 200
    old_token = client.post(
        endpoint,
        headers={"Authorization": "Bearer " + token},
        json={"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {}},
    )
    assert old_token.status_code == 401
    new_token = client.post(
        endpoint,
        headers={"Authorization": "Bearer " + rotated.json()["access_token"]},
        json={"jsonrpc": "2.0", "id": 6, "method": "initialize", "params": {}},
    )
    assert new_token.status_code == 200

    app.state.service.db.execute(
        "UPDATE kaiwu_integrations SET expires_at=? WHERE id=?",
        ("2000-01-01T00:00:00+00:00", first["integration"]["integration_id"]),
    )
    expired = client.post(
        endpoint,
        headers={"Authorization": "Bearer " + rotated.json()["access_token"]},
        json={"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {}},
    )
    assert expired.status_code == 401

    app.state.service.db.execute(
        "UPDATE kaiwu_integrations SET expires_at=? WHERE id=?",
        ("2099-01-01T00:00:00+00:00", first["integration"]["integration_id"]),
    )
    client.post(
        BASE + f"/v1/connectors/{identity['connector_id']}/drain",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )
    drained = client.post(
        endpoint,
        headers={"Authorization": "Bearer " + rotated.json()["access_token"]},
        json={"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {}},
    )
    assert drained.status_code == 503
    client.post(
        BASE + f"/v1/connectors/{identity['connector_id']}/resume",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )
    client.post(
        BASE + f"/v1/integrations/{first['integration']['integration_id']}/revoke",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )
    revoked = client.post(
        endpoint,
        headers={"Authorization": "Bearer " + rotated.json()["access_token"]},
        json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}},
    )
    assert revoked.status_code == 401


class _ConcurrentMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    lock = threading.Lock()
    barrier = None
    overlap = False

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        method = request.get("method")
        request_id = request.get("id")
        session = self.headers.get("Mcp-Session-Id", "")
        if method == "initialize":
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "concurrent-synthetic", "version": "1"},
                },
            }
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", f"upstream-{request_id}")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if method == "tools/call":
            try:
                type(self).barrier.wait(timeout=3)
                type(self).overlap = True
            except threading.BrokenBarrierError:
                pass
        params = request.get("params") or {}
        sku = (params.get("arguments") or {}).get("sku", "")
        text = f"{session}|{sku}"
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def test_edge_concurrent_integrations_do_not_cross_results(env):
    client, app, identity, _, _ = env
    _ConcurrentMCPHandler.barrier = threading.Barrier(2)
    _ConcurrentMCPHandler.overlap = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ConcurrentMCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app.state.service.db.execute(
            "UPDATE kaiwu_edge_routes SET visitor_port=? WHERE connector_id=? AND source_id=?",
            (server.server_address[1], identity["connector_id"], SOURCE_ID),
        )
        first = create_integration(client, identity, name="并发集成一").json()
        second = create_integration(client, identity, name="并发集成二").json()
        endpoint = f"/projects/kaiwu/mcp/sources/{SOURCE_ID}/mcp"

        def initialize(token: str, request_id: int) -> str:
            response = client.post(
                endpoint,
                headers={"Authorization": "Bearer " + token},
                json={"jsonrpc": "2.0", "id": request_id, "method": "initialize", "params": {}},
            )
            assert response.status_code == 200, response.text
            return response.headers["mcp-session-id"]

        first_session = initialize(first["access_token"], 1)
        second_session = initialize(second["access_token"], 2)

        def call(token: str, session_id: str, request_id: int, sku: str):
            return client.post(
                endpoint,
                headers={
                    "Authorization": "Bearer " + token,
                    "Mcp-Session-Id": session_id,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "lookup_order", "arguments": {"sku": sku}},
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_result = pool.submit(
                call, first["access_token"], first_session, 2, "SKU-A"
            )
            second_result = pool.submit(
                call, second["access_token"], second_session, 3, "SKU-B"
            )
            first_response = first_result.result(timeout=10)
            second_response = second_result.result(timeout=10)

        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text
        assert _ConcurrentMCPHandler.overlap is True
        assert first_response.json()["result"]["content"][0]["text"] == "upstream-1|SKU-A"
        assert second_response.json()["result"]["content"][0]["text"] == "upstream-2|SKU-B"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n')
        self.wfile.flush()
        time.sleep(2)
        self.wfile.write(b': done\n\n')
        self.wfile.flush()


def test_edge_forwards_sse_content_type_and_allows_close(env):
    client, app, identity, _, _ = env
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        app.state.service.db.execute(
            "UPDATE kaiwu_edge_routes SET visitor_port=? WHERE connector_id=? AND source_id=?",
            (server.server_address[1], identity["connector_id"], SOURCE_ID),
        )
        token = create_integration(client, identity).json()["access_token"]
        endpoint = f"http://testserver/projects/kaiwu/mcp/sources/{SOURCE_ID}/mcp"
        response = client.post(
            endpoint,
            headers={"Authorization": "Bearer " + token},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "lookup_order"}},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert "event: message" in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_data_plane_secret_material_is_derived_and_not_cached(env):
    client, app, identity, _, _ = env
    response = client.get(
        BASE + f"/v1/connectors/{identity['connector_id']}/data-plane",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["cache-control"] == "no-store"
    service_settings = app.state.service.settings
    relay = derive_secret(service_settings, "relay", identity["connector_id"])
    stcp = derive_secret(
        service_settings,
        "stcp",
        f"{identity['connector_id']}:{SOURCE_ID}:1",
    )
    assert relay in payload["frps_toml"]
    assert stcp in payload["visitor_toml"]
    assert relay not in json.dumps(app.state.service.db.all("SELECT * FROM kaiwu_connectors"))
    assert stcp not in json.dumps(app.state.service.db.all("SELECT * FROM kaiwu_sources"))
