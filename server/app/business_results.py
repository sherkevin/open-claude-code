"""Bounded display projection of explicit business tables, never a calculator.

Only structured MCP data is accepted. Coverage, dates, units and synthetic
labels are tool declarations; no completeness or total is inferred from rows.
"""
from __future__ import annotations

import json
import math
import re

MAX_ROWS = 200
MAX_COLUMNS = 24
MAX_RESULT_BYTES = 48_000
MAX_CELL_CHARS = 2_000
_UNSAFE_KEYS = {"__proto__", "constructor", "prototype"}


def scalar(value):
    return (value is None or type(value) in (str, bool)
            or type(value) is int and abs(value) <= 9_007_199_254_740_991
            or type(value) is float and math.isfinite(value))


def table_payload(structured):
    if not isinstance(structured, dict):
        return None
    table = structured.get("table")
    if isinstance(table, dict):
        payload = {**structured, **table}
    elif isinstance(table, list):
        payload = {**structured, "rows": table}
    else:
        payload = structured
    rows = payload.get("rows", payload.get("records"))
    return (payload, rows) if isinstance(rows, list) else None


def _short(value, maximum):
    return value if isinstance(value, str) and len(value) <= maximum else None


def project_business_result(result, source_ids=()):
    """Return an all-or-nothing scalar table projection; no partial row slicing."""
    if not isinstance(result, dict) or result.get("isError") or result.get("is_error"):
        return None
    pair = table_payload(result.get("structuredContent", result.get("structured_content")))
    if pair is None:
        return None
    payload, rows = pair
    if len(rows) > MAX_ROWS or any(not isinstance(row, dict) for row in rows):
        return None
    declarations = payload.get("columns")
    if declarations is None:
        # Column names are literal returned keys, not guessed business labels.
        keys = list(dict.fromkeys(key for row in rows for key in row))
        declarations = [{"key": key, "label": key} for key in keys]
    if not isinstance(declarations, list) or not 1 <= len(declarations) <= MAX_COLUMNS:
        return None
    columns = []
    for entry in declarations:
        if isinstance(entry, str):
            entry = {"key": entry, "label": entry}
        if not isinstance(entry, dict):
            return None
        key = _short(entry.get("key"), 100)
        if not key or key in _UNSAFE_KEYS or any(column["key"] == key for column in columns):
            return None
        label = _short(entry.get("label", key), 200)
        if label is None:
            return None
        column = {"key": key, "label": label}
        if isinstance(entry.get("type"), str) and entry["type"] in {"number", "string", "boolean"}:
            column["type"] = entry["type"]
        if entry.get("unit") is not None:
            unit = _short(entry["unit"], 100)
            if unit is None:
                return None
            column["unit"] = unit
        columns.append(column)
    projected = []
    for row in rows:
        cells = {}
        for column in columns:
            value = row.get(column["key"])
            if not scalar(value) or isinstance(value, str) and len(value) > MAX_CELL_CHARS:
                return None
            cells[column["key"]] = value
        projected.append(cells)
    scope = payload.get("scope")
    if scope is not None and (not isinstance(scope, dict) or len(scope) > MAX_COLUMNS
                             or any(not isinstance(k, str) or len(k) > 100 or k in _UNSAFE_KEYS
                                    or not scalar(v) or isinstance(v, str) and len(v) > 500
                                    for k, v in scope.items())):
        return None
    total = payload.get("total_rows")
    total = total if type(total) is int and 0 <= total <= 9_007_199_254_740_991 else None
    complete = payload.get("complete") if type(payload.get("complete")) is bool else None
    truncated = payload.get("truncated") is True
    warnings = payload.get("warnings", [])
    warnings = [item for item in warnings if isinstance(item, str) and len(item) <= 500][:20] if isinstance(warnings, list) else []
    if truncated:
        complete = False
    if total is not None and total < len(rows):
        warnings.append("工具报告的匹配总量少于已返回行数，请核对业务口径。")
        complete = None
    if complete is True and total is not None and total > len(rows):
        warnings.append("工具声明完整，但匹配总量大于已返回量；完整性待核。")
        complete = None
    output = {
        "schema_version": "business-result/v1",
        "title": _short(payload.get("title"), 200) or "业务查询结果",
        "columns": columns, "rows": projected, "scope": scope or {},
        "data_time": _short(payload.get("data_time"), 100),
        "returned_rows": len(projected), "total_rows": total, "complete": complete,
        "synthetic": payload.get("synthetic") if type(payload.get("synthetic")) is bool else None,
        "truncated": truncated, "warnings": warnings,
        # Only host-registered, authorized source IDs are exposed.
        "source_ids": [item for item in source_ids if isinstance(item, str)
                       and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item)][:30],
    }
    summary = _short(payload.get("summary"), 2_000)
    if summary is not None:
        output["summary"] = summary
    try:
        size = len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if size > MAX_RESULT_BYTES:
        return None
    return output
