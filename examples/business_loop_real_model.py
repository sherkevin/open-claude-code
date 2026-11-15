"""Isolated real Harness + HTTP MCP acceptance against synthetic business data.

serve creates an isolated product, with no runtime_factory replacement. It does
not submit model requests. verify requires --real-model and uses the explicitly
selected catalog model. No credentials, provider responses or prompts are printed.
Do not point --directory at an existing product data directory.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
import re
from pathlib import Path
import secrets
import signal
import stat
import sys
import time
from typing import Any, Literal
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import business_loop_preview as fixture

BRAND = "知行工作台 · 真实模型演示验收"
MARKER = "real-model-synthetic-business/v1"
TOOLS = ("order_summary", "shipment_summary", "slow_order_summary", "oversized_order_rows")
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def private_directory(path: Path, *, create=False) -> Path:
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise RuntimeError("Private directory must not contain symlinks")
    marker = path / "acceptance-scope.json"
    if create:
        if path.exists() and any(path.iterdir()) and not marker.is_file():
            raise RuntimeError("Refusing a nonempty directory without this acceptance marker")
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        if not marker.exists():
            fixture.secure_json(marker, {"schema": MARKER, "synthetic_dataset": True})
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise RuntimeError("Private directory must have mode 0700")
    if private_json(marker).get("schema") != MARKER:
        raise RuntimeError("Acceptance directory does not match the expected scope")
    return path


def private_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RuntimeError("Private JSON must be a regular file with mode 0600")
    return json.loads(path.read_text(encoding="utf-8"))


def business_result(path, kind, dataset_rows, region):
    from decimal import Decimal
    result = fixture.aggregate(path, kind, dataset_rows, region)
    # Keep provenance explicit without presenting an internal acceptance script
    # as the product's status. Machine flags and source locators stay intact.
    result["title"] = "订单区域汇总" if kind == "orders" else "客户发货汇总"
    result["summary"] = f"匹配 {result['scope']['匹配原始记录数']:,} 条记录，按{'区域' if kind == 'orders' else '客户'}汇总。"
    result["warnings"] = ["数据来源：示例数据。"]
    result["scope"]["aggregation"] = f"按{'区域' if kind == 'orders' else '客户'}汇总；金额为含税金额"
    result["process"] = "订单查询" if kind == "orders" else "发货查询"
    for source in result["sources"]:
        source["title"] = result["title"] + " · 示例数据"
    # This fixture owns the SQL contract: these three columns are additive,
    # disjoint group sums in the same scope/unit. General MCP tools must make
    # their own explicit declaration; the host never guesses from a column name.
    result["aggregates"] = [{"column": column["key"], "label": column["label"] + "合计",
        "operation": "sum", "scope": "returned_rows", "unit": column["unit"],
        "claim_labels": (["发货", "发货数量", "发货件数", "数量", "件数"] if kind == "shipments" and column["key"] == "quantity"
                         else [column["label"]]),
        "value": float(sum((Decimal(str(row[column["key"]])) for row in result["rows"]), Decimal(0)))}
        for column in result["columns"] if column["key"] in {"record_count", "amount", "quantity"}]
    return result


def create_mcp(path: Path, token: str):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
    from starlette.responses import JSONResponse
    mcp = FastMCP("real-model-synthetic-business", streamable_http_path="/mcp",
                  stateless_http=True, json_response=True, log_level="WARNING")
    calls = []
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    async def query(tool, kind, dataset_rows, region, slow=False):
        call = {"tool": tool, "dataset_rows": dataset_rows, "region": region,
                "started_at": time.time(), "completed": False}
        calls.append(call)
        fixture.secure_json(path.parent / "mcp-calls.json", {"synthetic_dataset": True, "calls": calls})
        started = time.monotonic()
        if slow:
            await asyncio.sleep(20)
        result = await asyncio.to_thread(business_result, path, kind, dataset_rows, region)
        call.update(completed=True, duration_ms=round((time.monotonic() - started) * 1000, 2), returned_rows=len(result["rows"]))
        fixture.secure_json(path.parent / "mcp-calls.json", {"synthetic_dataset": True, "calls": calls})
        return result

    @mcp.tool(annotations=annotations, structured_output=True)
    async def order_summary(dataset_rows: Literal[10000, 100000] = 10000,
                            region: Literal["全部", "华北", "华东", "华南"] = "华北") -> dict[str, Any]:
        """查询演示订单区域总额，业务范围2026-11-01至2026-11-14；后台聚合，不返回全表。"""
        return await query("order_summary", "orders", dataset_rows, region)

    @mcp.tool(annotations=annotations, structured_output=True)
    async def shipment_summary(dataset_rows: Literal[10000, 100000] = 10000,
                               region: Literal["全部", "华北", "华东", "华南"] = "全部") -> dict[str, Any]:
        """查询演示业务日期2026-11-14向哪些客户发货及数量，按客户汇总；数量单位为件。"""
        return await query("shipment_summary", "shipments", dataset_rows, region)

    @mcp.tool(annotations=annotations, structured_output=True)
    async def slow_order_summary(dataset_rows: Literal[10000, 100000] = 10000,
                                 region: Literal["全部", "华北", "华东", "华南"] = "华北") -> dict[str, Any]:
        """取消验收专用，等待20秒再查询演示订单；只在用户明确要求取消测试时使用。"""
        return await query("slow_order_summary", "orders", dataset_rows, region, slow=True)

    @mcp.tool(annotations=annotations, structured_output=True)
    async def oversized_order_rows() -> dict[str, Any]:
        """超限验收专用：故意返回201行演示数据；宿主应拒绝，不得把样本当全量。"""
        calls.append({"tool": "oversized_order_rows", "started_at": time.time(), "completed": True})
        fixture.secure_json(path.parent / "mcp-calls.json", {"synthetic_dataset": True, "calls": calls})
        return {"title": "超限验收", "rows": [{"order": i} for i in range(201)], "complete": True, "synthetic": True}

    inner = mcp.streamable_http_app()
    async def guarded(scope, receive, send):
        if scope["type"] == "http" and not secrets.compare_digest(
                dict(scope["headers"]).get(b"authorization", b""), ("Bearer " + token).encode()):
            return await JSONResponse({"error": "Synthetic MCP authentication required"}, status_code=401)(scope, receive, send)
        await inner(scope, receive, send)
    return guarded


def validated_settings(args, directory):
    from app.config import Settings
    from app.model_catalog import ModelCatalog
    from app.provider_policy import provider_route
    for name in ("AGENT_MODEL_CATALOG", "AGENT_PROVIDER_SETTINGS"):
        if os.environ.get(name):
            private_json(Path(os.environ[name]))
    settings = Settings(data_dir=directory / "product-data", runtime_type="harness", brand_name=BRAND,
        provider_test_mode=False, mcp_hosts=("127.0.0.1",), origins=(f"http://127.0.0.1:{args.api_port}",),
        deployment_mode="normal", cookie_secure=False, session_cookie_name="zhixing_session", cookie_path="/",
        model_router_enabled=False, tab_completion_enabled=False,
        knowledge_vision_enabled=False, deepseek_api_enabled=False, image_generation_enabled=False,
        feishu_h5_enabled=False, workflows_enabled=False)
    catalog = ModelCatalog(settings)
    selection = catalog.select(args.model_id, args.reasoning_level)
    provider = {**settings.provider_env(), **catalog.models[selection["model_id"]].get("provider_env", {})}
    if not provider_route(provider).production_ready or not (provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError("A configured authorized TokenHub route is required; no synthetic model fallback")
    if not settings.harness_node_path or not settings.harness_source_dir:
        raise RuntimeError("Dedicated Harness Node/source paths are required")
    if selection["upstream_effort"] not in (None, "low", "high", "max"):
        raise RuntimeError("Selected catalog reasoning level is not supported by Harness")
    return settings


def prepare(args):
    import subprocess
    from app.harness_runtime import official_client, PINNED_COMMIT
    directory = private_directory(args.directory, create=True)
    settings = validated_settings(args, directory)
    node, source = Path(settings.harness_node_path), Path(settings.harness_source_dir)
    if not node.is_file() or not os.access(node, os.X_OK):
        raise RuntimeError("Dedicated Harness Node executable is unavailable")
    version = subprocess.check_output([str(node), "--version"], text=True, stderr=subprocess.DEVNULL).strip()
    if version != "v24.9.0":
        raise RuntimeError("Harness acceptance requires the pinned Node v24.9.0")
    official_client(source)
    if not (source / "node_modules/tsx/dist/esm/index.mjs").is_file():
        raise RuntimeError("Pinned Harness runtime dependencies are not installed")
    report = {"schema": MARKER, "status": "prepared", "synthetic_dataset": True, "real_model": False,
              "model_requests_submitted": 0, "model_id": args.model_id, "reasoning_level": args.reasoning_level,
              "harness_commit": PINNED_COMMIT, "node_version": version, "api_port": args.api_port, "mcp_port": args.mcp_port,
              "max_steps": settings.max_turns, "max_output_tokens": settings.harness_max_output_tokens,
              "usd_hard_limit_enforced": False}
    fixture.secure_json(directory / "prepared.json", report)
    print(json.dumps(report), flush=True)


def create_product(args, directory, credentials):
    from app.main import create_app
    settings = validated_settings(args, directory)
    # Deliberately no runtime_factory: the real product chooses HarnessRuntime.
    app = create_app(settings)
    original = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            service = application.state.service
            if not service.db.one("SELECT id FROM users WHERE username=?", (credentials["username"],)):
                service.create_user({"username": credentials["username"], "password": credentials["password"],
                                     "display_name": "真实模型演示验收", "role": "admin"})
            yield
    app.router.lifespan_context = lifespan
    return app


@contextmanager
def client_for(directory):
    import httpx
    metadata = private_json(directory / "instance.json")
    credentials = private_json(directory / "credentials.json")
    endpoint = urlsplit(metadata["api_url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.username or endpoint.password or endpoint.path or endpoint.query or endpoint.fragment:
        raise RuntimeError("Acceptance API must be the owned loopback endpoint")
    with httpx.Client(base_url=metadata["api_url"], timeout=15, trust_env=False) as client:
        config = fixture.request(client, "GET", "/api/v1/config")
        if config.get("brand_name") != BRAND:
            raise RuntimeError("Refusing an API outside the isolated acceptance scope")
        fixture.request(client, "POST", "/api/v1/auth/login", {"username": credentials["username"], "password": credentials["password"]})
        yield client, metadata


def bootstrap(directory):
    with client_for(directory) as (client, metadata):
        credentials = private_json(directory / "credentials.json")
        connection = fixture.request(client, "POST", "/api/v1/connections", {"name": "真实模型验收 · 演示MCP",
            "url": metadata["mcp_url"], "auth_type": "bearer", "credential": credentials["mcp_bearer"], "scope": "private"}, expected=201)
        tested = fixture.request(client, "POST", f"/api/v1/connections/{connection['id']}/test")
        if tested["status"] != "connected" or {tool["name"] for tool in tested["tools"]} != set(TOOLS):
            raise RuntimeError("Synthetic MCP discovery did not return the expected tools")
        confirmations = {tool["name"]: {"connection_revision": tested["revision"], "definition_digest": tool["definition_digest"],
            "basis": "已核对独立演示MCP的固定只读SELECT实现；不授权任何生产业务工具。"}
            for tool in tested["tools"] if tool.get("read_only_confirmation_eligible")}
        assistant = fixture.request(client, "POST", "/api/v1/assistants", {"name": "真实模型 · 演示业务验收", "scope": "private",
            "description": "真实模型与演示业务数据，不含华微数据。",
            "instructions": "使用已授权业务工具回答，保留时间、组织、金额与数量单位。无数据不编造，超限需说明不能视为完整数据。引用工具登记的来源。",
            "connections": [{"connection_id": connection["id"], "allowed_tools": list(TOOLS), "auto_approved_tools": list(TOOLS),
                             "read_only_confirmations": confirmations}],
            "knowledge_base_ids": [], "skill_ids": [], "web_search_enabled": False,
            "default_model_id": metadata["model_id"], "default_reasoning_level": metadata["reasoning_level"]}, expected=201)
        metadata.update(connection_id=connection["id"], assistant_id=assistant["id"])
        fixture.secure_json(directory / "instance.json", metadata)


def serve(args):
    os.umask(0o077)
    directory = private_directory(args.directory, create=True)
    credentials = fixture.load_credentials(directory)
    with (directory / "serve.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sockets, servers = [], []
        try:
            fixture.seed_database(directory / "synthetic.sqlite3")
            fixture.secure_json(directory / "mcp-calls.json", {"synthetic_dataset": True, "calls": []})
            product = create_product(args, directory, credentials)
            sockets.append(fixture.bind_loopback(args.api_port))
            sockets.append(fixture.bind_loopback(args.mcp_port))
            servers = [fixture.LocalServer(create_mcp(directory / "synthetic.sqlite3", credentials["mcp_bearer"]), sockets[1]),
                       fixture.LocalServer(product, sockets[0])]
            for server in servers:
                server.start()
            fixture.secure_json(directory / "instance.json", {"schema": MARKER, "synthetic_dataset": True,
                "runtime_factory_replaced": False, "model_requests_submitted": 0, "model_id": args.model_id,
                "reasoning_level": args.reasoning_level, "api_url": f"http://127.0.0.1:{args.api_port}",
                "mcp_url": f"http://127.0.0.1:{args.mcp_port}/mcp", "pid": os.getpid()})
            bootstrap(directory)
            print(json.dumps({"status": "ready", "synthetic_dataset": True, "real_model_requested": False,
                              "api_port": args.api_port, "mcp_port": args.mcp_port}), flush=True)
            stopped = False
            def stop(_signum, _frame):
                nonlocal stopped
                stopped = True
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            while not stopped:
                time.sleep(0.2)
        finally:
            for server in reversed(servers):
                server.stop()
            for sock in sockets:
                sock.close()


CASES = {
    "orders_10k": ("orders", 10000, "华北", "查询1万条演示订单中，华北区域在2026-11-01至14日的订单总额，给出准确元金额、订单数和来源。"),
    "orders_100k": ("orders", 100000, "华北", "查询10万条演示订单中，华北区域在2026-11-01至14日的订单总额，给出准确元金额、订单数和来源。"),
    "shipments_100k": ("shipments", 100000, "全部", "业务日期是2026-11-14：10万条演示订单中今天向哪些客户发货，各多少件？给出客户、件数和来源。"),
    "cancel": ("orders", 10000, "华北", "执行取消验收：请调用slow_order_summary查询1万条华北演示订单，等待20秒的测试工具，其他工具不要调用。"),
    "oversize": (None, None, None, "执行超限验收：请仅调用一次oversized_order_rows，然后说明结果能否作为完整订单数据使用，不改用其他工具。"),
}


def evaluate(case, trace, calls, expected, answer):
    events = trace.get("events", [])
    tools = [event["data"] for event in events if event["type"] == "tool.completed"]
    results = [tool["business_result"] for tool in tools if tool.get("success") and tool.get("business_result")]
    trusted_sources = {source for tool in tools if tool.get("success") for source in tool.get("source_ids", [])}
    cited_sources = set(re.findall(r"\[\[source:([A-Za-z0-9_-]+)\]\]", answer))
    cited_sources.update(re.findall(r"\[[^\]\n]+\]\(/api/v1/sources/([A-Za-z0-9_-]+)\)", answer))
    real_model = any(event["type"] == "harness.event" and event["data"].get("type") in {"assistant/message", "assistant/attempt"} for event in events)
    status = trace["run"]["status"]
    checks = {"actual_model_event": real_model, "actual_mcp_call": bool(calls)}
    if case == "cancel":
        checks.update(cancelled=status == "cancelled", no_successful_business_result=not results,
                      slow_handler_started=any(call["tool"] == "slow_order_summary" for call in calls))
    elif case == "oversize":
        checks.update(bounded_rejection=any(tool.get("error_code") == "MCP_RESULT_TOO_LARGE" for tool in tools),
                      no_successful_business_result=not results)
    else:
        kind, size, region, _prompt = CASES[case]
        checks.update(succeeded=status == "succeeded", exact_rows=len(results) == 1 and results[0]["rows"] == expected["rows"],
                      correct_parameters=len(calls) == 1 and calls[0].get("tool") == ("order_summary" if kind == "orders" else "shipment_summary")
                          and calls[0].get("dataset_rows") == size and calls[0].get("region") == region,
                      cited_source=bool(cited_sources & trusted_sources) and cited_sources <= trusted_sources,
                      synthetic_marked=bool(results) and results[0].get("synthetic") is True)
        compact_answer = answer.replace(",", "").replace("，", "")
        # Orders ask for one total; shipments already have an exact, complete
        # table above. Do not require the model to duplicate all customer rows.
        if kind == "orders":
            checks["answer_contains_requested_total"] = all(f"{row['amount']:.2f}".rstrip("0").rstrip(".") in compact_answer for row in expected["rows"])
        else:
            checks["answer_has_business_summary"] = bool(answer.strip()) and ("发货" in answer or "客户" in answer)
        from app.business_facts import check_answer
        from app.business_results import project_business_result
        # The expected fixture has an explicit additive contract; also use it
        # to recheck preserved negative receipts made before host validation.
        expected_projection = project_business_result({"structuredContent": expected})
        validation = check_answer(answer, [expected_projection] if expected_projection else [])
        checks["no_deterministic_fact_conflict"] = validation["status"] != "conflict"
    usage_events = [event["data"] for event in events if event["type"] == "usage.updated"]
    usage = usage_events[-1] if usage_events else trace.get("usage") or {}
    return {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "real_model": real_model, "synthetic_dataset": True, "run_status": status,
            "timings": trace["run"].get("timings"), "duration_ms": trace["run"].get("duration_ms"),
            "model_input_bytes": [tool["model_input_bytes"] for tool in tools if tool.get("model_input_bytes") is not None],
            "usage": usage, "usage_missing": not usage_events, "mcp_calls": calls,
            "answer": answer, "cited_source_ids": sorted(cited_sources), "trusted_source_ids": sorted(trusted_sources),
            "answer_semantic_review": "failed" if not checks.get("no_deterministic_fact_conflict", True) else "pending",
            "fact_validation": validation if case not in {"cancel", "oversize"} else None,
            "remote_stop_proven": False if case == "cancel" else None}


def verify(args):
    if not args.real_model:
        raise RuntimeError("--real-model is required to authorize actual provider calls")
    directory = private_directory(args.directory)
    receipt = {"schema": MARKER, "synthetic_dataset": True, "real_model_requested": True,
               "started_at": datetime.now(timezone.utc).isoformat(), "cases": [], "sample_size_warning": "小样本，不能作为p95承诺或节省比例"}
    path = directory / ("acceptance-" + secrets.token_hex(6) + ".json")
    fixture.secure_json(path, receipt)
    with client_for(directory) as (client, metadata):
        for case in args.cases:
            kind, size, region, prompt = CASES[case]
            before = len(private_json(directory / "mcp-calls.json")["calls"])
            conversation = fixture.request(client, "POST", "/api/v1/conversations", {"assistant_id": metadata["assistant_id"],
                "title": "真实模型演示验收 · " + case, "model_id": metadata["model_id"], "reasoning_level": metadata["reasoning_level"]}, expected=201)
            submitted_at = time.monotonic()
            run = fixture.request(client, "POST", f"/api/v1/conversations/{conversation['id']}/messages",
                {"text": prompt, "file_ids": [], "client_request_id": "real-acceptance-" + secrets.token_hex(12)}, expected=202)
            run_id, cancelled = run["run_id"], False
            entry = {"case": case, "status": "running", "run_id": run_id, "conversation_id": conversation["id"],
                     "synthetic_dataset": True, "real_model": None, "usage": None}
            receipt["cases"].append(entry)
            fixture.secure_json(path, receipt)
            deadline = time.monotonic() + args.timeout
            try:
                while True:
                    run = fixture.request(client, "GET", f"/api/v1/runs/{run_id}")
                    if run["status"] in TERMINAL:
                        break
                    calls = private_json(directory / "mcp-calls.json")["calls"][before:]
                    if not cancelled and (time.monotonic() >= deadline or case == "cancel" and any(call["tool"] == "slow_order_summary" for call in calls)):
                        fixture.request(client, "POST", f"/api/v1/runs/{run_id}/cancel")
                        cancelled = True
                        deadline = time.monotonic() + 30
                    elif cancelled and time.monotonic() >= deadline:
                        raise RuntimeError("Run did not terminate after cancellation; receipt retained")
                    time.sleep(0.1)
                trace = fixture.request(client, "GET", f"/api/v1/runs/{run_id}/trace")
                messages = fixture.request(client, "GET", f"/api/v1/conversations/{conversation['id']}/messages")
                answer = "\n".join(message["text"] for message in messages if message.get("run_id") == run_id and message["role"] == "assistant")
                expected = business_result(directory / "synthetic.sqlite3", kind, size, region) if kind else None
                entry.update(evaluate(case, trace, private_json(directory / "mcp-calls.json")["calls"][before:], expected, answer))
                source_ids = {source for event in trace.get("events", []) if event["type"] == "tool.completed"
                              for source in event["data"].get("source_ids", [])}
                entry["readable_source_count"] = sum(fixture.request(client, "GET", f"/api/v1/sources/{source}").get("kind") == "data" for source in source_ids)
                if case not in {"cancel", "oversize"}:
                    entry["checks"]["registered_sources_readable"] = bool(source_ids) and entry["readable_source_count"] == len(source_ids)
                    entry["status"] = "passed" if all(entry["checks"].values()) else "failed"
            except Exception as exc:
                entry.update(status="failed", error_type=type(exc).__name__)
                # Preserve the run even when observing it failed. Do not retry
                # a possibly executed model/tool request or report its cost zero.
                if run.get("status") not in TERMINAL:
                    try:
                        fixture.request(client, "POST", f"/api/v1/runs/{run_id}/cancel")
                        entry["cleanup_cancel_requested"] = True
                    except Exception:
                        entry["cleanup_cancel_requested"] = False
                    entry["cleanup_terminal_verified"] = False
            finally:
                entry["observed_total_ms"] = round((time.monotonic() - submitted_at) * 1000, 2)
                fixture.secure_json(path, receipt)
            if entry.get("cleanup_terminal_verified") is False:
                break
    receipt["automatic_status"] = "passed" if all(case["status"] == "passed" for case in receipt["cases"]) else "failed"
    receipt["status"] = "pending_semantic_review" if receipt["automatic_status"] == "passed" else "failed"
    fixture.secure_json(path, receipt)
    print(json.dumps({"status": receipt["status"], "synthetic_dataset": True, "cases": len(receipt["cases"]), "receipt": str(path)}, ensure_ascii=False))
    return receipt["automatic_status"] == "passed"


def recheck(args):
    """Re-evaluate saved runs after an assertion fix; never resubmit a model task."""
    directory = private_directory(args.directory)
    previous = private_json(args.receipt)
    if previous.get("schema") != MARKER or not previous.get("synthetic_dataset"):
        raise RuntimeError("Receipt is outside this acceptance scope")
    report = {"schema": MARKER, "synthetic_dataset": True, "source_receipt": args.receipt.name,
              "new_model_requests_submitted": 0, "rechecked_at": datetime.now(timezone.utc).isoformat(), "cases": []}
    with client_for(directory) as (client, _metadata):
        for old in previous["cases"]:
            case, run_id = old["case"], old["run_id"]
            kind, size, region, _prompt = CASES[case]
            trace = fixture.request(client, "GET", f"/api/v1/runs/{run_id}/trace")
            expected = business_result(directory / "synthetic.sqlite3", kind, size, region) if kind else None
            result = evaluate(case, trace, old.get("mcp_calls", []), expected, old.get("answer", ""))
            result.update(case=case, run_id=run_id, conversation_id=old["conversation_id"], observed_total_ms=old.get("observed_total_ms"))
            report["cases"].append(result)
    report["automatic_status"] = "passed" if all(case["status"] == "passed" for case in report["cases"]) else "failed"
    report["status"] = "pending_semantic_review" if report["automatic_status"] == "passed" else "failed"
    path = directory / ("recheck-" + secrets.token_hex(6) + ".json")
    fixture.secure_json(path, report)
    print(json.dumps({"status": report["status"], "new_model_requests_submitted": 0, "receipt": str(path)}), flush=True)
    return report["automatic_status"] == "passed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "serve", "verify", "recheck"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--api-port", type=int, default=18085)
    parser.add_argument("--mcp-port", type=int, default=18878)
    parser.add_argument("--model-id")
    parser.add_argument("--reasoning-level")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--cases", nargs="+", choices=tuple(CASES), default=list(CASES))
    args = parser.parse_args()
    if args.command in ("prepare", "serve") and (not args.model_id or not args.reasoning_level):
        parser.error("prepare/serve requires explicitly selected --model-id and --reasoning-level")
    if args.command == "recheck" and not args.receipt:
        parser.error("recheck requires --receipt; the original receipt will remain unchanged")
    if not 1 <= args.timeout <= 600 or len(args.cases) > 30:
        parser.error("timeout must be 1..600 seconds and at most 30 requests are allowed")
    try:
        if args.command == "prepare":
            prepare(args)
        elif args.command == "serve":
            serve(args)
        elif args.command == "recheck":
            if not recheck(args):
                raise SystemExit(1)
        elif not verify(args):
            raise SystemExit(1)
    except Exception as exc:
        print(f"Acceptance failed ({type(exc).__name__}); private details withheld", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
