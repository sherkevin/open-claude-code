"""Independent checks for the synthetic HW-029a query-contract fixture.

The fixture is a contract probe, not product support. These tests validate the
fixture's own shape and arithmetic, and characterize the fixed-base adapter's
current inability to preserve native pagination, total basis and dataset
identity. They never call a customer MCP server or a real dataset.

The author did not execute this module; the lead runs the independent command.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "huawi-parity-query-contract"
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.business_results import project_business_result  # noqa: E402


def _load_verifier():
    path = FIXTURE / "verify_fixture.py"
    spec = importlib.util.spec_from_file_location("huawi_query_contract_verify", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load fixture verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = _load_verifier()


@pytest.fixture(scope="module")
def fixture():
    return VERIFY.load_fixture(FIXTURE)


def cases(fixture, name):
    return fixture["cases"][name]["cases"]


def all_cases(fixture):
    for group in fixture["cases"].values():
        yield from group["cases"]


def test_manifest_declares_synthetic_scope_and_no_product_support(fixture):
    manifest = fixture["manifest"]
    assert manifest["fixture_id"] == "huawi-parity-query-contract"
    assert manifest["synthetic_only"] is True
    assert manifest["customer_data"] is False
    assert manifest["customer_schema_inferred"] is False
    assert manifest["product_support_claimed"] is False
    assert manifest["tests_executed_by_author"] is False
    assert manifest["base_commit"] == "2c330bfd9a05bce9ca16b01fa46ba31af2c8da7a"
    assert manifest["non_claims"]
    assert "verify_fixture.py" in json.dumps(manifest)


def test_observed_shape_and_hypotheses_are_separated(fixture):
    observed = fixture["observed"]
    hypotheses = fixture["hypotheses"]
    assert "observed" in observed["status"]
    observed_names = {item["name"] for item in observed["fields"]}
    assert {"dataset", "columns", "data", "rows", "page", "has_more", "total"} <= observed_names
    assert observed["semantic_constraints_observed"]
    assert observed["explicit_non_claims"]
    assert hypotheses["not_customer_input_schema"] is True
    assert hypotheses["synthetic_only"] is True
    assert all(item["evidence_status"] != "observed" for item in hypotheses["hypotheses"])
    assert any("not observed" in item["evidence_status"] for item in hypotheses["hypotheses"])


def test_fixture_shape_and_ground_truth_verify_without_product_runtime():
    report = VERIFY.run(FIXTURE)
    assert report["ok"], "\n".join(report["errors"])
    assert report["product_runtime_imported"] is False
    assert report["network_used"] is False
    assert report["case_count"] == 81
    assert report["counts"]["cases/malformed.json"] == 26
    truth = report["ground_truth"]
    assert truth["row_count"] == 9
    assert truth["sum_amount_cents"] == 82500
    assert truth["sum_quantity"] == 58
    assert truth["distinct_region_code"] == 3
    assert truth["distinct_product_code"] == 3


def test_ground_truth_is_recomputed_directly_from_synthetic_rows(fixture):
    rows = fixture["dataset"]["rows"]
    assert len(rows) == 9
    assert sum(row["amount_cents"] for row in rows) == 82500
    assert sum(row["quantity"] for row in rows) == 58
    assert {row["region_code"] for row in rows} == {"SYNTH-R1", "SYNTH-R2", "SYNTH-R3"}
    assert {row["product_code"] for row in rows} == {"SYNTH-P1", "SYNTH-P2", "SYNTH-P3"}
    region_counts = {}
    region_amounts = {}
    for row in rows:
        region_counts[row["region_code"]] = region_counts.get(row["region_code"], 0) + 1
        region_amounts[row["region_code"]] = region_amounts.get(row["region_code"], 0) + row["amount_cents"]
    assert region_counts == {"SYNTH-R1": 4, "SYNTH-R2": 3, "SYNTH-R3": 2}
    assert region_amounts == {"SYNTH-R1": 31000, "SYNTH-R2": 24500, "SYNTH-R3": 27000}
    assert sum(region_counts.values()) == 9
    assert sum(region_amounts.values()) == 82500


def test_describe_search_and_exact_code_are_distinct_capabilities(fixture):
    describe = cases(fixture, "cases/describe.json")
    search = [case for case in describe if case["request"]["operation"] == "describe_search"]
    detail = [case for case in describe if case["request"]["operation"] == "describe_detail"]
    assert search and detail
    assert all(case["expected"]["detail_available"] is False for case in search)
    assert all(case["expected"]["fields_available"] is False for case in search)
    exact_primary = next(case for case in detail if case["request"]["dataset"] == "SYNTH-SALES-V1")
    assert exact_primary["expected"]["classification"] == "detail"
    assert exact_primary["expected"]["detail_available"] is True
    assert exact_primary["expected"]["field_names_are_labels"] is False
    assert exact_primary["expected"]["detail_field_names"] == [
        "order_code", "region_code", "product_code", "currency_code",
        "quantity", "amount_cents", "status_code",
    ]
    label_case = next(case for case in detail if case["request"]["dataset"] == "合成销售明细（测试夹具）")
    assert label_case["expected"]["classification"] == "detail_rejected"
    assert label_case["expected"]["detail_available"] is False
    search_with_fields = next(case for case in search if case["observation"].get("fields_included") is True)
    assert search_with_fields["expected"]["usable_as_detail"] is False


def test_fields_filters_ordering_and_pagination_cases_are_present(fixture):
    capabilities = {case["capability"] for case in all_cases(fixture)}
    assert {"fields", "filters", "ordering", "finite_pagination",
            "ambiguous_total_and_page_size", "incomplete_groups"} <= capabilities
    fields = cases(fixture, "cases/query_fields_filters_order.json")
    unpublished = next(case for case in fields if case["case_id"] == "query.fields.select_unpublished_field_rejected")
    assert unpublished["request"]["fields"] == ["internal_note"]
    assert unpublished["expected"]["classification"] == "rejected"
    star = next(case for case in fields if case["case_id"] == "query.fields.star_without_count_rejected")
    assert star["request"]["fields"] == ["*"]
    assert star["expected"]["reason"] == "star_only_for_count"
    label = next(case for case in fields if case["case_id"] == "query.fields.label_is_not_field_name")
    assert label["request"]["fields"] == ["订单编号"]
    assert label["expected"]["reason"] == "label_not_field_name"


def test_count_and_distinct_are_known_safe_but_sum_and_grouped_are_not(fixture):
    aggregate_cases = cases(fixture, "cases/query_aggregates.json")
    for case in aggregate_cases:
        request = case["request"]
        expected = case["expected"]
        if expected["classification"] != "valid_aggregate":
            continue
        if request["operation"] in {"count", "count_distinct"}:
            assert expected["contract_status"] == "observed_safe_ungrouped"
            assert expected.get("product_claim_allowed") is not False
        elif request["operation"] == "sum":
            assert expected["contract_status"] == "requires_separate_proof"
            assert expected["product_claim_allowed"] is False
    grouped = [case for case in aggregate_cases if case["request"]["operation"] == "group"
               and case["expected"]["classification"] == "valid_table"]
    assert grouped
    assert all(case["expected"]["contract_status"] == "requires_separate_proof" for case in grouped)
    assert all(case["expected"]["product_claim_allowed"] is False for case in grouped)


def test_pagination_and_ambiguous_total_cases_are_explicit(fixture):
    pagination = cases(fixture, "cases/query_pagination.json")
    short_page = next(case for case in pagination if case["case_id"] == "query.pagination.page3_size4_short_last")
    assert short_page["reply"]["rows"] == 4
    assert len(short_page["reply"]["data"]) == 1
    assert short_page["expected"]["returned_rows"] == 1
    assert short_page["expected"]["rows_field_is_page_size"] is True
    beyond = next(case for case in pagination if case["case_id"] == "query.pagination.page4_beyond_last_empty")
    assert beyond["reply"]["data"] == []
    assert beyond["reply"]["total"] == 9
    assert beyond["expected"]["zero_kind"] == "beyond_last_page_not_dataset_zero"
    absent = next(case for case in pagination if case["case_id"] == "query.pagination.page1_total_absent_unknown")
    assert "total" not in absent["reply"]
    assert absent["expected"]["total"] is None
    assert absent["expected"]["total_status"] == "unknown_not_zero"
    reasons = {case["expected"].get("reason") for case in pagination
               if case["expected"]["classification"] == "inconsistent"}
    assert {"has_more_contradicts_total", "missing_pages_but_has_more_false",
            "total_less_than_returned", "total_equals_aggregate_cell_ambiguous"} <= reasons
    incomplete = [case for case in pagination if case["expected"].get("group_coverage") == "incomplete"]
    assert incomplete
    for case in incomplete:
        assert case["expected"]["returned_group_count"] < case["expected"]["full_group_count"]
        assert case["expected"]["product_claim_allowed"] is False


def test_zero_empty_unknown_failure_denial_not_queried_and_malformed_differ(fixture):
    zero_cases = cases(fixture, "cases/zero_empty.json")
    classes = {case["expected"]["classification"] for case in zero_cases}
    assert {"valid_zero_aggregate", "valid_empty_result", "valid_table",
            "failure", "denied", "not_queried", "malformed"} <= classes
    pair = [case for case in zero_cases if case.get("pair_id") == "valid-zero-vs-empty-table"]
    assert {case["expected"]["classification"] for case in pair} == {
        "valid_zero_aggregate", "valid_empty_result",
    }
    for case in zero_cases:
        expected = case["expected"]
        if expected["classification"] in {"failure", "denied", "not_queried", "malformed"}:
            assert "valid_zero" in expected["must_not"]
            assert "empty_result" in expected["must_not"]
            assert "business_result" in expected["must_not"]
    malformed = next(case for case in zero_cases if case["case_id"] == "zero.malformed_is_not_zero")
    assert "data_not_list" in VERIFY.validate_native_envelope(malformed["reply"])


def test_every_malformed_reply_fails_closed_with_its_declared_reason(fixture):
    malformed = cases(fixture, "cases/malformed.json")
    assert len(malformed) == 26
    for case in malformed:
        expected = case["expected"]
        codes = VERIFY.validate_native_envelope(case["reply"])
        assert expected["reason"] in codes, (case["case_id"], expected["reason"], codes)
        assert expected["classification"] in {"malformed", "failure"}
        assert "valid_zero" in expected["must_not"]
        assert "empty_result" in expected["must_not"]
        assert "business_result" in expected["must_not"]


def test_current_base_adapter_cannot_project_the_native_envelope(fixture):
    pagination = cases(fixture, "cases/query_pagination.json")
    native = next(case for case in pagination
                  if case["case_id"] == "query.pagination.page3_size4_short_last")["reply"]
    wrapped = {"content": [], "structuredContent": native}

    # The fixed-base adapter expects rows/records as object rows and reads rows
    # as a list. The observed native reply uses a positional data matrix and an
    # integer page size, so it is rejected rather than silently misread.
    assert project_business_result(wrapped) is None

    # Even a manual, lossy conversion to object rows and key-based columns
    # cannot preserve page, has_more, dataset or the native total basis.
    converted = {
        "title": "synthetic converted",
        "dataset": native["dataset"],
        "page": native["page"],
        "has_more": native["has_more"],
        "columns": [
            {"key": column["name"], "label": column["label"], "type": column["type"]}
            for column in native["columns"]
        ],
        "rows": [
            dict(zip([column["name"] for column in native["columns"]], row))
            for row in native["data"]
        ],
    }
    projection = project_business_result({"content": [], "structuredContent": converted})
    assert projection is not None
    assert projection["returned_rows"] == 1
    assert projection["total_rows"] is None
    assert "page" not in projection
    assert "has_more" not in projection
    assert "dataset" not in projection
    assert "total_basis" not in projection

    # Missing object cells become null in the current projection, so a lossy
    # positional-to-object conversion could fabricate a null instead of failing.
    missing_cell = {"columns": [{"key": "order_code", "label": "订单编号"}], "rows": [{}]}
    projected_missing = project_business_result({"content": [], "structuredContent": missing_cell})
    assert projected_missing is not None
    assert projected_missing["rows"] == [{"order_code": None}]


def test_adapter_gaps_reference_existing_cases_and_follow_up(fixture):
    gaps = json.loads((FIXTURE / "adapter-gaps.json").read_text(encoding="utf-8"))
    known_ids = {case["case_id"] for case in all_cases(fixture)}
    assert gaps["adapter"] == "server/app/business_results.py:project_business_result"
    assert gaps["supporting_adapter"] == "server/app/runtime.py:result_fact_metadata"
    assert gaps["gaps"]
    for gap in gaps["gaps"]:
        assert gap["case_ids"]
        assert set(gap["case_ids"]) <= known_ids, gap["gap_id"]
        assert gap["native_observed"]
        assert gap["current_adapter_expects"]
        assert gap["consequence"]
    assert gaps["minimal_follow_up_after_HW_026"]
    assert any("returned_rows from len(data)" in item for item in gaps["minimal_follow_up_after_HW_026"])
    assert gaps["non_claims"]


def test_fixture_has_no_customer_identifiers_or_real_schema_claim(fixture):
    dataset = fixture["dataset"]
    assert dataset["synthetic"] is True
    assert dataset["dataset_code"].startswith("SYNTH-")
    assert all(entry["dataset_code"].startswith("SYNTH-") for entry in dataset["catalog"])
    for row in dataset["rows"]:
        assert row["order_code"].startswith("SYNTH-")
        assert row["region_code"].startswith("SYNTH-")
        assert row["product_code"].startswith("SYNTH-")
    for text in VERIFY.walk_strings(dataset):
        assert "华微" not in text
        assert "huawei" not in text.lower()
    for case in all_cases(fixture):
        for text in VERIFY.walk_strings(case):
            assert "华微" not in text
            assert "huawei" not in text.lower()
    assert fixture["hypotheses"]["not_customer_input_schema"] is True
    assert fixture["manifest"]["product_support_claimed"] is False
