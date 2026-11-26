"""Real pinned SDK + actual scoped host lifecycle with synthetic source authority.

The real SQLite authority is covered in the separate host pytest environment.
No service credential or customer data; provider is the existing loopback fixture.
"""
import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from mem0_probe import LocalProvider, calls
from mem0_index_adapter_probe import (
    Reader, docs, scope, Mem0IndexConfig, Mem0IndexUnavailable, load_mem0_backend,
)
from app import mem0_host


def main():
    assert version("mem0ai") == "2.2.1"
    assert version("qdrant-client") == "1.20.0"
    http = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    prior_reader = mem0_host.NativeMemorySourceReader
    try:
        with tempfile.TemporaryDirectory(prefix="mem0-scoped-host-") as tmp:
            root = Path(tmp)
            os.environ["MEM0_DIR"] = str(root / "mem0-home")
            url = f"http://127.0.0.1:{http.server_port}/v1"
            cfg = Mem0IndexConfig(enabled=True)
            a, b = scope(1, user="synthetic-host-a"), scope(1, user="synthetic-host-b")
            authority = {a.user_id: (a, docs(1)[:1]), b.user_id: (b, docs(2)[:1])}
            service = SimpleNamespace(settings=SimpleNamespace(data_dir=root), memory=authority)
            opened, closed, checks = [], [], []

            class SyntheticReader:
                def __init__(self, memory, *, run_id=None):
                    assert memory is authority and run_id is None
                def current_scope(self, user_id, conversation_id):
                    owner, _ = authority[user_id]
                    assert conversation_id == owner.conversation_id
                    return owner
                def load(self, owner):
                    expected, documents = authority[owner.user_id]
                    assert owner == expected
                    return Reader(documents).load(owner)

            mem0_host.NativeMemorySourceReader = SyntheticReader

            class Owned:
                def __init__(self, backend, paths):
                    self.backend, self.paths = backend, paths
                def __getattr__(self, name):
                    return getattr(self.backend, name)
                def close(self):
                    self.backend.close()
                    closed.append(self.paths)

            def factory(p):
                backend = load_mem0_backend({
                    "vector_store": {"provider": "qdrant", "config": {
                        "collection_name": "scoped_host_fixture", "path": str(p.vectors),
                        "embedding_model_dims": 8, "on_disk": True}},
                    "embedder": {"provider": "openai", "config": {
                        "model": "fixture-embedding", "api_key": "synthetic-test-only",
                        "openai_base_url": url, "embedding_dims": 8}},
                    "llm": {"provider": "openai", "config": {
                        "model": "fixture-llm", "api_key": "synthetic-test-only",
                        "openai_base_url": url}},
                    "history_db_path": str(p.history),
                })
                opened.append(p)
                return Owned(backend, p)

            host = mem0_host.Mem0Host(service, cfg, scoped_factory=factory)
            assert host.lookup(a.user_id, a.conversation_id, "fixture").status == "fallback"
            assert not opened and not host.scope_store.root.exists()
            checks.append("missing lookup performs no SDK open or store provision")
            for owner in (a, b):
                assert host.sync(owner.user_id, owner.conversation_id).added == 1
            assert len(opened) == len(closed) == 2 and opened[0] != opened[1]
            checks.append("explicit sync provisions isolated SDK storage and closes both")
            host = mem0_host.Mem0Host(service, cfg, scoped_factory=factory)
            for owner in (a, b):
                result = host.lookup(owner.user_id, owner.conversation_id, "fixture")
                assert result.status == "verified"
                assert [hit.text for hit in result.hits] == [authority[owner.user_id][1][0].text]
            assert len(opened) == len(closed) == 4 and opened[:2] == opened[2:]
            checks.append("fresh host reopens and retrieves only the correct scope")
            host.scope_store.retire(a)
            assert host.lookup(a.user_id, a.conversation_id, "fixture").status == "fallback"
            try:
                host.sync(a.user_id, a.conversation_id)
                raise AssertionError("retired scope synced")
            except Mem0IndexUnavailable:
                pass
            assert len(opened) == len(closed) == 4
            assert host.lookup(b.user_id, b.conversation_id, "fixture").status == "verified"
            assert len(opened) == len(closed) == 5
            checks.append("retired scope never reopens, other scope remains usable")
            c = scope(1, user="synthetic-host-close-loss")
            authority[c.user_id] = (c, docs(3)[:1])
            assert host.sync(c.user_id, c.conversation_id).added == 1

            class LostCloseReceipt(Owned):
                def close(self):
                    super().close()
                    raise RuntimeError("synthetic loss of SDK close receipt")

            def uncertain_factory(p):
                owned = factory(p)
                return LostCloseReceipt(owned.backend, p)

            uncertain = mem0_host.Mem0Host(service, cfg, scoped_factory=uncertain_factory)
            result = uncertain.lookup(c.user_id, c.conversation_id, "fixture")
            assert result.status == "fallback" and not result.hits
            before = len(opened)
            fresh = mem0_host.Mem0Host(service, cfg, scoped_factory=factory)
            assert fresh.lookup(c.user_id, c.conversation_id, "fixture").status == "fallback"
            assert len(opened) == before
            assert fresh.lookup(b.user_id, b.conversation_id, "fixture").status == "verified"
            assert len(opened) == before + 1
            checks.append("lost real SDK close acknowledgment persists across host instances and isolates only affected scope")
            assert calls["extraction"] == 0
            print(json.dumps({"passed": len(checks), "checks": checks,
                              "loopback_requests": calls, "production_authority_verified": False}))
    finally:
        mem0_host.NativeMemorySourceReader = prior_reader
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
