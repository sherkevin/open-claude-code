"""Strict projection of an explicitly bound, ungrouped ERP count receipt.

The runtime calls :func:`count_receipt_metadata` only after the same MCP call
has passed the existing tool authorization, approval and active-run checks.
Only a real ``structuredContent`` result with the observed name/label/type
columns and positional data matrix qualifies; model or tool text is never
parsed into counts. The positional matrix is observed in the customer test
trace; other representations remain synthetic compatibility hypotheses.
This does not establish source completeness, business meaning or access scope.
"""
import re

from .business_results import MAX_COLUMNS, project_business_result, scalar

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}\Z")
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_REQUEST_KEYS = {"dataset", "select", "filters", "group_by", "order_by",
                 "page", "rows", "include_total"}
_RESULT_KEYS = {"dataset", "columns", "data", "rows", "page",
                "has_more", "total", "truncated"}


def _name(value):
    return type(value) is str and _NAME.fullmatch(value) is not None


def _int(value, low, high):
    return type(value) is int and low <= value <= high and scalar(value)


def project_count_receipt(query, result):
    """Return an exact count projection or None; never infer missing values."""
    if not isinstance(query, dict) or set(query) - _REQUEST_KEYS:
        return None
    dataset = query.get("dataset")
    if not _name(dataset):
        return None
    if any(query.get(key, []) != [] for key in ("filters", "group_by", "order_by")):
        return None  # filtered/grouped counts require a separate verified contract
    if not _int(query.get("page", 1), 1, 1):
        return None
    if "rows" in query and not _int(query["rows"], 1, 1000):
        return None
    if "include_total" in query and type(query["include_total"]) is not bool:
        return None
    selects = query.get("select")
    if not isinstance(selects, list) or not 1 <= len(selects) <= min(8, MAX_COLUMNS):
        return None
    wanted = {}
    for item in selects:
        if not isinstance(item, dict) or set(item) != {"field", "aggregate", "alias"}:
            return None
        field, aggregate, alias = item["field"], item["aggregate"], item["alias"]
        if aggregate not in ("count", "count_distinct") or not _name(alias):
            return None
        if not (_name(field) or field == "*" and aggregate == "count"):
            return None
        if alias in wanted or alias in {"__proto__", "constructor", "prototype"}:
            return None
        wanted[alias] = {"field": field, "aggregate": aggregate}
    if not isinstance(result, dict):
        return None
    if any(result.get(key, False) is not False for key in ("isError", "is_error")):
        return None
    if "structured_content" in result:
        return None  # deliberately no competing envelope or fallback
    payload = result.get("structuredContent")
    if not isinstance(payload, dict) or set(payload) - _RESULT_KEYS:
        return None
    if payload.get("dataset") != dataset or payload.get("has_more") is not False:
        return None
    if payload.get("truncated", False) is not False:
        return None
    if not _int(payload.get("page"), 1, 1) or not _int(payload.get("rows"), 1, 1000):
        return None
    if "rows" in query and query["rows"] != payload["rows"]:
        return None
    if "total" in payload and not _int(payload["total"], 1, 1):
        return None  # total is not the COUNT cell; conflicting coverage is rejected
    columns = payload.get("columns")
    if not isinstance(columns, list) or len(columns) != len(wanted):
        return None
    keys = []
    for entry in columns:
        if isinstance(entry, dict):
            # The customer's actual structured MCP query has columns with
            # name/label/type, and data as a positional matrix. Only the
            # exact name binds a count cell to its approved select alias.
            if set(entry) == {"name", "label", "type"}:
                if not _name(entry.get("name")) or not all(
                    type(entry.get(k)) is str and len(entry[k]) <= 200
                    for k in ("label", "type")
                ):
                    return None
                entry = entry["name"]
            else:
                # Other supported shapes remain synthetic compatibility
                # candidates; they are not evidence of the live protocol.
                if set(entry) - {"key", "label"} or not _name(entry.get("key")):
                    return None
                entry = entry["key"]
        if not _name(entry) or entry not in wanted or entry in keys:
            return None
        keys.append(entry)
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1:
        return None
    row = data[0]
    if isinstance(row, dict):
        if set(row) != set(keys):
            return None
        values = [row[key] for key in keys]
    elif isinstance(row, list) and len(row) == len(keys):
        values = row
    else:
        return None
    if not all(_int(value, 0, _MAX_SAFE_INTEGER) for value in values):
        return None
    return {
        "dataset": dataset,
        "receipt_kind": "tool_reported_count_cells",
        "returned_aggregate_rows": 1,
        "page_size": payload["rows"],
        "scope_verified": False,
        "counts": [
            {"alias": alias, **wanted[alias], "value": value}
            for alias, value in zip(keys, values)
        ],
    }


def count_result_card(receipt, source_ids=()):
    """Reuse the canonical result-card projection without widening its schema.

    Only aliases and safe integer cells leave this module. The tool's display
    labels stay behind; ``complete``/``total_rows`` remain unknown because an
    aggregate cell says nothing about underlying record completeness.
    """
    counts = receipt.get("counts") if isinstance(receipt, dict) else None
    if not isinstance(counts, list) or not counts:
        return None
    for item in counts:
        if (not isinstance(item, dict) or item.get("aggregate") not in ("count", "count_distinct")
                or not _name(item.get("alias"))
                or not _int(item.get("value"), 0, _MAX_SAFE_INTEGER)):
            return None
    payload = {
        "title": "工具报告聚合值",
        "columns": [{"key": item["alias"], "label": item["alias"], "type": "number"}
                    for item in counts],
        "rows": [{item["alias"]: item["value"] for item in counts}],
        "warnings": [
            "该数值由业务工具在同一次已授权查询中报告；宿主仅核对结构、唯一别名、"
            "单行、请求绑定与当前任务有效性，不是独立真值，也不证明数据完整性或权限范围。"
        ],
    }
    return project_business_result({"structuredContent": payload}, source_ids)


def count_receipt_metadata(query, result, source_ids=()):
    """Return the strict count receipt plus a canonical result card, or None.

    A canonical table projection always wins unchanged, so this path cannot
    reinterpret an existing business-result/v1 payload as an aggregate.
    """
    if project_business_result(result) is not None:
        return None
    receipt = project_count_receipt(query, result)
    if receipt is None:
        return None
    card = count_result_card(receipt, source_ids)
    if card is None:
        return None
    return {"count_receipt": receipt, "business_result": card}
