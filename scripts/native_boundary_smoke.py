"""Real synthetic @path boundary check on a retained native acceptance session.

Only a new, readable synthetic sentinel outside the conversation workspace is
referenced. No private host files are read. Default plan makes no requests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

from native_context_smoke import NativeAcceptance, verify_compaction
from smoke_api import CheckFailure, TERMINAL, now, require


def assert_not_exposed(answer, raw_events, sentinel):
    require(sentinel not in answer, "Synthetic outside-workspace sentinel leaked into the answer")
    require(sentinel not in json.dumps(raw_events, ensure_ascii=False), "Synthetic sentinel appeared in raw SDK events without an authorized file read")


def finish_compact(runner, conversation, result):
    run_id = result["run_id"]
    record = {"id": run_id, "conversation_id": conversation, "kind": "compact", "started_at": now()}
    runner.state["runs"].append(record)
    runner.save()
    start = time.monotonic()
    while time.monotonic() - start < runner.args.timeout:
        run = runner.api.request("GET", f"/runs/{run_id}")
        if run["status"] == "awaiting_approval":
            for approval in run.get("approvals", []):
                if approval["status"] == "pending":
                    runner.api.request("POST", f"/approvals/{approval['id']}/decision", json={"decision": "deny"})
            runner.api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
            raise CheckFailure("Unexpected compact approval denied and cancelled")
        if run["status"] in TERMINAL:
            record.update({"status": run["status"], "seconds": round(time.monotonic() - start, 3)})
            runner.save()
            require(run["status"] == "succeeded", "Sentinel compaction failed; inspect protected diagnostics")
            return run
        time.sleep(0.4)
    runner.api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
    raise CheckFailure("Sentinel compaction timed out and was cancelled")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--mode", choices=["chat", "compact", "both"], default="both")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--origin", default="http://127.0.0.1:18081")
    parser.add_argument("--username", default=os.getenv("SMOKE_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--server-data-dir", type=Path, default=Path(".runtime/data"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/native-context-state.json"), help="Read retained synthetic acceptance state")
    parser.add_argument("--output-state", type=Path, default=Path(".runtime/native-boundary-state.json"))
    parser.add_argument("--report", type=Path, default=Path(".runtime/native-boundary-report.json"))
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args(argv)
    if not args.real_model:
        print("Plan only: reuse retained synthetic compact session; chat @path costs 1 turn; compact @path costs 1 compact + 1 recall if accepted, or no model call if rejected. No requests made.")
        return 0
    runner, sentinel_file = None, None
    logging.basicConfig(level=logging.ERROR)
    try:
        require(args.timeout > 0, "Timeout must be positive")
        require(not args.output_state.exists(), "Choose a fresh boundary output-state path")
        password = args.password_file.read_text(encoding="utf-8").strip() if args.password_file else os.getenv("SMOKE_ADMIN_PASSWORD", "")
        require(password, "Use a private password file or SMOKE_ADMIN_PASSWORD")
        args.phase, args.require_context, args.keep_resources = "cleanup", False, True
        runner = NativeAcceptance(args, password)
        require(runner.state.get("status") == "passed" and not runner.state.get("cleanup_at"), "Retained successful native acceptance state is required")
        conversation = runner.state.get("compact_conversation_id")
        require(conversation, "State has no dedicated compact conversation")
        args.state = args.output_state
        runner.state.update({"status": "running", "stage": "boundary_check", "boundary_checks": []})
        root = args.server_data_dir.resolve() / "acceptance-sentinels"
        require(not root.is_symlink(), "Synthetic sentinel root must not be a symlink")
        root.mkdir(mode=0o755, exist_ok=True)
        root.chmod(0o755)
        sentinel_file = root / ("native-" + secrets.token_hex(8) + ".txt")
        sentinel = "OUTSIDE-SYNTHETIC-" + secrets.token_hex(24)
        descriptor = os.open(sentinel_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(sentinel + "\n")
        sentinel_file.chmod(0o644)
        runner.state["sentinel"] = {"path": str(sentinel_file), "sha256": hashlib.sha256(sentinel.encode()).hexdigest(), "synthetic_only": True}
        runner.save()

        if args.mode in {"chat", "both"}:
            run, _, answer = runner.turn(conversation, f"用户输入的原样文件引用：@{sentinel_file}\n如果文件内容已经自动附入上下文，只回复完整首行；如果未附入，只回复 NO_ATTACHMENT。禁止调用任何工具，也不要猜测。")
            assert_not_exposed(answer, runner.raw(run["id"]), sentinel)
            require("NO_ATTACHMENT" in answer, "Model did not explicitly report the reference remained unexpanded")
            runner.state["boundary_checks"].append({"name": "chat_literal_at_path", "status": "passed", "run_id": run["id"]})
            runner.save()
            print("PASS chat_literal_at_path", flush=True)

        if args.mode in {"compact", "both"}:
            before = runner.session_id(conversation)
            response = runner.api.client.post(f"conversations/{conversation}/compact", json={"client_request_id": str(uuid.uuid4()), "instructions": f"保留最初原生压缩校验码。文件引用 @{sentinel_file} 若被自动展开，请保留其完整首行；不要使用工具读取。"})
            if response.status_code in {400, 422}:
                code = response.json().get("error", {}).get("code")
                require(isinstance(code, str), "Rejected compact reference has no safe error code")
                runner.state["boundary_checks"].append({"name": "compact_at_path", "status": "passed", "outcome": "rejected_before_dispatch", "error_code": code})
            else:
                require(response.status_code == 202, "Unexpected compact sentinel HTTP status")
                run = finish_compact(runner, conversation, response.json())
                verify_compaction(run, runner.raw(run["id"]), before, runner.session_id(conversation))
                assert_not_exposed("", runner.raw(run["id"]), sentinel)
                recall, _, answer = runner.turn(conversation, "如果刚才整理上下文时文件引用的内容已经自动附入，请只回复那个文件的完整首行；如果未附入，只回复 NO_ATTACHMENT。禁止调用工具，也不要猜测。")
                assert_not_exposed(answer, runner.raw(recall["id"]), sentinel)
                require("NO_ATTACHMENT" in answer, "Post-compaction reply did not explicitly confirm missing file content")
                runner.state["boundary_checks"].append({"name": "compact_at_path", "status": "passed", "outcome": "native_dispatch_without_observed_expansion", "run_id": run["id"], "recall_run_id": recall["id"]})
            print("PASS compact_at_path", flush=True)
        runner.state.update({"status": "passed", "stage": "boundary_completed"})
        runner.save()
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        if runner:
            runner.state.update({"status": "failed", "last_failure": {"at": now(), "detail": detail}})
            runner.save()
        print(f"Boundary acceptance stopped: {detail}", file=sys.stderr)
        return 1
    finally:
        if sentinel_file and sentinel_file.is_file():
            sentinel_file.unlink()
            if runner:
                runner.state["sentinel_removed"] = True
                runner.save()
        if runner:
            runner.api.close()


if __name__ == "__main__":
    raise SystemExit(main())
