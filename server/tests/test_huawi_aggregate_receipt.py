"""Synthetic contract tests only; do not use real customer result data."""
import copy
import unittest

from app.huawi_aggregate_receipt import (
    count_receipt_metadata, count_result_card, project_count_receipt)
from app.business_results import count_presentation_receipt, project_business_result


def example():
    q = {
        "dataset": "synthetic_table",
        "select": [
            {"field": "*", "aggregate": "count", "alias": "cnt"},
            {"field": "item", "aggregate": "count_distinct", "alias": "kinds"},
        ],
    }
    r = {"structuredContent": {
        "dataset": "synthetic_table", "columns": ["cnt", "kinds"],
        "data": [[17, 5]], "rows": 20, "page": 1, "has_more": False,
    }}
    return q, r


class CountReceiptTests(unittest.TestCase):
    def test_baseline_schema_gap(self):
        q, r = example()
        self.assertIsNone(project_business_result(r))
        self.assertIsNotNone(project_business_result({"structuredContent": {
            "columns": ["cnt"], "rows": [{"cnt": 17}], "total_rows": 1,
        }}))

    def test_positional_counts(self):
        q, r = example()
        out = project_count_receipt(q, r)
        self.assertEqual([x["value"] for x in out["counts"]], [17, 5])
        self.assertEqual(out["page_size"], 20)
        self.assertEqual(out["returned_aggregate_rows"], 1)
        self.assertIs(out["scope_verified"], False)

    def test_named_columns_and_row(self):
        q, r = example()
        p = r["structuredContent"]
        p["columns"] = [{"key": "kinds", "label": "ignored"}, {"key": "cnt"}]
        p["data"] = [{"cnt": 17, "kinds": 5}]
        out = project_count_receipt(q, r)
        self.assertEqual([x["alias"] for x in out["counts"]], ["kinds", "cnt"])
        self.assertEqual([x["value"] for x in out["counts"]], [5, 17])
        self.assertNotIn("ignored", str(out))

    def test_observed_matrix_contract(self):
        q, r = example()
        r["structuredContent"]["columns"] = [
            {"name": "kinds", "label": "Synthetic display label", "type": "int"},
            {"name": "cnt", "label": "Synthetic count", "type": "int"},
        ]
        r["structuredContent"]["data"] = [[5, 17]]
        receipt = project_count_receipt(q, r)
        self.assertEqual([(x["alias"], x["value"]) for x in receipt["counts"]],
                         [("kinds", 5), ("cnt", 17)])
        self.assertFalse(receipt["scope_verified"])
        self.assertNotIn("Synthetic display label", str(receipt))

    def test_observed_matrix_malformed_columns_rejected(self):
        q, r = example()
        valid = [{"name": "cnt", "label": "Count", "type": "int"},
                 {"name": "kinds", "label": "Distinct", "type": "int"}]
        for invalid in (
            [{"name": "cnt", "label": "Count", "type": "int", "key": "other"}, valid[1]],
            [{"name": "cnt", "label": {}, "type": "int"}, valid[1]],
            [valid[0], {"name": "cnt", "label": "Duplicate", "type": "int"}],
            [valid[0], {"name": "not_requested", "label": "Other", "type": "int"}],
        ):
            r["structuredContent"]["columns"] = invalid
            self.assertIsNone(project_count_receipt(q, r))

    def test_zero_cells_not_empty(self):
        q, r = example()
        r["structuredContent"]["data"] = [[0, 0]]
        self.assertEqual([x["value"] for x in project_count_receipt(q, r)["counts"]], [0, 0])
        r["structuredContent"]["data"] = []
        self.assertIsNone(project_count_receipt(q, r))

    def test_total_not_count(self):
        q, r = example()
        r["structuredContent"]["total"] = 1
        self.assertEqual(project_count_receipt(q, r)["counts"][0]["value"], 17)
        for total in (0, 17, True, "1", None):
            with self.subTest(total=total):
                r["structuredContent"]["total"] = total
                self.assertIsNone(project_count_receipt(q, r))

    def test_unsafe_or_ambiguous_requests(self):
        q, r = example()
        changes = [
            ("dataset", "other"),
            ("dataset", "invalid table"),
            ("group_by", ["item"]),
            ("filters", [{"field": "item"}]),
            ("order_by", ["item"]),
            ("page", 2),
            ("page", True),
            ("rows", 1),
            ("include_total", "true"),
            ("secret", "synthetic-only"),
            ("select", []),
            ("select", [{"field": "*", "aggregate": "sum", "alias": "cnt"}]),
            ("select", [{"field": "*", "aggregate": "count_distinct", "alias": "cnt"}]),
            ("select", [{"field": "*", "aggregate": "count"}]),
            ("select", [{"field": "*", "aggregate": "count", "alias": "cnt"}] * 2),
            ("select", [{"field": "*", "aggregate": "count", "alias": "__proto__"}]),
            ("select", [{"field": "*", "aggregate": "count", "alias": "cnt", "extra": 1}]),
        ]
        for key, val in changes:
            with self.subTest(key=key, val=val):
                a = copy.deepcopy(q)
                a[key] = val
                self.assertIsNone(project_count_receipt(a, r))

    def test_unsafe_or_ambiguous_payloads(self):
        q, r = example()
        cases = [
            ("dataset", "other"),
            ("has_more", True),
            ("has_more", 0),
            ("has_more", None),
            ("page", 2),
            ("page", True),
            ("rows", 0),
            ("rows", True),
            ("rows", 1001),
            ("rows", [{"cnt": 17}]),
            ("truncated", True),
            ("extra_data", "synthetic-only"),
            ("columns", ["cnt", "cnt"]),
            ("columns", ["cnt", "other"]),
            ("columns", [{"name": "cnt"}, {"name": "kinds"}]),
            ("columns", ["cnt"]),
            ("data", [[17, 5], [23, 1]]),
            ("data", [[17]]),
            ("data", [[17, 5, 23]]),
            ("data", [{"cnt": 17}]),
            ("data", [{"cnt": 17, "kinds": 5, "detail": "synthetic-only"}]),
            ("data", [[-1, 5]]),
            ("data", [[True, 5]]),
            ("data", [[17.0, 5]]),
            ("data", [["17", 5]]),
            ("data", [[9_007_199_254_740_992, 5]]),
            ("data", [[None, 5]]),
            ("data", ["truncated"]),
        ]
        for key, val in cases:
            with self.subTest(key=key, val=val):
                x = copy.deepcopy(r)
                x["structuredContent"][key] = val
                self.assertIsNone(project_count_receipt(q, x))

    def test_error_envelopes(self):
        q, r = example()
        for change in ({"isError": True}, {"is_error": True}, {"structured_content": {}}, {"isError": 0}):
            with self.subTest(change=change):
                self.assertIsNone(project_count_receipt(q, {**r, **change}))

    def test_result_card_reuses_canonical_projection_without_tool_labels(self):
        q, r = example()
        r["structuredContent"]["columns"] = [
            {"name": "kinds", "label": "Synthetic hidden label", "type": "int"},
            {"name": "cnt", "label": "Synthetic hidden count", "type": "int"},
        ]
        r["structuredContent"]["data"] = [[5, 17]]
        aggregate = count_receipt_metadata(q, r, ["host_source"])
        card = aggregate["business_result"]
        self.assertEqual(card["schema_version"], "business-result/v1")
        self.assertEqual([column["key"] for column in card["columns"]], ["kinds", "cnt"])
        self.assertEqual(card["rows"], [{"kinds": 5, "cnt": 17}])
        self.assertEqual(card["returned_rows"], 1)
        self.assertIsNone(card["total_rows"])
        self.assertIsNone(card["complete"])
        self.assertEqual(card["source_ids"], ["host_source"])
        self.assertTrue(card["warnings"])
        self.assertNotIn("Synthetic hidden", str(card))

    def test_zero_cells_are_reported_and_empty_data_is_not(self):
        q, r = example()
        r["structuredContent"]["data"] = [[0, 0]]
        aggregate = count_receipt_metadata(q, r)
        self.assertEqual(aggregate["business_result"]["rows"], [{"cnt": 0, "kinds": 0}])
        r["structuredContent"]["data"] = []
        self.assertIsNone(count_receipt_metadata(q, r))

    def test_canonical_table_projection_still_wins(self):
        q, _ = example()
        canonical = {"structuredContent": {
            "table": {"columns": [{"key": "cnt", "label": "Count"}],
                      "rows": [{"cnt": 17}]},
        }}
        self.assertIsNotNone(project_business_result(canonical))
        self.assertIsNone(count_receipt_metadata(q, canonical))

    def test_count_presentation_states_values_and_limits(self):
        q, r = example()
        text = count_presentation_receipt(project_count_receipt(q, r))
        self.assertIn("cnt=17", text)
        self.assertIn("kinds=5", text)
        self.assertIn("不是独立真值", text)
        self.assertIn("不得改称为记录数", text)
        self.assertNotIn("已验证", text)

    def test_result_card_rejects_a_tampered_receipt(self):
        q, r = example()
        receipt = project_count_receipt(q, r)
        for change in ({"counts": []}, {"counts": [{"alias": "cnt"}]},
                       {"counts": [{"alias": "cnt", "aggregate": "sum", "value": 17}]}):
            with self.subTest(change=change):
                self.assertIsNone(count_result_card({**receipt, **change}))


if __name__ == "__main__":
    unittest.main()
