"""Exercise real product routes and the pinned Mem0 SDK with a loopback embedding fixture.

Run in a disposable product + SDK environment with local-only model resolution.
Every stage uses the real app, committed native snapshots and synthetic fixtures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

import mem0_probe
from mem0_probe import LocalProvider, calls  # also installs the loopback-only guard

# Count rejected attempts too: a dependency may catch the guard's exception
# and otherwise leave no trace in successful provider-response counters.
rejected = {"dns": 0, "connect": 0}
pilot_local_only = mem0_probe.local_only
pilot_getaddrinfo = socket.getaddrinfo


def checked_address(address):
    try:
        return pilot_local_only(address)
    except AssertionError:
        rejected["connect"] += 1
        raise


def checked_dns(host, *args, **kwargs):
    try:
        return pilot_getaddrinfo(host, *args, **kwargs)
    except AssertionError:
        rejected["dns"] += 1
        raise


mem0_probe.local_only = checked_address
socket.getaddrinfo = checked_dns


class FaultProvider(LocalProvider):
    mode = "ok"
    fault_calls = 0

    def do_POST(self):
        if type(self).mode == "ok":
            return super().do_POST()
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).fault_calls += 1
        self.send_error(503)


def open_backend(paths, url):
    from app.mem0_index import load_mem0_backend

    assert url.startswith("http://127.0.0.1:")
    provider = {"api_key": "synthetic-test-only", "openai_base_url": url}
    return load_mem0_backend({
        "vector_store": {"provider": "qdrant", "config": {
            "collection_name": "product_fixture", "path": str(paths.vectors),
            "embedding_model_dims": 8, "on_disk": True}},
        "embedder": {"provider": "openai", "config": {
            **provider, "model": "fixture-embedding", "embedding_dims": 8}},
        "llm": {"provider": "openai", "config": {**provider, "model": "fixture-llm"}},
        "history_db_path": str(paths.history),
    }, io_timeout_seconds=3)


def run_phase(args):
    import pytest
    from fastapi.testclient import TestClient
    from test_mem0_product_api import _login, _new_app, _seed, _url
    from app import mem0_product

    project = Path(__file__).resolve().parents[2]
    assert Path(mem0_product.__file__).resolve() == project / "server/app/mem0_product.py"

    root = Path(args.root)
    os.environ["MEM0_DIR"] = str(root / "mem0-home")
    state_file = root / "conversations.json"
    checks, opened = [], []
    http = ThreadingHTTPServer(("127.0.0.1", 0), FaultProvider)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{http.server_port}/v1"
    def factory(paths):
        opened.append(paths)
        return open_backend(paths, url)

    def response(r, expected=200):
        assert r.status_code == expected, (r.status_code, r.text)
        return r.json()

    texts = ["库存阈值为十二", "审阅格式为 CSV", "只属于另一位用户的记录"]
    try:
        with pytest.MonkeyPatch.context() as env:
            if args.phase == "write":
                with TestClient(_new_app(root / "disabled", env, enabled=False)) as default:
                    _, _, _, cid = _seed(default.app.state.service)
                    _login(default, "owner")
                    denied = response(default.post(_url(cid, "/sync")), 403)
                    assert denied["error"]["code"] == "MEM0_DISABLED"
                    assert default.app.state.service.mem0_product.host is None
                    assert "mem0" not in sys.modules and calls["embedding"] == 0
                    checks.append("disabled route rejects sync before SDK import or provider I/O")

            app = _new_app(root / "app", env, enabled=True)
            with TestClient(app) as client:
                service = app.state.service
                service.mem0_product.configure(scoped_factory=factory)
                if args.phase == "write":
                    owner, other, assistant, first = _seed(service)
                    ids = [first]
                    other_assistant = service.save_assistant(
                        {"name": "Other owner", "connections": [],
                         "access": {"all_members": True, "user_ids": []}}, user=other)
                    for user, agent in ((owner, assistant), (other, other_assistant)):
                        ids.append(service.create_conversation(
                            {"assistant_id": agent["id"], "title": "scope fixture"}, user)["id"])
                    state_file.write_text(json.dumps(ids))
                else:
                    ids = json.loads(state_file.read_text())

                def query(cid):
                    return response(client.post(_url(cid, "/query"), json={"q": "库存阈值"}))

                if args.phase == "write":
                    for i, (cid, text) in enumerate(zip(ids, texts)):
                        _login(client, "other" if i == 2 else "owner")
                        native = f"/api/v1/conversations/{cid}/memory"
                        before = len(opened)
                        assert query(cid)["status"] == "fallback" and len(opened) == before
                        revision = response(client.get(native))["revision"]
                        response(client.put(native + "/file", json={"revision": revision,
                            "path": "MEMORY.md", "content": text, "operation_id": "seed"}))
                        synced = response(client.post(_url(cid, "/sync")))
                        assert synced["status"] == "indexed" and synced["added"] == 1
                        assert len(opened) == before + 1
                    assert len({p.history for p in opened}) == 3
                    checks.append("three committed API edits sync to three distinct SQLite/Qdrant scopes")
                    checks.append("three pre-sync queries return fallback without opening an SDK")
                else:
                    for i, (cid, text) in enumerate(zip(ids, texts)):
                        _login(client, "other" if i == 2 else "owner")
                        hit = query(cid)
                        assert hit["status"] == "verified"
                        assert [h["text"] for h in hit["hits"]] == [text]
                        source = hit["hits"][0]
                        native = f"/api/v1/conversations/{cid}/memory"
                        revision = response(client.get(native))["revision"]
                        assert source["revision"] == revision and source["source_path"] == "MEMORY.md"
                        assert source["content_sha256"] == hashlib.sha256(text.encode()).hexdigest()
                    checks.append("fresh product process retrieves exact per-scope text, revision, path and hash")
                    before = len(opened)
                    for action, body in (("sync", {}), ("query", {"q": "库存"}),
                                         ("retire", {"operation_id": "clear"})):
                        response(client.post(_url(ids[0], "/" + action), json=body), 404)
                    assert len(opened) == before
                    checks.append("other owner gets 404 on query, sync and retire before SDK open")

                    _login(client, "owner")
                    first, second, _ = ids
                    FaultProvider.mode = "unavailable"
                    try:
                        failed = query(first)
                    finally:
                        FaultProvider.mode = "ok"
                    assert FaultProvider.fault_calls == 1 and failed["status"] == "fallback"
                    assert failed["hits"] == []
                    native = f"/api/v1/conversations/{first}/memory"
                    assert response(client.get(native + "/file", params={"path": "MEMORY.md"}))["content"] == texts[0]
                    assert query(first)["status"] == "verified"
                    checks.append("single provider 503 yields fallback; native read and next query succeed")

                    response(client.post(_url(first, "/retire"), json={"operation_id": "wrong"}), 403)
                    revision = response(client.get(native))["revision"]
                    response(client.delete(native, params={"revision": revision, "operation_id": "clear"}))
                    receipt = response(client.post(_url(first, "/retire"), json={"operation_id": "clear"}))
                    assert receipt["state"] == "retired" and receipt["physical_deletion"] is False
                    assert receipt["cleanup_status"] == "not_performed" and opened[0].history.exists()
                    before = len(opened)
                    assert query(first)["status"] == "fallback"
                    response(client.post(_url(first, "/sync")), 503)
                    assert len(opened) == before and query(second)["status"] == "verified"
                    checks.append("committed clear retires only the target; history remains and reopen is refused")

                    # Fault injection after a confirmed real close, never skip closing the SDK.
                    from unittest.mock import patch
                    from app.mem0_index import Mem0SdkBackend

                    close = Mem0SdkBackend.close
                    def lost_receipt(instance):
                        close(instance)
                        raise RuntimeError("synthetic close receipt lost")
                    with patch.object(Mem0SdkBackend, "close", lost_receipt):
                        assert query(second)["status"] == "fallback"
                    before = len(opened)
                    assert query(second)["status"] == "fallback" and len(opened) == before
                    _login(client, "other")
                    assert query(ids[2])["status"] == "verified"
                    checks.append("loss of actual SDK close acknowledgement blocks only the affected scope")
        assert calls["extraction"] == 0
        print(json.dumps({"phase": args.phase, "product_pid": os.getpid(), "checks": checks,
                          "passed": len(checks), "loopback_provider": dict(calls),
                          "injected_provider_503": FaultProvider.fault_calls,
                          "blocked_nonlocal_attempts": rejected}, ensure_ascii=False), flush=True)
        assert not any(rejected.values()), rejected
    finally:
        http.shutdown()
        http.server_close()
        thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["write", "reopen"])
    parser.add_argument("--root")
    args = parser.parse_args()
    if sys.flags.optimize:
        raise RuntimeError("Run the probe with assertions enabled")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    if args.phase:
        return run_phase(args)
    packages = ("fastapi", "pydantic", "mem0ai", "openai", "qdrant-client", "protobuf")
    print(json.dumps({"mode": "same_process", "versions": {p: version(p) for p in packages}}),
          flush=True)
    with tempfile.TemporaryDirectory(prefix="d31-product-") as root:
        for phase in ("write", "reopen"):
            command = [sys.executable, __file__, "--phase", phase, "--root", root]
            subprocess.run(command, check=True, timeout=210)


if __name__ == "__main__":
    main()
