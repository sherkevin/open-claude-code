"""Test independent local SDK stores for synthetic scopes, not a product purge."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

from mem0_probe import LocalProvider, calls
from mem0_index_adapter_probe import (
    Mem0DerivedIndex, Mem0IndexConfig, Reader, docs, load_mem0_backend, scope,
)


def main():
    assert version("mem0ai") == "2.2.1"
    assert version("qdrant-client") == "1.20.0"
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="mem0-scope-store-") as temp:
            root = Path(temp)
            os.environ["MEM0_DIR"] = str(root / "mem0-home")
            url = f"http://127.0.0.1:{server.server_port}/v1"
            cfg = Mem0IndexConfig(enabled=True)
            a = scope(1, user="synthetic-scope-a")
            b = scope(1, user="synthetic-scope-b")
            c = scope(1, user=a.user_id, cid="synthetic-other-session")
            def config_for(s):
                # Illustrative deterministic path for the synthetic test.
                # No production scope/authority or deletion implementation.
                identity = json.dumps([s.user_id, cfg.agent_id, s.conversation_id],
                                      ensure_ascii=False, separators=(",", ":"))
                path = root / hashlib.sha256(identity.encode()).hexdigest()
                path.mkdir(mode=0o700, exist_ok=True)
                config = {
                    "vector_store": {"provider": "qdrant", "config": {
                        "collection_name": "scope_fixture", "path": str(path / "vectors"),
                        "embedding_model_dims": 8, "on_disk": True}},
                    "embedder": {"provider": "openai", "config": {
                        "model": "fixture-embedding", "api_key": "synthetic-test-only",
                        "openai_base_url": url, "embedding_dims": 8}},
                    "llm": {"provider": "openai", "config": {
                        "model": "fixture-llm", "api_key": "synthetic-test-only",
                        "openai_base_url": url}},
                    "history_db_path": str(path / "history.sqlite"),
                }
                return path, config
            pa, ca = config_for(a)
            pb, cb = config_for(b)
            pc, cc = config_for(c)
            assert len({pa, pb, pc}) == 3
            checks = []
            ba = load_mem0_backend(ca)
            bb = load_mem0_backend(cb)
            bc = load_mem0_backend(cc)
            try:
                items = [(a, ba, 1), (b, bb, 2), (c, bc, 3)]
                ids = []
                for s, backend, i in items:
                    index = Mem0DerivedIndex(cfg, backend, Reader(docs(i)[:1]))
                    assert index.sync(s).added == 1
                    rows = backend.get_all(filters=index._filters(s))["results"]
                    assert len(rows) == 1
                    ids.append(rows[0]["id"])
                    assert backend.memory._entity_store is None
                checks.append("three independently opened local SDK stores; no lazy entity store")
                for i, (s, backend, _) in enumerate(items):
                    index = Mem0DerivedIndex(cfg, backend, Reader([]))
                    for j, (other, _, _) in enumerate(items):
                        if i != j:
                            assert not backend.get_all(filters=index._filters(other))["results"]
                            assert not backend.memory.db.get_history(ids[j])
                        else:
                            assert backend.memory.db.get_history(ids[j])
                checks.append("different owners and same-owner different conversations isolate vectors and SQLite history")
                ba.close()
                # Simulate detaching a closed synthetic namespace; leave the
                # old contents quarantined for explicit verification, do not
                # treat detachment as erasure or reopen its former path.
                quarantine = root / "synthetic-quarantine"
                pa.rename(quarantine)
                assert not pa.exists() and (quarantine / "history.sqlite").is_file()
                assert (quarantine / "vectors").is_dir()
                assert pb.is_dir() and pc.is_dir()
                checks.append("closed synthetic namespace detaches as a unit without reading history")
                for s, backend, _ in items[1:]:
                    index = Mem0DerivedIndex(cfg, backend, Reader([]))
                    assert len(backend.get_all(filters=index._filters(s))["results"]) == 1
                checks.append("other two open scopes remain usable after detachment")
                bb.close()
                bc.close()
                for s, config, memory_id in [(b, cb, ids[1]), (c, cc, ids[2])]:
                    reopened = load_mem0_backend(config)
                    try:
                        index = Mem0DerivedIndex(cfg, reopened, Reader([]))
                        assert len(reopened.get_all(filters=index._filters(s))["results"]) == 1
                        assert reopened.memory.db.get_history(memory_id)
                        assert not reopened.memory.db.get_history(ids[0])
                    finally:
                        reopened.close()
                checks.append("other scopes reopen with original history; detached scope not recreated")
                assert not pa.exists()
                assert calls == {"embedding": 3, "extraction": 0}
                print(json.dumps({"passed": len(checks), "checks": checks,
                                  "sdk": version("mem0ai"), "loopback_requests": calls,
                                  "physical_deletion_verified": False,
                                  "production_authority_verified": False}))
            finally:
                for backend in (ba, bb, bc):
                    backend.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
