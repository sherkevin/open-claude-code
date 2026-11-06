"""Run the real UI against an isolated local server and MCP, without model calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime" / "local-browser"
PYTHON = ROOT / "server" / ".venv" / "Scripts" / "python.exe"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def wait_http(url: str, processes, acceptable=(200,)):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        for name, process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"{name} exited early with {process.returncode}; inspect its private log")
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status in acceptable:
                    return
        except urllib.error.HTTPError as exc:
            if exc.code in acceptable:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.3)
    raise RuntimeError(f"Readiness timed out for {url}")


def main() -> int:
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    node = shutil.which("node")
    if not node or not PYTHON.exists():
        raise RuntimeError("Local node and server/.venv/Scripts/python.exe are required")
    for port in (8768, 18082, 18083):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Port {port} is already occupied; no existing process will be stopped") from exc

    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    work = RUNTIME / stamp
    work.mkdir(mode=0o700)
    password_file = work / "admin-password"
    password_file.write_text(secrets.token_urlsafe(36), encoding="utf-8")
    password_file.chmod(0o600)

    env = os.environ.copy()
    # No provider, SDK, client credential or proxy configuration is inherited by this fixture.
    for name in list(env):
        if name.startswith(("ANTHROPIC_", "CLAUDE_", "AGENT_", "DEMO_MCP_", "SMOKE_", "BROWSER_")) or name in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"}:
            del env[name]
    env.update({
        "AGENT_DATA_DIR": str(work / "app-data"),
        "AGENT_ALLOWED_ORIGINS": "http://127.0.0.1:18083",
        "AGENT_MCP_ALLOWED_HOSTS": "127.0.0.1",
        "AGENT_COOKIE_SECURE": "false",
        "DEMO_MCP_HOST": "127.0.0.1",
        "DEMO_MCP_PORT": "8768",
        "DEMO_MCP_DATA_DIR": str(work / "demo-mcp"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "REAL_MODEL": "false",
        "KEEP_RESOURCES": "false",
        "BROWSER_EXECUTABLE": "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "BROWSER_REPORT": str(work / "browser-smoke.json"),
        "BROWSER_SCREENSHOTS": str(work / "screenshots"),
        "SMOKE_MCP_URL": "http://127.0.0.1:8768/mcp",
        "NO_PROXY": "127.0.0.1,localhost",
    })
    config = work / "vite.config.mjs"
    config.write_text("export default " + json.dumps({
        "root": str(ROOT / "client"),
        "build": {"outDir": str(ROOT / "client" / "dist")},
        "preview": {"host": "127.0.0.1", "port": 18083, "strictPort": True,
                    "proxy": {"/api": {"target": "http://127.0.0.1:18082", "changeOrigin": False}}},
    }) + ";\n", encoding="utf-8")
    processes = []
    logs = []
    status = {"work_dir": str(work), "real_model": False, "processes": [], "status": "running"}

    def start(name, command, cwd):
        output = (work / f"{name}.log").open("wb")
        logs.append(output)
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        processes.append((name, process))
        status["processes"].append({"name": name, "pid": process.pid})
        return process

    try:
        with (work / "init-admin.log").open("wb") as output:
            subprocess.run([str(PYTHON), "-m", "app.cli", "init-admin", "--password-file", str(password_file)],
                           cwd=ROOT / "server", env=env, stdin=subprocess.DEVNULL, stdout=output,
                           stderr=subprocess.STDOUT, creationflags=NO_WINDOW, check=True, timeout=30)
        start("demo-mcp", [str(PYTHON), str(ROOT / "examples" / "demo_mcp.py")], ROOT)
        start("server", [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18082", "--no-access-log"], ROOT / "server")
        start("client", [node, str(ROOT / "client" / "node_modules" / "vite" / "bin" / "vite.js"), "preview", "--config", str(config)], ROOT / "client")
        wait_http("http://127.0.0.1:8768/mcp", processes, (200, 400, 405, 406))
        wait_http("http://127.0.0.1:18082/api/v1/health/live", processes)
        wait_http("http://127.0.0.1:18083/api/v1/config", processes)
        print(f"Isolated local UI ready; evidence: {work}", flush=True)
        check = start("browser", [node, str(ROOT / "scripts" / "browser_smoke.mjs"), "--base-url", "http://127.0.0.1:18083", "--password-file", str(password_file)], ROOT)
        result = check.wait(timeout=360)
        status["browser_exit_code"] = result
        status["status"] = "passed" if result == 0 else "failed"
        print((work / "browser.log").read_text(encoding="utf-8", errors="replace"), end="", flush=True)
        return result
    except Exception as exc:
        status["status"] = "failed"
        status["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for name, process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    # The PID belongs to this live Popen object. Include only its own children
                    # (for example a browser process if the test itself times out).
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=15)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            for item in status["processes"]:
                if item["pid"] == process.pid:
                    item["stopped"] = process.poll() is not None
        for output in logs:
            output.close()
        (work / "launcher.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
        (RUNTIME / "latest.json").write_text(json.dumps({"work_dir": str(work), "status": status["status"]}) + "\n", encoding="utf-8")
        print("Owned background processes stopped; no existing process was targeted.", flush=True)


if __name__ == "__main__":
    sys.exit(main())
