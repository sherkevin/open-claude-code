"""Actual pinned Node Harness + local synthetic Messages server (no paid API).

Opt in with HARNESS_TEST_NODE and HARNESS_TEST_SOURCE. These tests substitute
only the model HTTP endpoint; the process, session log, tools and app are real.
"""
import base64
import io
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import PROVIDER_KEYS, Settings
from app.main import create_app
from test_api import PASSWORD, login, send


COMPACTION_SUMMARY = """## Primary Request and Intent
- Preserve the synthetic P-900 requirement for the resumed task.

## Key Technical Concepts
- Pinned DeepSeek Harness, local synthetic Messages endpoint, durable compaction.

## Files and Code
- (none)

## Errors and Fixes
- (none)

## Pending Jobs
- Continue the synthetic task after compaction.

## Current Work
- The bounded fixture history was compacted into this checkpoint.

## Next Step
- Continue from the established P-900 pressure limit of 7 bar.

## Critical Context
- Summary sentinel: COMPACT_SUMMARY_SENTINEL
- Preserve the exact pressure value: 7 bar."""


@pytest.fixture
def model_server():
    requests, released = [], threading.Event()

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
            text = " ".join(item.get("text", "") for item in blocks)
            compaction_request = "You are now acting as a compaction engine for this AI coding assistant." in json.dumps(body)
            result = next((item for item in blocks if item["type"] == "tool_result"), None)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            def event(kind, data):
                self.wfile.write((f"event: {kind}\ndata: " + json.dumps({"type": kind, **data}) + "\n\n").encode())
                self.wfile.flush()

            try:
                event("message_start", {"message": {"id": f"msg-{len(requests)}", "type": "message", "role": "assistant", "model": "fixture",
                      "content": [], "stop_reason": None, "stop_sequence": None,
                      "usage": {"input_tokens": 37 if compaction_request else 12, "output_tokens": 0}}})
                if compaction_request:
                    answer = COMPACTION_SUMMARY
                elif result:
                    source = re.search(r"\[\[source:[A-Za-z0-9_-]+\]\]", json.dumps(result, ensure_ascii=False))
                    answer = "资料已检索 " + source.group(0) if source else ("工具已拒绝" if result.get("is_error") else "表格已生成")
                else:
                    answer = "恢复成功" if "CASE_RESUME" in text else "实时回复成功"
                if not compaction_request and result is None and ("CASE_KNOWLEDGE" in text or "CASE_XLSX" in text):
                    name = "knowledge_search" if "CASE_KNOWLEDGE" in text else "generate_xlsx"
                    arguments = {"query": "P-900 pressure", "limit": 3} if name == "knowledge_search" else {
                        "filename": "approved.xlsx", "sheets": [{"name": "数据", "rows": [["项目", "数值"], ["压力", 7]]}]}
                    event("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": f"call-{len(requests)}", "name": name, "input": {}}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(arguments)}})
                    reason = "tool_use"
                else:
                    event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": answer[:2]}})
                    if "CASE_HANG" in text:
                        released.wait(30)
                    else:
                        time.sleep(0.15)
                    if "CASE_BROKEN" in text:
                        return
                    event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": answer[2:]}})
                    reason = "end_turn"
                event("content_block_stop", {"index": 0})
                event("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None},
                                        "usage": {"output_tokens": 6 if compaction_request else 8}})
                event("message_stop", {})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", requests
    released.set()
    server.shutdown()
    server.server_close()
    thread.join(2)


def wait_run(client, run_id, statuses=None):
    statuses = statuses or {"succeeded", "failed", "cancelled", "interrupted"}
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = client.get(f"/api/v1/runs/{run_id}").json()
        if result["status"] in statuses or result["status"] in {"failed", "cancelled", "interrupted"}:
            return result
        time.sleep(0.05)
    pytest.fail("real Harness run did not settle")


def image_fixture_settings(tmp_path, node, source, base):
    """The exact settings used by the real-Node image transport acceptance."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{"id": "fixture", "label": "Local attachment protocol fixture", "upstream_model": "fixture",
        "capabilities": ["multimodal"],
        "reasoning_levels": {"default": None}, "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base}}]}))
    return Settings(data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="", provider_settings="",
        provider_test_mode=True,
        origins=("http://testserver",), approval_seconds=30, model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False)


@pytest.mark.parametrize("revocation", ["delete", "bytes"])
def test_real_harness_text_image_payload_resume_and_revocation(tmp_path, monkeypatch, model_server, record_property, revocation):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = model_server
    settings = image_fixture_settings(tmp_path, node, source, base)
    app = create_app(settings)
    from app.harness_runtime import HarnessRuntime
    original_connect, processes = HarnessRuntime.connect, []

    async def track_process(runtime):
        await original_connect(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", track_process)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Test", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "Attachments"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        text = client.post("/api/v1/files", files={"file": ("facts.txt", b"Harness attached pressure is 7 bar.", "text/plain")}).json()
        stream = io.BytesIO()
        Image.new("RGB", (20, 10), "green").save(stream, format="PNG")
        image = client.post("/api/v1/files", files={"file": ("diagram.png", stream.getvalue(), "image/png")}).json()
        first = send(client, cid, "Compare attachments literally /permission", "attachment-first", [text["id"], image["id"]])["run_id"]
        settled = wait_run(client, first)
        assert settled["status"] == "succeeded", settled
        assert len(requests) == 1
        body = requests[0]
        assert "Harness attached pressure is 7 bar." in json.dumps(body)
        # The pinned upstream adds cwd and private normalized-copy hints; it
        # must not receive original upload paths or a tool to read host paths.
        assert str(settings.data_dir / "files") not in json.dumps(body)
        content = body["messages"][-1]["content"]
        rendered = next(block for block in content if block["type"] == "image")
        assert rendered["source"]["type"] == "base64"
        assert rendered["source"]["media_type"] in {"image/png", "image/jpeg", "image/webp"}
        # Official storage may normalize the source. Assert a real raster, not
        # a fake caption or a host filename substituted for visual input.
        with Image.open(io.BytesIO(base64.b64decode(rendered["source"]["data"]))) as raster:
            assert raster.size == (20, 10)
        assert all(tool["name"] != "Read" for tool in body["tools"])
        evidence = service.evidence.list(first, user)
        assert {entry["title"] for entry in evidence} == {"facts.txt", "diagram.png"}
        text_source = next(entry for entry in evidence if entry["title"] == "facts.txt")
        assert client.get(text_source["url"]).status_code == 200
        assert client.get(text_source["content_url"]).content == b"Harness attached pressure is 7 bar."
        service.create_user({"username": "outsider", "display_name": "Other", "password": PASSWORD, "role": "member"})
        login(client, "outsider")
        assert client.get(text_source["url"]).status_code == 404
        assert client.get(text_source["content_url"]).status_code == 404
        login(client)
        private = settings.data_dir / "harness" / cid
        assert (private / "attachments").is_dir()
        second = send(client, cid, "CASE_RESUME previous attachment", "attachment-resume")["run_id"]
        assert wait_run(client, second)["status"] == "succeeded"
        assert len(requests) == 2 and "Harness attached pressure" in json.dumps(requests[1])
        assert any(block["type"] == "image" for message in requests[1]["messages"] for block in message["content"] if isinstance(block, dict))
        image_source = next(entry for entry in evidence if entry["title"] == "diagram.png")
        if revocation == "delete":
            service.db.execute("DELETE FROM files WHERE id=?", (image["id"],))
            assert client.get(image_source["url"]).status_code == 404
        else:
            original = Path(service.require("files", image["id"])["path"])
            original.write_bytes(b"x" * original.stat().st_size)
        third = send(client, cid, "must not reuse revoked bytes", "attachment-revoked")["run_id"]
        assert wait_run(client, third)["status"] == "failed"
        assert len(requests) == 2
    assert len(processes) == 3 and all(process.poll() is not None for process in processes)
    record_property("paid_model_calls", 0)
    record_property("local_synthetic_model_requests", len(requests))
    record_property("visual_quality_claim", "none: encoded-image transport only")


def test_real_harness_stream_tools_approval_cancel_resume(tmp_path, monkeypatch, model_server, record_property):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = model_server
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{"id": "fixture", "label": "本地测试模型", "upstream_model": "fixture",
        "reasoning_levels": {"default": None}, "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base}}]}))
    settings = Settings(data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="", provider_settings="",
        provider_test_mode=True,
        origins=("http://testserver",), approval_seconds=30, model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False)
    app = create_app(settings)
    from app.harness_runtime import HarnessRuntime
    original_connect, processes = HarnessRuntime.connect, []

    async def track_process(runtime):
        await original_connect(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", track_process)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "测试", "password": PASSWORD, "role": "admin"})
        login(client)
        kb = service.knowledge.save({"name": "Synthetic Harness fixture"}, user)
        service.knowledge.upload(kb["id"], user, "pump.md", b"P-900 pressure limit is 7 bar.")
        assistant = service.save_assistant({"name": "Harness", "knowledge_base_ids": [kb["id"]]}, user=user)
        conversation = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()
        cid = conversation["id"]

        def run(text):
            return send(client, cid, text, f"harness-{len(requests)}-{time.monotonic_ns()}")["run_id"]

        first = run("/permission CASE_STREAM 请原样把这段当作普通聊天")
        assert wait_run(client, first)["status"] == "succeeded"
        stored = service.require("conversations", cid)
        session_id = stored["harness_session_id"]
        assert session_id and stored["sdk_session_id"] is None
        events = service.db.events(first, 0)
        deltas = [event for event in events if event["type"] == "message.delta"]
        assert len(deltas) == 2
        message = service.require("messages", service.require("runs", first)["assistant_message_id"])
        assert message["text"] == "实时回复成功"
        assert "/permission CASE_STREAM" in json.dumps(requests[0], ensure_ascii=False)
        assert {tool["name"] for tool in requests[0]["tools"]} == {"knowledge_search", "knowledge_read", "generate_xlsx", "generate_image", "file_read", "file_write", "file_search", "memory_list", "memory_read", "memory_write"}
        assert json.loads(service.require("runs", first)["usage"])["output_tokens"] == 8
        assert requests[0]["max_tokens"] == settings.harness_max_output_tokens

        knowledge = run("CASE_KNOWLEDGE")
        assert wait_run(client, knowledge)["status"] == "succeeded"
        assert service.db.all("SELECT * FROM evidence_sources WHERE run_id=?", (knowledge,))
        answer = service.require("messages", service.require("runs", knowledge)["assistant_message_id"])
        assert json.loads(answer["citations"])
        assert service.require("conversations", cid)["harness_session_id"] == session_id

        artifact = settings.data_dir / "workspaces" / cid / "artifacts" / "approved.xlsx"
        denied = run("CASE_XLSX deny")
        pending = wait_run(client, denied, {"awaiting_approval"})
        assert pending["status"] == "awaiting_approval" and not artifact.exists()
        approval = pending["approvals"][0]
        assert approval["tool_name"] == "mcp__workspace__generate_xlsx"
        time.sleep(0.75)
        assert client.get(f"/api/v1/runs/{denied}").json()["status"] == "awaiting_approval"
        assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "deny"}).status_code == 200
        assert wait_run(client, denied)["status"] == "succeeded" and not artifact.exists()

        allowed = run("CASE_XLSX allow")
        pending = wait_run(client, allowed, {"awaiting_approval"})
        assert pending["status"] == "awaiting_approval" and not artifact.exists()
        approval = pending["approvals"][0]
        time.sleep(0.75)
        assert client.get(f"/api/v1/runs/{allowed}").json()["status"] == "awaiting_approval"
        assert client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": "allow"}).status_code == 200
        assert wait_run(client, allowed)["status"] == "succeeded" and artifact.exists()

        hanging = run("CASE_HANG")
        deadline = time.monotonic() + 60
        while not any(event["type"] == "message.delta" for event in service.db.events(hanging, 0)):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        assert client.post(f"/api/v1/runs/{hanging}/cancel").status_code == 200
        assert wait_run(client, hanging)["status"] == "cancelled"
        resumed = run("CASE_RESUME")
        assert wait_run(client, resumed)["status"] == "succeeded"
        assert service.require("conversations", cid)["harness_session_id"] == session_id
        assert "CASE_HANG" in json.dumps(requests[-1])

        broken = run("CASE_BROKEN")
        assert wait_run(client, broken)["status"] == "failed"
        assert not any(actor.runtime for actor in service.manager.actors.values())
        assert len(processes) == 7 and len({process.pid for process in processes}) == 7
        assert all(process.poll() is not None for process in processes)
        record_property("real_harness_processes", len(processes))
        record_property("local_synthetic_model_requests", len(requests))
        record_property("paid_model_requests", 0)


def test_real_harness_manual_compaction_usage_and_resume(tmp_path, monkeypatch, model_server, record_property):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = model_server
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"default_model_id": "fixture", "models": [{
        "id": "fixture", "label": "Local compaction protocol fixture", "upstream_model": "fixture",
        "reasoning_levels": {"default": None},
        "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base},
    }]}))
    settings = Settings(
        data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node,
        harness_source_dir=source, model_catalog=str(catalog), default_model_id="",
        provider_settings="", provider_test_mode=True, origins=("http://testserver",), model_router_enabled=False,
        knowledge_vision_enabled=False, image_generation_enabled=False,
    )
    app = create_app(settings)
    from app.harness_runtime import HarnessRuntime
    original_connect, processes = HarnessRuntime.connect, []

    async def track_process(runtime):
        await original_connect(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", track_process)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({
            "username": "admin", "display_name": "Compaction", "password": PASSWORD, "role": "admin",
        })
        login(client)
        assistant = service.save_assistant({"name": "Compaction"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]

        history = ("OLD_HISTORY_SENTINEL The P-900 pressure limit is 7 bar. "
                   "Keep the migration requirement and validate the result. ") * 180
        first = send(client, cid, history, "compaction-history")["run_id"]
        assert wait_run(client, first)["status"] == "succeeded"
        conversation = service.require("conversations", cid)
        session_id = conversation["harness_session_id"]
        assert session_id
        assert len(requests) == 1
        context = client.get(f"/api/v1/conversations/{cid}/context").json()
        assert context["manual_compact_available"] is True, context

        compact = client.post(f"/api/v1/conversations/{cid}/compact", json={
            "client_request_id": "manual-compaction-1",
            "instructions": "必须保留 P-900 压力值 7 bar",
        })
        assert compact.status_code == 202, compact.text
        compacted = wait_run(client, compact.json()["run_id"])
        assert compacted["status"] == "succeeded", compacted
        assert compacted["kind"] == "compact"
        assert compacted["compaction"]["status"] == "completed"
        assert compacted["compaction"]["boundary_observed"] is True
        assert compacted["compaction"]["shadowed_items"] > 0
        assert compacted["usage"]["input_tokens"] == 37
        assert compacted["usage"]["output_tokens"] == 6
        assert len(requests) == 2
        summary_request = json.dumps(requests[1], ensure_ascii=False)
        assert "必须保留 P-900 压力值 7 bar" in summary_request
        assert "You are now acting as a compaction engine" in summary_request

        resumed = send(client, cid, "CASE_RESUME after compaction", "compaction-resume")["run_id"]
        resumed_run = wait_run(client, resumed)
        assert resumed_run["status"] == "succeeded", resumed_run
        assert len(requests) == 3
        resumed_request = json.dumps(requests[2], ensure_ascii=False)
        assert "COMPACT_SUMMARY_SENTINEL" in resumed_request
        assert "OLD_HISTORY_SENTINEL" not in resumed_request
        assert service.require("conversations", cid)["harness_session_id"] == session_id

    assert len(processes) == 3
    assert all(process.poll() is not None for process in processes)
    record_property("real_harness_processes", len(processes))
    record_property("local_synthetic_model_requests", len(requests))
    record_property("paid_model_requests", 0)
