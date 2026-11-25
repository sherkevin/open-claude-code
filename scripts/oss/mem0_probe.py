"""Exercise the real self-hosted Mem0 SDK with isolated synthetic data.

Run ONLY in the dedicated pilot venv. The loopback provider returns deterministic
fixtures; this validates integration, not memory extraction or retrieval quality.
No platform/customer credentials, sessions, files or network providers are used.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from importlib.metadata import version

os.environ["MEM0_TELEMETRY"] = "false"
# Test-only guard: forbid outbound socket connections and nonlocal DNS.
# Production must separately enforce network egress with OS/container policy.
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex
original_getaddrinfo = socket.getaddrinfo

def local_only(address):
    if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
        raise AssertionError("Non-loopback connection refused by pilot")

def connect(self, address):
    local_only(address)
    return original_connect(self, address)

def connect_ex(self, address):
    local_only(address)
    return original_connect_ex(self, address)

def getaddrinfo(host, *args, **kwargs):
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise AssertionError("Non-loopback DNS refused by pilot")
    return original_getaddrinfo(host, *args, **kwargs)

socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
calls = {"embedding": 0, "extraction": 0}

class LocalProvider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.headers.get("Authorization") == "Bearer synthetic-test-only"
        if self.path == "/v1/embeddings":
            calls["embedding"] += 1
            texts = body["input"]
            if isinstance(texts, str):
                texts = [texts]
            # Deliberately collide all vectors: the scope filter must protect
            # even when every other user's result would rank equally high.
            response = {"object": "list", "model": "fixture-embedding", "data": [
                {"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 7}
                for i, _ in enumerate(texts)], "usage": {"prompt_tokens": 1, "total_tokens": 1}}
        elif self.path == "/v1/chat/completions":
            calls["extraction"] += 1
            text = json.dumps({"memory": [{"text": "Synthetic preferred report format is CSV", "attributed_to": "user"}]})
            response = {"id": "fixture-completion", "object": "chat.completion", "created": 1,
                        "model": "fixture-llm", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        else:
            self.send_error(404)
            return
        raw = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

def texts(result):
    return {row["memory"] for row in result["results"]}

def main():
    assert version("mem0ai") == "2.2.1"
    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalProvider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix="mem0-probe-") as temp:
            root = Path(temp)
            os.environ["MEM0_DIR"] = str(root / "mem0-home")
            from mem0 import Memory
            from mem0.memory import telemetry
            assert telemetry.MEM0_TELEMETRY is False
            url = f"http://127.0.0.1:{server.server_port}/v1"
            config = {
                "vector_store": {"provider": "qdrant", "config": {
                    "collection_name": "synthetic_memories", "path": str(root / "vectors"),
                    "embedding_model_dims": 8, "on_disk": True}},
                "embedder": {"provider": "openai", "config": {"model": "fixture-embedding",
                    "api_key": "synthetic-test-only", "openai_base_url": url, "embedding_dims": 8}},
                "llm": {"provider": "openai", "config": {"model": "fixture-llm",
                    "api_key": "synthetic-test-only", "openai_base_url": url}},
                "history_db_path": str(root / "history.sqlite"),
            }
            a = {"user_id": "fixture-user-a", "agent_id": "fixture-agent-a", "run_id": "fixture-session-a"}
            b = {"user_id": "fixture-user-b", "agent_id": "fixture-agent-a", "run_id": "fixture-session-b"}
            c = {"user_id": "fixture-user-a", "agent_id": "fixture-agent-a", "run_id": "fixture-session-c"}
            cases = []
            def open_memory():
                return Memory.from_config(config)
            def close_memory(m):
                m.close()
                m.vector_store.client.close()
            m = open_memory()
            try:
                x = m.add("Synthetic inventory threshold is 12", **a, infer=False)["results"]
                assert len(x) == 1 and x[0]["event"] == "ADD"
                m.add("Synthetic inventory threshold is 900", **b, infer=False)
                m.add("Synthetic inventory threshold is 45", **c, infer=False)
                cases.append("approved-fact insertion via real SDK")
                expected = {"Synthetic inventory threshold is 12"}
                assert texts(m.search("inventory threshold", filters=a, threshold=0, top_k=10)) == expected
                assert texts(m.get_all(filters=b)) == {"Synthetic inventory threshold is 900"}
                assert texts(m.get_all(filters=c)) == {"Synthetic inventory threshold is 45"}
                cases.append("user and session filters even with identical vectors")
                try:
                    m.search("inventory", filters={})
                except ValueError:
                    pass
                else:
                    raise AssertionError("unscoped search unexpectedly accepted")
                cases.append("unscoped search rejected")
                result = m.add([{"role":"user","content":"Synthetic preference: CSV"}], **a, infer=True)
                assert texts(result) == {"Synthetic preferred report format is CSV"}
                assert calls["extraction"] == 1
                cases.append("real extraction pipeline with loopback fixture model")
                again = m.add([{"role":"user","content":"Synthetic preference: CSV"}], **a, infer=True)
                assert not again["results"]
                cases.append("duplicate extracted fact not reinserted")
                expected.add("Synthetic preferred report format is CSV")
                assert texts(m.get_all(filters=a)) == expected
            finally:
                close_memory(m)
            m = open_memory()
            try:
                assert texts(m.get_all(filters=a)) == expected
                cases.append("persistent reopen")
                m.delete_all(**a)
                assert not m.search("inventory", filters=a, threshold=0, top_k=10)["results"]
                assert texts(m.get_all(filters=b)) == {"Synthetic inventory threshold is 900"}
                assert texts(m.get_all(filters=c)) == {"Synthetic inventory threshold is 45"}
                cases.append("scoped delete removes retrieval while other scopes remain")
            finally:
                close_memory(m)
            print(json.dumps({"sdk": version("mem0ai"), "vector_store": "local Qdrant", "passed": len(cases),
                              "cases": cases, "loopback_requests": calls, "quality_evaluation": False}))
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)

if __name__ == "__main__":
    main()
