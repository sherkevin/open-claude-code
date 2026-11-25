"""Real browser + pinned Harness + local synthetic model, without customer data."""
import os
import json
from pathlib import Path
import socket
import subprocess
import threading
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn
from starlette.staticfiles import StaticFiles

from app.config import PROVIDER_KEYS
from app.main import create_app
from test_api import PASSWORD
from test_harness_real_runtime import (
    model_server, image_fixture_settings,
)

ROOT = Path(__file__).resolve().parents[2]


def test_goal_state_browser(tmp_path, monkeypatch, model_server):
    required = ["HARNESS_TEST_NODE", "HARNESS_TEST_SOURCE", "KAIWU_BROWSER_DIST",
                "KAIWU_BROWSER_NODE", "PLAYWRIGHT_CORE_PATH", "BROWSER_EXECUTABLE"]
    if not all(os.getenv(name) for name in required):
        pytest.skip("requires explicitly configured pinned runtime and browser build")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = model_server
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = image_fixture_settings(tmp_path, os.environ["HARNESS_TEST_NODE"],
                                      os.environ["HARNESS_TEST_SOURCE"], base)
    settings.origins = (origin,)
    settings.approval_seconds = 90
    settings.harness_goal_state_enabled = True
    app = create_app(settings)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            application.state.service.create_user({"username": "reporter", "display_name": "Fixture",
                                                   "password": PASSWORD, "role": "admin"})
            yield

    app.router.lifespan_context = lifespan
    app.mount("/", StaticFiles(directory=os.environ["KAIWU_BROWSER_DIST"], html=True))
    prefix = "/project/kaiwu"

    async def mounted(scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope["path"].startswith(prefix + "/"):
            scope = dict(scope)
            scope["path"] = scope["path"][len(prefix):]
            scope["raw_path"] = scope["raw_path"][len(prefix):]
        await app(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(mounted, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "isolated server failed to start"
            time.sleep(.05)
        subprocess.run([os.environ["KAIWU_BROWSER_NODE"], str(ROOT / "scripts/goal_state_browser.mjs")],
                       env=dict(os.environ, FEEDBACK_TEST_URL=origin), check=True, timeout=125)
        assert len(requests)==1
        assert app.state.service.db.one("SELECT count(*) n FROM runs")["n"]==1
        assert not app.state.service.db.all("SELECT id FROM approvals")
    finally:
        server.should_exit = True
        thread.join(20)
        sock.close()
