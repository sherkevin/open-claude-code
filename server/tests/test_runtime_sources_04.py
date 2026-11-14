import hashlib
import json
import shutil

from app.config import Settings
from app.db import dump, now, uid
from app.runtime import RunManager, SDKRuntime, tool_result_metadata
from app.service import Service


def test_modified_workspace_attachment_cannot_impersonate_original(tmp_path):
    # This test invokes the legacy SDK attachment hook directly.
    service = Service(Settings(data_dir=tmp_path, runtime_type="claude"))
    try:
        user = service.create_user({"username": "source-test", "display_name": "Test", "password": "safe-test-password-123", "role": "admin"})
        assistant = service.save_assistant({"name": "Source test", "connections": []})
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "test"}, user)
        file_id = uid()
        original = tmp_path / "files" / file_id
        original.write_text("Measured count: 42\nVerified", encoding="utf-8")
        service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", (file_id, user["id"], "facts.txt", "text/plain", original.stat().st_size, str(original), now(), None))
        response, _ = service.prepare_run(conversation["id"], {"text": "read", "file_ids": [file_id], "client_request_id": "sources"}, user)
        runtime = SDKRuntime(service, conversation["id"], service.runtime_config(conversation["id"]), None, None)
        runtime.current_run = service.require("runs", response["run_id"])
        copied = tmp_path / "workspaces" / conversation["id"] / "uploads" / f"{file_id}-facts.txt"
        copied.parent.mkdir(parents=True)
        shutil.copyfile(original, copied)
        hook = {"tool_input": {"file_path": str(copied), "offset": 1, "limit": 1}}
        sources = runtime.attachment_source(hook)
        assert len(sources) == 1
        assert sources[0]["excerpt"] == "Measured count: 42"
        assert sources[0]["locator"]["sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
        copied.write_text("Fabricated count: 9000", encoding="utf-8")
        assert runtime.attachment_source(hook) == []
    finally:
        service.db.close()


def test_tool_result_metadata_is_bounded_and_failure_has_no_facts():
    sources = [{"id": "source_1"}, {"id": "bad/id"}, {"id": "source_2"}, {"id": 9}]
    assert tool_result_metadata(True, sources) == {"source_ids": ["source_1", "source_2"], "result_facts": {"source_count": 2}}
    assert tool_result_metadata(False, sources) == {"source_ids": [], "result_facts": {}}


def test_run_finish_emits_canonical_source_replacement_and_total_timing(tmp_path):
    service = Service(Settings(data_dir=tmp_path))
    try:
        user = service.create_user({"username": "finish-test", "display_name": "Test", "password": "safe-test-password-123", "role": "admin"})
        assistant = service.save_assistant({"name": "Source test", "connections": [], "web_search_enabled": True})
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "test"}, user)
        response, _ = service.prepare_run(conversation["id"], {"text": "read", "file_ids": [], "client_request_id": "finish"}, user)
        run = service.require("runs", response["run_id"])
        source = service.evidence.register(run["id"], "web", "Actual source", {"url": "https://example.com", "fetched_at": now()}, "Actual excerpt", resource_type="web", resource_id=assistant["id"], original_url="https://example.com")
        service.db.execute("UPDATE messages SET text=? WHERE id=?", (f"A fact [[source:{source['id']}]]", run["assistant_message_id"]))
        RunManager(service).finish(run["id"], "succeeded")
        message = service.require("messages", run["assistant_message_id"])
        assert "[[source:" not in message["text"]
        assert f"[1](/api/v1/sources/{source['id']})" in message["text"]
        assert json.loads(message["citations"])[0]["id"] == source["id"]
        events = service.db.events(run["id"], 0)
        assert events[-2]["type"] == "message.updated"
        assert events[-1]["type"] == "run.completed" and events[-1]["data"]["timings"]["total_ms"] >= 0
    finally:
        service.db.close()
