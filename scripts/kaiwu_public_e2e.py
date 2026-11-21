#!/usr/bin/env python3
"""Run the synthetic Kaiwu bridge acceptance against the deployed public edge.

The connector, synthetic MCP and acceptance driver run on the school cloud.
The control plane, dedicated frps and cloud visitor run on the public host.
Only redacted evidence is written. Secrets stay in memory or owner-only runtime
files created by scripts/kaiwu_enterprise_e2e.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from kaiwu_enterprise_e2e import (
    CUSTOMER_BEARER,
    EVIDENCE,
    HMAC_CHANNEL,
    HMAC_KEY,
    HMAC_USER,
    ROOT,
    SYNTHETIC_ORDER,
    Driver,
    Running,
    SyntheticMCPHandler,
    terminate,
    wait_http,
)


DEFAULT_KEY = Path("/media/data3/dengkw/t3-code/home/.ssh/kaiwu_hk_other_20261116")
DEFAULT_HOST = "43.161.249.13"
DEVICE = "d5edb697-e33e-4e6d-9150-5b9c278b6524"
NGINX_ACCESS_LOG = "/var/log/nginx/flintw.com.access.log"


class PublicDriver:
    def __init__(
        self,
        *,
        admin_token: str,
        ssh_host: str,
        ssh_user: str,
        ssh_key: Path,
        cloud_base: str,
        edge_base: str,
        connector_binary: Path,
        frpc_binary: Path,
    ) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.runtime = ROOT / ".runtime" / f"huawi-public-e2e-{stamp}"
        self.runtime.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.logs = self.runtime / "logs"
        self.logs.mkdir(mode=0o700)
        self.connector_data = self.runtime / "connector-data"
        self.connector_data.mkdir(mode=0o700)
        self.connector_binary = connector_binary
        self.frpc_binary = frpc_binary
        self.cloud_base = cloud_base.rstrip("/")
        self.edge_base = edge_base.rstrip("/")
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.ssh_key = ssh_key
        self.admin_token = admin_token
        self.checks: list[dict[str, Any]] = []
        self.started = time.monotonic()
        self.mock_server = None
        self.connector: subprocess.Popen | None = None
        self.connector_log = None
        self.connector_token = ""
        self.identity: dict[str, Any] = {}
        self.source_id = ""
        self.integration: dict[str, Any] = {}
        self.revoked = False
        self.connector_client = None
        self.control = httpx.Client(
            base_url=self.cloud_base,
            timeout=45,
            trust_env=False,
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    def record(self, check: str, status: str, detail: str, **extra: Any) -> None:
        self.checks.append({"check": check, "status": status, "detail": detail, **extra})

    def ssh(self, command: str, *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "-i",
                str(self.ssh_key),
                f"{self.ssh_user}@{self.ssh_host}",
                command,
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )

    def sudo_ssh(self, command: str, *, timeout: float = 60) -> subprocess.CompletedProcess[str]:
        return self.ssh("sudo -n " + command, timeout=timeout)

    def connector_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            env.pop(name, None)
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        return env

    def start_mock(self, port: int) -> None:
        from http.server import ThreadingHTTPServer
        import threading

        SyntheticMCPHandler.calls = 0
        SyntheticMCPHandler.forbidden_calls = 0
        SyntheticMCPHandler.signatures_valid = 0
        self.mock_server = ThreadingHTTPServer(("127.0.0.1", port), SyntheticMCPHandler)
        threading.Thread(target=self.mock_server.serve_forever, daemon=True).start()

    def start_connector(self, port: int) -> None:
        self.connector_log = open(self.logs / "connector.log", "ab", buffering=0)
        self.connector = subprocess.Popen(
            [
                str(self.connector_binary),
                "serve",
                "--data-dir",
                str(self.connector_data),
                "--listen",
                f"127.0.0.1:{port}",
                "--cloud",
                self.cloud_base,
                "--edge",
                self.edge_base,
                "--frpc",
                str(self.frpc_binary),
            ],
            cwd=ROOT,
            env=self.connector_env(),
            stdin=subprocess.DEVNULL,
            stdout=self.connector_log,
            stderr=subprocess.STDOUT,
        )
        self.connector_client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=30,
            trust_env=False,
        )
        wait_http(f"http://127.0.0.1:{port}/", timeout=30)
        page = self.connector_client.get("/").text
        matched = re.search(r'const TOKEN = "([0-9a-f]{48})";', page)
        if not matched:
            raise RuntimeError("could not read local connector session token")
        self.connector_token = matched.group(1)

    def connector_post(self, path: str, payload: dict) -> dict:
        assert self.connector_client is not None
        response = self.connector_client.post(
            path,
            json=payload,
            headers={"X-Connector-Token": self.connector_token},
        )
        body = response.json()
        if response.status_code != 200 or not body.get("ok"):
            raise RuntimeError(f"connector API {path} failed: {response.status_code} {body}")
        return body["data"]

    def configure_connection(self, mcp_port: int) -> None:
        enrollment = self.control.post(
            "/v1/enrollments",
            json={"tenant_id": "tenant-huawi-public-synthetic", "tenant_name": "华微公网合成企业"},
        )
        if enrollment.status_code != 200:
            raise RuntimeError(f"enrollment failed: {enrollment.status_code} {enrollment.text}")
        self.identity = self.connector_post("/api/bind", {"code": enrollment.json()["code"]})
        imported = self.connector_post(
            "/api/import",
            {
                "config": json.dumps(
                    {
                        "mcpServers": {
                            "synthetic-order": {
                                "type": "http",
                                "url": f"http://127.0.0.1:{mcp_port}/mcp",
                                "headers": {"Authorization": f"Bearer {CUSTOMER_BEARER}"},
                            }
                        }
                    },
                    ensure_ascii=False,
                )
            },
        )
        self.source_id = imported["sources"][0]["source_id"]
        self.connector_post(
            "/api/auth-profile",
            {
                "source_id": self.source_id,
                "app_id": "MCP",
                "channel": HMAC_CHANNEL,
                "user_id": HMAC_USER,
                "hmac_key": HMAC_KEY,
                "tools": "lookup_order",
            },
        )
        self.connector_post("/api/connect", {})

    def start_remote_bridge(self) -> None:
        connector_id = self.identity["connector_id"]
        result = self.sudo_ssh(
            f"systemctl start kaiwu-bridge@{connector_id}.service",
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"remote bridge failed to start: {result.stderr[-1000:]}")

    def stop_remote_bridge(self) -> None:
        if not self.identity.get("connector_id"):
            return
        connector_id = self.identity["connector_id"]
        self.sudo_ssh(f"systemctl stop kaiwu-bridge@{connector_id}.service", timeout=60)

    def bridge_state(self) -> dict[str, Any]:
        connector_id = self.identity["connector_id"]
        result = self.sudo_ssh(
            f"systemctl show kaiwu-bridge@{connector_id}.service "
            "--property=ActiveState,SubState,Result,ExecMainStatus,NRestarts",
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError("could not read remote bridge state")
        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                fields[key] = value
        return fields

    def wait_connector_tunnel(self, timeout: float = 75) -> dict[str, Any]:
        assert self.connector_client is not None
        deadline = time.monotonic() + timeout
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            payload = self.connector_client.get("/api/status").json()
            status = payload.get("data") or {}
            if status.get("tunnel_connected") is True:
                return status
            if self.connector is not None and self.connector.poll() is not None:
                raise RuntimeError("connector process exited while waiting for tunnel")
            time.sleep(0.5)
        raise RuntimeError(f"connector did not confirm the public tunnel: {status.get('tunnel_detail')}")

    def wait_connector_tunnel_down(self, timeout: float = 30) -> dict[str, Any]:
        assert self.connector_client is not None
        deadline = time.monotonic() + timeout
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = self.connector_client.get("/api/status").json().get("data") or {}
            if status.get("tunnel_connected") is not True:
                return status
            time.sleep(0.4)
        raise RuntimeError("connector kept reporting the tunnel as connected")

    def create_integration(self) -> None:
        response = self.control.post(
            "/v1/integrations",
            json={
                "tenant_id": self.identity["tenant_id"],
                "connector_id": self.identity["connector_id"],
                "display_name": "华微业务查询",
                "source_ids": [self.source_id],
                "ttl_seconds": 3600,
            },
        )
        if response.status_code != 201:
            raise RuntimeError(f"integration creation failed: {response.status_code} {response.text}")
        self.integration = response.json()

    def endpoint(self) -> str:
        return f"{self.edge_base}/sources/{self.source_id}/mcp"

    def direct_initialize(self) -> tuple[httpx.Response, str]:
        response = httpx.post(
            self.endpoint(),
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=30,
            trust_env=False,
        )
        return response, response.headers.get("mcp-session-id", "")

    def wait_edge_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        last_status = 0
        while time.monotonic() < deadline:
            response, _ = self.direct_initialize()
            last_status = response.status_code
            if response.status_code == 200:
                return
            time.sleep(0.5)
        raise RuntimeError(f"public Edge did not become ready: HTTP {last_status}")

    def mcp_lifecycle(self) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def run() -> dict[str, Any]:
            headers = {"Authorization": "Bearer " + self.integration["access_token"]}
            async with httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(45, connect=15),
                follow_redirects=False,
                trust_env=False,
            ) as http:
                config = self.integration["mcp_config"]["mcpServers"]["华微业务查询"]
                async with streamable_http_client(config["url"], http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool("lookup_order", {"sku": "SKU-1001"})
                        text = "\n".join(str(getattr(item, "text", "")) for item in result.content)
                        return {
                            "protocol_version": initialized.protocolVersion,
                            "tools": [tool.name for tool in tools.tools],
                            "result_text": text,
                        }

        import asyncio

        return asyncio.run(run())

    def negative_checks(self) -> dict[str, Any]:
        wrong_key = httpx.post(
            self.endpoint(),
            headers={"Authorization": "Bearer synthetic-wrong-key"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=20,
            trust_env=False,
        )
        unknown_source = httpx.post(
            f"{self.edge_base}/sources/src_00000000000000000000000000000000/mcp",
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=20,
            trust_env=False,
        )
        initialized, session_id = self.direct_initialize()
        denied = httpx.post(
            self.endpoint(),
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
            timeout=30,
            trust_env=False,
        )
        return {
            "wrong_key_status": wrong_key.status_code,
            "unknown_source_status": unknown_source.status_code,
            "initialize_status": initialized.status_code,
            "unapproved_tool_status": denied.status_code,
        }

    def sse_first_event(self) -> dict[str, Any]:
        initialized, session_id = self.direct_initialize()
        if initialized.status_code != 200:
            raise RuntimeError(f"SSE initialize failed: {initialized.status_code}")
        started = time.monotonic()
        with httpx.Client(timeout=30, trust_env=False) as client:
            with client.stream(
                "POST",
                self.endpoint(),
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

    def stop_bridge_and_verify(self) -> dict[str, Any]:
        started = time.monotonic()
        self.stop_remote_bridge()
        state = self.bridge_state()
        response, _ = self.direct_initialize()
        return {
            "edge_status": response.status_code,
            "failure_seconds": round(time.monotonic() - started, 3),
            "active_state": state.get("ActiveState", ""),
            "sub_state": state.get("SubState", ""),
        }

    def revoked_state(self) -> dict[str, Any]:
        state = self.bridge_state()
        response, _ = self.direct_initialize()
        return {
            "edge_status_after_revoke": response.status_code,
            "active_state": state.get("ActiveState", ""),
            "sub_state": state.get("SubState", ""),
            "result": state.get("Result", ""),
            "exec_main_status": state.get("ExecMainStatus", ""),
        }

    def revoke(self) -> None:
        response = self.control.post(f"/v1/connectors/{self.identity['connector_id']}/revoke")
        if response.status_code != 200:
            raise RuntimeError(f"connector revoke failed: {response.status_code} {response.text}")
        self.revoked = True

    def nginx_websocket_counts(self) -> dict[str, int]:
        result = self.sudo_ssh(
            "python3 -c " + shlex.quote(
                f"import re; lines=open({NGINX_ACCESS_LOG!r},'r',errors='ignore').read().splitlines(); "
                "p=re.compile(r'GET /~!frp HTTP/[^\\\"]+\\\" ([0-9]{3})'); "
                "hits=[m.group(1) for m in map(p.search,lines) if m]; "
                "print(len(hits), sum(1 for x in hits if x=='101'))"
            ),
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"could not inspect nginx access log: {result.stderr[-500:]}")
        total, upgraded = [int(value) for value in result.stdout.split()]
        return {"total": total, "upgraded": upgraded}

    def tls_probe(self) -> dict[str, Any]:
        host = httpx.URL(self.edge_base).host
        context = ssl.create_default_context()
        with __import__("socket").create_connection((host, 443), timeout=15) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                version = tls.version()
        return {
            "host": host,
            "version": version,
            "subject": cert.get("subject", []),
            "issuer": cert.get("issuer", []),
            "not_after": cert.get("notAfter", ""),
            "subject_alt_name": cert.get("subjectAltName", []),
        }

    def revoke_integration(self) -> None:
        integration_id = self.integration.get("integration", {}).get("id", "")
        if integration_id:
            self.control.post(f"/v1/integrations/{integration_id}/revoke")

    def close(self) -> None:
        if self.connector_client:
            self.connector_client.close()
        self.control.close()
        if not self.revoked and self.identity.get("connector_id"):
            try:
                self.revoke()
            except Exception:
                pass
        if self.identity.get("connector_id"):
            try:
                self.stop_remote_bridge()
            except Exception:
                pass
        terminate(self.connector)
        if self.connector_log:
            self.connector_log.close()
        if self.mock_server:
            self.mock_server.shutdown()
            self.mock_server.server_close()


def pick_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def source_snapshot() -> dict[str, Any]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", "connector", "server", "scripts", "deploy"],
        cwd=ROOT,
        text=True,
    ).strip()
    return {"source_commit": commit, "source_dirty": bool(dirty)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-token", default=os.getenv("KAIWU_ADMIN_TOKEN", ""))
    parser.add_argument("--ssh-host", default=DEFAULT_HOST)
    parser.add_argument("--ssh-user", default="ubuntu")
    parser.add_argument("--ssh-key", type=Path, default=DEFAULT_KEY)
    parser.add_argument("--cloud-base", default="https://flintw.com/api/kaiwu")
    parser.add_argument("--edge-base", default="https://flintw.com/projects/kaiwu/mcp")
    parser.add_argument(
        "--connector",
        type=Path,
        default=ROOT / "connector/dist/linux/bin/kaiwu-connector",
    )
    parser.add_argument(
        "--frpc",
        type=Path,
        default=ROOT / "connector/packaging/bin/frpc-linux-amd64",
    )
    args = parser.parse_args()
    if not args.admin_token:
        raise SystemExit("KAIWU_ADMIN_TOKEN or --admin-token is required")
    for path in (args.ssh_key, args.connector, args.frpc):
        if not path.is_file():
            raise SystemExit(f"missing required file: {path}")

    driver = PublicDriver(
        admin_token=args.admin_token,
        ssh_host=args.ssh_host,
        ssh_user=args.ssh_user,
        ssh_key=args.ssh_key,
        cloud_base=args.cloud_base,
        edge_base=args.edge_base,
        connector_binary=args.connector.resolve(),
        frpc_binary=args.frpc.resolve(),
    )
    result: dict[str, Any] = {
        "status": "running",
        "scope": "public HTTPS/WSS control plane, relay, visitor and Edge; customer Windows not included",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime_dir": str(driver.runtime),
        **source_snapshot(),
        "public_api": args.cloud_base,
        "public_edge": args.edge_base,
        "checks": driver.checks,
        "binaries": {
            "connector_sha256": hashlib.sha256(args.connector.read_bytes()).hexdigest(),
            "frpc_sha256": hashlib.sha256(args.frpc.read_bytes()).hexdigest(),
        },
    }
    ws_before: dict[str, int] = {}
    try:
        health = httpx.get(args.cloud_base + "/v1/health", timeout=20, trust_env=False)
        if health.status_code != 200:
            raise RuntimeError(f"public control health failed: {health.status_code}")
        driver.record("public_control_health", "passed", "HTTPS health returned 200")
        result["tls"] = driver.tls_probe()
        driver.record("public_tls", "passed", f"{result['tls']['host']} {result['tls']['version']}")
        ws_before = driver.nginx_websocket_counts()

        mcp_port = pick_port()
        connector_port = pick_port()
        driver.start_mock(mcp_port)
        driver.start_connector(connector_port)
        driver.configure_connection(mcp_port)
        driver.record("connector_configured", "passed", "public cloud bind, source import and HMAC profile")
        driver.start_remote_bridge()
        driver.record("dedicated_frps_started", "passed", f"connector {driver.identity['connector_id']}")
        driver.create_integration()
        driver.record("enterprise_integration", "passed", "K and standard MCP JSON issued; K not recorded")
        driver.wait_edge_ready()
        tunnel = driver.wait_connector_tunnel()
        driver.record("public_tunnel_connected", "passed", str(tunnel.get("tunnel_detail")))

        lifecycle = driver.mcp_lifecycle()
        if lifecycle.get("result_text") != SYNTHETIC_ORDER:
            raise RuntimeError(f"unexpected synthetic MCP result: {lifecycle}")
        driver.record("public_mcp_lifecycle", "passed", json.dumps(lifecycle, ensure_ascii=False))

        negatives = driver.negative_checks()
        expected = {
            "wrong_key_status": 401,
            "unknown_source_status": 403,
            "initialize_status": 200,
            "unapproved_tool_status": 403,
        }
        if negatives != expected:
            raise RuntimeError(f"authorization negative checks failed: {negatives}")
        driver.record("public_authorization_negative", "passed", json.dumps(negatives))

        sse = driver.sse_first_event()
        if sse["status"] != 200 or not sse["first_event_contains_data"] or sse["first_event_seconds"] >= 1.2:
            raise RuntimeError(f"public SSE forwarding failed: {sse}")
        driver.record("public_sse_incremental", "passed", json.dumps(sse))

        stopped = driver.stop_bridge_and_verify()
        if stopped["edge_status"] not in {502, 503, 504}:
            raise RuntimeError(f"stopping the bridge did not fail closed: {stopped}")
        down_status = driver.wait_connector_tunnel_down()
        driver.record(
            "public_relay_stop_fail_closed",
            "passed",
            json.dumps({**stopped, "connector_tunnel_detail": down_status.get("tunnel_detail")}, ensure_ascii=False),
        )
        driver.start_remote_bridge()
        driver.wait_edge_ready(60)
        driver.wait_connector_tunnel(60)
        driver.record("public_relay_restart_recovery", "passed", "Edge initialize and connector tunnel recovered")

        ws_after = driver.nginx_websocket_counts()
        if ws_after["upgraded"] <= ws_before["upgraded"]:
            raise RuntimeError(f"no new Nginx WebSocket 101 observed: before={ws_before} after={ws_after}")
        driver.record(
            "public_websocket_upgrade",
            "passed",
            json.dumps({"before": ws_before, "after": ws_after}),
        )

        if SyntheticMCPHandler.calls < 2 or SyntheticMCPHandler.signatures_valid != SyntheticMCPHandler.calls:
            raise RuntimeError(
                f"unexpected synthetic call counters: calls={SyntheticMCPHandler.calls} "
                f"valid={SyntheticMCPHandler.signatures_valid}"
            )
        driver.record(
            "synthetic_hmac_calls",
            "passed",
            f"calls={SyntheticMCPHandler.calls}; valid={SyntheticMCPHandler.signatures_valid}",
        )

        driver.revoke()
        time.sleep(3)
        revoked = driver.revoked_state()
        if revoked["edge_status_after_revoke"] != 401 or revoked["active_state"] != "inactive":
            raise RuntimeError(f"revocation did not stop the public data plane: {revoked}")
        driver.record("public_revocation", "passed", json.dumps(revoked))
        result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        driver.record("failure", "failed", str(exc))
    finally:
        try:
            driver.revoke_integration()
        except Exception:
            pass
        driver.close()
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["duration_seconds"] = round(time.monotonic() - driver.started, 3)
        evidence = EVIDENCE / "e2e-public-latest.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "status": result["status"],
            "evidence": str(evidence),
            "runtime_dir": str(driver.runtime),
            "error": result.get("error"),
        }, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
