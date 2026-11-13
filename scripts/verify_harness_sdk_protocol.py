#!/usr/bin/env python3
"""Exercise the pinned upstream Python SDK against a local, fake JSON-RPC peer.

No Harness runtime, tool, provider, or model is launched by this verification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
PINNED_COMMIT = "fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"
LIMITATION = "fake-runtime-only"
PROMPT = "/permission danger-full-access"


def require(condition, description):
    if not condition:
        raise RuntimeError(description)


def fake_peer():
    """A deterministic fixture, not a replacement or emulator for dsh."""
    facts, decisions, pending = {}, {}, None

    def send(frame):
        print(json.dumps({"jsonrpc": "2.0", **frame}), flush=True)

    for line in sys.stdin:
        frame = json.loads(line)
        method, request_id = frame.get("method"), frame.get("id")
        if method == "initialize":
            facts["initialize"] = frame["params"]
            send({"id": request_id, "result": {"serverInfo": {
                "name": "deepseek-harness-sdk-runtime", "version": "fake-runtime-only",
            }}})
        elif method == "session/prompt":
            facts["prompt_method"] = method
            facts["prompt"] = frame["params"]
            pending = request_id
            send({"id": "fake-approval", "method": "approval/request", "params": {
                "sessionId": "smoke-main", "toolName": "unknown_tool", "callId": "fake-call",
            }})
            send({"id": "fake-unknown", "method": "unrecognized/request", "params": {}})
        elif method == "shutdown":
            send({"id": request_id, "result": {}})
            return
        elif method is not None:
            send({"id": request_id, "error": {"code": -32601, "message": "Fake peer rejects unsupported method"}})
        elif request_id in {"fake-approval", "fake-unknown"}:
            decisions[request_id] = frame
            if len(decisions) == 2 and pending is not None:
                # The prompt cannot settle until both host requests are answered.
                facts["decisions"] = decisions
                for session_id in ("unrelated-session", "smoke-main"):
                    send({"method": "session.event", "params": {
                        "sessionId": session_id,
                        "event": {"seq": 0, "time": 0, "type": "smoke/verified", "data": facts},
                    }})
                send({"id": pending, "result": {"messageId": "fake-message-receipt"}})
                pending = None


def source_identity(source, expected_commit):
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL,
    ).strip()
    require(commit == expected_commit, "Upstream commit does not match the reviewed pin")
    package = source / "python/sdk/src/deepseek_harness"
    files = sorted(package.glob("*.py"))
    require(bool(files), "Upstream Python SDK source is missing")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(source).as_posix()
        contents = path.read_bytes()
        committed = subprocess.check_output(
            ["git", "-C", str(source), "show", f"{commit}:{relative}"], stderr=subprocess.DEVNULL,
        )
        require(contents == committed, "Upstream SDK has unreviewed local modifications")
        digest.update(relative.encode("ascii") + b"\0" + contents + b"\0")
    return {"commit": commit, "sdk_source_sha256": digest.hexdigest(), "sdk_source_files": len(files)}


def verify(source, temp_root, expected_commit=PINNED_COMMIT):
    identity = source_identity(source, expected_commit)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source / "python/sdk/src"))
    from deepseek_harness.client import HarnessClient, HarnessConfig

    require(Path(sys.modules[HarnessClient.__module__].__file__).resolve().is_relative_to(source),
            "SDK import did not resolve to the checked upstream source")
    temp_root.mkdir(parents=True, exist_ok=True)
    checks = [{"name": "reviewed_upstream_source", "status": "PASS"}]
    with tempfile.TemporaryDirectory(prefix="harness-sdk-protocol-", dir=temp_root) as directory:
        client = HarnessClient(HarnessConfig(
            cwd=directory, dsh_home=str(Path(directory) / "home"),
            initialize_timeout_seconds=5, request_timeout_seconds=5, shutdown_timeout_seconds=1,
        ), _launch_args=(
            "/usr/bin/env", "-i", str(Path(sys.executable).resolve()), "-I", "-B",
            str(Path(__file__).resolve()), "--fake-peer",
        ))
        failures = []
        closing = threading.Event()

        def handle_requests():
            try:
                while not closing.is_set():
                    incoming = client.next_request()
                    if incoming.method == "approval/request":
                        client.respond(incoming.id, {"outcome": "rejected"})
                    else:
                        client.respond_error(incoming.id, code=-32601, message="Unsupported host request")
            except Exception as exc:
                if not closing.is_set():
                    failures.append(type(exc).__name__)

        worker = None
        try:
            client.start()
            worker = threading.Thread(target=handle_requests, daemon=True)
            worker.start()
            initialized = client.initialize(cwd=directory, provider="fake-provider", model="fake-model",
                                            approval_requests=True)
            require(initialized.serverInfo.version == LIMITATION, "Fake peer identity was not returned")
            with client.subscribe_session_notifications("smoke-main") as subscription:
                receipt = client.session_prompt("smoke-main", [{"type": "text", "text": PROMPT}])
                notification = subscription.next(timeout_seconds=5)
                require(notification.payload["sessionId"] == "smoke-main", "Unrelated session was not filtered")
                facts = notification.payload["event"]["data"]
                require(receipt == "fake-message-receipt", "Prompt receipt was not preserved")
                require(facts["initialize"].get("approvalRequests") is True, "Approval opt-in missing on the wire")
                require(facts["prompt_method"] == "session/prompt", "Prompt used native command dispatch")
                require(facts["prompt"]["contentBlocks"] == [{"type": "text", "text": PROMPT}],
                        "Slash-prefixed input was not preserved verbatim")
                require(facts["decisions"]["fake-approval"].get("result") == {"outcome": "rejected"},
                        "Approval was not denied")
                require(facts["decisions"]["fake-unknown"].get("error", {}).get("code") == -32601,
                        "Unknown host request was not rejected with a protocol error")
                require(not failures, "Host request handler failed")
                for name in (
                    "initialize_explicit_approval_opt_in", "slash_text_uses_session_prompt",
                    "fixture_host_default_denies_approval", "unknown_host_request_returns_error",
                    "host_requests_answered_while_prompt_waits", "upstream_session_subscription_filters_unrelated",
                ):
                    checks.append({"name": name, "status": "PASS"})
        finally:
            closing.set()
            client.close()
            if worker is not None:
                worker.join(timeout=2)
                require(not worker.is_alive(), "Host request handler did not stop")
    checks.append({"name": "fake_peer_and_request_worker_shutdown", "status": "PASS"})
    return {
        "status": "PASS", "scope": LIMITATION, "source": identity, "checks": checks,
        "real_runtime_started": False, "model_calls": 0,
        "limitations": [
            "Verifies the real Python SDK against a simulated peer, not the Node Harness runtime.",
            "Default rejection is implemented by this fixture host, not guaranteed by SDK defaults.",
            "Does not validate production isolation, application migration, plugins, models, or tool execution.",
        ],
    }


def main():
    if sys.argv[1:] == ["--fake-peer"]:
        fake_peer()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".runtime/third_party/deepseek-harness")
    parser.add_argument("--temp-root", type=Path, default=ROOT / ".runtime/tmp")
    args = parser.parse_args()
    try:
        report = verify(args.source.resolve(), args.temp_root.resolve())
    except Exception as exc:
        report = {"status": "FAIL", "scope": LIMITATION, "error_type": type(exc).__name__,
                  "error": str(exc), "real_runtime_started": False, "model_calls": 0}
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
