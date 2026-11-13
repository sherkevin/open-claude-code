#!/usr/bin/env python3
"""Layered synthetic concurrency load foundation for the Huawei trial.

The runner boots the real FastAPI application on an ephemeral loopback port
with an injected synthetic runtime.  It measures five independent layers at
1, 5, 10 and 25 concurrent workers: chat completion, SSE streaming, knowledge
retrieval, attachment round-trip, and cancellation.  Every request uses
synthetic resources; no paid model, external network, customer data, shared
service, or shared dashboard is touched.

Reported metrics per scenario and concurrency level: request count, success,
error codes, timeouts, cancellations, p50/p95/p99 latency, SSE first byte,
throughput, and process resource peaks (RSS, threads, file descriptors,
active session actors).

The numbers are an application-logic and resource foundation only.  They are
not a production capacity certification: the server, generator and SQLite
database share one process and one event loop.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import platform
import shlex
import sys
import time
from collections import Counter
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
    CheckFailure,
    EXTERNAL_VALIDATION_LIMITS,
    SYNTHETIC_ATTACHMENT,
    SYNTHETIC_QUERY,
    SyntheticRuntime,
    SyntheticTrialServer,
    event_types,
    make_client,
    request_json,
    require,
    sha256_bytes,
    sse_payloads,
    stream_run_events,
    wait_run,
)

SCHEMA_VERSION = "concurrency-load/v1"
DEFAULT_SCENARIOS = ("chat", "sse", "retrieval", "attachment", "cancel")
CONVERSATION_SCENARIOS = {"chat", "sse", "cancel"}
EXECUTION_SCENARIOS = {"chat", "sse", "cancel"}
CAPACITY_CODE = "EXECUTION_CAPACITY"
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolation percentile; returns None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 3)
    rank = (len(ordered) - 1) * fraction
    lower, upper = int(rank), min(int(rank) + 1, len(ordered) - 1)
    weight = rank - lower
    return round(float(ordered[lower]) * (1 - weight) + float(ordered[upper]) * weight, 3)


def latency_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 3),
    }


@dataclass(frozen=True)
class LoadConfig:
    levels: tuple[int, ...] = (1, 5, 10, 25)
    iterations: int = 2
    scenarios: tuple[str, ...] = DEFAULT_SCENARIOS
    timeout: float = 15.0
    sample_interval: float = 0.05
    # Deployment default, deliberately independent of the matrix levels.  A
    # higher concurrency level never raises this value.
    max_active_sessions: int = 4
    execution_hold_ms: int = 350
    data_dir: Path | None = None
    keep_data: bool = False
    seed: int = 20261112
    # FULL is the deployment and trial-acceptance default.  NORMAL is a
    # diagnostic-only option and must be labelled as such in any evidence.
    sqlite_synchronous: str = "FULL"

    def __post_init__(self) -> None:
        levels = tuple(sorted(dict.fromkeys(int(value) for value in self.levels)))
        if not levels or any(value < 1 for value in levels):
            raise ValueError("concurrency levels must be positive")
        scenarios = tuple(dict.fromkeys(self.scenarios))
        unknown = set(scenarios) - set(DEFAULT_SCENARIOS)
        if not scenarios or unknown:
            raise ValueError(f"scenarios must be a nonempty subset of {DEFAULT_SCENARIOS}")
        if self.iterations < 1:
            raise ValueError("iterations must be at least 1")
        if self.timeout <= 0 or self.sample_interval <= 0:
            raise ValueError("timeout and sample interval must be positive")
        if self.max_active_sessions < 1:
            raise ValueError("max_active_sessions must be positive")
        if self.execution_hold_ms < 0:
            raise ValueError("execution_hold_ms must not be negative")
        object.__setattr__(self, "levels", levels)
        object.__setattr__(self, "scenarios", scenarios)


class MatrixRuntime(SyntheticRuntime):
    """Synthetic runtime that holds newly admitted execution slots briefly.

    The hold makes the four-slot boundary observable in the same real HTTP
    matrix without turning accepted requests into a fixed-capacity gate.  It
    still performs only local deterministic work and never calls a provider.
    """

    hold_seconds = 0.35

    async def execute(self, run, message) -> None:
        await asyncio.sleep(type(self).hold_seconds)
        await super().execute(run, message)


class ResourceSampler:
    """Sample process-wide resource use while one matrix cell is running."""

    def __init__(self, app, interval: float = 0.05):
        self.app = app
        self.interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.rss_start = self._rss()
        self.rss_peak = self.rss_start
        self.threads_peak = self._threads()
        self.fds_peak = self._fds()
        self.actors_peak = self._actors()
        self.vm_hwm_end: int | None = None

    @staticmethod
    def _status_value(key: str) -> int | None:
        try:
            for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
                if line.startswith(key + ":"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
        return None

    def _rss(self) -> int | None:
        return self._status_value("VmRSS")

    @staticmethod
    def _threads() -> int | None:
        try:
            return int(Path("/proc/self/status").read_text(encoding="utf-8").split("Threads:")[1].split()[0])
        except (OSError, ValueError, IndexError):
            return None

    @staticmethod
    def _fds() -> int | None:
        try:
            return len(os.listdir("/proc/self/fd"))
        except OSError:
            return None

    def _actors(self) -> int | None:
        try:
            return len(self.app.state.manager.actors)
        except Exception:
            return None

    def _sample(self) -> None:
        rss = self._rss()
        if rss is not None:
            self.rss_peak = rss if self.rss_peak is None else max(self.rss_peak, rss)
        threads = self._threads()
        if threads is not None:
            self.threads_peak = threads if self.threads_peak is None else max(self.threads_peak, threads)
        fds = self._fds()
        if fds is not None:
            self.fds_peak = fds if self.fds_peak is None else max(self.fds_peak, fds)
        actors = self._actors()
        if actors is not None:
            self.actors_peak = actors if self.actors_peak is None else max(self.actors_peak, actors)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._sample()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    async def __aenter__(self) -> "ResourceSampler":
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._sample()
        self.vm_hwm_end = self._status_value("VmHWM")

    def as_dict(self) -> dict[str, Any]:
        return {
            "rss_start_bytes": self.rss_start,
            "rss_peak_bytes": self.rss_peak,
            "rss_end_bytes": self._rss(),
            "vm_hwm_bytes": self.vm_hwm_end or self._status_value("VmHWM"),
            "threads_peak": self.threads_peak,
            "fds_peak": self.fds_peak,
            "active_actors_peak": self.actors_peak,
        }


async def _create_conversation(client: httpx.AsyncClient, assistant_id: str, timeout: float) -> str:
    payload = await request_json(
        client,
        "POST",
        "/api/v1/conversations",
        expected=(201,),
        timeout=timeout,
        json={"assistant_id": assistant_id, "title": "合成并发压测"},
    )
    return payload["id"]


async def _chat_once(client: httpx.AsyncClient, conversation_id: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    sent = await request_json(
        client,
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages",
        expected=(202,),
        timeout=timeout,
        json={"client_request_id": uuid_hex(), "text": "合成并发：普通聊天", "file_ids": []},
    )
    run = await wait_run(client, sent["run_id"], timeout)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    if run["status"] == "failed" and run.get("error_code") == CAPACITY_CODE:
        return {
            "ok": True,
            "capacity_rejected": True,
            "run_id": run["id"],
            "latency_ms": elapsed,
            "http_status": 202,
            "error_code": CAPACITY_CODE,
        }
    require(run["status"] == "succeeded", f"chat run ended as {run['status']}")
    messages = await request_json(client, "GET", f"/api/v1/conversations/{conversation_id}/messages", timeout=timeout)
    require(
        any(item["run_id"] == run["id"] and item["role"] == "assistant" and "合成回复：" in item["text"] for item in messages),
        "chat assistant message is missing",
    )
    return {"ok": True, "completed": True, "run_id": run["id"], "latency_ms": elapsed, "http_status": 202}


async def _sse_once(client: httpx.AsyncClient, conversation_id: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    sent = await request_json(
        client,
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages",
        expected=(202,),
        timeout=timeout,
        json={"client_request_id": uuid_hex(), "text": "合成并发：SSE 事件流", "file_ids": []},
    )
    stream_started = time.perf_counter()
    stream = await stream_run_events(client, sent["run_id"], timeout)
    total = round((time.perf_counter() - started) * 1000, 3)
    first_byte = round((stream_started - started) * 1000 + stream["first_byte_ms"], 3)
    types = event_types(stream)
    completed = sse_payloads(stream, "run.completed")
    if completed:
        terminal = completed[-1].get("data", {})
        if terminal.get("status") == "failed" and terminal.get("error_code") == CAPACITY_CODE:
            require(types.count("run.completed") == 1, "capacity-rejected SSE stream has duplicate terminal events")
            return {
                "ok": True,
                "capacity_rejected": True,
                "run_id": sent["run_id"],
                "latency_ms": total,
                "first_byte_ms": first_byte,
                "http_status": 202,
                "error_code": CAPACITY_CODE,
                "reconnects": max(
                    0,
                    stream.get("connections", 1) - 1 - int(stream.get("stale_connection_retries", 0)),
                ),
                "connection_retries": int(stream.get("stale_connection_retries", 0)),
                "unknown_transport_retries": int(stream.get("unknown_transport_retries", 0)),
                "transport_failures": int(stream.get("transport_failures", 0)),
                "transport_attempts": stream.get("transport_attempts", []),
            }
    require("run.started" in types and "run.completed" in types, "SSE stream missed lifecycle events")
    require(completed and completed[-1].get("data", {}).get("status") == "succeeded", "SSE completion status mismatch")
    require(types.count("run.completed") == 1, "SSE stream contains duplicate terminal events")
    return {
        "ok": True,
        "completed": True,
        "run_id": sent["run_id"],
        "latency_ms": total,
        "first_byte_ms": first_byte,
        "http_status": 202,
        "reconnects": max(
            0,
            stream.get("connections", 1) - 1 - int(stream.get("stale_connection_retries", 0)),
        ),
        "connection_retries": int(stream.get("stale_connection_retries", 0)),
        "unknown_transport_retries": int(stream.get("unknown_transport_retries", 0)),
        "transport_failures": int(stream.get("transport_failures", 0)),
        "transport_attempts": stream.get("transport_attempts", []),
    }


async def _retrieval_once(client: httpx.AsyncClient, kb_id: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    payload = await request_json(
        client,
        "POST",
        f"/api/v1/knowledge-bases/{kb_id}/search",
        timeout=timeout,
        json={"query": SYNTHETIC_QUERY, "limit": 6, "mode": "bm25", "context_chars": 512},
    )
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    require(payload["retrieval"]["mode"] == "bm25", "retrieval mode mismatch")
    require(payload["retrieval"]["vector_model"] == "local-lexical-v1", "external vector model was selected")
    require(payload["results"], "retrieval returned no results")
    require("P-101" in (payload["results"][0].get("text") or ""), "retrieval missed the synthetic marker")
    return {"ok": True, "latency_ms": elapsed, "http_status": 200}


async def _attachment_once(client: httpx.AsyncClient, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.post(
        "/api/v1/files",
        files={"file": ("synthetic-concurrency-attachment.txt", SYNTHETIC_ATTACHMENT, "text/plain")},
        timeout=timeout,
    )
    if response.status_code != 201:
        raise CheckFailure(f"attachment upload returned HTTP {response.status_code}")
    payload = response.json()
    download = await client.get(f"/api/v1/files/{payload['id']}/content", timeout=timeout)
    if download.status_code != 200:
        raise CheckFailure(f"attachment download returned HTTP {download.status_code}")
    require(sha256_bytes(download.content) == sha256_bytes(SYNTHETIC_ATTACHMENT), "attachment hash mismatch")
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    return {"ok": True, "latency_ms": elapsed, "http_status": 201}


async def _cancel_once(client: httpx.AsyncClient, conversation_id: str, timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    sent = await request_json(
        client,
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages",
        expected=(202,),
        timeout=timeout,
        json={"client_request_id": uuid_hex(), "text": "wait", "file_ids": []},
    )
    started_or_terminal = await wait_run(
        client,
        sent["run_id"],
        timeout,
        statuses={"running", "succeeded", "failed", "cancelled", "interrupted"},
    )
    if started_or_terminal["status"] == "failed" and started_or_terminal.get("error_code") == CAPACITY_CODE:
        return {
            "ok": True,
            "capacity_rejected": True,
            "run_id": sent["run_id"],
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "http_status": 202,
            "error_code": CAPACITY_CODE,
        }
    require(started_or_terminal["status"] == "running", f"cancel prerequisite run ended as {started_or_terminal['status']}")
    cancel_started = time.perf_counter()
    response = await client.post(f"/api/v1/runs/{sent['run_id']}/cancel", timeout=timeout)
    if response.status_code != 200:
        raise CheckFailure(f"cancel returned HTTP {response.status_code}")
    final = await wait_run(client, sent["run_id"], timeout)
    elapsed = round((time.perf_counter() - cancel_started) * 1000, 3)
    require(final["status"] == "cancelled", f"cancelled run ended as {final['status']}")
    return {
        "ok": True,
        "completed": True,
        "run_id": sent["run_id"],
        "latency_ms": elapsed,
        "http_status": 200,
        "cancelled": True,
    }


def uuid_hex() -> str:
    return os.urandom(16).hex()


async def _create_fixture(client: httpx.AsyncClient, timeout: float) -> tuple[str, str]:
    """Create only synthetic resources used by a load process."""
    kb = await request_json(
        client,
        "POST",
        "/api/v1/knowledge-bases",
        expected=(201,),
        timeout=timeout,
        json={"name": "合成并发知识库", "scope": "private"},
    )
    document = await client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents",
        files={
            "file": (
                "synthetic-concurrency-manual.md",
                (
                    "# 合成并发手册\n\n泵站阀门编号 P-101，维护周期为每周一次。\n"
                    "本文件只用于学校云并发压测。\n"
                ).encode("utf-8"),
                "text/markdown",
            )
        },
        timeout=timeout,
    )
    if document.status_code != 201:
        raise CheckFailure(f"load knowledge upload returned HTTP {document.status_code}")
    if document.json().get("status") != "ready":
        raise CheckFailure("load knowledge document is not indexed")
    assistant = await request_json(
        client,
        "POST",
        "/api/v1/assistants",
        expected=(201,),
        timeout=timeout,
        json={
            "name": "合成并发助手",
            "instructions": "只使用合成资料。",
            "knowledge_base_ids": [kb["id"]],
            "connections": [],
            "web_search_enabled": False,
        },
    )
    return assistant["id"], kb["id"]


async def _execute_once(
    scenario: str,
    client: httpx.AsyncClient,
    conversation_id: str | None,
    kb_id: str,
    timeout: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        if scenario == "chat":
            require(conversation_id is not None, "chat conversation is missing")
            return await _chat_once(client, conversation_id, timeout)
        if scenario == "sse":
            require(conversation_id is not None, "SSE conversation is missing")
            return await _sse_once(client, conversation_id, timeout)
        if scenario == "retrieval":
            return await _retrieval_once(client, kb_id, timeout)
        if scenario == "attachment":
            return await _attachment_once(client, timeout)
        if scenario == "cancel":
            require(conversation_id is not None, "cancel conversation is missing")
            return await _cancel_once(client, conversation_id, timeout)
        raise CheckFailure(f"unknown scenario: {scenario}")
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return {
            "ok": False,
            "timeout": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": "TIMEOUT",
        }
    except CheckFailure as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": str(exc)[:240],
        }
    except Exception as exc:  # Keep one worker failure from aborting the matrix cell.
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def _aggregate(
    scenario: str,
    level: int,
    records: list[dict[str, Any]],
    duration: float,
    resources: dict[str, Any],
    iterations: int,
) -> dict[str, Any]:
    latencies = [item["latency_ms"] for item in records if isinstance(item.get("latency_ms"), (int, float))]
    first_bytes = [
        item["first_byte_ms"]
        for item in records
        if isinstance(item.get("first_byte_ms"), (int, float))
    ]
    errors = Counter(item.get("error") or "UNKNOWN" for item in records if not item.get("ok"))
    capacity_codes = Counter(
        item.get("error_code") or CAPACITY_CODE for item in records if item.get("capacity_rejected")
    )
    completed = sum(bool(item.get("completed")) for item in records)
    capacity_rejected = sum(bool(item.get("capacity_rejected")) for item in records)
    return {
        "scenario": scenario,
        "level": level,
        "iterations": iterations,
        "execution_layer": scenario in EXECUTION_SCENARIOS,
        "requests": len(records),
        "ok": sum(bool(item.get("ok")) for item in records),
        "completed": completed,
        "capacity_rejected": capacity_rejected,
        "classified_executions": completed + capacity_rejected,
        "error_count": sum(not item.get("ok") for item in records),
        "error_codes": dict(errors),
        "capacity_codes": dict(capacity_codes),
        "timeouts": sum(bool(item.get("timeout")) for item in records),
        "cancellations": sum(bool(item.get("cancelled")) for item in records),
        "sse_reconnects": sum(int(item.get("reconnects") or 0) for item in records) if scenario == "sse" else 0,
        "stale_connection_retries": sum(int(item.get("connection_retries") or 0) for item in records),
        "unknown_transport_retries": sum(int(item.get("unknown_transport_retries") or 0) for item in records),
        "transport_failures": sum(int(item.get("transport_failures") or 0) for item in records),
        "transport_attempts": [
            detail
            for item in records
            for detail in (item.get("transport_attempts") or [])
        ],
        "latency_ms": latency_summary(latencies),
        "first_byte_ms": latency_summary(first_bytes) if scenario == "sse" else None,
        "throughput_rps": round(len(records) / duration, 3) if duration > 0 else None,
        "duration_ms": round(duration * 1000, 3),
        "resources": resources,
    }


async def _run_round(
    app,
    client: httpx.AsyncClient,
    scenario: str,
    level: int,
    assistant_id: str,
    kb_id: str,
    config: LoadConfig,
) -> dict[str, Any]:
    conversations: list[str] = []
    if scenario in CONVERSATION_SCENARIOS:
        for _ in range(level):
            conversations.append(await _create_conversation(client, assistant_id, config.timeout))
    records: list[dict[str, Any]] = []

    async def worker(index: int) -> None:
        conversation_id = conversations[index] if conversations else None
        for _ in range(config.iterations):
            records.append(await _execute_once(scenario, client, conversation_id, kb_id, config.timeout))

    sampler = ResourceSampler(app, config.sample_interval)
    started = time.perf_counter()
    async with sampler:
        await asyncio.gather(*(worker(index) for index in range(level)))
    duration = time.perf_counter() - started
    return _aggregate(scenario, level, records, duration, sampler.as_dict(), config.iterations)


async def run_load(config: LoadConfig | None = None) -> dict[str, Any]:
    config = config or LoadConfig()
    generated_at = now()
    temp_owned = config.data_dir is None
    data_dir = Path(config.data_dir) if config.data_dir else Path(tempfile_mkdtemp())
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "synthetic": True,
        "mode": "in-process-loopback-http",
        "command": f"python3 {Path(__file__).relative_to(ROOT)}",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "uvicorn": uvicorn.__version__,
            "httpx": httpx.__version__,
            "server_and_generator_share_process": True,
            "sqlite_synchronous": config.sqlite_synchronous,
            "acceptance_durability": config.sqlite_synchronous == "FULL",
            "note": "FULL 为部署与验收默认；NORMAL 仅用于定位 fsync 成本，不作为门禁证据。",
        },
        "config": {
            "levels": list(config.levels),
            "iterations": config.iterations,
            "scenarios": list(config.scenarios),
            "timeout_seconds": config.timeout,
            "sample_interval_seconds": config.sample_interval,
            "seed": config.seed,
            "max_active_sessions": config.max_active_sessions,
            "execution_hold_ms": config.execution_hold_ms,
            "execution_slots_are_deployment_input": True,
            "capacity_script": "scripts/trial_capacity.py",
        },
        "matrix": [],
        "runtime": {},
        "external_validation_limits": list(EXTERNAL_VALIDATION_LIMITS),
    }
    server: SyntheticTrialServer | None = None
    try:
        MatrixRuntime.hold_seconds = config.execution_hold_ms / 1000
        server = await SyntheticTrialServer.start(
            data_dir,
            max_active_sessions=config.max_active_sessions,
            idle_seconds=30,
            delta_delay=0.003,
            runtime_factory=MatrixRuntime,
            sqlite_synchronous=config.sqlite_synchronous,
        )
        server.create_admin()
        async with make_client(server.base_url, timeout=config.timeout, max_connections=512) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "synthetic-admin", "password": "Synthetic-trial-password-123"},
                timeout=config.timeout,
            )
            if login.status_code != 200:
                raise CheckFailure(f"load login returned HTTP {login.status_code}")
            assistant_id, kb_id = await _create_fixture(client, config.timeout)
            for scenario in config.scenarios:
                for level in config.levels:
                    report["matrix"].append(
                        await _run_round(
                            server.app,
                            client,
                            scenario,
                            level,
                            assistant_id,
                            kb_id,
                            config,
                        )
                    )
        await server.stop()
        server = None
        report["runtime"] = {
            "connects": MatrixRuntime.connects,
            "executions": MatrixRuntime.executions,
            "model_calls": MatrixRuntime.model_calls,
        }
    finally:
        if server is not None:
            await server.stop()
        rows = report["matrix"]
        unexpected_errors = sum(row["error_count"] for row in rows)
        expected_capacity = sum(row["capacity_rejected"] for row in rows)
        execution_gap = [
            {
                "scenario": row["scenario"],
                "level": row["level"],
                "requests": row["requests"],
                "expected_capacity_is_missing": row["level"] > config.max_active_sessions
                and row["execution_layer"]
                and row["capacity_rejected"] == 0,
            }
            for row in rows
            if row["level"] > config.max_active_sessions
            and row["execution_layer"]
            and row["capacity_rejected"] == 0
            # Cancellation intentionally releases an actor while the burst
            # is still being classified. A complete cancellation accounting
            # is the capacity evidence for that scenario; requiring a
            # rejection as well makes the result timing-dependent.
            and not (
                row["scenario"] == "cancel"
                and row["cancellations"] == row["requests"]
            )
        ]
        report["summary"] = {
            "status": "passed" if rows and unexpected_errors == 0 and not execution_gap else "failed",
            "matrix_rows": len(rows),
            "requests": sum(row["requests"] for row in rows),
            "ok": sum(row["ok"] for row in rows),
            "errors": unexpected_errors,
            "capacity_rejections": expected_capacity,
            "completed": sum(row["completed"] for row in rows),
            "execution_gap": execution_gap,
            "timeouts": sum(row["timeouts"] for row in rows),
            "cancellations": sum(row["cancellations"] for row in rows),
            "sse_reconnects": sum(int(row.get("sse_reconnects") or 0) for row in rows),
            "stale_connection_retries": sum(int(row.get("stale_connection_retries") or 0) for row in rows),
            "unknown_transport_retries": sum(int(row.get("unknown_transport_retries") or 0) for row in rows),
            "transport_failures": sum(int(row.get("transport_failures") or 0) for row in rows),
            "model_calls": MatrixRuntime.model_calls,
            "provider_calls": 0,
            "capacity_script": "scripts/trial_capacity.py",
        }
        report["data_dir"] = str(data_dir)
        if temp_owned and not config.keep_data:
            import shutil

            shutil.rmtree(data_dir, ignore_errors=True)
            report["data_dir_retained"] = False
        else:
            report["data_dir_retained"] = True
    return report


def tempfile_mkdtemp() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="concurrency-load-")


def _ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 合成并发压测矩阵（2026-11-12）",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 模式：{report['mode']}（合成资源，无付费模型、无外网、无客户数据）",
        f"- 总体状态：{report['summary']['status']}",
        f"- 运行命令：`{report['command']}`",
        "",
        "## 1. 目的与边界",
        "",
        "本记录为华微试用版的学校云功能烟囱与并发压测基础。它验证产品服务在单进程回环环境下，",
        "聊天、SSE、知识检索、附件和取消五条链路的应用逻辑、错误处理、超时、取消与资源峰值，",
        "并给出可重复的测量口径。它不是生产容量认证，也不代表真实模型、客户数据或部署环境的性能。",
        "",
        "压测前已审查现有脚本：`scripts/smoke_api.py` 需要部署地址和凭据，模型调用必须显式 `--real-model`；",
        "`scripts/benchmark.py`、`scripts/multimodel_smoke.py` 等会调用真实模型；",
        "`scripts/synthetic_benchmark.py` 只覆盖离线基准夹具和工具契约，不启动产品 HTTP 服务。",
        "因此本轮新增独立脚本，默认只使用合成资源和注入的确定性运行时。",
        "",
        "## 2. 本次运行环境",
        "",
        f"- 平台：{report['environment']['platform']}",
        f"- Python：{report['environment']['python']}",
        f"- 处理器数量：{report['environment']['cpu_count']}",
        f"- uvicorn：{report['environment']['uvicorn']}；httpx：{report['environment']['httpx']}",
        f"- 并发档位：{', '.join(str(value) for value in report['config']['levels'])}",
        f"- 每个并发工作者的迭代次数：{report['config']['iterations']}",
        f"- 场景：{', '.join(report['config']['scenarios'])}",
        f"- 执行槽上限：{report['config']['max_active_sessions']}（显式部署输入，不从并发档位推导）",
        f"- 合成执行保持时间：{report['config']['execution_hold_ms']} 毫秒（只用于让槽位边界可观测）",
        f"- 容量边界：独立脚本 `{report['config']['capacity_script']}`（运行时门控，不复用本矩阵的并发数字）",
        "",
        "## 3. 并发矩阵",
        "",
        "| 场景 | 并发 | 请求数 | 完成 | 预期容量拒绝 | 未预期错误 | p50 毫秒 | p95 毫秒 | p99 毫秒 | 首字节 p50/p95 毫秒 | 吞吐（请求/秒） | 超时 | 取消 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "chat": "聊天",
        "sse": "SSE",
        "retrieval": "知识检索",
        "attachment": "附件",
        "cancel": "取消",
    }
    for row in report["matrix"]:
        latency = row["latency_ms"]
        first = row["first_byte_ms"]
        first_text = "—" if not first else f"{_ms(first['p50'])} / {_ms(first['p95'])}"
        lines.append(
            f"| {labels.get(row['scenario'], row['scenario'])} | {row['level']} | {row['requests']} | "
            f"{row['completed']} | {row['capacity_rejected']} | {row['error_count']} | "
            f"{_ms(latency['p50'])} | {_ms(latency['p95'])} | {_ms(latency['p99'])} | {first_text} | "
            f"{row['throughput_rps']:.2f} | {row['timeouts']} | {row['cancellations']} |"
        )
    lines += [
        "",
        "延迟统计覆盖该场景该档位全部已完成的请求，包含失败请求；首字节只对 SSE 场景统计。",
        "每个档位的样本数为并发数乘迭代次数，当前最多 50 个样本，p99 只作趋势参考。",
        "",
        "## 4. 资源峰值",
        "",
        "| 场景 | 并发 | RSS 峰值（MiB） | 线程峰值 | 文件描述符峰值 | 活动会话峰值 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["matrix"]:
        resources = row["resources"]
        rss = resources.get("rss_peak_bytes")
        rss_text = "—" if rss is None else f"{rss / (1024 * 1024):.1f}"
        lines.append(
            f"| {labels.get(row['scenario'], row['scenario'])} | {row['level']} | {rss_text} | "
            f"{resources.get('threads_peak')} | {resources.get('fds_peak')} | {resources.get('active_actors_peak')} |"
        )
    lines += [
        "",
        "资源数据来自 `/proc/self/status` 与 `/proc/self/fd`，由压测进程自身采样；",
        "服务端、压测客户端和 SQLite 在同一进程，因此峰值包含压测工具自身开销，不能等同于纯服务端内存。",
        "RSS、线程、文件描述符和活动会话都是进程级绝对值：后序档位会包含前序档位尚未回收的线程、连接和会话",
        "（会话 actor 空闲 30 秒后才回收），因此应看增长趋势，不能把单行数值当作该场景独占的开销。",
        "",
        "## 5. 结论",
        "",
    ]
    failed = [row for row in report["matrix"] if row["error_count"]]
    if not failed:
        lines.append(
            "本次合成运行中，全部场景与全部并发档位均返回预期结果，没有未预期错误或超时。"
            "聊天、SSE 和取消在执行槽已满时明确返回 `EXECUTION_CAPACITY`，没有被计为 5xx、静默排队或服务崩溃；"
            "检索命中合成标记，附件哈希校验通过，已接受的运行均记录到唯一预期终态。"
        )
    else:
        details = "；".join(
            f"{row['scenario']} 并发 {row['level']}：{row['error_count']} 个错误"
            + (f"（{json.dumps(row['error_codes'], ensure_ascii=False)}）" if row["error_codes"] else "")
            for row in failed
        )
        lines.append(f"本次合成运行出现未通过项：{details}。这些结果应作为容量或错误处理问题继续排查，不能视为通过。")
    lines += [
        "",
        "判读限制：服务端、压测客户端和 SQLite 共享同一个 Python 进程与同一个事件循环；未使用多 worker、",
        "反向代理、容器、TLS 或真实网络。执行槽由 `--max-active-sessions` 显式传入，默认和本次验收均为 4，",
        "不会随 5/10/25 档位提高。控制面/只读层与执行层分开计数，超额执行拒绝不会被总成功率掩盖。",
        "因此本矩阵证明的是当前单进程应用逻辑和默认四槽边界的合成基线，不是生产容量上限。",
    ]
    reconnects = sum(row.get("sse_reconnects") or 0 for row in report["matrix"])
    stale_retries = sum(row.get("stale_connection_retries") or 0 for row in report["matrix"])
    unknown_retries = sum(row.get("unknown_transport_retries") or 0 for row in report["matrix"])
    transport_failures = sum(row.get("transport_failures") or 0 for row in report["matrix"])
    lines += [
        "",
        f"SSE 终态：本次运行共有 {reconnects} 次 SSE 连接需要按事件游标断线重连补齐，"
        f"{stale_retries} 次可证实为收到任何响应头与响应字节之前就失败的连接重试，"
        f"{unknown_retries} 次发生在响应开始之后或已收到字节、无法证实是空闲连接，按未知传输重试计入重连；"
        f"原始传输失败共 {transport_failures} 次（不含超时），逐次记录异常类型、响应是否已开始、失败时已收字节和事件数。"
        "服务端已在读取运行终态后、关闭连接前按当前游标再回放一次事件，因此终态事件（`run.completed`）"
        "不会再落在两道读之间被单条连接错过；健康回环上的连接应在一次连接内收到完整生命周期。"
        "事件游标重连计数保留为可观测指标：一旦在健康回环上出现，应按超时或服务端关闭路径排查，不能用重连掩盖终态缺失。"
        "空闲连接重试单独计数，只有可证实发生在收到任何响应之前的失败才归入此类；无法证实的重试不扣减重连数，原始失败率保留。",

        "",
        "## 5.1 活动执行槽容量（独立验收）",
        "",
        "- 四槽上限、精确超额拒绝、取消与释放后恢复由 `scripts/trial_capacity.py` 单独验证；",
        "- 该脚本用运行时门控让四个活动槽在 8/25 个突发提交完成前始终保持占用，因此接受数与拒绝数是确定的，",
        "  不受本矩阵短合成任务完成时机的影响；",
        "- 本矩阵只证明应用逻辑在多并发下不报错，不代表 25 个模型并发执行；部署承诺仍为四个活动执行槽。",
        "",
        "## 6. 无法在无外部资源下验收的产品功能",
        "",
    ]
    lines += [f"- {item}" for item in EXTERNAL_VALIDATION_LIMITS]
    lines += [
        "",
        "## 7. 复现与判读",
        "",
        "```bash",
        "python3 scripts/concurrency_load.py \\",
        "  --levels 1,5,10,25 \\",
        "  --iterations 2 \\",
        "  --timeout 20 \\",
        "  --max-active-sessions 4 \\",
        "  --scenarios chat,sse,retrieval,attachment,cancel \\",
        "  --json-out .runtime/evidence/trial_concurrency.json \\",
        "  --markdown-out .runtime/evidence/trial_concurrency.md",
        "",
        "python3 scripts/trial_capacity.py \\",
        "  --sessions 4 --bursts 8,25 \\",
        "  --json-out .runtime/evidence/trial_capacity.json",
        "```",
        "",
        "运行过程中只使用临时 SQLite 数据目录、合成知识文档、合成附件和注入的确定性运行时；",
        "默认结束后删除临时目录，报告的 `data_dir_retained` 字段记录是否保留。",
        "判读时应同时查看错误码、超时和取消计数；吞吐和百分位不能单独作为验收结论。",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the synthetic layered concurrency load matrix.")
    parser.add_argument("--levels", default="1,5,10,25", help="Comma-separated concurrency levels.")
    parser.add_argument("--iterations", type=int, default=2, help="Iterations per worker in each matrix cell.")
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS), help="Comma-separated scenarios.")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request terminal timeout in seconds.")
    parser.add_argument("--sample-interval", type=float, default=0.05, help="Resource sampling interval in seconds.")
    parser.add_argument(
        "--max-active-sessions",
        type=int,
        default=4,
        help="Explicit deployed execution-slot count; never derived from the matrix levels.",
    )
    parser.add_argument(
        "--execution-hold-ms",
        type=int,
        default=350,
        help="Brief local hold that makes the configured slot boundary observable.",
    )
    parser.add_argument("--data-dir", type=Path, help="Use this data directory instead of an owned temporary directory.")
    parser.add_argument("--keep-data", action="store_true", help="Keep a script-owned temporary data directory.")
    parser.add_argument("--json-out", type=Path, help="Write the full JSON report to this path.")
    parser.add_argument("--markdown-out", type=Path, help="Write the Markdown matrix to this path.")
    parser.add_argument("--allow-errors", action="store_true", help="Exit zero even when measured requests fail.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        levels = tuple(int(value.strip()) for value in args.levels.split(",") if value.strip())
        scenarios = tuple(value.strip() for value in args.scenarios.split(",") if value.strip())
        config = LoadConfig(
            levels=levels,
            iterations=args.iterations,
            scenarios=scenarios,
            timeout=args.timeout,
            sample_interval=args.sample_interval,
            max_active_sessions=args.max_active_sessions,
            execution_hold_ms=args.execution_hold_ms,
            data_dir=args.data_dir,
            keep_data=args.keep_data,
        )
        report = asyncio.run(run_load(config))
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False))
        return 2
    report["command"] = "python3 " + " ".join(shlex.quote(item) for item in sys.argv)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["summary"]["status"],
                "matrix_rows": report["summary"]["matrix_rows"],
                "requests": report["summary"]["requests"],
                "errors": report["summary"]["errors"],
                "capacity_rejections": report["summary"]["capacity_rejections"],
                "timeouts": report["summary"]["timeouts"],
                "cancellations": report["summary"]["cancellations"],
                "model_calls": report["summary"]["model_calls"],
                "json_out": str(args.json_out) if args.json_out else None,
                "markdown_out": str(args.markdown_out) if args.markdown_out else None,
            },
            ensure_ascii=False,
        )
    )
    if report["summary"]["status"] == "passed" or args.allow_errors:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
