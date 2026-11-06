"""Opt-in restart acceptance; the operator, never this script, restarts the API.

One case costs two real model turns: prepare an interrupted action, then prove a
new user message works. Run both cases under both modes for the complete matrix.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import uuid

from lifecycle_smoke import Observer, digest, identity_key, private_json, proc_identity
from smoke_api import API, CheckFailure, TERMINAL, now, parse_sse, require, tool_matches


EXPECTED_STATUS = {"graceful": "cancelled", "crash": "interrupted"}
TARGET_TOOL = {"pending-write": "create_ticket", "slow-read": "slow_lookup"}


def history_checkpoint(messages):
    return [{"id": item["id"], "role": item["role"], "run_id": item.get("run_id"),
             "text_length": len(item["text"]), "text_sha256": digest(item["text"]),
             "file_ids_sha256": digest(item.get("file_ids", []))} for item in messages]


def assert_history_preserved(before, after):
    require([item["id"] for item in before] == [item["id"] for item in after], "Restart removed, reordered or automatically added messages")
    for saved, item in zip(before, after):
        require(saved["role"] == item["role"] and saved["run_id"] == item.get("run_id"), "Restart changed message identity or ownership")
        require(saved["file_ids_sha256"] == digest(item.get("file_ids", [])), "Restart changed message attachments")
        text = item["text"]
        # Streaming may append assistant text between prepare and shutdown. Existing text cannot disappear.
        if saved["role"] != "assistant":
            require(len(text) == saved["text_length"], "Restart changed an existing user message")
        require(digest(text[:saved["text_length"]]) == saved["text_sha256"], "Restart lost or rewrote persisted message text")


def started_calls(trace):
    return [{"event_id": event["id"], "call_id": event["data"].get("tool_call_id"),
             "tool_name": event["data"].get("tool_name"), "arguments_sha256": digest(event["data"].get("arguments", {}))}
            for event in trace["events"] if event["type"] == "tool.started"]


def assert_prepared(case, run, trace, arguments):
    tool = TARGET_TOOL[case]
    starts = [event for event in trace["events"] if event["type"] == "tool.started"]
    require(len(starts) == 1 and tool_matches(starts[0]["data"].get("tool_name"), tool), "Preparation must start exactly the requested synthetic tool")
    require(starts[0]["data"].get("arguments") == arguments, "Model changed synthetic acceptance arguments")
    require(not any(event["type"] == "tool.completed" for event in trace["events"]), "Tool already completed before restart could be prepared")
    if case == "pending-write":
        require(run["status"] == "awaiting_approval", "Write is not awaiting approval")
        pending = [item for item in run["approvals"] if item["status"] == "pending"]
        require(len(pending) == 1 and tool_matches(pending[0]["tool_name"], tool), "Expected one pending write approval")
        require(pending[0]["arguments"] == arguments, "Pending approval arguments differ from the requested write")
        require(datetime.fromisoformat(pending[0]["expires_at"]) > datetime.now(timezone.utc), "Approval already expired before restart")
        return pending[0]["id"]
    require(run["status"] == "running" and not run.get("approvals"), "Slow read is not running without approval")
    return None


def assert_terminal(mode, case, run, trace, saved_calls):
    require(run["status"] == EXPECTED_STATUS[mode], "Interrupted run status does not match the declared restart mode")
    require(not run.get("approvals"), "Restart left a pending approval usable")
    require(started_calls(trace) == saved_calls, "Restart reissued or changed a tool call")
    completions = [event for event in trace["events"] if event["type"] == "run.completed"]
    require(len(completions) == 1 and completions[0]["data"]["status"] == EXPECTED_STATUS[mode], "Restart must persist exactly one matching terminal event")
    require(not any(event["type"] == "approval.resolved" and event["data"].get("decision") == "allow" for event in trace["events"]), "A write was approved during restart acceptance")
    require(not any(event["type"] == "tool.completed" and event["data"].get("success") for event in trace["events"]), f"The {case} action completed successfully before interruption")


def reject_stale_approval(api, approval_id):
    # Deliberately use DENY: even a broken stale-approval implementation must not execute a write.
    api.request("POST", f"/approvals/{approval_id}/decision", expected=(409,), json={"decision": "deny"})


class RestartSafety:
    def __init__(self, args, password):
        self.args = args
        self.api = API(args.base_url, args.origin)
        self.observer = Observer(args.server_data_dir, args.server_process_file)
        self.ticket_db = args.demo_data_dir.resolve() / "tickets.sqlite3"
        require(self.ticket_db.is_file(), "Existing synthetic MCP ticket database is required")
        if args.phase == "prepare":
            require(not args.state.exists(), "State already exists; choose a new path instead of overwriting evidence")
            self.state = {"version": 1, "stage": "new", "base_url": self.api.base, "mode_declared": args.mode,
                          "case": args.case, "checks": [], "run_ids": [], "started_at": now()}
        else:
            self.state = json.loads(args.state.read_text(encoding="utf-8"))
            require(self.state.get("version") == 1 and self.state.get("base_url") == self.api.base, "State belongs to another server or script version")
            require(self.state["mode_declared"] == args.mode and self.state["case"] == args.case, "Keep the prepared restart mode and case unchanged")
        user = self.api.login(args.username, password)
        require(user["role"] == "admin", "Restart acceptance requires an administrator")
        require(not self.state.get("user_id") or self.state["user_id"] == user["id"], "Use the same acceptance account after restart")
        self.state["user_id"] = user["id"]

    def save(self):
        private_json(self.args.state, self.state)
        report = dict(self.state)
        report["evidence_boundary"] = [
            "Termination mode is operator-declared; retain actual service stop/crash logs separately.",
            "tool.started is the application's pre-tool hook, not independent proof of MCP request receipt.",
            "Only this synthetic fixture, request key and persisted application events are checked; no exactly-once guarantee for arbitrary external writes.",
            "Only the selected case and restart mode are covered by this report.",
        ]
        private_json(self.args.report, report)

    def check(self, name, operation):
        started = time.monotonic()
        try:
            result = operation()
        except Exception as exc:
            self.state["checks"].append({"name": name, "status": "failed", "detail": str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__, "at": now()})
            self.save()
            raise
        self.state["checks"].append({"name": name, "status": "passed", "seconds": round(time.monotonic() - started, 3), "at": now()})
        self.save()
        print(f"PASS {name}", flush=True)
        return result

    def ticket_count(self):
        with closing(sqlite3.connect(self.ticket_db.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
            return database.execute("SELECT count(*) FROM tickets WHERE request_key=?", (self.state["request_key"],)).fetchone()[0]

    def assert_no_ticket(self):
        require(self.ticket_count() == 0, "Synthetic request key has a ticket despite never being approved")

    def history(self):
        return self.api.request("GET", f"/conversations/{self.state['conversation_id']}/messages")

    def trace(self, run_id):
        return self.api.request("GET", f"/runs/{run_id}/trace")

    def send(self, prompt):
        result = self.api.request("POST", f"/conversations/{self.state['conversation_id']}/messages", expected=(202,), json={"client_request_id": str(uuid.uuid4()), "text": prompt, "file_ids": []})
        self.state["run_ids"].append(result["run_id"])
        self.save()
        return result["run_id"]

    def prepare(self):
        require(self.api.request("GET", "/health/ready").get("model_configured"), "Provider is not configured; no model message was submitted")
        tag = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
        self.state.update({"tag": tag, "request_key": "restart-safety-" + tag, "marker": "RESTART-OK-" + secrets.token_hex(6)})
        self.check("synthetic_ticket_absent_before_prepare", self.assert_no_ticket)
        token = os.getenv("DEMO_MCP_TOKEN", "")
        connection = self.api.request("POST", "/connections", expected=(200, 201), json={
            "name": f"重启验收连接 {tag}", "transport": "http", "url": self.args.mcp_url,
            "auth_type": "bearer" if token else "none", **({"credential": token} if token else {})})
        self.state["connection_id"] = connection["id"]
        self.save()
        connection = self.api.request("POST", f"/connections/{connection['id']}/test", timeout=60)
        require(connection["status"] == "connected" and {"create_ticket", "slow_lookup"}.issubset({tool["name"] for tool in connection["tools"]}), "Synthetic MCP tools are unavailable")
        assistant = self.api.request("POST", "/assistants", expected=(200, 201), json={
            "name": f"重启安全验收 {tag}", "description": "仅合成验收，不连接客户业务。", "enabled": True,
            "instructions": "严格执行用户指定的一次工具调用，不改参数。创建工单必须等待审批。被拒绝、取消或中断后不重试，不补做旧操作。用户要求纯文本回复时不得调用工具。",
            "access": {"all_members": False, "user_ids": []}, "connections": [{"connection_id": connection["id"], "allowed_tools": ["create_ticket", "slow_lookup"], "auto_approved_tools": ["slow_lookup"]}]})
        self.state["assistant_id"] = assistant["id"]
        self.save()
        conversation = self.api.request("POST", "/conversations", expected=(200, 201), json={"assistant_id": assistant["id"], "title": f"重启验收 {self.args.mode} {self.args.case} {tag}"})
        self.state["conversation_id"] = conversation["id"]
        self.save()
        if self.args.case == "pending-write":
            arguments = {"title": "重启验收合成故障", "description": "仅测试中断后的审批失效，不是真实工单，不得自动重试。", "requester": "重启验收", "request_key": self.state["request_key"]}
            prompt = "请只调用一次 create_ticket，参数严格使用以下JSON，然后等待人工审批，不得另做操作：" + json.dumps(arguments, ensure_ascii=False)
        else:
            arguments = {"delay_seconds": 30}
            prompt = '请只调用一次 slow_lookup，严格使用参数 {"delay_seconds":30}。不得调用其他工具，不得重试。'
        run_id = self.send(prompt)
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            run = self.api.request("GET", f"/runs/{run_id}")
            trace = self.trace(run_id)
            if run["status"] in TERMINAL:
                raise CheckFailure("Run finished before reaching the required restart preparation state")
            if started_calls(trace) and (self.args.case == "slow-read" or run["status"] == "awaiting_approval"):
                approval_id = assert_prepared(self.args.case, run, trace, arguments)
                self.state.update({"approval_id": approval_id, "started_calls": started_calls(trace),
                                   "history_checkpoint": history_checkpoint(self.history()),
                                   "before_restart": self.observer.snapshot(conversation["id"]),
                                   "prepared_at": now(), "stage": "prepared"})
                require(self.state["before_restart"]["active_run_id"] == run_id, "Prepared run is no longer active")
                require(self.state["before_restart"]["workers"], "No SDK worker was observed for the active run")
                self.check("prepared_action_is_uncompleted_and_unapproved", self.assert_no_ticket)
                self.save()
                print("PREPARED: restart the isolated API now using the declared mode; this script never performs the restart.", flush=True)
                if self.args.case == "slow-read":
                    print("The slow tool is bounded to 30 seconds. Restart immediately; a completed action fails the check.", flush=True)
                return
            time.sleep(0.2)
        raise CheckFailure("Timed out before the requested pending/running action was prepared")

    def recovery_assertions(self):
        before = self.state["before_restart"]
        current = self.observer.snapshot(self.state["conversation_id"])
        require(identity_key(before["server_process"]) != identity_key(current["server_process"]), "The API process has not changed")
        for old in before["workers"]:
            alive = proc_identity(old["pid"])
            require(not alive or identity_key(alive) != identity_key(old), "Old SDK worker survived the service restart")
        require(not current["workers"] and not current["active_run_id"], "Restart automatically resumed an execution")
        require(current["run_count"] == before["run_count"], "Restart automatically added a run")
        require(current["sdk_session_id"] == before["sdk_session_id"], "Restart lost the native session identifier")
        run_id = self.state["run_ids"][0]
        run, trace = self.api.request("GET", f"/runs/{run_id}"), self.trace(run_id)
        assert_terminal(self.args.mode, self.args.case, run, trace, self.state["started_calls"])
        assert_history_preserved(self.state["history_checkpoint"], self.history())
        if self.state.get("approval_id"):
            with closing(sqlite3.connect(self.observer.database.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
                row = database.execute("SELECT status FROM approvals WHERE id=? AND run_id=?", (self.state["approval_id"], run_id)).fetchone()
            require(row and row[0] in {"denied", "expired"}, "Old approval remains pending or was allowed")
        self.assert_no_ticket()
        self.state["after_restart"] = current
        self.state["interrupted_status"] = run["status"]

    def terminal_replay(self):
        run_id = self.state["run_ids"][0]
        trace = self.trace(run_id)
        cursor = trace["events"][-1]["id"] - 1
        with self.api.client.stream("GET", f"runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}) as response:
            require(response.status_code == 200, "Interrupted event replay failed")
            replay = list(parse_sse(response.iter_lines()))
        require(len(replay) == 1 and replay[0]["type"] == "run.completed" and replay[0]["data"]["status"] == EXPECTED_STATUS[self.args.mode], "Interrupted completion was not durably replayed")

    def followup(self):
        run_id = self.send(f"上一项验收已中止，不要补做、不重试任何旧工具，也不要创建工单。这是新的用户消息。此轮不要调用工具，只回复 {self.state['marker']}。")
        deadline = time.monotonic() + self.args.timeout
        while time.monotonic() < deadline:
            run = self.api.request("GET", f"/runs/{run_id}")
            trace = self.trace(run_id)
            require(not started_calls(trace), "Follow-up attempted a tool call instead of the requested text-only answer")
            if run["status"] in TERMINAL:
                require(run["status"] == "succeeded", "User follow-up did not succeed after restart")
                require(any(message["role"] == "assistant" and self.state["marker"] in message["text"] for message in trace["messages"]), "Follow-up answer omitted the requested synthetic marker")
                current = self.observer.snapshot(self.state["conversation_id"])
                require(current["run_count"] == self.state["before_restart"]["run_count"] + 1, "Follow-up created an unexpected number of runs")
                require(current["sdk_session_id"] == self.state["before_restart"]["sdk_session_id"], "Follow-up did not resume the native session")
                original_trace = self.trace(self.state["run_ids"][0])
                assert_terminal(self.args.mode, self.args.case, self.api.request("GET", f"/runs/{self.state['run_ids'][0]}"), original_trace, self.state["started_calls"])
                self.assert_no_ticket()
                self.state["followup"] = {"run_id": run_id, "status": run["status"], "usage": trace.get("usage"), "answer_sha256": digest([item["text"] for item in trace["messages"] if item["role"] == "assistant"])}
                return
            time.sleep(0.4)
        raise CheckFailure("Follow-up timed out")

    def verify(self):
        require(self.state["stage"] == "prepared", "Prepare the selected case before checking restart")
        self.check("restart_terminal_state_history_and_no_replay", self.recovery_assertions)
        # Give an accidental rescheduler a short observation window before any explicit user message.
        time.sleep(2)
        self.check("no_delayed_automatic_execution", self.recovery_assertions)
        if self.state.get("approval_id"):
            self.check("stale_approval_decision_rejected_409", lambda: reject_stale_approval(self.api, self.state["approval_id"]))
        self.check("interrupted_completion_replay", self.terminal_replay)
        self.check("new_user_message_succeeds_without_repeating_action", self.followup)
        self.state.update({"stage": "completed", "status": "passed", "finished_at": now()})
        self.save()

    def cancel_owned_runs(self):
        errors = 0
        for run_id in self.state["run_ids"]:
            try:
                run = self.api.request("GET", f"/runs/{run_id}")
                if run["status"] not in TERMINAL:
                    self.api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
            except Exception:
                errors += 1
        return errors

    def cleanup(self):
        require(self.cancel_owned_runs() == 0, "Could not cancel a test-owned run")
        if self.state.get("conversation_id"):
            self.api.request("PATCH", f"/conversations/{self.state['conversation_id']}", json={"archived": True})
        for kind in ("assistant", "connection"):
            if self.state.get(kind + "_id"):
                self.api.request("DELETE", f"/{kind}s/{self.state[kind + '_id']}", expected=(200, 204))
        self.state["cleanup_at"] = now()
        self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["plan", "prepare", "check", "cleanup"])
    parser.add_argument("--case", choices=list(TARGET_TOOL), default="pending-write")
    parser.add_argument("--mode", choices=list(EXPECTED_STATUS), default="graceful")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--origin", default="http://127.0.0.1:18081")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8767/mcp")
    parser.add_argument("--username", default=os.getenv("SMOKE_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--server-data-dir", type=Path, default=Path(".runtime/data"))
    parser.add_argument("--server-process-file", type=Path, default=Path(".runtime/processes.json"))
    parser.add_argument("--demo-data-dir", type=Path, default=Path(".runtime/demo-mcp"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args(argv)
    stem = f"restart-safety-{args.mode}-{args.case}"
    args.state = args.state or Path(f".runtime/{stem}-state.json")
    args.report = args.report or Path(f".runtime/{stem}-report.json")
    if args.phase == "plan":
        print("One case+mode: prepare (1 real turn), operator restarts isolated API, check (1 real follow-up), cleanup (no model).")
        print("Full matrix: pending-write and slow-read, each under graceful and crash. Four cases / eight real turns total. No requests made.")
        return 0
    if args.phase in {"prepare", "check"} and not args.real_model:
        print("Refusing model phase without --real-model. No requests made.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.ERROR)
    runner = None
    try:
        password = args.password_file.read_text(encoding="utf-8").strip() if args.password_file else os.getenv("SMOKE_ADMIN_PASSWORD", "")
        require(password and args.timeout > 0, "Provide private credentials and a positive timeout")
        runner = RestartSafety(args, password)
        {"prepare": runner.prepare, "check": runner.verify, "cleanup": runner.cleanup}[args.phase]()
        print(f"Phase {args.phase} complete; report: {args.report}")
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        if runner:
            runner.state["status"] = "failed"
            runner.state["stage"] = "failed"
            runner.state["failure"] = {"phase": args.phase, "at": now(), "detail": detail}
            # A failed preparation/check must not leave a newly waiting tool running.
            runner.state["failure_cancellation_errors"] = runner.cancel_owned_runs()
            runner.save()
        print(f"Restart acceptance stopped: {detail}", file=sys.stderr)
        return 1
    finally:
        if runner:
            runner.api.close()


if __name__ == "__main__":
    raise SystemExit(main())
