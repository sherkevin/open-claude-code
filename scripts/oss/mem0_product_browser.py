"""Serve a disposable product and built client under /project/kaiwu for Chromium."""
import argparse
import os
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

import uvicorn
from starlette.staticfiles import StaticFiles
from app.config import Settings
from app.main import create_app
from mem0_product_sdk_probe import FaultProvider, calls, open_backend, rejected
from test_mem0_product_api import _seed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--mode", choices=["baseline", "enabled", "disabled", "unconfigured", "partial"], required=True)
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      AGENT_KNOWLEDGE_EMBEDDING_MODEL="local-lexical-v1")
    if args.mode == "partial":
        FaultProvider.mode = "unavailable"
    with tempfile.TemporaryDirectory() as tmp, socket.socket() as sock, ThreadingHTTPServer(
            ("127.0.0.1", 0), FaultProvider) as provider:
        os.environ["MEM0_DIR"] = str(Path(tmp) / "mem0")
        sock.bind(("127.0.0.1", 0))
        origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        provider_url = f"http://127.0.0.1:{provider.server_port}/v1"
        settings = Settings(data_dir=Path(tmp) / "app", provider_settings="", model="synthetic",
                            origins=(origin,), runtime_type="harness", mem0_product_enabled=args.mode != "disabled")

        def forbidden_runtime(*args, **kwargs):
            raise AssertionError("browser fixture must never start a model run")

        app = create_app(settings, runtime_factory=forbidden_runtime)
        startup = app.router.lifespan_context
        env = dict(os.environ, MEMORY_TEST_ORIGIN=origin, MEMORY_TEST_MODE=args.mode)

        @asynccontextmanager
        async def lifespan(app):
            async with startup(app):
                _, _, _, cid = _seed(app.state.service)
                env["MEMORY_TEST_CONVERSATION"] = cid
                if args.mode in ("baseline", "enabled", "partial"):
                    app.state.service.mem0_product.configure(
                        scoped_factory=lambda paths: open_backend(paths, provider_url))
                yield

        app.router.lifespan_context = lifespan
        app.mount("/", StaticFiles(directory=args.dist, html=True))
        prefix = "/project/kaiwu"

        async def mounted(scope, receive, send):
            if scope["type"] in ("http", "websocket") and scope["path"].startswith(prefix + "/"):
                scope = dict(scope, path=scope["path"][len(prefix):],
                             raw_path=scope["raw_path"][len(prefix):])
            await app(scope, receive, send)

        server = uvicorn.Server(uvicorn.Config(mounted, log_level="warning", lifespan="on"))
        api_thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        sdk_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        api_thread.start()
        sdk_thread.start()
        try:
            deadline = time.monotonic() + 30
            while not server.started:
                if not api_thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("disposable workbench did not start")
                time.sleep(.1)
            subprocess.run([os.environ["KAIWU_BROWSER_NODE"], str(Path(__file__).with_suffix(".mjs"))],
                           env=env, check=True, timeout=150)
            assert not any(rejected.values()), rejected
            assert calls["extraction"] == 0
            assert FaultProvider.fault_calls == int(args.mode == "partial")
            print(f"loopback={calls} rejected_nonlocal={rejected} injected_503={FaultProvider.fault_calls}",
                  flush=True)
        finally:
            server.should_exit = True
            api_thread.join(15)
            provider.shutdown()
            sdk_thread.join()


if __name__ == "__main__":
    main()
