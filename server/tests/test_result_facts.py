from app.runtime import result_fact_metadata


def test_result_fact_metadata_projects_allowlisted_structured_scope():
    result = {
        "structuredContent": {
            "business_domain": "库存",
            "business_process": "出库",
            "business_object": "订单",
            "metrics": ["数量"],
            "fields": ["order_id", "status"],
            "filters": {"factory": "F01", "status": "open", "password": "must be redacted"},
            "time_range": "2026-11-01/2026-11-14",
            "aggregation": "count",
            "row_count": 18,
            "page": 1,
            "page_size": 20,
            "cache_hit": True,
            "cache_key": "scope:abc123",
            "api_token": "must be redacted",
            "returned_rows": 18,
            "original_rows": 240,
            "records": [{"secret": "must not be copied"}],
        }
    }
    value = result_fact_metadata(True, result, [{"id": "source_1"}])
    assert value["source_ids"] == ["source_1"]
    facts = value["result_facts"]
    assert facts["fields"] == ["order_id", "status"]
    assert facts["business_domain"] == "库存"
    assert facts["business_process"] == "出库"
    assert facts["business_object"] == "订单"
    assert facts["metrics"] == ["数量"]
    assert facts["filters"] == {"factory": "F01", "status": "open", "password": "redacted"}
    assert facts["row_count"] == 18
    assert facts["cache_hit"] is True
    assert facts["cache_key"].startswith("opaque:")
    assert "api_token" not in facts
    assert facts["filters"]["password"] == "redacted"
    assert "records" not in facts
    assert "secret" not in str(facts)


def test_result_fact_metadata_marks_missing_scope_as_unknown_and_failure_is_empty():
    value = result_fact_metadata(True, {"structuredContent": {"row_count": 0}}, [])
    facts = value["result_facts"]
    assert facts["row_count"] == 0
    assert facts["fields"] == "unknown"
    assert facts["filters"] == "unknown"
    assert facts["cache_hit"] == "unknown"
    assert result_fact_metadata(False, {"structuredContent": {"row_count": 9}}, []) == {
        "source_ids": [], "result_facts": {}
    }


def test_result_fact_metadata_accepts_source_id_projection_from_harness():
    value = result_fact_metadata(True, {"structuredContent": {}}, ["source_1"])
    assert value["source_ids"] == ["source_1"]
    assert value["result_facts"]["source_count"] == 1
