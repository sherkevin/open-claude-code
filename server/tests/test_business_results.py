import asyncio
import copy
import json

import pytest

from app.business_results import project_business_result
from app.mcp_tools import (MAX_MODEL_RESULT_BYTES, clean_mcp_value,
                           enforce_model_result_budget, include_structured_text)
from app.runtime import result_fact_metadata
from app.security import AppError
from test_harness_mcp_tools import (SYNTHETIC_CREDENTIAL, completed, mcp_server,
                                   request, system)  # noqa: F401


def payload(**changes):
    return {"title": "发货统计", "columns": [
        {"key": "customer", "label": "客户", "type": "string"},
        {"key": "quantity", "label": "发货数", "type": "number", "unit": "件"}],
        "rows": [{"customer": "合成客户甲", "quantity": 18}],
        "scope": {"timezone": "Asia/Shanghai", "date": "2026-11-14"},
        "data_time": "2026-11-14T18:00:00+08:00", "synthetic": True,
        "total_rows": 1, "complete": True, **changes}


def envelope(value):
    return {"content": [], "structuredContent": value}


def test_projection_preserves_values_units_scope_and_host_sources():
    result = project_business_result(envelope(payload(source_ids=["remote-untrusted"])), ["host_source"])
    assert result["schema_version"] == "business-result/v1"
    assert result["rows"] == payload()["rows"]
    assert result["columns"][1]["unit"] == "件"
    assert result["scope"] == payload()["scope"]
    assert result["data_time"] == payload()["data_time"]
    assert result["source_ids"] == ["host_source"]
    assert result["synthetic"] is True and result["complete"] is True
    assert result["returned_rows"] == 1 and result["total_rows"] == 1


def test_unknown_coverage_is_not_inferred_and_explicit_zero_survives():
    result = project_business_result(envelope({"columns": [{"key": "count", "label": "数"}], "rows": [{"count": 0}]}))
    assert result["total_rows"] is None and result["complete"] is None
    assert result["synthetic"] is None and result["data_time"] is None
    assert result["rows"] == [{"count": 0}]
    empty = project_business_result(envelope(payload(rows=[], total_rows=0)))
    assert empty["rows"] == [] and empty["total_rows"] == 0


@pytest.mark.parametrize("value", [
    {"content": [{"type": "text", "text": json.dumps(payload())}]},
    envelope({"summary": "total 42"}), envelope({"rows": [1, 2]}),
    envelope({"rows": []}), envelope({"rows": [{"value": {"nested": 1}}]}),
    envelope({"rows": [{"value": float("inf")}]}),
    envelope({"rows": [{"value": 9_007_199_254_740_992}]}),
    envelope({"rows": [{"value": 10**309}]}),
    envelope({"rows": [{"value": "\ud800"}]}),
    envelope(payload(scope={"region": "\ud800"})),
    envelope(payload(summary="\ud800")),
    envelope({"rows": [{"__proto__": "unsafe"}]}),
    envelope(payload(columns=[{"key": "x", "label": "X"}, {"key": "x", "label": "X"}])),
    envelope(payload(scope={"unexpected": [1, 2]})),
    {**envelope(payload()), "isError": True},
])
def test_only_explicit_bounded_scalar_tables_are_projected(value):
    assert project_business_result(value) is None


def test_malformed_optional_column_type_does_not_crash_or_invent_a_type():
    result = project_business_result(envelope({"columns": [{"key": "x", "label": "X", "type": {}}], "rows": [{"x": 1}]}))
    assert result["columns"] == [{"key": "x", "label": "X"}]


@pytest.mark.parametrize("container", ["records", "table"])
def test_named_table_containers_are_supported_without_invented_metadata(container):
    result = project_business_result(envelope({container: [{"actual_field": 5}]}))
    assert result["columns"] == [{"key": "actual_field", "label": "actual_field"}]
    assert result["rows"] == [{"actual_field": 5}]
    assert result["complete"] is None


def test_conflicting_completeness_is_visible_and_never_calculates_total():
    result = project_business_result(envelope(payload(total_rows=99)))
    assert result["complete"] is None and result["total_rows"] == 99
    assert result["warnings"] and result["rows"][0]["quantity"] == 18
    truncated = project_business_result(envelope(payload(truncated=True)))
    assert truncated["complete"] is False and truncated["truncated"] is True


def test_actual_transport_credential_scrub_precedes_table_projection():
    clean = clean_mcp_value(envelope(payload(rows=[{"customer": SYNTHETIC_CREDENTIAL, "quantity": 18}])), SYNTHETIC_CREDENTIAL)
    result = project_business_result(clean)
    assert SYNTHETIC_CREDENTIAL not in json.dumps(result)
    assert result["rows"][0]["customer"] == "[redacted]"


def test_failed_tool_cannot_publish_stale_business_table():
    assert "business_result" not in result_fact_metadata(False, envelope(payload()), ["source_1"])


@pytest.mark.parametrize("structured", [
    {"rows": [{"id": i} for i in range(201)]},
    {"rows": [{f"key{i}": i for i in range(25)}]},
    {"rows": [{"text": "中" * 12000}]},
])
def test_model_budget_rejects_whole_oversize_result_without_mutating(structured):
    body = include_structured_text(envelope(structured))
    original = copy.deepcopy(body)
    with pytest.raises(AppError) as exc:
        enforce_model_result_budget(body)
    assert exc.value.code == "MCP_RESULT_TOO_LARGE"
    assert body == original


def test_utf8_budget_and_exact_duplicate_representation():
    body = {"content": [{"type": "text", "text": "中" * (MAX_MODEL_RESULT_BYTES // 3)}]}
    assert enforce_model_result_budget(body) <= MAX_MODEL_RESULT_BYTES
    body["content"][0]["text"] += "中"
    with pytest.raises(AppError):
        enforce_model_result_budget(body)
    structured = payload()
    blocks = [{"type": "text", "text": json.dumps(structured)}] * 2
    deduplicated = include_structured_text({"content": blocks, "structuredContent": structured})
    assert len(deduplicated["content"]) == 1
    assert deduplicated["structuredContent"]["rows"] == structured["rows"]


def test_real_http_success_persists_business_result_and_replay(system):
    system.server.structured_override = payload(
        sources=[{"kind": "data", "title": "合成发货记录", "locator": {"table": "synthetic_shipments"}, "excerpt": "18 件"}])
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    event = completed(system)
    assert not result.get("is_error") and event["success"] is True
    assert event["business_result"]["rows"] == payload()["rows"]
    assert event["business_result"]["synthetic"] is True
    assert event["model_input_bytes"] == len("\n".join(block["text"] for block in result["content"]).encode())
    assert event["business_result"]["source_ids"] == event["source_ids"]
    replay = [item for item in system.service.public_events(system.actor.current_run_id, 0) if item["type"] == "tool.completed"]
    assert replay[-1]["data"]["business_result"] == event["business_result"]


def test_real_http_oversize_is_failure_before_sources_or_display(system):
    system.server.structured_override = payload(rows=[{"customer": str(i), "quantity": 1} for i in range(201)], total_rows=201)
    result = asyncio.run(system.runtime.bridge.invoke(request(system)))
    event = completed(system)
    assert result["is_error"] is True and event["success"] is False
    assert event["error_code"] == "MCP_RESULT_TOO_LARGE"
    assert "business_result" not in event and event["source_ids"] == []
    assert len(system.server.calls) == 1
    assert not system.service.evidence.list(system.actor.current_run_id, system.owner)
