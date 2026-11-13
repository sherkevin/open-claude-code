#!/usr/bin/env python3
"""Controlled-trial control-plane and soak acceptance on school cloud.

The runner starts the real FastAPI application on an ephemeral loopback port
with a deterministic synthetic runtime.  It never calls a paid model, reads a
credential, opens a public port, or uses customer data.

Two profiles are supported:

* ``control``: 20 authenticated virtual users for ten minutes exercise
  health, session state, conversation lists, knowledge retrieval and run
  polling while four long synthetic runs continuously emit SSE.
* ``soak``: at least 30 minutes of SSE, retrieval, submissions, cancellation
  and run polling.  The final phase rechecks login, retrieval, submission and
  cancellation after the load has drained.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import platform
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from trial_smoke import (  # noqa: E402
    PASSWORD,
    STREAM_RECONNECT_DELAY,
    CheckFailure,
    SSEParser,
    SyntheticRuntime,
    SyntheticTrialServer,
    error_code,
    make_client,
    require,
)

SCHEMA_VERSION = "trial-soak/v1"
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
CAPACITY_CODE = "EXECUTION_CAPACITY"
CONTROL_OPERATIONS = {"health", "auth_me", "conversations", "retrieval", "run_status", "sse"}
SOAK_OPERATIONS = CONTROL_OPERATIONS | {"submit", "cancel"}
VIRTUAL_USER_PREFIX = "synthetic-vu-"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 3)
    rank = (len(ordered) - 1) * fraction
    lower, upper = int(rank), min(int(rank) + 1, len(ordered) - 1)
    weight = rank - lower
    return round(
        float(ordered[lower]) * (1 - weight) + float(ordered[upper]) * weight,
        3,
    )


def latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 3),
    }


class _SqliteLogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        lowered = message.lower()
        if "database" in lowered and ("locked" in lowered or "busy" in lowered):
            self.messages.append(message[:300])


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.perf_counter)
    records: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    http_5xx: int = 0
    sqlite_busy_locked: int = 0

    def add(
        self,
        operation: str,
        started: float,
        *,
        ok: bool,
        status_code: int | None = None,
        code: str | None = None,
        expected_capacity: bool = False,
        error: str | None = None,
        first_event_ms: float | None = None,
    ) -> None:
        elapsed = round((time.perf_counter() - started) * 1000, 3)
        if status_code is not None and status_code >= 500:
            self.http_5xx += 1
        self.records[operation].append(
            {
                "ok": ok,
                "expected_capacity": expected_capacity,
                "latency_ms": elapsed,
                "status_code": status_code,
                "code": code,
                "error": error[:240] if error else None,
                "first_event_ms": first_event_ms,
            }
        )

    def summary(self, operations: set[str]) -> dict[str, Any]:
        values = [item for name in operations for item in self.records.get(name, [])]
        latencies = [item["latency_ms"] for item in values]
        failed = [item for item in values if not item["ok"]]
        codes = Counter(
            item.get("code") or item.get("error") or "UNKNOWN"
            for item in failed
        )
        statuses = Counter(str(item.get("status_code")) for item in values)
        first_events = [
            item["first_event_ms"]
            for item in values
            if isinstance(item.get("first_event_ms"), (int, float))
        ]
        return {
            "operations": {
                name: {
                    "requests": len(self.records.get(name, [])),
                    "ok": sum(bool(item["ok"]) for item in self.records.get(name, [])),
                    "expected_capacity": sum(
                        bool(item["expected_capacity"]) for item in self.records.get(name, [])
                    ),
                    "latency_ms": latency_summary(
                        [item["latency_ms"] for item in self.records.get(name, [])]
                    ),
                    "error_codes": dict(
                        Counter(
                            item.get("code") or item.get("error") or "UNKNOWN"
                            for item in self.records.get(name, [])
                            if not item["ok"]
                        )
                    ),
                }
                for name in sorted(operations)
            },
            "requests": len(values),
            "ok": sum(bool(item["ok"]) for item in values),
            "failed": len(failed),
            "expected_capacity": sum(bool(item["expected_capacity"]) for item in values),
            "success_rate_pct": round(
                sum(bool(item["ok"]) for item in values) / len(values) * 100, 6
            ) if values else 0.0,
            "latency_ms": latency_summary(latencies),
            "first_event_ms": latency_summary(first_events),
            "error_codes": dict(codes),
            "status_codes": dict(statuses),
        }


@dataclass
class StreamState:
    index: int
    conversation_id: str
    run_id: str
    assistant_message_id: str | None = None
    cursor: int = 0
    events: int = 0
    terminal_events: int = 0
    duplicate_event_ids: int = 0
    cross_run_events: int = 0
    reconnects: int = 0
    last_event_id_headers: int = 0
    first_event_ms: float | None = None
    finished_at: str | None = None
    terminal_status: str | None = None


class TrialRuntime(SyntheticRuntime):
    """Bounded synthetic runtime for long streams and short executions."""

    long_seconds = 3900.0
    stream_interval = 1.0

    async def execute(self, run, message) -> None:
        type(self).executions += 1
        text = str(message.get("text") or "").strip()
        if text == "wait" or text.startswith("soak-cancel"):
            await self.stopped.wait()
            raise asyncio.CancelledError()
        if text.startswith("soak-long"):
            deadline = time.monotonic() + type(self).long_seconds
            counter = 0
            while time.monotonic() < deadline:
                if self.stopped.is_set():
                    raise asyncio.CancelledError()
                counter += 1
                await self.emit(
                    "message.delta",
                    {
                        "message_id": run["assistant_message_id"],
                        "text": f"合成长稳心跳 {counter}。",
                    },
                )
                try:
                    await asyncio.wait_for(
                        self.stopped.wait(),
                        timeout=type(self).stream_interval,
                    )
                except asyncio.TimeoutError:
                    continue
                raise asyncio.CancelledError()
            reply = "合成长稳正常完成。"
        else:
            reply = "合成控制面回复：" + text
            for start in range(0, len(reply), 8):
                if self.stopped.is_set():
                    raise asyncio.CancelledError()
                await self.emit(
                    "message.delta",
                    {
                        "message_id": run["assistant_message_id"],
                        "text": reply[start:start + 8],
                    },
                )
                await asyncio.sleep(0.003)
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
                "source": "synthetic_soak_runtime",
                "model_call_count": 0,
            },
        )


def _proc_value(name: str) -> int | None:
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith(name + ":"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _threads() -> int | None:
    try:
        value = Path("/proc/self/status").read_text(encoding="utf-8").split("Threads:")[1]
        return int(value.split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _fds() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


class ResourceSampler:
    def __init__(self, app, interval: float = 5.0):
        self.app = app
        self.interval = interval
        self.stop = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.samples: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        try:
            actors = len(self.app.state.manager.actors)
        except Exception:
            actors = None
        return {
            "at": now(),
            "rss_bytes": _proc_value("VmRSS"),
            "vm_hwm_bytes": _proc_value("VmHWM"),
            "threads": _threads(),
            "fds": _fds(),
            "active_actors": actors,
        }

    async def loop(self) -> None:
        while not self.stop.is_set():
            self.samples.append(self.snapshot())
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
        self.samples.append(self.snapshot())
        self.task = asyncio.create_task(self.loop())

    async def finish(self) -> None:
        self.stop.set()
        if self.task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.samples.append(self.snapshot())

    def summary(self) -> dict[str, Any]:
        def values(name: str) -> list[int]:
            return [
                int(item[name])
                for item in self.samples
                if isinstance(item.get(name), int)
            ]

        rss, threads, fds, actors = (values(name) for name in ("rss_bytes", "threads", "fds", "active_actors"))
        return {
            "samples": len(self.samples),
            "rss_start_bytes": rss[0] if rss else None,
            "rss_peak_bytes": max(rss) if rss else None,
            "rss_end_bytes": rss[-1] if rss else None,
            "rss_growth_bytes": (rss[-1] - rss[0]) if rss else None,
            "threads_start": threads[0] if threads else None,
            "threads_peak": max(threads) if threads else None,
            "threads_end": threads[-1] if threads else None,
            "fds_start": fds[0] if fds else None,
            "fds_peak": max(fds) if fds else None,
            "fds_end": fds[-1] if fds else None,
            "active_actors_peak": max(actors) if actors else None,
            "active_actors_end": actors[-1] if actors else None,
        }


def virtual_username(index: int) -> str:
    return f"{VIRTUAL_USER_PREFIX}{index:02d}"


async def _login(client: httpx.AsyncClient, username: str = "synthetic-admin") -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    require(response.status_code == 200, f"login returned HTTP {response.status_code}")


async def _record_request(
    metrics: Metrics,
    operation: str,
    method: str,
    client: httpx.AsyncClient,
    path: str,
    *,
    expected: tuple[int, ...] = (200,),
    timeout: float,
    **kwargs: Any,
) -> tuple[httpx.Response | None, Any]:
    started = time.perf_counter()
    try:
        response = await client.request(method, path, timeout=timeout, **kwargs)
    except Exception as exc:
        metrics.add(
            operation,
            started,
            ok=False,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return None, None
    code = error_code(response)
    if response.status_code in expected:
        payload = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = None
        metrics.add(operation, started, ok=True, status_code=response.status_code, code=code)
        return response, payload
    metrics.add(
        operation,
        started,
        ok=False,
        status_code=response.status_code,
        code=code,
        error=f"unexpected HTTP {response.status_code}",
    )
    return response, None


async def _create_fixture(server: SyntheticTrialServer, virtual_users: int) -> None:
    server.create_admin()
    service = server.app.state.service
    user = service.require(
        "users",
        service.db.one("SELECT id FROM users WHERE username=?", ("synthetic-admin",))["id"],
    )
    for index in range(1, virtual_users + 1):
        service.create_user(
            {
                "username": virtual_username(index),
                "display_name": f"合成虚拟用户 {index:02d}",
                "password": PASSWORD,
                "role": "member",
            }
        )
    kb = service.knowledge.save(
        {
            "name": "合成控制面知识库",
            "description": "学校云控制面与长稳测试专用",
            "scope": "team",
            "enabled": True,
            "default_for_new_assistants": False,
        },
        user,
    )
    service.knowledge.upload(
        kb["id"],
        user,
        "synthetic-soak-manual.md",
        (
            "# 合成控制面手册\n\n泵站阀门编号 P-101，维护周期为每周一次。\n"
            "本文件只用于学校云控制面和长稳测试。\n"
        ).encode("utf-8"),
    )
    assistant = service.save_assistant(
        {
            "name": "合成控制面助手",
            "description": "只使用合成资料。",
            "instructions": "这是学校云合成控制面测试。",
            "knowledge_base_ids": [kb["id"]],
            "connections": [],
            "skill_ids": [],
            "web_search_enabled": False,
            "scope": "team",
            "access": {"all_members": True, "user_ids": []},
        },
        user=user,
    )
    require(assistant.get("id"), "synthetic assistant fixture was not created")


async def _create_conversation(client: httpx.AsyncClient, assistant_id: str, title: str, timeout: float) -> str:
    response = await client.post(
        "/api/v1/conversations",
        json={"assistant_id": assistant_id, "title": title, "auto_model_routing": False},
        timeout=timeout,
    )
    require(response.status_code == 201, f"conversation create returned HTTP {response.status_code}")
    return response.json()["id"]


async def _submit(client: httpx.AsyncClient, conversation_id: str, text: str, timeout: float) -> dict[str, Any]:
    return (
        await client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "client_request_id": os.urandom(16).hex(),
                "text": text,
                "file_ids": [],
                "auto_route": False,
            },
            timeout=timeout,
        )
    )


async def _get_run(client: httpx.AsyncClient, run_id: str, timeout: float) -> dict[str, Any]:
    response = await client.get(f"/api/v1/runs/{run_id}", timeout=timeout)
    require(response.status_code == 200, f"run view returned HTTP {response.status_code}")
    return response.json()


async def _wait_terminal(
    client: httpx.AsyncClient,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await _get_run(client, run_id, timeout)
        if last["status"] in TERMINAL:
            return last
        await asyncio.sleep(0.05)
    raise CheckFailure(
        f"run {run_id} did not settle within {timeout:g}s (last={last.get('status') if last else 'unavailable'})"
    )


async def _consume_connection(
    client: httpx.AsyncClient,
    state: StreamState,
    metrics: Metrics,
    run_started: float,
    chunk_seconds: float,
) -> bool:
    parser = SSEParser()
    headers = {"Accept": "text/event-stream"}
    if state.cursor:
        headers["Last-Event-ID"] = str(state.cursor)
        state.last_event_id_headers += 1
    started = time.perf_counter()
    operation_started = time.perf_counter()
    metrics_recorded = False

    def record(
        *,
        ok: bool,
        status_code: int | None,
        code: str | None = None,
        error: str | None = None,
    ) -> None:
        nonlocal metrics_recorded
        if metrics_recorded:
            return
        metrics_recorded = True
        metrics.add(
            "sse",
            operation_started,
            ok=ok,
            status_code=status_code,
            code=code,
            error=error,
            first_event_ms=state.first_event_ms,
        )

    try:
        async with client.stream(
            "GET",
            f"/api/v1/runs/{state.run_id}/events",
            headers=headers,
            timeout=httpx.Timeout(20.0, connect=5.0),
        ) as response:
            if response.status_code != 200:
                record(
                    ok=False,
                    status_code=response.status_code,
                    code=error_code(response),
                    error="SSE returned unexpected status",
                )
                return False
            async for line in response.aiter_lines():
                if line and state.first_event_ms is None:
                    state.first_event_ms = round((time.perf_counter() - run_started) * 1000, 3)
                event = parser.feed(line)
                if event is not None:
                    event_id = int(event["sse_id"] or 0)
                    if event_id <= state.cursor:
                        state.duplicate_event_ids += 1
                    state.cursor = max(state.cursor, event_id)
                    state.events += 1
                    payload = event.get("event")
                    if isinstance(payload, dict):
                        message_id = payload.get("message_id")
                        if (
                            state.assistant_message_id
                            and isinstance(message_id, str)
                            and message_id != state.assistant_message_id
                        ):
                            state.cross_run_events += 1
                        if event["sse_type"] == "run.completed":
                            state.terminal_events += 1
                            state.terminal_status = (payload.get("data") or {}).get("status")
                            state.finished_at = now()
                    if state.terminal_events:
                        record(ok=True, status_code=200)
                        return True
                if time.perf_counter() - started >= chunk_seconds:
                    # A deliberately bounded read is a healthy reconnect point,
                    # not an SSE failure or a terminal event.
                    record(ok=True, status_code=200, code="CHUNK_BOUNDARY")
                    return False
        record(
            ok=state.terminal_events == 1,
            status_code=200,
            code="STREAM_CLOSED" if not state.terminal_events else None,
            error=None if state.terminal_events else "stream closed before terminal event",
        )
        return bool(state.terminal_events)
    except asyncio.CancelledError:
        record(ok=False, status_code=None, code="CANCELLED", error="SSE consumer was cancelled")
        raise
    except Exception as exc:
        record(
            ok=False,
            status_code=None,
            error=f"{type(exc).__name__}: {str(exc)[:180]}",
        )
        return False


async def _stream_worker(
    index: int,
    base_url: str,
    assistant_id: str,
    username: str,
    states: dict[int, StreamState],
    metrics: Metrics,
    timeout: float,
    chunk_seconds: float,
) -> None:
    async with make_client(base_url, timeout=timeout) as client:
        await _login(client, username)
        conversation_id = await _create_conversation(
            client,
            assistant_id,
            f"合成 SSE 长连接 {index + 1}",
            timeout,
        )
        started = time.perf_counter()
        response = await _submit(client, conversation_id, f"soak-long-{index + 1}", timeout)
        require(response.status_code == 202, f"long SSE submit returned HTTP {response.status_code}")
        run_id = response.json()["run_id"]
        run = await _get_run(client, run_id, timeout)
        state = StreamState(
            index=index,
            conversation_id=conversation_id,
            run_id=run_id,
            assistant_message_id=run.get("assistant_message_id"),
        )
        states[index] = state
        while state.terminal_events == 0:
            complete = await _consume_connection(
                client,
                state,
                metrics,
                started,
                chunk_seconds,
            )
            if complete:
                break
            state.reconnects += 1
            await asyncio.sleep(STREAM_RECONNECT_DELAY)
        final = await _wait_terminal(client, run_id, timeout)
        state.terminal_status = final["status"]
        state.finished_at = now()


async def _control_worker(
    index: int,
    base_url: str,
    kb_id: str,
    status_run_id: str,
    cookies: dict[str, str],
    metrics: Metrics,
    deadline: float,
    timeout: float,
    pace: float,
) -> None:
    async with make_client(base_url, timeout=timeout) as client:
        client.cookies.update(cookies)
        while time.monotonic() < deadline:
            await _record_request(metrics, "health", "GET", client, "/api/v1/health/live", timeout=timeout)
            await _record_request(metrics, "auth_me", "GET", client, "/api/v1/auth/me", timeout=timeout)
            await _record_request(metrics, "conversations", "GET", client, "/api/v1/conversations", timeout=timeout)
            await _record_request(
                metrics,
                "retrieval",
                "POST",
                client,
                f"/api/v1/knowledge-bases/{kb_id}/search",
                timeout=timeout,
                json={"query": "P-101 维护周期", "mode": "bm25", "limit": 4, "context_chars": 256},
            )
            await _record_request(
                metrics,
                "run_status",
                "GET",
                client,
                f"/api/v1/runs/{status_run_id}",
                timeout=timeout,
            )
            await asyncio.sleep(pace)


async def _prepare_status_probes(
    base_url: str,
    assistant_id: str,
    usernames: list[str],
    timeout: float,
) -> list[dict[str, Any]]:
    clients = [make_client(base_url, timeout=timeout) for _ in usernames]
    try:
        await asyncio.gather(
            *(_login(client, username) for client, username in zip(clients, usernames))
        )
        conversation_ids = await asyncio.gather(
            *(
                _create_conversation(client, assistant_id, f"合成状态轮询 {index + 1}", timeout)
                for index, client in enumerate(clients)
            )
        )
        run_ids: list[str] = []
        for start in range(0, len(clients), 4):
            end = min(start + 4, len(clients))
            responses = await asyncio.gather(
                *(
                    _submit(clients[index], conversation_ids[index], f"soak-probe-{index + 1}", timeout)
                    for index in range(start, end)
                )
            )
            for response in responses:
                require(response.status_code == 202, "status probe submission was not accepted")
                run_ids.append(response.json()["run_id"])
            await asyncio.gather(
                *(_wait_terminal(clients[index], run_ids[index], timeout) for index in range(start, end))
            )
        probes = []
        for client, run_id in zip(clients, run_ids):
            response = await _get_run(client, run_id, timeout)
            require(response["status"] == "succeeded", "status probe did not succeed")
            probes.append({"run_id": run_id, "cookies": dict(client.cookies)})
        return probes
    finally:
        await asyncio.gather(*(client.aclose() for client in clients), return_exceptions=True)


async def _execution_worker(
    index: int,
    base_url: str,
    assistant_id: str,
    username: str,
    metrics: Metrics,
    deadline: float,
    timeout: float,
    pace: float,
) -> None:
    async with make_client(base_url, timeout=timeout) as client:
        await _login(client, username)
        conversation_id = await _create_conversation(
            client,
            assistant_id,
            f"合成执行并发 {index + 1}",
            timeout,
        )
        iteration = 0
        while time.monotonic() < deadline:
            iteration += 1
            started = time.perf_counter()
            response = await _submit(client, conversation_id, f"soak-quick-{iteration}", timeout)
            code = error_code(response)
            if response.status_code != 202:
                metrics.add(
                    "submit",
                    started,
                    ok=False,
                    status_code=response.status_code,
                    code=code,
                    error="unexpected submission HTTP status",
                )
                await asyncio.sleep(pace)
                continue
            run_id = response.json()["run_id"]
            run = await _wait_terminal(client, run_id, timeout)
            capacity = run["status"] == "failed" and run.get("error_code") == CAPACITY_CODE
            metrics.add(
                "submit",
                started,
                ok=run["status"] == "succeeded" or capacity,
                status_code=202,
                code=run.get("error_code"),
                expected_capacity=capacity,
                error=None if run["status"] == "succeeded" or capacity else f"run ended as {run['status']}",
            )
            if iteration % 3 == 0:
                cancel_started = time.perf_counter()
                response = await _submit(client, conversation_id, "soak-cancel", timeout)
                code = error_code(response)
                if response.status_code != 202:
                    metrics.add(
                        "cancel",
                        cancel_started,
                        ok=False,
                        status_code=response.status_code,
                        code=code,
                        error="unexpected cancel-submit HTTP status",
                    )
                else:
                    cancel_run_id = response.json()["run_id"]
                    state = None
                    for _ in range(200):
                        state = await _get_run(client, cancel_run_id, timeout)
                        if state["status"] in {"running", *TERMINAL}:
                            break
                        await asyncio.sleep(0.01)
                    if state and state["status"] == "running":
                        response = await client.post(f"/api/v1/runs/{cancel_run_id}/cancel", timeout=timeout)
                        code = error_code(response)
                        final = await _wait_terminal(client, cancel_run_id, timeout)
                        canceled = response.status_code == 200 and final["status"] == "cancelled"
                        metrics.add(
                            "cancel",
                            cancel_started,
                            ok=canceled,
                            status_code=response.status_code,
                            code=code,
                            error=None if canceled else f"cancel ended as {final['status']}",
                        )
                    elif state and state["status"] == "failed" and state.get("error_code") == CAPACITY_CODE:
                        metrics.add(
                            "cancel",
                            cancel_started,
                            ok=True,
                            status_code=202,
                            code=CAPACITY_CODE,
                            expected_capacity=True,
                        )
                    else:
                        metrics.add(
                            "cancel",
                            cancel_started,
                            ok=False,
                            status_code=202,
                            code=(state or {}).get("error_code"),
                            error="cancel prerequisite did not reach running",
                        )
            await asyncio.sleep(pace)


async def _trace_terminal_counts(
    base_url: str,
    stream_order: list[str],
    timeout: float,
) -> dict[str, int]:
    async def count(run_id: str, index: int) -> tuple[str, int]:
        async with make_client(base_url, timeout=timeout) as client:
            await _login(client, virtual_username(index + 1))
            response = await client.get(f"/api/v1/runs/{run_id}/trace", timeout=timeout)
        if response.status_code != 200:
            return run_id, -1
        events = response.json().get("events") or []
        return run_id, sum(event.get("type") == "run.completed" for event in events)

    return dict(await asyncio.gather(*(count(run_id, index) for index, run_id in enumerate(stream_order))))


async def _cancel_stream(
    base_url: str,
    state: StreamState,
    timeout: float,
) -> None:
    async with make_client(base_url, timeout=timeout) as client:
        await _login(client, virtual_username(state.index + 1))
        response = await client.post(f"/api/v1/runs/{state.run_id}/cancel", timeout=timeout)
        require(response.status_code == 200, "long-stream cleanup cancel failed")


async def _post_checks(
    base_url: str,
    kb_id: str,
    assistant_id: str,
    timeout: float,
) -> dict[str, Any]:
    async with make_client(base_url, timeout=timeout) as client:
        await _login(client)
        health = await client.get("/api/v1/health/live", timeout=timeout)
        require(health.status_code == 200, "post-soak health failed")
        search = await client.post(
            f"/api/v1/knowledge-bases/{kb_id}/search",
            json={"query": "P-101 维护周期", "mode": "bm25", "limit": 4, "context_chars": 256},
            timeout=timeout,
        )
        require(search.status_code == 200 and search.json().get("results"), "post-soak retrieval failed")
        conversation_id = await _create_conversation(client, assistant_id, "长稳后复测", timeout)
        submitted = await _submit(client, conversation_id, "soak-post", timeout)
        require(submitted.status_code == 202, "post-soak submit failed")
        post_run = await _wait_terminal(client, submitted.json()["run_id"], timeout)
        require(post_run["status"] == "succeeded", "post-soak submitted run did not succeed")
        cancellation = await _submit(client, conversation_id, "wait", timeout)
        require(cancellation.status_code == 202, "post-soak cancel submit failed")
        cancel_run_id = cancellation.json()["run_id"]
        for _ in range(200):
            cancel_view = await _get_run(client, cancel_run_id, timeout)
            if cancel_view["status"] == "running":
                break
            if cancel_view["status"] in TERMINAL:
                raise CheckFailure("post-soak cancel prerequisite settled before cancellation")
            await asyncio.sleep(0.01)
        else:
            raise CheckFailure("post-soak cancel prerequisite never started")
        cancel_response = await client.post(f"/api/v1/runs/{cancel_run_id}/cancel", timeout=timeout)
        require(cancel_response.status_code == 200, "post-soak cancel endpoint failed")
        canceled = await _wait_terminal(client, cancel_run_id, timeout)
        require(canceled["status"] == "cancelled", "post-soak run was not cancelled")
        return {
            "health": health.status_code,
            "retrieval_results": len(search.json()["results"]),
            "submit_run_id": post_run["id"],
            "submit_status": post_run["status"],
            "cancel_run_id": canceled["id"],
            "cancel_status": canceled["status"],
        }


async def run_acceptance(
    *,
    profile: str,
    duration: float,
    virtual_users: int,
    stream_count: int,
    timeout: float,
    pace: float,
    sqlite_synchronous: str = "FULL",
    data_dir: Path | None = None,
    keep_data: bool = False,
) -> dict[str, Any]:
    if profile not in {"control", "soak"}:
        raise ValueError("profile must be control or soak")
    if duration <= 0 or virtual_users < 1 or stream_count < 1 or pace < 0:
        raise ValueError("duration, users, streams and pace must be positive")
    temp_owned = data_dir is None
    data_dir = Path(data_dir) if data_dir else Path(
        __import__("tempfile").mkdtemp(prefix=f"trial-{profile}-")
    )
    generated_at = now()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "profile": profile,
        "synthetic": True,
        "mode": "synthetic-loopback-http",
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "sqlite_synchronous": sqlite_synchronous,
            "acceptance_durability": sqlite_synchronous == "FULL",
        },
        "config": {
            "duration_seconds": duration,
            "virtual_users": virtual_users,
            "stream_count": stream_count,
            "timeout_seconds": timeout,
            "pace_seconds": pace,
            "max_active_sessions": 4,
        },
        "metrics": {},
        "streams": [],
        "runtime": {},
        "post_checks": None,
        "acceptance": {},
        "limitations": [
            "真实模型、供应商限流与真实网络延迟未测。",
            "客户数据、客户 MCP、跨网、多进程、反向代理与生产容量未测。",
            "所有运行使用合成 SQLite、合成资料和注入的确定性运行时，model_calls=0。",
            "健康与只读控制指标仅代表学校云同机单进程回环环境。",
        ],
    }
    metrics = Metrics()
    sqlite_capture = _SqliteLogCapture()
    root_logger = logging.getLogger()
    root_logger.addHandler(sqlite_capture)
    server: SyntheticTrialServer | None = None
    sampler: ResourceSampler | None = None
    states: dict[int, StreamState] = {}
    transient_run_ids: list[str] = []
    shutdown_ok = False
    residual_actors = None
    try:
        TrialRuntime.long_seconds = duration + 600
        server = await SyntheticTrialServer.start(
            data_dir,
            max_active_sessions=4,
            idle_seconds=30,
            runtime_factory=TrialRuntime,
            sqlite_synchronous=sqlite_synchronous,
            deployment_mode="trial",
        )
        await _create_fixture(server, virtual_users + stream_count)
        service = server.app.state.service
        pragma = service.db.one("PRAGMA synchronous")["synchronous"]
        journal = service.db.one("PRAGMA journal_mode")["journal_mode"]
        report["environment"].update(
            {
                "synchronous_pragma": pragma,
                "journal_mode": journal,
                "busy_timeout_ms": service.db.one("PRAGMA busy_timeout")["timeout"],
            }
        )
        user = service.require(
            "users",
            service.db.one("SELECT id FROM users WHERE username=?", ("synthetic-admin",))["id"],
        )
        kb = service.knowledge.list(user)[0]
        assistant = service.db.one("SELECT id FROM assistants WHERE deleted=0 ORDER BY rowid LIMIT 1")
        require(assistant and assistant.get("id"), "synthetic assistant fixture is missing")
        execution_count = (
            max(1, min(3, virtual_users - 1))
            if profile == "soak"
            else 0
        )
        control_count = virtual_users if profile == "control" else max(1, virtual_users - execution_count)
        control_usernames = [
            virtual_username(stream_count + index + 1)
            for index in range(control_count)
        ]
        status_probes = await _prepare_status_probes(
            server.base_url,
            assistant["id"],
            control_usernames,
            timeout,
        )
        sampler = ResourceSampler(server.app)
        await sampler.start()
        deadline = time.monotonic() + duration
        stream_tasks = [
            asyncio.create_task(
                _stream_worker(
                    index,
                    server.base_url,
                    assistant["id"],
                    virtual_username(index + 1),
                    states,
                    metrics,
                    timeout,
                    15.0 if profile == "control" else 20.0,
                )
            )
            for index in range(stream_count)
        ]
        require_start = time.monotonic() + 30
        while len(states) < stream_count and time.monotonic() < require_start:
            if any(task.done() for task in stream_tasks):
                for task in stream_tasks:
                    with contextlib.suppress(Exception):
                        task.result()
                raise CheckFailure("a synthetic SSE stream stopped during startup")
            await asyncio.sleep(0.02)
        require(len(states) == stream_count, "synthetic SSE streams did not become active")

        tasks: list[asyncio.Task] = []
        if profile == "control":
            tasks = [
                asyncio.create_task(
                    _control_worker(
                        index,
                        server.base_url,
                        kb["id"],
                        status_probes[index]["run_id"],
                        status_probes[index]["cookies"],
                        metrics,
                        deadline,
                        timeout,
                        pace,
                    )
                )
                for index in range(virtual_users)
            ]
        else:
            tasks = [
                asyncio.create_task(
                    _control_worker(
                        index,
                        server.base_url,
                        kb["id"],
                        status_probes[index]["run_id"],
                        status_probes[index]["cookies"],
                        metrics,
                        deadline,
                        timeout,
                        pace,
                    )
                )
                for index in range(control_count)
            ]
            tasks.extend(
                asyncio.create_task(
                    _execution_worker(
                        index,
                        server.base_url,
                        assistant["id"],
                        virtual_username(
                            stream_count + control_count + index + 1
                        ),
                        metrics,
                        deadline,
                        timeout,
                        max(pace, 0.25),
                    )
                )
                for index in range(execution_count)
            )
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        await asyncio.gather(
            *(
                _cancel_stream(server.base_url, state, timeout)
                for state in states.values()
            )
        )
        await asyncio.gather(*tasks, return_exceptions=False)
        await asyncio.gather(*stream_tasks, return_exceptions=False)
        ordered_states = sorted(states.values(), key=lambda value: value.index)
        all_run_ids = [value.run_id for value in ordered_states]
        terminal_counts = {}
        terminal_counts = await _trace_terminal_counts(
            server.base_url,
            all_run_ids,
            timeout,
        )
        if profile == "soak":
            report["post_checks"] = await _post_checks(
                server.base_url,
                kb["id"],
                assistant["id"],
                timeout,
            )
        nonterminal = service.db.all(
            "SELECT id,status FROM runs WHERE status NOT IN ('succeeded','failed','cancelled','interrupted')"
        )
        terminal_anomalies = service.db.all(
            "SELECT run_id,SUM(CASE WHEN type='run.completed' THEN 1 ELSE 0 END) AS terminal_count "
            "FROM events GROUP BY run_id "
            "HAVING terminal_count != 1 ORDER BY terminal_count,run_id"
        )
        report["database"] = {
            "runs": service.db.one("SELECT COUNT(*) n FROM runs")["n"],
            "events": service.db.one("SELECT COUNT(*) n FROM events")["n"],
            "nonterminal_runs": nonterminal,
            "terminal_event_counts": terminal_counts,
            "terminal_event_anomalies": terminal_anomalies,
            "duplicate_terminal_runs": [
                run_id for run_id, count in terminal_counts.items() if count != 1
            ],
        }
        await sampler.finish()
        report["resources"] = sampler.summary()
        report["metrics"] = metrics.summary(CONTROL_OPERATIONS)
        if profile == "soak":
            report["execution_metrics"] = metrics.summary({"submit", "cancel"})
            report["execution_metrics"]["expected_capacity"] = sum(
                bool(item.get("expected_capacity"))
                for name in ("submit", "cancel")
                for item in metrics.records.get(name, [])
            )
        report["http_5xx"] = metrics.http_5xx
        report["sqlite_busy_locked"] = len(sqlite_capture.messages)
        report["sqlite_errors"] = sqlite_capture.messages
        report["runtime"] = {
            "executions": TrialRuntime.executions,
            "connects": TrialRuntime.connects,
            "model_calls": TrialRuntime.model_calls,
        }
        report["streams"] = [
            {
                **state.__dict__,
                "terminal_count_in_trace": terminal_counts.get(state.run_id),
            }
            for state in sorted(states.values(), key=lambda item: item.index)
        ]
        await server.stop()
        server = None
        shutdown_ok = True
        residual_actors = len(getattr(service.manager, "actors", {}))
    finally:
        if sampler is not None and not sampler.task.done():
            await sampler.finish()
        if server is not None:
            with contextlib.suppress(Exception):
                await server.stop()
            shutdown_ok = True
            residual_actors = len(getattr(server.app.state.manager, "actors", {}))
        root_logger.removeHandler(sqlite_capture)
        metrics.sqlite_busy_locked = len(sqlite_capture.messages)
        summary = report.get("metrics") or metrics.summary(CONTROL_OPERATIONS)
        streams = report.get("streams") or [
            {**state.__dict__} for state in sorted(states.values(), key=lambda item: item.index)
        ]
        resources = report.get("resources") or {}
        database = report.get("database") or {}
        issues: list[str] = []
        minimum_duration = 600 if profile == "control" else 1800
        if duration < minimum_duration:
            issues.append(f"duration below acceptance minimum ({duration:g} < {minimum_duration})")
        if report["environment"].get("synchronous_pragma") != 2:
            issues.append("SQLite synchronous pragma is not FULL")
        if summary.get("requests", 0) == 0:
            issues.append("no control-plane requests were measured")
        if summary.get("success_rate_pct", 0) < 99.9:
            issues.append("control success rate is below 99.9%")
        latency = summary.get("latency_ms") or {}
        if latency.get("p95") is None or latency["p95"] > 1000:
            issues.append("control p95 exceeds 1000 ms")
        if latency.get("p99") is None or latency["p99"] > 2000:
            issues.append("control p99 exceeds 2000 ms")
        if metrics.http_5xx:
            issues.append(f"{metrics.http_5xx} HTTP 5xx responses")
        if len(sqlite_capture.messages):
            issues.append("SQLite busy/locked errors were logged")
        if len(streams) != stream_count:
            issues.append("not all synthetic SSE streams ran")
        if any(item.get("terminal_events") != 1 for item in streams):
            issues.append("an SSE stream did not observe exactly one terminal event")
        if any(item.get("duplicate_event_ids") for item in streams):
            issues.append("SSE Last-Event-ID replay duplicated an event")
        if any(item.get("cross_run_events") for item in streams):
            issues.append("SSE stream contained a cross-run event")
        if any(item.get("terminal_count_in_trace") != 1 for item in streams):
            issues.append("run trace does not contain exactly one terminal event")
        if database.get("nonterminal_runs"):
            issues.append("nonterminal runs remain after load drain")
        if database.get("terminal_event_anomalies"):
            issues.append("one or more runs do not have exactly one durable terminal event")
        if not shutdown_ok:
            issues.append("temporary server did not shut down cleanly")
        if residual_actors:
            issues.append("execution actors remain after shutdown")
        if profile == "soak" and not report.get("post_checks"):
            issues.append("post-soak login/retrieval/submit/cancel checks are missing")
        execution = report.get("execution_metrics") or metrics.summary({"submit", "cancel"})
        if profile == "soak" and execution.get("operations", {}).get("submit", {}).get("requests", 0) == 0:
            issues.append("soak measured no submissions")
        if profile == "soak" and execution.get("operations", {}).get("cancel", {}).get("requests", 0) == 0:
            issues.append("soak measured no cancellation attempts")
        rss_growth = resources.get("rss_growth_bytes")
        fds_peak = resources.get("fds_peak")
        threads_peak = resources.get("threads_peak")
        threads_start = resources.get("threads_start")
        if rss_growth is not None and rss_growth > 256 * 1024 * 1024:
            issues.append("RSS growth exceeds 256 MiB")
        if fds_peak is not None and fds_peak > 1024:
            issues.append("file descriptor peak exceeds 1024")
        if threads_peak is not None and threads_start is not None and threads_peak > threads_start + 64:
            issues.append("thread growth exceeds 64")
        report["acceptance"] = {
            "status": "passed" if not issues else "failed",
            "issues": issues,
            "minimum_duration_seconds": minimum_duration,
            "control_success_threshold_pct": 99.9,
            "control_p95_limit_ms": 1000,
            "control_p99_limit_ms": 2000,
            "shutdown_ok": shutdown_ok,
            "residual_actors": residual_actors,
        }
        report["status"] = report["acceptance"]["status"]
        report["data_dir"] = str(data_dir)
        if temp_owned and not keep_data:
            import shutil

            shutil.rmtree(data_dir, ignore_errors=True)
            report["data_dir_retained"] = False
        else:
            report["data_dir_retained"] = True
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# 合成{('控制面' if report['profile'] == 'control' else '长稳')}验收",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 状态：{report['status']}",
        f"- 时长：{report['config']['duration_seconds']} 秒",
        f"- 虚拟用户：{report['config']['virtual_users']}",
        f"- SSE 活动运行：{report['config']['stream_count']}",
        f"- 执行槽：{report['config']['max_active_sessions']}",
        f"- SQLite synchronous：{report['environment'].get('synchronous_pragma')}",
        "",
        "## 控制面",
        "",
        f"- 总请求：{report['metrics'].get('requests')}",
        f"- 成功率：{report['metrics'].get('success_rate_pct')}%",
        f"- p50 / p95 / p99：{report['metrics'].get('latency_ms')}",
        f"- HTTP 5xx：{report.get('http_5xx', 0)}",
        f"- SQLite busy/locked：{report.get('sqlite_busy_locked')}",
        "",
        "## SSE",
        "",
        "| 流 | 事件数 | 断线重放 | Last-Event-ID | 重复事件 | 跨运行事件 | 唯一终态 |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report.get("streams", []):
        lines.append(
            f"| {item['index'] + 1} | {item['events']} | {item['reconnects']} | "
            f"{item['last_event_id_headers']} | {item['duplicate_event_ids']} | "
            f"{item['cross_run_events']} | {item['terminal_events']} |"
        )
    resources = report.get("resources") or {}
    lines += [
        "",
        "## 资源",
        "",
        f"- RSS 起始/峰值/结束：{resources.get('rss_start_bytes')} / {resources.get('rss_peak_bytes')} / {resources.get('rss_end_bytes')}",
        f"- 线程起始/峰值/结束：{resources.get('threads_start')} / {resources.get('threads_peak')} / {resources.get('threads_end')}",
        f"- 文件描述符起始/峰值/结束：{resources.get('fds_start')} / {resources.get('fds_peak')} / {resources.get('fds_end')}",
        f"- 活动执行器峰值：{resources.get('active_actors_peak')}",
        "",
        "## 验收",
        "",
        f"- 结论：{report['acceptance']['status']}",
    ]
    if report["acceptance"].get("issues"):
        lines.extend(f"- {item}" for item in report["acceptance"]["issues"])
    else:
        lines.append("- 所有硬门槛通过。")
    lines += [
        "",
        "## 边界",
        "",
    ]
    lines.extend(f"- {item}" for item in report["limitations"])
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run controlled-trial control-plane or soak acceptance.")
    parser.add_argument("--profile", choices=("control", "soak"), default="control")
    parser.add_argument("--duration", type=float, help="Measured load duration in seconds.")
    parser.add_argument("--virtual-users", type=int, default=20)
    parser.add_argument("--streams", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--pace", type=float, default=1.0)
    parser.add_argument("--sqlite-synchronous", choices=("FULL", "NORMAL"), default="FULL")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    duration = args.duration if args.duration is not None else (600 if args.profile == "control" else 1800)
    try:
        report = asyncio.run(
            run_acceptance(
                profile=args.profile,
                duration=duration,
                virtual_users=args.virtual_users,
                stream_count=args.streams,
                timeout=args.timeout,
                pace=args.pace,
                sqlite_synchronous=args.sqlite_synchronous,
                data_dir=args.data_dir,
                keep_data=args.keep_data,
            )
        )
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.markdown_out:
            args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
        print(
            json.dumps(
                {
                    "profile": report["profile"],
                    "status": report["status"],
                    "issues": report["acceptance"]["issues"],
                    "requests": report["metrics"].get("requests"),
                    "json_out": str(args.json_out) if args.json_out else None,
                },
                ensure_ascii=False,
            )
        )
        return 0 if report["status"] == "passed" else 1
    except Exception as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:500]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
