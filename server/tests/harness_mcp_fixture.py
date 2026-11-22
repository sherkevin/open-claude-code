"""Loopback-only real MCP transport and deterministic Messages model fixtures."""

import copy
import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations


@pytest.fixture
def local_mcp_server():
    """Official FastMCP over real HTTP; only a synthetic bearer is accepted."""
    calls, authenticated = [], []
    token = "synthetic-mcp-credential-must-stay-in-python"
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    mcp = FastMCP("harness-business-fixture", host="127.0.0.1", port=port, stateless_http=True)

    def called(tool, arguments):
        calls.append({"tool": tool, "arguments": copy.deepcopy(arguments)})

    def source_payload():
        return {"rows": [{"asset_id": "R-17", "pressure": 7}], "sources": [{
            "kind": "data", "title": "Synthetic asset pressure",
            "locator": {"database": "fixture", "table": "assets", "fields": ["asset_id", "pressure"],
                        "record_keys": ["R-17"], "time_range": "2026-11-10", "timezone": "UTC",
                        "queried_at": "2026-11-10T00:00:00Z", "trace_id": "fixture-read-17"},
            "excerpt": "R-17 pressure is 7 bar."}]}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    def read_assets(query: str, filters: dict, limit: int = 3) -> dict:
        """Read synthetic asset records using complete structured filters."""
        called("read_assets", {"query": query, "filters": filters, "limit": limit})
        return source_payload()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False))
    def update_asset(record_id: str, changes: dict, options: dict) -> dict:
        """Update one synthetic asset after host approval."""
        called("update_asset", {"record_id": record_id, "changes": changes, "options": options})
        return {"updated": record_id, "changes": changes, "options": options}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    def delete_asset(record_id: str, operation: str) -> dict:
        """Delete one synthetic asset after two administrator approvals."""
        called("delete_asset", {"record_id": record_id, "operation": operation})
        return {"deleted": record_id}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), structured_output=False)
    def read_failure(query: str) -> CallToolResult:
        """Return an MCP error containing untrusted source-shaped data."""
        called("read_failure", {"query": query})
        return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(source_payload()))])

    @mcp.tool()
    def hidden_admin(operation: str) -> dict:
        """An ungranted administrative tool that must never be exposed or called."""
        called("hidden_admin", {"operation": operation})
        return {"unexpected_execution": True}

    inner = mcp.streamable_http_app()

    async def authenticated_app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            valid = headers.get(b"authorization") == ("Bearer " + token).encode()
            authenticated.append(valid)
            if not valid:
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"Unauthorized"})
                return
        await inner(scope, receive, send)

    server = uvicorn.Server(uvicorn.Config(authenticated_app, log_config=None, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() >= deadline:
            server.should_exit = True
            thread.join(5)
            sock.close()
            pytest.fail("local FastMCP service did not start")
        time.sleep(0.01)
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{port}/mcp", token=token, calls=calls, authenticated=authenticated)
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive(), "local FastMCP service leaked"


@pytest.fixture
def local_model_server():
    """Stream selected calls through actual Harness model HTTP handling."""
    actions, requests = {}, []
    faults = {"disconnect_after_result": False, "overflow_after_result": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/v1/messages":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            last = body["messages"][-1]["content"]
            blocks = last if isinstance(last, list) else [{"type": "text", "text": last}]
            result = next((item for item in blocks if item["type"] == "tool_result"), None)
            text = " ".join(item.get("text", "") for item in blocks)
            action = next((value for case, value in actions.items() if case in text), None)
            if result and not action and faults["overflow_after_result"]:
                payload = json.dumps({"error": {"type": "invalid_request_error", "code": "context_length_exceeded",
                    "message": "This model maximum context length is 128000 tokens; your input exceeds that limit."}}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            disconnect = bool(result and not action and faults["disconnect_after_result"])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            if disconnect:
                self.send_header("Content-Length", "99999")
            self.end_headers()

            def event(kind, data):
                self.wfile.write((f"event: {kind}\ndata: " + json.dumps({"type": kind, **data}) + "\n\n").encode())
                self.wfile.flush()

            try:
                event("message_start", {"message": {"id": f"mcp-msg-{len(requests)}", "type": "message", "role": "assistant",
                    "model": "fixture", "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 12, "output_tokens": 0}}})
                # A cancelled tool call is restored as a synthetic error block
                # beside the next user message. Honor that new task even when
                # the same Messages turn contains an older tool_result.
                if action:
                    name, arguments = action
                    event("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": f"mcp-call-{len(requests)}", "name": name, "input": {}}})
                    encoded = json.dumps(arguments, ensure_ascii=False)
                    middle = len(encoded) // 2
                    for piece in (encoded[:middle], encoded[middle:]):
                        event("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": piece}})
                    reason = "tool_use"
                else:
                    marker = re.search(r"\[\[source:[A-Za-z0-9_-]+\]\]", json.dumps(result, ensure_ascii=False)) if result else None
                    answer = "已核实压力为 7 bar " + marker.group(0) if marker else "工具请求已处理，未提供可引用来源。"
                    event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": answer}})
                    if disconnect:
                        faults["disconnect_after_result"] = False
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    reason = "end_turn"
                event("content_block_stop", {"index": 0})
                event("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 8}})
                event("message_stop", {})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}/v1", actions=actions, requests=requests, faults=faults)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        assert not thread.is_alive(), "local model service leaked"
