#!/usr/bin/env python3
"""Standalone shape and ground-truth verifier for the synthetic HW-029a fixture.

This module deliberately imports no product runtime and opens no network. It
checks that the fixture is internally consistent, that valid cases agree with
arithmetic recomputed from the synthetic dataset, that malformed replies fail
closed, and that observed shape is separated from labelled hypotheses.

It does not prove product support, customer schema or real-data correctness.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASE_FILES = (
    "cases/describe.json",
    "cases/query_fields_filters_order.json",
    "cases/query_aggregates.json",
    "cases/query_pagination.json",
    "cases/zero_empty.json",
    "cases/malformed.json",
)
REQUIRED_ENVELOPE = ("dataset", "columns", "data", "rows", "page", "has_more")
ALLOWED_TYPES = {"string", "number", "boolean"}
ALLOWED_CONTRACT = {
    "observed_native_shape",
    "observed_field_publication_only",
    "observed_safe_ungrouped",
    "requires_separate_proof",
}
FORBIDDEN_MARKERS = ("华微", "huawei")


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot load {path}: {exc}") from exc


def load_fixture(root=HERE):
    root = Path(root)
    return {
        "root": root,
        "manifest": load_json(root / "manifest.json"),
        "observed": load_json(root / "observed" / "native-envelope.json"),
        "hypotheses": load_json(root / "hypotheses" / "request-and-type-encoding.json"),
        "dataset": load_json(root / "dataset" / "synthetic_sales.json"),
        "cases": {name: load_json(root / name) for name in CASE_FILES},
    }


def field_index(dataset):
    return {field["name"]: field for field in dataset["fields"]}


def published_names(dataset):
    return [field["name"] for field in dataset["fields"] if field.get("published") is True]


def label_map(dataset):
    return {field["label"]: field["name"] for field in dataset["fields"]}


def is_scalar(value):
    # bool is intentionally separate from int for JSON type checks.
    return value is None or type(value) in (str, bool, int, float)


def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from walk_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_strings(child)


def validate_dataset(dataset):
    errors = []
    if dataset.get("fixture_dataset_code") != dataset.get("dataset_code"):
        errors.append("dataset fixture_dataset_code and dataset_code differ")
    if dataset.get("synthetic") is not True:
        errors.append("dataset must be marked synthetic")
    code = dataset.get("dataset_code")
    if not isinstance(code, str) or not code.startswith("SYNTH-"):
        errors.append("dataset_code must be a synthetic SYNTH- code")
    fields = dataset.get("fields")
    if not isinstance(fields, list) or not fields:
        return errors + ["dataset fields must be a non-empty list"]
    names = []
    for field in fields:
        if not isinstance(field, dict):
            errors.append("dataset field must be an object")
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name:
            errors.append("dataset field missing name")
            continue
        if name in names:
            errors.append(f"duplicate dataset field name {name}")
        names.append(name)
        if not isinstance(field.get("label"), str) or not field["label"]:
            errors.append(f"dataset field {name} missing label")
        if field.get("type") not in ALLOWED_TYPES:
            errors.append(f"dataset field {name} has unsupported type {field.get('type')!r}")
        if type(field.get("published")) is not bool:
            errors.append(f"dataset field {name} published must be boolean")
    published = published_names(dataset)
    if not published:
        errors.append("dataset must publish at least one field")
    if len(published) == len(fields):
        errors.append("dataset must keep at least one unpublished field for rejection cases")
    rows = dataset.get("rows")
    if not isinstance(rows, list) or not rows:
        errors.append("dataset rows must be a non-empty list")
        return errors
    published_set = set(published)
    index = field_index(dataset)
    for position, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f"dataset row {position} must be an object")
            continue
        keys = set(row)
        if keys != published_set:
            errors.append(f"dataset row {position} keys differ from published fields")
        for name in published_set:
            if name not in row:
                continue
            value = row[name]
            if not is_scalar(value):
                errors.append(f"dataset row {position} field {name} is not scalar")
                continue
            if isinstance(value, float) and not math.isfinite(value):
                errors.append(f"dataset row {position} field {name} is not finite")
            expected_type = index[name].get("type")
            if expected_type == "string" and not isinstance(value, str):
                errors.append(f"dataset row {position} field {name} should be string")
            if expected_type == "number" and type(value) is not int and type(value) is not float:
                errors.append(f"dataset row {position} field {name} should be numeric")
            if expected_type == "boolean" and type(value) is not bool:
                errors.append(f"dataset row {position} field {name} should be boolean")
        order_code = row.get("order_code")
        if not isinstance(order_code, str) or not order_code.startswith("SYNTH-"):
            errors.append(f"dataset row {position} order_code is not synthetic")
    catalog = dataset.get("catalog")
    if not isinstance(catalog, list) or not catalog:
        errors.append("dataset catalog must be a non-empty list")
        return errors
    catalog_codes = []
    for entry in catalog:
        if not isinstance(entry, dict):
            errors.append("catalog entry must be an object")
            continue
        entry_code = entry.get("dataset_code")
        if not isinstance(entry_code, str) or not entry_code.startswith("SYNTH-"):
            errors.append("catalog entry dataset_code must be synthetic")
        if entry_code in catalog_codes:
            errors.append(f"duplicate catalog entry {entry_code}")
        catalog_codes.append(entry_code)
        if type(entry.get("detail_available")) is not bool:
            errors.append(f"catalog entry {entry_code} detail_available must be boolean")
    if code not in catalog_codes:
        errors.append("primary dataset code is missing from the catalog")
    for text in walk_strings(dataset):
        lowered = text.lower()
        if any(marker.lower() in lowered for marker in FORBIDDEN_MARKERS):
            errors.append("dataset contains a forbidden customer marker")
            break
    return errors


def validate_observed_and_hypotheses(observed, hypotheses):
    errors = []
    if not isinstance(observed, dict):
        return ["observed shape file must be an object"]
    if not isinstance(observed.get("status"), str) or "observed" not in observed["status"]:
        errors.append("observed shape status must state observed")
    observed_fields = {item.get("name") for item in observed.get("fields", []) if isinstance(item, dict)}
    for name in ("dataset", "columns", "data", "rows", "page", "has_more", "total"):
        if name not in observed_fields:
            errors.append(f"observed shape is missing field {name}")
    if not observed.get("semantic_constraints_observed"):
        errors.append("observed shape must list observed semantic constraints")
    if not observed.get("explicit_non_claims"):
        errors.append("observed shape must list explicit non-claims")
    if not isinstance(hypotheses, dict):
        return errors + ["hypotheses file must be an object"]
    if hypotheses.get("not_customer_input_schema") is not True:
        errors.append("hypotheses must explicitly deny a customer input schema")
    if hypotheses.get("synthetic_only") is not True:
        errors.append("hypotheses must be marked synthetic-only")
    items = hypotheses.get("hypotheses")
    if not isinstance(items, list) or not items:
        errors.append("hypotheses list must be non-empty")
    else:
        for item in items:
            status = str(item.get("evidence_status", "")) if isinstance(item, dict) else ""
            if status == "observed":
                errors.append("a hypothesis cannot be labelled exactly observed")
    return errors


def validate_manifest(manifest):
    errors = []
    if manifest.get("fixture_id") != "huawi-parity-query-contract":
        errors.append("unexpected fixture_id")
    if manifest.get("synthetic_only") is not True:
        errors.append("manifest must be synthetic-only")
    if manifest.get("customer_data") is not False:
        errors.append("manifest must deny customer data")
    if manifest.get("customer_schema_inferred") is not False:
        errors.append("manifest must deny customer schema inference")
    if manifest.get("product_support_claimed") is not False:
        errors.append("manifest must deny product support")
    if manifest.get("tests_executed_by_author") is not False:
        errors.append("manifest must state the author did not execute tests")
    if not manifest.get("non_claims"):
        errors.append("manifest must list non-claims")
    if not manifest.get("case_counts"):
        errors.append("manifest must declare case counts")
    return errors


def apply_filters(rows, filters):
    result = list(rows)
    for item in filters or []:
        field = item.get("field")
        operation = item.get("op", "eq")
        if operation != "eq":
            raise ValueError(f"unsupported synthetic filter operation: {operation}")
        value = item.get("value")
        result = [row for row in result if row.get(field) == value]
    return result


def sort_rows(rows, sort):
    result = list(rows)
    for spec in reversed(sort or []):
        field = spec["field"]
        direction = spec.get("direction", "asc")
        if direction not in {"asc", "desc"}:
            raise ValueError(f"unsupported sort direction: {direction}")
        result.sort(key=lambda row: row[field], reverse=direction == "desc")
    return result


def page_slice(rows, page, page_size):
    if type(page) is not int or page < 1:
        raise ValueError("page must be an integer >= 1")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("page_size must be an integer >= 1")
    start = (page - 1) * page_size
    return rows[start:start + page_size], start + page_size < len(rows)


def compute_select(request, dataset):
    rows = apply_filters(dataset["rows"], request.get("filters"))
    rows = sort_rows(rows, request.get("sort"))
    page_rows, has_more = page_slice(rows, request["page"], request["page_size"])
    fields = list(request["fields"])
    data = [[row[field] for field in fields] for row in page_rows]
    return {
        "data": data,
        "columns": fields,
        "returned_rows": len(data),
        "page": request["page"],
        "page_size": request["page_size"],
        "has_more": has_more,
        "total": len(rows),
        "underlying_total": len(rows),
    }


def compute_aggregate(request, dataset):
    rows = apply_filters(dataset["rows"], request.get("filters"))
    operation = request["operation"]
    field = None
    if operation == "count":
        value = len(rows)
    elif operation == "count_distinct":
        field = request["field"]
        value = len({row[field] for row in rows})
    elif operation == "sum":
        field = request["field"]
        value = sum(row[field] for row in rows)
    else:
        raise ValueError(f"unsupported aggregate operation: {operation}")
    return {
        "data": [[value]],
        "columns": [request["alias"]],
        "returned_rows": 1,
        "page": 1,
        "page_size": 1,
        "has_more": False,
        "total": None,
        "aggregate": operation,
        "field": field,
        "value": value,
        "underlying_row_count": len(rows),
    }


def compute_group(request, dataset):
    rows = apply_filters(dataset["rows"], request.get("filters"))
    group_by = request["group_by"]
    aggregate = request["aggregate"]
    kind = aggregate["kind"]
    groups = {}
    for row in rows:
        groups.setdefault(row[group_by], []).append(row)
    full = []
    for key, members in groups.items():
        if kind == "count":
            value = len(members)
        elif kind == "sum":
            value = sum(member[aggregate["field"]] for member in members)
        else:
            raise ValueError(f"unsupported group aggregate: {kind}")
        full.append([key, value])
    page_rows, has_more = page_slice(full, request["page"], request["page_size"])
    alias = "record_count" if kind == "count" else f"{aggregate['field']}_sum"
    return {
        "data": page_rows,
        "columns": [group_by, alias],
        "returned_rows": len(page_rows),
        "page": request["page"],
        "page_size": request["page_size"],
        "has_more": has_more,
        "total": len(full),
        "group_by": group_by,
        "aggregate": kind,
        "full_group_count": len(full),
        "returned_group_count": len(page_rows),
        "full_aggregate_sum": sum(item[1] for item in full),
        "returned_aggregate_sum": sum(item[1] for item in page_rows),
        "underlying_record_count": len(rows),
    }


def describe_candidates(dataset, keyword):
    term = str(keyword or "").lower()
    if not term:
        return []
    candidates = []
    for entry in dataset["catalog"]:
        haystacks = [entry.get("dataset_code", ""), entry.get("title", "")]
        haystacks.extend(entry.get("search_terms", []))
        if any(term in str(value).lower() for value in haystacks):
            candidates.append(entry["dataset_code"])
    return candidates


def validate_native_envelope(reply, expected_dataset=None):
    """Return shape error codes for the observed native envelope."""
    if not isinstance(reply, dict):
        return ["not_object"]
    if reply.get("isError") is True:
        return ["is_error"]
    missing = [name for name in REQUIRED_ENVELOPE if name not in reply]
    if missing:
        return ["missing_required"]
    errors = []
    dataset = reply.get("dataset")
    if not isinstance(dataset, str) or not dataset:
        errors.append("dataset_not_string")
    elif expected_dataset is not None and dataset != expected_dataset:
        errors.append("dataset_mismatch")
    columns = reply.get("columns")
    names = []
    if not isinstance(columns, list):
        errors.append("columns_not_list")
    elif not columns:
        errors.append("empty_columns")
    else:
        for column in columns:
            if not isinstance(column, dict):
                errors.append("column_not_object")
                continue
            name = column.get("name")
            if not isinstance(name, str) or not name:
                errors.append("column_missing_name")
                continue
            if name in names:
                errors.append("duplicate_column_name")
            names.append(name)
            if "label" not in column:
                errors.append("column_missing_label")
            elif not isinstance(column["label"], str) or not column["label"]:
                errors.append("column_label_not_string")
            if "type" not in column:
                errors.append("column_missing_type")
            elif not isinstance(column["type"], str) or not column["type"]:
                errors.append("column_type_not_string")
    data = reply.get("data")
    if not isinstance(data, list):
        errors.append("data_not_list")
    else:
        for row in data:
            if not isinstance(row, list):
                errors.append("row_not_list")
                continue
            if names and len(row) != len(names):
                errors.append("row_width_mismatch")
            for cell in row:
                if not is_scalar(cell):
                    errors.append("cell_not_scalar")
                elif isinstance(cell, float) and not math.isfinite(cell):
                    errors.append("non_finite_number")
    rows = reply.get("rows")
    if type(rows) is not int:
        errors.append("rows_not_integer")
    elif rows < 1:
        errors.append("page_size_below_one")
    elif isinstance(data, list) and len(data) > rows:
        errors.append("data_exceeds_page_size")
    page = reply.get("page")
    if type(page) is not int:
        errors.append("page_not_integer")
    elif page < 1:
        errors.append("page_below_one")
    if type(reply.get("has_more")) is not bool:
        errors.append("has_more_not_boolean")
    if "total" in reply:
        total = reply["total"]
        if type(total) is not int:
            errors.append("total_not_integer")
        elif total < 0:
            errors.append("total_negative")
        elif isinstance(data, list) and total < len(data):
            errors.append("total_less_than_returned")
    ordered = []
    for code in errors:
        if code not in ordered:
            ordered.append(code)
    return ordered


def semantic_issues(reply, request):
    if not isinstance(reply, dict) or reply.get("isError") is True:
        return ["not_business_result"]
    issues = []
    data = reply.get("data")
    total = reply.get("total")
    page = reply.get("page")
    page_size = reply.get("rows")
    has_more = reply.get("has_more")
    if isinstance(data, list):
        if type(total) is int:
            if total < len(data):
                issues.append("total_less_than_returned")
            if type(page) is int and type(page_size) is int:
                start = (page - 1) * page_size
                if has_more is True and total <= start + len(data):
                    issues.append("has_more_contradicts_total")
                if has_more is False and total > start + len(data):
                    issues.append("missing_pages_but_has_more_false")
            if (request.get("operation") == "count" and len(data) == 1
                    and len(data[0]) == 1 and total == data[0][0]):
                issues.append("total_equals_aggregate_cell_ambiguous")
    return issues


def request_fields(request):
    fields = []
    if isinstance(request.get("fields"), list):
        fields.extend(request["fields"])
    for item in request.get("filters", []) or []:
        if isinstance(item, dict) and isinstance(item.get("field"), str):
            fields.append(item["field"])
    for item in request.get("sort", []) or []:
        if isinstance(item, dict) and isinstance(item.get("field"), str):
            fields.append(item["field"])
    return fields


def check_declared_reason(reason, request, dataset):
    names = set(published_names(dataset))
    labels = label_map(dataset)
    fields = request_fields(request)
    if reason == "unpublished_field":
        return any(field not in names for field in fields)
    if reason == "star_only_for_count":
        return "*" in fields and request.get("operation") != "count"
    if reason == "label_not_field_name":
        return any(field in labels and field not in names for field in fields)
    if reason == "unpublished_group_by":
        return request.get("group_by") not in names
    if reason == "group_field_mismatch":
        return any(field != request.get("group_by") for field in request.get("group_fields", []))
    if reason == "group_aggregate_requires_group_by":
        return not request.get("group_by")
    if reason == "explicit_alias_required":
        return request.get("operation") in {"count", "count_distinct"} and not request.get("alias")
    if reason == "operator_not_observed":
        return any(item.get("op", "eq") != "eq" for item in request.get("filters", []) if isinstance(item, dict))
    return False


def check_outcome_reason(reason, reply, request):
    codes = validate_native_envelope(reply)
    if reason in codes:
        return True
    if reason == "query_execution_failed":
        return (isinstance(reply, dict) and isinstance(reply.get("error"), dict)
                and reply["error"].get("code") == "QUERY_EXECUTION_FAILED")
    if reason == "permission_denied":
        return (isinstance(reply, dict) and isinstance(reply.get("error"), dict)
                and reply["error"].get("code") == "PERMISSION_DENIED")
    if reason == "query_not_executed":
        return reply is None and request.get("executed") is False
    return False


def check_contract_status(expected, request):
    errors = []
    status = expected.get("contract_status")
    if status is None:
        return errors
    if status not in ALLOWED_CONTRACT:
        errors.append(f"unknown contract_status {status!r}")
        return errors
    operation = request.get("operation")
    expected_status = {
        "count": "observed_safe_ungrouped",
        "count_distinct": "observed_safe_ungrouped",
        "sum": "requires_separate_proof",
        "group": "requires_separate_proof",
    }.get(operation)
    if expected_status and status != expected_status:
        errors.append(f"contract_status {status!r} inconsistent with {operation}")
    if operation == "select" and status not in {"observed_native_shape", "observed_field_publication_only"}:
        errors.append(f"select contract_status {status!r} is inconsistent")
    return errors


def compare_valid_table(case, computed):
    errors = []
    expected = case["expected"]
    reply = case["reply"]
    request = case["request"]
    for key in ("data", "columns", "returned_rows", "page", "page_size", "has_more"):
        if expected.get(key) != computed.get(key):
            errors.append(f"expected {key} mismatch: expected={expected.get(key)!r} computed={computed.get(key)!r}")
    if expected.get("total") is not None:
        if computed.get("total") != expected["total"]:
            errors.append(f"total mismatch: expected={expected['total']!r} computed={computed.get('total')!r}")
    elif "total" in reply:
        errors.append("reply has total but expected total is null/unknown")
    if reply.get("data") != expected.get("data"):
        errors.append("reply data differs from expected data")
    column_names = [column.get("name") for column in reply.get("columns", [])]
    if column_names != expected.get("columns"):
        errors.append(f"reply column names differ: {column_names!r}")
    if reply.get("rows") != expected.get("page_size"):
        errors.append("reply rows does not carry the page size")
    if reply.get("page") != expected.get("page"):
        errors.append("reply page differs from expected page")
    if reply.get("has_more") != expected.get("has_more"):
        errors.append("reply has_more differs from expected has_more")
    if expected.get("total") is not None and reply.get("total") != expected["total"]:
        errors.append("reply total differs from expected total")
    basis = expected.get("total_basis")
    if basis == "records":
        if computed.get("underlying_total") != expected.get("total"):
            errors.append("records total_basis does not match computed matching rows")
    elif basis == "groups_consistent_but_not_declared":
        if computed.get("full_group_count") != expected.get("total"):
            errors.append("group total_basis does not match computed group count")
        if "underlying_record_count" in expected and computed.get("underlying_record_count") != expected["underlying_record_count"]:
            errors.append("underlying_record_count differs from computed rows")
    elif basis in {"unknown_not_zero", "not_applicable_aggregate_cell"}:
        if "total" in reply:
            errors.append(f"{basis} case unexpectedly carries a total")
    elif basis is not None:
        errors.append(f"unknown total_basis {basis!r}")
    for key in ("group_by", "aggregate", "full_group_count", "returned_group_count",
                "full_aggregate_sum", "returned_aggregate_sum", "underlying_record_count"):
        if key in expected and expected[key] != computed.get(key):
            errors.append(f"expected {key} mismatch: expected={expected[key]!r} computed={computed.get(key)!r}")
    coverage = expected.get("group_coverage")
    if isinstance(coverage, str) and coverage.startswith("incomplete"):
        if not computed.get("returned_group_count", 0) < computed.get("full_group_count", 0):
            errors.append("incomplete group coverage but returned groups are not fewer than full groups")
    if expected.get("must_not_treat_total_as_records"):
        if expected.get("total") == computed.get("underlying_record_count"):
            errors.append("total equals underlying record count but the case says not to treat it as records")
    if expected.get("rows_field_is_page_size"):
        if reply.get("rows") != request.get("page_size") or expected.get("returned_rows") >= request.get("page_size", 0):
            errors.append("rows-field page-size short-page check failed")
    if expected.get("ordering_claim") == "none_without_sort" and request.get("sort"):
        errors.append("ordering_claim none_without_sort but request has sort")
    if expected.get("operator_basis") == "eq_synthetic_hypothesis":
        if any(item.get("op", "eq") != "eq" for item in request.get("filters", [])):
            errors.append("operator_basis eq but request uses another operation")
    errors.extend(check_contract_status(expected, request))
    pagination = expected.get("pagination_completeness")
    if pagination not in (None, "requires_separate_proof"):
        errors.append(f"unexpected pagination_completeness {pagination!r}")
    if expected.get("product_claim_allowed") is False and expected.get("contract_status") != "requires_separate_proof":
        errors.append("product_claim_allowed false without requires_separate_proof")
    return errors


def compare_valid_aggregate(case, computed):
    errors = []
    expected = case["expected"]
    reply = case["reply"]
    request = case["request"]
    for key in ("data", "columns", "returned_rows", "page", "page_size", "has_more"):
        if expected.get(key) != computed.get(key):
            errors.append(f"expected {key} mismatch: expected={expected.get(key)!r} computed={computed.get(key)!r}")
    if expected.get("total") is not None:
        errors.append("aggregate case must not declare a business total")
    if "total" in reply:
        errors.append("aggregate reply must not carry total as an aggregate cell substitute")
    for key in ("aggregate", "field", "value"):
        if key in expected and expected[key] != computed.get(key):
            errors.append(f"expected {key} mismatch: expected={expected[key]!r} computed={computed.get(key)!r}")
    if reply.get("data") != expected.get("data"):
        errors.append("aggregate reply data differs from expected")
    column_names = [column.get("name") for column in reply.get("columns", [])]
    if column_names != expected.get("columns"):
        errors.append(f"aggregate reply column names differ: {column_names!r}")
    if reply.get("rows") != 1 or reply.get("page") != 1 or reply.get("has_more") is not False:
        errors.append("aggregate reply must be a single-row, first-page, no-more result")
    status = expected.get("contract_status")
    operation = request.get("operation")
    if operation in {"count", "count_distinct"} and status != "observed_safe_ungrouped":
        errors.append("count/count_distinct must use observed_safe_ungrouped")
    if operation == "sum" and status != "requires_separate_proof":
        errors.append("sum must use requires_separate_proof")
    if operation == "sum" and expected.get("product_claim_allowed") is not False:
        errors.append("sum must not allow a product claim")
    return errors


def evaluate_query_case(case, dataset):
    errors = []
    allowed_constraints = {"observed", "observed_field_publication_only",
                           "observed_group_field_rule_only", "not_in_known_safe_set"}
    if case.get("semantic_constraint") not in allowed_constraints:
        errors.append("unexpected semantic_constraint")
    expected = case.get("expected")
    if not isinstance(expected, dict):
        return errors + ["missing expected object"]
    request = case.get("request")
    if not isinstance(request, dict):
        return errors + ["missing request object"]
    classification = expected.get("classification")
    if classification in {"rejected", "unverified"}:
        reason = expected.get("reason")
        if not check_declared_reason(reason, request, dataset):
            errors.append(f"declared {classification} reason not supported: {reason!r}")
        if not expected.get("must_not"):
            errors.append("rejected/unverified case must list must_not")
        if classification == "unverified" and expected.get("product_claim_allowed") is not False:
            errors.append("unverified case must set product_claim_allowed false")
        return errors
    if classification in {"failure", "denied", "not_queried", "malformed"}:
        reason = expected.get("reason")
        if not check_outcome_reason(reason, case.get("reply"), request):
            errors.append(f"outcome reason not supported by reply: {reason!r}")
        if not expected.get("must_not"):
            errors.append("outcome case must list must_not")
        return errors
    if classification == "inconsistent":
        reply = case.get("reply")
        codes = set(validate_native_envelope(reply)) | set(semantic_issues(reply, request))
        if expected.get("reason") not in codes:
            errors.append(f"inconsistent reason {expected.get('reason')!r} not found in {sorted(codes)}")
        if not expected.get("must_not"):
            errors.append("inconsistent case must list must_not")
        return errors
    reply = case.get("reply")
    shape_errors = validate_native_envelope(reply, expected_dataset=dataset["dataset_code"])
    if shape_errors:
        errors.append("reply shape errors: " + ",".join(shape_errors))
    if classification in {"valid_table", "valid_empty_page", "valid_empty_result"}:
        if request.get("operation") == "group":
            computed = compute_group(request, dataset)
        else:
            computed = compute_select(request, dataset)
        errors.extend(compare_valid_table(case, computed))
    elif classification in {"valid_aggregate", "valid_zero_aggregate"}:
        computed = compute_aggregate(request, dataset)
        errors.extend(compare_valid_aggregate(case, computed))
    else:
        errors.append(f"unknown classification {classification!r}")
    return errors


def evaluate_describe_case(case, dataset):
    errors = []
    request = case.get("request", {})
    observation = case.get("observation", {})
    expected = case.get("expected", {})
    classification = expected.get("classification")
    operation = request.get("operation")
    if operation == "describe_search":
        keyword = request.get("keyword")
        candidates = describe_candidates(dataset, keyword)
        if expected.get("candidate_codes") != candidates:
            errors.append(f"search candidate codes differ: expected={expected.get('candidate_codes')!r} computed={candidates!r}")
        observed_candidates = [item.get("dataset_code") for item in observation.get("candidates", [])]
        if observed_candidates != candidates:
            errors.append("observation candidate codes differ from computed candidates")
        if observation.get("mode") != "search":
            errors.append("search observation mode must be search")
        if classification not in {"search_candidates", "search_no_match"}:
            errors.append("search case has an invalid classification")
        if classification == "search_candidates" and not candidates:
            errors.append("search_candidates case has no matching candidate")
        if classification == "search_no_match" and candidates:
            errors.append("search_no_match case has matching candidates")
        if expected.get("detail_available") is not False:
            errors.append("search result must not be treated as field detail")
        if expected.get("fields_available") is not False:
            errors.append("search result must not expose verified fields")
        if "usable_as_detail" in expected and expected["usable_as_detail"] is not False:
            errors.append("search result must not be usable as detail")
    elif operation == "describe_detail":
        code = request.get("dataset")
        catalog = {entry["dataset_code"]: entry for entry in dataset["catalog"]}
        titles = {entry.get("title") for entry in dataset["catalog"]}
        labels = label_map(dataset)
        if code == dataset["dataset_code"]:
            if classification != "detail":
                errors.append("primary exact code must classify as detail")
            if observation.get("mode") != "detail":
                errors.append("primary exact-code observation mode must be detail")
            if expected.get("detail_available") is not True or expected.get("fields_available") is not True:
                errors.append("primary exact code must expose detail fields")
            observed_names = [field.get("name") for field in observation.get("fields", [])]
            if observed_names != published_names(dataset):
                errors.append(f"observed field names differ from published names: {observed_names!r}")
            if expected.get("detail_field_names") != published_names(dataset):
                errors.append("expected detail field names differ from published names")
            if expected.get("field_names_are_labels") is not False:
                errors.append("detail field names must not be claimed to be labels")
            for field in observation.get("fields", []):
                if field.get("name") == field.get("label"):
                    errors.append("fixture field name equals label; names and labels are not distinguishable")
        elif code in catalog:
            entry = catalog[code]
            if entry.get("detail_available") is not False:
                errors.append("catalog-only entry unexpectedly offers field detail")
            if classification != "detail_not_found":
                errors.append("catalog-only code must classify as detail_not_found")
            if expected.get("detail_available") is not False or expected.get("fields_available") is not False:
                errors.append("catalog-only code must not expose detail fields")
        elif code in titles or code in labels:
            if classification != "detail_rejected":
                errors.append("title/label exact-code request must be rejected")
            if code == dataset["dataset_code"] or code in catalog:
                errors.append("a title/label was mistaken for a dataset code")
            if expected.get("detail_available") is not False:
                errors.append("rejected detail must not be available")
        else:
            if classification != "detail_not_found":
                errors.append("unknown code must classify as detail_not_found")
            if expected.get("reason") != "unknown_code":
                errors.append("unknown code reason must be unknown_code")
            if expected.get("detail_available") is not False:
                errors.append("unknown code must not expose detail")
    else:
        errors.append("unknown describe operation")
    return errors


def ground_truth(dataset):
    rows = dataset["rows"]

    def groups(field):
        result = {}
        for row in rows:
            key = row[field]
            item = result.setdefault(key, {"count": 0, "sum_amount_cents": 0, "sum_quantity": 0})
            item["count"] += 1
            item["sum_amount_cents"] += row["amount_cents"]
            item["sum_quantity"] += row["quantity"]
        return result

    return {
        "dataset_code": dataset["dataset_code"],
        "row_count": len(rows),
        "sum_amount_cents": sum(row["amount_cents"] for row in rows),
        "sum_quantity": sum(row["quantity"] for row in rows),
        "distinct_region_code": len({row["region_code"] for row in rows}),
        "distinct_product_code": len({row["product_code"] for row in rows}),
        "region_groups": groups("region_code"),
        "product_groups": groups("product_code"),
    }


def run(root=HERE):
    report = {
        "ok": True,
        "errors": [],
        "case_count": 0,
        "counts": {},
        "ground_truth": {},
        "product_runtime_imported": False,
        "network_used": False,
    }
    try:
        fixture = load_fixture(root)
    except Exception as exc:  # noqa: BLE001 - report load failure as data
        report["ok"] = False
        report["errors"].append(str(exc))
        return report
    errors = []
    errors.extend(validate_manifest(fixture["manifest"]))
    errors.extend(validate_dataset(fixture["dataset"]))
    errors.extend(validate_observed_and_hypotheses(fixture["observed"], fixture["hypotheses"]))
    report["ground_truth"] = ground_truth(fixture["dataset"])
    seen = set()
    counts = {}
    for name, group in fixture["cases"].items():
        if group.get("fixture") != fixture["manifest"].get("fixture_id"):
            errors.append(f"{name}: fixture id does not match manifest")
        cases = group.get("cases")
        if not isinstance(cases, list):
            errors.append(f"{name}: cases must be a list")
            continue
        counts[name] = len(cases)
        for case in cases:
            case_id = case.get("case_id")
            if not isinstance(case_id, str) or not case_id:
                errors.append(f"{name}: case without case_id")
                continue
            if case_id in seen:
                errors.append(f"duplicate case_id {case_id}")
            seen.add(case_id)
            try:
                if name == "cases/describe.json":
                    case_errors = evaluate_describe_case(case, fixture["dataset"])
                else:
                    case_errors = evaluate_query_case(case, fixture["dataset"])
            except Exception as exc:  # noqa: BLE001 - report case failure
                case_errors = [f"exception: {exc}"]
            errors.extend(f"{case_id}: {message}" for message in case_errors)
    report["case_count"] = len(seen)
    report["counts"] = counts
    declared = fixture["manifest"].get("case_counts", {})
    for name, count in counts.items():
        if declared.get(name) != count:
            errors.append(f"{name}: manifest count {declared.get(name)!r} != actual {count}")
    if declared.get("total") != len(seen):
        errors.append(f"manifest total count {declared.get('total')!r} != actual {len(seen)}")
    report["errors"] = errors
    report["ok"] = not errors
    return report


def main():
    report = run()
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
