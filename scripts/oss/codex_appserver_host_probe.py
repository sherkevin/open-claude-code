#!/usr/bin/env python3
"""Standalone synthetic acceptance probe for the Codex App Server host slice.

Runs the product host against the repository's deterministic fake stdio peer.
It never starts the real Codex binary, selects a model, reads a login or sends
an external request.  With ``--binary`` it only verifies the pinned executable
digest and reports that the real metadata contract remains the separate
bubblewrap probe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "server"))

from app.codex_appserver import (  # noqa: E402
    CodexHost, CodexHostConfig, CodexHostError, CodexHostUnavailable,
)

FAKE = Path(__file__).resolve().with_name("codex_appserver_fake_peer.py")
USER = "user-probe-000000000001"
CID = "conv-probe-000000000001"
PINNED_SHA256 = "c06a67fd8ac76508dc78bd99ce20c9f317b8d568beda59485f3f4134df9962d5"


def host(root: Path, scenario: str, *, turn_id: str = "", turn_timeout: float = 5.0):
    log = root / f"{scenario}-{time.time_ns()}.jsonl"
    command = (sys.executable, str(FAKE), "app-server", "--stdio",
               "--scenario", scenario, "--log", str(log))
    if turn_id:
        command += ("--turn-id", turn_id)
    return CodexHost(CodexHostConfig(
        enabled=True,
        command=command,
        data_dir=root,
        response_timeout_seconds=2.0,
        turn_timeout_seconds=turn_timeout,
        reconcile_timeout_seconds=3.0,
    )), log


def events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    output = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            output.append(json.loads(line))
        except ValueError:
            continue
    return output


def wait(predicate, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise RuntimeError("probe event timeout")


def run(root: Path) -> list[str]:
    checks: list[str] = []

    happy, _ = host(root, "happy")
    assert happy.ensure_thread(USER, CID)["bound"] is True
    receipt = happy.run_turn(USER, CID, "request-0001", "合成请求")
    assert receipt["status"] == "completed" and receipt["text"] == "合成回复"
    replay = happy.run_turn(USER, CID, "request-0001", "合成请求")
    assert replay["replayed"] is True
    checks.append("owner thread start and bound turn receipt")

    approval, approval_log = host(root, "approval")
    approval.ensure_thread(USER + "-a", CID)
    approved = approval.run_turn(USER + "-a", CID, "request-0001", "合成请求")
    assert approved["rejected_server_requests"] >= 1
    response = wait(lambda: next((event for event in events(approval_log)
                                  if event.get("event") == "server_request_response"
                                  and event.get("id") == 900), None))
    assert response["body"]["error"]["code"] == -32601
    checks.append("known approval request rejected without a grant")

    unknown, unknown_log = host(root, "unknown_request")
    unknown.ensure_thread(USER + "-u", CID)
    unknown.run_turn(USER + "-u", CID, "request-0001", "合成请求")
    response = wait(lambda: next((event for event in events(unknown_log)
                                  if event.get("event") == "server_request_response"
                                  and event.get("id") == 901), None))
    assert response["body"]["error"]["code"] == -32601
    checks.append("unknown server request rejected")

    mismatch, _ = host(root, "mismatch")
    mismatch.ensure_thread(USER + "-m", CID)
    try:
        mismatch.run_turn(USER + "-m", CID, "request-0001", "合成请求")
    except CodexHostUnavailable:
        pass
    else:
        raise AssertionError("mismatched notification did not fail closed")
    assert mismatch.status(USER + "-m", CID)["recovery_required"] is True
    checks.append("cross-thread notification fails closed")

    hang, _ = host(root, "hang", turn_timeout=0.4)
    hang.ensure_thread(USER + "-h", CID)
    try:
        hang.run_turn(USER + "-h", CID, "request-0001", "合成请求")
    except CodexHostUnavailable:
        pass
    else:
        raise AssertionError("turn timeout did not fail closed")
    assert hang.status(USER + "-h", CID)["recovery_required"] is True
    checks.append("turn timeout keeps an uncertain durable marker")

    interrupting, interrupt_log = host(root, "interrupt")
    interrupting.ensure_thread(USER + "-i", CID)
    outcome: dict = {}

    def turn() -> None:
        try:
            outcome["receipt"] = interrupting.run_turn(
                USER + "-i", CID, "request-0001", "合成请求")
        except BaseException as exc:  # pragma: no cover
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=turn)
    worker.start()
    active = wait(lambda: (interrupting.status(USER + "-i", CID)["active_turn"]
                           or {}).get("turn_id"))
    result = interrupting.interrupt(USER + "-i", CID, active)
    assert result["status"] == "interrupt_requested"
    worker.join(timeout=10)
    assert not worker.is_alive() and "error" not in outcome
    assert outcome["receipt"]["status"] == "interrupted"
    wait(lambda: any(event.get("event") == "interrupt" for event in events(interrupt_log)))
    checks.append("owned turn interrupt reaches the exact thread and turn")

    first, _ = host(root, "happy")
    thread_id = first.ensure_thread(USER + "-r", CID)["thread_id"]
    first.run_turn(USER + "-r", CID, "request-0001", "第一轮")
    second, second_log = host(root, "happy")
    assert second.ensure_thread(USER + "-r", CID)["thread_id"] == thread_id
    resumed = wait(lambda: [event for event in events(second_log)
                            if event.get("event") == "request"
                            and event.get("method") == "thread/resume"])
    assert resumed[0]["params"]["threadId"] == thread_id
    assert not [event for event in events(second_log)
                if event.get("event") == "request"
                and event.get("method") == "thread/start"]
    checks.append("restart resumes the same thread instead of creating another")

    crashing, _ = host(root, "exit")
    crashing.ensure_thread(USER + "-c", CID)
    try:
        crashing.run_turn(USER + "-c", CID, "request-0001", "合成请求")
    except CodexHostError:
        pass
    else:
        raise AssertionError("synthetic crash did not fence the turn")
    crashed = crashing.status(USER + "-c", CID)
    assert crashed["recovery_required"] is True
    active = crashed["active_turn"]["turn_id"]
    recovered, _ = host(root, "reconcile_terminal", turn_id=active)
    view = recovered.reconcile(USER + "-c", CID)
    assert view["recovery_required"] is False
    assert view["last_turn"]["status"] == "completed"
    checks.append("explicit reconcile clears a terminal crashed turn without replay")

    return checks


def verify_binary(path: Path, expected: str) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if actual != expected:
        raise ValueError("binary digest mismatch")
    return {"path": str(path), "sha256": actual, "metadata_only": True,
            "note": "real stdio execution remains scripts/oss/codex_appserver_stdio_probe.py"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--binary-sha256", default=PINNED_SHA256)
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(Path("/media/data3")):
        raise SystemExit("--root must be on /media/data3")
    root.mkdir(mode=0o700, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="codex-host-probe-", dir=root))
    summary = {"run_dir": str(run_dir), "checks": [], "binary": None}
    try:
        summary["checks"] = run(run_dir)
        if args.binary is not None:
            summary["binary"] = verify_binary(args.binary, args.binary_sha256)
        summary["passed"] = len(summary["checks"])
        summary["ok"] = True
    except Exception as exc:
        summary.update(ok=False, error=type(exc).__name__, detail=str(exc))
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
