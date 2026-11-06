import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.service import Service


def test_real_mcp_handshake_discovers_without_executing(tmp_path):
    pytest.importorskip("mcp")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    marker = tmp_path / "tool-was-called"
    process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mcp_server_fixture.py")), str(port), str(marker)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    service = Service(Settings(data_dir=tmp_path / "data", mcp_hosts=("127.0.0.1",)))
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=0.2, trust_env=False)
                break
            except httpx.RequestError:
                if process.poll() is not None:
                    raise AssertionError("MCP fixture exited before listening")
                time.sleep(0.1)
        else:
            raise AssertionError("MCP fixture did not listen")
        connection = service.save_connection({"name": "real MCP", "url": f"http://127.0.0.1:{port}/mcp", "auth_type": "none"})
        result = asyncio.run(service.test_connection(connection["id"]))
        assert result["status"] == "connected", result
        assert result["tools"][0]["name"] == "lookup"
        assert not marker.exists(), "Testing the connection must never execute business tools"
    finally:
        service.db.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
