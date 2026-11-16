"""Opt-in isolated Harness acceptance; plan mode never opens provider settings.

Reuse business_loop_real_model's real product/MCP setup. No runtime factory,
transport, model response or tool result is substituted. Reports contain only
bounded observations/checks, never credentials, raw responses or answer text.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "live-harness-validation/v1"
CASES = ("text", "attachment", "mcp", "knowledge", "vision", "web", "cancel", "queue")
DEFAULT_CASES = ("text", "attachment", "mcp", "knowledge")
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
PATHS = ("directory", "provider_settings_file", "model_catalog_file", "harness_node", "harness_source")
CONFIG_KEYS = {"schema", *PATHS, "model_id", "reasoning_level", "api_port", "mcp_port"}


def configuration(path):
    """Read only the non-secret coordination schema, not files referenced by it."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Invalid configuration file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) - CONFIG_KEYS or value.get("schema") != SCHEMA:
        raise ValueError("Invalid configuration schema")
    for name in PATHS:
        item = value.get(name)
        if not isinstance(item, str) or not Path(item).is_absolute() or any(ord(c) < 32 for c in item):
            raise ValueError("Configuration requires absolute paths")
    for name in ("model_id", "reasoning_level"):
        if not isinstance(value.get(name), str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", value[name]):
            raise ValueError("Invalid model selection")
    for name, default in (("api_port", 18085), ("mcp_port", 18878)):
        value.setdefault(name, default)
        if type(value[name]) is not int or not 1024 <= value[name] <= 65535:
            raise ValueError("Invalid loopback port")
    if value["api_port"] == value["mcp_port"]:
        raise ValueError("Ports must differ")
    return value


def plan(cases, *, max_runs=4, max_steps=4, output_tokens=1024, timeout=120, attempt_reservation=24):
    if (not cases or len(set(cases)) != len(cases) or set(cases) - set(CASES)
            or any(type(x) is not int for x in (max_runs, max_steps, output_tokens, timeout, attempt_reservation))
            or not 1 <= max_runs <= 8 or not 1 <= max_steps <= 6
            or not 128 <= output_tokens <= 4096 or not 10 <= timeout <= 180
            or not 1 <= attempt_reservation <= 64):
        raise ValueError("Invalid bounded plan")
    submissions = sum(case not in {"web", "queue"} for case in cases)
    reserved = submissions * (max_steps + 2)
    if submissions > max_runs or reserved > attempt_reservation:
        raise ValueError("Plan exceeds submission or attempt reservation budget")
    return {"schema": SCHEMA, "status": "planned", "cases": list(cases),
            "model_requests_submitted": 0, "planned_run_submissions": submissions,
            "max_run_submissions": max_runs, "max_steps_per_run": max_steps,
            "max_output_tokens": output_tokens, "timeout_seconds": timeout,
            "planned_attempt_reservation": reserved, "attempt_reservation_budget": attempt_reservation,
            "enforced_provider_request_cap": False, "enforced_usd_cap": False,
            "boundary": "Reservation is not a provider HTTP/billing hard limit; no automatic resubmission.",
            "blocked_cases": {case: ("independent_search_configuration_not_supplied" if case == "web"
                                     else "queue_requires_separate_linux_sequence_validation")
                              for case in cases if case in {"web", "queue"}}}


@contextmanager
def isolated_environment(config, limits):
    # Settings can otherwise inherit ambient credentials/endpoints/features.
    previous = dict(os.environ)
    for name in list(os.environ):
        if name.startswith(("AGENT_", "ANTHROPIC_", "OPENAI_")):
            del os.environ[name]
    os.environ.update({"AGENT_MODEL_CATALOG": config["model_catalog_file"],
        "AGENT_PROVIDER_SETTINGS": config["provider_settings_file"],
        "AGENT_HARNESS_NODE_PATH": config["harness_node"],
        "AGENT_HARNESS_SOURCE_DIR": config["harness_source"],
        "AGENT_MAX_TURNS": str(limits["max_steps_per_run"]),
        "AGENT_HARNESS_MAX_OUTPUT_TOKENS": str(limits["max_output_tokens"]),
        "AGENT_KNOWLEDGE_EMBEDDING_MODEL": "local-lexical-v1"})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def observed_usage(trace):
    """Unknown receipts remain unknown; do not expose arbitrary provider fields."""
    events = trace.get("events", [])
    receipts = [event.get("data", {}) for event in events if event.get("type") == "usage.updated"]
    usage = receipts[-1] if receipts else (trace.get("run", {}).get("usage") or trace.get("usage") or {})
    ledger = usage.get("token_accounting") or {}
    stages = ledger.get("stages")
    verified = isinstance(stages, list) and bool(stages) and all(
        isinstance(stage, dict) and stage.get("provider_call") is True
        and stage.get("source") == "harness_provider" for stage in stages)
    totals = ledger.get("provider_totals") or {}
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens")
    safe = {name: totals.get(name) if type(totals.get(name)) is int and totals[name] >= 0 else None for name in fields}
    return {"provider_attempts": len(stages) if verified else None, "tokens": safe,
            "token_receipt_complete": verified and safe["input_tokens"] is not None and safe["output_tokens"] is not None,
            "cost_usd": None, "cost_status": "unknown"}


def geometry():
    """Synthetic transport/visual fixture, unrelated to any user artwork."""
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (320, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((35, 40, 115, 120), fill="red")
    draw.rectangle((205, 40, 285, 120), fill="blue")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def upload(client, path, filename, data, mime):
    response = client.post(path, files={"file": (filename, data, mime)})
    if response.status_code != 201:
        raise RuntimeError("Synthetic upload failed")
    return response.json()


def readable_sources(client, citations, kind):
    sources = []
    for item in citations:
        identity = item.get("id", "")
        if item.get("kind") != kind or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity):
            continue
        response = client.get("/api/v1/sources/" + identity)
        if response.status_code == 200 and response.json().get("kind") == kind:
            sources.append(response.json())
    return sources


def evaluate(case, trace, answer, sources, *, expected_marker="", expected_sha="", mcp_checks=None):
    events = trace.get("events", [])
    actual = any(event.get("type") == "harness.event" and event.get("data", {}).get("type")
                 in {"assistant/message", "assistant/attempt"} for event in events)
    status = trace.get("run", {}).get("status")
    checks = {"actual_harness_model_event": actual, "terminal_success": status == "succeeded"}
    if case in {"text", "attachment", "knowledge"}:
        checks["synthetic_fact_matches"] = bool(expected_marker) and expected_marker in answer
    if case in {"attachment", "knowledge"}:
        checks["authorized_source_matches_upload"] = any(source.get("locator", {}).get("sha256") == expected_sha for source in sources)
    if case == "vision":
        compact = answer.lower()
        checks["visual_fixture_description_matches"] = all(word in compact for word in ("red", "circle", "left", "blue", "square", "right"))
    if mcp_checks is not None:
        checks.update(mcp_checks)
        if case == "cancel":
            checks.pop("terminal_success")
    usage = observed_usage(trace)
    outcome = "failed" if not all(checks.values()) else "inconclusive" if not usage["token_receipt_complete"] else "passed"
    return {"status": outcome, "checks": checks, "usage": usage,
            "semantic_review": "required" if case == "vision" else "bounded_fixture_only",
            "remote_stop_proven": False if case == "cancel" else None}


def poll(client, request, run_id, timeout, *, cancel_started=None):
    deadline = time.monotonic() + timeout
    cancelled = False
    while True:
        run = request(client, "GET", f"/api/v1/runs/{run_id}")
        if run["status"] in TERMINAL:
            return run
        if not cancelled and (time.monotonic() >= deadline or cancel_started and cancel_started()):
            request(client, "POST", f"/api/v1/runs/{run_id}/cancel")
            cancelled = True
            deadline = time.monotonic() + 30
        elif cancelled and time.monotonic() >= deadline:
            raise TimeoutError("Cancellation was not observed")
        time.sleep(0.15)


def execute(config, limits):
    if os.name != "posix":
        raise RuntimeError("Linux Harness environment required")
    directory = Path(config["directory"])
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Use a new empty isolated directory")
    if any(part.is_symlink() for part in (directory, *directory.parents)):
        raise ValueError("Symlink workspace refused")
    sys.path[:0] = [str(ROOT / "examples"), str(ROOT / "server")]
    import business_loop_real_model as live
    fixture = live.fixture
    args = SimpleNamespace(directory=directory, model_id=config["model_id"],
        reasoning_level=config["reasoning_level"], api_port=config["api_port"], mcp_port=config["mcp_port"])
    report = {**limits, "status": "preparing", "synthetic_dataset": True,
              "real_model_authorized": True, "cases": [], "model_id": config["model_id"],
              "reasoning_level": config["reasoning_level"], "provider_route": "tokenhub",
              "run_submissions": 0, "report_path": str(directory / "live-report.json")}
    sockets, servers = [], []
    with isolated_environment(config, limits):
        # The existing preparer verifies private files, route and pinned runtime
        # before any product prompt. Its stdout is safe but keep one JSON result.
        with redirect_stdout(io.StringIO()):
            live.prepare(args)
        settings = live.validated_settings(args, directory)
        from app.model_catalog import ModelCatalog
        catalog = ModelCatalog(settings)
        provider = {**settings.provider_env(), **catalog.models[config["model_id"]].get("provider_env", {})}
        if not provider.get("ANTHROPIC_API_KEY"):
            raise ValueError("Harness requires API key authentication")
        capabilities = catalog.models[config["model_id"]].get("capabilities", [])
        report["vision_declared"] = "multimodal" in capabilities
        save = lambda: fixture.secure_json(directory / "live-report.json", report)
        save()
        try:
            credentials = fixture.load_credentials(directory)
            fixture.seed_database(directory / "synthetic.sqlite3")
            fixture.secure_json(directory / "mcp-calls.json", {"synthetic_dataset": True, "calls": []})
            app = live.create_product(args, directory, credentials)
            sockets.extend([fixture.bind_loopback(args.api_port), fixture.bind_loopback(args.mcp_port)])
            servers.extend([fixture.LocalServer(live.create_mcp(directory / "synthetic.sqlite3", credentials["mcp_bearer"]), sockets[1]),
                            fixture.LocalServer(app, sockets[0])])
            for server in servers:
                server.start()
            fixture.secure_json(directory / "instance.json", {"schema": live.MARKER, "synthetic_dataset": True,
                "runtime_factory_replaced": False, "model_requests_submitted": 0,
                "model_id": args.model_id, "reasoning_level": args.reasoning_level,
                "api_url": f"http://127.0.0.1:{args.api_port}", "mcp_url": f"http://127.0.0.1:{args.mcp_port}/mcp"})
            live.bootstrap(directory)
            request = fixture.request
            with live.client_for(directory) as (client, metadata):
                for case in limits["cases"]:
                    entry = {"case": case, "status": "preparing"}
                    report["cases"].append(entry)
                    if case in limits["blocked_cases"] or case == "vision" and not report["vision_declared"]:
                        entry.update(status="blocked", reason=limits["blocked_cases"].get(case, "model_multimodal_capability_not_declared"))
                        save()
                        continue
                    if report["run_submissions"] >= limits["max_run_submissions"]:
                        entry.update(status="blocked", reason="run_submission_budget_exhausted")
                        save()
                        break
                    expected_marker, expected_sha, files = "", "", []
                    assistant_id = metadata["assistant_id"]
                    if case == "text":
                        a, b = 100 + secrets.randbelow(300), 500 + secrets.randbelow(300)
                        expected_marker = str(a + b)
                        prompt = f"Compute {a}+{b}. Reply with only the integer. Do not use tools."
                    elif case == "attachment":
                        expected_marker = "FACT-" + secrets.token_hex(8)
                        data = ("Synthetic validation only. The inspection receipt is " + expected_marker).encode()
                        expected_sha = hashlib.sha256(data).hexdigest()
                        row = upload(client, "/api/v1/files", "facts.txt", data, "text/plain")
                        files = [row["id"]]
                        prompt = "Read the attached facts.txt, return its inspection receipt exactly and cite this attachment."
                    elif case == "vision":
                        data = geometry()
                        row = upload(client, "/api/v1/files", "geometry.png", data, "image/png")
                        files = [row["id"]]
                        prompt = "Describe the two colored shapes and their left/right positions in this attached image in English."
                        entry["fixture_sha256"] = hashlib.sha256(data).hexdigest()
                    elif case == "knowledge":
                        expected_marker = "LIMIT-" + secrets.token_hex(8)
                        data = ("Synthetic pump KQ-900. Inspection acceptance code: " + expected_marker).encode()
                        expected_sha = hashlib.sha256(data).hexdigest()
                        kb = request(client, "POST", "/api/v1/knowledge-bases", {"name": "Synthetic KQ-900 validation", "scope": "private"}, expected=201)
                        document = upload(client, f"/api/v1/knowledge-bases/{kb['id']}/documents", "pump.txt", data, "text/plain")
                        if document.get("status") != "ready":
                            entry.update(status="blocked", reason="local_knowledge_index_not_ready")
                            save()
                            continue
                        assistant = request(client, "POST", "/api/v1/assistants", {"name": "Synthetic knowledge validation", "scope": "private",
                            "knowledge_base_ids": [kb["id"]], "instructions": "Use authorized knowledge tools and cite registered sources.",
                            "default_model_id": args.model_id, "default_reasoning_level": args.reasoning_level}, expected=201)
                        assistant_id = assistant["id"]
                        prompt = "Search and read the knowledge document for synthetic pump KQ-900. Return its inspection acceptance code exactly and cite the document."
                    else:
                        prompt = live.CASES["orders_100k" if case == "mcp" else "cancel"][3]
                    conversation = request(client, "POST", "/api/v1/conversations", {"assistant_id": assistant_id,
                        "title": "Synthetic live validation: " + case, "model_id": args.model_id,
                        "reasoning_level": args.reasoning_level}, expected=201)
                    if conversation.get("runtime_type") != "harness":
                        raise RuntimeError("Unexpected runtime")
                    before = len(live.private_json(directory / "mcp-calls.json")["calls"])
                    entry.update(conversation_id=conversation["id"], status="submitting")
                    # Reserve before submission; a lost response must never trigger
                    # another POST or make an unknown charge appear free.
                    report["run_submissions"] += 1
                    entry["client_request_id"] = "live-" + secrets.token_hex(12)
                    save()
                    run = request(client, "POST", f"/api/v1/conversations/{conversation['id']}/messages",
                        {"text": prompt, "file_ids": files, "client_request_id": entry["client_request_id"]}, expected=202)
                    entry.update(run_id=run["run_id"], status="running")
                    save()
                    started = lambda: any(call["tool"] == "slow_order_summary" for call in live.private_json(directory / "mcp-calls.json")["calls"][before:])
                    poll(client, request, run["run_id"], limits["timeout_seconds"], cancel_started=started if case == "cancel" else None)
                    trace = request(client, "GET", f"/api/v1/runs/{run['run_id']}/trace")
                    messages = request(client, "GET", f"/api/v1/conversations/{conversation['id']}/messages")
                    answers = [item for item in messages if item.get("run_id") == run["run_id"] and item["role"] == "assistant"]
                    answer = "\n".join(item["text"] for item in answers)
                    citations = [citation for item in answers for citation in item.get("citations", [])]
                    sources = readable_sources(client, citations, "knowledge" if case in {"attachment", "knowledge"} else "data")
                    mcp_checks = None
                    if case in {"mcp", "cancel"}:
                        old_case = "orders_100k" if case == "mcp" else "cancel"
                        expected = live.business_result(directory / "synthetic.sqlite3", "orders", 100000 if case == "mcp" else 10000, "华北")
                        mcp_checks = live.evaluate(old_case, trace, live.private_json(directory / "mcp-calls.json")["calls"][before:], expected, answer)["checks"]
                        if case == "mcp":
                            mcp_checks["authorized_data_source_readable"] = bool(sources)
                    entry.update(evaluate(case, trace, answer, sources, expected_marker=expected_marker, expected_sha=expected_sha, mcp_checks=mcp_checks))
                    entry["canonical_run_status"] = trace["run"]["status"]
                    save()
                    if entry["status"] != "passed":
                        break  # Preserve failure/unknown, never retry or spend on later cases.
            states = [entry["status"] for entry in report["cases"]]
            report["status"] = ("failed" if "failed" in states else "inconclusive" if "inconclusive" in states
                                else "blocked" if "blocked" in states else "passed")
            if any(entry.get("semantic_review") == "required" for entry in report["cases"]):
                report["semantic_review"] = "required_before_visual_quality_claim"
        except Exception as exc:
            # Never serialize exception messages, prompts, settings or HTTP bodies.
            report["status"] = "inconclusive" if report["run_submissions"] else "blocked"
            report["error_type"] = type(exc).__name__
        finally:
            for server in reversed(servers):
                server.stop()
            for sock in sockets:
                sock.close()
            report["owned_servers_stopped"] = all(not server.thread.is_alive() for server in servers)
            report["model_requests_submitted"] = None  # Product runs are not HTTP requests.
            save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(DEFAULT_CASES))
    parser.add_argument("--max-runs", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=4)
    parser.add_argument("--output-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--attempt-reservation", type=int, default=24)
    args = parser.parse_args(argv)
    try:
        limits = plan(args.cases, max_runs=args.max_runs, max_steps=args.max_steps,
                      output_tokens=args.output_tokens, timeout=args.timeout, attempt_reservation=args.attempt_reservation)
        config = configuration(args.config) if args.config else None
        if args.real_model:
            if config is None:
                raise ValueError("Explicit non-secret configuration required")
            result = execute(config, limits)
        else:
            result = limits
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"planned", "passed"} else 2
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "status": "blocked", "error_type": type(exc).__name__,
                          "no_automatic_resubmission": True}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
