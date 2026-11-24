"""Synthetic tests for the built-in demo MCP cloud self-test.

The success path uses a controlled HTTPS ASGI transport while the Edge's real
authentication router and a local synthetic MCP upstream are still exercised.
This is not evidence of a public Internet or Windows acceptance run.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import kaiwu_control
from app.config import Settings
from app.main import create_app


ADMIN_TOKEN = "admin-control-token-" + "e" * 32
BASE = "/api/kaiwu"
EDGE_PREFIX = "/projects/kaiwu/mcp"
DEMO_NAME = "开物内置测试服务（示例数据）"
PING = "kaiwu_demo_ping"
ORDER = "kaiwu_demo_order_summary"
INVENTORY = "kaiwu_demo_inventory_query"
MARKER = "KAIWU_BUILTIN_DEMO_V1"
EDGE_PUBLIC_BASE = "https://edge.example.test" + EDGE_PREFIX


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def json_body(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def demo_source_id(connector_id: str) -> str:
    digest = hashlib.sha256(
        ("kaiwu_builtin_demo_v1:" + connector_id).encode("utf-8")
    ).hexdigest()
    return "src_" + digest[:32]


def key_material():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return key, b64url(public), b64url(hashlib.sha256(public).digest())


def signed_headers(key, key_id: str, method: str, path: str, body: bytes) -> dict:
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
        kaiwu_control_edge_user="edge-self-test",
        kaiwu_edge_public_base=EDGE_PUBLIC_BASE,
        kaiwu_edge_mount_path=EDGE_PREFIX,
        kaiwu_edge_visitor_host="127.0.0.1",
        kaiwu_edge_visitor_port_start=38100,
        kaiwu_edge_visitor_port_end=38120,
        kaiwu_edge_request_timeout_seconds=2.0,
    )


def enroll(client, material) -> dict:
    _, public, _ = material
    created = client.post(
        BASE + "/v1/enrollments",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={"tenant_id": "tenant-demo", "tenant_name": "合成测试企业"},
    )
    assert created.status_code == 200, created.text
    redeemed = client.post(
        BASE + "/v1/enrollments/redeem",
        json={"code": created.json()["code"], "device_public_key": public},
    )
    assert redeemed.status_code == 200, redeemed.text
    return redeemed.json()


def register_source(
    client,
    identity: dict,
    material,
    *,
    source_id: str | None = None,
    display_name: str = DEMO_NAME,
    tools: list[str] | None = None,
) -> str:
    source_id = source_id or demo_source_id(identity["connector_id"])
    key, _, key_id = material
    path = BASE + "/v1/sources"
    body = json_body({
        "source_id": source_id,
        "display_name": display_name,
        "allowed_tools": tools or [PING, ORDER, INVENTORY],
        "tool_schema_sha256": "2" * 64,
        "target_binding_sha256": "3" * 64,
        "transport": "streamable_http",
    })
    response = client.post(
        path,
        content=body,
        headers={
            "Authorization": "Bearer " + identity["device_token"],
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, body),
        },
    )
    assert response.status_code == 200, response.text
    return source_id


class _UpstreamState:
    def __init__(self):
        self.requests: list[dict] = []
        self.mode = "success"
        self.lock = threading.Lock()


class _DemoMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _send(
        self,
        status: int,
        payload: dict | None = None,
        *,
        session: str = "",
    ) -> None:
        data = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def do_POST(self):
        state: _UpstreamState = self.server.state
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        try:
            request = json.loads(body)
        except ValueError:
            self._send(400, {"error": {"code": "INVALID_JSON"}})
            return
        with state.lock:
            state.requests.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "session": self.headers.get("Mcp-Session-Id", ""),
                "trace": self.headers.get("X-Kaiwu-Trace-ID", ""),
                "body": request,
            })
        if state.mode == "http500":
            self._send(500, {"error": {"code": "SYNTHETIC_UPSTREAM_FAILURE"}})
            return

        method = request.get("method")
        if method == "initialize":
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "kaiwu-demo", "version": "1.3.8"},
                    },
                },
                session="upstream-demo-session",
            )
            return
        if method == "notifications/initialized":
            self._send(202)
            return
        if method == "tools/list":
            self._send(200, {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {"tools": [{"name": PING}, {"name": ORDER}, {"name": INVENTORY}]},
            })
            return
        if method == "tools/call":
            if state.mode == "tool_error":
                self._send(200, {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32000, "message": "synthetic tool failure"},
                })
                return
            params = request.get("params") or {}
            nonce = (params.get("arguments") or {}).get("nonce", "")
            if state.mode == "bad_nonce":
                nonce = "different"
            probe_time = datetime.now(timezone.utc).isoformat()
            if state.mode == "bad_time":
                probe_time = "2020-01-01T00:00:00+00:00"
            marker = "WRONG_MARKER" if state.mode == "bad_marker" else MARKER
            probe = {
                "demo": True,
                "marker": marker,
                "nonce": nonce,
                "server_time_utc": probe_time,
                "instance_id": "demo-instance-1",
            }
            self._send(200, {
                "jsonrpc": "2.0",
                "id": request.get("id"),
                "result": {
                    "content": [{"type": "text", "text": json.dumps(probe)}],
                    "structuredContent": probe,
                    "isError": False,
                },
            })
            return
        self._send(400, {"error": {"code": "UNEXPECTED_METHOD"}})


@pytest.fixture
def env(tmp_path, monkeypatch):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _DemoMCPHandler)
    state = _UpstreamState()
    upstream.state = state
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    app = create_app(settings(tmp_path))
    try:
        with TestClient(app) as client:
            material = key_material()
            identity = enroll(client, material)
            source_id = register_source(client, identity, material)
            app.state.service.db.execute(
                "UPDATE kaiwu_edge_routes SET visitor_port=? "
                "WHERE connector_id=? AND source_id=?",
                (upstream.server_address[1], identity["connector_id"], source_id),
            )
            original = kaiwu_control._execute_self_test

            async def controlled_https_transport(*args, **kwargs):
                # This is a synthetic HTTPS transport, not a public deployment.
                kwargs["transport"] = httpx.ASGITransport(app=app)
                return await original(*args, **kwargs)

            monkeypatch.setattr(
                kaiwu_control,
                "_execute_self_test",
                controlled_https_transport,
            )
            yield client, app, identity, material, state, source_id
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def self_test(
    client,
    identity: dict,
    material,
    *,
    source_id: str | None = None,
    nonce: str = "roundtrip-123",
    connector_id: str | None = None,
    extra: dict | None = None,
):
    key, _, key_id = material
    connector_id = connector_id or identity["connector_id"]
    source_id = source_id or demo_source_id(connector_id)
    path = BASE + f"/v1/connectors/{connector_id}/self-test"
    body_value = {"source_id": source_id, "nonce": nonce}
    body_value.update(extra or {})
    body = json_body(body_value)
    return client.post(
        path,
        content=body,
        headers={
            "Authorization": "Bearer " + identity["device_token"],
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, body),
        },
    )


def cleanup_events(app):
    return app.state.service.db.all(
        "SELECT * FROM kaiwu_control_audit "
        "WHERE action=? ORDER BY id",
        (kaiwu_control.SELF_TEST_CLEANUP_ACTION,),
    )


def test_self_test_uses_fixed_edge_tool_and_discards_temporary_credential(env):
    client, app, identity, material, state, source_id = env
    response = self_test(
        client,
        identity,
        material,
        nonce="nonce-roundtrip-456",
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["ok"] is True
    assert data["trace_id"].startswith("kwtr_")
    assert data["source_id"] == source_id == demo_source_id(identity["connector_id"])
    assert [item["status"] for item in data["stages"]] == ["passed"] * 5
    assert data["probe"] == {
        "demo": True,
        "marker": MARKER,
        "nonce": "nonce-roundtrip-456",
        "server_time_utc": data["probe"]["server_time_utc"],
        "instance_id": "demo-instance-1",
    }
    assert response.headers["x-kaiwu-trace-id"] == data["trace_id"]

    assert [item["body"].get("method") for item in state.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert {item["path"] for item in state.requests} == {"/mcp"}
    assert all(item["authorization"] == "" for item in state.requests)
    assert state.requests[0]["session"] == ""
    assert all(item["session"] == "upstream-demo-session" for item in state.requests[1:])
    call = state.requests[-1]["body"]
    assert call["params"] == {"name": PING, "arguments": {"nonce": "nonce-roundtrip-456"}}
    assert "url" not in json.dumps(call)
    assert app.state.service.db.one("SELECT COUNT(*) AS n FROM kaiwu_integrations")["n"] == 0
    assert app.state.service.db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_integration_sources"
    )["n"] == 0

    cleanup = cleanup_events(app)
    assert len(cleanup) == 1
    assert cleanup[0]["outcome"] == "allow"
    assert cleanup[0]["trace_id"] == data["trace_id"]
    assert cleanup[0]["source_id"] == source_id
    assert json.loads(cleanup[0]["detail"]) == {
        "result": "succeeded",
        "tool_name": PING,
    }


def test_self_test_source_identity_is_computed_and_display_name_is_not_trusted(env):
    client, app, identity, material, state, source_id = env
    assert source_id == "src_" + hashlib.sha256(
        ("kaiwu_builtin_demo_v1:" + identity["connector_id"]).encode("utf-8")
    ).hexdigest()[:32]

    app.state.service.db.execute(
        "UPDATE kaiwu_sources SET display_name=? WHERE connector_id=? AND source_id=?",
        ("客户可编辑的展示名称", identity["connector_id"], source_id),
    )
    accepted = self_test(client, identity, material)
    assert accepted.status_code == 200, accepted.text

    ordinary_name_demo_id = "src_" + "b" * 32
    register_source(
        client,
        identity,
        material,
        source_id=ordinary_name_demo_id,
        display_name=DEMO_NAME,
    )
    wrong_source = self_test(
        client,
        identity,
        material,
        source_id=ordinary_name_demo_id,
    )
    assert wrong_source.status_code == 403
    assert wrong_source.json()["error"]["code"] == "SELF_TEST_SOURCE_DENIED"

    app.state.service.db.execute(
        "UPDATE kaiwu_sources SET allowed_tools=? WHERE connector_id=? AND source_id=?",
        (json.dumps([PING, ORDER]), identity["connector_id"], source_id),
    )
    wrong_tools = self_test(client, identity, material)
    assert wrong_tools.status_code == 403
    assert wrong_tools.json()["error"]["code"] == "SELF_TEST_SOURCE_DENIED"
    assert state.requests


def test_self_test_rejects_extra_fields_wrong_tenant_and_wrong_connector(env):
    client, app, identity, material, state, source_id = env
    extra = self_test(client, identity, material, extra={"url": "https://evil.invalid"})
    assert extra.status_code == 422
    assert state.requests == []

    unknown = self_test(client, identity, material, source_id="src_" + "d" * 32)
    assert unknown.status_code == 403
    assert unknown.json()["error"]["code"] == "SELF_TEST_SOURCE_DENIED"
    assert state.requests == []

    app.state.service.db.execute(
        "UPDATE kaiwu_sources SET tenant_id='tenant-other' "
        "WHERE connector_id=? AND source_id=?",
        (identity["connector_id"], source_id),
    )
    wrong_tenant = self_test(client, identity, material)
    assert wrong_tenant.status_code == 403
    assert wrong_tenant.json()["error"]["code"] == "SELF_TEST_SOURCE_DENIED"
    assert state.requests == []

    wrong_connector_id = "conn_" + "f" * 32
    wrong_connector = self_test(
        client,
        identity,
        material,
        connector_id=wrong_connector_id,
    )
    assert wrong_connector.status_code == 403
    assert wrong_connector.json()["error"]["code"] == "DEVICE_SCOPE_DENIED"
    assert state.requests == []


@pytest.mark.parametrize("base", [
    "http://127.0.0.1:38100" + EDGE_PREFIX,
    "http://testserver" + EDGE_PREFIX,
    "https://localhost" + EDGE_PREFIX,
    "https://localhost." + EDGE_PREFIX,
    "https://x.localhost." + EDGE_PREFIX,
    "https://127.0.0.1:38100" + EDGE_PREFIX,
])
def test_self_test_never_inherits_local_http_or_loopback_edge(env, base):
    client, app, identity, material, state, _ = env
    app.state.service.settings.kaiwu_edge_public_base = base
    response = self_test(client, identity, material)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SELF_TEST_EDGE_HTTPS_REQUIRED"
    assert state.requests == []
    assert app.state.service.db.one("SELECT COUNT(*) AS n FROM kaiwu_integrations")["n"] == 0


def test_self_test_reports_layered_failures_and_always_cleans_up(env):
    client, app, identity, material, state, source_id = env
    before_diagnosis_success = app.state.service.db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events "
        "WHERE event_code='diagnosis_succeeded'"
    )["n"]
    before_diagnosis_failed = app.state.service.db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events "
        "WHERE event_code='diagnosis_failed'"
    )["n"]
    state.mode = "http500"
    failed = self_test(client, identity, material, nonce="failure-http")
    assert failed.status_code == 502
    failed_data = failed.json()["data"]
    assert failed_data["ok"] is False
    assert [item["status"] for item in failed_data["stages"]] == [
        "failed", "skipped", "skipped", "skipped", "skipped",
    ]
    assert failed_data["stages"][0]["id"] == "edge_connect"
    assert app.state.service.db.one("SELECT COUNT(*) AS n FROM kaiwu_integrations")["n"] == 0
    assert app.state.service.db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events "
        "WHERE event_code='diagnosis_succeeded'"
    )["n"] == before_diagnosis_success
    assert app.state.service.db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events "
        "WHERE event_code='diagnosis_failed'"
    )["n"] == before_diagnosis_failed
    failed_cleanup = cleanup_events(app)[-1]
    assert failed_cleanup["outcome"] == "allow"
    assert failed_cleanup["trace_id"] == failed_data["trace_id"]
    assert json.loads(failed_cleanup["detail"])["result"] == "succeeded"

    state.mode = "tool_error"
    state.requests.clear()
    tool_failed = self_test(client, identity, material, nonce="failure-tool")
    assert tool_failed.status_code == 502
    assert [item["status"] for item in tool_failed.json()["data"]["stages"]] == [
        "passed", "passed", "passed", "failed", "skipped",
    ]
    assert app.state.service.db.one("SELECT COUNT(*) AS n FROM kaiwu_integrations")["n"] == 0

    state.mode = "bad_nonce"
    state.requests.clear()
    probe_failed = self_test(client, identity, material, nonce="failure-nonce")
    assert probe_failed.status_code == 502
    assert [item["status"] for item in probe_failed.json()["data"]["stages"]] == [
        "passed", "passed", "passed", "passed", "failed",
    ]
    assert probe_failed.json()["data"]["probe"] == {}
    assert app.state.service.db.one("SELECT COUNT(*) AS n FROM kaiwu_integrations")["n"] == 0
    assert cleanup_events(app)[-1]["source_id"] == source_id


def test_edge_temporary_k_allows_discovery_but_denies_other_tools(env):
    client, app, identity, material, state, source_id = env
    connector = app.state.service.db.one(
        "SELECT * FROM kaiwu_connectors WHERE id=?",
        (identity["connector_id"],),
    )
    integration_id, token = kaiwu_control._create_self_test_integration(
        app.state.service.db,
        connector,
        source_id,
        2.0,
    )
    endpoint = f"{EDGE_PREFIX}/sources/{source_id}/mcp"
    try:
        initialized = client.post(
            endpoint,
            headers={"Authorization": "Bearer " + token},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        assert initialized.status_code == 200, initialized.text
        session_id = initialized.headers["mcp-session-id"]

        listed = client.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + token,
                "Mcp-Session-Id": session_id,
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200, listed.text
        assert {item["name"] for item in listed.json()["result"]["tools"]} == {
            PING, ORDER, INVENTORY,
        }

        before = len(state.requests)
        other_tool = client.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + token,
                "Mcp-Session-Id": session_id,
            },
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": ORDER, "arguments": {}},
            },
        )
        assert other_tool.status_code == 403
        assert other_tool.json()["error"]["code"] == "SELF_TEST_TOOL_DENIED"

        extra_argument = client.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + token,
                "Mcp-Session-Id": session_id,
            },
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": PING, "arguments": {"nonce": "abc", "extra": True}},
            },
        )
        assert extra_argument.status_code == 403
        assert extra_argument.json()["error"]["code"] == "SELF_TEST_TOOL_DENIED"

        batch = client.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + token,
                "Mcp-Session-Id": session_id,
            },
            json=[
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": PING, "arguments": {"nonce": "batch-good"}},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {"name": INVENTORY, "arguments": {}},
                },
            ],
        )
        assert batch.status_code == 403
        assert batch.json()["error"]["code"] == "SELF_TEST_TOOL_DENIED"
        assert len(state.requests) == before
    finally:
        kaiwu_control._revoke_self_test_integration(
            app.state.service.db,
            integration_id,
        )
    assert app.state.service.db.one("SELECT COUNT(*) AS n FROM kaiwu_integrations")["n"] == 0


def test_independent_demo_slot_keeps_the_business_cap_and_legacy_route(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        key, public, key_id = key_material()
        identity = enroll(client, (key, public, key_id))
        connector_id = identity["connector_id"]
        demo_id = demo_source_id(connector_id)

        def device_post(route, payload):
            path = BASE + route
            body = json_body(payload)
            return client.post(path, content=body, headers={
                "Authorization": "Bearer " + identity["device_token"],
                "Content-Type": "application/json",
                **signed_headers(key, key_id, "POST", path, body),
            })

        def register(source_id, tools, binding="3" * 64, name="合成来源"):
            return device_post("/v1/sources", {
                "source_id": source_id,
                "display_name": name,
                "allowed_tools": tools,
                "tool_schema_sha256": "2" * 64,
                "target_binding_sha256": binding,
                "transport": "streamable_http",
            })

        business = [f"src_{index:032x}" for index in range(1, 17)]
        for source_id in business:
            assert register(source_id, ["lookup_order"]).status_code == 200
        assert register(demo_id, ["lookup_order"], name=DEMO_NAME).status_code == 409
        assert register("src_" + "a" * 32, [PING, ORDER, INVENTORY]).status_code == 409
        legacy = register(demo_id, [PING, ORDER, INVENTORY], name=DEMO_NAME)
        assert legacy.status_code == 200, legacy.text
        db = app.state.service.db

        def route():
            return db.one(
                "SELECT * FROM kaiwu_edge_routes WHERE connector_id=? AND source_id=?",
                (connector_id, demo_id),
            )

        original_route = route()
        stable = hashlib.sha256(f"kaiwu_builtin_demo_v1:{demo_id}".encode()).hexdigest()
        migrated = register(demo_id, [PING, ORDER, INVENTORY], stable, name=DEMO_NAME)
        assert migrated.status_code == 200, migrated.text
        assert migrated.json()["source"]["target_binding_sha256"] == stable
        assert migrated.json()["source"]["revision"] == legacy.json()["source"]["revision"]
        assert route() == original_route
        assert register(business[0], ["lookup_order"], stable).status_code == 409

        source_ids = business + [demo_id]
        integration = client.post(
            BASE + "/v1/integrations",
            headers={"Authorization": "Bearer " + ADMIN_TOKEN},
            json={
                "tenant_id": identity["tenant_id"], "connector_id": connector_id,
                "display_name": "合成全部来源", "source_ids": source_ids,
            },
        )
        assert integration.status_code == 201, integration.text
        assert set(integration.json()["integration"]["source_ids"]) == set(source_ids)

        heartbeat = device_post(f"/v1/connectors/{connector_id}/heartbeat", {
            "state": "READY", "version": "test",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_ids": source_ids, "last_error": "",
        })
        assert heartbeat.status_code == 200, heartbeat.text
