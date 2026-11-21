"""Synthetic acceptance tests for customer diagnostics and Edge tracing.

Everything here runs against a temporary SQLite database and a loopback MCP
fixture. No real connector, cloud deployment or customer data is used.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app import kaiwu_diagnostics as diag


ADMIN_TOKEN = "admin-control-token-" + "d" * 32
BASE = "/api/kaiwu"
SOURCE_A = "src_" + "a" * 32
SOURCE_B = "src_" + "b" * 32
EDGE_PREFIX = "/projects/kaiwu/mcp"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def key_material():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return key, b64url(public), b64url(hashlib.sha256(public).digest())


def json_body(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def signed_headers(
    key: Ed25519PrivateKey,
    key_id: str,
    method: str,
    path: str,
    body: bytes,
) -> dict[str, str]:
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
        kaiwu_edge_mount_path=EDGE_PREFIX,
        kaiwu_edge_visitor_host="127.0.0.1",
        kaiwu_edge_visitor_port_start=37100,
        kaiwu_edge_visitor_port_end=37120,
        kaiwu_edge_request_timeout_seconds=1.0,
    )


def enroll_and_source(
    client: TestClient,
    material,
    *,
    tenant_id: str,
    tenant_name: str,
    source_id: str,
    tools: list[str],
):
    key, public_key, key_id = material
    created = client.post(
        BASE + "/v1/enrollments",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={"tenant_id": tenant_id, "tenant_name": tenant_name},
    )
    assert created.status_code == 200, created.text
    redeemed = client.post(
        BASE + "/v1/enrollments/redeem",
        json={"code": created.json()["code"], "device_public_key": public_key},
    )
    assert redeemed.status_code == 200, redeemed.text
    identity = redeemed.json()
    source = {
        "source_id": source_id,
        "display_name": "合成来源",
        "allowed_tools": tools,
        "tool_schema_sha256": "2" * 64,
        "target_binding_sha256": "3" * 64,
        "transport": "streamable_http",
    }
    body = json_body(source)
    path = BASE + "/v1/sources"
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
    return identity


@pytest.fixture
def env(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client:
        material_a = key_material()
        material_b = key_material()
        identity_a = enroll_and_source(
            client,
            material_a,
            tenant_id="tenant-a",
            tenant_name="合成企业甲",
            source_id=SOURCE_A,
            tools=["lookup_order"],
        )
        identity_b = enroll_and_source(
            client,
            material_b,
            tenant_id="tenant-b",
            tenant_name="合成企业乙",
            source_id=SOURCE_B,
            tools=["read_orders"],
        )
        yield {
            "client": client,
            "app": app,
            "a": identity_a,
            "b": identity_b,
            "key_a": material_a[0],
            "key_id_a": material_a[2],
            "key_b": material_b[0],
            "key_id_b": material_b[2],
        }


def make_event(code: str = "connector_started", **overrides) -> dict:
    spec = diag.CATALOG[code]
    event = {
        "schema_version": 1,
        "event_id": "evt_" + secrets.token_hex(16),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "level": spec.level,
        "component": spec.component,
        "event_code": code,
        "message_code": code,
    }
    event.update(overrides)
    return event


def upload(client, identity, key, key_id, events, *, connector_id=None):
    connector_id = connector_id or identity["connector_id"]
    path = BASE + f"/v1/connectors/{connector_id}/events"
    body = json_body({"events": events})
    return client.post(
        path,
        content=body,
        headers={
            "Authorization": "Bearer " + identity["device_token"],
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, body),
        },
    )


def device_get(client, identity, key, key_id, connector_id, suffix):
    path = BASE + f"/v1/connectors/{connector_id}/{suffix}"
    signed_path = path.split("?", 1)[0]
    return client.get(
        path,
        headers={
            "Authorization": "Bearer " + identity["device_token"],
            **signed_headers(key, key_id, "GET", signed_path, b""),
        },
    )


def admin_get(client, query=""):
    return client.get(
        BASE + "/v1/diagnostics/events" + query,
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
    )


def test_upload_positive_negative_scope_and_idempotency(env):
    client, a = env["client"], env["a"]
    key, key_id = env["key_a"], env["key_id_a"]
    events = [
        make_event("connector_started"),
        make_event("connection_failed"),
        make_event("platform_request_failed", source_id=SOURCE_A, http_status=502),
    ]
    first = upload(client, a, key, key_id, events)
    assert first.status_code == 200, first.text
    assert first.json() == {"accepted": 3}
    again = upload(client, a, key, key_id, events)
    assert again.status_code == 200
    assert again.json() == {"accepted": 3}
    rows = env["app"].state.service.db.all("SELECT * FROM kaiwu_diagnostic_events")
    assert len(rows) == 3
    assert {row["origin"] for row in rows} == {"connector"}
    assert {row["tenant_id"] for row in rows} == {"tenant-a"}
    assert {row["connector_id"] for row in rows} == {a["connector_id"]}

    # One invalid event rejects the whole batch with no partial write.
    before = len(rows)
    mixed = [make_event("connector_started"), make_event("source_saved", level="info", component="auth")]
    rejected = upload(client, a, key, key_id, mixed)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "DIAGNOSTICS_EVENT_INVALID"
    assert env["app"].state.service.db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events"
    )["n"] == before

    # Unknown or free-form fields are rejected rather than stored.
    extra = make_event("connector_started")
    extra["message"] = "CANARY free text"
    extra_rejected = upload(client, a, key, key_id, [extra])
    assert extra_rejected.status_code == 422
    unknown_code = make_event("connector_started")
    unknown_code["event_code"] = unknown_code["message_code"] = "make_me_admin"
    assert upload(client, a, key, key_id, [unknown_code]).status_code == 422

    # Device A cannot write Device B's path or claim Device B's source.
    cross_path = upload(client, a, key, key_id, [make_event("connector_started")], connector_id=env["b"]["connector_id"])
    assert cross_path.status_code == 403
    assert cross_path.json()["error"]["code"] == "DEVICE_SCOPE_DENIED"
    cross_source = upload(client, a, key, key_id, [make_event("connector_started", source_id=SOURCE_B)])
    assert cross_source.status_code == 422
    assert cross_source.json()["error"]["code"] == "DIAGNOSTICS_EVENT_INVALID"
    tool_not_approved = upload(
        client,
        a,
        key,
        key_id,
        [make_event("mcp_tool_failed", source_id=SOURCE_A, tool_name="read_orders")],
    )
    assert tool_not_approved.status_code == 422

    # Batch and body limits.
    too_many = [make_event("connector_started") for _ in range(101)]
    assert upload(client, a, key, key_id, too_many).status_code == 422
    oversized_body = b'{"events":[' + b" " * (256 * 1024) + b"]}"
    path = BASE + f"/v1/connectors/{a['connector_id']}/events"
    assert client.post(
        path,
        content=oversized_body,
        headers={
            "Authorization": "Bearer " + a["device_token"],
            "Content-Type": "application/json",
            **signed_headers(key, key_id, "POST", path, oversized_body),
        },
    ).status_code in {413, 422}


def test_install_event_fields_scope_and_retry(env):
    client, identity = env["client"], env["a"]
    key, key_id = env["key_a"], env["key_id_a"]
    attempt_id = "a" * 32
    event_id = "evt_" + hashlib.sha256(f"kaiwu-install-v1/{attempt_id}/3".encode()).hexdigest()[:32]
    event = make_event(
        "install_stage_failed", event_id=event_id, install_attempt_id=attempt_id,
        install_seq=3, install_stage="service", install_outcome="failed", install_code="service_start",
        occurred_at=(datetime.now(timezone.utc) - timedelta(days=15)).isoformat(),
    )
    negative = [
        dict(event, install_seq=49),
        dict(event, install_stage="free form"),
        dict(event, install_outcome="ok"),
        dict(event, install_code="raw error"),
        dict(event, event_id="evt_" + "b" * 32),
        dict(event, source_id=SOURCE_A),
        dict(make_event(), install_code="ok"),
    ]
    for item in negative:
        response = upload(client, identity, key, key_id, [item])
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "DIAGNOSTICS_EVENT_INVALID"
    assert upload(client, identity, key, key_id, [event], connector_id=env["b"]["connector_id"]).status_code == 403
    for _ in range(2):
        assert upload(client, identity, key, key_id, [event]).json() == {"accepted": 1}
    diag.prune(env["app"].state.service.db)
    rows = admin_get(client, f"?install_attempt_id={attempt_id}").json()["events"]
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "tenant-a"
    assert rows[0]["connector_id"] == identity["connector_id"]
    assert {field: rows[0][field] for field in diag.INSTALL_FIELDS} == {
        field: event[field] for field in diag.INSTALL_FIELDS
    }
    assert admin_get(client, "?install_attempt_id=invalid").status_code == 422
    assert device_get(client, identity, key, key_id, identity["connector_id"], "platform-events").json()["events"] == []
    assert device_get(client, identity, key, key_id, identity["connector_id"], "events").status_code == 405
    revoke_path = BASE + f"/v1/connectors/{identity['connector_id']}/self-revoke"
    revoked = client.post(
        revoke_path,
        headers={"Authorization": "Bearer " + identity["device_token"],
                 **signed_headers(key, key_id, "POST", revoke_path, b"")},
    )
    assert revoked.status_code == 200, revoked.text
    assert upload(client, identity, key, key_id, [event]).json()["error"]["code"] == "DEVICE_REVOKED"


def test_rpc_error_code_contract_boundaries(env):
    client, a = env["client"], env["a"]
    key, key_id = env["key_a"], env["key_id_a"]
    accepted = ["-32700", "-32603", "-32000", "-32099"]
    rejected = ["32000", "-32999", "-31999", "TOOL_ERROR", "TIMEOUT", "0", ""]
    for code in accepted:
        event = make_event("platform_request_failed", source_id=SOURCE_A, rpc_error_code=code)
        response = upload(client, a, key, key_id, [event])
        assert response.status_code == 200, (code, response.text)
    for code in rejected:
        event = make_event("platform_request_failed", source_id=SOURCE_A)
        event["rpc_error_code"] = code
        response = upload(client, a, key, key_id, [event])
        assert response.status_code == 422, (code, response.text)


def test_admin_read_cursor_filters_and_credential_isolation(env):
    client, a, b = env["client"], env["a"], env["b"]
    for index in range(5):
        events = [make_event("connector_started", event_id="evt_" + f"{index:032x}")]
        assert upload(client, a, env["key_a"], env["key_id_a"], events).status_code == 200
    for index in range(2):
        events = [make_event("connector_started", event_id="evt_" + f"{100 + index:032x}")]
        assert upload(client, b, env["key_b"], env["key_id_b"], events).status_code == 200

    device_on_admin = device_get(client, a, env["key_a"], env["key_id_a"], a["connector_id"], "platform-events")
    assert device_on_admin.status_code == 200
    assert set(device_on_admin.json()) == {"events"}

    denied = client.get(
        BASE + "/v1/diagnostics/events",
        headers={"Authorization": "Bearer " + a["device_token"]},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "CREDENTIAL_PLANE_DENIED"
    admin_as_device = client.post(
        BASE + f"/v1/connectors/{a['connector_id']}/events",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={"events": []},
    )
    assert admin_as_device.status_code == 403
    assert admin_as_device.json()["error"]["code"] == "CREDENTIAL_PLANE_DENIED"
    assert client.get(BASE + "/v1/diagnostics/events").status_code == 401

    first = admin_get(client, "?limit=2")
    assert first.status_code == 200
    assert len(first.json()["events"]) == 2
    cursor = first.json()["next_cursor"]
    assert cursor
    second = admin_get(client, f"?limit=2&cursor={cursor}")
    assert second.status_code == 200
    ids = [item["event_id"] for item in first.json()["events"] + second.json()["events"]]
    assert len(ids) == len(set(ids))
    assert all(item["origin"] == "connector" for item in first.json()["events"])

    only_b = admin_get(client, f"?connector_id={b['connector_id']}")
    assert only_b.status_code == 200
    assert {item["connector_id"] for item in only_b.json()["events"]} == {b["connector_id"]}
    assert admin_get(client, "?level=critical").status_code == 422
    assert admin_get(client, "?cursor=abc").status_code == 422


def test_retention_and_origin_separation(env):
    client, a = env["client"], env["a"]
    db = env["app"].state.service.db
    now_value = datetime.now(timezone.utc)
    rows = []
    for index in range(5):
        event = make_event("connector_started", event_id="evt_" + f"{200 + index:032x}")
        rows.append(diag.normalize_event(
            diag.DiagnosticEvent.model_validate(event),
            {SOURCE_A: {"lookup_order"}},
        ))
    diag.insert_rows(db, "tenant-a", a["connector_id"], rows, origin="connector")
    diag.prune(db, current=now_value, max_per_connector=2, max_global=1000)
    assert db.one("SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events")["n"] == 2

    old = make_event("connector_started", occurred_at=(now_value - timedelta(days=15)).isoformat())
    old_row = diag.normalize_event(diag.DiagnosticEvent.model_validate(old), {SOURCE_A: {"lookup_order"}})
    fresh = make_event("connector_started")
    fresh_row = diag.normalize_event(diag.DiagnosticEvent.model_validate(fresh), {SOURCE_A: {"lookup_order"}})
    diag.insert_rows(db, "tenant-a", a["connector_id"], [old_row, fresh_row], origin="connector")
    diag.prune(db, current=now_value)
    assert db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events WHERE event_id=?",
        (old_row["event_id"],),
    )["n"] == 0
    assert db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events WHERE event_id=?",
        (fresh_row["event_id"],),
    )["n"] == 1

    # A connector-originated platform event is not shown on the device page;
    # only Edge-derived outcomes are.
    connector_platform = make_event("platform_request_succeeded", source_id=SOURCE_A)
    assert upload(client, a, env["key_a"], env["key_id_a"], [connector_platform]).status_code == 200
    diag.record_platform_event(
        db,
        tenant_id="tenant-a",
        connector_id=a["connector_id"],
        source_id=SOURCE_A,
        tool_name="lookup_order",
        trace_id="kwtr_" + "e" * 32,
        success=True,
        http_status=200,
        rpc_error_code="",
        duration_ms=5,
    )
    page = device_get(client, a, env["key_a"], env["key_id_a"], a["connector_id"], "platform-events?limit=50")
    assert page.status_code == 200
    payload = page.json()
    assert set(payload) == {"events"}
    assert len(payload["events"]) == 1
    assert payload["events"][0]["trace_id"] == "kwtr_" + "e" * 32
    assert "origin" not in payload["events"][0]
    admin = admin_get(client, f"?connector_id={a['connector_id']}")
    origins = {item["origin"] for item in admin.json()["events"]}
    assert {"connector", "edge"} <= origins

    # Edge-recorded events also trigger the same retention pass.
    diag._last_prune.clear()
    db.execute(
        "INSERT INTO kaiwu_diagnostic_events"
        "(tenant_id,connector_id,event_id,occurred_at,level,component,event_code,message_code,origin,received_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "tenant-a",
            a["connector_id"],
            "evt_" + "f" * 32,
            (now_value - timedelta(days=20)).isoformat(),
            "info",
            "connector",
            "connector_started",
            "connector_started",
            "edge",
            (now_value - timedelta(days=20)).isoformat(),
        ),
    )
    diag.record_platform_event(
        db,
        tenant_id="tenant-a",
        connector_id=a["connector_id"],
        source_id=SOURCE_A,
        tool_name="lookup_order",
        trace_id="kwtr_" + "1" * 32,
        success=True,
        http_status=200,
        rpc_error_code="",
        duration_ms=1,
    )
    assert db.one(
        "SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events WHERE event_id=?",
        ("evt_" + "f" * 32,),
    )["n"] == 0


def test_sse_inspector_frames_and_multi_line_data():
    inspector = diag.JsonRpcStreamInspector()
    frame = (
        b'event: message\n'
        b'data: {"jsonrpc":"2.0",\n'
        b'data: "id":1,\n'
        b'data: "error":{"code":-32000,"message":"CANARY"}}\n\n'
    )
    for index in range(0, len(frame), 7):
        inspector.feed(frame[index:index + 7])
    inspector.finish()
    assert inspector.outcome() == (False, "-32000")

    progress = diag.JsonRpcStreamInspector()
    progress.feed(b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n')
    progress.finish()
    assert progress.outcome() == (None, "")

    result = diag.JsonRpcStreamInspector()
    result.feed(b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n')
    result.finish()
    assert result.outcome() == (True, "")


def test_intermediate_database_gains_origin_column(tmp_path):
    path = tmp_path / "intermediate.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE kaiwu_diagnostic_events("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,tenant_id TEXT NOT NULL,"
        "connector_id TEXT NOT NULL,event_id TEXT NOT NULL,occurred_at TEXT NOT NULL,"
        "level TEXT NOT NULL,component TEXT NOT NULL,event_code TEXT NOT NULL,"
        "message_code TEXT NOT NULL,operation_id TEXT,trace_id TEXT,source_id TEXT,"
        "tool_name TEXT,http_status INTEGER,rpc_error_code TEXT,duration_ms INTEGER,"
        "attempt INTEGER,received_at TEXT NOT NULL,UNIQUE(tenant_id,connector_id,event_id));"
        "INSERT INTO kaiwu_diagnostic_events"
        "(tenant_id,connector_id,event_id,occurred_at,level,component,event_code,"
        "message_code,received_at) VALUES('t','c','evt_" + "a" * 32 + "','2026-11-19T00:00:00+00:00',"
        "'info','connector','connector_started','connector_started','2026-11-19T00:00:00+00:00');"
    )
    connection.close()
    database = Database(path)
    try:
        columns = {row["name"] for row in database.all("PRAGMA table_info(kaiwu_diagnostic_events)")}
        assert "origin" in columns
        assert set(diag.INSTALL_FIELDS).issubset(columns)
        assert database.one("SELECT origin FROM kaiwu_diagnostic_events")["origin"] == "connector"
        indexes = {row["name"] for row in database.all("PRAGMA index_list(kaiwu_diagnostic_events)")}
        assert "kaiwu_diagnostic_events_origin" in indexes
    finally:
        database.close()


class _MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"
    requests: list[dict] = []

    def log_message(self, *_args):
        return

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, chunks: list[bytes]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(chunk)
            self.wfile.flush()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.requests.append({
            "trace": self.headers.get("X-Kaiwu-Trace-ID", ""),
            "body": raw,
        })
        mode = type(self).mode
        if mode == "slow":
            time.sleep(2.5)
            self._json(200, {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        elif mode == "http401":
            data = b"CANARY_SECRET unauthorized"
            self.send_response(401)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif mode == "empty":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif mode == "jsonrpc_error":
            self._json(200, {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "CANARY_SECRET"}})
        elif mode == "iserror":
            self._json(200, {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": [{"type": "text", "text": "CANARY_SECRET"}]}})
        elif mode == "sse_result":
            self._sse([b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'])
        elif mode == "sse_multiline_error":
            self._sse([
                b'event: message\ndata: {"jsonrpc":"2.0",\n',
                b'data: "id":1,\n',
                b'data: "error":{"code":-32000,"message":"CANARY_SECRET"}}\n\n',
            ])
        elif mode == "sse_progress":
            self._sse([b'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'])
        elif mode == "sse_eof_no_result":
            self._sse([])
        elif mode == "sse_abort":
            # Declare a body, send only a partial event, then drop the socket.
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "512")
            self.end_headers()
            self.wfile.write(b'event: message\ndata: {"jsonrpc":"2.0",')
            self.wfile.flush()
        else:
            self._json(200, {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})


@pytest.fixture
def edge_env(env, tmp_path):
    _MCPHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MCPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    db = env["app"].state.service.db
    db.execute(
        "UPDATE kaiwu_edge_routes SET visitor_port=? WHERE connector_id=? AND source_id=?",
        (port, env["a"]["connector_id"], SOURCE_A),
    )
    created = env["client"].post(
        BASE + "/v1/integrations",
        headers={"Authorization": "Bearer " + ADMIN_TOKEN},
        json={
            "tenant_id": env["a"]["tenant_id"],
            "connector_id": env["a"]["connector_id"],
            "display_name": "合成平台",
            "source_ids": [SOURCE_A],
            "ttl_seconds": 3600,
        },
    )
    assert created.status_code == 201, created.text
    token = created.json()["access_token"]
    try:
        yield {**env, "token": token, "port": port}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def edge_post(client, token, source_id, body, *, trace="kwtr_" + "0" * 32):
    return client.post(
        EDGE_PREFIX + f"/sources/{source_id}/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "X-Kaiwu-Trace-ID": trace,
        },
        json=body,
    )


def last_edge_event(db, connector_id):
    row = db.one(
        "SELECT * FROM kaiwu_diagnostic_events WHERE connector_id=? AND origin='edge' "
        "ORDER BY id DESC LIMIT 1",
        (connector_id,),
    )
    return dict(row) if row else {}


def test_edge_trace_header_and_outcome_classification(edge_env):
    client, db, a = edge_env["client"], edge_env["app"].state.service.db, edge_env["a"]
    token = edge_env["token"]
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "lookup_order", "arguments": {}}}

    _MCPHandler.mode = "ok"
    response = edge_post(client, token, SOURCE_A, request, trace="kwtr_" + "9" * 32)
    assert response.status_code == 200
    trace = response.headers["x-kaiwu-trace-id"]
    assert re.fullmatch(r"kwtr_[0-9a-f]{32}", trace)
    assert trace != "kwtr_" + "9" * 32
    assert _MCPHandler.requests[-1]["trace"] == trace
    row = last_edge_event(db, a["connector_id"])
    assert row["event_code"] == "platform_request_succeeded"
    assert row["trace_id"] == trace
    assert row["tool_name"] == "lookup_order"
    assert row["http_status"] == 200
    assert row["origin"] == "edge"

    _MCPHandler.mode = "jsonrpc_error"
    response = edge_post(client, token, SOURCE_A, request)
    assert response.status_code == 200
    row = last_edge_event(db, a["connector_id"])
    assert row["event_code"] == "platform_request_failed"
    assert row["rpc_error_code"] == "-32601"
    assert "CANARY_SECRET" not in json.dumps(row)

    _MCPHandler.mode = "iserror"
    edge_post(client, token, SOURCE_A, request)
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_failed"

    _MCPHandler.mode = "http401"
    response = edge_post(client, token, SOURCE_A, request)
    assert response.status_code == 401
    row = last_edge_event(db, a["connector_id"])
    assert row["event_code"] == "platform_request_failed"
    assert row["http_status"] == 401
    assert "CANARY_SECRET" not in json.dumps(row)

    _MCPHandler.mode = "slow"
    response = edge_post(client, token, SOURCE_A, request)
    assert response.status_code == 504
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_failed"

    # A notification may use an empty ACK; a real call may not.
    _MCPHandler.mode = "empty"
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert edge_post(client, token, SOURCE_A, notification).status_code == 202
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_succeeded"
    assert edge_post(client, token, SOURCE_A, request).status_code == 202
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_failed"

    admin = admin_get(client, f"?connector_id={a['connector_id']}")
    assert admin.status_code == 200
    assert "CANARY_SECRET" not in admin.text


def test_edge_sse_outcomes(edge_env):
    client, db, a = edge_env["client"], edge_env["app"].state.service.db, edge_env["a"]
    token = edge_env["token"]
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "lookup_order"}}

    _MCPHandler.mode = "sse_result"
    response = edge_post(client, token, SOURCE_A, request)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_succeeded"

    _MCPHandler.mode = "sse_multiline_error"
    response = edge_post(client, token, SOURCE_A, request)
    assert response.status_code == 200
    row = last_edge_event(db, a["connector_id"])
    assert row["event_code"] == "platform_request_failed"
    assert row["rpc_error_code"] == "-32000"
    assert "CANARY_SECRET" not in json.dumps(row)

    # Progress notifications are not a tool result.
    _MCPHandler.mode = "sse_progress"
    edge_post(client, token, SOURCE_A, request)
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_failed"

    # EOF without a result is an explicit failure, not a silent success.
    _MCPHandler.mode = "sse_eof_no_result"
    edge_post(client, token, SOURCE_A, request)
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_failed"

    # A mid-stream read interruption is also a failure, never a success.
    _MCPHandler.mode = "sse_abort"
    edge_post(client, token, SOURCE_A, request)
    assert last_edge_event(db, a["connector_id"])["event_code"] == "platform_request_failed"


def test_ops_shell_is_static_and_hardened(env):
    client = env["client"]
    response = client.get(BASE + "/ops")
    assert response.status_code == 200
    html = response.text
    assert 'type="password"' in html
    assert 'autocomplete="off"' in html
    assert ADMIN_TOKEN not in html
    assert "innerHTML" not in html
    assert "<script src=" not in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "document.cookie" not in html
    assert "default-src 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    # The shell itself contains no event data.
    assert "platform_request_succeeded" not in html


def test_ops_javascript_runs_with_real_date_conversion(env, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not available")
    script = re.search(r"<script>(.*)</script>", diag.OPS_PAGE, re.DOTALL).group(1)
    script_path = tmp_path / "ops.js"
    script_path.write_text(script, encoding="utf-8")
    harness = r'''
process.env.TZ = "Asia/Shanghai";
const fs = require("fs");
const elements = {};
function makeEl(id) {
  return {
    id: id, value: "", textContent: "", children: [],
    classList: { add() {}, remove() {} },
    addEventListener(type, fn) { this.handlers = this.handlers || {}; this.handlers[type] = fn; },
    appendChild(child) { this.children.push(child); },
    replaceChildren(...kids) { this.children = kids; },
    click() {}, remove() {}, href: ""
  };
}
["login","console","status","rows","token","auth","refresh","download","logout",
 "f-connector","f-source","f-level","f-trace","f-install","f-since","f-until"].forEach(id => { elements[id] = makeEl(id); });
global.document = {
  getElementById: id => elements[id],
  createElement: tag => makeEl(tag),
  body: { appendChild() {} }
};
global.URLSearchParams = URLSearchParams;
global.Blob = class { constructor(parts, options) { this.parts = parts; this.options = options; } };
global.URL = { createObjectURL: () => "blob:test", revokeObjectURL: () => {} };
global.fetch = async (url, options) => {
  global.__lastFetch = { url: url, options: options };
  return {
    ok: true, status: 200,
    json: async () => ({ events: [{
      occurred_at: "2026-11-19T00:00:00+00:00", level: "info",
      connector_id: "conn_1", source_id: "src_1",
      event_code: "install_stage_failed", message: "安装阶段失败",
      received_at: "2026-11-19T00:00:01+00:00",
      install_attempt_id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      install_seq: 3, install_stage: "service", install_outcome: "failed", install_code: "service_start"
    }] })
  };
};
eval(fs.readFileSync(process.argv[2], "utf8"));
(async () => {
  try {
    elements["f-since"].value = "2026-11-19T08:00";
    elements["f-install"].value = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    elements["token"].value = "admin-token";
    elements["auth"].handlers.submit({ preventDefault() {} });
    await new Promise(resolve => setTimeout(resolve, 100));
    if (!global.__lastFetch) throw new Error("fetch not called");
    if (!global.__lastFetch.options.headers.Authorization.startsWith("Bearer ")) throw new Error("missing auth header");
    if (global.__lastFetch.url.includes("admin-token")) throw new Error("token leaked to URL");
    if (elements["token"].value !== "") throw new Error("token input not cleared");
    if (elements["rows"].children.length !== 1) throw new Error("row not rendered");
    if (!global.__lastFetch.url.includes("install_attempt_id=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")) throw new Error("install filter missing");
    if (!elements["rows"].children[0].children.some(cell => cell.textContent === "service_start")) throw new Error("install stage code not rendered");
    if (!global.__lastFetch.url.includes("since=2026-11-19T00%3A00%3A00Z")) throw new Error("date conversion failed: " + global.__lastFetch.url);
    console.log("JS_OK");
    process.exit(0);
  } catch (error) {
    console.error(error && error.stack ? error.stack : error);
    process.exit(1);
  }
})();
'''
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(harness, encoding="utf-8")
    result = subprocess.run(
        [node, str(harness_path), str(script_path)],
        capture_output=True,
        text=True,
        timeout=30,
        env={"TZ": "Asia/Shanghai", "PATH": "/usr/bin:/bin:/home/dengkw/.local/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "JS_OK" in result.stdout
