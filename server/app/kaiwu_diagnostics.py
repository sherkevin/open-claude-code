"""Privacy-bounded diagnostics for the Kaiwu connector control plane.

This module owns the frozen event vocabulary, strict wire validation, the
SQLite persistence/query helpers and the read-only operator page.  It never
stores free-form text: message text is derived from the catalog below, and
callers cannot submit message, details, headers, URLs, arguments, results or
credentials.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator

from .db import Database, now
from .security import AppError


SCHEMA_VERSION = 1
MAX_BATCH_EVENTS = 100
MAX_BATCH_BYTES = 256 * 1024
MAX_DEVICE_EVENTS = 500
DEFAULT_DEVICE_EVENTS = 200
MAX_ADMIN_EVENTS = 500
DEFAULT_ADMIN_EVENTS = 200
RETENTION_DAYS = 14
MAX_EVENTS_PER_CONNECTOR = 100_000
MAX_EVENTS_GLOBAL = 1_000_000
MAX_DURATION_MS = 3_600_000
MAX_ATTEMPT = 100
CLOCK_SKEW_FORWARD_SECONDS = 300
PRUNE_INTERVAL_SECONDS = 60.0

EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{32}$")
OPERATION_ID_RE = re.compile(r"^op_[0-9a-f]{16,64}$")
TRACE_ID_RE = re.compile(r"^kwtr_[0-9a-f]{24,64}$")
SOURCE_ID_RE = re.compile(r"^src_[0-9a-f]{32}$")
CONNECTOR_ID_RE = re.compile(r"^conn_[0-9a-f]{32}$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
INTEGER_CODE_RE = re.compile(r"^-32(?:[0-6][0-9]{2}|700)$")
RFC3339_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})$"
)

LEVELS = ("info", "warn", "error")
COMPONENTS = (
    "connector",
    "config",
    "source",
    "auth",
    "diagnosis",
    "tunnel",
    "mcp",
    "platform",
    "diagnostics",
)


@dataclass(frozen=True)
class EventSpec:
    level: str
    component: str
    message: str


# This catalog is a byte-for-byte mirror of
# connector/internal/diagnostics/catalog.go.  Both sides must change together.
CATALOG: dict[str, EventSpec] = {
    "connector_started": EventSpec("info", "connector", "连接器已启动"),
    "connector_stopped": EventSpec("info", "connector", "连接器已停止"),
    "version_loaded": EventSpec("info", "connector", "连接器版本已加载"),
    "enrollment_succeeded": EventSpec("info", "connector", "企业绑定成功"),
    "enrollment_failed": EventSpec("error", "connector", "企业绑定失败"),
    "config_imported": EventSpec("info", "config", "MCP 配置已导入"),
    "config_import_failed": EventSpec("error", "config", "MCP 配置导入失败"),
    "source_saved": EventSpec("info", "source", "MCP 服务配置已保存"),
    "source_save_failed": EventSpec("error", "source", "MCP 服务配置保存失败"),
    "source_tools_loaded": EventSpec("info", "source", "MCP 工具目录读取成功"),
    "source_tools_failed": EventSpec("error", "source", "MCP 工具目录读取失败"),
    "source_enabled": EventSpec("info", "source", "MCP 服务已启用"),
    "source_disabled": EventSpec("warn", "source", "MCP 服务已停用"),
    "source_toggle_failed": EventSpec("error", "source", "MCP 服务启停失败"),
    "auth_profile_saved": EventSpec("info", "auth", "本机动态认证配置已保存"),
    "auth_profile_failed": EventSpec("error", "auth", "本机动态认证配置保存失败"),
    "diagnosis_started": EventSpec("info", "diagnosis", "开始检查 MCP 服务"),
    "diagnosis_succeeded": EventSpec("info", "diagnosis", "MCP 服务检查通过"),
    "diagnosis_failed": EventSpec("error", "diagnosis", "MCP 服务检查未通过"),
    "connection_started": EventSpec("info", "connector", "开始建立公网连接"),
    "connection_succeeded": EventSpec("info", "connector", "公网连接已就绪"),
    "connection_failed": EventSpec("error", "connector", "公网连接建立失败"),
    "connection_paused": EventSpec("warn", "connector", "公网连接已暂停"),
    "connection_disconnected": EventSpec("warn", "connector", "公网连接已断开"),
    "connection_revoked": EventSpec("warn", "connector", "本机设备已撤销"),
    "frp_started": EventSpec("info", "tunnel", "公网隧道进程已启动"),
    "frp_stopped": EventSpec("error", "tunnel", "公网隧道进程已停止"),
    "frp_retry": EventSpec("warn", "tunnel", "公网隧道正在重试"),
    "frp_recovered": EventSpec("info", "tunnel", "公网隧道已恢复"),
    "mcp_request_started": EventSpec("info", "mcp", "收到 MCP 调用"),
    "mcp_request_succeeded": EventSpec("info", "mcp", "MCP 调用成功"),
    "mcp_request_failed": EventSpec("error", "mcp", "MCP 调用失败"),
    "mcp_tool_succeeded": EventSpec("info", "mcp", "工具调用成功"),
    "mcp_tool_failed": EventSpec("error", "mcp", "工具调用失败"),
    "platform_request_succeeded": EventSpec("info", "platform", "开物平台调用已到达并成功"),
    "platform_request_failed": EventSpec("error", "platform", "开物平台调用已到达但未成功"),
    "diagnostics_paused": EventSpec("warn", "diagnostics", "诊断上报已暂停"),
    "diagnostics_resumed": EventSpec("info", "diagnostics", "诊断上报已恢复"),
    "diagnostics_upload_recovered": EventSpec("info", "diagnostics", "诊断上报已恢复连接"),
    "operation_failed": EventSpec("error", "connector", "操作失败，请提供事件编号"),
}


def message_for(code: str) -> str:
    spec = CATALOG.get(code)
    return spec.message if spec else "操作失败，请提供事件编号"


def _invalid(reason: str) -> AppError:
    return AppError(422, "DIAGNOSTICS_EVENT_INVALID", f"诊断事件字段无效：{reason}")


def parse_rfc3339(value: str) -> datetime:
    """Parse RFC3339 with up to nanosecond precision into an aware UTC value."""
    if not isinstance(value, str):
        raise ValueError("occurred_at must be RFC3339 text")
    match = RFC3339_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("occurred_at must be RFC3339")
    date_part, time_part, fraction, offset = match.groups()
    fraction_text = ""
    if fraction:
        fraction_text = "." + fraction[:6].ljust(6, "0")
    zone = "+00:00" if offset.lower() == "z" else offset
    parsed = datetime.fromisoformat(f"{date_part}T{time_part}{fraction_text}{zone}")
    return parsed.astimezone(timezone.utc)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosticEvent(_Model):
    schema_version: StrictInt
    event_id: StrictStr
    occurred_at: datetime
    level: StrictStr
    component: StrictStr
    event_code: StrictStr
    message_code: StrictStr
    operation_id: StrictStr | None = None
    trace_id: StrictStr | None = None
    source_id: StrictStr | None = None
    tool_name: StrictStr | None = None
    http_status: StrictInt | None = None
    rpc_error_code: StrictStr | None = None
    duration_ms: StrictInt | None = None
    attempt: StrictInt | None = None

    @field_validator("occurred_at", mode="before")
    @classmethod
    def _occurred_at(cls, value: Any) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = parse_rfc3339(value)
        else:
            raise ValueError("occurred_at must be RFC3339 text")
        if parsed.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return parsed.astimezone(timezone.utc)


class EventBatch(_Model):
    events: list[DiagnosticEvent]


def source_grants(db: Database, connector_id: str) -> dict[str, set[str]]:
    """Return only the approved source/tool pairs for one connector."""
    grants: dict[str, set[str]] = {}
    for row in db.all(
        "SELECT source_id,allowed_tools FROM kaiwu_sources WHERE connector_id=?",
        (connector_id,),
    ):
        try:
            tools = json.loads(row["allowed_tools"])
        except (TypeError, ValueError):
            tools = []
        grants[row["source_id"]] = {
            tool for tool in tools if isinstance(tool, str) and TOOL_NAME_RE.fullmatch(tool)
        }
    return grants


def normalize_event(event: DiagnosticEvent, grants: dict[str, set[str]]) -> dict[str, Any]:
    """Validate one event and return the exact SQLite row fields."""
    if event.schema_version != SCHEMA_VERSION:
        raise _invalid("schema_version")
    if not EVENT_ID_RE.fullmatch(event.event_id):
        raise _invalid("event_id")
    spec = CATALOG.get(event.event_code)
    if spec is None:
        raise _invalid("event_code")
    if event.message_code != event.event_code:
        raise _invalid("message_code")
    if event.level != spec.level or event.component != spec.component:
        raise _invalid("catalog_mismatch")
    occurred = event.occurred_at
    if occurred > datetime.now(timezone.utc) + timedelta(seconds=CLOCK_SKEW_FORWARD_SECONDS):
        raise _invalid("occurred_at_future")
    if event.operation_id is not None and not OPERATION_ID_RE.fullmatch(event.operation_id):
        raise _invalid("operation_id")
    if event.trace_id is not None and not TRACE_ID_RE.fullmatch(event.trace_id):
        raise _invalid("trace_id")
    if event.source_id is not None:
        if not SOURCE_ID_RE.fullmatch(event.source_id):
            raise _invalid("source_id")
        if event.source_id not in grants:
            raise _invalid("source_not_owned")
    if event.tool_name is not None:
        if not TOOL_NAME_RE.fullmatch(event.tool_name):
            raise _invalid("tool_name")
        if event.source_id is None or event.tool_name not in grants.get(event.source_id, set()):
            raise _invalid("tool_not_approved")
    if event.http_status is not None and not 0 <= event.http_status <= 599:
        raise _invalid("http_status")
    if event.rpc_error_code is not None:
        if not INTEGER_CODE_RE.fullmatch(event.rpc_error_code):
            raise _invalid("rpc_error_code")
    if event.duration_ms is not None and not 0 <= event.duration_ms <= MAX_DURATION_MS:
        raise _invalid("duration_ms")
    if event.attempt is not None and not 0 <= event.attempt <= MAX_ATTEMPT:
        raise _invalid("attempt")
    return {
        "event_id": event.event_id,
        "occurred_at": occurred.isoformat(),
        "level": event.level,
        "component": event.component,
        "event_code": event.event_code,
        "message_code": event.message_code,
        "operation_id": event.operation_id,
        "trace_id": event.trace_id,
        "source_id": event.source_id,
        "tool_name": event.tool_name,
        "http_status": event.http_status,
        "rpc_error_code": event.rpc_error_code,
        "duration_ms": event.duration_ms,
        "attempt": event.attempt,
    }

_INSERT_SQL = (
    "INSERT OR IGNORE INTO kaiwu_diagnostic_events"
    "(tenant_id,connector_id,event_id,occurred_at,level,component,event_code,"
    "message_code,operation_id,trace_id,source_id,tool_name,http_status,"
    "rpc_error_code,duration_ms,attempt,origin,received_at)"
    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def insert_rows(
    db: Database,
    tenant_id: str,
    connector_id: str,
    rows: list[dict[str, Any]],
    *,
    origin: str,
) -> int:
    """Insert validated rows in the caller's transaction and ACK idempotently."""
    if origin not in {"connector", "edge"}:
        raise ValueError("invalid diagnostics origin")
    received = now()
    with db.transaction():
        for row in rows:
            db.execute(
                _INSERT_SQL,
                (
                    tenant_id,
                    connector_id,
                    row["event_id"],
                    row["occurred_at"],
                    row["level"],
                    row["component"],
                    row["event_code"],
                    row["message_code"],
                    row.get("operation_id"),
                    row.get("trace_id"),
                    row.get("source_id"),
                    row.get("tool_name"),
                    row.get("http_status"),
                    row.get("rpc_error_code"),
                    row.get("duration_ms"),
                    row.get("attempt"),
                    origin,
                    received,
                ),
            )
    return len(rows)


_prune_lock = threading.Lock()
_last_prune: dict[int, float] = {}


def prune(
    db: Database,
    *,
    current: datetime | None = None,
    retention_days: int = RETENTION_DAYS,
    max_per_connector: int = MAX_EVENTS_PER_CONNECTOR,
    max_global: int = MAX_EVENTS_GLOBAL,
) -> None:
    """Apply age, per-connector and global caps in oldest-first order."""
    current = current or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=retention_days)).isoformat()
    with db.transaction():
        db.execute("DELETE FROM kaiwu_diagnostic_events WHERE occurred_at < ?", (cutoff,))
        over = db.all(
            "SELECT connector_id,COUNT(*) AS n FROM kaiwu_diagnostic_events "
            "GROUP BY connector_id HAVING COUNT(*) > ?",
            (max_per_connector,),
        )
        for row in over:
            excess = int(row["n"]) - max_per_connector
            db.execute(
                "DELETE FROM kaiwu_diagnostic_events WHERE id IN ("
                "SELECT id FROM kaiwu_diagnostic_events WHERE connector_id=? "
                "ORDER BY received_at ASC,id ASC LIMIT ?)",
                (row["connector_id"], excess),
            )
        total = db.one("SELECT COUNT(*) AS n FROM kaiwu_diagnostic_events")["n"]
        if total > max_global:
            db.execute(
                "DELETE FROM kaiwu_diagnostic_events WHERE id IN ("
                "SELECT id FROM kaiwu_diagnostic_events "
                "ORDER BY received_at ASC,id ASC LIMIT ?)",
                (total - max_global,),
            )


def maybe_prune(db: Database) -> None:
    key = id(db)
    current = time.monotonic()
    with _prune_lock:
        previous = _last_prune.get(key, 0.0)
        if current - previous < PRUNE_INTERVAL_SECONDS:
            return
        _last_prune[key] = current
    prune(db)


def event_view(row: dict[str, Any], *, admin: bool) -> dict[str, Any]:
    view: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": row["event_id"],
        "occurred_at": row["occurred_at"],
        "level": row["level"],
        "component": row["component"],
        "event_code": row["event_code"],
        "message_code": row["message_code"],
    }
    for key in (
        "operation_id",
        "trace_id",
        "source_id",
        "tool_name",
        "http_status",
        "rpc_error_code",
        "duration_ms",
        "attempt",
    ):
        if row.get(key) is not None:
            view[key] = row[key]
    view["message"] = message_for(row["message_code"])
    if admin:
        view["origin"] = row["origin"]
        view["tenant_id"] = row["tenant_id"]
        view["connector_id"] = row["connector_id"]
        view["received_at"] = row["received_at"]
    return view


def query_connector_events(
    db: Database,
    connector_id: str,
    *,
    limit: int,
    edge_only: bool = True,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_DEVICE_EVENTS))
    sql = "SELECT * FROM kaiwu_diagnostic_events WHERE connector_id=?"
    values: list[Any] = [connector_id]
    if edge_only:
        # A connector can also upload platform_* events from its own local
        # observation. The device page must show only Edge-derived outcomes so
        # one request is never double counted.
        sql += " AND origin='edge' AND component='platform'"
    sql += " ORDER BY id DESC LIMIT ?"
    values.append(limit)
    return [event_view(row, admin=False) for row in db.all(sql, tuple(values))]


def query_admin_events(
    db: Database,
    *,
    limit: int,
    cursor: int | None,
    connector_id: str | None,
    source_id: str | None,
    level: str | None,
    trace_id: str | None,
    event_code: str | None,
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[dict[str, Any]], int | None]:
    limit = max(1, min(int(limit), MAX_ADMIN_EVENTS))
    clauses = ["1=1"]
    values: list[Any] = []
    if cursor is not None:
        clauses.append("id < ?")
        values.append(cursor)
    if connector_id:
        clauses.append("connector_id = ?")
        values.append(connector_id)
    if source_id:
        clauses.append("source_id = ?")
        values.append(source_id)
    if level:
        clauses.append("level = ?")
        values.append(level)
    if trace_id:
        clauses.append("trace_id = ?")
        values.append(trace_id)
    if event_code:
        clauses.append("event_code = ?")
        values.append(event_code)
    if since is not None:
        clauses.append("occurred_at >= ?")
        values.append(since.isoformat())
    if until is not None:
        clauses.append("occurred_at <= ?")
        values.append(until.isoformat())
    values.append(limit + 1)
    rows = db.all(
        "SELECT * FROM kaiwu_diagnostic_events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY id DESC LIMIT ?",
        tuple(values),
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = int(rows[-1]["id"]) if has_more and rows else None
    return [event_view(row, admin=True) for row in rows], next_cursor


def validate_filter(name: str, value: str | None, pattern: re.Pattern[str] | None) -> None:
    if value is None or value == "":
        return
    if pattern is None:
        return
    if not pattern.fullmatch(value):
        raise AppError(422, "DIAGNOSTICS_FILTER_INVALID", f"诊断筛选条件无效：{name}")


def new_trace_id() -> str:
    return "kwtr_" + secrets.token_hex(16)


def new_event_id() -> str:
    return "evt_" + secrets.token_hex(16)


def _numeric_code(error: Any) -> str:
    code = ""
    if isinstance(error, dict):
        raw = error.get("code")
        if isinstance(raw, int) and not isinstance(raw, bool):
            code = str(raw)
        elif isinstance(raw, str):
            code = raw
    if code and INTEGER_CODE_RE.fullmatch(code):
        return code
    return ""


def inspect_jsonrpc(payload: bytes) -> tuple[bool | None, str]:
    """Classify one JSON-RPC document.

    ``None`` means the payload does not carry a classifiable result or error.
    Only an explicit JSON-RPC ``error`` or ``result.isError`` is a failure;
    arbitrary upstream text never becomes an event code.
    """
    if not payload or not payload.strip():
        return None, ""
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, ""
    return _inspect_value(value)


def _inspect_value(value: Any) -> tuple[bool | None, str]:
    if isinstance(value, list):
        seen: bool | None = None
        for item in value:
            ok, code = _inspect_value(item)
            if ok is False:
                return False, code
            if ok is True:
                seen = True
        return seen, ""
    if not isinstance(value, dict):
        return None, ""
    if value.get("error") is not None:
        return False, _numeric_code(value["error"])
    if "result" in value:
        result = value["result"]
        if isinstance(result, dict) and result.get("isError") is True:
            return False, ""
        return True, ""
    return None, ""


def jsonrpc_tool_name(payload: bytes) -> str:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    candidates = value if isinstance(value, list) else [value]
    for item in candidates:
        if not isinstance(item, dict) or item.get("method") != "tools/call":
            continue
        params = item.get("params")
        if not isinstance(params, dict):
            continue
        name = params.get("name")
        if isinstance(name, str) and TOOL_NAME_RE.fullmatch(name):
            return name
    return ""


def is_notification_request(payload: bytes) -> bool:
    """True only for JSON-RPC notifications, including notifications/*."""
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    candidates = value if isinstance(value, list) else [value]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        method = item.get("method")
        if isinstance(method, str) and method.startswith("notifications/"):
            return True
        if isinstance(method, str) and "id" not in item:
            return True
    return False


class JsonRpcStreamInspector:
    """Best-effort classifier that never delays or rewrites SSE bytes."""

    def __init__(self, limit: int = MAX_BATCH_BYTES):
        self._limit = limit
        self._buffer = b""
        self._seen = 0
        self.saw_result = False
        self.saw_error = False
        self.error_code = ""

    def feed(self, chunk: bytes) -> None:
        if self._seen >= self._limit:
            return
        remaining = self._limit - self._seen
        chunk = chunk[:remaining]
        self._buffer += chunk
        self._seen += len(chunk)
        while True:
            boundary = self._boundary()
            if boundary is None:
                break
            index, width = boundary
            block = self._buffer[:index]
            self._buffer = self._buffer[index + width:]
            self._classify_block(block)
        if self._seen >= self._limit:
            self._classify_block(self._buffer)
            self._buffer = b""

    def finish(self) -> None:
        if self._buffer:
            self._classify_block(self._buffer)
            self._buffer = b""

    def _boundary(self) -> tuple[int, int] | None:
        found = [
            (self._buffer.find(b"\r\n\r\n"), 4),
            (self._buffer.find(b"\n\n"), 2),
            (self._buffer.find(b"\r\r"), 2),
        ]
        found = [(index, width) for index, width in found if index >= 0]
        if not found:
            return None
        return min(found, key=lambda item: (item[0], -item[1]))

    def _classify_block(self, block: bytes) -> None:
        data_lines: list[bytes] = []
        for raw in re.split(rb"\r\n|\r|\n", block):
            if not raw.startswith(b"data:"):
                continue
            value = raw[5:]
            if value.startswith(b" "):
                value = value[1:]
            data_lines.append(value)
        if not data_lines:
            return
        payload = b"\n".join(data_lines).strip()
        if not payload or payload == b"[DONE]":
            return
        ok, code = inspect_jsonrpc(payload)
        if ok is False:
            self.saw_error = True
            self.error_code = code
        elif ok is True:
            self.saw_result = True

    def outcome(self) -> tuple[bool | None, str]:
        if self.saw_error:
            return False, self.error_code
        if self.saw_result:
            return True, ""
        return None, ""


def record_platform_event(
    db: Database,
    *,
    tenant_id: str,
    connector_id: str,
    source_id: str,
    tool_name: str,
    trace_id: str,
    success: bool,
    http_status: int,
    rpc_error_code: str,
    duration_ms: int,
) -> None:
    """Persist one Edge platform outcome. The caller decides whether errors
    must be swallowed so diagnostics can never break MCP traffic."""
    code = "platform_request_succeeded" if success else "platform_request_failed"
    spec = CATALOG[code]
    status = int(http_status)
    if not 0 <= status <= 599:
        status = 0
    row = {
        "event_id": new_event_id(),
        "occurred_at": now(),
        "level": spec.level,
        "component": spec.component,
        "event_code": code,
        "message_code": code,
        "operation_id": None,
        "trace_id": trace_id if TRACE_ID_RE.fullmatch(trace_id) else None,
        "source_id": source_id if SOURCE_ID_RE.fullmatch(source_id) else None,
        "tool_name": tool_name if TOOL_NAME_RE.fullmatch(tool_name) else None,
        "http_status": status if status else None,
        "rpc_error_code": rpc_error_code if INTEGER_CODE_RE.fullmatch(rpc_error_code) else None,
        "duration_ms": max(0, min(int(duration_ms), MAX_DURATION_MS)),
        "attempt": None,
    }
    insert_rows(db, tenant_id, connector_id, [row], origin="edge")
    # Edge traffic can continue while the connector pauses its own upload.
    # Pruning here keeps the same age/per-device/global caps in that mode too.
    maybe_prune(db)


OPS_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>开物连接器诊断</title>
<style>
:root{color-scheme:light;font-family:"Noto Sans SC","Microsoft YaHei",system-ui,sans-serif}
body{margin:0;background:#f5f3ee;color:#1d2529}
header{padding:18px 22px;background:#123239;color:#f5f3ee}
main{padding:18px 22px;max-width:1400px;margin:0 auto}
.bar{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin-bottom:12px}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#40535a}
input,select,button{font:inherit;padding:7px 9px;border:1px solid #b7b1a5;border-radius:4px;background:#fff}
button{background:#123239;color:#fff;border-color:#123239;cursor:pointer}
button.secondary{background:#fff;color:#123239}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border-bottom:1px solid #ded8cc;padding:7px 8px;text-align:left;vertical-align:top}
th{background:#ece7dc;position:sticky;top:0}
td{max-width:280px;overflow-wrap:anywhere}
.status{min-height:22px;font-size:13px;color:#40535a;margin-bottom:8px}
.hidden{display:none}
#token{min-width:320px}
</style>
</head>
<body>
<header><strong>开物连接器诊断</strong> <span>只读运维页</span></header>
<main>
<section id="login">
<p class="status">请输入控制面管理员令牌。令牌只保存在本页内存中，刷新后清空，不会写入地址栏、Cookie、浏览器存储或下发到 Windows 连接器。</p>
<form id="auth" class="bar">
<label>管理员令牌<input id="token" type="password" autocomplete="off" spellcheck="false" placeholder="请输入管理员令牌"></label>
<button type="submit">进入</button>
</form>
</section>
<section id="console" class="hidden">
<div class="bar">
<label>连接器<input id="f-connector" placeholder="conn_..."></label>
<label>来源<input id="f-source" placeholder="src_..."></label>
<label>级别<select id="f-level"><option value="">全部</option><option>info</option><option>warn</option><option>error</option></select></label>
<label>追踪编号<input id="f-trace" placeholder="kwtr_..."></label>
<label>开始时间<input id="f-since" type="datetime-local"></label>
<label>结束时间<input id="f-until" type="datetime-local"></label>
<button id="refresh" type="button">刷新</button>
<button id="download" type="button" class="secondary">下载当前结果</button>
<button id="logout" type="button" class="secondary">退出</button>
</div>
<div id="status" class="status"></div>
<div style="overflow:auto;max-height:70vh">
<table><thead><tr>
<th>发生时间</th><th>级别</th><th>事件来源</th><th>连接器</th><th>来源</th><th>事件</th><th>说明</th><th>工具</th><th>状态码</th><th>耗时(ms)</th><th>追踪编号</th><th>接收时间</th>
</tr></thead><tbody id="rows"></tbody></table>
</div>
</section>
</main>
<script>
(function () {
  "use strict";
  var adminToken = "";
  var timer = null;
  var current = [];
  var login = document.getElementById("login");
  var consoleBox = document.getElementById("console");
  var statusLine = document.getElementById("status");
  var tbody = document.getElementById("rows");

  function setStatus(text) { statusLine.textContent = text; }

  function showLogin() {
    adminToken = "";
    current = [];
    tbody.replaceChildren();
    consoleBox.classList.add("hidden");
    login.classList.remove("hidden");
    if (timer) { clearInterval(timer); timer = null; }
    setStatus("");
  }

  function pad(value) { return String(value).padStart(2, "0"); }

  function isoFromLocal(value) {
    if (!value) { return ""; }
    var d = new Date(value);
    if (isNaN(d.getTime())) { return ""; }
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) +
      "T" + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds()) + "Z";
  }

  function query() {
    var params = new URLSearchParams();
    params.set("limit", "200");
    var fields = [["connector_id", "f-connector"], ["source_id", "f-source"],
      ["level", "f-level"], ["trace_id", "f-trace"]];
    fields.forEach(function (pair) {
      var value = document.getElementById(pair[1]).value.trim();
      if (value) { params.set(pair[0], value); }
    });
    var since = isoFromLocal(document.getElementById("f-since").value);
    var until = isoFromLocal(document.getElementById("f-until").value);
    if (since) { params.set("since", since); }
    if (until) { params.set("until", until); }
    return params.toString();
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text === null || text === undefined ? "" : String(text);
    return td;
  }

  function render(events) {
    current = events;
    tbody.replaceChildren();
    events.forEach(function (event) {
      var tr = document.createElement("tr");
      [event.occurred_at, event.level, event.origin, event.connector_id, event.source_id,
       event.event_code, event.message, event.tool_name, event.http_status,
       event.duration_ms, event.trace_id, event.received_at].forEach(function (value) {
        tr.appendChild(cell(value));
      });
      tbody.appendChild(tr);
    });
    setStatus("已显示 " + events.length + " 条脱敏诊断事件。");
  }

  function load() {
    if (!adminToken) { return; }
    fetch("/api/kaiwu/v1/diagnostics/events?" + query(), {
      method: "GET",
      headers: { "Authorization": "Bearer " + adminToken, "Accept": "application/json" },
      cache: "no-store",
      credentials: "omit"
    }).then(function (response) {
      if (response.status === 401 || response.status === 403) {
        showLogin();
        setStatus("管理员令牌无效或已失效，请重新输入。");
        return null;
      }
      if (!response.ok) { throw new Error("HTTP " + response.status); }
      return response.json();
    }).then(function (data) {
      if (data) { render(Array.isArray(data.events) ? data.events : []); }
    }).catch(function () {
      setStatus("读取失败，请检查网络或稍后重试。");
    });
  }

  document.getElementById("auth").addEventListener("submit", function (event) {
    event.preventDefault();
    var input = document.getElementById("token");
    adminToken = input.value.trim();
    input.value = "";
    if (!adminToken) { return; }
    login.classList.add("hidden");
    consoleBox.classList.remove("hidden");
    load();
    timer = setInterval(load, 3000);
  });

  document.getElementById("refresh").addEventListener("click", load);
  document.getElementById("logout").addEventListener("click", showLogin);
  document.getElementById("download").addEventListener("click", function () {
    var blob = new Blob([JSON.stringify(current, null, 2)], { type: "application/json" });
    var link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = "kaiwu-diagnostics.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(link.href); }, 1000);
  });
  showLogin();
})();
</script>
</body>
</html>
"""


def ops_page_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "img-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        ),
    }
