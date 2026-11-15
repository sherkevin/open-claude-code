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
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from .security import AppError

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


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
                    if column["key"] in keys or not isinstance(column["label"], str):
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

    def rows(self, table_id, user, *, offset=0, limit=50, q="", filter_column="", filter_value="", sort="", direction="asc"):
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
                "query": {"q": q, "filter_column": filter_column, "filter_value": filter_value, "sort": sort, "direction": direction}}


def create_business_data_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/business-data")

    @router.get("")
    def tables(user=Depends(authenticated), service=Depends(svc)):
        return BusinessData(service).tables(user)

    @router.get("/{table_id}/rows")
    def rows(table_id: str, offset: int = Query(0, ge=0, le=1000000), limit: int = Query(50, ge=1, le=100),
             q: str = Query("", max_length=100), filter_column: str = Query("", max_length=64),
             filter_value: str = Query("", max_length=100), sort: str = Query("", max_length=64),
             direction: str = Query("asc", pattern="^(asc|desc)$"), user=Depends(authenticated), service=Depends(svc)):
        return BusinessData(service).rows(table_id, user, offset=offset, limit=limit, q=q,
            filter_column=filter_column, filter_value=filter_value, sort=sort, direction=direction)

    return router
