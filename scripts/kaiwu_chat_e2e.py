#!/usr/bin/env python3
"""Run one actual Kaiwu web-chat acceptance through the enterprise MCP bridge.

The wrapper reuses the isolated bridge driver, starts the real product server
and Vite client, imports a synthetic provider credential into an owner-only
runtime file, and delegates the user-visible workflow to
``scripts/kaiwu_chat_browser.mjs``. It never writes credentials to evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from kaiwu_enterprise_e2e import (
    CUSTOMER_BEARER,
    EVIDENCE,
    HMAC_KEY,
    ROOT,
    SYNTHETIC_ORDER,
    Driver,
    SyntheticMCPHandler,
    pick_port,
    run_checked,
    source_snapshot,
    terminate,
    wait_http,
)


HOME_PROVIDER_SETTINGS = Path("/home/dengkw/.claude/settings.json")
MODEL_CATALOG = Path(
    "/media/data3/dengkw/projects/open-claude-code/.runtime/config/model-catalog.json"
)
HARNESS_NODE = Path(
    "/media/data3/dengkw/projects/open-claude-code/.runtime/harness-toolchain/"
    "node-v24.9.0-linux-x64/bin/node"
)
HARNESS_SOURCE = Path(
    "/media/data3/dengkw/projects/open-claude-code/.runtime/third_party/deepseek-harness"
)
PLAYWRIGHT_CORE = Path(
    "/media/data3/dengkw/t3-code/bin/t3-0.1.9-linux-x64/node_modules/playwright-core"
)
CHROMIUM = Path(
    "/media/data3/dengkw/tmp-archive-20261104/pw-browsers/chromium-1248/"
    "chrome-linux64/chrome"
)


def write_private(path: Path, value: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)


def provider_settings(path: Path) -> str:
    document = json.loads(HOME_PROVIDER_SETTINGS.read_text(encoding="utf-8"))
    source = document.get("env") if isinstance(document, dict) else None
    if not isinstance(source, dict):
        raise RuntimeError("private provider settings do not contain env")
    allowed = (
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "API_TIMEOUT_MS",
    )
    selected = {
        key: source[key]
        for key in allowed
        if isinstance(source.get(key), (str, int)) and str(source[key]).strip()
    }
    if not selected.get("ANTHROPIC_BASE_URL") or not (
        selected.get("ANTHROPIC_AUTH_TOKEN") or selected.get("ANTHROPIC_API_KEY")
    ):
        raise RuntimeError("private provider route is incomplete")
    write_private(path, json.dumps({"env": selected}, ensure_ascii=False, indent=2) + "\n")
    return str(selected["ANTHROPIC_BASE_URL"])


def scan_artifacts(paths: list[Path], secrets_to_find: list[str]) -> dict[str, Any]:
    hits: list[dict[str, str]] = []
    scanned: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        scanned.append(str(path))
        raw = path.read_bytes()
        for index, secret in enumerate(secrets_to_find, 1):
            if secret and secret.encode("utf-8") in raw:
                hits.append({"file": str(path), "secret_class": f"value_{index}"})
    return {"scanned": scanned, "hits": hits}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frps", default="")
    parser.add_argument(
        "--linux-frpc",
        default=str(ROOT / "connector/packaging/bin/frpc-linux-amd64"),
    )
    parser.add_argument("--run-timeout", type=int, default=300)
    args = parser.parse_args()

    go = Path("/media/data3/dengkw/toolchains/go1.27.4/bin/go")
    frps = Path(args.frps) if args.frps else Path(
        "/media/data3/dengkw/scratch/tmp/kaiwu-release-audit/verified-linux/"
        "frp_0.61.1_linux_amd64/frps"
    )
    frps = frps.resolve()
    frpc = Path(args.linux_frpc).resolve()
    required = [go, frps, frpc, MODEL_CATALOG, HARNESS_NODE, HARNESS_SOURCE,
                PLAYWRIGHT_CORE, CHROMIUM, HOME_PROVIDER_SETTINGS]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise SystemExit("missing required acceptance input: " + ", ".join(missing))

    driver = Driver(server_port=8000)
    runtime_cache = driver.runtime / "cache"
    runtime_tmp = driver.runtime / "tmp"
    runtime_cache.mkdir(mode=0o700)
    runtime_tmp.mkdir(mode=0o700)
    # Chromium rejects Unix singleton socket paths above the platform limit.
    # Keep the browser scratch directory under the approved data disk but use
    # a deliberately short path instead of the full evidence runtime path.
    browser_tmp = Path("/media/data3/dengkw/.tmp") / f"hwchat-{os.getpid()}"
    browser_tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ.update({
        "TMPDIR": str(browser_tmp),
        "TMP": str(browser_tmp),
        "TEMP": str(browser_tmp),
        "GOCACHE": str(runtime_cache / "go-build"),
        "GOTMPDIR": str(runtime_tmp),
        "PYTHONPYCACHEPREFIX": str(runtime_cache / "pycache"),
        "XDG_CACHE_HOME": str(runtime_cache / "xdg"),
    })
    private = driver.runtime / "browser-private"
    private.mkdir(mode=0o700)
    password = "Huawi-Web-" + secrets.token_urlsafe(24)
    password_file = private / "admin-password.txt"
    write_private(password_file, password + "\n")
    provider_file = private / "provider-settings.json"
    provider_base = provider_settings(provider_file)
    mcp_config_file = private / "mcp-config.json"
    vite_port = pick_port()
    vite_url = f"http://127.0.0.1:{vite_port}"
    screenshot_dir = driver.runtime / "browser-screenshots"
    browser_report_path = driver.runtime / "browser-report.json"

    driver.cloud_env_overrides.update({
        "AGENT_PROVIDER_SETTINGS": str(provider_file),
        "AGENT_PROVIDER_TEST_MODE": "true",
        "AGENT_MODEL_CATALOG": str(MODEL_CATALOG),
        "AGENT_DEFAULT_MODEL_ID": "dano-v2.1",
        "AGENT_HARNESS_NODE_PATH": str(HARNESS_NODE),
        "AGENT_HARNESS_SOURCE_DIR": str(HARNESS_SOURCE),
        "AGENT_ALLOWED_ORIGINS": (
            f"http://127.0.0.1:{driver.server_port},{vite_url}"
        ),
    })

    started = datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "status": "running",
        "started_at": started.isoformat(),
        "runtime_dir": str(driver.runtime),
        "workspace": str(ROOT),
        **source_snapshot(),
        "browser_mode": "actual_product_ui",
        "checks": [],
        "provider": {
            "route": "loopback_test_adapter",
            "host": provider_base.split("://", 1)[-1].split("/", 1)[0],
            "model_id": "dano-v2.1",
            "upstream_model": "tencent-cloud/deepseek-v4.1-flash-1017[1m]",
            "provider_test_mode": True,
            "production_route": False,
        },
        "limitations": [
            "The business MCP and order are deterministic synthetic fixtures.",
            "The model call traverses the school-cloud loopback adapter; this is not a Tencent Cloud TokenHub production deployment.",
            "This proves the actual product web UI and model tool loop, not customer Windows Server 2019 or real public DNS/TLS.",
        ],
    }

    vite = None
    try:
        result["versions"] = {
            "go": subprocess.check_output([str(go), "version"], text=True).strip(),
            "frps": subprocess.check_output([str(frps), "--version"], text=True).strip(),
            "frpc": subprocess.check_output([str(frpc), "--version"], text=True).strip(),
            "node": subprocess.check_output(["node", "--version"], text=True).strip(),
            "vite": subprocess.check_output(
                ["client/node_modules/.bin/vite", "--version"], cwd=ROOT, text=True
            ).strip(),
        }

        admin_env = driver.env()
        run_checked(
            [
                sys.executable,
                "-m",
                "app.cli",
                "init-admin",
                "--username",
                "browser-admin",
                "--password-file",
                str(password_file),
                "--display-name",
                "华微网页验收管理员",
            ],
            cwd=ROOT / "server",
            env=admin_env,
        )
        result["checks"].append({
            "check": "isolated_admin_initialized",
            "status": "passed",
            "detail": "owner-only password file; password not recorded",
        })

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
            cwd=ROOT / "connector",
            env={**driver.env(), "CGO_ENABLED": "0"},
        )

        driver.start_mock()
        driver.start_cloud()
        driver.start_connector(connector_binary, frpc)
        driver.configure_connection()
        driver.start_bridge(frps, frpc)
        driver.create_integration()
        driver.wait_edge_ready()
        driver.wait_connector_tunnel()
        write_private(
            mcp_config_file,
            json.dumps(driver.integration["mcp_config"], ensure_ascii=False, indent=2) + "\n",
        )
        result["checks"].append({
            "check": "isolated_bridge_ready",
            "status": "passed",
            "detail": "official frps/frpc, visitor, Edge and synthetic MCP are running",
        })

        vite = driver.spawn(
            "vite",
            [
                str(ROOT / "client/node_modules/.bin/vite"),
                "--host",
                "127.0.0.1",
                "--port",
                str(vite_port),
                "--strictPort",
            ],
            cwd=ROOT / "client",
            env=driver.env(),
        )
        wait_http(vite_url, timeout=30)
        result["checks"].append({
            "check": "actual_client_started",
            "status": "passed",
            "detail": vite_url,
        })

        calls_before = SyntheticMCPHandler.calls
        browser_env = os.environ.copy()
        browser_env.update({
            "PLAYWRIGHT_CORE_PATH": str(PLAYWRIGHT_CORE),
            "BROWSER_EXECUTABLE": str(CHROMIUM),
            "BROWSER_NO_SANDBOX": "true",
            "BROWSER_HEADLESS": "true",
            "BROWSER_RUN_TIMEOUT_MS": str(args.run_timeout * 1000),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        browser = subprocess.run(
            [
                "node",
                str(ROOT / "scripts/kaiwu_chat_browser.mjs"),
                "--base-url",
                vite_url,
                "--password-file",
                str(password_file),
                "--mcp-config-file",
                str(mcp_config_file),
                "--report",
                str(browser_report_path),
                "--screenshots",
                str(screenshot_dir),
            ],
            cwd=ROOT,
            env=browser_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=args.run_timeout + 60,
            check=False,
        )
        result["browser_output"] = browser.stdout[-12000:]
        if browser.returncode != 0:
            raise RuntimeError(f"browser acceptance failed with exit {browser.returncode}")
        if not browser_report_path.is_file():
            raise RuntimeError("browser acceptance did not write its report")
        browser_report = json.loads(browser_report_path.read_text(encoding="utf-8"))
        if browser_report.get("status") != "passed":
            raise RuntimeError("browser report did not finish successfully")
        tool_events = browser_report.get("chat", {}).get("tool_call_events", [])
        if len(tool_events) != 1 or tool_events[0].get("success") is not True:
            raise RuntimeError("the browser report does not prove one successful tool call")
        if browser_report.get("chat", {}).get("answer_contains_fixture") is not True:
            raise RuntimeError("the final assistant answer did not contain the synthetic order")

        calls_after = SyntheticMCPHandler.calls
        signatures_after = SyntheticMCPHandler.signatures_valid
        if calls_after <= calls_before:
            raise RuntimeError("the web chat did not invoke the synthetic business MCP")
        answer_text = json.dumps(browser_report.get("chat", {}), ensure_ascii=False)
        if SYNTHETIC_ORDER not in answer_text:
            raise RuntimeError("the web chat evidence does not contain the synthetic order")
        result["checks"].append({
            "check": "actual_web_chat_called_mcp",
            "status": "passed",
            "detail": (
                f"synthetic MCP calls increased {calls_before}->{calls_after}; "
                f"valid HMAC tool calls={signatures_after}"
            ),
        })
        result["browser_report"] = browser_report

        integration_token = driver.integration.get("access_token", "")
        provider_document = json.loads(provider_file.read_text(encoding="utf-8"))
        provider_env = provider_document.get("env", {})
        provider_token = str(
            provider_env.get("ANTHROPIC_AUTH_TOKEN")
            or provider_env.get("ANTHROPIC_API_KEY")
            or ""
        )
        scan_paths = [
            browser_report_path,
            *sorted(driver.logs.glob("*.log")),
            driver.cloud_data / "app.sqlite3",
            *sorted(screenshot_dir.glob("*.png")),
        ]
        secret_scan = scan_artifacts(
            scan_paths,
            [integration_token, CUSTOMER_BEARER, HMAC_KEY, password, provider_token],
        )
        browser_storage_check = next(
            (
                item for item in browser_report.get("checks", [])
                if item.get("name") == "import_standard_mcp_json_without_secret_leak"
            ),
            None,
        )
        if secret_scan["hits"] or browser_storage_check is None:
            raise RuntimeError("public artifact or browser storage secret check failed")
        result["secret_scan"] = {
            **secret_scan,
            "browser_storage_checked": True,
            "note": "PNG metadata bytes were scanned; visual masking is separately captured in screenshots.",
        }
        result["checks"].append({
            "check": "secret_boundary",
            "status": "passed",
            "detail": "K, customer bearer, HMAC key, administrator password and provider token absent from public artifacts",
        })
        result["status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["checks"].append({
            "check": "failure",
            "status": "failed",
            "detail": str(exc),
        })
    finally:
        if vite is not None:
            terminate(vite.process)
            vite.log.close()
            driver.processes.remove(vite)
        driver.close()
        shutil.rmtree(browser_tmp, ignore_errors=True)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["duration_seconds"] = round(
            (datetime.now(timezone.utc) - started).total_seconds(), 3
        )
        EVIDENCE.mkdir(mode=0o700, parents=True, exist_ok=True)
        evidence_path = EVIDENCE / "e2e-chat-browser-latest.json"
        write_private(
            evidence_path,
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            mode=0o600,
        )
        print(json.dumps({
            "status": result["status"],
            "evidence": str(evidence_path),
            "runtime_dir": str(driver.runtime),
            "error": result.get("error"),
        }, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
