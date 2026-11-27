#!/usr/bin/env python3
"""Deterministic synthetic Codex App Server peer for host-contract tests.

This script speaks only the native JSON-RPC stdio frames used by the pinned
Codex 0.165.0 app-server contract.  It performs no network I/O, reads no
credential or user configuration, starts no model, and runs no business tool.
It exists so the product host adapter can be tested without the real binary and
without a provider turn.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid

_write_lock = threading.Lock()
_log_lock = threading.Lock()
_log_path = None
_state = {
    "thread_id": None,
    "cwd": None,
    "turns": [],
    "interrupted": False,
}
_interrupt_seen = threading.Event()


def log(event: dict) -> None:
    if not _log_path:
        return
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with _log_lock:
        with open(_log_path, "a", encoding="utf-8") as target:
            target.write(line + "\n")
            target.flush()


def send(value: dict) -> None:
    line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _write_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def result(request_id, value: dict) -> None:
    send({"id": request_id, "result": value})


def error(request_id, code: int, message: str) -> None:
    send({"id": request_id, "error": {"code": code, "message": message}})


def notification(method: str, params: dict) -> None:
    send({"method": method, "params": params})


def current_thread() -> str:
    if not _state["thread_id"]:
        _state["thread_id"] = args.thread_id or ("thr-" + uuid.uuid4().hex)
    return _state["thread_id"]


def turn_record(turn_id: str, status: str) -> dict:
    return {"id": turn_id, "status": status}


def remember(turn_id: str, status: str) -> None:
    _state["turns"] = [turn for turn in _state["turns"] if turn["id"] != turn_id]
    _state["turns"].append(turn_record(turn_id, status))


def emit_completed(thread_id: str, turn_id: str, status: str) -> None:
    remember(turn_id, status)
    notification("turn/completed", {
        "threadId": thread_id,
        "turn": turn_record(turn_id, status),
    })


def turn_scenario(thread_id: str, turn_id: str, scenario: str) -> None:
    def run() -> None:
        try:
            notification("turn/started", {
                "threadId": thread_id,
                "turn": turn_record(turn_id, "inProgress"),
            })
            if scenario == "exit":
                log({"event": "scenario_exit"})
                os._exit(0)
            if scenario in {"approval", "unknown_request"}:
                request_id = 900 if scenario == "approval" else 901
                method = ("item/commandExecution/requestApproval"
                          if scenario == "approval" else "unknown/hostRequest")
                notification_method = method
                send({"id": request_id, "method": notification_method,
                      "params": {"threadId": thread_id, "turnId": turn_id,
                                 "itemId": "item-synthetic", "synthetic": True}})
                # Give the host time to reject the server request before the
                # synthetic turn completes.
                time.sleep(0.2)
            if scenario == "mismatch":
                notification("item/agentMessage/delta", {
                    "threadId": "thr-not-owned", "turnId": turn_id,
                    "itemId": "item-synthetic", "delta": "synthetic",
                })
                time.sleep(1.0)
                return
            if scenario in {"interrupt", "interrupt_never", "reconcile_interrupt"}:
                notification("item/agentMessage/delta", {
                    "threadId": thread_id, "turnId": turn_id,
                    "itemId": "item-synthetic", "delta": "合成",
                })
                started = time.monotonic()
                while time.monotonic() - started < 10:
                    if _interrupt_seen.wait(0.05):
                        break
                if scenario == "interrupt_never":
                    return
                emit_completed(thread_id, turn_id, "interrupted")
                return
            if scenario == "hang":
                notification("item/agentMessage/delta", {
                    "threadId": thread_id, "turnId": turn_id,
                    "itemId": "item-synthetic", "delta": "合成",
                })
                return
            # happy and server-request scenarios
            notification("item/agentMessage/delta", {
                "threadId": thread_id, "turnId": turn_id,
                "itemId": "item-synthetic", "delta": "合成",
            })
            notification("item/agentMessage/delta", {
                "threadId": thread_id, "turnId": turn_id,
                "itemId": "item-synthetic", "delta": "回复",
            })
            emit_completed(thread_id, turn_id, "completed")
        except Exception:
            os._exit(2)

    threading.Thread(target=run, name="fake-codex-scenario", daemon=True).start()


def handle(message: dict) -> None:
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}
    if method is None:
        # Response to a host-facing server request (for example an approval).
        log({"event": "server_request_response", "id": request_id,
             "body": message})
        return
    log({"event": "request", "method": method, "id": request_id, "params": params})
    if method == "initialize":
        result(request_id, {"userAgent": "fake-codex-app-server/0.165.0"})
        return
    if method == "initialized":
        return
    if method == "thread/start":
        thread_id = current_thread()
        _state["cwd"] = params.get("cwd")
        if not _state["cwd"]:
            error(request_id, -32602, "cwd required")
            return
        result(request_id, {"thread": {"id": thread_id, "cwd": _state["cwd"],
                                       "turns": list(_state["turns"])}})
        notification("thread/started", {
            "threadId": thread_id,
            "thread": {"id": thread_id, "cwd": _state["cwd"]},
        })
        return
    if method == "thread/name/set":
        if args.scenario == "name_rejected":
            error(request_id, -32600, "cannot persist thread metadata")
        elif params.get("threadId") != current_thread() or not params.get("name"):
            error(request_id, -32602, "thread identity and name required")
        else:
            result(request_id, {})
        return
    if method == "thread/resume":
        if args.scenario == "resume_missing":
            error(request_id, -32600, "thread not found")
            return
        thread_id = params.get("threadId")
        cwd = params.get("cwd")
        if not isinstance(thread_id, str) or not isinstance(cwd, str):
            error(request_id, -32602, "thread identity required")
            return
        _state["thread_id"] = thread_id
        _state["cwd"] = cwd
        result(request_id, {"thread": {"id": thread_id, "cwd": cwd,
                                       "turns": list(_state["turns"])}})
        return
    if method == "thread/read":
        thread_id = params.get("threadId")
        if thread_id != current_thread():
            error(request_id, -32602, "thread identity mismatch")
            return
        turns = list(_state["turns"])
        if args.scenario == "reconcile_terminal":
            turns = [turn_record(args.turn_id or "turn-reconciled", "completed")]
        elif args.scenario == "reconcile_interrupt":
            status = "interrupted" if _state["interrupted"] else "inProgress"
            turns = [turn_record(args.turn_id or "turn-reconciled", status)]
        result(request_id, {"thread": {
            "id": thread_id,
            "cwd": _state["cwd"] or "",
            "turns": turns,
        }})
        return
    if method == "turn/start":
        thread_id = params.get("threadId")
        if thread_id != current_thread():
            error(request_id, -32602, "thread identity mismatch")
            return
        turn_id = args.turn_id or ("turn-" + uuid.uuid4().hex)
        remember(turn_id, "inProgress")
        result(request_id, {"turn": turn_record(turn_id, "inProgress")})
        turn_scenario(thread_id, turn_id, args.scenario)
        return
    if method == "turn/interrupt":
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        if thread_id != current_thread() or not isinstance(turn_id, str):
            error(request_id, -32602, "turn identity mismatch")
            return
        log({"event": "interrupt", "threadId": thread_id, "turnId": turn_id})
        result(request_id, {})
        if args.scenario in {"interrupt", "reconcile_interrupt"}:
            _state["interrupted"] = True
            _interrupt_seen.set()
        return
    error(request_id, -32601, "unknown client method")


def main() -> None:
    global _log_path
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="happy")
    parser.add_argument("--log", default="")
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--turn-id", default="")
    # Accept and ignore the production argv tail so the same file can be the
    # server-owned executable in API tests.
    parsed, _ = parser.parse_known_args()
    global args
    args = parsed
    _log_path = args.log or None
    if _log_path:
        log({
            "event": "startup",
            "cwd": os.getcwd(),
            "scenario": args.scenario,
            "env": {key: os.environ.get(key) for key in (
                "HOME", "CODEX_HOME", "TMPDIR", "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME", "XDG_DATA_HOME", "PATH")},
            "disallowed_present": [key for key in (
                "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY",
                "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY") if key in os.environ],
        })
        if args.turn_id:
            remember(args.turn_id, "inProgress")
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            log({"event": "malformed"})
            return
        if not isinstance(message, dict):
            log({"event": "malformed"})
            return
        handle(message)
    log({"event": "stdin_closed"})
    _interrupt_seen.set()


if __name__ == "__main__":
    main()
