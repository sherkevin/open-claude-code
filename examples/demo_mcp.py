"""A real, local-only MCP fixture with clearly labeled synthetic business data.

Compatible with mcp==1.30.0. Run with the server virtual environment. This is
an acceptance fixture, not a production knowledge base or ticketing system.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.responses import JSONResponse


HOST = os.environ.get("DEMO_MCP_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEMO_MCP_PORT", "8767"))
TOKEN = os.environ.get("DEMO_MCP_TOKEN", "")
DATA_DIR = Path(os.environ.get("DEMO_MCP_DATA_DIR", str(Path(__file__).resolve().parents[1] / ".runtime" / "demo-mcp")))


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_binding() -> None:
    if not 1 <= PORT <= 65535:
        raise ValueError("DEMO_MCP_PORT must be between 1 and 65535")
    if not is_loopback(HOST) and len(TOKEN) < 24:
        raise ValueError("Non-loopback demo binding requires DEMO_MCP_TOKEN with at least 24 characters")


DOCUMENTS = (
    {
        "id": "DEMO-KB-001",
        "title": "演示设备报修流程",
        "tags": ["设备", "报修", "工单", "故障"],
        "content": "这是演示规则：员工发现设备故障后记录设备编号、故障现象和联系人；普通故障在 4 个工作小时内响应，影响业务的严重故障在 30 分钟内响应。创建报修工单前必须确认提交内容。",
    },
    {
        "id": "DEMO-KB-002",
        "title": "演示差旅报销规则",
        "tags": ["差旅", "报销", "发票", "费用"],
        "content": "这是演示规则：差旅结束后 10 个工作日内提交报销，附行程、发票与审批记录。演示住宿上限为每晚 500 元。此数值仅用于验收，不代表任何客户的真实制度。",
    },
    {
        "id": "DEMO-KB-003",
        "title": "演示知识库使用说明",
        "tags": ["知识库", "演示", "权限", "数据"],
        "content": "本连接仅包含人工编写的演示知识与演示工单。正式业务数据应由客户自己的 MCP 提供，业务权限由其服务端执行。",
    },
)


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(DATA_DIR / "tickets.sqlite3", timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("""CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL, description TEXT NOT NULL, requester TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def ticket_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {"demo": True, **dict(row)}


def ticket_source(rows, filters=None, operation="SELECT"):
    """Actual query provenance, independent of how an answer is phrased."""
    records = [dict(row) for row in rows]
    return {"kind": "data", "title": "工单记录", "locator": {
        "data_source": "业务试用数据连接", "database": "tickets.sqlite3", "schema": "main", "table": "tickets",
        "fields": ["id", "title", "description", "requester", "status", "created_at"],
        "record_keys": [row["id"] for row in records], "filters": filters or {},
        "time_range": {"field": "created_at", "from": min((row["created_at"] for row in records), default=None),
                       "to": max((row["created_at"] for row in records), default=None), "scope": "returned_records"},
        "timezone": "UTC", "queried_at": datetime.now(timezone.utc).isoformat(), "row_count": len(records),
        "aggregation": "none", "query": operation, "dataset_purpose": "synthetic_acceptance_fixture",
    }, "excerpt": json.dumps(records, ensure_ascii=False)}


allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
allowed_hosts.extend(part.strip() for part in os.environ.get("DEMO_MCP_ALLOWED_HOSTS", "").split(",") if part.strip())
if not is_loopback(HOST) and HOST not in {"0.0.0.0", "::"}:
    allowed_hosts.append(f"{HOST}:*")

mcp = FastMCP(
    "业务数据连接",
    instructions="知识查询、质量分析和工单查询是只读；create_ticket 会持久化工单，必须经过产品审批。来源元数据说明数据集用途和位置；按查询结果回答，不固定追加用途提醒。",
    host=HOST,
    port=PORT,
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    log_level="WARNING",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"],
    ),
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def query_sales_orders(region: str = "", status: str = "", date_from: str = "", date_to: str = "", min_amount: float = 0, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """按区域、状态、日期与金额查询销售订单，返回匹配数量、分页记录与来源。金额条件按元填写。"""
    from business_scenario import query_orders
    result = query_orders(DATA_DIR, region, status, date_from, date_to, min_amount, limit, offset)
    result["sources"] = [{"kind": "data", "title": "销售订单", "locator": {"database": "business-v2.sqlite3", "table": "sales_orders", "fields": list(result["rows"][0]) if result["rows"] else [], "row_count": len(result["rows"]), "matched_rows": result["matched_rows"], "filters": {"region": region, "status": status, "date_from": date_from, "date_to": date_to, "min_amount": min_amount}, "offset": offset, "limit": limit, "dataset_purpose": result["dataset_purpose"]}, "excerpt": json.dumps(result["rows"], ensure_ascii=False)}]
    return result


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def search_knowledge(query: str, limit: int = 3) -> dict[str, Any]:
    """Search synthetic Chinese company rules; always returns source IDs and labels data as demo."""
    query = query.strip().lower()
    if not query or len(query) > 300:
        raise ValueError("query must contain 1 to 300 characters")
    limit = max(1, min(limit, 5))
    words = [word for word in query.replace("，", " ").replace("。", " ").split() if word]
    ranked = []
    for document in DOCUMENTS:
        text = " ".join([document["title"], document["content"], *document["tags"]]).lower()
        score = sum(3 for tag in document["tags"] if tag in query)
        score += sum(1 for word in words if word in text)
        if score:
            ranked.append((score, document))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return {"demo": True, "query": query, "documents": [doc for _, doc in ranked[:limit]], "total_matches": len(ranked)}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def create_ticket(title: str, description: str, requester: str, request_key: str) -> dict[str, Any]:
    """Persist one synthetic ticket. This WRITE requires user approval; request_key prevents duplicate creation."""
    fields = {"title": title.strip(), "description": description.strip(), "requester": requester.strip(), "request_key": request_key.strip()}
    limits = {"title": 150, "description": 2000, "requester": 100, "request_key": 160}
    for field, value in fields.items():
        if not value or len(value) > limits[field]:
            raise ValueError(f"{field} must contain 1 to {limits[field]} characters")
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM tickets WHERE request_key = ?", (fields["request_key"],)).fetchone()
        if existing:
            if any(existing[key] != fields[key] for key in ("title", "description", "requester")):
                raise ValueError("request_key already belongs to a ticket with different content")
            return {"created": False, "ticket": ticket_dict(existing), "sources": [ticket_source([existing], {"request_key": fields["request_key"]})]}
        ticket_id = f"DEMO-{uuid.uuid4().hex[:12].upper()}"
        connection.execute(
            "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, fields["request_key"], fields["title"], fields["description"], fields["requester"], "open", datetime.now(timezone.utc).isoformat()),
        )
        row = connection.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return {"created": True, "ticket": ticket_dict(row), "sources": [ticket_source([row], {"id": ticket_id}, "INSERT RETURNING")]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def list_tickets(request_key: str = "", limit: int = 20) -> dict[str, Any]:
    """Read persisted synthetic tickets, optionally by exact request_key. No business record is changed."""
    if len(request_key) > 160:
        raise ValueError("request_key is too long")
    limit = max(1, min(limit, 100))
    with database() as connection:
        if request_key:
            rows = connection.execute("SELECT * FROM tickets WHERE request_key = ? ORDER BY created_at DESC LIMIT ?", (request_key, limit)).fetchall()
        else:
            rows = connection.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return {"demo": True, "tickets": [ticket_dict(row) for row in rows], "sources": [ticket_source(rows, {"request_key": request_key or None, "limit": limit, "order": "created_at DESC"})]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def query_quality_metrics(line: str = "", start_date: str = "2026-11-01", end_date: str = "2026-11-04") -> dict[str, Any]:
    """Query line/batch quality counts and calculate the weighted yield for an inclusive date range. Returns database/table/fields/filter/time/aggregation provenance."""
    if line not in {"", "L01", "L02"}:
        raise ValueError("line must be L01, L02 or empty for all lines")
    for date in (start_date, end_date):
        datetime.strptime(date, "%Y-%m-%d")
    if start_date > end_date:
        raise ValueError("start_date must not exceed end_date")
    with database() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS quality_batches (
            batch_id TEXT PRIMARY KEY, line TEXT NOT NULL, production_date TEXT NOT NULL,
            inspected_count INTEGER NOT NULL, qualified_count INTEGER NOT NULL)""")
        connection.executemany("INSERT OR IGNORE INTO quality_batches VALUES(?,?,?,?,?)", [
            ("QA-1101-A", "L01", "2026-11-01", 1000, 980), ("QA-1102-A", "L01", "2026-11-02", 1200, 1152),
            ("QA-1103-A", "L01", "2026-11-03", 1000, 950), ("QA-1104-A", "L01", "2026-11-04", 1500, 1455),
            ("QA-1101-B", "L02", "2026-11-01", 800, 784), ("QA-1102-B", "L02", "2026-11-02", 900, 882),
            ("QA-1103-B", "L02", "2026-11-03", 1100, 1089), ("QA-1104-B", "L02", "2026-11-04", 1000, 990),
        ])
        rows = connection.execute("SELECT * FROM quality_batches WHERE production_date>=? AND production_date<=? AND (?='' OR line=?) ORDER BY production_date,line,batch_id", (start_date, end_date, line, line)).fetchall()
    records = [dict(row) for row in rows]
    inspected = sum(row["inspected_count"] for row in records)
    qualified = sum(row["qualified_count"] for row in records)
    result = {"rows": records, "inspected_count": inspected, "qualified_count": qualified,
              "yield_rate": qualified / inspected if inspected else None, "batch_count": len(records)}
    source = {"kind": "data", "title": "产线批次质量统计", "locator": {
        "data_source": "业务试用数据连接", "database": "tickets.sqlite3", "schema": "main", "table": "quality_batches",
        "fields": ["batch_id", "line", "production_date", "inspected_count", "qualified_count"],
        "record_keys": [row["batch_id"] for row in records], "filters": {"line": line or "all"},
        "time_range": {"field": "production_date", "from": start_date, "to": end_date, "inclusive": True},
        "timezone": "Asia/Shanghai", "queried_at": datetime.now(timezone.utc).isoformat(), "row_count": len(records),
        "aggregation": "yield_rate = SUM(qualified_count) / SUM(inspected_count); NULL when denominator=0",
        "dataset_purpose": "synthetic_acceptance_fixture", "dataset_version": "2026-11-05",
    }, "excerpt": json.dumps(result, ensure_ascii=False)}
    return {**result, "sources": [source]}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def slow_lookup(delay_seconds: int = 15) -> dict[str, Any]:
    """Acceptance-only bounded read delay, useful for checking cancellation. Does not create or modify records."""
    delay_seconds = max(1, min(delay_seconds, 30))
    await asyncio.sleep(delay_seconds)
    return {"demo": True, "waited_seconds": delay_seconds, "message": "演示只读查询完成"}


class BearerGuard:
    """Small ASGI token guard for this fixture, not an OAuth implementation."""
    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and TOKEN:
            headers = dict(scope.get("headers", []))
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            if not hmac.compare_digest(authorization, f"Bearer {TOKEN}"):
                response = JSONResponse({"error": "Demo MCP authorization required"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    validate_binding()
    with database():
        pass
    app = BearerGuard(mcp.streamable_http_app())
    print(json.dumps({"service": "demo-mcp", "host": HOST, "port": PORT, "path": "/mcp", "synthetic_data": True, "authentication": "bearer" if TOKEN else "loopback-only"}, ensure_ascii=False), flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
