"""Isolated real SDK probe; default plan never reads provider credentials.

Run from the Linux project root. This does not operate the product database or
any business MCP. Reports never contain credentials, full answers or transcripts.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import secrets
import sys
import time

from anyio import fail_after


async def probe(args):
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
    from claude_agent_sdk.types import PermissionResultDeny
    root = args.project_root.resolve()
    sys.path.insert(0, str(root / "server"))
    from app.config import Settings
    provider_file = args.provider_file or root / ".runtime/provider-settings.json"
    settings = Settings(provider_settings=str(provider_file))
    provider = settings.provider_env()
    if not (provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError("ProviderCredentialsMissing")
    directory = root / ".runtime/native-sdk-probe" / (time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3))
    workspace, config = directory / "workspace", directory / "config"
    for path in (workspace, config):
        path.mkdir(parents=True, mode=0o700)
    provider.update({"CLAUDE_CONFIG_DIR": str(config), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
    report = {"schema_version": 1, "sdk_version": version("claude-agent-sdk"), "model": settings.model,
              "status": "running", "runs": [], "hooks": [], "contexts": [],
              "boundary": "Isolated native SDK probe with synthetic text, no MCP and no product sessions. Context control may send extra unstreamed token-counting requests; this probe does not count those network requests."}
    report_file = directory / "report.json"

    def save():
        fd = os.open(report_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")

    async def deny(*_):
        return PermissionResultDeny(message="This synthetic probe does not authorize tools")

    async def precompact(data, *_):
        report["hooks"].append({"event": "PreCompact", "trigger": data.get("trigger")})
        save()
        return {}

    options = ClaudeAgentOptions(cwd=str(workspace), env=provider, model=settings.model, effort="medium",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "仅执行合成上下文压缩验收，不调用工具、不写长期记忆。保留用户约定的原生校验码。"},
        tools=[], mcp_servers={}, strict_mcp_config=True, setting_sources=[],
        settings=json.dumps({"autoMemoryEnabled": False}), permission_mode="default", can_use_tool=deny,
        hooks={"PreCompact": [HookMatcher(hooks=[precompact])]}, max_turns=3, max_budget_usd=2,
        include_partial_messages=True, stderr=lambda _: None)
    client = ClaudeSDKClient(options=options)
    marker = "NATIVE-PROBE-" + secrets.token_hex(12)
    sessions = set()

    async def turn(label, text):
        item = {"label": label, "status": "running", "boundaries": []}
        report["runs"].append(item)
        save()
        answer = []
        start = time.monotonic()
        with fail_after(args.timeout):
            await client.query(text)
            async for event in client.receive_response():
                kind = type(event).__name__
                if kind == "SystemMessage" and event.subtype == "compact_boundary":
                    metadata = event.data.get("compact_metadata", {})
                    item["boundaries"].append({key: metadata.get(key) for key in ("trigger", "pre_tokens")})
                elif kind == "AssistantMessage":
                    if getattr(event, "error", None):
                        item["assistant_error"] = str(event.error)[:80]
                    for block in event.content:
                        if type(block).__name__ == "TextBlock":
                            answer.append(block.text)
                elif kind == "ResultMessage":
                    sessions.add(event.session_id)
                    item.update({"subtype": event.subtype, "is_error": event.is_error,
                                 "session_id": event.session_id, "terminal_reason": getattr(event, "terminal_reason", None),
                                 "api_error_status": getattr(event, "api_error_status", None),
                                 "usage": event.usage, "model_usage": getattr(event, "model_usage", None),
                                 "sdk_estimated_cumulative_cost_usd": event.total_cost_usd})
        joined = "\n".join(answer)
        item.update({"seconds": round(time.monotonic() - start, 3), "answer_sha256": hashlib.sha256(joined.encode()).hexdigest()})
        if item.get("is_error") is not False or item.get("assistant_error") or (item.get("api_error_status") or 0) >= 400:
            item["status"] = "failed"
            save()
            raise RuntimeError("NativeRunFailed")
        item["status"] = "passed"
        save()
        print(f"PASS {label}", flush=True)
        return joined

    async def context(label):
        item = {"label": label}
        start = time.monotonic()
        try:
            with fail_after(args.context_timeout):
                value = await client.get_context_usage()
            keys = ("totalTokens", "maxTokens", "rawMaxTokens", "percentage", "model", "isAutoCompactEnabled", "autoCompactThreshold")
            item.update({"status": "available", **{key: value.get(key) for key in keys},
                         "categories": [{key: entry.get(key) for key in ("name", "tokens")} for entry in value.get("categories", [])]})
        except Exception as exc:
            item.update({"status": "unavailable", "error_type": type(exc).__name__})
        item["seconds"] = round(time.monotonic() - start, 3)
        report["contexts"].append(item)
        save()
        print(f"CONTEXT {label} {item['status']}", flush=True)

    try:
        # connect/disconnect enter/exit the SDK's persistent AnyIO task group.
        # Keep them in the same task and outside short-lived cancel scopes.
        await client.connect()
        info = await client.get_server_info()
        report["compact_advertised"] = "compact" in (info or {}).get("commands", []) or "compact" in (info or {}).get("slash_commands", [])
        lines = "\n".join(f"合成占位记录 {i:03d}: SYN-{i:03d}，没有客户资料，背景可合并，校验码必须保留。" for i in range(140))
        await turn("seed", f"请记住原生校验码 {marker}。这些合成背景可压缩，但校验码要完整保留。只回复已记录，不调用工具。\n{lines}")
        await turn("confirm_before_compaction", "请只回复之前约定的原生校验码，不使用工具。")
        await context("before")
        await turn("compact", "/compact 保留原生校验码的完整字面值以及本次验收目标，合成占位记录可合并。")
        boundary = report["runs"][-1]["boundaries"]
        if not any(item.get("trigger") == "manual" and type(item.get("pre_tokens")) is int for item in boundary):
            raise RuntimeError("NativeCompactBoundaryMissing")
        await context("after")
        answer = await turn("recall_after_compaction", "请只回复我们在最初约定的原生校验码，不使用工具。")
        if marker not in answer or len(sessions) != 1:
            raise RuntimeError("CompactedSessionContinuityFailed")
        report.update({"status": "passed", "marker_recalled": True, "same_native_session": True})
        save()
        return 0
    except Exception as exc:
        report.update({"status": "failed", "error_type": type(exc).__name__, "error_code": str(exc) if str(exc) in {"NativeRunFailed", "NativeCompactBoundaryMissing", "CompactedSessionContinuityFailed"} else None})
        save()
        print(f"FAILED {report['error_type']} {report.get('error_code') or ''}", flush=True)
        return 1
    finally:
        try:
            await client.disconnect()
            report["disconnected"] = True
        except Exception as exc:
            report["disconnect_error_type"] = type(exc).__name__
        save()
        print(f"REPORT {report_file}", flush=True)


async def bounded_probe(args):
    # Python3.10-compatible overall timeout: the entire SDK lifetime stays in
    # one child task; do not wrap only connect() in asyncio.wait_for().
    return await asyncio.wait_for(probe(args), timeout=4 * args.timeout + 2 * args.context_timeout + 60)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--provider-file", type=Path)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--context-timeout", type=float, default=90)
    args = parser.parse_args()
    if not args.real_model:
        print("Plan only: isolated SDK, two synthetic turns, context control, native /compact, context control and recall. No requests made.")
        return 0
    try:
        return asyncio.run(bounded_probe(args))
    except Exception as exc:
        print(f"Probe stopped before report: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
