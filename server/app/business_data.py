"""Bounded, read-only browsing of operator-registered business tables.

The catalog is server-owned, never accepted from a browser. Each table inherits
an existing MCP connection's visibility and enabled state. No SQL/path input is
accepted from the caller; opening the table does not invoke a model.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from .security import AppError

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_CONDITIONS = 12
NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
COMPARISONS = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
EMPTY_OPERATORS = {"is_empty", "is_not_empty"}


def _filter_error(message):
    raise AppError(422, "INVALID_DATA_FILTER", message)


def _column_type(column):
    # Type is operator-owned metadata, never inferred from a field's name.
    if column.get("format") == "currency_cents":
        return "number"
    kind = column.get("type", "text")
    return kind if kind in {"text", "integer", "number", "date", "datetime", "boolean"} else "text"


def _filter_value(value, column, position):
    label = f"第 {position} 条条件的{column['label']}"
    if isinstance(value, bool) and _column_type(column) == "boolean":
        return int(value)
    if type(value) not in (str, int, float):
        _filter_error(f"{label}需要填写有效值")
    text = str(value).strip()
    if not text or len(text) > 100:
        _filter_error(f"{label}不能为空，且不能超过 100 字；查空值请选择“为空”")
    kind = _column_type(column)
    if kind in ("integer", "number"):
        if not NUMBER.fullmatch(text):
            _filter_error(f"{label}需要填写数字，不含千位分隔符")
        try:
            number = Decimal(text)
            if column.get("format") == "currency_cents":
                if "." in text and len(text.rsplit(".", 1)[1]) > 2:
                    _filter_error(f"{label}按元填写，最多两位小数")
                number *= 100
                if number != number.to_integral_value():
                    _filter_error(f"{label}按元填写，最多两位小数")
            if kind == "integer" and number != number.to_integral_value():
                _filter_error(f"{label}需要填写整数")
            if not number.is_finite() or abs(number) > 9_007_199_254_740_991:
                _filter_error(f"{label}超出支持的数字范围")
            return int(number) if number == number.to_integral_value() else float(number)
        except InvalidOperation:
            _filter_error(f"{label}需要填写有效数字")
    if kind == "date":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                raise ValueError()
            return date.fromisoformat(text).isoformat()
        except ValueError:
            _filter_error(f"{label}需要填写有效日期，格式为 YYYY-MM-DD")
    if kind == "datetime":
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?", text):
                raise ValueError()
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            # SQLite julianday also interprets zone-free values as UTC.
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
        except (ValueError, OverflowError):
            _filter_error(f"{label}需要填写有效的 ISO 日期时间")
    if kind == "boolean":
        if text not in ("true", "false"):
            _filter_error(f"{label}请选择是或否")
        return int(text == "true")
    return text


def _conditions(raw, logic, columns):
    if logic not in ("and", "or"):
        _filter_error("条件关系请选择全部满足或任一满足")
    if isinstance(raw, str):
        try:
            if len(raw) > 8192:
                raise ValueError()
            raw = json.loads(raw) if raw else []
        except (ValueError, TypeError):
            _filter_error("筛选条件格式无效，请重新设置")
    if not isinstance(raw, list) or len(raw) > MAX_CONDITIONS:
        _filter_error(f"最多支持 {MAX_CONDITIONS} 条筛选条件")
    column_map = {c["key"]: c for c in columns}
    clauses, params = [], []
    for position, item in enumerate(raw, 1):
        if not isinstance(item, dict) or set(item) - {"field", "operator", "value", "value_to"}:
            _filter_error(f"第 {position} 条筛选条件格式无效")
        column = column_map.get(item.get("field")) if isinstance(item.get("field"), str) else None
        if column is None:
            _filter_error(f"第 {position} 条条件请选择允许查看的字段")
        kind = _column_type(column)
        operator = item.get("operator")
        allowed = {"eq", "ne", *EMPTY_OPERATORS}
        allowed |= {"contains", "not_contains", "starts_with"} if kind == "text" else set()
        allowed |= {"gt", "gte", "lt", "lte", "between"} if kind in {"integer", "number", "date", "datetime"} else set()
        if not isinstance(operator, str) or operator not in allowed:
            _filter_error(f"第 {position} 条条件不适用于所选字段")
        name = _identifier(column["key"])
        if operator in EMPTY_OPERATORS:
            empty = f"({name} IS NULL OR CAST({name} AS TEXT) = '')"
            clauses.append(empty if operator == "is_empty" else f"NOT {empty}")
            continue
        value = _filter_value(item.get("value"), column, position)
        if operator in ("contains", "not_contains", "starts_with"):
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(f"CAST({name} AS TEXT) {'NOT LIKE' if operator == 'not_contains' else 'LIKE'} ? ESCAPE '\\'")
            params.append(("" if operator == "starts_with" else "%") + escaped + "%")
        else:
            expression = f"julianday({name})" if kind == "datetime" else name
            placeholder = "julianday(?)" if kind == "datetime" else "?"
            if operator == "between":
                upper = _filter_value(item.get("value_to"), column, position)
                if value > upper:
                    _filter_error(f"第 {position} 条条件的范围起点不能大于终点")
                clauses.append(f"{expression} BETWEEN {placeholder} AND {placeholder}")
                params.extend([value, upper])
            else:
                clauses.append(f"{expression} {COMPARISONS[operator]} {placeholder}")
                params.append(value)
    return "(" + f" {logic.upper()} ".join(clauses) + ")" if clauses else "", params, raw


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid catalog identifier")
    return '"' + value + '"'


class BusinessData:
    def __init__(self, service):
        self.service = service

    def _catalog(self):
        configured = self.service.settings.business_data_catalog
        if not configured:
            return []
        try:
            path = Path(configured)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 262144:
                raise ValueError("Invalid catalog")
            data = json.loads(path.read_text())
            if data.get("version") != 1 or not isinstance(data.get("tables"), list) or len(data["tables"]) > 40:
                raise ValueError("Invalid catalog")
            seen = set()
            for table in data["tables"]:
                _identifier(table["id"]); _identifier(table["table"]); _identifier(table["primary_key"])
                if table["id"] in seen or not isinstance(table.get("connection_id"), str):
                    raise ValueError("Invalid catalog")
                seen.add(table["id"])
                columns = table["columns"]
                if not isinstance(columns, list) or not 1 <= len(columns) <= 30:
                    raise ValueError("Invalid columns")
                keys = set()
                for column in columns:
                    _identifier(column["key"])
                    if (column["key"] in keys or not isinstance(column["label"], str)
                            or ("type" in column and not isinstance(column["type"], str))):
                        raise ValueError("Invalid columns")
                    keys.add(column["key"])
                if table["primary_key"] not in keys:
                    raise ValueError("Primary key must be visible")
                for key, value in table.get("filters", {}).items():
                    _identifier(key)
                    if key not in keys or type(value) not in (str, int, float, bool):
                        raise ValueError("Invalid scope")
                if table.get("scope") not in ("demo", "public", "business"):
                    raise ValueError("Missing provenance")
                db_path = Path(table["path"])
                if not db_path.is_absolute() or db_path.is_symlink() or not db_path.is_file():
                    raise ValueError("Invalid registered database")
            return data["tables"]
        except (OSError, ValueError, KeyError, TypeError):
            raise AppError(503, "DATA_CATALOG_UNAVAILABLE", "业务数据目录暂不可用，请联系管理员") from None

    def _access(self, table, user):
        connection = self.service.require_resource("connections", table["connection_id"], user)
        if not connection["enabled"]:
            raise AppError(404, "NOT_FOUND", "数据表不存在或无访问权限")

    @staticmethod
    def _public(table):
        return {k: table[k] for k in ("id", "name", "description", "scope", "connection_id", "columns") if k in table}

    def tables(self, user):
        result = []
        for table in self._catalog():
            try:
                self._access(table, user)
            except AppError as e:
                if e.status in (403, 404):
                    continue
                raise
            result.append(self._public(table))
        return result

    def rows(self, table_id, user, *, offset=0, limit=50, q="", filter_column="", filter_value="", sort="", direction="asc", filters="", filter_logic="and"):
        table = next((t for t in self._catalog() if t["id"] == table_id), None)
        if table is None:
            raise AppError(404, "NOT_FOUND", "数据表不存在或无访问权限")
        self._access(table, user)
        columns = table["columns"]
        keys = {c["key"] for c in columns}
        sort = sort or table["primary_key"]
        if sort not in keys or direction not in ("asc", "desc") or (filter_column and filter_column not in keys):
            raise AppError(422, "INVALID_DATA_FILTER", "筛选或排序字段无效")
        if not 1 <= limit <= 100 or not 0 <= offset <= 1000000 or len(q) > 100 or len(filter_value) > 100:
            raise AppError(422, "INVALID_DATA_FILTER", "筛选范围无效")
        where, params = [], []
        for key, value in table.get("filters", {}).items():
            where.append(f"{_identifier(key)} = ?"); params.append(value)
        scope_sql = " WHERE " + " AND ".join(where) if where else ""
        scope_params = list(params)
        if q.strip():
            escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            search = [c["key"] for c in columns if c.get("searchable", False)]
            if not search:
                raise AppError(422, "INVALID_DATA_FILTER", "此数据表不支持关键词搜索")
            where.append("(" + " OR ".join(f"CAST({_identifier(k)} AS TEXT) LIKE ? ESCAPE '\\'" for k in search) + ")")
            params.extend([f"%{escaped}%"] * len(search))
        if filter_column and filter_value:
            where.append(f"{_identifier(filter_column)} = ?"); params.append(filter_value)
        conditions, condition_params, applied_filters = _conditions(filters, filter_logic, columns)
        if conditions:
            # A caller's OR group must never widen the administrator-owned scope.
            where.append(conditions); params.extend(condition_params)
        query_scope = " WHERE " + " AND ".join(where) if where else ""
        name = _identifier(table["table"])
        start = time.monotonic()
        try:
            with sqlite3.connect(Path(table["path"]).as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA trusted_schema=OFF")
                db.set_progress_handler(lambda: int(time.monotonic() - start > 3), 1000)
                db.execute("BEGIN")
                # Registered tables must be ordinary tables, not views running arbitrary expressions.
                kind = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table["table"],)).fetchone()
                if not kind or kind["type"] != "table" or "VIRTUAL TABLE" in (kind["sql"] or "").upper():
                    raise ValueError("Invalid table")
                schema = {row["name"]: row for row in db.execute(f"PRAGMA table_info({name})")}
                # SQLite can interpret an unknown double-quoted identifier as a
                # string literal. Reject schema drift before executing filters.
                if not keys.issubset(schema):
                    raise ValueError("Registered column missing")
                primary = schema[table["primary_key"]]
                if (primary["pk"] != 1 or sum(bool(c["pk"]) for c in schema.values()) != 1
                        or not (primary["notnull"] or primary["type"].upper() == "INTEGER")):
                    raise ValueError("Stable non-null primary key required")
                if db.execute(f"SELECT 1 FROM {name} WHERE {_identifier(table['primary_key'])} IS NULL LIMIT 1").fetchone():
                    raise ValueError("Null paging key")
                total = db.execute(f"SELECT count(*) FROM {name}{scope_sql}", scope_params).fetchone()[0]
                matched = db.execute(f"SELECT count(*) FROM {name}{query_scope}", params).fetchone()[0] if query_scope != scope_sql else total
                ordering = f"{_identifier(sort)} {direction.upper()}"
                if sort != table["primary_key"]:
                    ordering += f", {_identifier(table['primary_key'])} ASC"
                select = ','.join(_identifier(c["key"]) for c in columns)
                records = db.execute(f"SELECT {select} FROM {name}{query_scope} ORDER BY {ordering} LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
                result = []
                for record in records:
                    row = {}
                    for key in keys:
                        value = record[key]
                        if isinstance(value, bytes):
                            value = "[二进制内容]"
                        if isinstance(value, str) and len(value) > 4096:
                            value = value[:4096] + "…（内容已截取）"
                        row[key] = value
                    result.append(row)
        except (sqlite3.Error, OSError, ValueError):
            raise AppError(503, "BUSINESS_DATA_UNAVAILABLE", "业务数据读取未完成，请重试或联系管理员") from None
        return {"table": self._public(table), "total_rows": total, "matched_rows": matched, "offset": offset,
                "limit": limit, "rows": result, "read_only": True, "model_called": False,
                "duration_ms": round((time.monotonic() - start) * 1000),
                "query": {"q": q, "filter_column": filter_column, "filter_value": filter_value, "sort": sort, "direction": direction,
                          "filters": applied_filters, "filter_logic": filter_logic}}


def create_business_data_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/business-data")

    @router.get("")
    def tables(user=Depends(authenticated), service=Depends(svc)):
        return BusinessData(service).tables(user)

    @router.get("/{table_id}/rows")
    def rows(table_id: str, offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=100),
             q: str = Query("", max_length=100), filter_column: str = Query("", max_length=64),
             filter_value: str = Query("", max_length=100), sort: str = Query("", max_length=64),
             direction: str = Query("asc", pattern="^(asc|desc)$"), filters: str = Query("", max_length=8192),
             filter_logic: str = Query("and", pattern="^(and|or)$"), user=Depends(authenticated), service=Depends(svc)):
        return BusinessData(service).rows(table_id, user, offset=offset, limit=limit, q=q,
            filter_column=filter_column, filter_value=filter_value, sort=sort, direction=direction,
            filters=filters, filter_logic=filter_logic)

    return router
