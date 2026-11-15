"""Loopback-only product preview with real MCP transport and synthetic records.

No model is contacted. A deterministic test runtime drives the normal product
login, authorization, tool bridge, evidence and event paths. This script is not
imported by the product and does not add a selectable production fake mode.

Run with the project's test environment:
  python examples/business_loop_preview.py serve
  python examples/business_loop_preview.py verify
  python examples/business_loop_preview.py benchmark
Credentials are generated under preview-private with mode 0600, never printed.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = ROOT / "server"
PRIVATE = ROOT.parent / "产品开发_20261114" / "preview-private"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

API_PORT = 8000
MCP_PORT = 8877
ORGANIZATION = "合成验收组织"
DATA_DATE = "2026-11-14"
DATA_TIME = "2026-11-14T12:00:00+08:00"
BRAND = "知行工作台 · 合成验收"
NOTICE = "合成验收，无模型推理"
REGIONS = ("华北", "华东", "华南")
TOOLS = ("order_summary", "shipment_summary", "slow_order_summary")


def secure_json(path: Path, value: object) -> None:
    """Atomic private JSON; refuse credential/receipt symlink destinations."""
    if path.is_symlink():
        raise RuntimeError("Preview file must not be a symlink")
    temporary = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
    os.replace(temporary, path)


def load_credentials(directory: Path) -> dict:
    path = directory / "credentials.json"
    if path.is_symlink():
        raise RuntimeError("Credential file must not be a symlink")
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise RuntimeError("Credential file must have mode 0600")
        return json.loads(path.read_text(encoding="utf-8"))
    credentials = {"username": "preview", "password": secrets.token_urlsafe(32),
                   "mcp_bearer": secrets.token_urlsafe(36)}
    secure_json(path, credentials)
    return credentials


def seed_database(path: Path) -> None:
    """Materialize 100,000 records; the 10k case selects a strict subset."""
    if path.is_symlink():
        raise RuntimeError("Synthetic database must not be a symlink")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS fixture_meta(version INTEGER NOT NULL)")
        existing = db.execute("SELECT version FROM fixture_meta").fetchone()
        if existing:
            if existing[0] != 1 or db.execute("SELECT count(*) FROM orders").fetchone()[0] != 100000:
                raise RuntimeError("Existing synthetic fixture does not match version 1")
            return
        db.execute("""CREATE TABLE orders (
            id INTEGER PRIMARY KEY, organization TEXT NOT NULL, region TEXT NOT NULL,
            customer TEXT NOT NULL, amount_cents INTEGER NOT NULL, quantity INTEGER NOT NULL,
            order_date TEXT NOT NULL, shipment_date TEXT NOT NULL)""")
        db.executemany("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)", (
            (i, ORGANIZATION, REGIONS[(i - 1) % 3], f"合成客户{(i - 1) % 8 + 1:02d}",
             10000 + (i % 997) * 123, 1 + i % 40,
             f"2026-11-{1 + (i - 1) % 14:02d}", DATA_DATE if i % 5 else "2026-11-13")
            for i in range(1, 100001)))
        db.execute("INSERT INTO fixture_meta VALUES(1)")
    os.chmod(path, 0o600)


def aggregate(path: Path, kind: str, dataset_rows: int = 10000, region: str = "华北",
              organization: str = ORGANIZATION) -> dict:
    """Only fixed aggregate SELECTs run against a read-only SQLite connection."""
    if type(dataset_rows) is not int or dataset_rows not in (10000, 100000):
        raise ValueError("Only the 10k and 100k synthetic datasets are supported")
    if region not in ("全部", *REGIONS) or organization != ORGANIZATION or kind not in ("orders", "shipments"):
        raise ValueError("Query is outside the synthetic fixture scope")
    where = "organization=? AND id<=?"
    parameters: list = [organization, dataset_rows]
    if region != "全部":
        where += " AND region=?"
        parameters.append(region)
    if kind == "shipments":
        where += " AND shipment_date=?"
        parameters.append(DATA_DATE)
    group = "region" if kind == "orders" else "customer"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        records = db.execute(f"SELECT {group},count(*),sum(amount_cents),sum(quantity) FROM orders WHERE {where} GROUP BY {group} ORDER BY {group}", parameters).fetchall()
    matched = sum(row[1] for row in records)
    columns = [{"key": group, "label": "区域" if kind == "orders" else "客户", "type": "string"},
               {"key": "record_count", "label": "订单数" if kind == "orders" else "发货记录数", "type": "integer", "unit": "笔"},
               {"key": "amount", "label": "订单金额" if kind == "orders" else "关联订单金额", "type": "number", "unit": "CNY"},
               {"key": "quantity", "label": "数量", "type": "integer", "unit": "件"}]
    rows = [{group: row[0], "record_count": row[1], "amount": row[2] / 100, "quantity": row[3]} for row in records]
    scope = {"organization": organization, "region": region,
             "date": DATA_DATE if kind == "shipments" else "2026-11-01 至 2026-11-14",
             "timezone": "Asia/Shanghai", "aggregation": f"按{'区域' if kind == 'orders' else '客户'}汇总；金额为合成含税金额",
             "数据集记录数": dataset_rows, "匹配原始记录数": matched}
    title = "合成订单区域汇总" if kind == "orders" else "合成客户发货汇总"
    result = {"schema_version": "business-result/v1", "title": title, "columns": columns, "rows": rows,
              "summary": f"{NOTICE}。真实查询 {dataset_rows:,} 行合成数据中的授权范围，匹配 {matched:,} 条原始记录，返回 {len(rows)} 行汇总。",
              "scope": scope, "synthetic": True, "complete": True, "truncated": False,
              "total_rows": len(rows), "returned_rows": len(rows), "original_rows": dataset_rows,
              "data_time": DATA_TIME, "cache_hit": False, "warnings": ["仅验证产品链路与聚合取数，不代表华微数据、模型质量或模型响应速度。"],
              "business_domain": "订单" if kind == "orders" else "发货", "process": "只读合成验收",
              "object": "orders", "metrics": ["record_count", "amount", "quantity"],
              "fields": [column["key"] for column in columns], "filters": scope,
              "aggregation": "database_group_by", "row_count": len(rows), "time_range": scope["date"]}
    query_digest = hashlib.sha256(json.dumps([kind, dataset_rows, region, organization], ensure_ascii=False).encode()).hexdigest()[:20]
    result["sources"] = [{"kind": "data", "title": title + " · 合成记录",
        "locator": {"database": "synthetic-orders-v1", "table": "orders", "fields": result["fields"],
                    "record_keys": [str(row[group]) for row in rows], "time_range": scope["date"],
                    "timezone": "Asia/Shanghai", "queried_at": datetime.now(timezone.utc).isoformat(),
                    "trace_id": query_digest, "synthetic": True, "matched_original_rows": matched,
                    "dataset_rows": dataset_rows, "aggregation": "database_group_by"},
        "excerpt": json.dumps({"synthetic": True, "scope": scope, "rows": rows}, ensure_ascii=False)}]
    return result


def create_mcp(path: Path, token: str, observations: dict):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from starlette.responses import JSONResponse
    mcp = FastMCP("business-loop-synthetic", host="127.0.0.1", port=MCP_PORT,
        instructions=NOTICE + "。仅查询本地合成订单，无生产数据或写入工具。",
        streamable_http_path="/mcp", stateless_http=True, json_response=True, log_level="WARNING")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    async def query(kind, dataset_rows, region, organization, delay=0):
        call = {"kind": kind, "dataset_rows": dataset_rows, "region": region, "slow": bool(delay),
                "started_at": time.time(), "completed": False}
        observations["calls"].append(call)
        secure_json(path.parent / "transport-observations.json", observations)
        if delay:
            await asyncio.sleep(delay)
        result = await asyncio.to_thread(aggregate, path, kind, dataset_rows, region, organization)
        call.update(completed=True, returned_rows=len(result["rows"]), matched_original_rows=result["scope"]["匹配原始记录数"],
                    payload_bytes=len(json.dumps(result, ensure_ascii=False).encode()))
        secure_json(path.parent / "transport-observations.json", observations)
        return result

    @mcp.tool(annotations=annotations, structured_output=True)
    async def order_summary(dataset_rows: Literal[10000, 100000] = 10000,
            region: Literal["全部", "华北", "华东", "华南"] = "华北",
            organization: Literal["合成验收组织"] = ORGANIZATION) -> dict[str, Any]:
        """查询1万或10万行合成订单，数据库按区域聚合；仅返1至3行。"""
        return await query("orders", dataset_rows, region, organization)

    @mcp.tool(annotations=annotations, structured_output=True)
    async def shipment_summary(dataset_rows: Literal[10000, 100000] = 10000,
            region: Literal["全部", "华北", "华东", "华南"] = "全部",
            organization: Literal["合成验收组织"] = ORGANIZATION) -> dict[str, Any]:
        """查询合成日期2026-11-14的发货，数据库按客户聚合；最多8行。"""
        return await query("shipments", dataset_rows, region, organization)

    @mcp.tool(annotations=annotations, structured_output=True)
    async def slow_order_summary(dataset_rows: Literal[10000, 100000] = 10000,
            region: Literal["全部", "华北", "华东", "华南"] = "华北",
            organization: Literal["合成验收组织"] = ORGANIZATION) -> dict[str, Any]:
        """取消验收专用：等待20秒后再执行只读合成订单汇总。"""
        return await query("orders", dataset_rows, region, organization, delay=20)

    inner = mcp.streamable_http_app()
    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            authorization = dict(scope["headers"]).get(b"authorization", b"")
            valid = secrets.compare_digest(authorization, ("Bearer " + token).encode())
            observations["authenticated_requests"] += int(valid)
            observations["rejected_requests"] += int(not valid)
            if not valid:
                secure_json(path.parent / "transport-observations.json", observations)
                return await JSONResponse({"error": "Synthetic MCP credential required"}, status_code=401)(scope, receive, send)
        await inner(scope, receive, send)
    return guarded


def runtime_class():
    from app.context import context_snapshot
    from app.db import dump, uid
    from app.harness_tools import HarnessToolBridge
    from app.runtime import WorkspaceRuntime
    from app.security import AppError

    class SyntheticPreviewRuntime(WorkspaceRuntime):
        """Test-only planner; actual tools, ACLs, approvals and evidence are product code."""
        close_after_run = True

        async def connect(self):
            self.session_id = "synthetic-preview-" + uid()
            self.interrupted = False
            self.pending = None
            self.bridge = HarnessToolBridge(self)

        async def execute(self, run, message):
            self.current_run = run
            metadata = json.loads(run.get("execution_meta") or "{}")
            metadata.update(execution_kind="synthetic_acceptance_no_model", model_call_count=0, synthetic=True)
            self.service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump(metadata), run["id"]))
            await self.emit("usage.updated", {"input_tokens": None, "output_tokens": None,
                "cache_read_input_tokens": None, "cache_creation_input_tokens": None,
                "estimated_cost_usd": None, "currency": "USD", "source": "synthetic_acceptance_no_model",
                "model_call_count": 0, "synthetic": True, "note": NOTICE + "；Token与模型费用不适用，不填造计数。"})
            text = message["text"]
            if not any(word in text for word in ("订单", "发货", "取消", "慢速")):
                await self.emit("message.delta", {"message_id": run["assistant_message_id"],
                    "text": NOTICE + "。本预览仅支持固定合成验收题：10万条华北订单总额、1万条今日发货、取消测试。"})
                return
            name = "slow_order_summary" if any(word in text for word in ("取消", "慢速")) else "shipment_summary" if "发货" in text else "order_summary"
            candidates = [tool for tool in self.bridge.business_tools.values() if tool.server_tool == name]
            if len(candidates) != 1:
                raise AppError(409, "PREVIEW_TOOL_UNAVAILABLE", "合成验收工具未绑定或不可用，请检查业务连接")
            arguments = {"dataset_rows": 100000 if any(word in text for word in ("10万", "十万", "100000", "100,000", "100k")) else 10000,
                         "region": next((region for region in REGIONS if region in text), "全部" if name == "shipment_summary" else "华北"),
                         "organization": ORGANIZATION}
            self.pending = asyncio.create_task(self.bridge.invoke({"schema": "workspace-tool/v1", "sessionId": self.session_id,
                "callId": "preview-" + uid(), "toolName": candidates[0].name, "arguments": arguments}))
            result = await self.pending
            if self.interrupted:
                raise asyncio.CancelledError()
            if result.get("is_error") or result.get("isError"):
                raise AppError(502, "PREVIEW_TOOL_FAILED", "合成验收工具未完成，请查看执行记录")
            structured = result.get("structuredContent") or {}
            markers = []
            for block in result.get("content", []):
                if block.get("type") == "text":
                    import re
                    markers.extend(re.findall(r"\[\[source:[A-Za-z0-9_-]+\]\]", block.get("text", "")))
            summary = structured.get("summary") or NOTICE + "。只读工具已返回结果。"
            await self.emit("message.delta", {"message_id": run["assistant_message_id"],
                "text": summary + "\n\n结果来自本机 MCP 与真实数据库聚合；这不是模型生成答案。" + ("\n\n" + " ".join(dict.fromkeys(markers)) if markers else "")})

        async def interrupt(self):
            self.interrupted = True
            if self.pending and not self.pending.done():
                self.pending.cancel()

        async def close(self):
            await self.interrupt()

        async def context_usage(self):
            return context_snapshot(error=NOTICE + "，没有原生模型上下文。", runtime_type="harness")
    return SyntheticPreviewRuntime


def create_product(directory: Path, credentials: dict):
    # Do not inherit any real provider credential into this isolated process.
    from app.config import PROVIDER_KEYS, Settings
    for key in PROVIDER_KEYS:
        os.environ.pop(key, None)
    os.environ["AGENT_KNOWLEDGE_EMBEDDING_MODEL"] = "local-lexical-v1"
    catalog_path = directory / "model-catalog.json"
    secure_json(catalog_path, {"default_model_id": "synthetic-no-model", "models": [{
        "id": "synthetic-no-model", "label": "合成验收 · 无模型推理", "upstream_model": "synthetic-no-model",
        "reasoning_levels": {"default": None}, "default_reasoning_level": "default",
        "provider_env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_API_KEY": "unused-synthetic-only"}}]})
    settings = Settings(data_dir=directory / "product-data", brand_name=BRAND,
        model_catalog=str(catalog_path), provider_settings="", provider_test_mode=True,
        runtime_type="harness", deployment_mode="normal", trial_capabilities=None,
        mcp_hosts=("127.0.0.1",), origins=("http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173"),
        cookie_secure=False, idle_seconds=600, approval_seconds=120,
        model_router_enabled=False, tab_completion_enabled=False,
        knowledge_vision_enabled=False, deepseek_api_enabled=False, image_generation_enabled=False,
        feishu_h5_enabled=False, workflows_enabled=False)
    from app.main import create_app
    app = create_app(settings, runtime_factory=runtime_class())
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            service = application.state.service
            if not service.db.one("SELECT id FROM users WHERE username=?", (credentials["username"],)):
                service.create_user({"username": credentials["username"], "password": credentials["password"],
                                     "display_name": "合成验收管理员", "role": "admin"})
            yield
    app.router.lifespan_context = lifespan
    if (ROOT / "client" / "dist" / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=ROOT / "client" / "dist", html=True), name="synthetic-product-ui")
    return app


class LocalServer:
    def __init__(self, app, sock):
        import uvicorn
        self.socket = sock
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, log_config=None))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [sock]}, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local preview server did not start")
            time.sleep(0.05)

    def stop(self):
        self.server.should_exit = True
        if self.thread.ident is None:
            self.socket.close()
            return
        self.thread.join(8)
        if self.thread.is_alive():
            self.server.force_exit = True
            self.thread.join(2)
        self.socket.close()


def bind_loopback(port: int) -> socket.socket:
    sock = socket.socket()
    try:
        # Allow restarting this owned service after TCP TIME_WAIT, never sharing
        # a listening port (SO_REUSEPORT remains disabled).
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
        return sock
    except BaseException:
        sock.close()
        raise RuntimeError(f"Loopback port {port} unavailable; existing services were not stopped") from None


@contextmanager
def api_client(directory: Path):
    import httpx
    credentials = load_credentials(directory)
    with httpx.Client(base_url=f"http://127.0.0.1:{API_PORT}", timeout=40, trust_env=False) as client:
        config = client.get("/api/v1/config")
        if config.status_code != 200 or config.json().get("brand_name") != BRAND:
            raise RuntimeError("API endpoint is not this synthetic preview")
        response = client.post("/api/v1/auth/login", json={"username": credentials["username"], "password": credentials["password"]})
        if response.status_code != 200:
            raise RuntimeError("Synthetic preview login failed; credential content withheld")
        yield client


def request(client, method, path, payload=None, expected=200):
    response = client.request(method, path, json=payload)
    if response.status_code != expected:
        raise RuntimeError(f"Preview API {method} {path.split('?')[0]} returned HTTP {response.status_code}; body withheld")
    return response.json()


def bootstrap(directory: Path, credentials: dict) -> dict:
    with api_client(directory) as client:
        connections = request(client, "GET", "/api/v1/connections")
        connection = next((value for value in connections if value["name"] == "合成订单与发货 · 本机MCP"), None)
        if not connection:
            connection = request(client, "POST", "/api/v1/connections", {"name": "合成订单与发货 · 本机MCP",
                "url": f"http://127.0.0.1:{MCP_PORT}/mcp", "auth_type": "bearer", "credential": credentials["mcp_bearer"],
                "scope": "team", "source_metadata": {"synthetic": "true", "purpose": "本地验收，无客户数据"}}, expected=201)
        tested = request(client, "POST", f"/api/v1/connections/{connection['id']}/test")
        if tested["status"] != "connected" or {tool["name"] for tool in tested["tools"]} != set(TOOLS):
            raise RuntimeError("Actual MCP discovery did not return the expected read-only synthetic tools")
        confirmations = {tool["name"]: {"connection_revision": tested["revision"], "definition_digest": tool["definition_digest"],
            "basis": "本地合成数据库仅开放固定只读聚合工具，已检查实现；不代表生产工具验收。"}
            for tool in tested["tools"] if tool.get("definition_digest") and tool.get("read_only_confirmation_eligible")}
        body = {"name": "业务查询 · 合成验收", "description": "仅合成验收，无模型推理。可试：10万条华北订单总额、1万条今日发货、取消测试。",
                "instructions": NOTICE + "。只调用授权的合成工具，不执行生产写入。", "scope": "team",
                "connections": [{"connection_id": connection["id"], "allowed_tools": list(TOOLS), "auto_approved_tools": list(TOOLS),
                                 "read_only_confirmations": confirmations}], "knowledge_base_ids": [], "skill_ids": [], "web_search_enabled": False,
                "default_model_id": "synthetic-no-model", "default_reasoning_level": "default"}
        assistants = request(client, "GET", "/api/v1/assistants")
        assistant = next((value for value in assistants if value["name"] == body["name"]), None)
        assistant = request(client, "PATCH", f"/api/v1/assistants/{assistant['id']}", {**body, "expected_revision": assistant["revision"]}) if assistant else request(client, "POST", "/api/v1/assistants", body, expected=201)
        metadata = {"synthetic": True, "model_inference": False, "brand": BRAND, "api_url": f"http://127.0.0.1:{API_PORT}",
            "mcp_url": f"http://127.0.0.1:{MCP_PORT}/mcp", "connection_id": connection["id"], "assistant_id": assistant["id"],
            "discovered_tools": [tool["name"] for tool in tested["tools"]], "pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}
        secure_json(directory / "preview.json", metadata)
        return metadata


def poll(client, run_id, terminal=True):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        run = request(client, "GET", f"/api/v1/runs/{run_id}")
        if (terminal and run["status"] in ("succeeded", "failed", "cancelled", "interrupted")) or (not terminal and run["status"] == "running"):
            return run
        time.sleep(0.05)
    raise RuntimeError("Synthetic preview run did not settle within the bounded observation window")


def verify(directory: Path) -> dict:
    metadata = json.loads((directory / "preview.json").read_text(encoding="utf-8"))
    receipt = {"synthetic": True, "model_inference": False, "api_url": metadata["api_url"], "cases": [], "verified_at": datetime.now(timezone.utc).isoformat()}
    with api_client(directory) as client:
        def submit(text):
            conversation = request(client, "POST", "/api/v1/conversations", {"assistant_id": metadata["assistant_id"], "title": text}, expected=201)
            run = request(client, "POST", f"/api/v1/conversations/{conversation['id']}/messages",
                {"text": text, "file_ids": [], "client_request_id": "preview-" + secrets.token_hex(8)}, expected=202)
            return conversation, run["run_id"]

        for dataset_rows, kind, text, region in [(10000, "orders", "1万条华北订单总额", "华北"),
                (100000, "orders", "10万条华北订单总额", "华北"), (100000, "shipments", "10万条今日发货", "全部")]:
            conversation, run_id = submit(text)
            run = poll(client, run_id)
            trace = request(client, "GET", f"/api/v1/runs/{run_id}/trace")
            events = [event for event in trace["events"] if event["type"] == "tool.completed" and event["data"].get("success") is True]
            expected = aggregate(directory / "synthetic.sqlite3", kind, dataset_rows, region)
            result = events[0]["data"].get("business_result") if len(events) == 1 else None
            if run["status"] != "succeeded" or not result or result.get("rows") != expected["rows"] or result.get("synthetic") is not True:
                raise RuntimeError("Synthetic result did not match the independent database aggregate or event projection")
            if any(trace.get("usage", {}).get(key) is not None for key in ("input_tokens", "output_tokens", "estimated_cost_usd")):
                raise RuntimeError("Synthetic preview must not fabricate model usage")
            sources = events[0]["data"].get("source_ids", [])
            if not sources or any(request(client, "GET", f"/api/v1/sources/{source}").get("kind") != "data" for source in sources):
                raise RuntimeError("Actual MCP result did not register readable evidence")
            secure_json(directory / ("trace-" + run_id + ".json"), trace)
            receipt["cases"].append({"case": text, "status": "passed", "conversation_id": conversation["id"], "run_id": run_id,
                "original_rows": dataset_rows, "returned_rows": len(result["rows"]), "matched_records": expected["scope"]["匹配原始记录数"],
                "source_count": len(sources), "duration_ms": trace["run"]["duration_ms"], "model_tokens": None})

        bad = request(client, "POST", "/api/v1/connections", {"name": "合成验收 · 错误凭据",
            "url": metadata["mcp_url"], "auth_type": "bearer", "credential": "intentionally-invalid-preview-credential"}, expected=201)
        tested = request(client, "POST", f"/api/v1/connections/{bad['id']}/test")
        if tested["status"] != "needs_auth" or tested["tools"]:
            raise RuntimeError("Incorrect MCP credentials did not fail closed")
        receipt["cases"].append({"case": "MCP错误凭据", "status": "passed", "connection_id": bad["id"], "observed_status": tested["status"]})

        observation_file = directory / "transport-observations.json"
        prior_calls = len(json.loads(observation_file.read_text(encoding="utf-8"))["calls"])
        conversation, run_id = submit("取消测试：10万条华北订单总额")
        poll(client, run_id, terminal=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            observed = json.loads(observation_file.read_text(encoding="utf-8"))
            if len(observed["calls"]) > prior_calls and observed["calls"][-1]["slow"]:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Cancel test did not reach the actual MCP HTTP tool handler")
        request(client, "POST", f"/api/v1/runs/{run_id}/cancel")
        cancelled = poll(client, run_id)
        trace = request(client, "GET", f"/api/v1/runs/{run_id}/trace")
        if cancelled["status"] != "cancelled" or any(event["type"] == "tool.completed" and event["data"].get("success") for event in trace["events"]):
            raise RuntimeError("Cancelled run returned a successful business result")
        secure_json(directory / ("trace-" + run_id + ".json"), trace)
        receipt["cases"].append({"case": "调用中取消", "status": "passed", "conversation_id": conversation["id"], "run_id": run_id,
                                 "observed_status": cancelled["status"], "real_mcp_handler_started": True, "remote_stop_proven": False})
    secure_json(directory / "acceptance.json", receipt)
    return receipt


def distribution(values: list[int | float]) -> dict:
    """Nearest-rank observations, with explicit sample count; never an SLA."""
    ordered = sorted(values)
    return {"samples": len(ordered),
            "p50": ordered[math.ceil(len(ordered) * 0.50) - 1] if ordered else None,
            "p95": ordered[math.ceil(len(ordered) * 0.95) - 1] if ordered else None,
            "min": ordered[0] if ordered else None, "max": ordered[-1] if ordered else None}


def benchmark(directory: Path) -> dict:
    """Exactly 20 sequential real HTTP runs, 10 per actual data-size subset."""
    metadata = json.loads((directory / "preview.json").read_text(encoding="utf-8"))
    receipt = {"synthetic": True, "model_inference": False, "requested_runs": 20,
        "business_result_cache": "not_implemented_in_fixture", "os_database_cache": "not_flushed_or_measured",
        "concurrency": 1, "percentile_method": "nearest_rank", "cases": [], "groups": [],
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "limitations": ["无模型推理，Token与模型费用不适用。", "产品链路时长来自服务端任务创建至结束，不是浏览器首屏时长。",
            "model_input_bytes是工具返回到模型边界的UTF-8文本字节，本预览没有将它送入模型。",
            "仅测试无业务结果缓存；没有热缓存收益对照，未清理数据库或操作系统缓存。", "每组10次，单并发观察，不作为稳定SLA或容量结论。"]}
    with api_client(directory) as client:
        for index in range(20):
            size = 10000 if index % 2 == 0 else 100000
            text = f"{'1万' if size == 10000 else '10万'}条华北订单总额"
            case = {"index": index + 1, "original_rows": size, "status": "failed"}
            try:
                conversation = request(client, "POST", "/api/v1/conversations", {
                    "assistant_id": metadata["assistant_id"], "title": f"合成链路测量 {index + 1:02d} · {text}"}, expected=201)
                observed_start = time.monotonic()
                submitted = request(client, "POST", f"/api/v1/conversations/{conversation['id']}/messages",
                    {"text": text, "file_ids": [], "client_request_id": "measure-" + secrets.token_hex(8)}, expected=202)
                run_id = submitted["run_id"]
                run = poll(client, run_id)
                observed_ms = round((time.monotonic() - observed_start) * 1000, 2)
                trace = request(client, "GET", f"/api/v1/runs/{run_id}/trace")
                secure_json(directory / ("benchmark-trace-" + run_id + ".json"), trace)
                events = [event for event in trace["events"] if event["type"] == "tool.completed" and event["data"].get("success") is True]
                data = events[0]["data"] if len(events) == 1 else {}
                result = data.get("business_result") or {}
                expected = aggregate(directory / "synthetic.sqlite3", "orders", size, "华北")
                facts = data.get("result_facts") or {}
                count = data.get("model_input_bytes")
                correct = (run["status"] == "succeeded" and result.get("rows") == expected["rows"]
                    and result.get("synthetic") is True and facts.get("cache_hit") is False
                    and facts.get("original_rows") == size and type(count) is int and count > 0)
                usage = trace.get("usage") or {}
                unknown_usage = all(usage.get(key) is None for key in ("input_tokens", "output_tokens", "estimated_cost_usd"))
                case.update(status="passed" if correct and unknown_usage else "failed", run_status=run["status"],
                    run_id=run_id, conversation_id=conversation["id"], returned_rows=len(result.get("rows", [])),
                    matched_original_rows=expected["scope"]["匹配原始记录数"], model_input_bytes=count,
                    business_cache_hit=facts.get("cache_hit"), product_chain_ms=trace["run"]["duration_ms"],
                    client_observed_until_poll_ms=observed_ms, model_tokens=None)
            except Exception as exc:
                case["error_type"] = type(exc).__name__
            receipt["cases"].append(case)
            secure_json(directory / "benchmark.json", receipt)
    for size in (10000, 100000):
        all_cases = [case for case in receipt["cases"] if case["original_rows"] == size]
        passed = [case for case in all_cases if case["status"] == "passed"]
        receipt["groups"].append({"original_rows": size, "attempts": len(all_cases), "passed": len(passed),
            "failed": len(all_cases) - len(passed), "business_cache": "none",
            "product_chain_ms": distribution([case["product_chain_ms"] for case in passed]),
            "client_observed_until_poll_ms": distribution([case["client_observed_until_poll_ms"] for case in passed]),
            "model_input_bytes": distribution([case["model_input_bytes"] for case in passed]),
            "returned_rows": sorted({case["returned_rows"] for case in passed})})
    receipt["status"] = "passed" if all(case["status"] == "passed" for case in receipt["cases"]) else "failed"
    secure_json(directory / "benchmark.json", receipt)
    return receipt


def serve(directory: Path):
    os.umask(0o077)
    if directory.is_symlink():
        raise RuntimeError("Private preview directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    with (directory / "preview.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This private preview is already running") from None
        sockets, servers = [], []
        try:
            sockets = [bind_loopback(API_PORT)]
            sockets.append(bind_loopback(MCP_PORT))
            credentials = load_credentials(directory)
            seed_database(directory / "synthetic.sqlite3")
            observations = {"authenticated_requests": 0, "rejected_requests": 0, "calls": []}
            logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
            servers = [LocalServer(create_mcp(directory / "synthetic.sqlite3", credentials["mcp_bearer"], observations), sockets[1]),
                       LocalServer(create_product(directory, credentials), sockets[0])]
            for server in servers:
                server.start()
            metadata = bootstrap(directory, credentials)
            print(json.dumps({"status": "ready", "synthetic": True, "model_inference": False, "api_url": metadata["api_url"],
                              "pid": os.getpid(), "credentials_file": str(directory / "credentials.json")}, ensure_ascii=False), flush=True)
            stopped = threading.Event()
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stopped.set())
            while not stopped.wait(1):
                if not all(server.thread.is_alive() for server in servers):
                    raise RuntimeError("One local preview service exited")
        finally:
            for server in reversed(servers):
                server.stop()
            for sock in sockets:
                sock.close()
            if "observations" in locals():
                secure_json(directory / "transport-observations.json", observations)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("serve", "verify", "benchmark"))
    args = parser.parse_args()
    try:
        if args.command == "serve":
            serve(PRIVATE)
        elif args.command == "verify":
            receipt = verify(PRIVATE)
            print(json.dumps({"status": "passed", "synthetic": True, "cases": len(receipt["cases"]),
                              "receipt": str(PRIVATE / "acceptance.json")}, ensure_ascii=False))
        else:
            receipt = benchmark(PRIVATE)
            print(json.dumps({"status": receipt["status"], "synthetic": True, "model_inference": False,
                "groups": receipt["groups"], "receipt": str(PRIVATE / "benchmark.json")}, ensure_ascii=False))
            if receipt["status"] != "passed":
                raise SystemExit(1)
    except Exception as exc:
        # Exception types are useful; arbitrary provider/request bodies are not.
        print(f"Preview failed: {type(exc).__name__}: {str(exc) if isinstance(exc, RuntimeError) else 'details withheld'}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
