#!/usr/bin/env python3
"""Exercise the compiled connector, native WSS relay and platform import on loopback."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from kaiwu_enterprise_e2e import (
    CONNECTOR_DIR,
    ROOT,
    SERVER_DIR,
    SYNTHETIC_ORDER,
    Driver,
    SyntheticMCPHandler,
    run_checked,
    terminate,
    wait_http,
)
from kaiwu_tls_wss_e2e import TLSDriver


class NativeDriver(TLSDriver):
    # The platform calls the Edge on loopback. The device API always uses TLS.
    edge_base = Driver.edge_base
    direct_initialize = Driver.direct_initialize

    def __init__(self, openssl: str, nginx: str) -> None:
        super().__init__(openssl, nginx)
        self.cloud_env_overrides = {
            "AGENT_KAIWU_NATIVE_RELAY_ENABLED": "true",
            "AGENT_KAIWU_CONTROL_RELAY_ADDRESS": "",
            "AGENT_COOKIE_SECURE": "false",
            "AGENT_COOKIE_PATH": "/",
            "AGENT_MCP_ALLOWED_HOSTS": "127.0.0.1",
        }

    @property
    def cloud_base(self) -> str:
        return f"https://localhost:{self.tls_port}/api/kaiwu"

    def env(self) -> dict[str, str]:
        env = super().env()
        if os.environ.get("PYTHONPATH"):
            env["PYTHONPATH"] += os.pathsep + os.environ["PYTHONPATH"]
        return env

    def start_native_connector(self, binary: Path) -> None:
        self.connector_process = self.spawn(
            "connector",
            [str(binary), "serve", "--native-relay", "--data-dir", str(self.connector_data),
             "--listen", f"127.0.0.1:{self.connector_port}", "--cloud", self.cloud_base,
             "--edge", self.edge_base, "--platform", f"https://localhost:{self.tls_port}/"],
            cwd=ROOT,
            env={**self.env(), "SSL_CERT_FILE": str(self.ca_file)},
        )
        wait_http(f"http://127.0.0.1:{self.connector_port}/")
        page = self.connector_client.get("/").text
        self.connector_token = page.split('const TOKEN = "', 1)[1].split('"', 1)[0]

    def platform_check(self) -> None:
        password_path = self.runtime / "synthetic-platform-password"
        password = "synthetic-policy-password-123"
        password_path.write_text(password, encoding="utf-8")
        try:
            run_checked(
                [sys.executable, "-m", "app.cli", "init-admin", "--username", "synthetic-admin",
                 "--password-file", str(password_path)], cwd=SERVER_DIR, env=self.env(),
            )
        finally:
            password_path.unlink(missing_ok=True)

        with httpx.Client(base_url=f"http://127.0.0.1:{self.server_port}", timeout=20, trust_env=False) as platform:
            root = "/api/v1"
            handoff = root + "/connections/kaiwu-handoff"
            code = self.fresh_handoff_code()
            pre_login = platform.post(handoff + "/preview", json={"handoff_code": code})
            assert pre_login.status_code == 401, pre_login.status_code
            platform.post(root + "/auth/login", json={"username": "synthetic-admin", "password": password}).raise_for_status()

            preview = platform.post(handoff + "/preview", json={"handoff_code": code}).raise_for_status().json()
            assert preview["allowed_tools"] == ["lookup_order"]
            payload = {"handoff_code": code, "action": "copy", "scope": "team", "name": "华微业务查询"}
            copied = platform.post(handoff + "/redeem", json=payload).raise_for_status().json()
            config = json.loads(copied["mcp_config_json"])
            entry = config["mcpServers"]["华微业务查询"]
            assert entry["type"] == "http" and entry["url"] == f"{self.edge_base}/sources/{self.source_id}/mcp"
            self.integration = {"mcp_config": config, "access_token": entry["headers"]["Authorization"].split(" ", 1)[1]}
            assert platform.post(handoff + "/redeem", json=payload).status_code == 409
            imported = platform.post(root + "/connections/import", json={"config": config, "scope": "team"})
            imported.raise_for_status()
            assert imported.json()["binding_required"] is True
            self.record("copy_and_legacy_import", "passed", "authorized copy, single use, existing import format")

            payload = {"handoff_code": self.fresh_handoff_code(), "action": "import", "scope": "team", "name": "华微快捷导入"}
            result = platform.post(handoff + "/redeem", json=payload).raise_for_status().json()
            connection = result["connection"]
            repeated = platform.post(handoff + "/redeem", json=payload).raise_for_status().json()
            assert connection["id"] == repeated["connection"]["id"]
            tested = platform.post(root + f"/connections/{connection['id']}/test").raise_for_status().json()
            assert tested["status"] == "connected", tested["status"]
            assert {tool["name"] for tool in tested["tools"]} == {"lookup_order"}
            self.record("direct_import_and_discovery", "passed", "one connection created, approved tool discovered")

    def fresh_handoff_code(self) -> str:
        result = self.connector_post("/api/platform-handoff", {"source_id": self.source_id})
        link = urlsplit(result["url"])
        assert link.scheme == "https" and not link.query and link.fragment.startswith("kaiwu-mcp=")
        return link.fragment.removeprefix("kaiwu-mcp=")

    def assert_no_visitor(self) -> None:
        with sqlite3.connect(self.cloud_data / "app.sqlite3") as db:
            visitor_count = db.execute(
                "SELECT COUNT(*) FROM kaiwu_edge_routes WHERE connector_id=?", (self.identity["connector_id"],)
            ).fetchone()[0]
        assert visitor_count == 0

    def check_concurrent_calls(self) -> None:
        initialized, session_id = self.direct_initialize()
        initialized.raise_for_status()
        url = self.integration["mcp_config"]["mcpServers"]["华微业务查询"]["url"]
        headers = {"Authorization": "Bearer " + self.integration["access_token"], "Mcp-Session-Id": session_id}
        with httpx.Client(headers=headers, timeout=20, trust_env=False) as client:
            def call(request_id: int) -> int:
                response = client.post(url, json={
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": "lookup_order", "arguments": {"sku": "SKU-1001"}},
                }).raise_for_status().json()
                assert response["result"]["content"][0]["text"] == SYNTHETIC_ORDER
                return response["id"]

            expected_ids = list(range(100, 112))
            with ThreadPoolExecutor(max_workers=len(expected_ids)) as executor:
                assert list(executor.map(call, expected_ids)) == expected_ids
        self.record("concurrent_requests", "passed", "12 simultaneous calls returned their matching JSON-RPC responses")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", default="go")
    parser.add_argument("--nginx", default="nginx")
    parser.add_argument("--openssl", default="openssl")
    args = parser.parse_args()
    driver = NativeDriver(args.openssl, args.nginx)
    try:
        binary = driver.runtime / "kaiwu-connector"
        run_checked([args.go, "build", "-trimpath", "-o", str(binary), "./cmd/connector"],
                    cwd=CONNECTOR_DIR, env={**os.environ, "CGO_ENABLED": "0"})
        driver.prepare_certificates()
        driver.start_mock()
        driver.start_cloud()
        driver.start_nginx()
        driver.start_native_connector(binary)
        issued = driver.configure_connection({"persistent": True, "issued_to": "合成试装负责人"})
        assert issued["expires_at"] is None
        enrollment_id = issued["enrollment_id"]
        connected = driver.wait_connector_tunnel()
        assert connected["transport_mode"] == "native"
        driver.assert_no_visitor()
        driver.record("native_wss", "passed", "trusted loopback TLS and signed device upgrade, no visitor allocated")
        devices = driver.control.get(f"/v1/enrollments/{enrollment_id}/devices").raise_for_status().json()
        assert issued["code"] not in json.dumps(devices)
        assert devices["devices"][0]["connector_id"] == driver.identity["connector_id"]
        driver.record("persistent_code_audit", "passed", "no expiry, real connector visible in admin device history")
        driver.platform_check()

        lifecycle = driver.mcp_lifecycle()
        assert lifecycle["tools"] == ["lookup_order"] and lifecycle["result_text"] == SYNTHETIC_ORDER
        assert SyntheticMCPHandler.signatures_valid == 1
        driver.record("huawi_hmac_mcp_call", "passed", "local dynamic HMAC and correct synthetic result")
        assert driver.negative_checks() == {
            "wrong_key_status": 401, "unknown_source_status": 403,
            "initialize_status": 200, "unapproved_tool_status": 403,
        }
        first_event = driver.sse_first_event()
        assert first_event["status"] == 200 and first_event["first_event_contains_data"]
        assert first_event["first_event_seconds"] < 1.2, first_event
        driver.record("sse_and_source_isolation", "passed", "early SSE event, wrong key/source/tool rejected")
        driver.check_concurrent_calls()

        terminate(driver.connector_process.process)
        time.sleep(0.2)
        assert driver.direct_initialize()[0].status_code == 503
        driver.start_native_connector(binary)
        driver.wait_connector_tunnel()
        assert driver.direct_initialize()[0].status_code == 200
        driver.record("restart_same_json", "passed", "offline is explicit, current JSON works after restart")

        revoked = driver.revoke_and_verify(enrollment_id)
        assert revoked["edge_status_after_revoke"] == 401 and revoked["visitor_route_released"]
        driver.record("revoke", "passed", "revoking the code also disables its previously enrolled device")
        print(json.dumps({"status": "passed", "checks": driver.checks}, ensure_ascii=False, indent=2))
        return 0
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
