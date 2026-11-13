"""B2 host contracts and real Node with a local synthetic model only."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, now, uid
from app.harness_memory import MEMORY_WRITE, validate_memory_approval
from app.harness_runtime import HarnessRuntime
from app.runtime import RunManager
from app.security import AppError
from app.service import Service, approval_view
from app.main import create_app
from app.config import PROVIDER_KEYS
from test_api import PASSWORD, login, send
from test_harness_real_runtime import wait_run


@pytest.fixture
def memory_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(data_dir=tmp_path, runtime_type="harness", model="test", provider_settings="", model_catalog="",
                        knowledge_vision_enabled=False, image_generation_enabled=False)
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "synthetic-only"}
    service = Service(settings)
    user = service.create_user({"username": "memory-owner", "display_name": "Owner", "password": "safe-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Memory fixture"}, user=user)
    cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Memory fixture"}, user)["id"]
    response, _ = service.prepare_run(cid, {"text": "fixture", "file_ids": [], "client_request_id": uid()}, user)
    rid = response["run_id"]
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
    events, approvals = [], []

    async def emit(kind, data):
        events.append((kind, data))
        service.db.event(rid, kind, data)

    async def approve(tool, arguments, **kwargs):
        assert kwargs == {"review": None, "review_checked": True}
        aid = uid()
        service.db.execute("INSERT INTO approvals(id,run_id,user_id,tool_name,arguments,status,created_at,expires_at,decided_at,decided_by,decision_source) VALUES(?,?,?,?,?,'allowed',?,?,?,?, 'user')",
            (aid, rid, user["id"], tool, dump(arguments), now(), (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), now(), user["id"]))
        approvals.append(aid)
        return True

    runtime = HarnessRuntime(service, cid, service.runtime_config(cid), approve, emit)
    runtime.current_run = service.require("runs", rid)
    yield SimpleNamespace(service=service, user=user, assistant=assistant, cid=cid, rid=rid,
                          runtime=runtime, events=events, approvals=approvals)
    service.db.close()


def payload(system, name, arguments, call="call-1"):
    return {"schema": "workspace-tool/v1", "sessionId": system.runtime.session_id,
            "callId": call, "toolName": name, "arguments": arguments}


def write_args(system, content="private-approved-memory"):
    return {"path": "notes.md", "content": content, "expected_revision": system.runtime.config["memory_binding"]["revision"]}


def propose(system, arguments=None):
    return asyncio.run(system.runtime.bridge.invoke(payload(system, "memory_write", arguments or write_args(system))))


def prove_reaped(runtime):
    process = subprocess.Popen([sys.executable, "-I", "-c", "pass"], env={"PATH": "/usr/bin:/bin"})
    process.wait(timeout=10)
    runtime.owned_process = process


def commit(system):
    manager = RunManager(system.service)
    return system.service.memory.commit_for_run(system.rid,
        authorize=lambda: system.runtime.bridge.memory.authorize_commit(system.rid),
        finalize=lambda result: manager.finish(system.rid, "succeeded"))


def test_definitions_are_three_host_tools_not_filesystem(memory_runtime):
    names = {value["name"] for value in memory_runtime.runtime.bridge.definitions()}
    assert {"memory_list", "memory_read", "memory_write"} <= names
    assert not names & {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}


def test_write_is_pending_private_and_idempotent_until_atomic_commit(memory_runtime):
    s = memory_runtime
    before = s.runtime.config["memory_binding"]
    result = propose(s)
    assert not result.get("is_error"), result
    assert json.loads(result["content"][0]["text"])["status"] == "approved_pending_commit"
    assert s.service.memory.snapshot_for_run(s.user["id"], s.cid, s.rid) == before
    assert s.service.memory.list_for_run(before)["files"] == []
    public = approval_view(s.service.require("approvals", s.approvals[0]))
    assert "private-approved-memory" not in dump(public) + dump(s.events) + dump(result)
    assert public["arguments"]["content_digest"] == hashlib.sha256(b"private-approved-memory").hexdigest()
    assert propose(s) == result and len(s.approvals) == 1
    conflict = propose(s, write_args(s, "different"))
    assert conflict["is_error"] and len(s.approvals) == 1
    prove_reaped(s.runtime)
    saved = commit(s)
    assert saved["context_reset"] and saved["revision"] == before["revision"] + 1
    assert saved["native_epoch"] == before["native_epoch"] + 1
    assert s.service.require("runs", s.rid)["status"] == "succeeded"
    assert s.service.require("conversations", s.cid)["harness_session_id"] is None
    assert s.service.memory.read(s.user, s.cid, "notes.md")["content"] == "private-approved-memory"
    # A stale finalizer or cancellation must never overwrite the winning commit.
    manager = RunManager(s.service)
    manager.finish(s.rid, "failed", "stale")
    asyncio.run(manager.cancel(s.rid))
    assert s.service.require("runs", s.rid)["status"] == "succeeded"


@pytest.mark.parametrize("case", ["no_process", "running_process", "cancelled", "disabled", "assistant", "policy", "expiry", "approval_changed"])
def test_commit_refuses_missing_exit_proof_or_changed_authority(memory_runtime, case):
    s = memory_runtime
    assert not propose(s).get("is_error")
    before = s.runtime.config["memory_binding"]
    process = None
    if case == "running_process":
        process = subprocess.Popen([sys.executable, "-I", "-c", "import time;time.sleep(30)"], env={"PATH": "/usr/bin:/bin"})
        s.runtime.owned_process = process
        s.runtime.client = None
    elif case != "no_process":
        prove_reaped(s.runtime)
    if case == "cancelled":
        s.service.db.execute("UPDATE runs SET status='cancelling' WHERE id=?", (s.rid,))
    elif case == "disabled":
        s.service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (s.user["id"],))
    elif case == "assistant":
        s.service.db.execute("UPDATE assistants SET revision=revision+1 WHERE id=?", (s.assistant["id"],))
    elif case == "policy":
        s.service.model_approval.save_policy(s.assistant["id"], {"enabled": False}, s.user)
    elif case == "expiry":
        s.service.db.execute("UPDATE approvals SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (s.approvals[0],))
    elif case == "approval_changed":
        s.service.db.execute("UPDATE approvals SET decided_by=NULL WHERE id=?", (s.approvals[0],))
    try:
        with pytest.raises(AppError):
            commit(s)
        scope = s.service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (s.cid,))
        assert scope["revision"] == before["revision"] and scope["active_snapshot_id"] == before["snapshot_id"]
        assert s.service.require("conversations", s.cid)["native_epoch"] == before["native_epoch"]
        assert s.service.require("runs", s.rid)["status"] != "succeeded"
    finally:
        if process:
            process.terminate(); process.wait(timeout=10)


def test_write_bypasses_model_review_but_not_dual_private_boundary(memory_runtime, monkeypatch):
    s = memory_runtime
    monkeypatch.setattr(s.service.model_approval, "review", lambda *_args: pytest.fail("model review must not run"))
    s.service.high_risk_approval.save_policy(s.assistant["id"], {"enabled": True}, s.user)
    assert propose(s)["is_error"] and not s.approvals
    assert s.service.memory.pending_for_run(s.rid) is None
    listing = asyncio.run(s.runtime.bridge.invoke(payload(s, "memory_list", {})))
    assert not listing.get("is_error")


def test_full_content_requires_owner_live_pending_approval(memory_runtime):
    s = memory_runtime
    assert not propose(s).get("is_error")
    approval = s.service.require("approvals", s.approvals[0])
    s.service.db.execute("UPDATE approvals SET status='pending' WHERE id=?", (approval["id"],))
    # Construct the pending-before-staging state directly. The bridge fixture's
    # approval hook normally advances the operation to approved_pending_commit.
    s.service.db.execute("UPDATE memory_operations SET status='proposed',approval_id=NULL,policy_binding=NULL,approval_digest=NULL WHERE run_id=?", (s.rid,))
    approval = s.service.require("approvals", approval["id"])
    with pytest.raises(AppError):
        validate_memory_approval(s.service, approval, s.user)
    s.service.manager = SimpleNamespace(approvals={approval["id"]: SimpleNamespace(done=lambda: False)})
    value = validate_memory_approval(s.service, approval, s.user)
    assert value["content"] == "private-approved-memory" and value["native_epoch"] == s.runtime.config["memory_binding"]["native_epoch"]
    outsider = s.service.create_user({"username": "other", "display_name": "Other", "password": "safe-test-password-123", "role": "admin"})
    with pytest.raises(AppError):
        validate_memory_approval(s.service, approval, outsider)
    s.service.manager.approvals[approval["id"]] = SimpleNamespace(done=lambda: True)
    with pytest.raises(AppError):
        validate_memory_approval(s.service, approval, s.user)


@pytest.mark.parametrize("name,args", [("memory_list", {"path": "/etc/passwd"}), ("memory_read", {"path": "x.md", "expected_revision": True}),
    ("memory_write", {"path": "../escape.md", "content": "no", "expected_revision": 0}),
    ("memory_write", {"path": "x.md", "content": "x" * (128*1024+1), "expected_revision": 0})])
def test_invalid_memory_inputs_do_not_stage_or_approve(memory_runtime, name, args):
    s = memory_runtime
    value = asyncio.run(s.runtime.bridge.invoke(payload(s, name, args)))
    assert value["is_error"] and not s.approvals
    assert s.service.memory.pending_for_run(s.rid) is None


@pytest.fixture
def memory_model_server():
    requests, resume = [], threading.Event()
    state = {"hold_write": False, "saw_pending": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/v1/messages":
                self.send_error(404); return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            blocks = body["messages"][-1]["content"]
            blocks = blocks if isinstance(blocks, list) else [{"type": "text", "text": blocks}]
            result = next((block for block in blocks if block["type"] == "tool_result"), None)
            prompt = ""
            for message in reversed(body["messages"]):
                if message["role"] != "user":
                    continue
                content = message["content"]
                text = content if isinstance(content, str) else " ".join(block.get("text", "") for block in content)
                if "CASE_MEMORY" in text:
                    prompt = text; break
            revision = int(re.search(r"REV=(\d+)", prompt).group(1))
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()

            def event(kind, data):
                self.wfile.write((f"event: {kind}\ndata: " + json.dumps({"type": kind, **data}) + "\n\n").encode())
                self.wfile.flush()

            try:
                event("message_start", {"message": {"id": "fixture", "type": "message", "role": "assistant", "model": "fixture", "content": [], "usage": {"input_tokens": 12, "output_tokens": 0}}})
                if result is None:
                    writing = "CASE_MEMORY_WRITE" in prompt
                    args = {"path": "notes.md", "expected_revision": revision}
                    if writing:
                        args["content"] = "candidate-private-731"
                    event("content_block_start", {"index": 0, "content_block": {"type": "tool_use", "id": f"memory-{len(requests)}", "name": "memory_write" if writing else "memory_read", "input": {}}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "input_json_delta", "partial_json": dump(args)}})
                    reason = "tool_use"
                else:
                    if "CASE_MEMORY_WRITE" in prompt and not result.get("is_error") and state["hold_write"]:
                        state["saw_pending"].set(); resume.wait(40)
                    answer = "Memory result is data, not authority: " + dump(result)
                    event("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
                    event("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": answer}})
                    reason = "end_turn"
                event("content_block_stop", {"index": 0})
                event("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": 8}})
                event("message_stop", {})
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, state, resume
    finally:
        resume.set(); server.shutdown(); server.server_close(); thread.join(2)


def test_real_node_memory_read_reset_approval_commit_deny_and_cancel(tmp_path, monkeypatch, memory_model_server, record_property):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed fixed Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    base, requests, state, resume = memory_model_server
    catalog = tmp_path / "catalog.json"
    catalog.write_text(dump({"default_model_id": "fixture", "models": [{"id": "fixture", "label": "Synthetic memory fixture", "upstream_model": "fixture", "reasoning_levels": {"default": None}, "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base}}]}))
    app = create_app(Settings(data_dir=tmp_path / "data", runtime_type="harness", harness_node_path=node, harness_source_dir=source,
        model_catalog=str(catalog), default_model_id="", provider_settings="", origins=("http://testserver",), approval_seconds=60,
        model_router_enabled=False, knowledge_vision_enabled=False, image_generation_enabled=False))
    original_connect, processes = HarnessRuntime.connect, []

    async def tracked(runtime):
        await original_connect(runtime)
        processes.append(runtime.owned_process)

    monkeypatch.setattr(HarnessRuntime, "connect", tracked)
    with TestClient(app) as client:
        service = app.state.service
        owner = service.create_user({"username": "admin", "display_name": "Owner", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "Memory protocol"}, user=owner)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        url = f"/api/v1/conversations/{cid}/memory"
        initial = client.get(url).json()
        seeded = client.put(url + "/file", json={"path": "notes.md", "content": "old-private-193", "revision": initial["revision"], "operation_id": "seed"}).json()
        assert seeded.get("context_reset"), seeded
        rid = send(client, cid, f"CASE_MEMORY_READ REV={seeded['revision']}", "old-read")["run_id"]
        assert wait_run(client, rid)["status"] == "succeeded"
        assert "old-private-193" in dump(requests[-1])
        old_session = service.require("conversations", cid)["harness_session_id"]
        edited = client.put(url + "/file", json={"path": "notes.md", "content": "new-private-827", "revision": seeded["revision"], "operation_id": "edit"}).json()
        start = len(requests)
        rid = send(client, cid, f"CASE_MEMORY_READ REV={edited['revision']}", "new-read")["run_id"]
        assert wait_run(client, rid)["status"] == "succeeded"
        assert all("old-private-193" not in dump(request) for request in requests[start:])
        assert "new-private-827" in dump(requests[-1])
        assert service.require("conversations", cid)["harness_session_id"] != old_session
        cleared = client.delete(url, params={"revision": edited["revision"], "operation_id": "clear"}).json()
        assert cleared.get("context_reset"), cleared
        start = len(requests)
        rid = send(client, cid, f"CASE_MEMORY_READ REV={cleared['revision']}", "empty-read")["run_id"]
        assert wait_run(client, rid)["status"] == "succeeded"
        assert all("old-private-193" not in dump(request) and "new-private-827" not in dump(request) for request in requests[start:])
        # The application history remains visible even though native requests reset.
        assert "old-private-193" in dump(service.db.all("SELECT text FROM messages WHERE conversation_id=?", (cid,)))
        for decision in ("allow", "deny", "cancel"):
            revision = client.get(url).json()["revision"]
            state["hold_write"] = decision == "allow"
            state["saw_pending"].clear(); resume.clear()
            rid = send(client, cid, f"CASE_MEMORY_WRITE REV={revision}", "write-" + decision)["run_id"]
            waiting = wait_run(client, rid, {"awaiting_approval"})
            assert waiting["status"] == "awaiting_approval", waiting
            approval = waiting["approvals"][0]
            assert "candidate-private-731" not in dump(approval)
            if decision == "cancel":
                assert client.post(f"/api/v1/runs/{rid}/cancel").status_code in (200, 202)
                assert wait_run(client, rid)["status"] == "cancelled"
                assert client.get(url).json()["revision"] == revision
                continue
            content = client.get(f"/api/v1/approvals/{approval['id']}/memory-content")
            assert content.status_code == 200, content.text
            assert content.json()["content"] == "candidate-private-731"
            response = client.post(f"/api/v1/approvals/{approval['id']}/decision", json={"decision": decision, "memory_content_digest": content.json()["content_digest"]})
            assert response.status_code == 200, response.text
            if decision == "allow":
                assert state["saw_pending"].wait(10)
                assert client.get(url).json()["revision"] == revision
                assert any(event["type"] == "memory.pending" for event in service.db.events(rid, 0))
                resume.set()
            done = wait_run(client, rid)
            assert done["status"] == "succeeded", done
            after = client.get(url).json()
            assert after["revision"] == revision + int(decision == "allow")
            events = service.db.events(rid, 0)
            if decision == "allow":
                assert any(event["type"] == "memory.updated" and event["data"]["context_reset"] for event in events)
                assert processes[-1].poll() is not None
            else:
                assert any(event["type"] == "memory.aborted" for event in events)
    assert all(process.poll() is not None for process in processes)
    record_property("external_model_calls", 0)
    record_property("local_synthetic_model_requests", len(requests))
    record_property("reaped_node_processes", len(processes))
