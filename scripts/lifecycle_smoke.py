"""Phased, provider-opt-in session lifecycle acceptance on the Linux server.

Only HTTP mutates application state. Local SQLite and /proc are observed read-only
to distinguish remembered text from actual process reuse and SDK resume.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import uuid

from smoke_api import API, CheckFailure, TERMINAL, now, parse_sse, require


MODEL_PHASES = {"begin", "after-idle", "after-restart"}


def private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def proc_identity(pid: int, proc: Path = Path("/proc")):
    """PID plus Linux start ticks prevents accidental equality after PID reuse."""
    try:
        text = (proc / str(pid) / "stat").read_text()
        fields = text[text.rfind(")") + 2:].split()
        return {"pid": pid, "start_ticks": int(fields[19]), "parent_pid": int(fields[1])}
    except (OSError, ValueError, IndexError):
        return None


def identity_key(item):
    return item["pid"], item["start_ticks"]


def identity_set(items):
    return {identity_key(item) for item in items}


def verify_transition(before: dict, after: dict, mode: str):
    require(before["sdk_session_id"] == after["sdk_session_id"] and bool(after["sdk_session_id"]), "SDK session ID changed or is missing")
    same_server = identity_key(before["server_process"]) == identity_key(after["server_process"])
    require(same_server if mode != "restart" else not same_server, "Server process identity does not match the required lifecycle transition")
    old, new = identity_set(before["workers"]), identity_set(after["workers"])
    require(bool(old) and bool(new), "SDK worker identity evidence is missing")
    if mode == "warm":
        require(old == new, "Consecutive messages did not reuse the same SDK worker")
    else:
        require(old.isdisjoint(new), "A resumed message reused an old worker instead of a new process")


class Observer:
    def __init__(self, data_dir: Path, process_file: Path):
        self.data_dir = data_dir.resolve()
        self.database = self.data_dir / "app.sqlite3"
        self.process_file = process_file
        require(sys.platform.startswith("linux") and Path("/proc").is_dir(), "Run lifecycle acceptance on the Linux execution server")
        require(self.database.is_file(), "Existing server database is required; the script never creates or modifies it")

    def server(self):
        try:
            pid = json.loads(self.process_file.read_text())["server"]
        except (OSError, ValueError, KeyError):
            raise CheckFailure("Cannot read the existing server PID from the private process record")
        require(type(pid) is int and pid > 1, "Invalid server PID record")
        identity = proc_identity(pid)
        require(identity, "Recorded server process is not alive")
        # Inspect only command arguments, never /proc environ or credential files.
        command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
        require(b"uvicorn" in command and b"app.main:app" in command, "Recorded PID does not identify this product API process")
        return identity

    def workers(self, conversation_id: str, server: dict):
        require(conversation_id.isalnum(), "Unexpected conversation identifier")
        root = (self.data_dir / "workspaces" / conversation_id).resolve()
        identities = {}
        for directory in Path("/proc").iterdir():
            if directory.name.isdecimal():
                identity = proc_identity(int(directory.name))
                if identity:
                    identities[identity["pid"]] = identity
        result = []
        for pid, item in identities.items():
            try:
                if (Path("/proc") / str(pid) / "cwd").resolve(strict=True) != root:
                    continue
                command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
                if b"stream-json" not in command:
                    continue
                ancestor = item["parent_pid"]
                seen = set()
                while ancestor in identities and ancestor not in seen and ancestor != server["pid"]:
                    seen.add(ancestor)
                    ancestor = identities[ancestor]["parent_pid"]
                if ancestor == server["pid"]:
                    result.append(item)
            except OSError:
                continue
        return sorted(result, key=lambda item: item["pid"])

    def snapshot(self, conversation_id: str):
        server = self.server()
        # mode=ro prevents accidental creation or writes. No user/secret/session tables are read.
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            row = db.execute("SELECT sdk_session_id,active_run_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            require(row is not None, "Conversation is absent from the observed server database")
            count = db.execute("SELECT count(*) FROM runs WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
        return {"observed_at": now(), "sdk_session_id": row[0], "active_run_id": row[1], "run_count": count,
                "server_process": server, "workers": self.workers(conversation_id, server)}


class Lifecycle:
    def __init__(self, args, username, password):
        self.args = args
        self.api = API(args.base_url, args.origin)
        self.observer = Observer(args.server_data_dir, args.server_process_file)
        self.state = {"version": 1, "stage": "new", "base_url": self.api.base, "checks": [], "runs": []}
        if args.phase != "begin":
            self.state = json.loads(args.state.read_text(encoding="utf-8"))
            require(self.state.get("version") == 1 and self.state.get("base_url") == self.api.base, "State belongs to a different server or script version")
        else:
            require(not args.state.exists(), "State file already exists; finish/clean up it or choose a new path")
        user = self.api.login(username, password)
        require(user.get("role") == "admin", "This acceptance requires an administrator")
        if self.state.get("user_id"):
            require(self.state["user_id"] == user["id"], "Continue using the same acceptance account")
        self.state["user_id"] = user["id"]

    def save(self):
        private_json(self.args.state, self.state)
        # The state holds a synthetic marker, not credentials. The report omits even that marker.
        report = {key: value for key, value in self.state.items() if key != "marker"}
        report["evidence_boundary"] = "Real text-only session lifecycle; no customer MCP, in-flight write restart or model quality benchmark is established."
        private_json(self.args.report, report)

    def check(self, name, operation):
        start = time.monotonic()
        try:
            result = operation()
        except Exception as exc:
            detail = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
            self.state["checks"].append({"name": name, "status": "failed", "detail": detail, "at": now()})
            self.save()
            raise
        self.state["checks"].append({"name": name, "status": "passed", "seconds": round(time.monotonic() - start, 3), "at": now()})
        self.save()
        print(f"PASS {name}", flush=True)
        return result

    def history(self):
        messages = self.api.request("GET", f"/conversations/{self.state['conversation_id']}/messages")
        return {"message_count": len(messages), "sha256": digest(messages)}

    def turn(self, prompt, recall=True):
        conversation_id = self.state["conversation_id"]
        result = self.api.request("POST", f"/conversations/{conversation_id}/messages", expected=(202,), json={"client_request_id": str(uuid.uuid4()), "text": prompt, "file_ids": []})
        run_id = result["run_id"]
        self.state["runs"].append({"id": run_id, "started_at": now()})
        self.save()
        start = time.monotonic()
        while time.monotonic() - start < self.args.timeout:
            run = self.api.request("GET", f"/runs/{run_id}")
            if run["status"] == "awaiting_approval":
                for approval in run.get("approvals", []):
                    if approval["status"] == "pending":
                        self.api.request("POST", f"/approvals/{approval['id']}/decision", json={"decision": "deny"})
                self.api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
                raise CheckFailure("Unexpected tool approval was denied; lifecycle turns must remain text-only")
            if run["status"] in TERMINAL:
                break
            time.sleep(0.4)
        else:
            self.api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
            raise CheckFailure("Lifecycle model turn timed out and was cancelled")
        require(run["status"] == "succeeded", "Lifecycle model turn did not succeed; inspect protected diagnostics")
        trace = self.api.request("GET", f"/runs/{run_id}/trace")
        require(not any(event["type"] in {"tool.started", "approval.required"} for event in trace["events"]), "A text-only lifecycle turn unexpectedly used a tool")
        answers = [item["text"] for item in trace["messages"] if item["role"] == "assistant"]
        require(answers, "Successful lifecycle turn has no assistant answer")
        if recall:
            require(any(self.state["marker"] in answer for answer in answers), "Reply did not recall the marker from an earlier turn")
        item = self.state["runs"][-1]
        item.update({"status": run["status"], "duration_seconds": round(time.monotonic() - start, 3),
                     "answer_sha256": digest(answers), "usage": trace.get("usage"), "last_event_id": trace["events"][-1]["id"]})
        self.save()
        return self.observer.snapshot(conversation_id)

    def replay_completion(self, run):
        cursor = max(0, run["last_event_id"] - 1)
        with self.api.client.stream("GET", f"runs/{run['id']}/events", headers={"Last-Event-ID": str(cursor)}) as response:
            require(response.status_code == 200, "Persisted event replay failed")
            events = list(parse_sse(response.iter_lines()))
        require(events and events[-1]["type"] == "run.completed" and events[-1]["data"]["status"] == "succeeded", "Completed run was not durably replayable")
        require(all(event["run_id"] == run["id"] and event["id"] > cursor for event in events), "Replay cursor or run ownership is incorrect")

    def begin(self):
        ready = self.api.request("GET", "/health/ready")
        require(ready.get("model_configured"), "Model provider is not configured; no model message was submitted")
        tag = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
        self.state["tag"] = tag
        self.state["marker"] = "LIFECYCLE-" + secrets.token_hex(12)
        assistant = self.api.request("POST", "/assistants", expected=(201, 200), json={"name": f"会话连续性验收 {tag}", "description": "合成标记，仅验证会话连续性。", "instructions": "仅根据对话回答。不要调用任何工具或写入文件。需要回忆校验码时只回复原校验码。", "connections": [], "access": {"all_members": False, "user_ids": []}, "enabled": True})
        self.state["assistant_id"] = assistant["id"]
        self.save()
        conversation = self.api.request("POST", "/conversations", expected=(201, 200), json={"assistant_id": assistant["id"], "title": f"连续性验收 {tag}"})
        self.state["conversation_id"] = conversation["id"]
        self.save()
        first = self.check("seed_synthetic_conversation_marker", lambda: self.turn(f"请记住连续性校验码 {self.state['marker']}。本轮只回复已记住，不要调用工具。", recall=False))
        second = self.check("second_turn_recalls_without_repeating_marker", lambda: self.turn("请只回复我们刚才约定的连续性校验码，不要调用工具。"))
        self.check("same_sdk_session_and_worker_between_turns", lambda: verify_transition(first, second, "warm"))
        self.state.update({"stage": "ready_for_idle", "idle_since_epoch": time.time(), "before_idle": second, "history": self.history()})
        self.save()

    def after_idle(self):
        require(self.state["stage"] == "ready_for_idle", "Run begin before after-idle")
        require(time.time() - self.state["idle_since_epoch"] >= self.args.idle_seconds, "Configured idle interval has not elapsed; no model request was sent")
        before = self.state["before_idle"]
        current = self.observer.snapshot(self.state["conversation_id"])
        require(identity_key(current["server_process"]) == identity_key(before["server_process"]), "Server restarted during idle wait; this does not prove idle recycling")
        require(not current["workers"], "SDK worker is still alive; idle recycling has not occurred")
        for worker in before["workers"]:
            alive = proc_identity(worker["pid"])
            require(not alive or identity_key(alive) != identity_key(worker), "Old SDK worker survived idle recycling outside the expected process tree")
        require(current["run_count"] == before["run_count"] and not current["active_run_id"], "Conversation changed while idle")
        require(self.history() == self.state["history"], "Conversation history changed while idle")
        self.state["idle_recycled_observation"] = current
        after = self.check("recall_after_idle_recycling", lambda: self.turn("请只回复前面约定的连续性校验码，不要调用工具。"))
        self.check("new_worker_resumes_same_sdk_session_after_idle", lambda: verify_transition(before, after, "idle"))
        self.state.update({"stage": "ready_for_restart", "before_restart": after, "history": self.history()})
        self.save()

    def after_restart(self):
        require(self.state["stage"] == "ready_for_restart", "Complete after-idle before restarting")
        before = self.state["before_restart"]
        current = self.observer.snapshot(self.state["conversation_id"])
        require(identity_key(current["server_process"]) != identity_key(before["server_process"]), "API process has not restarted; no model request was sent")
        for worker in before["workers"]:
            alive = proc_identity(worker["pid"])
            require(not alive or identity_key(alive) != identity_key(worker), "An old SDK worker survived the API restart")
        require(not current["workers"] and not current["active_run_id"], "Restart unexpectedly launched or retained a conversation worker")
        require(current["sdk_session_id"] == before["sdk_session_id"] and current["run_count"] == before["run_count"], "Restart lost session identity or automatically added a run")
        self.check("history_persists_without_automatic_replay", lambda: require(self.history() == self.state["history"], "Restart changed persisted conversation messages"))
        self.check("completed_sse_replays_after_restart", lambda: self.replay_completion(self.state["runs"][-1]))
        self.state["restart_observation"] = current
        after = self.check("recall_after_service_restart", lambda: self.turn("请只回复我们此前约定的连续性校验码，不要调用工具。"))
        self.check("new_service_and_worker_resume_same_sdk_session", lambda: verify_transition(before, after, "restart"))
        self.state.update({"stage": "completed", "status": "passed", "finished_at": now()})
        self.save()

    def cleanup(self):
        for run in self.state["runs"]:
            current = self.api.request("GET", f"/runs/{run['id']}")
            if current["status"] not in TERMINAL:
                self.api.request("POST", f"/runs/{run['id']}/cancel", expected=(200, 202))
        if self.state.get("conversation_id"):
            self.api.request("PATCH", f"/conversations/{self.state['conversation_id']}", json={"archived": True})
        if self.state.get("assistant_id"):
            self.api.request("DELETE", f"/assistants/{self.state['assistant_id']}", expected=(200, 204))
        self.state["cleanup_at"] = now()
        self.save()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["plan", "begin", "after-idle", "after-restart", "cleanup"])
    parser.add_argument("--real-model", action="store_true", help="Required for any phase that submits model messages")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--origin", default="http://127.0.0.1:18081")
    parser.add_argument("--username", default=os.getenv("SMOKE_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--server-data-dir", type=Path, default=Path(".runtime/data"))
    parser.add_argument("--server-process-file", type=Path, default=Path(".runtime/processes.json"))
    parser.add_argument("--state", type=Path, default=Path(".runtime/lifecycle-state.json"))
    parser.add_argument("--report", type=Path, default=Path(".runtime/lifecycle-report.json"))
    parser.add_argument("--idle-seconds", type=float, default=600, help="Actual configured AGENT_IDLE_SECONDS; observer also requires the worker to be gone")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args(argv)
    if args.phase == "plan":
        print("begin: 2 real turns; wait actual idle timeout; after-idle: 1 real turn; operator restarts the isolated API; after-restart: 1 real turn; cleanup: no model call.")
        print("Run on Linux with existing private admin credentials, read-only server data, and current server PID record. No requests were made.")
        return 0
    if args.phase in MODEL_PHASES and not args.real_model:
        print("Refusing model phase without --real-model. No requests were made.", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.ERROR)
    lifecycle = None
    try:
        password = args.password_file.read_text(encoding="utf-8").strip() if args.password_file else os.getenv("SMOKE_ADMIN_PASSWORD", "")
        require(bool(password), "Use a private password file or SMOKE_ADMIN_PASSWORD")
        require(args.idle_seconds > 0 and args.timeout > 0, "Timeout values must be positive")
        lifecycle = Lifecycle(args, args.username, password)
        {"begin": lifecycle.begin, "after-idle": lifecycle.after_idle, "after-restart": lifecycle.after_restart, "cleanup": lifecycle.cleanup}[args.phase]()
        print(f"Phase {args.phase} complete; sanitized report: {args.report}")
        return 0
    except Exception as exc:
        detail = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        if lifecycle:
            lifecycle.state["last_failure"] = {"phase": args.phase, "at": now(), "detail": detail}
            lifecycle.save()
        print(f"Lifecycle acceptance stopped: {detail}", file=sys.stderr)
        return 1
    finally:
        if lifecycle:
            lifecycle.api.close()


if __name__ == "__main__":
    raise SystemExit(main())
