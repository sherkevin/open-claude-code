import pytest

from app.business_results import oversized_only_reply
from app.runtime import RunManager
from test_tool_constraints import env, prepare


def failure(**changes):
    return {"type": "tool.completed", "data": {
        "tool_name": "mcp__synthetic__oversized_order_rows", "success": False,
        "error_code": "MCP_RESULT_TOO_LARGE", **changes,
    }}


@pytest.mark.parametrize("events", [[], [failure(success=True)], [failure(success=None)],
    [failure(error_code="MCP_TIMEOUT")], [failure(tool_name="web_search")],
    [failure(), {"type": "tool.completed", "data": {"success": True, "tool_name": "web_search"}}],
    [failure(), failure(error_code="AUTH_REQUIRED")]])
def test_other_or_mixed_outcomes_are_not_rewritten(events):
    assert oversized_only_reply(events) is None


@pytest.mark.parametrize("terminal", ["succeeded", "cancelled", "failed"])
def test_terminal_oversize_preserves_original_and_does_not_override_cancellation(env, terminal):
    service = env.service
    run_id = prepare(env)["run_id"]
    run = service.require("runs", run_id)
    original = "可以扩大返回范围，金额为零。"
    service.db.execute("UPDATE messages SET text=? WHERE id=?", (original, run["assistant_message_id"]))
    service.db.event(run_id, "tool.completed", failure()["data"])
    RunManager(service).finish(run_id, terminal)
    final = service.require("runs", run_id)
    answer = service.require("messages", run["assistant_message_id"])["text"]
    corrections = [event for event in service.db.events(run_id, 0) if event["type"] == "business.failure_answer"]
    if terminal != "succeeded":
        assert final["status"] == terminal
        assert not corrections
        return
    assert final["status"] == "failed"
    assert final["error_code"] == "MCP_RESULT_TOO_LARGE"
    assert "未取得可用业务结果" in answer
    assert "扩大返回范围" not in answer and "金额为零" not in answer
    assert corrections[0]["data"]["original_answer"] == original
    assert corrections[0]["data"]["replacement"] == oversized_only_reply([failure()])
    events = service.db.events(run_id, 0)
    assert events[-1]["type"] == "run.completed"
    assert events[-1]["data"]["status"] == "failed"


def test_multiple_oversized_calls_have_one_consistent_reply():
    assert oversized_only_reply([failure(), failure()]) == oversized_only_reply([failure()])
