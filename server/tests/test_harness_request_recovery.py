"""Pinned Node + real HTTP/SSE recovery; local synthetic provider, no paid calls."""
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.config import PROVIDER_KEYS, Settings
from app.main import create_app
from test_api import PASSWORD, login, send
from test_harness_real_runtime import wait_run


@pytest.fixture
def recovery_provider():
    state = {"case": "first", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            attempt, case = len(state["requests"]), state["case"]
            if case.startswith("http-"):
                status = int(case.split("-")[1])
                payload = json.dumps({"error": {"type": "synthetic", "message": "private-provider-diagnostic"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            failed = (case in {"first", "cancel", "partial"} and attempt == 1
                      or case == "always" or case == "after-tool" and attempt == 2
                      or case == "two-steps" and attempt in {1, 3})
            if failed and case != "partial":
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            if case == "partial":
                # A truncated HTTP body produces TRANSPORT, rather than a valid
                # clean EOF (STREAM_CLOSED), after text reached the customer.
                self.send_header("Content-Length", "99999")
            self.end_headers()

            def event(kind, data):
                self.wfile.write((f"event: {kind}\ndata: " + json.dumps({"type": kind, **data}) + "\n\n").encode())
                self.wfile.flush()

            try:
                event("message_start", {"message": {"id": f"msg-{attempt}", "type": "message", "role": "assistant", "model": "fixture",
                    "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 0}}})
                wants_tool = case == "after-tool" and attempt == 1 or case == "two-steps" and attempt == 2
                if wants_tool:
                    event("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": "only-tool-call", "name": "knowledge_search", "input": {}}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"query": "P-900 pressure", "limit": 3})}})
                else:
                    event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "已收到部分回复" if case == "partial" else "恢复完成"}})
                    if case == "partial":
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                event("content_block_stop", {"index": 0})
                event("message_delta", {"delta": {"stop_reason": "tool_use" if wants_tool else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 8}})
                event("message_stop", {})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", state
    server.shutdown()
    server.server_close()
    worker.join(2)


@pytest.mark.parametrize("case,expected_status,expected_requests", [
    ("first", "succeeded", 2), ("after-tool", "succeeded", 3),
    ("always", "failed", 2), ("two-steps", "failed", 3),
    ("partial", "failed", 1), ("cancel", "cancelled", 1),
    ("http-401", "failed", 1), ("http-403", "failed", 1), ("http-429", "failed", 1),
])
def test_bounded_request_recovery_preserves_tools_and_respects_visibility_and_cancel(
    tmp_path, monkeypatch, recovery_provider, case, expected_status, expected_requests,
):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, state = recovery_provider
    state["case"] = case
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{"id": "fixture", "label": "Local recovery fixture", "upstream_model": "fixture",
        "reasoning_levels": {"default": None}, "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base}}]}))
    app = create_app(Settings(data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="", provider_settings="",
        origins=("http://testserver",), approval_seconds=30, model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False))
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Test", "password": PASSWORD, "role": "admin"})
        login(client)
        kb = service.knowledge.save({"name": "Synthetic recovery fixture"}, user)
        service.knowledge.upload(kb["id"], user, "facts.md", b"P-900 pressure limit is 7 bar.")
        assistant = service.save_assistant({"name": "Recovery fixture", "knowledge_base_ids": [kb["id"]]}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        run_id = send(client, cid, "Query the P-900 business record", "request-recovery")["run_id"]
        if case == "cancel":
            deadline = time.monotonic() + 30
            while not any(e["type"] == "model.retry" for e in service.db.events(run_id, 0)):
                assert time.monotonic() < deadline, "retry backoff never started"
                time.sleep(0.01)
            assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
        result = wait_run(client, run_id)
        assert result["status"] == expected_status, result
        assert len(state["requests"]) == expected_requests
        events = service.db.events(run_id, 0)
        retries = [e["data"] for e in events if e["type"] == "model.retry"]
        expected_retries = 1 if case in {"first", "after-tool", "always", "two-steps", "cancel"} else 0
        assert len(retries) == expected_retries
        assert all(e == {"retry": 1, "max_retries": 1, "delay_ms": 500, "code": "MODEL_TRANSPORT_ERROR"} for e in retries)
        tool_events = [e for e in events if e["type"] == "tool.completed"]
        assert len(tool_events) == (1 if case in {"after-tool", "two-steps"} else 0)
        if tool_events:
            assert tool_events[0]["data"]["success"] is True
        if case == "after-tool":
            assert state["requests"][1]["messages"] == state["requests"][2]["messages"]
        if case in {"first", "after-tool"}:
            # Known recovered usage cannot erase an earlier unknown contribution.
            usage = json.loads(service.require("runs", run_id)["usage"])
            assert usage["input_tokens"] is None
            assert usage["token_accounting"]["provider_usage_complete"] is False
            assert usage["token_accounting"]["known_subtotals"]["input_tokens"] > 0
        if expected_status == "failed" and not case.startswith("http-"):
            assert result["error_code"] == "MODEL_TRANSPORT_ERROR"
        visible = "".join(e["data"]["text"] for e in events if e["type"] == "message.delta")
        assert visible.count("恢复完成") == (1 if expected_status == "succeeded" else 0)
        if case == "partial":
            assert visible.count("已收到部分回复") == 1
        assert "private-provider-diagnostic" not in json.dumps(events)
