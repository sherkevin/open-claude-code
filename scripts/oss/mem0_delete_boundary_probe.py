"""Fixed SDK deletion boundary; synthetic rows only, no production or external I/O."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

# Reuse existing loopback-only provider and outbound network guard.
from mem0_probe import LocalProvider, calls
from mem0_index_adapter_probe import (
    Mem0DerivedIndex, Mem0IndexConfig, Reader, docs, load_mem0_backend, scope,
)


def main():
    from qdrant_client import models

    assert version("mem0ai") == "2.2.1"
    assert version("qdrant-client") == "1.20.0"
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="mem0-delete-boundary-") as temp:
            root = Path(temp)
            os.environ["MEM0_DIR"] = str(root / "mem0-home")
            url = f"http://127.0.0.1:{server.server_port}/v1"
            config = {
                "vector_store": {"provider": "qdrant", "config": {
                    "collection_name": "delete_boundary_fixture", "path": str(root / "vectors"),
                    "embedding_model_dims": 8, "on_disk": True}},
                "embedder": {"provider": "openai", "config": {
                    "model": "fixture-embedding", "api_key": "synthetic-test-only",
                    "openai_base_url": url, "embedding_dims": 8}},
                "llm": {"provider": "openai", "config": {
                    "model": "fixture-llm", "api_key": "synthetic-test-only",
                    "openai_base_url": url}},
                "history_db_path": str(root / "history.sqlite"),
            }
            backend = load_mem0_backend(config, io_timeout_seconds=1.0)
            checks = []
            try:
                first = scope(1, user="synthetic-delete-owner")
                other = scope(1, user="synthetic-other-owner")
                reader = Reader(docs(1)[:1])
                other_reader = Reader(docs(2)[:1])
                adapter = Mem0DerivedIndex(Mem0IndexConfig(enabled=True), backend, reader)
                other_adapter = Mem0DerivedIndex(Mem0IndexConfig(enabled=True), backend, other_reader)
                assert adapter.sync(first).added == 1
                assert other_adapter.sync(other).added == 1
                fields = ("user_id", "agent_id", "run_id", "kaiwu_schema",
                          "kaiwu_user_id", "kaiwu_conversation_id",
                          "kaiwu_agent_id", "kaiwu_run_id")
                filters = adapter._filters(first)
                store = backend.memory.vector_store
                points, cursor = store.client.scroll(
                    collection_name=store.collection_name,
                    scroll_filter=models.Filter(must=[
                        models.FieldCondition(key=key, match=models.MatchValue(value=value))
                        for key, value in filters.items()
                    ]),
                    limit=201, with_payload=list(fields), with_vectors=False,
                )
                assert len(points) == 1 and cursor is None
                point = points[0]
                assert set(point.payload) <= set(fields) and point.vector is None
                assert all(point.payload[k] == v for k, v in filters.items())
                assert point.payload["kaiwu_user_id"] == first.user_id
                assert point.payload["kaiwu_conversation_id"] == first.conversation_id
                assert point.payload["kaiwu_agent_id"] == adapter.config.agent_id
                assert point.payload["kaiwu_run_id"] == first.conversation_id
                checks.append("Qdrant supports scoped ownership-only projection without body or vectors")

                # These calls intentionally inspect synthetic SDK history to verify
                # deletion semantics; they are NOT an authorized post-revocation
                # production path.
                before = backend.get_all(filters=filters)["results"]
                assert len(before) == 1 and before[0]["memory"] == reader.docs[0].text
                memory_id = before[0]["id"]
                assert memory_id == str(point.id)
                expected = reader.docs[0].text
                assert any(h["new_memory"] == expected
                           for h in backend.memory.db.get_history(memory_id))
                checks.append("Mem0 ADD history retains synthetic source text")
                request_count = dict(calls)
                backend.delete(memory_id)
                assert not backend.get_all(filters=filters)["results"]
                history = backend.memory.db.get_history(memory_id)
                assert any(h["event"] == "DELETE" and h["old_memory"] == expected
                           and h["is_deleted"] for h in history)
                assert calls == request_count
                checks.append("Mem0 delete removes vector but copies old text into DELETE history")
                assert len(backend.get_all(filters=other_adapter._filters(other))["results"]) == 1
                checks.append("other owner preserved")
                backend.close()
                reopened = load_mem0_backend(config, io_timeout_seconds=1.0)
                try:
                    assert not reopened.get_all(filters=filters)["results"]
                    assert any(h["event"] == "DELETE" and h["old_memory"] == expected
                               for h in reopened.memory.db.get_history(memory_id))
                    assert len(reopened.get_all(filters=other_adapter._filters(other))["results"]) == 1
                finally:
                    reopened.close()
                checks.append("deleted text remains in persistent SQLite after reopen")
                print(json.dumps({"passed": len(checks), "checks": checks,
                                  "sdk": version("mem0ai"), "qdrant": version("qdrant-client"),
                                  "loopback_requests": calls,
                                  "physical_deletion_verified": False}))
            finally:
                backend.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
