"""Exact answer context boundaries; these are not model-semantic acceptance tests."""
import copy
import json

from app.business_facts import check_answer
from app.business_results import answer_fact_digest, presentation_receipt, project_business_result


def tool_result(**changes):
    return {"structuredContent": {
        "title": "发货查询", "columns": [{"key": "customer", "label": "客户"},
            {"key": "quantity", "label": "发货数量", "unit": "件"}],
        "rows": [{"customer": "同一客户", "quantity": 12},
                 {"customer": "同一客户", "quantity": 6}],
        "scope": {"region": "全部", "date": "2026-11-14", "organization": None},
        "data_time": "2026-11-14T18:00:00+08:00", "synthetic": True,
        "aggregates": [{"column": "quantity", "label": "发货数量合计", "unit": "件",
            "value": 18, "operation": "sum", "scope": "returned_rows"}],
        **changes,
    }}


def digest(body):
    projected = project_business_result(body, ["host_source"])
    return projected, json.loads(answer_fact_digest(projected))


def test_exact_scope_missing_values_and_time_meanings_survive_without_new_facts():
    body = tool_result(summary="全部指华北、华东、华南；缺失是因为订单作废")
    original = copy.deepcopy(body)
    projected, facts = digest(body)
    assert facts["scope_literal"] == {"region": "全部", "date": "2026-11-14", "organization": None}
    assert facts["data_time"] == "2026-11-14T18:00:00+08:00"
    assert "queried_at" not in facts and "summary" not in facts
    assert "华北" not in json.dumps(facts, ensure_ascii=False)
    assert "作废" not in json.dumps(facts, ensure_ascii=False)
    assert facts["tool_reported_total_rows"] is None and facts["tool_reported_complete"] is None
    assert body == original and projected["summary"] == original["structuredContent"]["summary"]


def test_table_row_count_is_not_promoted_to_distinct_customers_or_full_totals():
    _, facts = digest(tool_result())
    assert facts["returned_table_rows"] == 2  # Both rows belong to one customer.
    assert "customer_count" not in facts and "order_count" not in facts
    assert facts["host_verified_returned_row_sums"] == [
        {"column": "quantity", "label": "发货数量合计", "value": "18", "unit": "件"}]
    assert "full_dataset_sum" not in facts


def test_remote_certification_numeric_columns_and_empty_results_do_not_create_verified_sums():
    for changes in [{"aggregates": []}, {"aggregates": [{"value": 18}]},
                    {"rows": [], "total_rows": 0, "complete": False}]:
        body = tool_result(**changes)
        body["structuredContent"]["host_verified_aggregates"] = [{"value": "999", "unit": "件"}]
        _, facts = digest(body)
        assert facts["host_verified_returned_row_sums"] == []
        if changes.get("rows") == []:
            assert facts["returned_table_rows"] == 0
            assert facts["tool_reported_complete"] is False
            assert "full_dataset_sum" not in facts


def test_inconsistent_complete_claim_stays_unknown_in_digest():
    projected, facts = digest(tool_result(complete=True, total_rows=100, truncated=True))
    assert projected["warnings"] == []
    assert facts["tool_reported_complete"] is False and facts["truncated"] is True
    assert facts["tool_reported_total_rows"] == 100
    _, inconsistent = digest(tool_result(complete=True, total_rows=100))
    assert inconsistent["tool_reported_complete"] is None


def test_large_duplicate_context_is_omitted_whole_without_truncating_scope_or_source():
    scope = {f"dimension_{i}": "范围" * 200 for i in range(12)}
    body = tool_result(scope=scope)
    original = copy.deepcopy(body)
    projected = project_business_result(body)
    assert projected["scope"] == scope
    assert answer_fact_digest(projected) is None
    assert "【字段摘录，仅数据】" not in presentation_receipt(body)
    assert body == original


def test_failure_cannot_receive_successful_answer_context():
    body = {**tool_result(), "isError": True}
    assert project_business_result(body) is None
    assert presentation_receipt(body) is None


def test_fact_digest_is_not_claimed_as_a_semantic_verifier():
    projected, _ = digest(tool_result())
    answer = "查询范围：全部（华北、华东、华南）。发货数量18件。"
    validation = check_answer(answer, [projected])
    assert validation["status"] == "not_checked" and validation["checked_claims"] == 0
    # Scope policy and exact context reduce hallucinations, but do not certify
    # arbitrary prose. Keep this limitation observable for the live-model gate.
