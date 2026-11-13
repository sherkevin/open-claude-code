"""Run an isolated loopback preview on Linux without changing system services.

Usage: .venv/bin/python scripts/preview.py start|stop|status
Requires the server installed in the project venv, built client/dist, and nginx.
Credentials stay in .runtime/ with owner-only permissions.
"""
from __future__ import annotations
import argparse
import json
import os
import secrets
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime"
PYTHON = ROOT / ".venv/bin/python"
API_PORT = 18080
CLIENT_PORT = 18081
MCP_PORT = 8767

def private_write(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
    path.chmod(0o600)

def environment():
    env = dict(os.environ)
    provider_path = os.getenv("AGENT_PROVIDER_SETTINGS", "")
    if provider_path and not Path(provider_path).is_file():
        raise SystemExit("AGENT_PROVIDER_SETTINGS does not point to a readable settings file")
    if not provider_path:
        provider_path = next((str(path) for path in (STATE / "provider-settings.json", Path.home() / ".claude/settings.json") if path.is_file()), "")
    provider = json.loads(Path(provider_path).read_text()) if provider_path else {}
    model = os.getenv("AGENT_MODEL") or os.getenv("ANTHROPIC_MODEL") or provider.get("env", {}).get("ANTHROPIC_MODEL") or provider.get("model")
    if not model:
        raise SystemExit("Configure AGENT_MODEL or a provider settings model before starting")
    env.update({
        "PYTHONPATH": str(ROOT / "server"),
        "AGENT_DATA_DIR": str(STATE / "data"),
        "AGENT_BRAND_NAME": "知行工作台",
        "AGENT_MODEL": model,
        "AGENT_EFFORT": "medium",
        "AGENT_ALLOWED_ORIGINS": f"http://127.0.0.1:{CLIENT_PORT},http://localhost:{CLIENT_PORT},http://127.0.0.1:{API_PORT},http://localhost:{API_PORT}",
        "AGENT_MCP_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "AGENT_COOKIE_SECURE": "false",
        "AGENT_PROVIDER_COST_RELIABLE": "false",
        "DEMO_MCP_HOST": "127.0.0.1",
        "DEMO_MCP_PORT": str(MCP_PORT),
        "DEMO_MCP_DATA_DIR": str(STATE / "demo-mcp"),
    })
    if provider_path:
        env["AGENT_PROVIDER_SETTINGS"] = provider_path
    else:
        env.pop("AGENT_PROVIDER_SETTINGS", None)
    return env

def owned_process(pid: int) -> bool:
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
        return str(ROOT) in command
    except OSError:
        return False

def recorded():
    file = STATE / "processes.json"
    return json.loads(file.read_text()) if file.is_file() else {}

def stop():
    for name, pid in recorded().items():
        if owned_process(pid):
            os.kill(pid, signal.SIGTERM)
            print(f"Stopping project {name} (PID {pid})")
    time.sleep(1)

def start():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    current = recorded()
    if any(owned_process(pid) for pid in current.values()):
        raise SystemExit("Project preview already running; use status, or stop before start.")
    if not (ROOT / "client/dist/index.html").is_file():
        raise SystemExit("Client build missing")
    password_file = STATE / "admin-password"
    if not password_file.exists():
        private_write(password_file, secrets.token_urlsafe(24) + "\n")
    env = environment()
    database = STATE / "data/app.sqlite3"
    admin_exists = False
    if database.exists():
        with sqlite3.connect(database) as connection:
            admin_exists = connection.execute("SELECT 1 FROM users WHERE username='admin'").fetchone() is not None
    if not admin_exists:
        subprocess.run([str(PYTHON), "-m", "app.cli", "init-admin", "--username", "admin", "--password-file", str(password_file), "--display-name", "管理员"], cwd=ROOT / "server", env=env, check=True)
    nginx_dir = STATE / "nginx"
    nginx_dir.mkdir(exist_ok=True)
    config = f'''pid {nginx_dir}/nginx.pid;
error_log {nginx_dir}/error.log;
events {{ worker_connections 256; }}
http {{
  include /etc/nginx/mime.types;
  default_type application/octet-stream;
  access_log {nginx_dir}/access.log;
  client_body_temp_path {nginx_dir}/body;
  proxy_temp_path {nginx_dir}/proxy;
  server {{
    listen 127.0.0.1:{CLIENT_PORT};
    server_name localhost;
    root {ROOT}/client/dist;
    index index.html;
    client_max_body_size 24m;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy same-origin always;
    gzip on;
    gzip_comp_level 6;
    gzip_min_length 1024;
    gzip_vary on;
    gzip_types
      text/plain
      text/css
      text/xml
      application/xml
      application/json
      application/javascript
      text/javascript
      application/wasm
      image/svg+xml;
    location /api/ {{
      proxy_pass http://127.0.0.1:{API_PORT};
      proxy_http_version 1.1;
      proxy_set_header Host $http_host;
      proxy_set_header Connection "";
      proxy_buffering off;
      proxy_read_timeout 3600s;
    }}
    location /assets/ {{ expires 1y; try_files $uri =404; }}
    location / {{ add_header Cache-Control no-cache; try_files $uri $uri/ /index.html; }}
  }}
}}
'''
    private_write(nginx_dir / "nginx.conf", config)
    subprocess.run(["nginx", "-t", "-p", str(nginx_dir) + "/", "-c", str(nginx_dir / "nginx.conf")], check=True)
    processes = {}
    commands = {
        "mcp": [str(PYTHON), str(ROOT / "examples/demo_mcp.py")],
        "server": [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
        "client": ["nginx", "-p", str(nginx_dir) + "/", "-c", str(nginx_dir / "nginx.conf"), "-g", "daemon off;"],
    }
    for name, command in commands.items():
        with (STATE / f"{name}.log").open("ab") as logfile:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
        processes[name] = process.pid
    private_write(STATE / "processes.json", json.dumps(processes, indent=2))
    deadline = time.monotonic() + 20
    while True:
        try:
            with urlopen(f"http://127.0.0.1:{API_PORT}/api/v1/health/ready", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            pass
        if time.monotonic() >= deadline:
            raise SystemExit("Preview API did not become ready; inspect the private server log")
        time.sleep(0.2)
    print(f"Preview processes started. Client: http://127.0.0.1:{CLIENT_PORT}")
    print(f"Admin username: admin; password stored only in {password_file}")

def status():
    result = {name: {"pid": pid, "running": owned_process(pid)} for name, pid in recorded().items()}
    try:
        with urlopen(f"http://127.0.0.1:{API_PORT}/api/v1/health/ready", timeout=3) as response:
            result["api_health"] = json.loads(response.read())
    except Exception as exc:
        result["api_health"] = type(exc).__name__
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    options = parser.parse_args()
    if os.name != "posix":
        raise SystemExit("Preview launcher is for the Linux development server")
    {"start": start, "stop": stop, "status": status}[options.action]()
