"""Run the built workbench against disposable FastAPI/SQLite on loopback."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
import uvicorn
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "server"), str(ROOT / "server/tests")]
from app.main import create_app
from app.config import Settings
from test_api import FakeRuntime, PASSWORD


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--browser-script", choices=("feedback_tracking_browser.mjs","schedule_catalog_browser.mjs","schedule_editor_browser.mjs"), default="feedback_tracking_browser.mjs")
    args = parser.parse_args()
    assert not args.data.exists(), "Refuse existing data directory"
    args.data.mkdir(mode=0o700, parents=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    product = create_app(Settings(data_dir=args.data, provider_settings="", origins=(origin,)), runtime_factory=FakeRuntime)
    original_lifespan = product.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            for name, role in [("reporter","member"), ("admin","admin")]:
                app.state.service.create_user({"username":name,"display_name":name,"password":PASSWORD,"role":role})
            yield

    product.router.lifespan_context = lifespan
    product.mount("/", StaticFiles(directory=args.dist, html=True))
    prefix = "/project/kaiwu"

    async def mounted(scope, receive, send):
        if scope["type"] in ("http","websocket") and scope["path"].startswith(prefix+"/"):
            scope = dict(scope)
            scope["path"] = scope["path"][len(prefix):]
            scope["raw_path"] = scope["raw_path"][len(prefix):]
        await product(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(mounted, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic()+15
        while not server.started:
            if not thread.is_alive() or time.monotonic()>deadline:
                raise RuntimeError("isolated server did not start")
            time.sleep(.05)
        env = dict(os.environ, FEEDBACK_TEST_URL=origin)
        subprocess.run([args.node,str(ROOT/"scripts"/args.browser_script)], env=env, check=True, timeout=100)
    finally:
        server.should_exit=True
        thread.join(15)
        sock.close()

if __name__=="__main__":
    main()
