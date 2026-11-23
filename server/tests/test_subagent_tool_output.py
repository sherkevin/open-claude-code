from app.workspace_tools import child_tool_result


def test_model_receives_complete_ordered_answers_and_individual_failures():
    first = "预计缺口 7 件。\n\n| 物料 | 余量 |\n|---|---|\n| 甲 | 83 |"
    children = [
        {"child_id": "private-id-a", "status": "succeeded", "result": {"output": first, "stop_reason": "completed"}},
        {"child_id": "private-id-b", "status": "interrupted", "stop_reason": "timeout"},
        {"child_id": "private-id-c", "status": "failed", "stop_reason": "approval_denied"},
        {"child_id": "private-id-d", "status": "succeeded", "result": {"output": "按月对账。", "stop_reason": "completed"}},
    ]
    assert [child_tool_result(child) for child in children] == [
        {"output": first},
        {"status": "interrupted", "reason": "timeout"},
        {"status": "failed", "reason": "approval_denied"},
        {"output": "按月对账。"},
    ]
    assert children[0]["result"]["stop_reason"] == "completed"
