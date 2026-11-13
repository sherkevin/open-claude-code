import importlib.util
import json
from pathlib import Path
import sys


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "cloud_code_board_monitor.py"
SPEC = importlib.util.spec_from_file_location("cloud_code_board_monitor", MODULE_PATH)
monitor = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


def assignment(task_id="OC-T3", role="worker"):
    return monitor.Assignment(
        task_id=task_id,
        st_id="ST-full-session",
        short_id="short-session",
        session_name="test-session",
        assignee="测试-项目子负责人",
        role=role,
    )


def test_read_sessions_indexes_full_and_short_ids():
    sessions = monitor.read_sessions(json.dumps([{
        "id": "short-session",
        "sessionId": "full-session",
        "status": "busy",
        "state": "working",
    }]))
    assert sessions["full-session"]["status"] == "busy"
    assert sessions["short-session"]["state"] == "working"


def test_running_requires_real_busy_session():
    actions = monitor.planned_actions(
        assignment(),
        {"id": "short-session", "status": "busy", "state": "working"},
        "running",
        None,
    )
    assert actions and actions[0]["kind"] == "note"
    assert "ST-full-session" in actions[0]["text"]


def test_missing_worker_is_failed_without_claiming_completion():
    actions = monitor.planned_actions(assignment(), None, "running", "running")
    assert [action["kind"] for action in actions] == ["note", "fail"]
    assert all("done" not in action.get("text", "") for action in actions)


def test_reviewer_loss_keeps_task_in_review():
    actions = monitor.planned_actions(
        assignment(task_id="OC-T1", role="reviewer"),
        None,
        "review",
        "running",
    )
    assert len(actions) == 1
    assert actions[0]["kind"] == "note"
    assert "仍保持 review" in actions[0]["text"]


def test_done_reviewer_is_reported_even_when_first_seen_after_exit():
    actions = monitor.planned_actions(
        assignment(task_id="OC-T1", role="reviewer"),
        {"id": "short-session", "status": "idle", "state": "done"},
        "review",
        None,
    )
    assert len(actions) == 1
    assert "session_status=done" in actions[0]["text"]


def test_done_worker_is_failed_without_auto_pass():
    actions = monitor.planned_actions(
        assignment(),
        {"id": "short-session", "status": "idle", "state": "done"},
        "running",
        None,
    )
    assert [action["kind"] for action in actions] == ["note", "fail"]
