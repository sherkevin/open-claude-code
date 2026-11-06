import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from multimodel_smoke import CONFIGS, StreamObservation, cleanup_resources, resumed_marker
from smoke_api import CheckFailure


def test_main_saves_original_failure_before_failed_cleanup(tmp_path, monkeypatch):
    import multimodel_smoke
    password = tmp_path / "password"
    password.write_text("fixture-password")
    report = tmp_path / "report.json"
    class API:
        def __init__(self, *_):
            pass
        def login(self, *_):
            pass
        def request(self, method, path, **kwargs):
            if path == "/models":
                return [{"id": model, "label": model, "available": True} for model in ["dano-v2.1", "deepseek-v4.1-flash", "glm-5.3"]]
            if method == "POST" and path == "/assistants":
                return {"id": "fixture-assistant"}
            if method == "POST" and path == "/conversations":
                return {"id": "fixture-conversation"}
            if method == "GET" and path.endswith("/messages"):
                raise CheckFailure("primary failure")
            if method == "PATCH":
                assert json.loads(report.read_text())["error"] == "primary failure"
                raise CheckFailure("secondary cleanup failure")
            raise AssertionError((method, path))
        def close(self):
            pass
    monkeypatch.setattr(multimodel_smoke, "API", API)
    monkeypatch.setattr(sys, "argv", ["multimodel_smoke", "--real-model", "--password-file", str(password), "--report", str(report)])
    with pytest.raises(CheckFailure, match="^primary failure$"):
        multimodel_smoke.main()
    saved = json.loads(report.read_text())
    assert saved["status"] == "failed" and saved["error"] == "primary failure"
    assert saved["cleanup"]["errors"][0]["error"] == "secondary cleanup failure"


def test_resume_validates_four_passed_results_without_requiring_old_report_terminal_status():
    labels = {"dano-v2.1": "Dano-v2.1", "deepseek-v4.1-flash": "deepseek-v4.1-flash"}
    marker = "SWITCH-0123456789abcdefabcd"
    source = {"status": "running", "runs": [{"status": "succeeded", "model_id": model, "reasoning_level": level,
        "identity_answer": json.dumps({"marker": marker, "current_public_model": labels[model]})} for model, level, _ in CONFIGS[:4]]}
    assert resumed_marker(source, labels) == marker
    source["runs"][-1]["identity_answer"] = json.dumps({"marker": marker, "current_public_model": "Dano-v2.1"})
    with pytest.raises(CheckFailure, match="identity"):
        resumed_marker(source, labels)


def test_cleanup_waits_for_cancellation_before_archive(monkeypatch):
    calls = []
    statuses = iter(["running", "cancelling", "cancelled"])
    class API:
        def request(self, method, path, **kwargs):
            calls.append((method, path))
            return {"status": next(statuses)} if method == "GET" else {}
    monkeypatch.setattr("multimodel_smoke.time.sleep", lambda _: None)
    result = cleanup_resources(API(), "run", {"id": "chat"}, {"id": "assistant"})
    assert result["active_run_final_status"] == "cancelled" and not result["errors"]
    assert calls[-2:] == [("PATCH", "/conversations/chat"), ("DELETE", "/assistants/assistant")]


def test_cleanup_errors_do_not_mask_failure_or_delete_live_assistant():
    calls = []
    class API:
        def request(self, method, path, **kwargs):
            calls.append((method, path))
            if method == "PATCH":
                raise CheckFailure("archive returned 409")
            return {"status": "cancelled"}
    result = cleanup_resources(API(), "run", {"id": "chat"}, {"id": "assistant"})
    assert result["errors"][0]["operation"] == "archive" and result["retained_resources"]
    assert all(method != "DELETE" for method, _ in calls)


def test_cleanup_timeout_retains_resources_without_archive():
    calls = []
    class API:
        def request(self, method, path, **kwargs):
            calls.append((method, path))
            return {"status": "cancelling"}
    result = cleanup_resources(API(), "run", {"id": "chat"}, {"id": "assistant"}, timeout=0)
    assert result["errors"][0]["operation"] == "cancel_wait"
    assert all(method not in {"PATCH", "DELETE"} for method, _ in calls)


def test_stream_observation_distinguishes_thinking_from_visible_text(tmp_path):
    database = tmp_path / "app.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE raw_events(id INTEGER PRIMARY KEY,run_id TEXT,payload TEXT,created_at TEXT)")
        def put(index, item):
            db.execute("INSERT INTO raw_events VALUES(?,?,?,?)", (index, "run", json.dumps(item), f"2026-11-05T00:00:0{index}+00:00"))
        put(1, {"type": "StreamEvent", "value": {"event": {"type": "message_start"}}})
        put(2, {"type": "StreamEvent", "value": {"event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "do-not-retain"}}}})
        put(3, {"type": "StreamEvent", "value": {"event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "fixture"}}}})
        put(4, {"type": "AssistantMessage", "value": {"model": "glm-5.3"}})
        put(5, {"type": "ResultMessage", "value": {"session_id": "same-native-session"}})
    observer = StreamObservation(database, "run", "2026-11-05T00:00:00+00:00")
    value = observer.update()
    assert value["first_event_elapsed_ms"] == 1000 and value["first_delta_elapsed_ms"] == 2000
    assert value["first_visible_text_elapsed_ms"] == 3000 and value["thinking_delta_events"] == 1
    assert value["observed_upstream_models"] == ["glm-5.3"]
    assert value["observed_session_ids"] == ["same-native-session"]
    assert "do-not-retain" not in json.dumps(value)
    assert observer.update() == value and value["raw_events_seen"] == 5
