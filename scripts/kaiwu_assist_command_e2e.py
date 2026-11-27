#!/usr/bin/env python3
"""Drive the real server and compiled connector through a remote CMD assistance session.

The customer side performs one action: start assistance from the connector's local
page. Every later command is pulled by the connector, executed locally and reported
back automatically. This script asserts that flow end to end on loopback.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
RUNTIME_ROOT = ROOT / ".runtime"
TENANT_ID = "tenant-kaiwu-assist-e2e"
TENANT_NAME = "开物协助联调企业"
ADMIN_USER = "assist-admin"
ADMIN_PASSWORD = "synthetic-assist-password-123"


def pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_http(url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2.0, trust_env=False).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"timed out waiting for {url}")


def terminate(process: subprocess.Popen | None, timeout: float = 10.0) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


class Harness:
    def __init__(self) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.runtime = RUNTIME_ROOT / f"kaiwu-assist-real-{stamp}"
        self.runtime.mkdir(parents=True, exist_ok=False)
        self.logs = self.runtime / "logs"
        self.logs.mkdir(mode=0o700)
        self.server_port = pick_port()
        self.connector_port = pick_port()
        self.admin_token = "assist-admin-" + secrets.token_urlsafe(32)
        self.server: subprocess.Popen | None = None
        self.connector: subprocess.Popen | None = None
        self.connector_token = ""
        self.checks: list[dict] = []

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "PYTHONPATH": str(SERVER_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "AGENT_DATA_DIR": str(self.runtime / "server-data"),
            "AGENT_PROVIDER_SETTINGS": "",
            "AGENT_COOKIE_SECURE": "false",
            "AGENT_ALLOWED_ORIGINS": f"http://127.0.0.1:{self.server_port}",
            "AGENT_MCP_ALLOWED_HOSTS": "127.0.0.1",
            "AGENT_KAIWU_CONTROL_ENABLED": "true",
            "AGENT_KAIWU_CONTROL_ADMIN_TOKEN": self.admin_token,
            "AGENT_KAIWU_NATIVE_RELAY_ENABLED": "true",
            "AGENT_KAIWU_ASSIST_TENANTS": TENANT_ID,
            "AGENT_KAIWU_EDGE_PUBLIC_BASE": f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp",
            "AGENT_KAIWU_EDGE_MOUNT_PATH": "/projects/kaiwu/mcp",
        })
        return env

    def spawn(self, name: str, command: list[str], cwd: Path) -> subprocess.Popen:
        log = open(self.logs / f"{name}.log", "ab", buffering=0)
        return subprocess.Popen(
            command, cwd=cwd, env=self.env(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
        )

    def check(self, name: str, ok: bool, **detail) -> None:
        self.checks.append({"check": name, "status": "pass" if ok else "fail", **detail})
        if not ok:
            raise AssertionError(f"{name}: {detail}")

    def start_server(self) -> None:
        password_file = self.runtime / "admin-password"
        password_file.write_text(ADMIN_PASSWORD, encoding="utf-8")
        subprocess.run(
            [sys.executable, "-m", "app.cli", "init-admin", "--username", ADMIN_USER,
             "--password-file", str(password_file)],
            cwd=SERVER_DIR, env=self.env(), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.server = self.spawn("server", [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(self.server_port), "--log-level", "warning",
        ], SERVER_DIR)
        wait_http(f"http://127.0.0.1:{self.server_port}/api/v1/health/live")

    def start_connector(self) -> None:
        binary = Path(os.environ.get(
            "CONNECTOR_BINARY",
            "/media/data3/dengkw/acceptance/kaiwu-assist-real-e2e/kaiwu-connector-linux",
        ))
        self.connector = self.spawn("connector", [
            str(binary), "serve",
            "--data-dir", str(self.runtime / "connector-data"),
            "--listen", f"127.0.0.1:{self.connector_port}",
            "--cloud", f"http://127.0.0.1:{self.server_port}/api/kaiwu",
            "--edge", f"http://127.0.0.1:{self.server_port}/projects/kaiwu/mcp",
            "--platform", f"http://127.0.0.1:{self.server_port}/",
            "--native-relay",
        ], ROOT)
        wait_http(f"http://127.0.0.1:{self.connector_port}/")
        page = httpx.get(f"http://127.0.0.1:{self.connector_port}/", trust_env=False).text
        matched = re.search(r'const TOKEN = "([0-9a-f]{48})";', page)
        if not matched:
            raise RuntimeError("could not read local connector session token")
        self.connector_token = matched.group(1)


def main() -> int:
    harness = Harness()
    local = httpx.Client(
        base_url=f"http://127.0.0.1:{harness.connector_port}", timeout=60, trust_env=False,
        headers={"X-Connector-Token": harness.connector_token},
    )
    try:
        harness.start_server()
        print(f"server ready on 127.0.0.1:{harness.server_port}")

        platform = httpx.Client(
            base_url=f"http://127.0.0.1:{harness.server_port}/api/v1", timeout=60, trust_env=False,
        )
        user = platform.post("/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASSWORD})
        user.raise_for_status()
        harness.check("admin-login", user.json().get("role") == "admin", user=user.json().get("username"))

        control = httpx.Client(
            base_url=f"http://127.0.0.1:{harness.server_port}/api/kaiwu", timeout=60, trust_env=False,
            headers={"Authorization": f"Bearer {harness.admin_token}"},
        )
        enrollment = control.post("/v1/enrollments", json={"tenant_id": TENANT_ID, "tenant_name": TENANT_NAME})
        enrollment.raise_for_status()
        code = enrollment.json()["code"]
        harness.check("enrollment-issued", bool(code))

        harness.start_connector()
        local.headers["X-Connector-Token"] = harness.connector_token
        bound = local.post("/api/bind", json={"code": code})
        bound.raise_for_status()
        identity = bound.json()["data"]
        print(f"connector bound: {identity.get('connector_id')}")
        harness.check("connector-bound", identity.get("state") in ("READY", "CONFIGURING"), state=identity.get("state"))

        # Negotiate the fixed week consent capability before the customer's
        # explicit start, exactly like the local page does.
        local.get("/api/assist/offer").raise_for_status()
        started = local.post("/api/assist/start", json={})
        started.raise_for_status()
        offer = started.json()["data"]
        harness.check("week-consent-negotiated", offer.get("consent_policy") == "week_168h", offer=offer)
        print(f"assistance opened: {offer['id']}")

        assist = httpx.Client(
            base_url=f"http://127.0.0.1:{harness.server_port}/api/v1/connections/kaiwu-assist",
            timeout=60, trust_env=False, cookies=platform.cookies,
        )
        devices = assist.get("/devices")
        if devices.status_code != 200:
            print(f"devices failed: {devices.status_code} {devices.text}", file=sys.stderr)
        devices.raise_for_status()
        device = next((row for row in devices.json()["devices"] if row["id"] == identity["connector_id"]), None)
        harness.check("device-visible", device is not None)
        session_id = device.get("active_session_id") or device.get("session_id")
        harness.check("session-visible", bool(session_id), session_id=session_id)

        claimed = assist.post(f"/sessions/{session_id}/claim")
        claimed.raise_for_status()

        def run_command(command: str, timeout_seconds: int) -> dict:
            queued = assist.post(
                f"/sessions/{session_id}/jobs",
                json={"action": "command", "command": command, "timeout_seconds": timeout_seconds},
            )
            queued.raise_for_status()
            deadline = time.monotonic() + timeout_seconds + 60
            while time.monotonic() < deadline:
                view = assist.get(f"/sessions/{session_id}")
                view.raise_for_status()
                job = view.json()["latest_job"]
                if job and job.get("status") in ("succeeded", "failed"):
                    return job
                time.sleep(0.5)
            raise RuntimeError(f"command job did not finish: {command}")

        marker = "kaiwu-assist-e2e-ok"
        first = run_command(f"echo {marker}", 30)
        harness.check(
            "command-executed-and-reported",
            first.get("status") == "succeeded" and first.get("code") == "command_completed"
            and first.get("exit_code") == 0 and marker in (first.get("output") or ""),
            job=first,
        )
        print(f"command 1 succeeded in {first.get('duration_ms')} ms: {first.get('output', '').strip()}")

        second = run_command("sleep 30", 5)
        harness.check(
            "command-timeout-killed",
            second.get("status") == "failed" and second.get("code") == "command_timeout",
            job=second,
        )
        print("command 2 reported command_timeout")

        stopped = local.post("/api/assist/stop", json={"session_id": session_id})
        stopped.raise_for_status()
        after = assist.get(f"/sessions/{session_id}")
        after.raise_for_status()
        harness.check("session-stopped", after.json()["status"] == "stopped", status=after.json()["status"])
        print("session stopped by the customer side")
        return 0
    finally:
        terminate(harness.connector)
        terminate(harness.server)
        report = harness.runtime / "report.json"
        report.write_text(json.dumps({
            "tenant": TENANT_ID,
            "server_port": harness.server_port,
            "connector_port": harness.connector_port,
            "checks": harness.checks,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"report: {report}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - report and fail
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
