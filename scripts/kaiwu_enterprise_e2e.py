#!/usr/bin/env python3
"""Run one synthetic end-to-end Kaiwu enterprise bridge acceptance.

The script starts the real control-plane app, real connector binary, official
frps/frpc 0.61.1 binaries, a cloud visitor, the public Edge, and a deterministic
local MCP server. It writes a non-secret JSON summary under the evidence path.
All temporary data stays below the repository .runtime directory.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
CONNECTOR_DIR = ROOT / "connector"
RUNTIME_ROOT = ROOT / ".runtime"
EVIDENCE = ROOT / "docs/evidence/huawi-mcp-20261118-enterprise-bridge"

ADMIN_TOKEN = "e2e-admin-" + secrets.token_urlsafe(32)
CUSTOMER_BEARER = "synthetic-customer-bearer"
HMAC_KEY = "demo-key-only"
HMAC_CHANNEL = "demo"
HMAC_USER = "u01"
SYNTHETIC_ORDER = "SYNTHETIC-ORDER-42"


def pick_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1, trust_env=False)
            if response.status_code < 500:
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = type(exc).__name__
        time.sleep(0.2)
    raise RuntimeError(f"service did not become ready: {url}: {last_error}")


def wait_port(host: str, port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError(f"port did not become ready: {host}:{port}")


def wait_port_closed(host: str, port: int, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not port_open(host, port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"port did not close: {host}:{port}")


def port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def terminate(process: subprocess.Popen | None, timeout: float = 10) -> int | None:
    if process is None or process.poll() is not None:
        return process.returncode if process else None
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return process.returncode


def run_checked(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(command)}\n{completed.stdout[-4000:]}")


class SyntheticMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    calls = 0
    forbidden_calls = 0
    signatures_valid = 0
    lock = threading.Lock()
    session_id = "customer-session-synthetic"

    def log_message(self, *_args):
        return

    def _json(self, status: int, payload: dict, *, session: bool = False) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if session:
            self.send_header("Mcp-Session-Id", self.session_id)
        self.end_headers()
        self.wfile.write(raw)

    def _validate_signature(self, request: dict) -> bool:
        if request.get("method") != "tools/call":
            return True
        request_id = self.headers.get("requestId", "")
        timestamp = self.headers.get("timestamp", "")
        if self.headers.get("appid") != "MCP":
            return False
        if self.headers.get("channel") != HMAC_CHANNEL or self.headers.get("userid") != HMAC_USER:
            return False
        canonical = (
            f"appid=MCP&channel={HMAC_CHANNEL}&userid={HMAC_USER}"
            f"&requestId={request_id}&timestamp={timestamp}"
        )
        expected = hmac.new(HMAC_KEY.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, self.headers.get("sign", ""))

    def do_POST(self):
        if self.path != "/mcp":
            self._json(404, {"error": "not found"})
            return
        if self.headers.get("Authorization", "") != f"Bearer {CUSTOMER_BEARER}":
            self._json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        method = request.get("method")
        request_id = request.get("id")
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            self._json(200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "synthetic-order-mcp", "version": "1.0"},
                },
            }, session=True)
            return
        if method == "tools/list":
            self._json(200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "lookup_order",
                            "description": "Return fixed synthetic order data",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "sku": {"type": "string"},
                                    "stream": {"type": "boolean"},
                                },
                                "required": ["sku"],
                            },
                        },
                        {
                            "name": "write_order",
                            "description": "Must remain unapproved",
                            "inputSchema": {"type": "object"},
                        },
                    ]
                },
            })
            return
        if method == "tools/call":
            params = request.get("params") or {}
            if params.get("name") != "lookup_order":
                with self.lock:
                    self.forbidden_calls += 1
                self._json(200, {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"isError": True, "content": [{"type": "text", "text": "unexpected tool"}]},
                })
                return
            if not self._validate_signature(request):
                self._json(401, {"error": "bad signature"})
                return
            with self.lock:
                self.calls += 1
                self.signatures_valid += 1
            result = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "isError": False,
                    "content": [{"type": "text", "text": SYNTHETIC_ORDER}],
                },
            }
            if (params.get("arguments") or {}).get("stream"):
                raw = (
                    "event: message\n"
                    f"data: {json.dumps(result, separators=(',', ':'))}\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                self.wfile.write(raw)
                self.wfile.flush()
                time.sleep(1.5)
                self.wfile.write(b": synthetic-done\n\n")
                self.wfile.flush()
                return
            self._json(200, result)
            return
        self._json(200, {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "unknown method"},
        })


@dataclass
class Running:
    process: subprocess.Popen
    log: Any


class Driver:
    def __init__(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.runtime = RUNTIME_ROOT / f"huawi-enterprise-e2e-{stamp}"
        self.runtime.mkdir(parents=True, exist_ok=False)
        self.cloud_data = self.runtime / "cloud-data"
        self.connector_data = self.runtime / "connector-data"
        self.bridge_data = self.runtime / "bridge-data"
        self.platform_data = self.runtime / "platform-data"
        self.logs = self.runtime / "logs"
        self.logs.mkdir(mode=0o700)
        self.server_port = pick_port()
        self.connector_port = pick_port()
        self.mcp_port = pick_port()
        self.relay_port = pick_port()
        self.visitor_port = pick_port()
        self.admin_token = ADMIN_TOKEN
        self.processes: list[Running] = []
        self.mock_server: ThreadingHTTPServer | None = None
        self.bridge: subprocess.Popen | None = None
        self.checks: list[dict[str, Any]] = []
        self.started = time.monotonic()
        self.connector_client = httpx.Client(
            base_url=f"http://127.0.0.1:{self.connector_port}",
            timeout=30,
            trust_env=False,
        )
        self.control = httpx.Client(
            base_url=f"http://127.0.0.1:{self.server_port}/api/kaiwu",
            timeout=30,
            trust_env=False,
            headers={"Authorization": f"Bearer {self.admin_token}"},
        )
        self.connector_token = ""
        self.identity: dict[str, Any] = {}
        self.source_id = ""
        self.integration: dict[str, Any] = {}

    @property
    def edge_base(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp"

    @property
    def cloud_base(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/api/kaiwu"

    def record(self, check: str, status: str, detail: str, **extra: Any) -> None:
        self.checks.append({"check": check, "status": status, "detail": detail, **extra})

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(SERVER_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AGENT_DATA_DIR": str(self.cloud_data),
            "AGENT_PROVIDER_SETTINGS": "",
            "AGENT_ALLOWED_ORIGINS": f"http://127.0.0.1:{self.server_port}",
            "AGENT_KAIWU_CONTROL_ENABLED": "true",
            "AGENT_KAIWU_CONTROL_ADMIN_TOKEN": self.admin_token,
            "AGENT_KAIWU_CONTROL_RELAY_ADDRESS": "127.0.0.1",
            "AGENT_KAIWU_CONTROL_RELAY_PORT": str(self.relay_port),
            "AGENT_KAIWU_CONTROL_RELAY_PROTOCOL": "tcp",
            "AGENT_KAIWU_CONTROL_RELAY_TLS_ENABLED": "false",
            "AGENT_KAIWU_CONTROL_RELAY_BIND_ADDRESS": "127.0.0.1",
            "AGENT_KAIWU_CONTROL_EDGE_USER": "edge-synthetic",
            "AGENT_KAIWU_EDGE_PUBLIC_BASE": self.edge_base,
            "AGENT_KAIWU_EDGE_MOUNT_PATH": "/projects/kaiwu/mcp",
            "AGENT_KAIWU_EDGE_VISITOR_HOST": "127.0.0.1",
            "AGENT_KAIWU_EDGE_VISITOR_PORT_START": str(self.visitor_port),
            "AGENT_KAIWU_EDGE_VISITOR_PORT_END": str(self.visitor_port),
            "AGENT_KAIWU_EDGE_REQUEST_TIMEOUT_SECONDS": "20",
        })
        return env

    def spawn(self, name: str, command: list[str], *, cwd: Path, env: dict[str, str]) -> Running:
        log_path = self.logs / f"{name}.log"
        log = open(log_path, "ab", buffering=0)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        running = Running(process, log)
        self.processes.append(running)
        return running

    def start_mock(self) -> None:
        SyntheticMCPHandler.calls = 0
        SyntheticMCPHandler.forbidden_calls = 0
        SyntheticMCPHandler.signatures_valid = 0
        self.mock_server = ThreadingHTTPServer(("127.0.0.1", self.mcp_port), SyntheticMCPHandler)
        threading.Thread(target=self.mock_server.serve_forever, daemon=True).start()

    def start_cloud(self) -> None:
        env = self.env()
        self.spawn(
            "cloud",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.server_port),
                "--log-level",
                "warning",
            ],
            cwd=SERVER_DIR,
            env=env,
        )
        wait_http(f"http://127.0.0.1:{self.server_port}/api/v1/health/live")

    def start_connector(self, binary: Path, linux_frpc: Path) -> None:
        env = self.env()
        self.spawn(
            "connector",
            [
                str(binary),
                "serve",
                "--data-dir",
                str(self.connector_data),
                "--listen",
                f"127.0.0.1:{self.connector_port}",
                "--cloud",
                self.cloud_base,
                "--edge",
                self.edge_base,
                "--frpc",
                str(linux_frpc),
            ],
            cwd=ROOT,
            env=env,
        )
        wait_http(f"http://127.0.0.1:{self.connector_port}/")
        page = self.connector_client.get("/").text
        matched = re.search(r'const TOKEN = "([0-9a-f]{48})";', page)
        if not matched:
            raise RuntimeError("could not read local connector session token")
        self.connector_token = matched.group(1)

    def connector_post(self, path: str, payload: dict) -> dict:
        response = self.connector_client.post(
            path,
            json=payload,
            headers={"X-Connector-Token": self.connector_token},
        )
        body = response.json()
        if response.status_code != 200 or not body.get("ok"):
            raise RuntimeError(f"connector API {path} failed: {response.status_code} {body}")
        return body["data"]

    def configure_connection(self) -> None:
        enrollment = self.control.post(
            "/v1/enrollments",
            json={"tenant_id": "tenant-huawi-synthetic", "tenant_name": "华微合成企业"},
        )
        if enrollment.status_code != 200:
            raise RuntimeError(f"enrollment failed: {enrollment.status_code} {enrollment.text}")
        code = enrollment.json()["code"]
        self.identity = self.connector_post("/api/bind", {"code": code})
        imported = self.connector_post("/api/import", {
            "config": json.dumps({
                "mcpServers": {
                    "synthetic-order": {
                        "type": "http",
                        "url": f"http://127.0.0.1:{self.mcp_port}/mcp",
                        "headers": {"Authorization": f"Bearer {CUSTOMER_BEARER}"},
                    }
                }
            }, ensure_ascii=False)
        })
        self.source_id = imported["sources"][0]["source_id"]
        self.connector_post("/api/auth-profile", {
            "source_id": self.source_id,
            "app_id": "MCP",
            "channel": HMAC_CHANNEL,
            "user_id": HMAC_USER,
            "hmac_key": HMAC_KEY,
            "tools": "lookup_order",
        })
        self.connector_post("/api/connect", {})

    def start_bridge(self, frps: Path, linux_frpc: Path) -> None:
        env = self.env()
        log_path = self.logs / "bridge.log"
        log = open(log_path, "ab", buffering=0)
        self.bridge = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.kaiwu_bridge",
                "--data-dir",
                str(self.cloud_data),
                "--connector-id",
                self.identity["connector_id"],
                "--frps",
                str(frps),
                "--frpc",
                str(linux_frpc),
                "--runtime-dir",
                str(self.bridge_data),
                "--poll-seconds",
                "0.5",
            ],
            cwd=SERVER_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        wait_port("127.0.0.1", self.relay_port)
        wait_port("127.0.0.1", self.visitor_port)

    def create_integration(self) -> None:
        response = self.control.post("/v1/integrations", json={
            "tenant_id": self.identity["tenant_id"],
            "connector_id": self.identity["connector_id"],
            "display_name": "华微业务查询",
            "source_ids": [self.source_id],
            "ttl_seconds": 3600,
        })
        if response.status_code != 201:
            raise RuntimeError(f"integration creation failed: {response.status_code} {response.text}")
        self.integration = response.json()

    def wait_edge_ready(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        last_status = 0
        while time.monotonic() < deadline:
            response, _ = self.direct_initialize()
            last_status = response.status_code
            if response.status_code == 200:
                return
            time.sleep(0.3)
        raise RuntimeError(f"Edge did not become ready through the real tunnel: HTTP {last_status}")

    def wait_connector_tunnel(self, timeout: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = self.connector_client.get("/api/status")
            payload = response.json()
            status = payload.get("data") or {}
            if status.get("tunnel_connected") is True:
                return status
            time.sleep(0.3)
        raise RuntimeError(
            f"connector did not confirm running frpc proxies: {status.get('tunnel_detail')}"
        )

    def wait_connector_tunnel_down(self, timeout: float = 12) -> dict:
        deadline = time.monotonic() + timeout
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            payload = self.connector_client.get("/api/status").json()
            status = payload.get("data") or {}
            if status.get("tunnel_connected") is not True:
                return status
            time.sleep(0.3)
        raise RuntimeError("connector kept reporting tunnel_connected after relay stop")

    def hard_kill_frps_and_recover(self) -> dict:
        if self.bridge is None or self.bridge.poll() is not None:
            raise RuntimeError("bridge process is not running before hard-kill test")
        children_file = Path(f"/proc/{self.bridge.pid}/task/{self.bridge.pid}/children")
        try:
            child_pids = [int(value) for value in children_file.read_text().split()]
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not enumerate bridge children: {exc}") from exc
        frps_pid = None
        for pid in child_pids:
            try:
                executable = Path(os.readlink(f"/proc/{pid}/exe"))
            except OSError:
                continue
            if executable.name == "frps":
                frps_pid = pid
                break
        if frps_pid is None:
            raise RuntimeError(f"dedicated frps child not found: {child_pids}")

        started = time.monotonic()
        os.kill(frps_pid, signal.SIGKILL)
        wait_port_closed("127.0.0.1", self.relay_port, timeout=8)
        down_seconds = round(time.monotonic() - started, 3)
        wait_port("127.0.0.1", self.relay_port, timeout=15)
        wait_port("127.0.0.1", self.visitor_port, timeout=15)
        self.wait_edge_ready(30)
        recovered, _ = self.direct_initialize()
        return {
            "killed_process": "frps",
            "down_seconds": down_seconds,
            "recovery_seconds": round(time.monotonic() - started, 3),
            "edge_status": recovered.status_code,
        }

    def platform_import_and_discover(self) -> dict:
        sys.path.insert(0, str(SERVER_DIR))
        from app.config import Settings
        from app.mcp_import import parse_mcp_config
        from app.service import Service

        config = self.integration["mcp_config"]
        plan = parse_mcp_config(config, ("127.0.0.1",))
        connection = plan.connections[0]
        if connection.credential != self.integration["access_token"]:
            raise RuntimeError("platform importer did not preserve Edge integration credential")
        service = Service(Settings(
            data_dir=self.platform_data,
            provider_settings="",
            mcp_hosts=("127.0.0.1",),
        ))
        try:
            user = service.create_user({
                "username": "synthetic-admin",
                "display_name": "Synthetic Admin",
                "password": "synthetic-password",
                "role": "admin",
            })
            saved = service.save_connection(
                {**connection.create_fields(), "scope": "private"},
                user=user,
            )
            result = asyncio.run(service.test_connection(saved["id"]))
            if result.get("status") != "connected":
                raise RuntimeError(f"platform discovery failed: {result}")
            names = {item["name"] for item in result.get("tools", [])}
            if "lookup_order" not in names or "write_order" in names:
                raise RuntimeError(f"platform tool scope mismatch: {names}")
            return {
                "format": plan.format,
                "tool_names": sorted(names),
                "url": connection.url,
            }
        finally:
            service.db.close()

    def mcp_lifecycle(self) -> dict:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def run() -> dict:
            headers = {
                "Authorization": "Bearer " + self.integration["access_token"],
            }
            async with httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                trust_env=False,
            ) as http:
                async with streamable_http_client(
                    self.integration["mcp_config"]["mcpServers"]["华微业务查询"]["url"],
                    http_client=http,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool("lookup_order", {"sku": "SKU-1001"})
                        text = "\n".join(
                            str(getattr(item, "text", ""))
                            for item in result.content
                        )
                        return {
                            "protocol_version": initialized.protocolVersion,
                            "tools": [tool.name for tool in tools.tools],
                            "result_text": text,
                        }

        return asyncio.run(run())

    def direct_initialize(self) -> tuple[httpx.Response, str]:
        endpoint = (
            f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp"
        )
        response = httpx.post(
            endpoint,
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=20,
            trust_env=False,
        )
        return response, response.headers.get("mcp-session-id", "")

    def negative_checks(self) -> dict:
        endpoint = (
            f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp"
        )
        wrong_key = httpx.post(
            endpoint,
            headers={"Authorization": "Bearer synthetic-wrong-key"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=10,
            trust_env=False,
        )
        unknown_source = httpx.post(
            f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp/"
            "sources/src_00000000000000000000000000000000/mcp",
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=10,
            trust_env=False,
        )
        initialized, session_id = self.direct_initialize()
        denied_tool = httpx.post(
            endpoint,
            headers={
                "Authorization": "Bearer " + self.integration["access_token"],
                "Mcp-Session-Id": session_id,
            },
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "write_order", "arguments": {}},
            },
            timeout=20,
            trust_env=False,
        )
        return {
            "wrong_key_status": wrong_key.status_code,
            "unknown_source_status": unknown_source.status_code,
            "initialize_status": initialized.status_code,
            "unapproved_tool_status": denied_tool.status_code,
        }

    def frp_rejection_checks(self, linux_frpc: Path) -> dict:
        sys.path.insert(0, str(SERVER_DIR))
        from app.config import Settings
        from app.kaiwu_bridge import derive_secret, render_visitor_config

        bridge_settings = Settings(
            data_dir=self.cloud_data,
            provider_settings="",
            kaiwu_control_admin_token=self.admin_token,
            kaiwu_control_relay_address="127.0.0.1",
            kaiwu_control_relay_port=self.relay_port,
            kaiwu_control_relay_protocol="tcp",
            kaiwu_control_relay_tls_enabled=False,
            kaiwu_control_edge_user="edge-synthetic",
            kaiwu_edge_visitor_host="127.0.0.1",
        )
        relay_token = derive_secret(bridge_settings, "relay", self.identity["connector_id"])
        stcp_token = derive_secret(
            bridge_settings,
            "stcp",
            f"{self.identity['connector_id']}:{self.source_id}:1",
        )

        wrong_relay_path = self.runtime / "wrong-relay.toml"
        wrong_relay_path.write_text(
            render_visitor_config(
                bridge_settings,
                self.identity["connector_id"],
                "wrong-relay-token",
                [{
                    "source_id": self.source_id,
                    "stcp_secret": stcp_token,
                    "visitor_host": "127.0.0.1",
                    "visitor_port": pick_port(),
                }],
                log_path=str(self.runtime / "wrong-relay.log"),
            ),
            encoding="utf-8",
        )
        wrong_relay = subprocess.Popen(
            [str(linux_frpc), "-c", str(wrong_relay_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wrong_relay_code = wrong_relay.wait(timeout=8)
        except subprocess.TimeoutExpired:
            wrong_relay_code = None
        finally:
            terminate(wrong_relay)
            wrong_relay_path.unlink(missing_ok=True)

        probes = {}
        for label, secret, server_name in (
            ("wrong_stcp", "wrong-stcp-token", self.source_id),
            ("wrong_name", stcp_token, "src_ffffffffffffffffffffffffffffffff"),
        ):
            port = pick_port()
            path = self.runtime / f"{label}.toml"
            path.write_text(
                render_visitor_config(
                    bridge_settings,
                    self.identity["connector_id"],
                    relay_token,
                    [{
                        "source_id": server_name,
                        "stcp_secret": secret,
                        "visitor_host": "127.0.0.1",
                        "visitor_port": port,
                    }],
                    log_path=str(self.runtime / f"{label}.log"),
                ),
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [str(linux_frpc), "-c", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                wait_port("127.0.0.1", port, timeout=8)
                try:
                    response = httpx.post(
                        f"http://127.0.0.1:{port}/mcp",
                        content=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                        headers={"Content-Type": "application/json"},
                        timeout=6,
                        trust_env=False,
                    )
                    probes[label] = response.status_code
                except httpx.RequestError:
                    probes[label] = "connection_error"
            finally:
                terminate(process)
                path.unlink(missing_ok=True)
        return {
            "wrong_relay_exit_code": wrong_relay_code,
            "wrong_stcp_probe": probes["wrong_stcp"],
            "wrong_name_probe": probes["wrong_name"],
        }

    def sse_first_event(self) -> dict:
        endpoint = (
            f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp"
        )
        initialized, session_id = self.direct_initialize()
        if initialized.status_code != 200:
            raise RuntimeError(f"SSE initialize failed: {initialized.status_code}")
        started = time.monotonic()
        with httpx.Client(timeout=10, trust_env=False) as client:
            with client.stream(
                "POST",
                endpoint,
                headers={
                    "Authorization": "Bearer " + self.integration["access_token"],
                    "Mcp-Session-Id": session_id,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "lookup_order",
                        "arguments": {"sku": "SKU-1001", "stream": True},
                    },
                },
            ) as response:
                first = next(response.iter_raw())
                elapsed = time.monotonic() - started
        return {
            "status": response.status_code,
            "first_event_seconds": round(elapsed, 3),
            "first_event_contains_data": b"data:" in first,
            "content_type": response.headers.get("content-type", ""),
        }

    def revoke_and_verify(self) -> dict:
        started = time.monotonic()
        response = self.control.post(
            f"/v1/connectors/{self.identity['connector_id']}/revoke"
        )
        if response.status_code != 200:
            raise RuntimeError(f"connector revoke failed: {response.status_code} {response.text}")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and self.bridge and self.bridge.poll() is None:
            time.sleep(0.2)
        stop_seconds = round(time.monotonic() - started, 3)
        code = self.bridge.returncode if self.bridge else None
        after = httpx.post(
            f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp",
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}},
            timeout=10,
            trust_env=False,
        )
        with sqlite3.connect(self.cloud_data / "app.sqlite3") as database:
            route_released = database.execute(
                "SELECT COUNT(*) FROM kaiwu_edge_routes WHERE connector_id=? AND source_id=?",
                (self.identity["connector_id"], self.source_id),
            ).fetchone()[0] == 0
        return {
            "bridge_exit_code": code,
            "stop_seconds": stop_seconds,
            "edge_status_after_revoke": after.status_code,
            "frps_port_open": port_open("127.0.0.1", self.relay_port),
            "visitor_port_open": port_open("127.0.0.1", self.visitor_port),
            "visitor_route_released": route_released,
        }

    def stop_bridge_and_verify(self) -> dict:
        code = terminate(self.bridge, timeout=10)
        self.bridge = None
        started = time.monotonic()
        after = httpx.post(
            f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp",
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 8, "method": "initialize", "params": {}},
            timeout=10,
            trust_env=False,
        )
        return {
            "bridge_stop_exit_code": code,
            "edge_status": after.status_code,
            "failure_seconds": round(time.monotonic() - started, 3),
        }

    def scan_public_artifacts(self) -> dict:
        secret_values = [
            self.integration.get("access_token", ""),
            CUSTOMER_BEARER,
            HMAC_KEY,
        ]
        hits = []
        frpc_config = self.connector_data / "frpc.toml"
        for path in [*self.logs.glob("*.log"), self.cloud_data / "app.sqlite3", frpc_config]:
            if not path.exists():
                continue
            raw = path.read_bytes()
            for value in secret_values:
                if value and value.encode() in raw:
                    hits.append({"file": path.name, "secret_class": "integration/customer/local-hmac"})
        frpc_text = frpc_config.read_text(encoding="utf-8") if frpc_config.exists() else ""
        template_ok = (
            "{{ .Envs.KAIWU_RELAY_TOKEN }}" in frpc_text
            and "{{ .Envs.KAIWU_STCP_" in frpc_text
        )
        return {
            "scanned": ["logs/*.log", "cloud app.sqlite3", "connector frpc.toml"],
            "hits": hits,
            "frpc_template_secret_free": template_ok,
        }

    def write_evidence(self, result: dict) -> Path:
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE / "e2e-synthetic-latest.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def close(self) -> None:
        self.connector_client.close()
        self.control.close()
        terminate(self.bridge)
        pid_path = self.connector_data / "frpc.pid"
        if pid_path.is_file():
            try:
                pid = int(pid_path.read_text(encoding="utf-8").strip())
                os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError):
                pass
        for item in reversed(self.processes):
            terminate(item.process)
            item.log.close()
        if self.mock_server:
            self.mock_server.shutdown()
            self.mock_server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frps", default="")
    parser.add_argument("--linux-frpc", default=str(ROOT / "connector/packaging/bin/frpc-linux-amd64"))
    args = parser.parse_args()

    go = Path("/media/data3/dengkw/toolchains/go1.27.4/bin/go")
    frps = Path(args.frps) if args.frps else Path(
        "/media/data3/dengkw/scratch/tmp/kaiwu-release-audit/verified-linux/"
        "frp_0.61.1_linux_amd64/frps"
    )
    frps = frps.resolve()
    frpc = Path(args.linux_frpc).resolve()
    if not go.is_file() or not frps.is_file() or not frpc.is_file():
        raise SystemExit("missing pinned Go/frps/frpc executable")

    driver = Driver()
    result: dict[str, Any] = {
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime_dir": str(driver.runtime),
        "versions": {},
        "checks": driver.checks,
    }
    try:
        run_checked([str(go), "version"], cwd=CONNECTOR_DIR)
        result["versions"]["go"] = subprocess.check_output(
            [str(go), "version"], text=True
        ).strip()
        result["versions"]["frps"] = subprocess.check_output(
            [str(frps), "--version"], text=True
        ).strip()
        result["versions"]["frpc"] = subprocess.check_output(
            [str(frpc), "--version"], text=True
        ).strip()
        result["binaries"] = {
            "frps_sha256": hashlib.sha256(frps.read_bytes()).hexdigest(),
            "frpc_sha256": hashlib.sha256(frpc.read_bytes()).hexdigest(),
        }
        connector_binary = driver.runtime / "kaiwu-connector"
        run_checked(
            [
                str(go),
                "build",
                "-trimpath",
                "-o",
                str(connector_binary),
                "./cmd/connector",
            ],
            cwd=CONNECTOR_DIR,
            env={**os.environ, "CGO_ENABLED": "0"},
        )
        result["binaries"]["connector_sha256"] = hashlib.sha256(
            connector_binary.read_bytes()
        ).hexdigest()

        driver.start_mock()
        driver.start_cloud()
        driver.start_connector(connector_binary, frpc)
        driver.configure_connection()
        driver.record("connector_configured", "passed", "bind, import, HMAC profile and frpc start")
        driver.start_bridge(frps, frpc)
        driver.record("real_frp_started", "passed", "dedicated frps and visitor listeners are open")
        driver.create_integration()
        driver.record("enterprise_integration", "passed", "K and one-time standard MCP JSON issued")
        driver.wait_edge_ready()
        connector_tunnel = driver.wait_connector_tunnel()
        driver.record(
            "connector_tunnel_status",
            "passed",
            str(connector_tunnel.get("tunnel_detail")),
        )

        imported = driver.platform_import_and_discover()
        driver.record("platform_import_discovery", "passed", json.dumps(imported, ensure_ascii=False))
        lifecycle = driver.mcp_lifecycle()
        if lifecycle["result_text"] != SYNTHETIC_ORDER:
            raise RuntimeError(f"unexpected synthetic MCP result: {lifecycle}")
        driver.record("mcp_lifecycle", "passed", json.dumps(lifecycle, ensure_ascii=False))

        negatives = driver.negative_checks()
        if negatives != {
            "wrong_key_status": 401,
            "unknown_source_status": 403,
            "initialize_status": 200,
            "unapproved_tool_status": 403,
        }:
            raise RuntimeError(f"negative checks failed: {negatives}")
        driver.record("negative_authorization", "passed", json.dumps(negatives))

        frp_negatives = driver.frp_rejection_checks(frpc)
        if (
            frp_negatives["wrong_relay_exit_code"] in {None, 0}
            or frp_negatives["wrong_stcp_probe"] not in {502, 503, "connection_error"}
            or frp_negatives["wrong_name_probe"] not in {502, 503, "connection_error"}
        ):
            raise RuntimeError(f"FRP negative checks failed: {frp_negatives}")
        driver.record("frp_negative_credentials", "passed", json.dumps(frp_negatives))

        sse = driver.sse_first_event()
        if sse["status"] != 200 or not sse["first_event_contains_data"] or sse["first_event_seconds"] >= 1.2:
            raise RuntimeError(f"SSE incremental forwarding failed: {sse}")
        driver.record("sse_incremental", "passed", json.dumps(sse))

        hard_killed = driver.hard_kill_frps_and_recover()
        if hard_killed["edge_status"] != 200 or hard_killed["recovery_seconds"] > 10:
            raise RuntimeError(f"hard-killed relay child did not recover in time: {hard_killed}")
        driver.record("relay_child_hard_kill_recovery", "passed", json.dumps(hard_killed))

        stopped = driver.stop_bridge_and_verify()
        if stopped["edge_status"] not in {502, 503, 504}:
            raise RuntimeError(f"stopping relay did not fail closed: {stopped}")
        driver.record("relay_stop_fail_closed", "passed", json.dumps(stopped))
        stopped_status = driver.wait_connector_tunnel_down()
        driver.record(
            "connector_status_degrades",
            "passed",
            str(stopped_status.get("tunnel_detail")),
        )
        driver.start_bridge(frps, frpc)
        driver.wait_edge_ready(30)
        restarted, _ = driver.direct_initialize()
        driver.record("relay_restart_recovery", "passed", f"Edge initialize status {restarted.status_code}")

        scan = driver.scan_public_artifacts()
        if scan["hits"] or not scan["frpc_template_secret_free"]:
            raise RuntimeError(f"secret scan found public artifact leakage: {scan}")
        driver.record("secret_scan", "passed", json.dumps(scan))

        revoked = driver.revoke_and_verify()
        if (
            revoked["bridge_exit_code"] != 0
            or revoked["stop_seconds"] > 5
            or revoked["edge_status_after_revoke"] != 401
            or revoked["frps_port_open"]
            or revoked["visitor_port_open"]
            or not revoked["visitor_route_released"]
        ):
            raise RuntimeError(f"revocation did not stop dedicated data plane: {revoked}")
        driver.record("revocation_stops_data_plane", "passed", json.dumps(revoked))
        result["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        driver.record("failure", "failed", str(exc))
    finally:
        driver.close()
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["duration_seconds"] = round(time.monotonic() - driver.started, 3)
        path = driver.write_evidence(result)
        print(json.dumps({
            "status": result["status"],
            "evidence": str(path),
            "runtime_dir": str(driver.runtime),
            "error": result.get("error"),
        }, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
