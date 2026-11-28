"""Synthetic tests for the isolated Huawi matrix projection candidate.

No customer data, credentials, account, network or production endpoint is
used. The committed 81-case fixture is synthetic. The author did not execute
these tests; the lead runs the independent command.

Every query uses the observed structured select wire shape: a plain
``{"field": name}`` or an aggregate with an explicit alias. The projector
must bind each returned column name to that exact field or alias.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest import TestCase

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.huawi_matrix_projection import (  # noqa: E402
    MAX_CELL_CHARS, MAX_COLUMNS, MAX_ROWS, project_matrix_result)

FIXTURE = ROOT / "fixtures" / "huawi-parity-query-contract"
DATASET = "SYNTH-SALES-V1"
COLUMNS = [
    {"name": "order_code", "label": "订单编号", "type": "string"},
    {"name": "quantity", "label": "数量", "type": "number"},
]


def example_query():
    return {
        "dataset": DATASET,
        "select": [{"field": "order_code"}, {"field": "quantity"}],
        "filters": [],
        "group_by": [],
        "order_by": [],
        "page": 1,
        "rows": 2,
    }


def example_result():
    return {
        "content": [],
        "structuredContent": {
            "dataset": DATASET,
            "columns": copy.deepcopy(COLUMNS),
            "data": [["SYNTH-ORD-0001", 10], ["SYNTH-ORD-0002", 5]],
            "rows": 2,
            "page": 1,
            "has_more": True,
            "total": 9,
        },
    }


def payload(**changes):
    return example_result()["structuredContent"] | changes


class ValidProjectionTests(TestCase):
    def test_valid_page_is_bound_to_query_arguments(self):
        query, result = example_query(), example_result()
        before = copy.deepcopy((query, result))
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["schema_version"], "huawi-matrix-projection/v1")
        self.assertEqual(projected["classification"], "valid_table")
        self.assertEqual(projected["support_status"],
                         "synthetic_candidate_not_product_support")
        self.assertEqual(projected["dataset"], DATASET)
        self.assertEqual(projected["rows"], [
            {"order_code": "SYNTH-ORD-0001", "quantity": 10},
            {"order_code": "SYNTH-ORD-0002", "quantity": 5},
        ])
        self.assertEqual(projected["returned_rows"], 2)
        self.assertEqual(projected["page"], 1)
        self.assertEqual(projected["page_size"], 2)
        self.assertIs(projected["rows_field_is_page_size"], True)
        self.assertIs(projected["has_more"], True)
        self.assertEqual(projected["page_coverage"], "more_pages_not_returned")
        self.assertIs(projected["global_coverage"], False)
        self.assertIs(projected["complete"], None)
        self.assertIs(projected["scope_verified"], False)
        self.assertEqual(projected["total"], 9)
        self.assertEqual(projected["total_basis"], "output_rows")
        self.assertIs(projected["total_is_aggregate_cell"], False)
        self.assertIs(projected["total_is_record_count"], False)
        self.assertIs(projected["empty"], False)
        self.assertIs(projected["filtered"], False)
        self.assertIs(projected["ordered"], False)
        self.assertEqual(projected["select_column_binding"],
                         "exact_selected_field_or_alias")
        self.assertEqual(projected["selected_output_columns"],
                         ["order_code", "quantity"])
        self.assertEqual(projected["aggregate_columns"], [])
        self.assertIs(projected["has_aggregate"], False)
        self.assertIs(projected["aggregate_value_verified"], False)
        self.assertIs(projected["type_vocabulary_verified"], False)
        # The projector is pure and never mutates its inputs.
        self.assertEqual((query, result), before)
        self.assertEqual(projected, project_matrix_result(query, result))

    def test_page_size_is_not_returned_rows(self):
        query = example_query()
        query["page"] = 3
        query["rows"] = 4
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": copy.deepcopy(COLUMNS),
            "data": [["SYNTH-ORD-0009", 6]],
            "rows": 4,
            "page": 3,
            "has_more": False,
            "total": 9,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["returned_rows"], 1)
        self.assertEqual(projected["page_size"], 4)
        self.assertNotEqual(projected["returned_rows"], projected["page_size"])
        self.assertTrue(any("页大小" in item for item in projected["warnings"]))

    def test_zero_cell_is_not_an_empty_result(self):
        query = {
            "dataset": DATASET, "select": [{"field": "record_count"}],
            "filters": [], "group_by": [], "order_by": [],
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "record_count", "label": "记录数", "type": "number"}],
            "data": [[0]],
            "rows": 1, "page": 1, "has_more": False,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["classification"], "valid_table")
        self.assertIs(projected["empty"], False)
        self.assertEqual(projected["zero_cell_count"], 1)
        self.assertEqual(projected["zero_kind"], "zero_cells_present_not_empty")
        self.assertTrue(any("不是空结果" in item for item in projected["warnings"]))

    def test_empty_result_with_zero_total(self):
        query = example_query()
        query["rows"] = 3
        query["filters"] = [{"field": "region_code", "op": "eq", "value": "SYNTH-R9"}]
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": copy.deepcopy(COLUMNS),
            "data": [], "rows": 3, "page": 1, "has_more": False, "total": 0,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["classification"], "valid_empty_result")
        self.assertIs(projected["empty"], True)
        self.assertEqual(projected["empty_kind"], "query_reported_zero_output_rows")
        self.assertEqual(projected["total"], 0)
        self.assertEqual(projected["total_basis"], "output_rows")
        self.assertTrue(projected["filtered"])
        self.assertTrue(any("过滤" in item for item in projected["warnings"]))

    def test_empty_first_page_without_total_is_unknown_not_zero(self):
        query = example_query()
        query["rows"] = 3
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": copy.deepcopy(COLUMNS),
            "data": [], "rows": 3, "page": 1, "has_more": False,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["classification"], "valid_empty_result")
        self.assertEqual(projected["empty_kind"], "empty_first_page_total_unknown")
        self.assertEqual(projected["total_basis"], "unknown_not_zero")
        self.assertTrue(any("未知不等于 0" in item for item in projected["warnings"]))

    def test_beyond_last_empty_page_is_not_dataset_zero(self):
        query = example_query()
        query["page"] = 4
        query["rows"] = 3
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": copy.deepcopy(COLUMNS),
            "data": [], "rows": 3, "page": 4, "has_more": False, "total": 9,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["classification"], "valid_empty_page")
        self.assertEqual(projected["empty_kind"], "beyond_last_page_not_dataset_zero")
        self.assertEqual(projected["total"], 9)
        self.assertIs(projected["global_coverage"], False)
        self.assertTrue(any("不代表数据集为空" in item for item in projected["warnings"]))

    def test_grouped_total_basis_is_not_declared(self):
        query = {
            "dataset": DATASET,
            "select": [{"field": "region_code"},
                       {"field": "*", "aggregate": "count",
                        "alias": "record_count"}],
            "filters": [],
            "group_by": ["region_code"],
            "order_by": [],
            "page": 1,
            "rows": 2,
            "include_total": True,
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [
                {"name": "region_code", "label": "区域编码", "type": "string"},
                {"name": "record_count", "label": "记录数", "type": "number"},
            ],
            "data": [["SYNTH-R1", 4], ["SYNTH-R2", 3]],
            "rows": 2, "page": 1, "has_more": True, "total": 3,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertIs(projected["grouped"], True)
        self.assertEqual(projected["group_by"], ["region_code"])
        self.assertEqual(projected["total"], 3)
        self.assertEqual(projected["total_basis"],
                         "groups_consistent_but_not_declared")
        self.assertIs(projected["total_is_record_count"], False)
        self.assertIs(projected["total_is_aggregate_cell"], False)
        self.assertIs(projected["total_may_count_output_groups"], True)
        self.assertEqual(projected["group_coverage"],
                         "returned_page_of_groups_not_all_groups")
        self.assertEqual(projected["output_coverage"],
                         "incomplete_relative_to_possible_later_pages")
        self.assertTrue(any("口径未声明" in item for item in projected["warnings"]))

    def test_complete_group_page_is_still_scope_unverified(self):
        query = {
            "dataset": DATASET, "select": [{"field": "region_code"}],
            "filters": [], "group_by": ["region_code"], "order_by": [],
            "page": 1, "rows": 3, "include_total": True,
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "region_code", "label": "区域编码", "type": "string"}],
            "data": [["SYNTH-R1"], ["SYNTH-R2"], ["SYNTH-R3"]],
            "rows": 3, "page": 1, "has_more": False, "total": 3,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["group_coverage"],
                         "total_consistent_with_all_returned_groups_basis_not_declared")
        self.assertEqual(projected["output_coverage"],
                         "total_consistent_with_returned_output_basis_not_declared")
        self.assertIs(projected["global_coverage"], False)
        self.assertIs(projected["scope_verified"], False)

    def test_incomplete_later_group_page(self):
        query = {
            "dataset": DATASET, "select": [{"field": "region_code"}],
            "filters": [], "group_by": ["region_code"], "order_by": [],
            "page": 2, "rows": 2, "include_total": True,
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "region_code", "label": "区域编码", "type": "string"}],
            "data": [["SYNTH-R3"]],
            "rows": 2, "page": 2, "has_more": False, "total": 3,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["returned_rows"], 1)
        self.assertEqual(projected["page_size"], 2)
        self.assertEqual(projected["group_coverage"],
                         "returned_groups_incomplete_if_total_counts_groups")
        self.assertTrue(any("若 total 按输出组数计" in item
                            for item in projected["warnings"]))

    def test_filtered_and_ordered_output_is_labelled_candidate(self):
        query = example_query()
        query["filters"] = [{"field": "region_code", "op": "eq", "value": "SYNTH-R1"}]
        query["order_by"] = [{"field": "order_code", "direction": "asc"}]
        projected = project_matrix_result(query, example_result())
        self.assertIsNotNone(projected)
        self.assertIs(projected["filtered"], True)
        self.assertIs(projected["ordered"], True)
        self.assertEqual(projected["support_status"],
                         "synthetic_candidate_not_product_support")
        self.assertIs(projected["scope_verified"], False)
        self.assertTrue(any("过滤" in item for item in projected["warnings"]))

    def test_arbitrary_type_word_is_not_interpreted(self):
        query = example_query()
        query["select"] = [{"field": "amount"}]
        query["page"] = 1
        query["rows"] = 1
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "amount", "label": "金额", "type": "decimal"}],
            "data": [["1.25"]],
            "rows": 1, "page": 1, "has_more": False, "total": 1,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["columns"][0]["type"], "decimal")
        self.assertEqual(projected["rows"], [{"amount": "1.25"}])
        self.assertIs(projected["type_vocabulary_verified"], False)

    def test_none_and_boundary_scalars_are_preserved(self):
        query = example_query()
        query["select"] = [
            {"field": field} for field in
            ("missing", "negative", "fraction", "flag", "max_int")]
        query["rows"] = 1
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [
                {"name": "missing", "label": "空", "type": "string"},
                {"name": "negative", "label": "负数", "type": "number"},
                {"name": "fraction", "label": "小数", "type": "number"},
                {"name": "flag", "label": "布尔", "type": "boolean"},
                {"name": "max_int", "label": "最大整数", "type": "number"},
            ],
            "data": [[None, -5, 1.5, True, 9_007_199_254_740_991]],
            "rows": 1, "page": 1, "has_more": False, "total": 1,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["rows"][0], {
            "missing": None, "negative": -5, "fraction": 1.5,
            "flag": True, "max_int": 9_007_199_254_740_991,
        })

    def test_row_and_column_bounds_are_accepted_at_the_limit(self):
        query = example_query()
        query["select"] = [{"field": "value"}]
        query["rows"] = MAX_ROWS
        rows_result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "value", "label": "值", "type": "number"}],
            "data": [[index] for index in range(MAX_ROWS)],
            "rows": MAX_ROWS, "page": 1, "has_more": False,
            "total": MAX_ROWS,
        }}
        projected = project_matrix_result(query, rows_result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["returned_rows"], MAX_ROWS)

        query = example_query()
        query["rows"] = 1
        columns = [{"name": f"col_{index}", "label": str(index), "type": "number"}
                   for index in range(MAX_COLUMNS)]
        query["select"] = [{"field": column["name"]} for column in columns]
        columns_result = {"structuredContent": {
            "dataset": DATASET, "columns": columns,
            "data": [[index for index in range(MAX_COLUMNS)]],
            "rows": 1, "page": 1, "has_more": False, "total": 1,
        }}
        projected = project_matrix_result(query, columns_result)
        self.assertIsNotNone(projected)
        self.assertEqual(len(projected["columns"]), MAX_COLUMNS)


class BindingTests(TestCase):
    def assert_rejected(self, query, result):
        self.assertIsNone(project_matrix_result(query, result))

    def test_dataset_must_match(self):
        query = example_query()
        query["dataset"] = "SYNTH-OTHER"
        self.assert_rejected(query, example_result())

    def test_page_and_page_size_must_match(self):
        query = example_query()
        query["page"] = 2
        self.assert_rejected(query, example_result())
        query = example_query()
        query["rows"] = 3
        self.assert_rejected(query, example_result())

    def test_missing_page_and_rows_are_not_assumed(self):
        query = example_query()
        query.pop("page")
        query.pop("rows")
        projected = project_matrix_result(query, example_result())
        self.assertIsNotNone(projected)
        self.assertEqual(projected["page"], 1)
        self.assertEqual(projected["page_size"], 2)

    def test_include_total_false_cannot_carry_total(self):
        query = example_query()
        query["include_total"] = False
        self.assert_rejected(query, example_result())

    def test_include_total_true_with_absent_total_is_unknown(self):
        query = example_query()
        query["include_total"] = True
        result = example_result()
        result["structuredContent"].pop("total")
        result["structuredContent"]["has_more"] = False
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertIsNone(projected["total"])
        self.assertEqual(projected["total_basis"], "unknown_not_zero")

    def test_returned_columns_must_match_select_exactly(self):
        query = example_query()
        missing = example_result()
        missing["structuredContent"]["columns"] = [
            {"name": "order_code", "label": "订单编号", "type": "string"}]
        missing["structuredContent"]["data"] = [
            ["SYNTH-ORD-0001"], ["SYNTH-ORD-0002"]]
        self.assert_rejected(query, missing)

        extra = example_result()
        extra["structuredContent"]["columns"] = copy.deepcopy(COLUMNS) + [
            {"name": "region_code", "label": "区域编码", "type": "string"}]
        extra["structuredContent"]["data"] = [
            ["SYNTH-ORD-0001", 10, "SYNTH-R1"],
            ["SYNTH-ORD-0002", 5, "SYNTH-R1"]]
        self.assert_rejected(query, extra)

        reordered = example_result()
        reordered["structuredContent"]["columns"] = [
            {"name": "quantity", "label": "数量", "type": "number"},
            {"name": "order_code", "label": "订单编号", "type": "string"}]
        reordered["structuredContent"]["data"] = [
            [10, "SYNTH-ORD-0001"], [5, "SYNTH-ORD-0002"]]
        self.assert_rejected(query, reordered)

    def test_unobserved_select_shapes_fail_closed(self):
        bad_selects = [
            "SELECT * FROM synthetic",
            ["order_code"],
            [None],
            [{"field": "*"}],
            [{"field": "bad name"}],
            [{"field": "order_code", "aggregate": "sum", "alias": "total"}],
            [{"aggregate": "count", "alias": "record_count"}],
            [{"field": "*", "aggregate": "count"}],
            [{"field": "order_code", "aggregate": "count", "alias": "n"}],
            [{"field": "*", "aggregate": "count_distinct", "alias": "n"}],
            [{"field": "order_code", "aggregate": "count_distinct",
              "alias": "n", "unit": "x"}],
            [{"field": "order_code", "aggregate": "count_distinct",
              "alias": "__proto__"}],
            [{"field": "order_code"}, {"field": "order_code"}],
            [{"field": "order_code"},
             {"field": "quantity", "aggregate": "count_distinct",
              "alias": "order_code"}],
            [{"field": "*", "aggregate": "count", "alias": "order_code"},
             {"field": "order_code"}],
            [{"field": "*", "aggregate": "count", "alias": "record_count"}] * (
                MAX_COLUMNS + 1),
        ]
        for select in bad_selects:
            with self.subTest(select=select):
                query = example_query()
                query["select"] = select
                self.assert_rejected(query, example_result())

    def test_count_aggregate_binds_alias_and_is_not_certified(self):
        query = {
            "dataset": DATASET,
            "select": [{"field": "*", "aggregate": "count",
                        "alias": "record_count"}],
            "filters": [], "group_by": [], "order_by": [],
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "record_count", "label": "记录数",
                         "type": "number"}],
            "data": [[9]], "rows": 1, "page": 1, "has_more": False,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["selected_output_columns"],
                         ["record_count"])
        self.assertEqual(projected["aggregate_columns"], [
            {"field": "*", "aggregate": "count", "alias": "record_count"}])
        self.assertIs(projected["has_aggregate"], True)
        self.assertIs(projected["aggregate_value_verified"], False)
        self.assertIs(projected["scope_verified"], False)
        self.assertTrue(any("聚合" in item for item in projected["warnings"]))

    def test_count_distinct_alias_must_match_returned_column(self):
        query = {
            "dataset": DATASET,
            "select": [{"field": "region_code", "aggregate": "count_distinct",
                        "alias": "distinct_count"}],
            "filters": [], "group_by": [], "order_by": [],
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "region_code", "label": "区域编码",
                         "type": "string"}],
            "data": [[3]], "rows": 1, "page": 1, "has_more": False,
        }}
        self.assert_rejected(query, result)

    def test_group_field_must_be_a_selected_plain_field(self):
        query = example_query()
        query["group_by"] = ["region_code"]
        self.assert_rejected(query, example_result())

    def test_group_by_aggregate_alias_is_ambiguous(self):
        query = {
            "dataset": DATASET,
            "select": [{"field": "region_code"},
                       {"field": "*", "aggregate": "count",
                        "alias": "record_count"}],
            "filters": [], "group_by": ["record_count"], "order_by": [],
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [
                {"name": "region_code", "label": "区域编码", "type": "string"},
                {"name": "record_count", "label": "记录数", "type": "number"},
            ],
            "data": [["SYNTH-R1", 4]], "rows": 1, "page": 1,
            "has_more": False,
        }}
        self.assert_rejected(query, result)

    def test_order_by_must_reference_an_output_name(self):
        query = example_query()
        query["order_by"] = [{"field": "region_code", "direction": "asc"}]
        self.assert_rejected(query, example_result())

    def test_order_by_malformed_shapes_fail_closed(self):
        for order_by in (
            "order_code",
            [{"direction": "asc"}],
            [{"field": "order_code", "direction": 1}],
            [{"field": "order_code", "direction": "a" * 33}],
            [{"field": "order_code", "unknown": True}],
            ["order_code"],
        ):
            with self.subTest(order_by=order_by):
                query = example_query()
                query["order_by"] = order_by
                self.assert_rejected(query, example_result())

    def test_order_by_output_alias_is_accepted_without_reading_direction(self):
        query = {
            "dataset": DATASET,
            "select": [{"field": "*", "aggregate": "count",
                        "alias": "record_count"}],
            "filters": [], "group_by": [],
            "order_by": [{"field": "record_count", "direction": "desc"}],
        }
        result = {"structuredContent": {
            "dataset": DATASET,
            "columns": [{"name": "record_count", "label": "记录数",
                         "type": "number"}],
            "data": [[9]], "rows": 1, "page": 1, "has_more": False,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertIs(projected["ordered"], True)

    def test_filter_items_are_not_interpreted_but_must_be_a_list(self):
        query = example_query()
        query["filters"] = [{"field": "unselected_field", "op": "unproven",
                             "value": 1}]
        projected = project_matrix_result(query, example_result())
        self.assertIsNotNone(projected)
        self.assertIs(projected["filtered"], True)
        query["filters"] = {"field": "order_code"}
        self.assert_rejected(query, example_result())

    def test_malformed_query_shapes_fail_closed(self):
        cases = [
            ("dataset", "bad dataset"),
            ("dataset", "1SYNTH"),
            ("dataset", "__proto__"),
            ("select", []),
            ("select", None),
            ("select", "order_code"),
            ("select", [{}] * (MAX_COLUMNS + 1)),
            ("filters", {}),
            ("filters", None),
            ("group_by", {}),
            ("group_by", None),
            ("group_by", ["bad field"]),
            ("group_by", ["__proto__"]),
            ("group_by", ["region_code", "region_code"]),
            ("order_by", None),
            ("page", 0),
            ("page", None),
            ("page", True),
            ("page", 9_007_199_254_740_992),
            ("rows", 0),
            ("rows", None),
            ("rows", 1_001),
            ("rows", True),
            ("include_total", None),
            ("include_total", "true"),
            ("extra", "synthetic-only"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                query = example_query()
                query[key] = value
                self.assert_rejected(query, example_result())

    def test_non_object_query_fails_closed(self):
        for value in (None, [], "query", 1):
            with self.subTest(value=value):
                self.assert_rejected(value, example_result())


class ConsistencyTests(TestCase):
    def assert_rejected(self, result):
        self.assertIsNone(project_matrix_result(example_query(), result))

    def test_total_below_returned_rows(self):
        self.assert_rejected({"structuredContent": payload(total=1)})

    def test_total_equal_to_returned_with_more_pages(self):
        result = {"structuredContent": payload(total=2)}
        self.assertIsNone(project_matrix_result(example_query(), result))

    def test_first_page_total_must_equal_returned_rows(self):
        result = {"structuredContent": payload(has_more=False, total=9)}
        self.assert_rejected(result)
        result = {"structuredContent": payload(has_more=False, total=3)}
        self.assert_rejected(result)

    def test_later_last_page_total_must_be_plausible(self):
        query = example_query()
        query["page"] = 2
        query["rows"] = 2
        first = {"dataset": DATASET, "columns": copy.deepcopy(COLUMNS),
                 "data": [["SYNTH-ORD-0003", 1]],
                 "rows": 2, "page": 2, "has_more": False, "total": 3}
        projected = project_matrix_result(query, {"structuredContent": first})
        self.assertIsNotNone(projected)
        # A later non-empty page cannot account for every output row.
        same = dict(first, total=1)
        self.assert_rejected({"structuredContent": same})
        # More prior rows than the largest possible page prefix are impossible.
        too_many = dict(first, total=7)
        self.assert_rejected({"structuredContent": too_many})

    def test_empty_page_cannot_have_more_pages(self):
        result = {"structuredContent": payload(
            data=[], has_more=True, total=9)}
        self.assert_rejected(result)

    def test_first_empty_page_cannot_report_positive_total(self):
        result = {"structuredContent": payload(
            data=[], has_more=False, total=3)}
        self.assert_rejected(result)

    def test_grouped_total_basis_is_not_used_for_page_completeness(self):
        query = {
            "dataset": DATASET, "select": [{"field": "region_code"}],
            "filters": [], "group_by": ["region_code"], "order_by": [],
            "page": 1, "rows": 2, "include_total": True,
        }
        columns = [{"name": "region_code", "label": "区域编码", "type": "string"}]
        result = {"structuredContent": {
            "dataset": DATASET, "columns": columns,
            "data": [["SYNTH-R1"], ["SYNTH-R2"]],
            "rows": 2, "page": 1, "has_more": False, "total": 9,
        }}
        projected = project_matrix_result(query, result)
        self.assertIsNotNone(projected)
        self.assertEqual(projected["total_basis"],
                         "groups_consistent_but_not_declared")
        self.assertIs(projected["total_basis_ambiguous"], True)
        self.assertEqual(projected["group_coverage"],
                         "returned_groups_incomplete_if_total_counts_groups")
        # Lower-bound contradictions remain rejected under either basis.
        for total, has_more in ((1, False), (2, True), (3, False)):
            with self.subTest(total=total, has_more=has_more):
                body = dict(result["structuredContent"],
                            total=total, has_more=has_more)
                if total == 3:
                    body["data"] = []
                self.assertIsNone(project_matrix_result(
                    query, {"structuredContent": body}))

    def test_total_types_and_bounds(self):
        for total in (-1, 1.0, "1", True, None, 9_007_199_254_740_992):
            with self.subTest(total=total):
                result = {"structuredContent": payload(total=total)}
                self.assert_rejected(result)


class EnvelopeTests(TestCase):
    def assert_rejected(self, result):
        self.assertIsNone(project_matrix_result(example_query(), result))

    def test_result_must_be_a_bounded_envelope(self):
        self.assertIsNotNone(project_matrix_result(
            example_query(), {"structuredContent": payload()}))
        for result in (
            None, [], "result", 1,
            {"content": [], "structured_content": payload()},
            {"content": "text", "structuredContent": payload()},
            {"content": [], "structuredContent": payload(), "extra": True},
            {"content": [], "structuredContent": payload(), "isError": True},
            {"content": [], "structuredContent": payload(), "isError": 0},
            {"content": [], "structuredContent": payload(), "isError": None},
            {"content": [], "structuredContent": payload(), "is_error": True},
            {"content": [], "structuredContent": "payload"},
            {"content": [], "structuredContent": None},
        ):
            with self.subTest(result=result):
                self.assert_rejected(result)

    def test_unknown_or_missing_payload_fields(self):
        for key in ("dataset", "columns", "data", "rows", "page", "has_more"):
            with self.subTest(key=key):
                body = payload()
                body.pop(key)
                self.assert_rejected({"structuredContent": body})
        self.assert_rejected({"structuredContent": payload(truncated=True)})
        self.assert_rejected({"structuredContent": payload(dataset="SYNTH-OTHER")})

    def test_column_declarations_are_strict(self):
        for columns in (
            None, {}, [], [{"name": "order_code"}],
            [{"name": "order_code", "label": "A", "type": "string", "unit": "x"}],
            [{"name": "order_code", "label": "A", "type": "string"},
             {"name": "order_code", "label": "B", "type": "string"}],
            [{"name": "bad name", "label": "A", "type": "string"}],
            [{"name": "__proto__", "label": "A", "type": "string"}],
            [{"name": "order_code", "label": {}, "type": "string"}],
            [{"name": "order_code", "label": "A" * 201, "type": "string"}],
            [{"name": "order_code", "label": "A", "type": {}}],
            [{"name": "order_code", "label": "A", "type": ""}],
            [{"name": "order_code", "label": "A", "type": "x" * 101}],
        ):
            with self.subTest(columns=columns):
                body = payload(columns=columns)
                self.assert_rejected({"structuredContent": body})

    def test_positional_rows_are_strict(self):
        query = example_query()
        query.pop("rows")
        invalid = (
            {}, "rows", [["SYNTH-ORD-0001"]],
            [["SYNTH-ORD-0001", 10, "extra"]],
            [[{"nested": 1}, 10]],
            [["SYNTH-ORD-0001", [10]]],
            [["SYNTH-ORD-0001", float("inf")]],
            [["SYNTH-ORD-0001", float("nan")]],
            [["SYNTH-ORD-0001", 9_007_199_254_740_992]],
            [["x" * (MAX_CELL_CHARS + 1), 10]],
        )
        for data in invalid:
            with self.subTest(data=data):
                body = payload(data=data, rows=1, has_more=False, total=1)
                self.assertIsNone(project_matrix_result(
                    query, {"structuredContent": body}))

    def test_data_cannot_exceed_page_size_or_projection_bounds(self):
        query = example_query()
        query.pop("rows")
        body = payload(data=[["SYNTH-ORD-0001", 10], ["SYNTH-ORD-0002", 5]],
                       rows=1, has_more=False, total=2)
        self.assertIsNone(project_matrix_result(query, {"structuredContent": body}))
        columns = [{"name": "value", "label": "值", "type": "string"}]
        query["select"] = [{"field": "value"}]
        rows = [["x"] for _ in range(MAX_ROWS + 1)]
        body = payload(columns=columns, data=rows, rows=MAX_ROWS + 1,
                       has_more=False, total=MAX_ROWS + 1)
        self.assertIsNone(project_matrix_result(query, {"structuredContent": body}))
        too_many_columns = [
            {"name": f"col_{index}", "label": str(index), "type": "number"}
            for index in range(MAX_COLUMNS + 1)
        ]
        body = payload(columns=too_many_columns, data=[[0] * (MAX_COLUMNS + 1)],
                       rows=1, has_more=False, total=1)
        self.assertIsNone(project_matrix_result(query, {"structuredContent": body}))

    def test_page_and_has_more_types(self):
        for key, value in (("rows", "2"), ("rows", 0), ("rows", 1_001),
                           ("page", "1"), ("page", 0), ("page", True),
                           ("has_more", "false"), ("has_more", 0),
                           ("has_more", None)):
            with self.subTest(key=key, value=value):
                self.assert_rejected({"structuredContent": payload(**{key: value})})

    def test_oversized_projection_fails_closed(self):
        query = example_query()
        query["select"] = [{"field": "value"}]
        query["rows"] = MAX_ROWS
        long_text = "x" * MAX_CELL_CHARS
        body = {
            "dataset": DATASET,
            "columns": [{"name": "value", "label": "值", "type": "string"}],
            "data": [[long_text] for _ in range(MAX_ROWS)],
            "rows": MAX_ROWS, "page": 1, "has_more": False, "total": MAX_ROWS,
        }
        self.assertIsNone(project_matrix_result(query, {"structuredContent": body}))


class FixtureIntegrationTests(TestCase):
    """Cross-check the synthetic 81-case fixture against the projector.

    Fixture requests use a normalized synthetic vocabulary (``operation``,
    ``fields``, ``page_size``, ``sort``), not the native input schema. This
    adapter translates each case into the observed structured select shape so
    exact select-to-column binding is exercised:

    * select:         ``[{"field": f}, ...]``
    * count:          ``[{"field": "*", "aggregate": "count", "alias": a}]``
    * count_distinct: ``[{"field": f, "aggregate": "count_distinct",
      "alias": a}]``
    * group:          ``[{"field": group_by}, aggregate item]``

    Group requests do not carry an aggregate alias, so the adapter derives a
    synthetic alias from the reply's non-group column. This is a test-only
    assumption, not a native request-schema claim.

    SUM is not an observed safe aggregate. The module therefore fails closed
    on ungrouped and grouped SUM select items; those cases are asserted to be
    rejected here rather than projected as arithmetic ground truth.
    """

    @staticmethod
    def _aggregate_alias(case, excluded):
        reply = case.get("reply")
        columns = reply.get("columns") if isinstance(reply, dict) else None
        if isinstance(columns, list):
            for column in columns:
                if isinstance(column, dict):
                    name = column.get("name")
                    if isinstance(name, str) and name not in excluded:
                        return name
        return None

    def _select_from_case(self, case):
        request = case["request"]
        operation = request.get("operation")
        if operation == "select":
            return [{"field": field} for field in request.get("fields", [])]
        if operation == "count":
            item = {"field": "*", "aggregate": "count"}
            alias = request.get("alias")
            if alias is not None:
                item["alias"] = alias
            return [item]
        if operation == "count_distinct":
            item = {"field": request.get("field"),
                    "aggregate": "count_distinct"}
            alias = request.get("alias")
            if alias is not None:
                item["alias"] = alias
            return [item]
        if operation == "sum":
            # Not in the observed safe aggregate set; the module must reject it.
            return [{"field": request.get("field"),
                     "aggregate": "sum",
                     "alias": request.get("alias")}]
        if operation == "group":
            group_by = request.get("group_by")
            aggregate = request.get("aggregate") or {}
            if not isinstance(group_by, str):
                # No usable group field: keep the query structurally simple and
                # let the null reply exercise the fail-closed path.
                return [{"field": "synthetic_field"}]
            alias = self._aggregate_alias(case, {group_by}) or "aggregate_value"
            kind = aggregate.get("kind")
            field = aggregate.get("field", "*")
            if kind == "count":
                field = "*"
            return [{"field": group_by},
                    {"field": field, "aggregate": kind, "alias": alias}]
        raise AssertionError(f"unknown operation {operation!r}")

    def _query_from_case(self, case):
        request = case["request"]
        group_by = request.get("group_by")
        query = {
            "dataset": request["dataset"],
            "select": self._select_from_case(case),
            "filters": copy.deepcopy(request.get("filters", [])),
            "group_by": [group_by] if isinstance(group_by, str) else [],
            "order_by": copy.deepcopy(request.get("sort", [])),
        }
        if "page" in request:
            query["page"] = request["page"]
        if "page_size" in request:
            query["rows"] = request["page_size"]
        if "include_total" in request:
            query["include_total"] = request["include_total"]
        return query

    @staticmethod
    def _result_from_reply(reply):
        if not isinstance(reply, dict):
            return reply
        if "isError" in reply or "is_error" in reply:
            return reply
        return {"content": [], "structuredContent": reply}

    def test_all_non_describe_fixture_cases(self):
        paths = sorted((FIXTURE / "cases").glob("*.json"))
        self.assertTrue(paths)
        total = 0
        tested = 0
        for path in paths:
            group = json.loads(path.read_text(encoding="utf-8"))
            for case in group["cases"]:
                total += 1
                if "describe" in path.name:
                    continue
                tested += 1
                request = case["request"]
                operation = request.get("operation")
                aggregate = request.get("aggregate")
                expected = case["expected"]
                classification = expected["classification"]
                projected = project_matrix_result(
                    self._query_from_case(case),
                    self._result_from_reply(case.get("reply")))
                is_unproven_sum = (
                    operation == "sum"
                    or (operation == "group" and isinstance(aggregate, dict)
                        and aggregate.get("kind") == "sum"))
                if is_unproven_sum:
                    self.assertIsNone(projected, case["case_id"])
                    continue
                if classification in {"failure", "denied", "malformed",
                                      "inconsistent", "rejected", "unverified",
                                      "not_queried"}:
                    self.assertIsNone(projected, case["case_id"])
                    continue
                if case.get("reply") is None:
                    self.assertIsNone(projected, case["case_id"])
                    continue
                self.assertIsNotNone(projected, case["case_id"])
                reply = case["reply"]
                self.assertEqual(projected["returned_rows"],
                                 expected["returned_rows"], case["case_id"])
                self.assertEqual(projected["returned_rows"], len(reply["data"]),
                                 case["case_id"])
                self.assertEqual(projected["page_size"], reply["rows"],
                                 case["case_id"])
                self.assertEqual(projected["page"], reply["page"],
                                 case["case_id"])
                self.assertIs(projected["global_coverage"], False,
                              case["case_id"])
                self.assertIs(projected["scope_verified"], False,
                              case["case_id"])
                self.assertEqual(projected["support_status"],
                                 "synthetic_candidate_not_product_support",
                                 case["case_id"])
                self.assertEqual(projected["select_column_binding"],
                                 "exact_selected_field_or_alias",
                                 case["case_id"])
                self.assertEqual(
                    [column["name"] for column in projected["columns"]],
                    [column["name"] for column in reply["columns"]],
                    case["case_id"])
                if "total" in reply:
                    self.assertEqual(projected["total"], reply["total"],
                                     case["case_id"])
                else:
                    self.assertIsNone(projected["total"], case["case_id"])
                    self.assertEqual(projected["total_basis"],
                                     "unknown_not_zero", case["case_id"])
                grouped = bool(self._query_from_case(case)["group_by"])
                if "total" in reply:
                    self.assertEqual(
                        projected["total_basis"],
                        "groups_consistent_but_not_declared" if grouped
                        else "output_rows",
                        case["case_id"])
                if classification == "valid_empty_result":
                    self.assertEqual(projected["classification"],
                                     "valid_empty_result", case["case_id"])
                elif classification == "valid_empty_page":
                    self.assertEqual(projected["classification"],
                                     "valid_empty_page", case["case_id"])
                else:
                    self.assertEqual(projected["classification"],
                                     "valid_table", case["case_id"])
                self.assertEqual(projected["empty"],
                                 expected["returned_rows"] == 0,
                                 case["case_id"])
                if classification == "valid_zero_aggregate":
                    self.assertGreater(projected["zero_cell_count"], 0,
                                       case["case_id"])
                    self.assertIs(projected["empty"], False, case["case_id"])
        self.assertEqual(total, 81)
        self.assertEqual(tested, 73)


if __name__ == "__main__":
    import unittest
    unittest.main()


def test_count_cells_reject_non_integer_or_negative_values():
    for aggregate, field in (("count", "*"), ("count_distinct", "region_code")):
        query = {"dataset": DATASET, "select": [
            {"field": field, "aggregate": aggregate, "alias": "n"}]}
        for cell in (-1, True, None, "3", 1.5, 3.0, 9_007_199_254_740_992):
            result = {"structuredContent": {
                "dataset": DATASET,
                "columns": [{"name": "n", "label": "count", "type": "number"}],
                "data": [[cell]], "rows": 1, "page": 1, "has_more": False}}
            assert project_matrix_result(query, result) is None
        result["structuredContent"]["data"] = [[0]]
        assert project_matrix_result(query, result)["rows"] == [{"n": 0}]
