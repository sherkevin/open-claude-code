import copy
import json

import pytest

from app.business_facts import check_answer
from app.business_results import project_business_result, presentation_receipt
from app.runtime import RunManager
from test_tool_constraints import env, prepare


def data():
    return {"title": "合成发货", "columns": [
        {"key": "customer", "label": "客户"},
        {"key": "record_count", "label": "发货记录数", "unit": "笔"},
        {"key": "quantity", "label": "数量", "unit": "件"}],
        "rows": [{"customer": f"合成客户{i+1:02}", "record_count": 10000, "quantity": value}
                 for i, value in enumerate([160000, 210000, 160000, 210000, 260000, 210000, 260000, 210000])],
        "complete": True, "total_rows": 8, "synthetic": True,
        "aggregates": [{"column": "quantity", "label": "数量合计", "unit": "件", "value": 1680000,
                        "claim_labels": ["发货", "发货数量", "发货件数", "数量", "件数"],
                        "operation": "sum", "scope": "returned_rows"}]}


def projection(payload=None):
    return project_business_result({"structuredContent": payload or data()}, ["host_source"])


# Exact bad total and table shape from real-model receipt b79fc0f15a71.
# Dataset is synthetic; source identifiers are deliberately test-local.
BAD_TABLE = """业务日期 2026-11-14，按客户汇总的发货情况如下：

| 客户 | 发货记录数（笔） | 件数（件） |
|---|---|---|
| 合成客户01 | 10,000 | 160,000 |
| 合成客户02 | 10,000 | 210,000 |
| 合成客户03 | 10,000 | 160,000 |
| 合成客户04 | 10,000 | 210,000 |
| 合成客户05 | 10,000 | 260,000 |
| 合成客户06 | 10,000 | 210,000 |
| 合成客户07 | 10,000 | 260,000 |
| 合成客户08 | 10,000 | 210,000 |
| **合计** | **80,000** | **1,890,000** |
"""


def test_declared_sum_is_verified_exactly_without_mutating_source():
    value = data()
    original = copy.deepcopy(value)
    output = projection(value)
    assert value == original
    assert output["host_verified_aggregates"][0]["value"] == "1680000"
    assert "1680000 件" in presentation_receipt({"structuredContent": value})


@pytest.mark.parametrize("change", [
    {"operation": "average"}, {"scope": "full_dataset"}, {"value": 1890000},
    {"unit": "箱"}, {"value": True}, {"value": 10**309}, {"value": float("inf")},
    {"column": {}}, {"label": ""}, {"value": "1680000"},
])
def test_unsupported_or_inconsistent_aggregate_is_never_verified(change):
    value = data()
    value["aggregates"][0].update(change)
    assert "host_verified_aggregates" not in projection(value)


def test_numeric_columns_and_remote_verified_labels_never_authorize_arithmetic():
    value = data()
    value.pop("aggregates")
    value["host_verified_aggregates"] = [{"value": "1"}]
    result = projection(value)
    assert "host_verified_aggregates" not in result
    assert check_answer(BAD_TABLE, [result])["status"] == "not_checked"


def test_decimal_sum_not_binary_float_approximation():
    value = data()
    value["rows"] = [{"quantity": .1}, {"quantity": .2}]
    value["aggregates"][0]["value"] = .3
    assert projection(value)["host_verified_aggregates"][0]["value"] == "0.3"


@pytest.mark.parametrize("answer", [BAD_TABLE, "本次返回行发货合计1,890,000件。", "本次返回的全部8行数量合计189万件。",
    "本次返回行合计168万件。\n\n本次返回行合计189万件。"])
def test_known_conflict_is_rejected_even_if_correct_number_also_present(answer):
    validation = check_answer(answer, [projection()])
    assert validation["status"] == "conflict"
    assert validation["conflicts"][0]["expected"] == "1680000"


def test_correct_total_unknown_language_and_multiple_scopes_have_honest_coverage():
    assert check_answer("本次返回行发货合计168万件。", [projection()])["status"] == "no_detected_conflict"
    assert check_answer("发货合计189万件。", [projection()])["status"] == "not_checked"
    assert check_answer("客户数量见业务卡。", [projection()])["status"] == "not_checked"
    assert check_answer(BAD_TABLE, [projection(), projection()])["status"] == "not_checked"
    assert check_answer("Hello, world", [])["status"] == "not_checked"


def test_unbounded_numeric_prose_does_not_break_terminal_transaction():
    for number in ["1e999999999", "9" * 200]:
        assert check_answer("本次返回行合计" + number + "件。", [projection()])["status"] == "not_checked"


@pytest.mark.parametrize("answer", ["合计不是189万件，而是168万件。", "若合计189万件，需要另行核对。",
    "原回答‘合计189万件’错误。正确合计168万件。", "> 合计189万件。\n\n正确合计168万件。",
    "```\n合计189万件。\n```\n正确合计168万件。"])
def test_quoted_hypothetical_or_corrected_numbers_are_not_asserted_false_totals(answer):
    assert check_answer(answer, [projection()])["status"] != "conflict"


@pytest.mark.parametrize("answer", [
    "发货数量合计168万件，其中客户01发货16万件。",
    "本次发货总计168万件，上次总计189万件。",
    "本次数量合计168万件，损坏1件。",
    "退货总计2000件；发货合计168万件。",
    "|说明|退货数量（件）|\n|---|---|\n|合计|2000|",
    "|说明|数量（件）|\n|---|---|\n|原错误合计|1890000|\n|正确合计|1680000|",
    "以下仅是假设示例，不是本次结果：\n\n|客户|件数（件）|\n|---|---|\n|合计|1890000|",
    "以下是上次的结果：\n\n|客户|数量（件）|\n|---|---|\n|合计|1890000|",
    "以下是假设情况：\n发货合计189万件。",
    "本次发货合计168万件，假设补入一笔后，合计189万件。",
    "客户01的发货情况：\n|项目|件数（件）|\n|---|---|\n|合计|160000|",
    "退货情况如下：\n|项目|件数（件）|\n|---|---|\n|合计|2000|",
    "本次发货合计168万件，合计189万件这一说法不成立。",
])
def test_review_counterexamples_do_not_misclassify_row_values_scopes_or_metrics(answer):
    assert check_answer(answer, [projection()])["status"] != "conflict"


def test_overlapping_currency_aliases_never_cross_validate_metrics():
    raw = {"columns": [{"key": "amount", "label": "订单金额", "unit": "CNY"},
                       {"key": "refund", "label": "退款金额", "unit": "元"}],
           "rows": [{"amount": 100, "refund": 20}],
           "aggregates": [{"column": key, "label": label, "unit": unit, "value": value,
                           "scope": "returned_rows", "operation": "sum"}
                          for key, label, unit, value in [("amount", "订单金额合计", "CNY", 100), ("refund", "退款金额合计", "元", 20)]]}
    assert check_answer("订单金额总计100元；退款金额总计20元。", [projection(raw)])["status"] == "not_checked"


def test_declared_metric_label_total_row_with_unit_is_not_a_header():
    answer = BAD_TABLE.replace("**合计**", "数量合计").replace("**1,890,000**", "189万件")
    assert check_answer(answer, [projection()])["status"] == "conflict"


@pytest.mark.parametrize("answer", [BAD_TABLE.replace("| 合成客户01 | 10,000 | 160,000 |\n", ""),
    BAD_TABLE.replace("合成客户01", "其他客户"), BAD_TABLE.replace("160,000", "150,000", 1),
    "以下是假设情况：\n\n" + BAD_TABLE,
    "本次返回的全部7行合计189万件。"])
def test_incomplete_or_different_membership_and_hypothetical_full_table_are_unverified(answer):
    assert check_answer(answer, [projection()])["status"] == "not_checked"


@pytest.mark.parametrize("revoked", [False, True])
def test_terminal_fact_conflict_preserves_original_and_fails_run_only_with_authorized_facts(env, revoked):
    service = env.service
    run_id = prepare(env)["run_id"]
    run = service.require("runs", run_id)
    raw = {"structuredContent": {**data(), "sources": [{"kind": "data", "title": "Synthetic",
        "locator": {"table": "synthetic"}, "excerpt": "Synthetic only"}]}}
    sources = service.evidence.register_mcp(run_id, env.connection["id"], raw, env.tool)
    ids = [source["id"] for source in sources]
    assert ids
    business = project_business_result(raw, ids)
    service.db.event(run_id, "tool.completed", {"success": True, "source_ids": ids, "business_result": business})
    service.db.execute("UPDATE messages SET text=? WHERE id=?", (BAD_TABLE, run["assistant_message_id"]))
    if revoked:
        service.db.execute("UPDATE connections SET enabled=0 WHERE id=?", (env.connection["id"],))
    RunManager(service).finish(run_id, "succeeded")
    final = service.require("runs", run_id)
    events = service.db.events(run_id, 0)
    checks = [event["data"] for event in events if event["type"] == "business.answer_validation"]
    if revoked:
        assert not checks
        return
    assert final["status"] == "failed" and final["error_code"] == "BUSINESS_FACT_CONFLICT"
    assert checks[0]["original_answer"] == BAD_TABLE
    text = service.require("messages", run["assistant_message_id"])["text"]
    assert "1680000 件" in text and "1,890,000" in text and "未通过业务事实校验" in text
    assert events[-1]["data"]["status"] == "failed"
