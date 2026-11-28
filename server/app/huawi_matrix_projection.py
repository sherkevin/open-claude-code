"""Strict projection of a bounded native Huawi query matrix envelope.

This module is deliberately isolated: it is a pure function, has no
runtime/bridge wiring and never calls a tool, a model or the network. It does
not replace :mod:`app.huawi_aggregate_receipt`; the existing ungrouped count
projector stays unchanged.

Only the observed result shape qualifies::

    structuredContent:
        dataset: str
        columns: [{name, label, type}, ...]
        data:    [[scalar, ...], ...]   positional matrix
        rows:    int                    page size, not len(data)
        page:    int
        has_more: bool
        total:   optional int           output row/group count, not an
                                        aggregate cell

The projection is bound to the same approved top-level query arguments:
``dataset`` must match exactly, supplied ``page``/``rows`` must match the
envelope, ``include_total=false`` may not carry ``total``, and ``select`` is
resolved item by item with the observed structured wire shape::

    {"field": "synthetic_field"}                               plain field
    {"field": "*", "aggregate": "count", "alias": "record_count"}
    {"field": "synthetic_field", "aggregate": "count_distinct",
     "alias": "distinct_count"}

Each returned ``columns[].name`` must equal the selected field name or its
explicit alias, in order, with no extra, missing or duplicate column. Only
``count`` and ``count_distinct`` are accepted; any other aggregate is
unproven and fails closed. A ``group_by`` field must be an actually selected
plain field, never an aggregate alias. ``order_by`` refers to output names
or aliases, but nested filter operators and sort directions are not
interpreted. No SQL strings, missing aggregate fields or missing aliases are
accepted.

Malformed, inconsistent, unknown-schema or ambiguous envelopes return
``None``. A valid zero cell is not an empty result, an empty page is not a
global zero, and ``total`` is never re-labelled as a record count or an
aggregate value. ``scope_verified`` is always false.

All examples and tests for filtered/grouped/paginated output are synthetic
candidates until a separate exact contract is established. They are not
customer support, production acceptance or real-data verification.
"""
from __future__ import annotations

import json
import math
import re

MAX_ROWS = 200
MAX_COLUMNS = 24
MAX_PAGE_SIZE = 1_000
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_CELL_CHARS = 2_000
MAX_RESULT_BYTES = 96_000

_FIELD_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}\Z")
_DATASET_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,99}\Z")
_UNSAFE_NAMES = {"__proto__", "constructor", "prototype"}
_AGGREGATES = {"count", "count_distinct"}

_QUERY_KEYS = {
    "dataset", "select", "filters", "group_by", "order_by", "page", "rows",
    "include_total",
}
_RESULT_KEYS = {"content", "structuredContent", "isError", "is_error"}
_PAYLOAD_KEYS = {
    "dataset", "columns", "data", "rows", "page", "has_more", "total",
}
_REQUIRED_PAYLOAD_KEYS = {
    "dataset", "columns", "data", "rows", "page", "has_more",
}


def _field(value):
    return (type(value) is str and _FIELD_NAME.fullmatch(value) is not None
            and value not in _UNSAFE_NAMES)


def _dataset(value):
    return (type(value) is str and _DATASET_CODE.fullmatch(value) is not None
            and value not in _UNSAFE_NAMES)


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _scalar(value):
    """Accept only JSON scalars that survive the host's numeric bounds."""
    if value is None:
        return True
    kind = type(value)
    if kind is str:
        return len(value) <= MAX_CELL_CHARS
    if kind is bool:
        return True
    if kind is int:
        return abs(value) <= MAX_SAFE_INTEGER
    if kind is float:
        return math.isfinite(value)
    return False


def _is_zero(value):
    return ((type(value) is int and value == 0)
            or (type(value) is float and value == 0.0))


def _select(value):
    """Resolve the observed structured select into exact output column names.

    Only two item shapes are observed: a plain field and an aggregate with an
    explicit alias. The output name is the field for a plain item and the
    alias for an aggregate item. Duplicate output names, alias collisions
    with selected fields, SQL strings, missing aggregate fields, missing
    aliases, ``*`` outside COUNT and any aggregate other than COUNT or
    COUNT DISTINCT fail closed.
    """
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_COLUMNS:
        return None
    outputs = []
    plain_fields = set()
    aggregates = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        keys = set(entry)
        if keys == {"field"}:
            field = entry["field"]
            if not _field(field):
                return None
            name = field
            plain_fields.add(field)
        elif keys == {"field", "aggregate", "alias"}:
            field = entry["field"]
            aggregate = entry["aggregate"]
            alias = entry["alias"]
            if type(aggregate) is not str or aggregate not in _AGGREGATES:
                return None
            if not _field(alias):
                return None
            if aggregate == "count":
                if field != "*":
                    return None
                source = "*"
            else:
                if not _field(field):
                    return None
                source = field
            name = alias
            aggregates.append({
                "field": source, "aggregate": aggregate, "alias": alias,
            })
        else:
            return None
        if name in outputs:  # duplicate column or alias collision
            return None
        outputs.append(name)
    return {
        "outputs": outputs,
        "plain_fields": plain_fields,
        "aggregates": aggregates,
    }


def _normalized_query(query):
    if not isinstance(query, dict) or set(query) - _QUERY_KEYS:
        return None
    dataset = query.get("dataset")
    if not _dataset(dataset):
        return None
    select = _select(query.get("select"))
    if select is None:
        return None

    filters = query.get("filters", [])
    group_by = query.get("group_by", [])
    order_by = query.get("order_by", [])
    if not isinstance(filters, list):
        return None
    if not isinstance(group_by, list) or not isinstance(order_by, list):
        return None
    if any(not _field(field) for field in group_by):
        return None  # nested group encoding is unobserved; fail closed
    if len(set(group_by)) != len(group_by):
        return None
    # A group field must be an actually selected plain field. Grouping by an
    # aggregate alias or by an unselected name is ambiguous, so fail closed.
    if any(field not in select["plain_fields"] for field in group_by):
        return None
    # order_by uses output names/aliases. The exact direction vocabulary is
    # not proven, so only a bounded string is checked, never interpreted.
    for item in order_by:
        if not isinstance(item, dict) or "field" not in item:
            return None
        if set(item) - {"field", "direction"}:
            return None
        if item["field"] not in select["outputs"]:
            return None
        if "direction" in item:
            direction = item["direction"]
            if type(direction) is not str or not 1 <= len(direction) <= 32:
                return None

    page = query.get("page")
    if "page" in query and not _integer(page, 1, MAX_SAFE_INTEGER):
        return None
    rows = query.get("rows")
    if "rows" in query and not _integer(rows, 1, MAX_PAGE_SIZE):
        return None
    include_total = query.get("include_total")
    if "include_total" in query and type(include_total) is not bool:
        return None
    return {
        "dataset": dataset,
        "select_outputs": select["outputs"],
        "select_aggregates": select["aggregates"],
        "filters_present": bool(filters),
        "group_by": list(group_by),
        "order_by_present": bool(order_by),
        "page": page,
        "rows": rows,
        "include_total": include_total,
    }


def _payload(result):
    if not isinstance(result, dict) or set(result) - _RESULT_KEYS:
        return None
    if result.get("isError", False) is not False:
        return None
    if result.get("is_error", False) is not False:
        return None
    if "content" in result and not isinstance(result["content"], list):
        return None
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        return None
    keys = set(payload)
    if keys - _PAYLOAD_KEYS or not _REQUIRED_PAYLOAD_KEYS <= keys:
        return None
    return payload


def _columns(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_COLUMNS:
        return None
    columns = []
    names = set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"name", "label", "type"}:
            return None
        name, label, type_name = entry["name"], entry["label"], entry["type"]
        if not _field(name) or name in names:
            return None
        if type(label) is not str or len(label) > 200:
            return None
        # The native type vocabulary is not established. Only the fact that
        # the declaration is a non-empty string is checked; cells are never
        # coerced according to this value.
        if type(type_name) is not str or not 1 <= len(type_name) <= 100:
            return None
        names.add(name)
        columns.append({"name": name, "label": label, "type": type_name})
    return columns


def _data(value, width):
    if not isinstance(value, list) or len(value) > MAX_ROWS:
        return None
    rows = []
    for row in value:
        if not isinstance(row, list) or len(row) != width:
            return None
        if any(not _scalar(cell) for cell in row):
            return None
        rows.append(list(row))
    return rows


def _consistent(returned, page, page_size, has_more, total, grouped):
    """Reject envelopes whose total contradicts page state.

    The page-index origin is not observed. Checks below hold for either a
    0-based or 1-based page selector. For grouped output the total basis may
    be records or groups, so only lower-bound checks are safe there. This
    remains intentionally incomplete: pagination completeness still requires
    separate contract proof.
    """
    if total is None:
        return not (returned == 0 and has_more)
    if total < returned:
        return False
    if has_more:
        # More pages exist, so the global output count must exceed this page.
        return total > returned
    if grouped:
        # total may count returned groups or underlying records; both are at
        # least the returned group count. page/rows cannot validate the rest.
        return True
    if page == 1:
        # A single-page first-page response cannot hide or invent output rows.
        return total == returned
    # A later page cannot contain every output row, and a last page cannot
    # need more prior rows than the largest possible 0/1-based prefix.
    return returned < total <= returned + page * page_size


def _warnings(rows_field, returned, has_more, total, grouped, grouped_short,
              filtered, empty, zero_cells):
    warnings = []
    if has_more:
        warnings.append("本次只返回当前页；has_more=true 表示仍有未返回页，不能把本页当作全量。")
    else:
        warnings.append("has_more=false 只表示本次查询没有后续页，不证明数据源完整或权限范围。")
    if rows_field != returned:
        warnings.append(
            f"rows={rows_field} 是页大小，不是返回行数；本次 returned_rows={returned} 取 len(data)。")
    if total is None:
        warnings.append("工具未提供 total；未知不等于 0，不能据此判断全量为零。")
    elif grouped:
        warnings.append(
            "total 可能是输出组数或记录数，口径未声明；它不是聚合单元格，不能直接当作记录数或完整组数。")
    else:
        warnings.append("total 按输出行数解释，不是聚合单元格；输出行数也不等于底层记录数。")
    if empty:
        warnings.append("空页/空结果只表示本次查询未返回行，不代表数据集为空、业务为零或权限范围。")
    elif zero_cells:
        warnings.append("数值 0 是已返回单元格，不是空结果。")
    if grouped:
        warnings.append("分组结果只覆盖本页输出组；分组聚合值未经宿主独立核验，不能据此外推全量。")
    if grouped_short:
        warnings.append("若 total 按输出组数计，已返回组数少于工具报告，本页不是完整分组结果。")
    if filtered:
        warnings.append("过滤条件已作为同一已批准请求参数绑定；过滤后的业务含义与覆盖范围未经独立核验。")
    return warnings


def project_matrix_result(query, result):
    """Return a bounded matrix projection or ``None``.

    ``query`` is the same approved top-level query argument object that
    produced ``result``. ``result`` is the MCP result envelope. There is no
    fallback to ``structured_content``, text content, model output or any
    second representation.
    """
    normalized = _normalized_query(query)
    if normalized is None:
        return None
    payload = _payload(result)
    if payload is None:
        return None
    if payload["dataset"] != normalized["dataset"]:
        return None

    columns = _columns(payload["columns"])
    if columns is None:
        return None
    # Exact, ordered select-to-column binding: every returned column name
    # must be the selected field or its explicit alias, with no extra,
    # missing, reordered or duplicate column.
    if [column["name"] for column in columns] != normalized["select_outputs"]:
        return None

    rows_field = payload["rows"]
    page = payload["page"]
    has_more = payload["has_more"]
    if not _integer(rows_field, 1, MAX_PAGE_SIZE):
        return None
    if not _integer(page, 1, MAX_SAFE_INTEGER):
        return None
    if type(has_more) is not bool:
        return None

    data = _data(payload["data"], len(columns))
    if data is None or len(data) > rows_field:
        return None
    # COUNT and COUNT DISTINCT are nonnegative safe integers, not arbitrary
    # scalars. Reject malformed aggregate cells even in an uncertified table.
    aggregate_indices = [normalized["select_outputs"].index(item["alias"])
                         for item in normalized["select_aggregates"]]
    if any(not _integer(row[index], 0, MAX_SAFE_INTEGER)
           for row in data for index in aggregate_indices):
        return None

    total_present = "total" in payload
    total = payload.get("total")
    if total_present and not _integer(total, 0, MAX_SAFE_INTEGER):
        return None

    # Bind only the argument facts the envelope can actually prove.
    if normalized["rows"] is not None and normalized["rows"] != rows_field:
        return None
    if normalized["page"] is not None and normalized["page"] != page:
        return None
    if normalized["include_total"] is False and total_present:
        return None
    group_by = normalized["group_by"]
    returned = len(data)
    grouped = bool(group_by)
    if not _consistent(returned, page, rows_field, has_more, total, grouped):
        return None
    if returned == 0 and has_more:
        return None  # an empty page cannot announce a further page
    if returned == 0 and page == 1 and total is not None and total > 0:
        return None  # a first empty page cannot report positive output

    zero_cells = sum(1 for row in data for cell in row if _is_zero(cell))
    empty = returned == 0

    if empty:
        if total == 0:
            classification = "valid_empty_result"
            empty_kind = "query_reported_zero_output_rows"
        elif total is None and page == 1:
            classification = "valid_empty_result"
            empty_kind = "empty_first_page_total_unknown"
        elif page > 1 and total is not None:
            classification = "valid_empty_page"
            empty_kind = "beyond_last_page_not_dataset_zero"
        else:
            classification = "valid_empty_page"
            empty_kind = "empty_page_total_unknown"
    else:
        classification = "valid_table"
        empty_kind = None

    if total is None:
        total_basis = "unknown_not_zero"
        total_status = "unknown_not_zero"
    elif grouped:
        total_basis = "groups_consistent_but_not_declared"
        total_status = "reported_output_count_basis_not_declared"
    else:
        total_basis = "output_rows"
        total_status = "reported_output_row_count"

    if has_more:
        page_coverage = "more_pages_not_returned"
    else:
        page_coverage = "no_further_page_reported"
    if not grouped:
        group_coverage = None
        if total is not None and total == returned and not has_more:
            output_coverage = "all_output_rows_reported_scope_not_verified"
        elif total is not None and total > returned:
            output_coverage = "incomplete_relative_to_reported_output_rows"
        else:
            output_coverage = "output_row_total_unknown"
    else:
        if has_more:
            group_coverage = "returned_page_of_groups_not_all_groups"
            output_coverage = "incomplete_relative_to_possible_later_pages"
        elif total is None:
            group_coverage = "output_group_total_unknown"
            output_coverage = "output_group_total_unknown"
        elif total == returned:
            group_coverage = "total_consistent_with_all_returned_groups_basis_not_declared"
            output_coverage = "total_consistent_with_returned_output_basis_not_declared"
        else:
            group_coverage = "returned_groups_incomplete_if_total_counts_groups"
            output_coverage = "incomplete_or_total_basis_not_declared"

    grouped_short = grouped and total is not None and returned < total
    warnings = _warnings(
        rows_field, returned, has_more, total, grouped, grouped_short,
        normalized["filters_present"], empty, zero_cells)
    if normalized["select_aggregates"]:
        warnings.append(
            "聚合列按显式 alias 与本请求 select 绑定；聚合值未经宿主独立核验，"
            "不证明底层记录真值或完整范围。")
    projection = {
        "schema_version": "huawi-matrix-projection/v1",
        "classification": classification,
        "support_status": "synthetic_candidate_not_product_support",
        "dataset": payload["dataset"],
        "columns": columns,
        "rows": [dict(zip([column["name"] for column in columns], row))
                 for row in data],
        "returned_rows": returned,
        "page": page,
        "page_size": rows_field,
        "rows_field_is_page_size": True,
        "has_more": has_more,
        "page_coverage": page_coverage,
        "output_coverage": output_coverage,
        "pagination_completeness": "requires_separate_proof",
        "global_coverage": False,
        "complete": None,
        "scope_verified": False,
        "total": total if total_present else None,
        "total_basis": total_basis,
        "total_status": total_status,
        "total_is_aggregate_cell": False,
        "total_is_record_count": False,
        "total_may_count_output_groups": grouped and total_present,
        "total_basis_ambiguous": grouped and total_present,
        "empty": empty,
        "empty_kind": empty_kind,
        "zero_cell_count": zero_cells,
        "zero_kind": "zero_cells_present_not_empty" if zero_cells else None,
        "grouped": grouped,
        "group_by": list(group_by),
        "group_coverage": group_coverage,
        "filtered": normalized["filters_present"],
        "ordered": normalized["order_by_present"],
        "type_vocabulary_verified": False,
        "select_column_binding": "exact_selected_field_or_alias",
        "selected_output_columns": list(normalized["select_outputs"]),
        "aggregate_columns": [dict(item) for item in normalized["select_aggregates"]],
        "has_aggregate": bool(normalized["select_aggregates"]),
        "aggregate_value_verified": False,
        "warnings": warnings,
    }
    try:
        encoded = json.dumps(
            projection, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if len(encoded) > MAX_RESULT_BYTES:
        return None
    return projection
