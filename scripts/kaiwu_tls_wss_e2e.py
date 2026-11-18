#!/usr/bin/env python3
"""Verify the Kaiwu bridge through a local HTTPS/WSS reverse proxy.

This is deliberately a loopback test. It exercises the official frps/frpc
WebSocket transport, TLS certificate validation, the MCP Edge and the complete
MCP lifecycle, but it does not claim public DNS, a production certificate or a
customer network was used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from kaiwu_enterprise_e2e import (  # noqa: E402
    CONNECTOR_DIR,
    EVIDENCE,
    RUNTIME_ROOT,
    SERVER_DIR,
    SYNTHETIC_ORDER,
    Driver,
    SyntheticMCPHandler,
    pick_port,
    run_checked,
    source_snapshot,
    terminate,
    wait_port,
)


LOCAL_HOST = "localhost"


class TLSDriver(Driver):
    """Run the existing synthetic driver with TLS/WSS on the relay edge."""

    def __init__(self, ssl_binary: str, nginx_binary: str) -> None:
        super().__init__()
        self.ssl_binary = ssl_binary
        self.nginx_binary = nginx_binary
        self.tls_dir = self.runtime / "tls"
        self.nginx_dir = self.tls_dir / "nginx"
        self.tls_logs = self.tls_dir / "logs"
        self.tls_dir.mkdir(mode=0o700)
        self.nginx_dir.mkdir(mode=0o700)
        self.tls_logs.mkdir(mode=0o700)
        self.frps_port = pick_port()
        self.relay_port = self.frps_port
        self.tls_port = pick_port()
        self.ca_file = self.tls_dir / "ca.crt"
        self.server_cert = self.tls_dir / "server.crt"
        self.server_key = self.tls_dir / "server.key"
        self.wrong_ca_file = self.tls_dir / "wrong-ca.crt"
        self.nginx_conf = self.nginx_dir / "nginx.conf"
        self.nginx_access = self.tls_logs / "nginx-access.log"
        self.nginx_error = self.tls_logs / "nginx-error.log"
        self.nginx_process = None
        self.connector_process = None
        self.cloud_env_overrides.update({
            "AGENT_KAIWU_CONTROL_RELAY_ADDRESS": LOCAL_HOST,
            "AGENT_KAIWU_CONTROL_RELAY_PORT": str(self.tls_port),
            "AGENT_KAIWU_CONTROL_RELAY_PROTOCOL": "wss",
            "AGENT_KAIWU_CONTROL_RELAY_TLS_ENABLED": "true",
            "AGENT_KAIWU_CONTROL_RELAY_TLS_SERVER_NAME": LOCAL_HOST,
            "AGENT_KAIWU_CONTROL_RELAY_TLS_TRUSTED_CA_FILE": str(self.ca_file),
            "AGENT_KAIWU_CONTROL_RELAY_TLS_CLIENT_TRUSTED_CA_FILE": str(self.ca_file),
            "AGENT_KAIWU_CONTROL_RELAY_BIND_ADDRESS": "127.0.0.1",
            "AGENT_KAIWU_CONTROL_RELAY_BIND_PORT": str(self.frps_port),
            "AGENT_KAIWU_EDGE_PUBLIC_BASE": f"https://{LOCAL_HOST}:{self.tls_port}/projects/kaiwu/mcp",
        })

    @property
    def edge_base(self) -> str:
        return f"https://{LOCAL_HOST}:{self.tls_port}/projects/kaiwu/mcp"

    def _openssl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.ssl_binary, *args],
            cwd=self.tls_dir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )

    def prepare_certificates(self) -> dict[str, Any]:
        commands = [
            (
                "ca",
                [
                    "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(self.tls_dir / "ca.key"),
                    "-out", str(self.ca_file), "-days", "1",
                    "-subj", "/CN=Kaiwu Loopback Test CA",
                ],
            ),
            (
                "server_key",
                [
                    "req", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(self.server_key),
                    "-out", str(self.tls_dir / "server.csr"),
                    "-subj", f"/CN={LOCAL_HOST}",
                ],
            ),
            (
                "wrong_ca",
                [
                    "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(self.tls_dir / "wrong-ca.key"),
                    "-out", str(self.wrong_ca_file), "-days", "1",
                    "-subj", "/CN=Wrong Loopback Test CA",
                ],
            ),
        ]
        for label, args in commands:
            completed = self._openssl(*args)
            if completed.returncode != 0:
                raise RuntimeError(f"openssl {label} failed: {completed.stdout[-2000:]}")
        ext = self.tls_dir / "server.ext"
        ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8")
        completed = self._openssl(
            "x509", "-req", "-in", str(self.tls_dir / "server.csr"),
            "-CA", str(self.ca_file), "-CAkey", str(self.tls_dir / "ca.key"),
            "-CAcreateserial", "-out", str(self.server_cert), "-days", "1",
            "-extfile", str(ext),
        )
        if completed.returncode != 0:
            raise RuntimeError(f"openssl server certificate failed: {completed.stdout[-2000:]}")
        fingerprint = self._openssl(
            "x509", "-in", str(self.server_cert), "-noout", "-fingerprint", "-sha256"
        )
        return {
            "subject_alt_name": ["DNS:localhost", "IP:127.0.0.1"],
            "server_certificate_sha256": fingerprint.stdout.strip(),
            "trusted_ca_file": str(self.ca_file),
            "wrong_ca_file": str(self.wrong_ca_file),
            "certificate_generation": "openssl temporary CA and server certificate",
        }

    def write_nginx_config(self) -> None:
        config = f"""daemon off;
master_process off;
pid {self.nginx_dir / 'nginx.pid'};
error_log {self.nginx_error} info;
events {{}}
http {{
  access_log {self.nginx_access} combined buffer=64k flush=1s;
  client_body_temp_path {self.nginx_dir / 'client'};
  proxy_temp_path {self.nginx_dir / 'proxy'};
  fastcgi_temp_path {self.nginx_dir / 'fastcgi'};
  uwsgi_temp_path {self.nginx_dir / 'uwsgi'};
  scgi_temp_path {self.nginx_dir / 'scgi'};
  map $http_upgrade $connection_upgrade {{
    default upgrade;
    '' close;
  }}
  server {{
    listen 127.0.0.1:{self.tls_port} ssl;
    server_name {LOCAL_HOST};
    ssl_certificate {self.server_cert};
    ssl_certificate_key {self.server_key};
    ssl_protocols TLSv1.2 TLSv1.3;

    location /~!frp {{
      proxy_pass http://127.0.0.1:{self.frps_port};
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection $connection_upgrade;
      proxy_set_header Host $host;
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      proxy_buffering off;
    }}

    location /projects/kaiwu/mcp/ {{
      proxy_pass http://127.0.0.1:{self.server_port};
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_read_timeout 300s;
      proxy_send_timeout 300s;
      proxy_buffering off;
    }}
  }}
}}
"""
        self.nginx_conf.write_text(config, encoding="utf-8")

    def start_nginx(self) -> None:
        self.write_nginx_config()
        self.nginx_process = self.spawn(
            "nginx",
            [self.nginx_binary, "-c", str(self.nginx_conf)],
            cwd=self.tls_dir,
            env=self.env(),
        )
        wait_port("127.0.0.1", self.tls_port, timeout=10)

    def start_connector(self, binary: Path, linux_frpc: Path) -> None:
        super().start_connector(binary, linux_frpc)
        self.connector_process = self.processes[-1]

    def wait_edge_ready(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        last_error = "not attempted"
        while time.monotonic() < deadline:
            try:
                response, _ = self.direct_initialize()
                if response.status_code == 200:
                    return
                last_error = f"HTTP {response.status_code}"
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.3)
        raise RuntimeError(f"TLS Edge did not become ready: {last_error}")

    def direct_initialize(self) -> tuple[httpx.Response, str]:
        endpoint = (
            f"https://{LOCAL_HOST}:{self.tls_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp"
        )
        response = httpx.post(
            endpoint,
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=20,
            trust_env=False,
            verify=str(self.ca_file),
        )
        return response, response.headers.get("mcp-session-id", "")

    def platform_import_and_discover_tls(self) -> dict[str, Any]:
        from app.config import Settings
        from app.mcp_import import parse_mcp_config

        config = self.integration["mcp_config"]
        plan = parse_mcp_config(config, ("127.0.0.1", LOCAL_HOST))
        connection = plan.connections[0]
        if connection.credential != self.integration["access_token"]:
            raise RuntimeError("platform importer did not preserve Edge integration credential")
        if not connection.url.startswith(f"https://{LOCAL_HOST}:{self.tls_port}/"):
            raise RuntimeError(f"unexpected imported URL: {connection.url}")
        return {
            "format": plan.format,
            "url": connection.url,
            "credential_preserved": True,
            "allowed_hosts": ["127.0.0.1", LOCAL_HOST],
            "service_settings_class": Settings.__name__,
        }

    def mcp_lifecycle_tls(self) -> dict[str, Any]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def run() -> dict[str, Any]:
            headers = {"Authorization": "Bearer " + self.integration["access_token"]}
            async with httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(20, connect=10),
                follow_redirects=False,
                trust_env=False,
                verify=str(self.ca_file),
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
                            str(getattr(item, "text", "")) for item in result.content
                        )
                        return {
                            "protocol_version": initialized.protocolVersion,
                            "tools": [tool.name for tool in tools.tools],
                            "result_text": text,
                        }

        return asyncio.run(run())

    def wrong_ca_check(self) -> dict[str, Any]:
        endpoint = (
            f"https://{LOCAL_HOST}:{self.tls_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp"
        )
        try:
            response = httpx.post(
                endpoint,
                headers={"Authorization": "Bearer " + self.integration["access_token"]},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                timeout=10,
                trust_env=False,
                verify=str(self.wrong_ca_file),
            )
            return {"rejected": response.status_code >= 400, "status": response.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"rejected": True, "exception": type(exc).__name__}

    def https_negative_checks(self) -> dict[str, Any]:
        endpoint = (
            f"https://{LOCAL_HOST}:{self.tls_port}/projects/kaiwu/mcp/"
            f"sources/{self.source_id}/mcp"
        )
        wrong_key = httpx.post(
            endpoint,
            headers={"Authorization": "Bearer synthetic-wrong-key"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=10,
            trust_env=False,
            verify=str(self.ca_file),
        )
        unknown_source = httpx.post(
            f"https://{LOCAL_HOST}:{self.tls_port}/projects/kaiwu/mcp/"
            "sources/src_00000000000000000000000000000000/mcp",
            headers={"Authorization": "Bearer " + self.integration["access_token"]},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            timeout=10,
            trust_env=False,
            verify=str(self.ca_file),
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
            verify=str(self.ca_file),
        )
        return {
            "wrong_key_status": wrong_key.status_code,
            "unknown_source_status": unknown_source.status_code,
            "initialize_status": initialized.status_code,
            "unapproved_tool_status": denied_tool.status_code,
        }

    def read_nginx_access(self) -> str:
        return self.nginx_access.read_text(encoding="utf-8") if self.nginx_access.exists() else ""

    def wait_for_websocket_log(self, timeout: float = 8) -> str:
        deadline = time.monotonic() + timeout
        access = ""
        while time.monotonic() < deadline:
            access = self.read_nginx_access()
            if any("/~!frp" in line and " 101 " in line for line in access.splitlines()):
                return access
            time.sleep(0.2)
        return access

    def close_tls_connections_for_log(self) -> dict[str, Any]:
        # The access log record for an upgraded WebSocket is written when the
        # connection ends, so close the visitor and connector before reading it.
        bridge_code = terminate(self.bridge, timeout=10)
        self.bridge = None
        connector_code = terminate(
            self.connector_process.process if self.connector_process else None,
            timeout=10,
        )
        access = self.wait_for_websocket_log()
        ws_lines = [line for line in access.splitlines() if "/~!frp" in line]
        return {
            "bridge_exit_code": bridge_code,
            "connector_exit_code": connector_code,
            "websocket_access_lines": ws_lines,
            "websocket_101_observed": any(" 101 " in line for line in ws_lines),
            "nginx_access_log": str(self.nginx_access),
        }

    def close(self) -> None:
        super().close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frps", default="")
    parser.add_argument("--linux-frpc", default=str(CONNECTOR_DIR / "packaging/bin/frpc-linux-amd64"))
    parser.add_argument("--nginx", default="")
    parser.add_argument("--go", default="/media/data3/dengkw/toolchains/go1.27.4/bin/go")
    args = parser.parse_args()

    frps = Path(args.frps) if args.frps else Path(
        "/media/data3/dengkw/scratch/tmp/kaiwu-release-audit/verified-linux/"
        "frp_0.61.1_linux_amd64/frps"
    )
    frps = frps.resolve()
    frpc = Path(args.linux_frpc).resolve()
    nginx = Path(args.nginx).resolve() if args.nginx else Path(
        "/media/data3/dengkw/toolchains/nginx-huawi/root-1.18.0-6ubuntu14.22/usr/sbin/nginx"
    )
    go = Path(args.go)
    ssl_binary = shutil.which("openssl") or ""
    if not all(path.is_file() for path in (frps, frpc, nginx, go)) or not ssl_binary:
        raise SystemExit("missing pinned FRP, nginx, Go or openssl executable")

    driver = TLSDriver(ssl_binary, str(nginx))
    result: dict[str, Any] = {
        "status": "running",
        "scope": "loopback HTTPS/WSS only; not public DNS, production certificate, Tencent Cloud or customer network",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "runtime_dir": str(driver.runtime),
        **source_snapshot(),
        "versions": {},
        "binaries": {},
        "checks": driver.checks,
    }
    try:
        result["versions"]["go"] = subprocess.check_output([str(go), "version"], text=True).strip()
        result["versions"]["frps"] = subprocess.check_output([str(frps), "--version"], text=True).strip()
        result["versions"]["frpc"] = subprocess.check_output([str(frpc), "--version"], text=True).strip()
        result["versions"]["nginx"] = subprocess.check_output([str(nginx), "-v"], stderr=subprocess.STDOUT, text=True).strip()
        result["binaries"] = {
            "frps_sha256": hashlib.sha256(frps.read_bytes()).hexdigest(),
            "frpc_sha256": hashlib.sha256(frpc.read_bytes()).hexdigest(),
            "nginx_sha256": hashlib.sha256(nginx.read_bytes()).hexdigest(),
        }
        result["tls"] = driver.prepare_certificates()

        connector_binary = driver.runtime / "kaiwu-connector"
        run_checked(
            [str(go), "build", "-trimpath", "-o", str(connector_binary), "./cmd/connector"],
            cwd=CONNECTOR_DIR,
            env={**os.environ, "CGO_ENABLED": "0"},
        )
        result["binaries"]["connector_sha256"] = hashlib.sha256(connector_binary.read_bytes()).hexdigest()

        driver.start_mock()
        driver.start_cloud()
        driver.start_nginx()
        driver.start_connector(connector_binary, frpc)
        driver.configure_connection()
        driver.record("tls_wss_edge_started", "passed", "nginx TLS listener and WSS reverse proxy are open")
        driver.start_bridge(frps, frpc)
        driver.create_integration()
        driver.wait_edge_ready()
        driver.record("connector_tunnel_status", "passed", str(driver.wait_connector_tunnel().get("tunnel_detail")))

        imported = driver.platform_import_and_discover_tls()
        driver.record("platform_https_import", "passed", json.dumps(imported, ensure_ascii=False))
        lifecycle = driver.mcp_lifecycle_tls()
        if lifecycle["result_text"] != SYNTHETIC_ORDER:
            raise RuntimeError(f"unexpected synthetic MCP result over TLS: {lifecycle}")
        if "lookup_order" not in lifecycle["tools"] or "write_order" in lifecycle["tools"]:
            raise RuntimeError(f"unexpected tool scope over TLS: {lifecycle['tools']}")
        driver.record("mcp_lifecycle_https", "passed", json.dumps(lifecycle, ensure_ascii=False))

        wrong_ca = driver.wrong_ca_check()
        if not wrong_ca["rejected"]:
            raise RuntimeError(f"wrong CA was accepted: {wrong_ca}")
        driver.record("wrong_ca_rejected", "passed", json.dumps(wrong_ca, ensure_ascii=False))

        negatives = driver.https_negative_checks()
        if negatives != {
            "wrong_key_status": 401,
            "unknown_source_status": 403,
            "initialize_status": 200,
            "unapproved_tool_status": 403,
        }:
            raise RuntimeError(f"HTTPS negative checks failed: {negatives}")
        driver.record("https_authorization", "passed", json.dumps(negatives, ensure_ascii=False))

        driver.record(
            "synthetic_mcp_called",
            "passed",
            f"calls={SyntheticMCPHandler.calls}; signatures_valid={SyntheticMCPHandler.signatures_valid}; fixture={SYNTHETIC_ORDER}",
        )
        log_result = driver.close_tls_connections_for_log()
        driver.record("websocket_upgrade", "passed" if log_result["websocket_101_observed"] else "failed", json.dumps(log_result, ensure_ascii=False))
        if not log_result["websocket_101_observed"]:
            raise RuntimeError(f"nginx did not record WebSocket 101: {log_result}")
        result["tls_log"] = log_result
        result["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        driver.record("failure", "failed", str(exc))
    finally:
        driver.close()
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["duration_seconds"] = round(time.monotonic() - driver.started, 3)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        path = EVIDENCE / "e2e-tls-wss-latest.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({
            "status": result["status"],
            "evidence": str(path),
            "runtime_dir": str(driver.runtime),
            "error": result.get("error"),
        }, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
