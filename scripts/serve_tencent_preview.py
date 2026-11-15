"""Serve an isolated full product and its built UI without replacing live data.

Configuration stays in a mode-0600 JSON file on the target server. This launcher
does not seed data, mutate the existing deployment, or replace the runtime.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18092)
    parser.add_argument("--static-dir", type=Path, required=True)
    args = parser.parse_args()
    source = args.env_file
    if source.is_symlink() or not source.is_file() or stat.S_IMODE(source.stat().st_mode) != 0o600:
        parser.error("A private regular configuration file with mode 0600 is required")
    values = json.loads(source.read_text())
    if not isinstance(values, dict) or any(
        not isinstance(k, str) or not k.startswith(("AGENT_", "ANTHROPIC_"))
        or not isinstance(v, str) for k, v in values.items()
    ):
        parser.error("Configuration contains unsupported environment entries")
    if not (args.static_dir / "index.html").is_file():
        parser.error("Build the UI before starting this preview")
    project = Path(__file__).resolve().parents[1]
    required = ("AGENT_DATA_DIR", "AGENT_SESSION_COOKIE_NAME", "AGENT_COOKIE_PATH")
    if any(not values.get(key) for key in required):
        parser.error("An explicit data directory and isolated session cookie are required")
    data = Path(values["AGENT_DATA_DIR"]).resolve()
    if not data.is_relative_to(project / ".private"):
        parser.error("Preview data must be contained in this checkout's private directory")
    if values["AGENT_COOKIE_PATH"] != "/zhixing-next/" or values["AGENT_SESSION_COOKIE_NAME"] != "zhixing_preview_20261115":
        parser.error("This preview requires its own cookie name and path")
    os.umask(0o077)
    for key in tuple(os.environ):
        if key.startswith(("AGENT_", "ANTHROPIC_")):
            del os.environ[key]
    os.environ.update(values)
    sys.path.insert(0, str(project / "server"))
    from app.main import app
    from starlette.staticfiles import StaticFiles
    import uvicorn
    app.mount("/", StaticFiles(directory=args.static_dir, html=True), name="preview-ui")
    # The application's explicit trusted-proxy policy handles forwarded headers.
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
