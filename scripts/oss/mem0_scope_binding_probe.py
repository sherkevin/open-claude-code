"""Real pinned SDK lifecycle under a private scope-store lease (synthetic only)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

from mem0_probe import LocalProvider, calls
from mem0_index_adapter_probe import (
    Mem0DerivedIndex, Mem0IndexConfig, Mem0IndexUnavailable, Reader, docs,
    load_mem0_backend, scope,
)
from app.mem0_scope_store import ScopeStore


def main():
    assert version("mem0ai") == "2.2.1"
    assert version("qdrant-client") == "1.20.0"
    http = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="mem0-binding-") as tmp:
            root = Path(tmp)
            os.environ["MEM0_DIR"] = str(root / "mem0-home")
            url = f"http://127.0.0.1:{http.server_port}/v1"
            cfg = Mem0IndexConfig(enabled=True)
            a = scope(1, user="synthetic-binding-a")
            b = scope(1, user="synthetic-binding-b")
            s = ScopeStore(root, cfg)
            checks = []

            def sdk(p):
                return load_mem0_backend({
                    "vector_store": {"provider": "qdrant", "config": {
                        "collection_name": "scope_fixture", "path": str(p.vectors),
                        "embedding_model_dims": 8, "on_disk": True}},
                    "embedder": {"provider": "openai", "config": {
                        "model": "fixture-embedding", "api_key": "synthetic-test-only",
                        "openai_base_url": url, "embedding_dims": 8}},
                    "llm": {"provider": "openai", "config": {
                        "model": "fixture-llm", "api_key": "synthetic-test-only",
                        "openai_base_url": url}},
                    "history_db_path": str(p.history),
                })

            ids = []
            for i, owner in enumerate((a, b), 1):
                s.provision(owner)
                with s.lease(owner) as p:
                    backend = sdk(p)
                    try:
                        index = Mem0DerivedIndex(cfg, backend, Reader(docs(i)[:1]))
                        assert index.sync(owner).added == 1
                        rows = backend.get_all(filters=index._filters(owner))["results"]
                        assert len(rows) == 1
                        ids.append(rows[0]["id"])
                    finally:
                        backend.close()
            checks.append("two real SDK stores provisioned under separate persistent leases")
            s = ScopeStore(root, cfg)
            for i, owner in enumerate((a, b)):
                with s.lease(owner) as p:
                    backend = sdk(p)
                    try:
                        index = Mem0DerivedIndex(cfg, backend, Reader([]))
                        assert len(backend.get_all(filters=index._filters(owner))["results"]) == 1
                        assert backend.memory.db.get_history(ids[i])
                        assert not backend.memory.db.get_history(ids[1-i])
                    finally:
                        backend.close()
            checks.append("new host object reopens both persisted SDK stores with isolated histories")
            s.retire(a)
            try:
                with ScopeStore(root, cfg).lease(a):
                    raise AssertionError("retired scope opened")
            except Mem0IndexUnavailable:
                pass
            checks.append("retired scope denied before SDK initialization")
            with s.lease(b) as p:
                backend = sdk(p)
                try:
                    index = Mem0DerivedIndex(cfg, backend, Reader([]))
                    assert len(backend.get_all(filters=index._filters(b))["results"]) == 1
                    assert backend.memory.db.get_history(ids[1])
                finally:
                    backend.close()
            checks.append("other scope remains usable after retirement")
            assert calls == {"embedding": 2, "extraction": 0}
            print(json.dumps({"passed": len(checks), "checks": checks,
                              "loopback_requests": calls, "physical_deletion_verified": False,
                              "production_authority_verified": False}))
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
