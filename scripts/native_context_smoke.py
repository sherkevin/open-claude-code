"""Opt-in native compaction and cross-conversation memory acceptance.

Normal product HTTP drives the real SDK. Local SQLite is strictly read-only and
limited to this script's synthetic conversations/runs. No provider mocks.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import logging
import math
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import uuid

from lifecycle_smoke import digest, private_json
from smoke_api import API, CheckFailure, TERMINAL, now, parse_sse, require


def verify_compaction(run, raw_events, before_session, after_session):
    require(before_session and before_session == after_session, "Compaction changed or lost the native SDK session")
    boundaries = [item.get("value", {}).get("data", {}).get("compact_metadata", {})
                  for item in raw_events if item.get("value", {}).get("subtype") == "compact_boundary"]
    require(boundaries, "No native compact_boundary was observed; success/no-op is not compaction evidence")
    require(any(item.get("trigger") == "manual" and type(item.get("pre_tokens")) is int and item["pre_tokens"] > 0
                for item in boundaries), "Native boundary has no valid manual trigger/token evidence")
    metadata = run.get("compaction") or {}
    require(run.get("kind") == "compact" and metadata.get("status") == "completed", "Product compaction state disagrees with the native event")
    return [{key: item.get(key) for key in ("trigger", "pre_tokens")} for item in boundaries]


def verify_memory_write(trace, contents, marker):
    completed = [event.get("data", {}) for event in trace.get("events", []) if event.get("type") == "tool.completed"]
    require(any(item.get("tool_name") in {"Write", "Edit"} and item.get("success") is True for item in completed),
            "No successful native memory-capable Write/Edit was observed")
    require(any(marker in text for text in contents), "Native memory files do not contain the synthetic marker")


def verify_context(value, required=False):
    require(value.get("source") == "native_runtime", "Context does not identify its native source")
    status = value.get("status")
    require(status in {"available", "unavailable", "not_measured"}, "Unknown context measurement status")
    if status == "available":
        require(type(value.get("total_tokens")) is int and value["total_tokens"] >= 0, "Context has no valid current-token count")
        require(type(value.get("max_tokens")) is int and value["max_tokens"] > 0, "Context has no valid measured window")
        require(type(value.get("percentage")) in {int, float} and value["percentage"] >= 0, "Context has no valid percentage")
    else:
        for key in ("total_tokens", "max_tokens", "percentage"):
            number = value.get(key)
            require(number is None or (type(number) in {int, float} and math.isfinite(number) and number >= 0), "Partial native context contains an invalid measured value")
        require(any(value.get(key) is None for key in ("total_tokens", "max_tokens", "percentage")), "Unavailable context has no unknown fields")
        require(not required, "Native context counting is unavailable on this provider")
    return {key: value.get(key) for key in ("source", "status", "total_tokens", "max_tokens", "percentage", "auto_compact_enabled", "auto_compact_threshold", "model", "measured_at", "cached")}


class NativeAcceptance:
    def __init__(self, args, password):
        self.args = args
        self.database = args.server_data_dir.resolve() / "app.sqlite3"
        require(self.database.is_file(), "Existing local server database is required for read-only native evidence")
        self.state = {"version": 1, "stage": "new", "checks": [], "runs": [], "conversations": []}
        if args.phase == "cleanup":
            self.state = json.loads(args.state.read_text(encoding="utf-8"))
            require(self.state.get("version") == 1, "State version mismatch")
        else:
            require(not args.state.exists(), "Choose a new state path or clean up the existing run first")
        self.api = API(args.base_url, args.origin)
        try:
            require(not self.state.get("base_url") or self.state["base_url"] == self.api.base, "State belongs to another API")
            user = self.api.login(args.username, password)
            require(user.get("role") == "admin", "Acceptance requires a dedicated administrator")
            require(not self.state.get("user_id") or self.state["user_id"] == user["id"], "Continue with the same acceptance account")
            self.state.update({"base_url": self.api.base, "user_id": user["id"]})
        except Exception:
            self.api.close()
            raise

    def save(self):
        private_json(self.args.state, self.state)
        report = {key: value for key, value in self.state.items() if key not in {"compact_marker", "memory_marker"}}
        report["evidence_boundary"] = "Synthetic manual native compaction and memory only; no automatic-threshold, customer-data, cross-user inference or long-term quality claim."
        private_json(self.args.report, report)

    def check(self, name, operation):
        start = time.monotonic()
        try:
            result = operation()
        except Exception as exc:
            self.state["checks"].append({"name": name, "status": "failed", "at": now(), "detail": str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__})
            self.save()
            raise
        self.state["checks"].append({"name": name, "status": "passed", "seconds": round(time.monotonic() - start, 3), "at": now()})
        self.save()
        print(f"PASS {name}", flush=True)
        return result

    def query_readonly(self, statement, values):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            return db.execute(statement, values).fetchall()

    def session_id(self, conversation):
        rows = self.query_readonly("SELECT sdk_session_id FROM conversations WHERE id=? AND user_id=?", (conversation, self.state["user_id"]))
        require(rows and rows[0][0], "Native SDK session identity is missing")
        return rows[0][0]

    def raw(self, run_id):
        return [json.loads(row[0]) for row in self.query_readonly("SELECT payload FROM raw_events WHERE run_id=? ORDER BY id", (run_id,))]

    def new_conversation(self, label):
        value = self.api.request("POST", "/conversations", expected=(200, 201), json={"assistant_id": self.state["assistant_id"], "title": f"原生验收 {label} {self.state['tag']}"})
        self.state["conversations"].append(value["id"])
        self.save()
        return value["id"]

    def turn(self, conversation, prompt, compact=False):
        body = {"client_request_id": str(uuid.uuid4())}
        body.update({"instructions": prompt} if compact else {"text": prompt, "file_ids": []})
        result = self.api.request("POST", f"/conversations/{conversation}/{'compact' if compact else 'messages'}", expected=(200, 202), json=body)
        run_id = result["run_id"]
        record = {"id": run_id, "conversation_id": conversation, "kind": "compact" if compact else "chat", "started_at": now()}
        self.state["runs"].append(record)
        self.save()
        start = time.monotonic()
        while time.monotonic() - start < self.args.timeout:
            run = self.api.request("GET", f"/runs/{run_id}")
            if run["status"] == "awaiting_approval":
                for approval in run.get("approvals", []):
                    if approval["status"] == "pending":
                        self.api.request("POST", f"/approvals/{approval['id']}/decision", json={"decision": "deny"})
                self.api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
                raise CheckFailure("Unexpected approval was denied and cancelled; no business write is approved by this script")
            if run["status"] in TERMINAL:
                break
            time.sleep(0.4)
        else:
            self.api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
            raise CheckFailure("Native acceptance run timed out and was cancelled")
        record.update({"status": run["status"], "seconds": round(time.monotonic() - start, 3)})
        self.save()
        require(run["status"] == "succeeded", "Native acceptance run failed; inspect protected server diagnostics")
        trace = self.api.request("GET", f"/runs/{run_id}/trace")
        answer = "\n".join(item["text"] for item in trace["messages"] if item["role"] == "assistant")
        record.update({"answer_sha256": digest(answer), "usage": trace.get("usage"), "sdk_session_id": self.session_id(conversation)})
        self.save()
        return run, trace, answer

    def context(self, conversation, refresh=False):
        value = self.api.request("POST" if refresh else "GET", f"/conversations/{conversation}/context" + ("/refresh" if refresh else ""), timeout=self.args.timeout)
        return verify_context(value, required=self.args.require_context and refresh)

    def compact(self):
        conversation = self.new_conversation("压缩")
        self.state["compact_conversation_id"] = conversation
        marker = self.state["compact_marker"] = "COMPACT-" + secrets.token_hex(12)
        self.save()
        notes = "\n".join(f"合成记录 {index:03d}：编号 SYN-{index:03d}，该测试没有客户资料；记录仅作上下文占位，最终事实以原生校验码为准。" for index in range(160))
        self.turn(conversation, f"请记住原生压缩校验码 {marker}，这是之后必须完整保留的关键验收事实。以下都是合成背景。禁止使用工具或保存到长期记忆。只回复已记录。\n{notes}")
        original = self.api.request("GET", f"/conversations/{conversation}/messages")
        original_hashes = {item["id"]: digest(item) for item in original}
        before_session = self.session_id(conversation)
        self.state["context_before"] = self.context(conversation, refresh=True)
        run, trace, _ = self.turn(conversation, "保留原生压缩校验码的完整字面值及本次验收目标。合成记录可以合并。不要把会话内容写入长期记忆。", compact=True)
        self.state["native_boundaries"] = verify_compaction(run, self.raw(run["id"]), before_session, self.session_id(conversation))
        require(any(event["type"] == "context.compacted" for event in trace["events"]), "Product trace lacks the compacted event")
        with self.api.client.stream("GET", f"runs/{run['id']}/events", headers={"Last-Event-ID": "0"}) as response:
            require(response.status_code == 200, "Compaction event replay failed")
            replayed = list(parse_sse(response.iter_lines()))
        require(any(event["type"] == "context.compacted" for event in replayed), "Compaction boundary was not persisted for SSE replay")
        current = {item["id"]: digest(item) for item in self.api.request("GET", f"/conversations/{conversation}/messages")}
        require(all(current.get(key) == value for key, value in original_hashes.items()), "Compaction rewrote existing product history")
        self.state["context_after"] = self.context(conversation)
        _, _, answer = self.turn(conversation, "请只回复之前约定的原生压缩校验码，禁止使用工具或长期记忆。")
        require(marker in answer, "Post-compaction response did not preserve the earlier marker")
        require(before_session == self.session_id(conversation), "Follow-up did not continue the compacted native session")

    def memory_view(self):
        return self.api.request("GET", f"/assistants/{self.state['assistant_id']}/memory")

    def clear_memory(self):
        view = self.memory_view()
        return self.api.request("DELETE", f"/assistants/{self.state['assistant_id']}/memory", params={"revision": view["revision"]})

    def memory(self):
        marker = self.state["memory_marker"] = "MEMORY-" + secrets.token_hex(12)
        first = self.new_conversation("记忆写入")
        _, trace, _ = self.turn(first, f"这是原生记忆验收：请使用原生长期记忆文件记住稳定项目约定：原生记忆校验码为 {marker}。请确实写入你自己的原生记忆目录，在 MEMORY.md 索引中保留完整校验码；只写验收记忆，不写工作区的普通文件。完成后只回复已保存。")
        view = self.memory_view()
        contents = [self.api.request("GET", f"/assistants/{self.state['assistant_id']}/memory/file", params={"path": item["path"]})["content"] for item in view["files"]]
        verify_memory_write(trace, contents, marker)
        self.state["memory_saved"] = {"revision": view["revision"], "file_count": len(contents), "content_sha256": digest(contents)}
        second = self.new_conversation("记忆读取")
        _, recall_trace, answer = self.turn(second, "我们约定的原生记忆校验码是什么？仅回答校验码，没有记忆则 NO_NATIVE_MEMORY。不要创建或修改记忆。")
        self.state["memory_recall_tools"] = [event.get("data", {}).get("tool_name") for event in recall_trace["events"] if event["type"] == "tool.started"]
        require(marker in answer, "New conversation did not recall actual native memory")
        require(self.session_id(first) != self.session_id(second), "Cross-conversation memory test reused a native conversation")
        require(not self.clear_memory()["files"], "Memory clear left managed memory files")
        third = self.new_conversation("清空后读取")
        _, _, answer = self.turn(third, "我们约定的原生记忆校验码是什么？仅回答校验码，没有记忆则 NO_NATIVE_MEMORY。不要创建或修改记忆。")
        require(marker not in answer and "NO_NATIVE_MEMORY" in answer, "Fresh conversation loaded a marker after memory was cleared")
        require(self.session_id(third) not in {self.session_id(first), self.session_id(second)}, "Memory-clear observation is not a fresh native session")

    def run(self):
        require(self.api.request("GET", "/health/ready").get("model_configured"), "No provider configured; no inference was submitted")
        self.state["tag"] = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        assistant = self.api.request("POST", "/assistants", expected=(200, 201), json={"name": f"原生验收 {self.state['tag']}", "description": "合成原生上下文与记忆验收", "instructions": "仅执行合成验收任务。只有用户明确要求保存时才写入自己的原生长期记忆。不得读取其他用户或其他助手目录。不要使用业务工具。", "connections": [], "access": {"all_members": False, "user_ids": []}, "enabled": True})
        self.state.update({"assistant_id": assistant["id"], "stage": "running", "group": self.args.group})
        self.save()
        if self.args.group in {"compact", "all"}:
            self.check("native_manual_compaction_and_continuity", self.compact)
        if self.args.group in {"memory", "all"}:
            self.check("native_memory_write_new_session_read_and_clear", self.memory)
        self.state.update({"stage": "completed", "status": "passed", "finished_at": now()})
        self.save()
        if not self.args.keep_resources:
            self.cleanup()

    def cleanup(self):
        for record in self.state["runs"]:
            run = self.api.request("GET", f"/runs/{record['id']}")
            if run["status"] not in TERMINAL:
                self.api.request("POST", f"/runs/{record['id']}/cancel", expected=(200, 202))
                raise CheckFailure("Cancelled an active acceptance run; retry cleanup after it finishes")
        if self.state.get("assistant_id") and not self.state.get("cleanup_at"):
            self.clear_memory()
            for conversation in self.state["conversations"]:
                self.api.request("PATCH", f"/conversations/{conversation}", json={"archived": True})
            self.api.request("DELETE", f"/assistants/{self.state['assistant_id']}", expected=(200, 204))
        self.state["cleanup_at"] = now()
        self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", nargs="?", choices=["plan", "run", "cleanup"], default="plan")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--group", choices=["compact", "memory", "all"], default="all")
    parser.add_argument("--require-context", action="store_true", help="Fail if extra native token-counting requests are unsupported")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--origin", default="http://127.0.0.1:18081")
    parser.add_argument("--username", default=os.getenv("SMOKE_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--server-data-dir", type=Path, default=Path(".runtime/data"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/native-context-state.json"))
    parser.add_argument("--report", type=Path, default=Path(".runtime/native-context-report.json"))
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--keep-resources", action="store_true")
    args = parser.parse_args(argv)
    if args.phase == "plan":
        print("compact: 2 ordinary model tasks + 1 native /compact, plus native token-counting requests; memory: 3 model tasks; all: both groups. No requests made.")
        print("Run on the API server with read-only local database access. Success requires original SDK evidence, not fabricated runtime results. Cleanup archives only this run's synthetic resources.")
        return 0
    if args.phase == "run" and not args.real_model:
        print("Refusing provider requests without --real-model. No requests made.", file=sys.stderr)
        return 2
    runner = None
    logging.basicConfig(level=logging.ERROR)
    try:
        require(args.timeout > 0, "Timeout must be positive")
        password = args.password_file.read_text(encoding="utf-8").strip() if args.password_file else os.getenv("SMOKE_ADMIN_PASSWORD", "")
        require(password, "Use a private password file or SMOKE_ADMIN_PASSWORD")
        runner = NativeAcceptance(args, password)
        (runner.run if args.phase == "run" else runner.cleanup)()
        print(f"Native acceptance {args.phase} complete; sanitized report: {args.report}")
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        if runner:
            runner.state["last_failure"] = {"at": now(), "detail": detail}
            runner.save()
        print(f"Native acceptance stopped: {detail}", file=sys.stderr)
        return 1
    finally:
        if runner:
            runner.api.close()


if __name__ == "__main__":
    raise SystemExit(main())
