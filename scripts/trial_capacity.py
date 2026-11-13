#!/usr/bin/env python3
"""Bounded execution-slot acceptance for the Huawei trial on school cloud.

The runner boots the real FastAPI application on an ephemeral loopback port
with a gated synthetic runtime.  Every accepted execution blocks inside the
runtime until the test releases it, so the configured slots stay occupied while
8 or 25 submissions arrive at the same moment.  That makes the boundary
deterministic: with four slots, exactly four submissions may execute and every
other submission must fail with the explicit ``EXECUTION_CAPACITY`` code.

The report separates accepted executions, exact capacity rejections and
unexpected failures, proves the observed peak simultaneous execution never
exceeds the configured slots, and covers cancellation plus slot recovery.  It
never calls a paid model, opens an external network connection, reads a
production credential, or touches real customer data.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from trial_smoke import (  # noqa: E402
    EXTERNAL_VALIDATION_LIMITS,
    PASSWORD,
    CheckFailure,
    SyntheticTrialServer,
    make_client,
    request_json,
    require,
)

SCHEMA_VERSION = "trial-capacity/v1"
CAPACITY_CODE = "EXECUTION_CAPACITY"
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uuid_hex() -> str:
    return os.urandom(16).hex()


class GatedRuntime:
    """Synthetic runtime that keeps accepted executions active until released.

    The gate is a class-level event shared by every execution in the current
    phase.  Cancellation is per execution: ``interrupt`` only opens that
    execution's stop event, matching the product contract where cancelling one
    run must not disturb the other active runs.
    """

    active = 0
    peak = 0
    started = 0
    release = asyncio.Event()

    def __init__(self, service, conversation_id, config, approve, emit):
        self.service = service
        self.conversation_id = conversation_id
        self.config = config
        self.approve = approve
        self.emit = emit
        self.stopped = asyncio.Event()

    @classmethod
    def reset_counters(cls) -> None:
        cls.active = 0
        cls.peak = 0
        cls.started = 0
        cls.release = asyncio.Event()

    async def connect(self) -> None:
        return None

    async def execute(self, run, message) -> None:
        cls = type(self)
        cls.active += 1
        cls.started += 1
        cls.peak = max(cls.peak, cls.active)
        try:
            release = asyncio.create_task(cls.release.wait())
            stopped = asyncio.create_task(self.stopped.wait())
            done, pending = await asyncio.wait({release, stopped}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
            if self.stopped.is_set():
                raise asyncio.CancelledError()
            reply = "合成容量：已获准执行"
            await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": reply})
            await self.emit(
                "usage.updated",
                {
                    "input_tokens": 8,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "estimated_cost_usd": 0,
                    "currency": "USD",
                    "cost_is_estimate": False,
                    "source": "synthetic_capacity_runtime",
                    "model_call_count": 0,
                },
            )
        finally:
            cls.active -= 1

    async def interrupt(self) -> None:
        self.stopped.set()

    async def close(self) -> None:
        return None


@dataclass(frozen=True)
class CapacityConfig:
    sessions: int = 4
    bursts: tuple[int, ...] = (8, 25)
    timeout: float = 30.0
    classify_timeout: float = 30.0
    data_dir: Path | None = None
    keep_data: bool = False
    # FULL is the deployment and trial-acceptance default.  NORMAL is a
    # diagnostic-only option and must be labelled as such in any evidence.
    sqlite_synchronous: str = "FULL"

    def __post_init__(self) -> None:
        if self.sessions < 1:
            raise ValueError("sessions must be positive")
        bursts = tuple(sorted(dict.fromkeys(int(value) for value in self.bursts)))
        if not bursts or any(value <= self.sessions for value in bursts):
            raise ValueError("every burst must be larger than the configured sessions")
        if self.timeout <= 0 or self.classify_timeout <= 0:
            raise ValueError("timeouts must be positive")
        durability = str(self.sqlite_synchronous).strip().upper()
        if durability not in {"FULL", "NORMAL"}:
            raise ValueError("sqlite_synchronous must be FULL or NORMAL; OFF is not permitted")
        object.__setattr__(self, "bursts", bursts)
        object.__setattr__(self, "sqlite_synchronous", durability)


def _classify_status(run_ok: bool, synchronous_pragma: int | None, shutdown: dict[str, Any]) -> str:
    """Acceptance status from real PRAGMA and clean shutdown, not config strings."""
    if not run_ok or not shutdown.get("ok"):
        return "failed"
    if synchronous_pragma is None:
        return "failed"
    if synchronous_pragma != 2:  # SQLite: 2 == FULL, 1 == NORMAL, 0 == OFF.
        return "diagnostic_pass"
    return "passed"


async def _create_assistant(client: httpx.AsyncClient, timeout: float) -> str:
    payload = await request_json(
        client,
        "POST",
        "/api/v1/assistants",
        expected=(201,),
        timeout=timeout,
        json={
            "name": "合成容量助手",
            "instructions": "只用于学校云容量合成测试。",
            "knowledge_base_ids": [],
            "connections": [],
            "web_search_enabled": False,
        },
    )
    return payload["id"]


async def _create_conversation(client: httpx.AsyncClient, assistant_id: str, timeout: float) -> str:
    payload = await request_json(
        client,
        "POST",
        "/api/v1/conversations",
        expected=(201,),
        timeout=timeout,
        json={"assistant_id": assistant_id, "title": "合成容量会话"},
    )
    return payload["id"]


async def _create_conversations(
    client: httpx.AsyncClient, assistant_id: str, count: int, timeout: float
) -> list[str]:
    conversations = []
    for _ in range(count):
        conversations.append(await _create_conversation(client, assistant_id, timeout))
    return conversations


async def _submit_burst(
    client: httpx.AsyncClient, conversations: list[str], timeout: float
) -> list[dict[str, Any]]:
    """Send every submission at the same moment behind one local barrier."""
    gate = asyncio.Event()

    async def submit(conversation_id: str) -> dict[str, Any]:
        await gate.wait()
        try:
            response = await client.post(
                f"/api/v1/conversations/{conversation_id}/messages",
                json={"client_request_id": uuid_hex(), "text": "容量突发提交", "file_ids": []},
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            return {"conversation_id": conversation_id, "http_status": None, "error": f"{type(exc).__name__}: {exc}"[:200]}
        if response.status_code != 202:
            return {
                "conversation_id": conversation_id,
                "http_status": response.status_code,
                "error": "unexpected submission status",
            }
        return {"conversation_id": conversation_id, "http_status": 202, "run_id": response.json()["run_id"]}

    tasks = [asyncio.create_task(submit(item)) for item in conversations]
    gate.set()
    return await asyncio.gather(*tasks)


async def _classify_run(
    client: httpx.AsyncClient, run_id: str, timeout: float
) -> dict[str, Any]:
    """Wait until a run is either actively executing or terminally rejected."""
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        view = await request_json(client, "GET", f"/api/v1/runs/{run_id}", timeout=timeout)
        last = view
        if view["status"] == "running":
            return {"outcome": "accepted", "status": view["status"], "error_code": view.get("error_code")}
        if view["status"] in TERMINAL:
            if view["status"] == "failed" and view.get("error_code") == CAPACITY_CODE:
                return {"outcome": "rejected", "status": view["status"], "error_code": view["error_code"]}
            return {
                "outcome": "unexpected",
                "status": view["status"],
                "error_code": view.get("error_code"),
                "error": view.get("error"),
            }
        await asyncio.sleep(0.01)
    return {
        "outcome": "timeout",
        "status": (last or {}).get("status"),
        "error_code": (last or {}).get("error_code"),
    }


async def _classify_all(
    client: httpx.AsyncClient, submissions: list[dict[str, Any]], timeout: float
) -> list[dict[str, Any]]:
    async def classify(item: dict[str, Any]) -> dict[str, Any]:
        if not item.get("run_id"):
            return {**item, "outcome": "http_error"}
        result = await _classify_run(client, item["run_id"], timeout)
        return {**item, **result}

    return await asyncio.gather(*(classify(item) for item in submissions))


async def _wait_terminal(
    client: httpx.AsyncClient, run_id: str, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await request_json(client, "GET", f"/api/v1/runs/{run_id}", timeout=timeout)
        if last["status"] in TERMINAL:
            return last
        await asyncio.sleep(0.01)
    raise CheckFailure(f"run did not reach a terminal state within {timeout:g}s")


async def _wait_active(expected: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if GatedRuntime.active == expected:
            return GatedRuntime.active
        await asyncio.sleep(0.005)
    return GatedRuntime.active


def _tally(classified: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [item for item in classified if item["outcome"] == "accepted"]
    rejected = [item for item in classified if item["outcome"] == "rejected"]
    unexpected = [item for item in classified if item["outcome"] not in {"accepted", "rejected"}]
    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "unexpected": len(unexpected),
        "unexpected_detail": unexpected,
        "accepted_run_ids": [item["run_id"] for item in accepted],
        "rejected_run_ids": [item["run_id"] for item in rejected],
    }


async def _burst_phase(
    client: httpx.AsyncClient,
    assistant_id: str,
    burst: int,
    config: CapacityConfig,
    phase: str,
    phases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Submit ``burst`` runnable requests while the slots stay occupied."""
    GatedRuntime.reset_counters()
    conversations = await _create_conversations(client, assistant_id, burst, config.timeout)
    started = time.perf_counter()
    submissions = await _submit_burst(client, conversations, config.timeout)
    classified = await _classify_all(client, submissions, config.classify_timeout)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    tally = _tally(classified)
    result = {
        "phase": phase,
        "burst": burst,
        "configured_sessions": config.sessions,
        "duration_ms": elapsed,
        **tally,
        "peak_simultaneous_executions": GatedRuntime.peak,
        "observed_active": GatedRuntime.active,
        "runtime_started": GatedRuntime.started,
        "submissions": classified,
    }
    # Save the full tally before asserting so a failed burst still leaves the
    # accepted/rejected/unexpected detail in the report instead of an empty
    # phases list.
    phases.append(result)
    require(
        tally["accepted"] == config.sessions,
        f"{phase}: expected exactly {config.sessions} accepted executions, observed {tally['accepted']}",
    )
    require(
        tally["rejected"] == burst - config.sessions,
        f"{phase}: expected exactly {burst - config.sessions} capacity rejections, observed {tally['rejected']}",
    )
    require(tally["unexpected"] == 0, f"{phase}: {tally['unexpected']} submission(s) failed for an unexpected reason")
    require(
        GatedRuntime.peak == config.sessions,
        f"{phase}: peak simultaneous executions was {GatedRuntime.peak}, expected {config.sessions}",
    )
    require(
        GatedRuntime.started == config.sessions,
        f"{phase}: runtime started {GatedRuntime.started} executions, expected {config.sessions}",
    )
    return result


async def _cancel_and_recover(
    client: httpx.AsyncClient,
    assistant_id: str,
    phase: dict[str, Any],
    config: CapacityConfig,
    phases: list[dict[str, Any]],
) -> dict[str, Any]:
    """Cancel one active run, replace it, then release every remaining run."""
    record: dict[str, Any] = {
        "phase": "cancel_and_recover",
        "cancelled_run": phase["accepted_run_ids"][0] if phase["accepted_run_ids"] else None,
        "status": "started",
    }
    phases.append(record)
    accepted = phase["accepted_run_ids"]
    cancel_target = accepted[0]
    response = await client.post(f"/api/v1/runs/{cancel_target}/cancel", timeout=config.timeout)
    require(response.status_code == 200, f"cancel returned HTTP {response.status_code}")
    cancelled = await _wait_terminal(client, cancel_target, config.classify_timeout)
    require(cancelled["status"] == "cancelled", f"cancelled run ended as {cancelled['status']}")
    active_after_cancel = await _wait_active(config.sessions - 1, config.classify_timeout)
    require(
        active_after_cancel == config.sessions - 1,
        f"cancellation left {active_after_cancel} active executions, expected {config.sessions - 1}",
    )

    # A replacement must reuse the freed slot immediately, without waiting for
    # the cancelled actor's idle timeout.
    replacement = (await _create_conversations(client, assistant_id, 1, config.timeout))[0]
    submissions = await _submit_burst(client, [replacement], config.timeout)
    classified = await _classify_all(client, submissions, config.classify_timeout)
    tally = _tally(classified)
    require(tally["accepted"] == 1, "replacement submission was not accepted after cancellation")
    active = await _wait_active(config.sessions, config.classify_timeout)
    require(active == config.sessions, f"slot was not reused: active={active}")

    # Release the gate: every remaining execution must complete successfully.
    GatedRuntime.release.set()
    outcomes = []
    for run_id in accepted:
        if run_id == cancel_target:
            outcomes.append({"run_id": run_id, "status": "cancelled"})
            continue
        terminal = await _wait_terminal(client, run_id, config.classify_timeout)
        require(terminal["status"] == "succeeded", f"released run ended as {terminal['status']}")
        outcomes.append({"run_id": run_id, "status": terminal["status"]})
    terminal = await _wait_terminal(client, classified[0]["run_id"], config.classify_timeout)
    require(terminal["status"] == "succeeded", f"replacement run ended as {terminal['status']}")
    outcomes.append({"run_id": classified[0]["run_id"], "status": terminal["status"]})
    active_end = await _wait_active(0, config.classify_timeout)
    require(active_end == 0, f"{active_end} executions were still active after release")
    record.update(
        {
            "cancelled_run": cancel_target,
            "active_after_cancel": active_after_cancel,
            "replacement_run": classified[0]["run_id"],
            "accepted": 1,
            "rejected": 0,
            "unexpected": 0,
            "slot_reused": True,
            "peak_simultaneous_executions": GatedRuntime.peak,
            "outcomes": outcomes,
            "status": "passed",
        }
    )
    return record


async def _recovery_after_release(
    client: httpx.AsyncClient,
    assistant_id: str,
    config: CapacityConfig,
    phases: list[dict[str, Any]],
) -> dict[str, Any]:
    """After every slot is free, a fresh bounded burst must execute normally."""
    record: dict[str, Any] = {"phase": "recovery_after_release", "sessions": config.sessions, "status": "started"}
    phases.append(record)
    GatedRuntime.reset_counters()
    GatedRuntime.release.set()
    conversations = await _create_conversations(client, assistant_id, config.sessions, config.timeout)
    submissions = await _submit_burst(client, conversations, config.timeout)
    require(all(item.get("run_id") for item in submissions), "recovery submission was not accepted at HTTP level")
    outcomes = []
    for item in submissions:
        terminal = await _wait_terminal(client, item["run_id"], config.classify_timeout)
        require(terminal["status"] == "succeeded", f"recovery run ended as {terminal['status']}")
        outcomes.append({"run_id": item["run_id"], "status": terminal["status"]})
    require(GatedRuntime.started == config.sessions, "recovery did not execute every accepted run")
    require(GatedRuntime.peak <= config.sessions, f"recovery peak {GatedRuntime.peak} exceeded {config.sessions}")
    record.update(
        {
            "succeeded": len(outcomes),
            "accepted": len(outcomes),
            "rejected": 0,
            "unexpected": 0,
            "peak_simultaneous_executions": GatedRuntime.peak,
            "outcomes": outcomes,
            "status": "passed",
        }
    )
    return record


async def run_capacity(config: CapacityConfig | None = None) -> dict[str, Any]:
    config = config or CapacityConfig()
    temp_owned = config.data_dir is None
    data_dir = Path(config.data_dir) if config.data_dir else Path(tempfile.mkdtemp(prefix="trial-capacity-"))
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now(),
        "synthetic": True,
        "mode": "synthetic-loopback-http-gated",
        "command": f"python3 {Path(__file__).relative_to(ROOT)}",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "uvicorn": uvicorn.__version__,
            "httpx": httpx.__version__,
            "sqlite_synchronous": config.sqlite_synchronous,
            "acceptance_durability": config.sqlite_synchronous == "FULL",
            "note": "FULL 为部署与验收默认；NORMAL 仅用于定位 fsync 成本，不作为门禁证据。",
        },
        "config": {
            "sessions": config.sessions,
            "bursts": list(config.bursts),
            "timeout_seconds": config.timeout,
            "classify_timeout_seconds": config.classify_timeout,
        },
        "phases": [],
        "external_validation_limits": list(EXTERNAL_VALIDATION_LIMITS),
    }
    server: SyntheticTrialServer | None = None
    run_ok = False
    error: str | None = None
    synchronous_pragma: int | None = None
    shutdown: dict[str, Any] = {
        "attempted": False,
        "ok": False,
        "error": None,
        "active_drained": None,
        "residual_actors": None,
        "residual_retiring": None,
        "server_task_done": None,
    }
    try:
        server = await SyntheticTrialServer.start(
            data_dir,
            max_active_sessions=config.sessions,
            idle_seconds=60,
            runtime_factory=GatedRuntime,
            sqlite_synchronous=config.sqlite_synchronous,
        )
        server.create_admin()
        service = server.app.state.service
        synchronous_pragma = int(service.db.conn.execute("PRAGMA synchronous").fetchone()[0])
        journal_mode = service.db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        report["environment"]["synchronous_pragma"] = synchronous_pragma
        report["environment"]["journal_mode"] = journal_mode
        report["environment"]["acceptance_durability"] = synchronous_pragma == 2
        requested_pragma = 2 if config.sqlite_synchronous == "FULL" else 1
        require(
            synchronous_pragma == requested_pragma,
            f"SQLite PRAGMA synchronous is {synchronous_pragma}, expected {requested_pragma} for {config.sqlite_synchronous}",
        )
        async with make_client(server.base_url, timeout=config.timeout, max_connections=max(config.bursts) * 2) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "synthetic-admin", "password": PASSWORD},
                timeout=config.timeout,
            )
            require(login.status_code == 200, f"capacity login returned HTTP {login.status_code}")
            assistant_id = await _create_assistant(client, config.timeout)
            for index, burst in enumerate(config.bursts):
                phase = await _burst_phase(
                    client, assistant_id, burst, config, f"burst_{burst}", report["phases"]
                )
                if index == 0:
                    await _cancel_and_recover(client, assistant_id, phase, config, report["phases"])
                # Ensure every accepted execution is finished before the next
                # burst re-arms the gate.
                GatedRuntime.release.set()
                for run_id in phase["accepted_run_ids"]:
                    await _wait_terminal(client, run_id, config.classify_timeout)
                await _wait_active(0, config.classify_timeout)
            await _recovery_after_release(client, assistant_id, config, report["phases"])
            run_ok = True
    except Exception as exc:  # Keep a failing run reproducible instead of aborting silently.
        error = f"{type(exc).__name__}: {str(exc)[:400]}"
    finally:
        # A failure can leave accepted executions blocked on the gate.  Open
        # it and give them a moment to write their terminal state before the
        # lifespan closes the database, so a shutdown never masks the real
        # failure with "Cannot operate on a closed database".
        GatedRuntime.release.set()
        if server is not None:
            shutdown["attempted"] = True
            deadline = time.monotonic() + 5
            while GatedRuntime.active and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            shutdown["active_drained"] = GatedRuntime.active == 0
            try:
                await server.stop()
            except BaseException as exc:  # Record a failed stop; never swallow it into a pass.
                shutdown["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            try:
                manager = server.app.state.service.manager
                shutdown["residual_actors"] = len(manager.actors)
                shutdown["residual_retiring"] = len(manager.retiring)
            except Exception as exc:
                shutdown["residual_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            shutdown["server_task_done"] = server.task.done()
            shutdown["ok"] = (
                shutdown["error"] is None
                and shutdown["active_drained"]
                and shutdown["residual_actors"] == 0
                and shutdown["residual_retiring"] == 0
                and shutdown["server_task_done"] is True
            )
        report["shutdown"] = shutdown
        status = _classify_status(run_ok, synchronous_pragma, shutdown)
        if status == "failed" and error is None:
            error = "阶段断言或关停校验未通过" if not run_ok else "关停未干净完成"
        report["status"] = status
        if error:
            report["error"] = error
        report["summary"] = {
            "status": status,
            "phases": len(report["phases"]),
            "accepted": sum(int(item.get("accepted") or 0) for item in report["phases"]),
            "rejected": sum(int(item.get("rejected") or 0) for item in report["phases"]),
            "unexpected": sum(int(item.get("unexpected") or 0) for item in report["phases"]),
            "peak_simultaneous_executions": max(
                [int(item.get("peak_simultaneous_executions") or 0) for item in report["phases"]] or [0]
            ),
            "synchronous_pragma": synchronous_pragma,
            "shutdown_ok": shutdown["ok"],
        }
        report["data_dir"] = str(data_dir)
        keep = config.keep_data or status == "failed"
        if temp_owned and not keep:
            shutil.rmtree(data_dir, ignore_errors=True)
            report["data_dir_retained"] = False
        else:
            report["data_dir_retained"] = True
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the synthetic bounded execution-slot capacity acceptance.")
    parser.add_argument("--sessions", type=int, default=4, help="Configured active execution slots.")
    parser.add_argument("--bursts", default="8,25", help="Comma-separated simultaneous submission counts.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request HTTP timeout in seconds.")
    parser.add_argument("--classify-timeout", type=float, default=30.0, help="Terminal-state observation timeout.")
    parser.add_argument("--data-dir", type=Path, help="Use this data directory instead of an owned temporary directory.")
    parser.add_argument("--keep-data", action="store_true", help="Keep the data directory even when the run passes.")
    parser.add_argument("--sqlite-synchronous", default="FULL", choices=("FULL", "NORMAL"),
                        help="FULL is the acceptance default; NORMAL is diagnostic-only and reports diagnostic_pass.")
    parser.add_argument("--json-out", type=Path, help="Write the full JSON report to this path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bursts = tuple(int(value.strip()) for value in args.bursts.split(",") if value.strip())
        config = CapacityConfig(
            sessions=args.sessions,
            bursts=bursts,
            timeout=args.timeout,
            classify_timeout=args.classify_timeout,
            data_dir=args.data_dir,
            keep_data=args.keep_data,
            sqlite_synchronous=args.sqlite_synchronous,
        )
        report = asyncio.run(run_capacity(config))
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False))
        return 2
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "summary": report["summary"],
                "data_dir": report["data_dir"],
                "data_dir_retained": report["data_dir_retained"],
                "json_out": str(args.json_out) if args.json_out else None,
                "error": report.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
