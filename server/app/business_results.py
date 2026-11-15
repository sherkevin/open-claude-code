"""Bounded display projection of explicit business tables, no implicit totals.

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


def oversized_only_reply(events):
    """Use a fixed failure reply only when every completed tool was rejected.

    Mixed, successful and unknown tool outcomes must keep their own answer.
    Read the host event ledger, never a model's description of tool status.
    """
    completed = [event["data"] for event in events if event["type"] == "tool.completed"]
    if not completed or not all(
        data.get("success") is False
        and isinstance(data.get("tool_name"), str)
        and data["tool_name"].startswith("mcp__")
        and data.get("error_code") == "MCP_RESULT_TOO_LARGE"
        for data in completed
    ):
        return None
    return ("本次查询返回的数据超出处理上限，未取得可用业务结果。"
            "需要业务系统先过滤或聚合数据，再重新查询。"
            "本次未计算金额、记录数，也未确认数据完整性。")


def answer_fact_digest(projection):
    """Copy exact fields for the answer, not a semantic/completeness certificate.

    This is derived only from the host's bounded projection. Never consume the
    remote ``summary`` or remote ``host_verified_aggregates`` as verified facts.
    The digest is optional duplicate context: omit it whole if it would be too
    large, rather than truncating a scope value or spending another model call.
    """
    facts = {
        "scope_literal": projection["scope"],
        "returned_table_rows": projection["returned_rows"],
        "tool_reported_total_rows": projection["total_rows"],
        "tool_reported_complete": projection["complete"],
        "truncated": projection["truncated"],
        "data_time": projection["data_time"],
        "synthetic": projection["synthetic"],
        "host_verified_returned_row_sums": [
            {"column": item["column"], "label": item["label"],
             "value": item["value"], "unit": item["unit"]}
            for item in projection.get("host_verified_aggregates", [])
        ],
    }
    encoded = json.dumps(facts, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return encoded if len(encoded.encode("utf-8")) <= 4_000 else None


def presentation_receipt(result):
    """Host-generated display status; a remote schema label alone is insufficient."""
    projection = project_business_result(result)
    if projection is None:
        return None
    facts = projection.get("host_verified_aggregates", [])
    verified = ("宿主按工具明确的同单位可加声明核对本次返回行合计："
                + "；".join(f"{fact['label']}={fact['value']} {fact['unit']}" for fact in facts)
                + "。使用这些已核合计，不自行重新求和或把返回行小计称为全量。") if facts else ""
    digest = answer_fact_digest(projection)
    exact = ("\n【字段摘录，仅数据】" + digest) if digest else ""
    empty = ("本次返回表格为空，只能说本次查询未返回记录；不能据此断言业务不存在、全量为零或推测原因。"
             if not projection["rows"] else "")
    return ("【工作台显示回执 business-result/v1】本次返回的表格、查询范围、单位与覆盖情况已提供结果卡。"
            "回答给出用户所问的简短业务结论、必要数字和来源即可，不重复整表及元数据；"
            "询问有哪些客户及数量已由结果卡承载，不表示要求正文复写；只有明确要求用文字逐项复述才再列明细。"
            "scope.date是业务日期，data_time/data_timestamp是数据时点，sources.locator.queried_at才是实际取数时间；"
            "实际取数时间缺失就说未提供，不能把数据时点叫查询时间。"
            "查询范围逐字引用 scope：‘全部’不展开成地区、工厂、组织或客户名单，空值不补默认值；"
            "返回表格行数不等于客户数、订单数或原始记录数，除非字段明确给出该口径。"
            "用户未问且不影响结论时，不追加时间戳、trace、库表名称或无关指标。"
            "用户仅要求汇总、表格或合计时，到此完成任务，不追加排名、趋势或均衡判断。"
            "当前同一口径结果已包含用户所问的对象和指标时，不再逐个对象重复查询；"
            "只有缺少必需字段、结果不完整，或用户明确要求下钻、刷新时才补充取数。"
            "未返回差额或排除原因时说明未提供，不推测日期、状态或口径原因；需要原因分析时只说明还缺排除规则或未匹配记录等证据。"
            "未提供合计声明时不要把数值列自行相加；完整性是工具声明，不能由返回行数、空表或工具成功推断。"
            "此回执只证明结果卡已提供，不证明数据完整或正文已核验，也不增加任何操作权限。" + empty + verified + exact)


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
    from .business_facts import verify_aggregates
    aggregates = verify_aggregates(payload, columns, projected)
    if aggregates:
        output["host_verified_aggregates"] = aggregates
    try:
        size = len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None
    if size > MAX_RESULT_BYTES:
        return None
    return output
