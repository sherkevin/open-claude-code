#!/usr/bin/env python3
"""Kaiwu 1.3.8 bundled-demo acceptance.

Engineering-only. Reuses the reviewed 1.3.7 HTTP/CSRF/MCP parser helper.
Never installs, starts/stops services, binds a device, issues credentials,
edits sources, pauses tunnels or changes demo settings.
The opt-in self-test case calls the product's self-test API; that API may
register its own demo source and connect the tunnel. Run it only in the
supervisor-approved acceptance scope.
No raw responses, credentials, business metadata or exceptions are recorded.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import socket
import sys
import time
import urllib.parse
import uuid

DEMO_NAME = "开物内置测试服务（示例数据）"
MARKER = "KAIWU_BUILTIN_DEMO_V1"
TOOLS = {"kaiwu_demo_ping", "kaiwu_demo_order_summary", "kaiwu_demo_inventory_query"}
SOURCE_RE = re.compile(r"src_[0-9a-f]{32}")
CONNECTOR_RE = re.compile(r"conn_[0-9a-f]{32}")
TRACE_RE = re.compile(r"kwtr_[0-9a-f]{24,64}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
MAX_RESPONSE = 2 * 1024 * 1024
H = None


class Failure(Exception):
    pass


def need(condition, code):
    if not condition:
        raise Failure(code)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf8")).hexdigest()


def expected_demo_id(connector_id):
    return "src_" + hashlib.sha256(
        ("kaiwu_builtin_demo_v1:" + connector_id).encode("utf8")
    ).hexdigest()[:32]


def load_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def loopback_url(value, path=None):
    p = urllib.parse.urlsplit(value)
    need(p.scheme == "http" and p.hostname == "127.0.0.1"
         and p.username is None and p.password is None
         and not p.query and not p.fragment and p.port is not None,
         "LOOPBACK_URL_REQUIRED")
    if path is not None:
        need(p.path == path, "LOOPBACK_PATH_INVALID")
    return p


def load_helper(path, local_url):
    global H
    path = Path(path).resolve()
    need(path.is_file() and path.name == "windows_diagnostics_acceptance_137.py",
         "REVIEWED_HTTP_HELPER_MISSING")
    spec = importlib.util.spec_from_file_location("kaiwu_reviewed_http137", path)
    H = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(H)
    H.LOCAL = local_url
    H.MAX_RESPONSE = MAX_RESPONSE
    return hashlib.sha256(path.read_bytes()).hexdigest()


def status_for(local, args):
    status = local.get("/api/status")
    version = re.search(r"\d+\.\d+\.\d+", str(status.get("version", "")))
    need(version is not None and version.group(0) == args.expected_version,
         "CONNECTOR_VERSION_MISMATCH")
    if args.expected_commit:
        need(args.expected_commit in str(status.get("version", "")),
             "CONNECTOR_BUILD_COMMIT_MISMATCH")
    if args.expect_unbound:
        need(status.get("bound") is False, "FRESH_UNBOUND_CONNECTOR_REQUIRED")
    need(isinstance(status.get("sources"), list), "SOURCE_STATUS_INVALID")
    return status


def source_projection(status):
    demo_id = (status.get("builtin_demo") or {}).get("source_id", "")
    source_hashes = {}
    for source in status["sources"]:
        need(isinstance(source, dict), "SOURCE_STATUS_INVALID")
        sid = source.get("source_id", "")
        need(SOURCE_RE.fullmatch(sid) is not None, "SOURCE_ID_INVALID")
        if sid != demo_id:
            need(sid not in source_hashes, "DUPLICATE_SOURCE_ID")
            source_hashes[sid] = digest(source)
    profiles = [p for p in (status.get("auth_profiles") or [])
                if isinstance(p, dict) and p.get("source_id") != demo_id]
    profiles.sort(key=lambda p: p.get("source_id", ""))
    return {
        "connector_id": status.get("connector_id", ""),
        "bound": status.get("bound") is True,
        "business_source_hashes": source_hashes,
        "business_source_count": len(source_hashes),
        "business_auth_profiles_sha256": digest(profiles),
        "builtin_demo_source_id": demo_id,
    }


def demo_source(status):
    demo = status.get("builtin_demo")
    need(isinstance(demo, dict), "BUNDLED_DEMO_STATUS_MISSING")
    need(demo.get("enabled") is True and demo.get("running") is True
         and not demo.get("error_code"), "BUNDLED_DEMO_NOT_RUNNING")
    sid = demo.get("source_id", "")
    need(SOURCE_RE.fullmatch(sid) is not None, "DEMO_SOURCE_ID_INVALID")
    if status.get("bound"):
        connector_id = status.get("connector_id", "")
        need(CONNECTOR_RE.fullmatch(connector_id) is not None, "CONNECTOR_ID_INVALID")
        need(sid == expected_demo_id(connector_id), "DEMO_SOURCE_ID_FORMULA_MISMATCH")
    rows = [s for s in status["sources"] if s.get("source_id") == sid]
    need(len(rows) == 1, "DEMO_SOURCE_NOT_UNIQUE")
    source = rows[0]
    need(source.get("display_name") == DEMO_NAME
         and not source.get("disabled", False), "DEMO_SOURCE_METADATA_INVALID")
    need(set(source.get("tool_allowlist", [])) == TOOLS,
         "DEMO_SOURCE_TOOL_ALLOWLIST_INVALID")
    loopback_url(source.get("upstream_url", ""), "/mcp")
    return source


def rpc_open(endpoint, configured_headers):
    client = H.opener()
    headers = dict(configured_headers)
    headers["Accept"] = "application/json, text/event-stream"
    status, meta, raw = H.fetch(client, endpoint, method="POST", headers=headers, body={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "kaiwu-builtin-demo-acceptance", "version": "1.3.8"}},
    })
    need(status == 200, "MCP_INITIALIZE_HTTP_FAILED")
    init = H.decode_rpc(raw, meta["content_type"], 1)
    need("error" not in init and isinstance(init.get("result"), dict),
         "MCP_INITIALIZE_FAILED")
    version = init["result"].get("protocolVersion", "")
    need(re.fullmatch(r"\d{4}-\d{2}-\d{2}", version) is not None,
         "MCP_NEGOTIATED_VERSION_INVALID")
    headers["MCP-Protocol-Version"] = version
    if meta["session_id"]:
        headers["Mcp-Session-Id"] = meta["session_id"]
    status, _, _ = H.fetch(client, endpoint, method="POST", headers=headers, body={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    need(status in (200, 202, 204), "MCP_INITIALIZED_NOTIFICATION_FAILED")
    return client, headers


def rpc(client, endpoint, headers, request_id, method, params=None):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    status, meta, raw = H.fetch(client, endpoint, method="POST", headers=headers,
                              body=body, timeout=20)
    need(status == 200, "MCP_REQUEST_HTTP_FAILED")
    response = H.decode_rpc(raw, meta["content_type"], request_id)
    need("error" not in response and isinstance(response.get("result"), dict),
         "MCP_REQUEST_RPC_FAILED")
    result = response["result"]
    need(result.get("isError") is not True, "MCP_TOOL_RETURNED_ERROR")
    return result, meta


def structured(result):
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        texts = [c.get("text") for c in result.get("content", [])
                 if isinstance(c, dict) and c.get("type") == "text"
                 and isinstance(c.get("text"), str)]
        need(len(texts) == 1, "MCP_STRUCTURED_CONTENT_MISSING")
        value = json.loads(texts[0])
    need(isinstance(value, dict), "MCP_STRUCTURED_CONTENT_INVALID")
    need(value.get("demo") is True and value.get("marker") == MARKER,
         "BUNDLED_DEMO_MARKER_MISMATCH")
    return value


def check_ping(value, nonce):
    need(value.get("demo") is True and value.get("marker") == MARKER
         and value.get("nonce") == nonce, "FRESH_NONCE_MISMATCH")
    instance_id = value.get("instance_id", "")
    need(isinstance(instance_id, str) and SAFE_ID_RE.fullmatch(instance_id) is not None,
         "DEMO_INSTANCE_ID_INVALID")
    now = datetime.now(timezone.utc)
    observed = datetime.fromisoformat(value.get("server_time_utc", "").replace("Z", "+00:00"))
    need(observed.tzinfo is not None and abs((now - observed).total_seconds()) <= 120,
         "FRESH_SERVER_TIME_INVALID")
    return {"nonce": nonce, "instance_id": instance_id,
            "server_time_utc": observed.isoformat()}


def run_rpc_probe(endpoint, configured_headers, public):
    client, headers = rpc_open(endpoint, configured_headers)
    listing, _ = rpc(client, endpoint, headers, 2, "tools/list")
    rows = listing.get("tools")
    need(isinstance(rows, list) and len(rows) == 3
         and {x.get("name") for x in rows if isinstance(x, dict)} == TOOLS,
         "MCP_FIXED_TOOL_LIST_MISMATCH")
    nonce = uuid.uuid4().hex
    checks, traces = {}, {}
    calls = [
        ("kaiwu_demo_ping", {"nonce": nonce}),
        ("kaiwu_demo_order_summary", {}),
        ("kaiwu_demo_inventory_query", {}),
    ]
    for index, (name, arguments) in enumerate(calls, 3):
        result, meta = rpc(client, endpoint, headers, index, "tools/call",
                           {"name": name, "arguments": arguments})
        value = structured(result)
        if name == "kaiwu_demo_ping":
            checks["fresh_probe"] = check_ping(value, nonce)
        elif name == "kaiwu_demo_order_summary":
            need(value.get("order_count") == 12 and "示例" in str(value.get("notice", "")),
                 "DEMO_ORDER_ASSERTION_FAILED")
            checks["order_count_12_verified"] = True
        else:
            need(value.get("product_code") == "KAIWU-DEMO-001"
                 and value.get("stock") == 128 and "示例" in str(value.get("notice", "")),
                 "DEMO_INVENTORY_ASSERTION_FAILED")
            checks["inventory_128_verified"] = True
        if public:
            trace = meta.get("trace_id", "")
            need(TRACE_RE.fullmatch(trace) is not None, "PUBLIC_TRACE_ID_MISSING")
            traces[name] = trace
    return {"tool_names_verified": sorted(TOOLS), **checks, "traces": traces}


def public_config(path, sid):
    obj = load_json(path)
    servers = obj.get("mcpServers") if isinstance(obj, dict) else None
    need(isinstance(servers, dict) and len(servers) == 1, "PRIVATE_MCP_CONFIG_INVALID")
    server = next(iter(servers.values()))
    need(isinstance(server, dict), "PRIVATE_MCP_CONFIG_INVALID")
    endpoint = server.get("url", "")
    p = urllib.parse.urlsplit(endpoint)
    need(p.scheme == "https" and p.hostname == "flintw.com"
         and p.port in (None, 443) and p.username is None and p.password is None
         and not p.query and not p.fragment
         and p.path == "/projects/kaiwu/mcp/sources/" + sid + "/mcp",
         "PUBLIC_ENDPOINT_OUTSIDE_BUILTIN_DEMO_SCOPE")
    headers = server.get("headers")
    need(isinstance(headers, dict) and all(isinstance(k, str) and isinstance(v, str)
                                         for k, v in headers.items()),
         "PRIVATE_HEADERS_INVALID")
    token = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
    need(token.startswith("Bearer ") and len(token) > 16, "TEST_INTEGRATION_CREDENTIAL_MISSING")
    return endpoint, headers


def inspect_bad_source(status, sid):
    rows = [s for s in status["sources"] if s.get("source_id") == sid]
    need(len(rows) == 1 and not rows[0].get("disabled", False),
         "ENABLED_BAD_TEST_SOURCE_REQUIRED")
    p = loopback_url(rows[0].get("upstream_url", ""), "/mcp")
    try:
        sock = socket.create_connection(("127.0.0.1", p.port), timeout=1)
    except OSError:
        return True
    sock.close()
    raise Failure("BAD_TEST_SOURCE_IS_REACHABLE")


def self_test(local, args, before):
    need(args.allow_self_test, "SELF_TEST_OPT_IN_REQUIRED")
    if args.bad_source_id:
        inspect_bad_source(before, args.bad_source_id)
    http_status, _, raw = H.fetch(
        local.client, args.local_url + "/api/self-test", method="POST",
        headers={"X-Connector-Token": local.token, "Origin": args.local_url,
                 "Accept": "application/json"}, body={}, timeout=120)
    document = H.decode_json(raw)
    need(isinstance(document, dict) and isinstance(document.get("data"), dict),
         "SELF_TEST_RESPONSE_CONTRACT_NOT_IMPLEMENTED")
    data = document["data"]
    stages = data.get("stages")
    need(isinstance(stages, list) and stages, "SELF_TEST_STAGES_MISSING")
    clean_stages = []
    for stage in stages:
        need(isinstance(stage, dict)
             and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", str(stage.get("id", "")))
             and stage.get("status") in ("passed", "failed", "skipped"),
             "SELF_TEST_STAGE_CONTRACT_INVALID")
        clean_stages.append({"id": stage["id"], "status": stage["status"]})
    success = data.get("ok") is True
    checked_probe = None
    if args.expect_failure_stage:
        need(not success and any(s["id"] == args.expect_failure_stage and s["status"] == "failed"
                                 for s in clean_stages), "EXPECTED_FAILURE_STAGE_NOT_OBSERVED")
    else:
        need(http_status == 200 and success and all(s["status"] == "passed" for s in clean_stages),
             "PUBLIC_SELF_TEST_NOT_PASSED")
        need(TRACE_RE.fullmatch(str(data.get("trace_id", ""))) is not None,
             "SELF_TEST_TRACE_MISSING")
        need(data.get("source_id") == expected_demo_id(before.get("connector_id", "")),
             "SELF_TEST_SOURCE_MISMATCH")
        probe = data.get("probe")
        need(isinstance(probe, dict) and re.fullmatch(r"[A-Za-z0-9_-]{16,128}",
                                                    str(probe.get("nonce", ""))),
             "SELF_TEST_NONCE_INVALID")
        checked_probe = check_ping(probe, probe["nonce"])
    after = status_for(local, args)
    need(source_projection(before)["business_source_hashes"]
         == source_projection(after)["business_source_hashes"],
         "SELF_TEST_CHANGED_BUSINESS_SOURCES")
    need(source_projection(before)["business_auth_profiles_sha256"]
         == source_projection(after)["business_auth_profiles_sha256"],
         "SELF_TEST_CHANGED_BUSINESS_AUTH_PROFILES")
    if args.bad_source_id:
        inspect_bad_source(after, args.bad_source_id)
    return {"http_status": http_status, "product_self_test_ok": success,
            "stages": clean_stages,
            "source_id": data.get("source_id") if SOURCE_RE.fullmatch(str(data.get("source_id", ""))) else None,
            "fresh_probe": checked_probe,
            "trace_id": data.get("trace_id") if TRACE_RE.fullmatch(str(data.get("trace_id", ""))) else None,
            "business_source_configuration_preserved": True,
            "enabled_unreachable_test_source_preserved": bool(args.bad_source_id)}


def artifact_record(args):
    if not args.artifact:
        return {"artifact_association": "not_supplied"}
    p = Path(args.artifact)
    need(p.is_file(), "ARTIFACT_MISSING")
    actual = hashlib.sha256(p.read_bytes()).hexdigest()
    need(args.expected_artifact_sha256 and actual == args.expected_artifact_sha256,
         "ARTIFACT_SHA256_MISMATCH")
    return {"artifact_sha256": actual, "artifact_size_bytes": p.stat().st_size}


def persist(args, report):
    directory = Path(args.evidence_dir).resolve()
    need(directory.is_absolute(), "ABSOLUTE_EVIDENCE_DIRECTORY_REQUIRED")
    if os.name != "nt":
        need(str(directory).startswith("/media/data3/dengkw/"),
             "VIP_EVIDENCE_MUST_STAY_IN_DATA3")
    directory.mkdir(parents=True, exist_ok=True)
    name = "builtin138-" + args.command + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") \
        + "-" + uuid.uuid4().hex[:8] + ".json"
    target = directory / name
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    latest = directory / ("builtin138-" + args.command + "-latest.json")
    temporary = latest.with_suffix(".tmp")
    temporary.write_text(target.read_text(encoding="utf8"), encoding="utf8")
    os.replace(temporary, latest)


def main():
    parser = argparse.ArgumentParser(description="Scoped Kaiwu bundled-demo acceptance; never installs or binds")
    parser.add_argument("command", choices=["snapshot", "local-probe", "public-probe", "self-test", "preserved", "demo-state"])
    parser.add_argument("--local-url", required=True, help="Existing loopback UI, e.g. http://127.0.0.1:17872")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--helper", default=str(Path(__file__).resolve().parents[1]
                        / "huawi-diagnostics-20261119/windows_diagnostics_acceptance_137.py"))
    parser.add_argument("--expected-version", default="1.3.8")
    parser.add_argument("--expected-commit")
    parser.add_argument("--expect-unbound", action="store_true")
    parser.add_argument("--expected-demo-state", choices=["on", "off"], help="Required for read-only demo-state case")
    parser.add_argument("--baseline", help="snapshot receipt for preserved")
    parser.add_argument("--private-mcp-json", help="Test-only integration JSON, never copied to output")
    parser.add_argument("--allow-self-test", action="store_true", help="Explicitly allow product self-test to register/connect only its demo")
    parser.add_argument("--expect-failure-stage", help="Exact observed stage ID; transport/JSON errors never count as expected failure")
    parser.add_argument("--bad-source-id", help="Existing enabled loopback test source with no listener; never added by this script")
    parser.add_argument("--artifact")
    parser.add_argument("--expected-artifact-sha256")
    args = parser.parse_args()
    report = {"case": args.command, "started_at_utc": stamp(), "passed": False, "exit_code": 1,
              "scope": "API/MCP acceptance only; installation, SCM and agent conversation are separate gates",
              "preservation_scope": "Local API source configuration and redacted auth metadata hashes; unchanged secret bytes require a separate local check or existing business-source authentication test",
              "os": {"system": platform.system(), "release": platform.release(), "version": platform.version(),
                     "machine": platform.machine()}}
    secrets = []
    try:
        need(re.fullmatch(r"\d+\.\d+\.\d+", args.expected_version) is not None, "VERSION_ARGUMENT_INVALID")
        loopback_url(args.local_url, "")
        report["helper_sha256"] = load_helper(args.helper, args.local_url)
        report.update(artifact_record(args))
        local = H.LocalClient()
        secrets.append(local.token)
        status = status_for(local, args)
        report["observed_version"] = re.search(r"\d+\.\d+\.\d+", status["version"]).group(0)
        report["source_projection"] = source_projection(status)
        if args.command == "local-probe":
            source = demo_source(status)
            report["local_mcp"] = run_rpc_probe(source["upstream_url"], {}, False)
        elif args.command == "public-probe":
            source = demo_source(status)
            need(bool(args.private_mcp_json), "TEST_INTEGRATION_JSON_REQUIRED")
            endpoint, headers = public_config(args.private_mcp_json, source["source_id"])
            for value in headers.values():
                if len(value) >= 8:
                    secrets.append(value)
                    if value.lower().startswith("bearer "):
                        secrets.append(value[7:])
            report["public_mcp"] = run_rpc_probe(endpoint, headers, True)
            local_probe = run_rpc_probe(source["upstream_url"], {}, False)
            need(report["public_mcp"]["fresh_probe"]["instance_id"]
                 == local_probe["fresh_probe"]["instance_id"],
                 "PUBLIC_PROBE_REACHED_DIFFERENT_DEMO_INSTANCE")
            report["public_and_local_instance_match"] = True
        elif args.command == "self-test":
            demo_source(status) if not args.expect_failure_stage else None
            report["self_test"] = self_test(local, args, status)
        elif args.command == "demo-state":
            need(bool(args.expected_demo_state), "EXPECTED_DEMO_STATE_REQUIRED")
            if args.expected_demo_state == "on":
                demo_source(status)
            else:
                demo = status.get("builtin_demo") or {}
                need(demo.get("enabled") is False and demo.get("running") is False
                     and not demo.get("error_code"), "DEMO_DID_NOT_STOP")
                sid = demo.get("source_id", "")
                need(SOURCE_RE.fullmatch(sid) is not None, "DEMO_SOURCE_ID_INVALID")
                if status.get("bound"):
                    need(sid == expected_demo_id(status.get("connector_id", "")),
                         "DEMO_SOURCE_ID_FORMULA_MISMATCH")
                sources = [s for s in status["sources"] if s.get("source_id") == sid]
                need(len(sources) == 1 and sources[0].get("disabled") is True,
                     "DEMO_SOURCE_NOT_DISABLED")
            report["expected_demo_state_verified"] = args.expected_demo_state
        elif args.command == "preserved":
            need(bool(args.baseline), "SNAPSHOT_RECEIPT_REQUIRED")
            baseline = load_json(args.baseline)
            need(baseline.get("case") == "snapshot" and baseline.get("passed") is True,
                 "BASELINE_NOT_A_PASSED_SNAPSHOT")
            before = baseline["source_projection"]
            now = report["source_projection"]
            for key in ("connector_id", "bound", "business_source_hashes",
                        "business_source_count", "business_auth_profiles_sha256"):
                need(before.get(key) == now.get(key), "PRESERVED_STATE_MISMATCH")
            if before.get("builtin_demo_source_id"):
                need(before["builtin_demo_source_id"] == now["builtin_demo_source_id"],
                     "DEMO_SOURCE_ID_CHANGED")
            report["preserved_state_verified"] = True
        report["passed"], report["exit_code"] = True, 0
    except BaseException as exc:
        report["failure_category"] = str(exc) if isinstance(exc, Failure) else (
            exc.category if H is not None and isinstance(exc, H.AcceptanceFailure) else "UNEXPECTED_EXECUTION_FAILURE")
        report["exception_type"] = type(exc).__name__
    report["finished_at_utc"] = stamp()
    encoded = json.dumps(report, ensure_ascii=False)
    if any(value in encoded for value in secrets if len(value) >= 8):
        report = {"case": args.command, "passed": False, "exit_code": 1,
                  "failure_category": "PRIVATE_VALUE_IN_RECEIPT_BLOCKED"}
    try:
        persist(args, report)
    except BaseException as exc:
        report["passed"], report["exit_code"] = False, 1
        report["failure_category"] = "EVIDENCE_WRITE_FAILED"
        report["exception_type"] = type(exc).__name__
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
