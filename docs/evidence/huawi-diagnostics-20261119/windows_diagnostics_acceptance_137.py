#!/usr/bin/env python3
"""Supervisor acceptance for Kaiwu Connector 1.3.7 on Shervin Windows.

Standard library only. This script never changes services, source configuration,
credentials, the inventory fixture, FRP state, or diagnostics upload settings.
Public calls use the existing local private integration JSON. No credential,
response body/header dump, raw exception, or traceback is printed or persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta

BASE_DEFAULT = r"D:\KaiwuAcceptance\public-e2e-20261119"
LOCAL = "http://127.0.0.1:17872"
MOCK_HEALTH = "http://127.0.0.1:28765/health"
EXPECTED_LOCAL_MCP = "http://127.0.0.1:28765/mcp"
SKU = "SKU-1001"
INVALID_SKU = "KAIWU_DIAGNOSTICS_ACCEPTANCE_INVALID_SKU_CANARY"
MAX_RESPONSE = 2 * 1024 * 1024
TRACE_RE = re.compile(r"^kwtr_[0-9a-f]{24,64}$")
SOURCE_RE = re.compile(r"^src_[0-9a-f]{32}$")
CONNECTOR_RE = re.compile(r"^conn_[0-9a-f]{32}$")
EVENT_RE = re.compile(r"^evt_[0-9a-f]{32}$")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
EVENT_KEYS = {
    "schema_version", "event_id", "occurred_at", "level", "component",
    "event_code", "message_code", "operation_id", "trace_id", "source_id",
    "tool_name", "http_status", "rpc_error_code", "duration_ms", "attempt",
    "message", "origin",
}
SAFE_COMPONENTS = {
    "connector", "config", "source", "auth", "diagnosis", "tunnel",
    "mcp", "platform", "diagnostics",
}
CASES = ("precheck", "postcheck", "public-success", "business-error", "verify-logs")
BEIJING = timezone(timedelta(hours=8))


class AcceptanceFailure(Exception):
    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


def require(condition: bool, category: str) -> None:
    if not condition:
        raise AcceptanceFailure(category)


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise AcceptanceFailure("INPUT_FILE_MISSING") from None
    except (ValueError, UnicodeError):
        raise AcceptanceFailure("INPUT_JSON_INVALID") from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AcceptanceFailure("REDIRECT_BLOCKED")


def opener(cookie_jar=None):
    handlers = [
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        NoRedirect(),
    ]
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    return urllib.request.build_opener(*handlers)


def fetch(client, url, *, method="GET", headers=None, body=None, timeout=10.0):
    encoded = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    h = dict(headers or {})
    if encoded is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=encoded, headers=h, method=method)
    try:
        response = client.open(req, timeout=max(0.05, timeout))
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read(MAX_RESPONSE + 1)
        require(len(raw) <= MAX_RESPONSE, "RESPONSE_TOO_LARGE")
        # These values remain in memory. Only a validated non-secret trace id
        # or a bounded integer status is ever copied into a receipt.
        meta = {
            "content_type": response.headers.get("Content-Type", ""),
            "session_id": response.headers.get("Mcp-Session-Id", ""),
            "trace_id": response.headers.get("X-Kaiwu-Trace-ID", ""),
        }
        return response.status, meta, raw


def decode_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise AcceptanceFailure("RESPONSE_JSON_INVALID") from None


def decode_rpc(raw: bytes, content_type: str, request_id: int):
    if "text/event-stream" not in content_type.lower():
        value = decode_json(raw)
        require(isinstance(value, dict) and value.get("id") == request_id, "RPC_RESPONSE_ID_MISMATCH")
        return value
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeError:
        raise AcceptanceFailure("RPC_SSE_INVALID") from None
    for event in text.split("\n\n"):
        parts = [line[5:].lstrip(" ") for line in event.split("\n") if line.startswith("data:")]
        if not parts:
            continue
        try:
            value = json.loads("\n".join(parts))
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("id") == request_id:
            return value
    raise AcceptanceFailure("RPC_RESPONSE_MISSING")


class LocalClient:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.client = opener(self.jar)
        status, _, _ = fetch(self.client, LOCAL + "/", timeout=5.0)
        require(status == 200, "LOCAL_PAGE_UNAVAILABLE")
        tokens = [c.value for c in self.jar if c.name == "kconn" and c.domain == "127.0.0.1"]
        require(len(tokens) == 1, "LOCAL_SESSION_COOKIE_MISSING")
        self.token = tokens[0]

    def get(self, path: str, timeout=5.0):
        status, _, raw = fetch(
            self.client, LOCAL + path,
            headers={"X-Connector-Token": self.token, "Origin": LOCAL, "Accept": "application/json"},
            timeout=timeout,
        )
        require(status == 200, "LOCAL_API_HTTP_ERROR")
        value = decode_json(raw)
        require(isinstance(value, dict) and value.get("ok") is True, "LOCAL_API_REJECTED")
        require(isinstance(value.get("data"), dict), "LOCAL_API_SHAPE_INVALID")
        return value["data"]


def load_fixture(base: Path):
    raw = (base / "data" / "inventory.json").read_bytes()
    document = json.loads(raw.decode("utf-8-sig"))
    candidates = document
    if isinstance(document, dict):
        if "items" in document:
            candidates = document["items"]
        elif "skus" in document:
            candidates = document["skus"]
    item = candidates.get(SKU) if isinstance(candidates, dict) else next(
        (x for x in candidates if isinstance(x, dict) and x.get("sku") == SKU), None
    ) if isinstance(candidates, list) else None
    require(isinstance(item, dict), "INVENTORY_FIXTURE_INVALID")
    require(isinstance(item.get("quantity"), (int, float)) and not isinstance(item.get("quantity"), bool),
            "INVENTORY_QUANTITY_INVALID")
    require(isinstance(item.get("revision"), str) and bool(item["revision"]), "INVENTORY_REVISION_INVALID")
    require(isinstance(item.get("note"), str), "INVENTORY_NOTE_INVALID")
    return item, hashlib.sha256(raw).hexdigest()


def load_public_config(base: Path):
    value = read_json(base / "handoff-private" / "kaiwu-shervin-mcp.private.json")
    servers = value.get("mcpServers") if isinstance(value, dict) else None
    require(isinstance(servers, dict) and len(servers) == 1, "PRIVATE_MCP_CONFIG_SHAPE_INVALID")
    server = next(iter(servers.values()))
    require(isinstance(server, dict), "PRIVATE_MCP_CONFIG_SHAPE_INVALID")
    endpoint = server.get("url")
    require(isinstance(endpoint, str), "PUBLIC_ENDPOINT_INVALID")
    parsed = urllib.parse.urlsplit(endpoint)
    match = re.fullmatch(r"/projects/kaiwu/mcp/sources/(src_[0-9a-f]{32})/mcp", parsed.path)
    require(parsed.scheme == "https" and parsed.hostname == "flintw.com" and parsed.port in (None, 443)
            and parsed.username is None and not parsed.query and not parsed.fragment and bool(match),
            "PUBLIC_ENDPOINT_OUTSIDE_ACCEPTANCE_SCOPE")
    headers = server.get("headers")
    require(isinstance(headers, dict) and all(isinstance(k, str) and isinstance(v, str)
            for k, v in headers.items()), "PRIVATE_HEADERS_INVALID")
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    require(auth.startswith("Bearer ") and len(auth) > 16, "INTEGRATION_CREDENTIAL_MISSING")
    return endpoint, headers, match.group(1)


def canaries(base: Path, headers: dict, fixture: dict, local_token: str):
    secret_values = [local_token]
    for value in headers.values():
        if len(value) >= 8:
            secret_values.append(value)
            if value.lower().startswith("bearer "):
                secret_values.append(value[7:])
    # Only the existing Shervin synthetic MCP credentials are read here.
    # They never leave this Python process or appear in any receipt.
    mock_private = read_json(base / "data" / "mock_mcp_config.private.json")
    require(isinstance(mock_private, dict), "MOCK_PRIVATE_CONFIG_INVALID")
    for key in ("bearer_token", "hmac_key"):
        value = mock_private.get(key)
        require(isinstance(value, str) and len(value) >= 8, "MOCK_PRIVATE_CONFIG_INVALID")
        secret_values.append(value)
    business_values = [INVALID_SKU, fixture["revision"], fixture["note"], EXPECTED_LOCAL_MCP]
    return list(set(secret_values)), list(set(x for x in business_values if isinstance(x, str) and len(x) >= 8))


def privacy_metrics(local_data: dict, platform_data: dict, secret_values: list, business_values: list):
    encoded = json.dumps([local_data, platform_data], ensure_ascii=False)
    rows = list(local_data.get("events") or []) + list(platform_data.get("events") or [])
    bad_fields = 0
    bad_shape = 0
    for row in rows:
        if not isinstance(row, dict):
            bad_shape += 1
            continue
        bad_fields += len(set(row) - EVENT_KEYS)
        if (not EVENT_RE.fullmatch(str(row.get("event_id", "")))
                or row.get("schema_version") != 1
                or row.get("level") not in ("info", "warn", "error")
                or row.get("component") not in SAFE_COMPONENTS
                or row.get("event_code") != row.get("message_code")):
            bad_shape += 1
    metrics = {
        "scanned_event_count": len(rows),
        "secret_canary_count": len(secret_values),
        "business_canary_count": len(business_values),
        "secret_canary_hit_count": sum(1 for v in secret_values if v in encoded),
        "business_canary_hit_count": sum(1 for v in business_values if v in encoded),
        "unexpected_field_count": bad_fields,
        "invalid_event_shape_count": bad_shape,
    }
    return metrics


def verify_trace(local: LocalClient, base: Path, headers: dict, fixture: dict,
                 source_id: str, trace_id: str, expect_error: bool, started_at=None):
    require(bool(TRACE_RE.fullmatch(trace_id)), "TRACE_ID_INVALID")
    secret_values, business_values = canaries(base, headers, fixture, local.token)
    started = time.monotonic() if started_at is None else started_at
    deadline = started + 5.0
    polls = 0
    local_matches = []
    platform_matches = []
    latest_metrics = {}
    pause = threading.Event()
    while time.monotonic() < deadline:
        polls += 1
        try:
            remaining = max(0.05, deadline - time.monotonic())
            local_data = local.get("/api/logs?limit=1000&trace_id=" + trace_id,
                                   timeout=min(1.0, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            platform_data = local.get("/api/platform-logs?limit=500",
                                      timeout=min(2.0, remaining))
        except (urllib.error.URLError, TimeoutError, OSError):
            pause.wait(min(0.15, max(0.0, deadline - time.monotonic())))
            continue
        # An empty Go slice can be serialized as null while the first event is pending.
        for data, category in ((local_data, "LOCAL_LOGS_SHAPE_INVALID"),
                               (platform_data, "PLATFORM_LOGS_SHAPE_INVALID")):
            if data.get("events") is None:
                data["events"] = []
            require(isinstance(data.get("events"), list), category)
        latest_metrics = privacy_metrics(local_data, platform_data, secret_values, business_values)
        require(latest_metrics["secret_canary_hit_count"] == 0, "SECRET_CANARY_IN_DIAGNOSTICS")
        require(latest_metrics["business_canary_hit_count"] == 0, "BUSINESS_CANARY_IN_DIAGNOSTICS")
        require(latest_metrics["unexpected_field_count"] == 0
                and latest_metrics["invalid_event_shape_count"] == 0, "DIAGNOSTIC_EVENT_CONTRACT_INVALID")
        expected_local = "mcp_request_failed" if expect_error else "mcp_request_succeeded"
        expected_platform = "platform_request_failed" if expect_error else "platform_request_succeeded"
        local_matches = [x for x in local_data["events"]
                         if x.get("trace_id") == trace_id and x.get("source_id") == source_id
                         and x.get("event_code") == expected_local]
        platform_matches = [x for x in platform_data["events"]
                            if x.get("trace_id") == trace_id and x.get("source_id") == source_id]
        if platform_data.get("available") is True and local_matches and platform_matches:
            require(time.monotonic() <= deadline, "CORRELATED_LOGS_NOT_VISIBLE_WITHIN_5_SECONDS")
            require(len(platform_matches) == 1, "DUPLICATE_PLATFORM_OUTCOME")
            require(platform_matches[0].get("event_code") == expected_platform, "PLATFORM_OUTCOME_MISMATCH")
            require(platform_matches[0].get("http_status") == 200, "PLATFORM_HTTP_STATUS_MISMATCH")
            if expect_error:
                require(str(platform_matches[0].get("rpc_error_code")) == "-32602",
                        "PLATFORM_RPC_ERROR_CODE_MISMATCH")
            return {
                **latest_metrics, "poll_count": polls, "connector_trace_event_count": len(local_matches),
                "platform_trace_event_count": len(platform_matches),
                "verification_elapsed_ms": int((time.monotonic() - started) * 1000),
                "same_trace_verified": True,
            }
        pause.wait(min(0.15, max(0.0, deadline - time.monotonic())))
    raise AcceptanceFailure("CORRELATED_LOGS_NOT_VISIBLE_WITHIN_5_SECONDS")


def run_pre_or_post(args, report):
    base = Path(args.base_dir)
    local = LocalClient()
    status = local.get("/api/status")
    _, headers, source_id = load_public_config(base)
    fixture, fixture_hash = load_fixture(base)
    require(status.get("bound") is True, "CONNECTOR_NOT_BOUND")
    require(status.get("state") == "READY" and status.get("frpc_running") is True
            and status.get("tunnel_connected") is True, "CONNECTOR_NOT_READY")
    require(CONNECTOR_RE.fullmatch(str(status.get("connector_id", ""))) is not None, "CONNECTOR_ID_INVALID")
    sources = status.get("sources")
    require(isinstance(sources, list), "SOURCE_STATUS_INVALID")
    selected = [s for s in sources if isinstance(s, dict) and s.get("source_id") == source_id]
    require(len(selected) == 1 and not selected[0].get("disabled", False), "TARGET_SOURCE_NOT_ACTIVE")
    require(selected[0].get("upstream_url") == EXPECTED_LOCAL_MCP, "TARGET_SOURCE_NOT_LOCAL_FIXTURE")
    enabled_ids = sorted(s["source_id"] for s in sources
                         if isinstance(s, dict) and not s.get("disabled", False)
                         and SOURCE_RE.fullmatch(str(s.get("source_id", ""))))
    version = VERSION_RE.search(str(status.get("version", "")))
    require(version is not None, "CONNECTOR_VERSION_INVALID")
    expected = args.expected_version or ("1.3.7" if args.command == "postcheck" else None)
    if expected is not None:
        require(version.group(0) == expected, "CONNECTOR_VERSION_MISMATCH")
    health_status, _, _ = fetch(opener(), MOCK_HEALTH, timeout=2.0)
    require(health_status == 200, "LOCAL_MCP_HEALTH_FAILED")
    report.update({
        "observed_version": version.group(0), "state": "READY",
        "connector_id": status["connector_id"], "source_id": source_id,
        "enabled_source_ids": enabled_ids, "enabled_source_count": len(enabled_ids),
        "inventory_sha256": fixture_hash, "local_mcp_health_status": health_status,
    })
    if args.command == "postcheck":
        baseline = read_json(base / "evidence" / "diag137-precheck-latest.json")
        require(baseline.get("passed") is True, "PRECHECK_BASELINE_NOT_PASSED")
        for key in ("connector_id", "source_id", "enabled_source_ids", "inventory_sha256"):
            require(report[key] == baseline.get(key), "PRESERVED_STATE_MISMATCH")
        local_data = local.get("/api/logs?limit=200")
        platform_data = local.get("/api/platform-logs?limit=200")
        require(platform_data.get("available") is True, "PLATFORM_LOGS_UNAVAILABLE")
        stats = local_data.get("stats") or {}
        require(stats.get("paused") is False, "DIAGNOSTICS_UPLOAD_PAUSED")
        require(not stats.get("storage_error_code"), "DIAGNOSTICS_STORAGE_UNHEALTHY")
        require(not stats.get("last_error_code"), "DIAGNOSTICS_UPLOAD_UNHEALTHY")
        secret_values, business_values = canaries(base, headers, fixture, local.token)
        metrics = privacy_metrics(local_data, platform_data, secret_values, business_values)
        report["privacy"] = metrics
        require(not any(metrics[k] for k in (
            "secret_canary_hit_count", "business_canary_hit_count",
            "unexpected_field_count", "invalid_event_shape_count")), "DIAGNOSTICS_PRIVACY_CHECK_FAILED")
        report["preserved_state_verified"] = True


def run_public(args, report):
    base = Path(args.base_dir)
    endpoint, configured, source_id = load_public_config(base)
    fixture, fixture_hash = load_fixture(base)
    # Establish the local session before the measured public call; its setup must
    # not extend the five-second correlation acceptance window.
    local = LocalClient() if args.verify_logs else None
    client = opener()
    headers = dict(configured)
    headers["Accept"] = "application/json, text/event-stream"
    status, meta, raw = fetch(client, endpoint, method="POST", headers=headers, body={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "kaiwu-diagnostics-acceptance", "version": "1.3.7"}},
    })
    require(status == 200, "MCP_INITIALIZE_HTTP_FAILED")
    initialized = decode_rpc(raw, meta["content_type"], 1)
    require("error" not in initialized and isinstance(initialized.get("result"), dict), "MCP_INITIALIZE_FAILED")
    protocol = initialized["result"].get("protocolVersion")
    require(isinstance(protocol, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", protocol)),
            "MCP_PROTOCOL_VERSION_INVALID")
    headers["MCP-Protocol-Version"] = protocol
    if meta["session_id"]:
        headers["Mcp-Session-Id"] = meta["session_id"]
    notify_status, _, _ = fetch(client, endpoint, method="POST", headers=headers, body={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    require(notify_status in (200, 202, 204), "MCP_INITIALIZED_NOTIFICATION_FAILED")
    expect_error = args.command == "business-error"
    status, meta, raw = fetch(client, endpoint, method="POST", headers=headers, body={
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "get_inventory", "arguments": {"sku": INVALID_SKU if expect_error else SKU}},
    })
    tool_completed_monotonic = time.monotonic()
    report["http_status"] = status
    require(status == 200, "PUBLIC_TOOL_HTTP_STATUS_MISMATCH")
    require(bool(TRACE_RE.fullmatch(meta["trace_id"])), "PUBLIC_TRACE_ID_MISSING")
    report.update({"trace_id": meta["trace_id"], "source_id": source_id,
                   "tool_completed_at_utc": stamp(), "inventory_sha256": fixture_hash})
    result = decode_rpc(raw, meta["content_type"], 2)
    if expect_error:
        require(isinstance(result.get("error"), dict) and result["error"].get("code") == -32602,
                "EXPECTED_BUSINESS_ERROR_NOT_RETURNED")
        report.update({"business_error_verified": True, "rpc_error_code": -32602})
    else:
        require("error" not in result and isinstance(result.get("result"), dict), "PUBLIC_TOOL_RPC_ERROR")
        tool_result = result["result"]
        require(tool_result.get("isError") is not True, "PUBLIC_TOOL_RESULT_ERROR")
        actual = tool_result.get("structuredContent")
        require(isinstance(actual, dict), "PUBLIC_STRUCTURED_CONTENT_MISSING")
        require(actual.get("sku") == SKU and all(actual.get(k) == fixture[k]
                for k in ("quantity", "note", "revision")), "PUBLIC_LOCAL_DATA_MISMATCH")
        report.update({"sku_matched": True, "quantity_matched": True,
                       "note_matched": True, "revision_matched": True})
    report["rpc_assertions_passed"] = True
    if args.verify_logs:
        report["logs"] = verify_trace(local, base, configured, fixture, source_id,
                                      meta["trace_id"], expect_error, tool_completed_monotonic)


def run_verify(args, report):
    base = Path(args.base_dir)
    saved = read_json(base / "evidence" / ("diag137-" + args.case + "-latest.json"))
    require(saved.get("rpc_assertions_passed") is True, "PUBLIC_CASE_RECEIPT_NOT_VERIFIED")
    _, headers, source_id = load_public_config(base)
    fixture, _ = load_fixture(base)
    require(saved.get("source_id") == source_id, "PUBLIC_CASE_SOURCE_CHANGED")
    trace_id = saved.get("trace_id", "")
    require(isinstance(trace_id, str) and bool(TRACE_RE.fullmatch(trace_id)), "TRACE_ID_INVALID")
    report.update({"trace_id": trace_id, "source_id": source_id, "verified_case": args.case})
    report["logs"] = verify_trace(LocalClient(), base, headers, fixture, source_id,
                                  trace_id, args.case == "business-error")


def persist_receipt(base: Path, report: dict):
    directory = base / "evidence"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    name = "diag137-" + report["case"] + "-" + suffix + ".json"
    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    (directory / name).write_text(raw, encoding="utf-8")
    latest = directory / ("diag137-" + report["case"] + "-latest.json")
    temporary = latest.with_suffix(".tmp")
    temporary.write_text(raw, encoding="utf-8")
    os.replace(temporary, latest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows-only Kaiwu 1.3.7 diagnostics acceptance")
    parser.add_argument("command", choices=CASES)
    parser.add_argument("--base-dir", default=BASE_DEFAULT)
    parser.add_argument("--expected-version", default=None)
    parser.add_argument("--verify-logs", action="store_true",
                        help="immediately verify both sides of the public call trace within five seconds")
    parser.add_argument("--case", choices=("public-success", "business-error"), default="public-success",
                        help="public case receipt to inspect with verify-logs")
    args = parser.parse_args()
    report = {"case": args.command, "started_at_utc": stamp(),
              "started_at_beijing": datetime.now(BEIJING).isoformat(), "passed": False, "exit_code": 1}
    started = time.monotonic()
    try:
        require(os.name == "nt", "WINDOWS_REQUIRED")
        if args.expected_version is not None:
            require(bool(re.fullmatch(r"\d+\.\d+\.\d+", args.expected_version)), "EXPECTED_VERSION_INVALID")
        if args.command in ("precheck", "postcheck"):
            run_pre_or_post(args, report)
        elif args.command in ("public-success", "business-error"):
            run_public(args, report)
        else:
            run_verify(args, report)
        report["passed"] = True
        report["exit_code"] = 0
    except AcceptanceFailure as exc:
        report["failure_category"] = exc.category
        report["exception_type"] = "AcceptanceFailure"
    except KeyboardInterrupt:
        report["failure_category"] = "INTERRUPTED"
        report["exception_type"] = "KeyboardInterrupt"
        report["exit_code"] = 130
    except BaseException as exc:
        # Deliberately no str(exc), repr(exc), response content, headers or traceback.
        report["failure_category"] = "UNEXPECTED_EXECUTION_FAILURE"
        report["exception_type"] = type(exc).__name__
    report["finished_at_utc"] = stamp()
    report["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    if os.name == "nt":
        try:
            persist_receipt(Path(args.base_dir), report)
        except BaseException as exc:
            report["passed"] = False
            report["exit_code"] = 1
            report["failure_category"] = "EVIDENCE_WRITE_FAILED"
            report["exception_type"] = type(exc).__name__
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
