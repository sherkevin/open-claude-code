#!/usr/bin/env python3
"""Repeatable synthetic vertical-slice smoke for the Huawei trial on school cloud.

This runner boots the real FastAPI application on an ephemeral loopback port
with an explicitly injected synthetic runtime.  It never calls a paid model,
opens an external network connection, reads a production credential, or touches
real customer data.  The temporary data directory is removed by default.

The smoke covers: health, admin login, model catalog, synthetic knowledge
upload and BM25 retrieval, assistant knowledge binding, attachment upload and
download, chat with an attachment, SSE event ordering, cancellation and
conversation reuse, usage aggregation, member knowledge isolation, and logout.
Cases that cannot be accepted without external resources are listed in the
report and in ``EXTERNAL_VALIDATION_LIMITS``.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

SCHEMA_VERSION = "trial-smoke/v1"
PASSWORD = "Synthetic-trial-password-123"
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
STREAM_RECONNECT_ATTEMPTS = 4
STREAM_RECONNECT_DELAY = 0.05
SYNTHETIC_DOCUMENT = (
    "# 合成设备维护手册\n\n"
    "本文件只用于学校云合成烟囱，不含任何真实客户资料。\n\n"
    "## 泵站阀门\n\n"
    "泵站阀门编号 P-101，维护周期为每周一次。\n\n"
    "## 振动阈值\n\n"
    "轴承温度检查时的振动安全阈值为 0.07 毫米。\n"
).encode("utf-8")
SYNTHETIC_ATTACHMENT = (b"synthetic trial attachment line\n" * 2048)
SYNTHETIC_QUERY = "P-101 维护周期"

EXTERNAL_VALIDATION_LIMITS = [
    "真实模型推理质量、真实首字延迟、Token 吞吐与费用：本烟囱注入确定性合成运行时，model_calls 始终为 0。",
    "真实客户 MCP 工具、真实业务系统与真实客户数据：未配置、未调用任何真实连接或客户样本。",
    "真实文档解析质量（PDF、DOCX、XLSX、图片 OCR、视觉嵌入）与知识图谱语义效果：只上传一小段合成 Markdown，并使用本地词法向量。",
    "联网网页搜索、真实网络错误、带宽限制和跨公网延迟：未发起任何外网请求。",
    "多进程或多 worker、容器、反向代理、TLS 与学校云实际部署容量：只在 127.0.0.1 回环启动单进程临时服务。",
    "长时间稳定性、内存泄漏和跨进程 SQLite 锁竞争：本烟囱不执行 soak 或分布式压测。",
    "真实客户权限矩阵、业务正确率和原方案基线：需要客户授权、真实样本和标准答案。",
    "浏览器端渲染、交互和页面性能：本烟囱只验证服务端 HTTP 与 SSE 协议；SSE 的 Last-Event-ID 断点续传由压测脚本覆盖。",
    "模型是否按提示实际使用检索结果：检索接口和证据链已验证，但合成运行时不会消费 RAG 上下文。",
]


class CheckFailure(RuntimeError):
    """A deliberately safe assertion message without raw response payloads."""


def require(condition: Any, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
        code = payload.get("error", {}).get("code")
        if isinstance(code, str) and code and code.replace("_", "").isalnum():
            return code[:80]
    except (ValueError, AttributeError):
        pass
    return None


def http_failure(method: str, path: str, response: httpx.Response) -> CheckFailure:
    code = error_code(response)
    suffix = f" ({code})" if code else ""
    return CheckFailure(f"{method} {path.split('?')[0]} returned HTTP {response.status_code}{suffix}")


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    expected: tuple[int, ...] = (200,),
    **kwargs: Any,
) -> Any:
    response = await client.request(method, path, **kwargs)
    if response.status_code not in expected:
        raise http_failure(method, path, response)
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise CheckFailure(f"{method} {path.split('?')[0]} did not return JSON") from exc


class SyntheticRuntime:
    """Deterministic in-process runtime; it can never reach a provider.

    The runtime factory is injected through ``create_app`` exactly like the
    existing tests do.  Normal messages emit a few deltas and a synthetic usage
    record.  The literal text ``wait`` parks until cancellation so the smoke can
    exercise the real run-cancellation path.
    """

    connects = 0
    executions = 0
    model_calls = 0
    delta_delay = 0.003
    delta_size = 8

    def __init__(self, service, conversation_id, config, approve, emit):
        self.service = service
        self.conversation_id = conversation_id
        self.config = config
        self.approve = approve
        self.emit = emit
        self.stopped = asyncio.Event()

    @classmethod
    def reset_counters(cls) -> None:
        cls.connects = 0
        cls.executions = 0
        cls.model_calls = 0

    async def connect(self) -> None:
        type(self).connects += 1

    async def execute(self, run, message) -> None:
        type(self).executions += 1
        text = str(message.get("text") or "")
        if text.strip() == "wait":
            await self.stopped.wait()
            raise asyncio.CancelledError()
        reply = "合成回复：" + text
        for start in range(0, len(reply), type(self).delta_size):
            if self.stopped.is_set():
                raise asyncio.CancelledError()
            await asyncio.sleep(type(self).delta_delay)
            await self.emit(
                "message.delta",
                {"message_id": run["assistant_message_id"], "text": reply[start:start + type(self).delta_size]},
            )
        await self.emit(
            "usage.updated",
            {
                "input_tokens": 12,
                "output_tokens": 4,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "estimated_cost_usd": 0,
                "currency": "USD",
                "cost_is_estimate": False,
                "source": "synthetic_trial_runtime",
                "model_call_count": 0,
            },
        )

    async def interrupt(self) -> None:
        self.stopped.set()

    async def close(self) -> None:
        return None


def make_client(
    base_url: str,
    *,
    timeout: float = 30.0,
    max_connections: int = 256,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Origin": base_url, "Accept": "application/json"},
        timeout=httpx.Timeout(timeout, connect=5.0),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max(1, max_connections // 2),
        ),
        follow_redirects=False,
        trust_env=False,
    )


class SyntheticTrialServer:
    """Ephemeral loopback HTTP server around the real product application."""

    def __init__(self, app, server: uvicorn.Server, task: asyncio.Task, base_url: str, data_dir: Path):
        self.app = app
        self.server = server
        self.task = task
        self.base_url = base_url
        self.data_dir = data_dir
        self._previous_env: dict[str, str | None] = {}

    @classmethod
    async def start(
        cls,
        data_dir: Path | str,
        *,
        max_active_sessions: int = 8,
        idle_seconds: int = 30,
        delta_delay: float = 0.003,
        runtime_factory=SyntheticRuntime,
        sqlite_synchronous: str = "FULL",
        deployment_mode: str = "normal",
        trial_capabilities: tuple[str, ...] | None = None,
        origins: tuple[str, ...] = (),
        approval_seconds: int = 3,
    ) -> "SyntheticTrialServer":
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        previous_env = {}
        for key, value in (
            ("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1"),
            ("AGENT_KNOWLEDGE_ALLOW_MODEL_DOWNLOAD", "false"),
        ):
            previous_env[key] = os.environ.get(key)
            os.environ[key] = value
        reset = getattr(runtime_factory, "reset_counters", None)
        if callable(reset):
            reset()
        if hasattr(runtime_factory, "delta_delay"):
            runtime_factory.delta_delay = delta_delay
        settings = Settings(
            data_dir=data_dir,
            provider_settings="",
            model="synthetic-trial-runtime",
            model_catalog="",
            default_model_id="",
            origins=origins,
            idle_seconds=idle_seconds,
            approval_seconds=approval_seconds,
            max_active_sessions=max_active_sessions,
            upload_max_bytes=20 * 1024 * 1024,
            deployment_mode=deployment_mode,
            trial_capabilities=trial_capabilities,
            # FULL is the deployment and acceptance default: every commit
            # fsyncs, exactly like production.  The school-cloud data disk
            # measures roughly 76 ms per fsync, so NORMAL exists only as an
            # explicitly-labelled diagnostic for isolating that cost; it is
            # never the configuration used for trial acceptance evidence.
            sqlite_synchronous=sqlite_synchronous,
        )
        # This runner injects its runtime explicitly. Never inherit host model
        # credentials or provider routes into the synthetic application.
        settings.provider_env = lambda: {}
        app = create_app(settings, runtime_factory=runtime_factory)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="on",
            loop="asyncio",
            http="h11",
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if task.done():
                    exc = task.exception()
                    raise CheckFailure(
                        f"temporary server failed to start ({type(exc).__name__ if exc else 'unknown'})"
                    )
                if server.started:
                    break
                await asyncio.sleep(0.01)
            else:
                raise CheckFailure("temporary server did not start within 20 seconds")
            sockets = [sock for asgi_server in server.servers for sock in asgi_server.sockets]
            require(bool(sockets), "temporary server has no listening socket")
            port = sockets[0].getsockname()[1]
            instance = cls(app, server, task, f"http://127.0.0.1:{port}", data_dir)
            instance._previous_env = previous_env
            return instance
        except BaseException:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=10)
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            raise

    def create_admin(self, username: str = "synthetic-admin") -> dict[str, Any]:
        return self.app.state.service.create_user(
            {
                "username": username,
                "display_name": "合成试用管理员",
                "password": PASSWORD,
                "role": "admin",
            }
        )

    async def stop(self) -> None:
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, timeout=15)
        except asyncio.TimeoutError:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        for key, value in self._previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class SSEParser:
    """Minimal SSE frame parser for product event streams."""

    def __init__(self):
        self.sse_id: str | None = None
        self.sse_type: str | None = None
        self.data: list[str] = []

    def feed(self, line: str) -> dict[str, Any] | None:
        if line == "":
            if not self.data and not self.sse_type:
                return None
            raw = "\n".join(self.data)
            payload = None
            if raw:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise CheckFailure("SSE data was not valid JSON") from exc
            event = {"sse_id": self.sse_id, "sse_type": self.sse_type or "message", "event": payload}
            self.sse_id, self.sse_type, self.data = None, None, []
            return event
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            self.sse_id = value
        elif field == "event":
            self.sse_type = value
        elif field == "data":
            self.data.append(value)
        return None


async def wait_run(
    client: httpx.AsyncClient,
    run_id: str,
    timeout: float = 10.0,
    statuses: set[str] | None = None,
) -> dict[str, Any]:
    target = statuses or TERMINAL
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        payload = await request_json(client, "GET", f"/api/v1/runs/{run_id}")
        require(isinstance(payload, dict), "run view is not an object")
        last = payload
        if payload["status"] in target:
            return payload
        await asyncio.sleep(0.01)
    state = last["status"] if last else "unavailable"
    raise CheckFailure(f"run did not reach {sorted(target)} within {timeout:g}s (last={state})")


async def stream_run_events(
    client: httpx.AsyncClient,
    run_id: str,
    timeout: float = 10.0,
    after: int = 0,
) -> dict[str, Any]:
    """Consume the run event stream, reconnecting from the last event id.

    The server replays the events it already holds and then closes a terminal
    run in the same connection.  Reconnects remain available because a real
    client can lose a connection mid-run, but a complete synthetic run on a
    healthy loopback must finish in one connection; the smoke asserts that so
    a missed terminal event cannot hide behind the reconnect path.
    """
    started = time.perf_counter()
    deadline = started + timeout
    headers_ms: float | None = None
    first_byte_ms: float | None = None
    events: list[dict[str, Any]] = []
    cursor = max(int(after), 0)
    connections = 0
    stale_connection_retries = 0
    unknown_transport_retries = 0
    transport_failures = 0
    transport_attempts: list[dict[str, Any]] = []
    for attempt in range(1, STREAM_RECONNECT_ATTEMPTS + 1):
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        connections += 1
        parser = SSEParser()
        completed = False
        response_started = False
        body_bytes = 0
        attempt_events = 0
        headers = {"Accept": "text/event-stream"}
        if cursor:
            headers["Last-Event-ID"] = str(cursor)
        try:
            async with client.stream(
                "GET",
                f"/api/v1/runs/{run_id}/events",
                headers=headers,
                timeout=httpx.Timeout(min(timeout, remaining), connect=5.0),
            ) as response:
                response_started = True
                if headers_ms is None:
                    headers_ms = round((time.perf_counter() - started) * 1000, 3)
                if response.status_code != 200:
                    raise http_failure("GET", f"/api/v1/runs/{run_id}/events", response)
                async for line in response.aiter_lines():
                    body_bytes += len(line.encode("utf-8")) + 1
                    if first_byte_ms is None and line:
                        first_byte_ms = round((time.perf_counter() - started) * 1000, 3)
                    event = parser.feed(line)
                    if event is None:
                        continue
                    events.append(event)
                    attempt_events += 1
                    cursor = max(cursor, int(event["sse_id"] or 0))
                    if event["sse_type"] == "run.completed":
                        completed = True
                        break
        except httpx.TimeoutException as exc:
            transport_attempts.append(
                {
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "classification": "timeout",
                    "response_started": response_started,
                    "body_bytes_at_failure": body_bytes,
                    "events_received": attempt_events,
                }
            )
            if events or attempt == STREAM_RECONNECT_ATTEMPTS:
                raise CheckFailure(f"SSE stream timed out after {timeout:g}s") from exc
        except httpx.TransportError as exc:
            # A pooled keep-alive connection can be closed by the server while
            # this client is busy elsewhere (uvicorn closes idle connections
            # after 5 s by default).  Only a failure that provably happened
            # before any response status line/headers (``response_started`` is
            # still false) can be attributed to that stale connection.  A
            # failure after headers, or one that produced bytes/events, is
            # recorded as unknown and still counts as a reconnect: the raw
            # transport failure rate is preserved rather than retried to zero.
            transport_failures += 1
            detail = {
                "attempt": attempt,
                "error_type": type(exc).__name__,
                "message": str(exc)[:120],
                "response_started": response_started,
                "body_bytes_at_failure": body_bytes,
                "events_received": attempt_events,
            }
            if not response_started and attempt_events == 0:
                detail["classification"] = "stale_connection_pre_response"
                stale_connection_retries += 1
            else:
                detail["classification"] = "unknown_transport_retry"
                unknown_transport_retries += 1
            transport_attempts.append(detail)
            if events or attempt == STREAM_RECONNECT_ATTEMPTS:
                raise
        if completed:
            break
        if attempt < STREAM_RECONNECT_ATTEMPTS:
            await asyncio.sleep(STREAM_RECONNECT_DELAY)
    total_ms = round((time.perf_counter() - started) * 1000, 3)
    require(first_byte_ms is not None, "SSE stream produced no body")
    require(events, "SSE stream produced no events")
    return {
        "headers_ms": headers_ms,
        "first_byte_ms": first_byte_ms,
        "total_ms": total_ms,
        "connections": connections,
        "stale_connection_retries": stale_connection_retries,
        "unknown_transport_retries": unknown_transport_retries,
        "transport_failures": transport_failures,
        "transport_attempts": transport_attempts,
        "cursor": cursor,
        "events": events,
    }


def event_types(stream: dict[str, Any]) -> list[str]:
    return [item["sse_type"] for item in stream["events"]]


def sse_payloads(stream: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item["event"] for item in stream["events"] if item["sse_type"] == kind and isinstance(item["event"], dict)]


@dataclass
class TrialSmokeConfig:
    data_dir: Path | None = None
    keep_data: bool = False
    report_path: Path | None = None
    timeout: float = 10.0
    # FULL is the deployment and trial-acceptance default.  NORMAL is a
    # diagnostic-only option and must be labelled as such in any evidence.
    sqlite_synchronous: str = "FULL"


@dataclass
class TrialState:
    kb_id: str | None = None
    document_id: str | None = None
    assistant_id: str | None = None
    conversation_id: str | None = None
    attachment_id: str | None = None


async def _check(report: dict[str, Any], name: str, operation: Callable[[], Any]) -> bool:
    started = time.perf_counter()
    try:
        detail = await operation()
        entry = {
            "name": name,
            "status": "passed",
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        }
        if detail is not None:
            entry["detail"] = detail
    except Exception as exc:  # The report records failures instead of aborting early.
        entry = {
            "name": name,
            "status": "failed",
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "error_type": type(exc).__name__,
            "error": str(exc)[:400],
        }
    report["checks"].append(entry)
    return entry["status"] == "passed"


async def _run_trial_policy_phase(
    data_dir: Path,
    report: dict[str, Any],
    state: TrialState,
) -> dict[str, Any]:
    """Restart the same synthetic dataset in controlled-trial mode.

    The normal-mode phase above creates the preset data.  This phase then
    verifies that the real HTTP and service boundaries refuse every capability
    outside the trial allowlist, while the enabled login/read/chat/search paths
    continue to work.  Admin role is deliberately used for reverse checks.
    """
    from app.deployment_policy import DEFAULT_TRIAL_CAPABILITIES, FEATURE_DISABLED

    server = await SyntheticTrialServer.start(
        data_dir,
        max_active_sessions=4,
        idle_seconds=30,
        deployment_mode="trial",
        sqlite_synchronous="FULL",
    )
    try:
        server.create_admin() if not server.app.state.service.db.one(
            "SELECT id FROM users WHERE username=?", ("synthetic-admin",)
        ) else None
        async with make_client(server.base_url) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "synthetic-admin", "password": PASSWORD},
            )
            require(login.status_code == 200, f"trial login returned HTTP {login.status_code}")

            async def trial_config_policy():
                payload = await request_json(client, "GET", "/api/v1/config")
                capabilities = set(payload.get("capabilities") or [])
                require(payload.get("deployment_mode") == "trial", "trial mode is not advertised")
                require(capabilities == set(DEFAULT_TRIAL_CAPABILITIES), "trial capability set differs from the reviewed default")
                require(payload.get("allowed_retrieval_modes") == ["bm25", "hybrid"], "trial retrieval modes are widened")
                return {
                    "deployment_mode": payload["deployment_mode"],
                    "capabilities": len(capabilities),
                    "allowed_retrieval_modes": payload["allowed_retrieval_modes"],
                }

            async def trial_allowed_reads_and_chat():
                me = await request_json(client, "GET", "/api/v1/auth/me")
                require(me.get("role") == "admin", "trial admin identity is unavailable")
                assistants = await request_json(client, "GET", "/api/v1/assistants")
                require(any(item["id"] == state.assistant_id for item in assistants), "preset assistant is unavailable")
                bases = await request_json(client, "GET", "/api/v1/knowledge-bases")
                require(any(item["id"] == state.kb_id for item in bases), "preset knowledge base is unavailable")
                await request_json(
                    client,
                    "GET",
                    "/api/v1/assistants",
                )
                conversation = await request_json(
                    client,
                    "POST",
                    "/api/v1/conversations",
                    expected=(201,),
                    json={
                        "assistant_id": state.assistant_id,
                        "title": "受控试用合成会话",
                        "auto_model_routing": False,
                    },
                )
                sent = await request_json(
                    client,
                    "POST",
                    f"/api/v1/conversations/{conversation['id']}/messages",
                    expected=(202,),
                    json={
                        "client_request_id": uuid.uuid4().hex,
                        "text": "受控试用允许的基础对话",
                        "file_ids": [],
                        "auto_route": False,
                    },
                )
                run = await wait_run(client, sent["run_id"], 10)
                require(run["status"] == "succeeded", f"trial chat ended as {run['status']}")
                state.conversation_id = conversation["id"]
                return {"conversation_id": conversation["id"], "run_id": run["id"]}

            async def trial_retrieval_modes():
                require(state.kb_id is not None, "knowledge base prerequisite is missing")
                modes = {}
                for mode in ("bm25", "hybrid"):
                    payload = await request_json(
                        client,
                        "POST",
                        f"/api/v1/knowledge-bases/{state.kb_id}/search",
                        json={"query": SYNTHETIC_QUERY, "limit": 4, "mode": mode, "context_chars": 256},
                    )
                    require(payload["retrieval"]["mode"] == mode, f"trial retrieval mode changed from {mode}")
                    require(payload["results"], f"trial retrieval returned no results for {mode}")
                    modes[mode] = len(payload["results"])
                return {"results_by_mode": modes}

            async def trial_reverse_boundaries():
                service = server.app.state.service
                assert state.conversation_id is not None
                before = {
                    table: service.db.one(f"SELECT COUNT(*) n FROM {table}")["n"]
                    for table in ("runs", "messages", "approvals", "files")
                }

                def denied(response: httpx.Response, operation: str):
                    require(response.status_code == 403, f"{operation} returned HTTP {response.status_code}")
                    require(error_code(response) == FEATURE_DISABLED, f"{operation} did not return FEATURE_DISABLED")

                for method, path, body in (
                    ("POST", "/api/v1/assistants", {"name": "denied"}),
                    ("POST", "/api/v1/files", None),
                    ("GET", "/api/v1/approvals/pending", None),
                    ("POST", "/api/v1/artifacts/xlsx", {"filename": "denied.xlsx", "sheets": []}),
                    ("GET", "/docs", None),
                    ("GET", "/openapi.json", None),
                ):
                    kwargs = {"json": body} if body is not None else {}
                    response = await client.request(method, path, **kwargs)
                    denied(response, f"{method} {path}")

                for mode in ("vector", "hipporag", "unknown-mode"):
                    response = await client.post(
                        f"/api/v1/knowledge-bases/{state.kb_id}/search",
                        json={"query": SYNTHETIC_QUERY, "mode": mode},
                    )
                    denied(response, f"retrieval mode {mode}")

                conversation_id = state.conversation_id
                for body, label in (
                    ({"client_request_id": uuid.uuid4().hex, "text": "denied route", "auto_route": True}, "auto_route"),
                    ({"client_request_id": uuid.uuid4().hex, "text": "denied file", "file_ids": ["synthetic-denied-file"]}, "file_ids"),
                    ({"client_request_id": uuid.uuid4().hex, "text": "denied skill", "skill_ids": ["synthetic-denied-skill"]}, "skill_ids"),
                ):
                    response = await client.post(f"/api/v1/conversations/{conversation_id}/messages", json=body)
                    denied(response, f"message field {label}")

                response = await client.post(
                    "/api/v1/conversations",
                    json={"assistant_id": state.assistant_id, "title": "denied routing", "auto_model_routing": True},
                )
                denied(response, "conversation auto_model_routing")

                def tool_boundaries():
                    try:
                        service.authorize_tool(conversation_id, "mcp__workspace__file_write", {"path": "denied.txt", "content": "denied"})
                    except Exception as exc:
                        require(getattr(exc, "code", None) == FEATURE_DISABLED, "disallowed tool did not fail with FEATURE_DISABLED")
                    else:
                        raise CheckFailure("disallowed tool was authorized")
                    require(
                        service.authorize_tool(conversation_id, "mcp__workspace__knowledge_search", {"query": SYNTHETIC_QUERY}) is True,
                        "allowed trial knowledge tool was refused",
                    )

                await asyncio.to_thread(tool_boundaries)
                after = {
                    table: service.db.one(f"SELECT COUNT(*) n FROM {table}")["n"]
                    for table in ("runs", "messages", "approvals", "files")
                }
                require(after == before, f"trial denials created side effects: before={before} after={after}")
                return {"denied_route_or_mode_variants": 9, "denied_message_fields": 3, "denied_conversation_field": 1, "side_effects": 0}

            checks = (
                ("trial_deployment_policy", trial_config_policy),
                ("trial_allowed_reads_and_chat", trial_allowed_reads_and_chat),
                ("trial_bm25_and_hybrid", trial_retrieval_modes),
                ("trial_reverse_boundaries", trial_reverse_boundaries),
            )
            for name, operation in checks:
                await _check(report, name, operation)
        return {
            "connects": SyntheticRuntime.connects,
            "executions": SyntheticRuntime.executions,
            "model_calls": SyntheticRuntime.model_calls,
        }
    finally:
        await server.stop()


async def run_trial_smoke(config: TrialSmokeConfig | None = None) -> dict[str, Any]:
    config = config or TrialSmokeConfig()
    generated_at = now()
    temp_owned = config.data_dir is None
    # macOS tempfile may return /var, an OS symlink to /private/var. Resolve
    # this synthetic runner's path before the private-memory boundary checks
    # every ancestor; the application must continue rejecting symlink paths.
    data_dir = (Path(config.data_dir) if config.data_dir else Path(tempfile.mkdtemp(prefix="trial-smoke-"))).resolve()
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "synthetic": True,
        "mode": "synthetic-loopback-http",
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
        "checks": [],
        "runtime": {},
        "external_validation_limits": list(EXTERNAL_VALIDATION_LIMITS),
    }
    state = TrialState()
    server: SyntheticTrialServer | None = None
    try:
        server = await SyntheticTrialServer.start(data_dir, max_active_sessions=8, idle_seconds=30, sqlite_synchronous=config.sqlite_synchronous)
        server.create_admin()
        async with make_client(server.base_url) as client:
            async def health_live():
                payload = await request_json(client, "GET", "/api/v1/health/live")
                require(payload.get("status") == "ok", "live health is not ok")
                return {"status": payload["status"]}

            async def health_ready():
                payload = await request_json(client, "GET", "/api/v1/health/ready")
                require(payload.get("status") == "ready", "ready health is not ready")
                return {"status": payload["status"], "model_configured": payload.get("model_configured")}

            async def login_admin():
                response = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "synthetic-admin", "password": PASSWORD},
                )
                require(response.status_code == 200, f"login returned HTTP {response.status_code}")
                require("httponly" in response.headers.get("set-cookie", "").lower(), "session cookie is not HttpOnly")
                payload = response.json()
                require(payload.get("role") == "admin", "synthetic admin role mismatch")
                return {"user_id": payload["id"], "role": payload["role"]}

            async def model_catalog():
                payload = await request_json(client, "GET", "/api/v1/models")
                require(isinstance(payload, list) and payload, "model catalog is empty")
                require(any(item.get("available") for item in payload), "no available model entry")
                return {"models": len(payload), "available": sum(bool(item.get("available")) for item in payload)}

            async def create_knowledge_base():
                payload = await request_json(
                    client,
                    "POST",
                    "/api/v1/knowledge-bases",
                    expected=(201,),
                    json={
                        "name": "华微试用合成知识库",
                        "description": "仅用于学校云合成烟囱，不含客户数据",
                        "scope": "private",
                    },
                )
                state.kb_id = payload["id"]
                require(payload["can_edit"] is True, "created knowledge base is not editable")
                return {"kb_id": payload["id"]}

            async def upload_synthetic_document():
                require(state.kb_id is not None, "knowledge base prerequisite is missing")
                response = await client.post(
                    f"/api/v1/knowledge-bases/{state.kb_id}/documents",
                    files={"file": ("synthetic-trial-manual.md", SYNTHETIC_DOCUMENT, "text/markdown")},
                )
                require(response.status_code == 201, f"document upload returned HTTP {response.status_code}")
                payload = response.json()
                require(payload["status"] == "ready", "document is not fully indexed")
                require(payload["sha256"] == sha256_bytes(SYNTHETIC_DOCUMENT), "document hash mismatch")
                require(payload["chunk_count"] >= 1, "document produced no chunks")
                state.document_id = payload["id"]
                content = await client.get(payload["content_url"])
                require(content.status_code == 200, "document content is unavailable")
                require(content.content == SYNTHETIC_DOCUMENT, "document content round-trip mismatch")
                return {"document_id": payload["id"], "chunks": payload["chunk_count"], "status": payload["status"]}

            async def retrieval_bm25():
                require(state.kb_id is not None, "knowledge base prerequisite is missing")
                payload = await request_json(
                    client,
                    "POST",
                    f"/api/v1/knowledge-bases/{state.kb_id}/search",
                    json={"query": SYNTHETIC_QUERY, "limit": 6, "mode": "bm25", "context_chars": 512},
                )
                require(payload["retrieval"]["mode"] == "bm25", "retrieval mode mismatch")
                require(payload["retrieval"]["vector_model"] == "local-lexical-v1", "external vector model was selected")
                require(payload["retrieval"]["semantic"] is False, "local lexical fallback must not claim semantic search")
                require(payload["results"], "retrieval returned no results")
                top = payload["results"][0]
                require("P-101" in (top.get("text") or ""), "top retrieval hit does not contain the synthetic marker")
                require(top.get("chunk_id"), "retrieval hit has no chunk id")
                require(payload["context"]["text"], "retrieval context is empty")
                return {
                    "results": len(payload["results"]),
                    "top_chunk": top["chunk_id"],
                    "context_characters": payload["context"]["characters"],
                }

            async def create_assistant():
                require(state.kb_id is not None, "knowledge base prerequisite is missing")
                payload = await request_json(
                    client,
                    "POST",
                    "/api/v1/assistants",
                    expected=(201,),
                    json={
                        "name": "华微试用合成助手",
                        "description": "只使用合成资料的试用助手",
                        "instructions": "只使用合成资料回答，不访问外部系统。",
                        "knowledge_base_ids": [state.kb_id],
                        "connections": [],
                        "web_search_enabled": False,
                    },
                )
                require(payload["knowledge_base_ids"] == [state.kb_id], "assistant knowledge binding mismatch")
                require(payload["connections"] == [], "synthetic assistant must not bind external connections")
                state.assistant_id = payload["id"]
                return {"assistant_id": payload["id"]}

            async def upload_attachment():
                response = await client.post(
                    "/api/v1/files",
                    files={"file": ("synthetic-trial-attachment.txt", SYNTHETIC_ATTACHMENT, "text/plain")},
                )
                require(response.status_code == 201, f"attachment upload returned HTTP {response.status_code}")
                payload = response.json()
                require(payload["size"] == len(SYNTHETIC_ATTACHMENT), "attachment size mismatch")
                download = await client.get(f"/api/v1/files/{payload['id']}/content")
                require(download.status_code == 200, "attachment content is unavailable")
                require(sha256_bytes(download.content) == sha256_bytes(SYNTHETIC_ATTACHMENT), "attachment hash mismatch")
                state.attachment_id = payload["id"]
                return {"file_id": payload["id"], "bytes": payload["size"], "hash_verified": True}

            async def create_conversation():
                require(state.assistant_id is not None, "assistant prerequisite is missing")
                payload = await request_json(
                    client,
                    "POST",
                    "/api/v1/conversations",
                    expected=(201,),
                    json={"assistant_id": state.assistant_id, "title": "华微试用合成烟囱"},
                )
                state.conversation_id = payload["id"]
                return {"conversation_id": payload["id"], "model": payload.get("model_id")}

            async def chat_with_attachment():
                require(state.conversation_id is not None and state.attachment_id is not None, "chat prerequisites are missing")
                started = time.perf_counter()
                sent = await request_json(
                    client,
                    "POST",
                    f"/api/v1/conversations/{state.conversation_id}/messages",
                    expected=(202,),
                    json={
                        "client_request_id": uuid.uuid4().hex,
                        "text": "合成烟囱：请读取附件和知识库",
                        "file_ids": [state.attachment_id],
                    },
                )
                run = await wait_run(client, sent["run_id"], config.timeout)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                require(run["status"] == "succeeded", f"chat run ended as {run['status']}")
                usage = run.get("usage") or {}
                require(usage.get("input_tokens") == 12, "synthetic usage was not recorded")
                messages = await request_json(
                    client, "GET", f"/api/v1/conversations/{state.conversation_id}/messages"
                )
                assistant_messages = [m for m in messages if m["run_id"] == run["id"] and m["role"] == "assistant"]
                require(assistant_messages and "合成回复：" in assistant_messages[0]["text"], "assistant message is missing")
                user_messages = [m for m in messages if m["run_id"] == run["id"] and m["role"] == "user"]
                require(user_messages and user_messages[0]["file_ids"] == [state.attachment_id], "attachment was not bound to the message")
                trace = await request_json(client, "GET", f"/api/v1/runs/{run['id']}/trace")
                types = [item["type"] for item in trace["events"]]
                require("run.started" in types and "run.completed" in types, "run trace events are incomplete")
                return {"run_id": run["id"], "duration_ms": elapsed_ms, "events": len(trace["events"])}

            async def sse_stream():
                require(state.conversation_id is not None, "conversation prerequisite is missing")
                sent = await request_json(
                    client,
                    "POST",
                    f"/api/v1/conversations/{state.conversation_id}/messages",
                    expected=(202,),
                    json={"client_request_id": uuid.uuid4().hex, "text": "合成烟囱：SSE 事件流", "file_ids": []},
                )
                stream = await stream_run_events(client, sent["run_id"], config.timeout)
                types = event_types(stream)
                require("run.started" in types, "SSE stream is missing run.started")
                require("message.delta" in types, "SSE stream is missing message.delta")
                require("run.completed" in types, "SSE stream is missing run.completed")
                ids = [int(item["sse_id"]) for item in stream["events"] if item["sse_id"] is not None]
                require(ids == sorted(ids) and len(set(ids)) == len(ids), "SSE event ids are not strictly increasing")
                require(stream["connections"] == 1, "SSE stream needed a reconnect on a healthy loopback")
                completed = sse_payloads(stream, "run.completed")
                require(completed and completed[-1].get("data", {}).get("status") == "succeeded", "SSE completion status mismatch")
                return {
                    "first_byte_ms": stream["first_byte_ms"],
                    "total_ms": stream["total_ms"],
                    "events": len(stream["events"]),
                    "connections": stream["connections"],
                }

            async def cancel_run():
                require(state.conversation_id is not None, "conversation prerequisite is missing")
                sent = await request_json(
                    client,
                    "POST",
                    f"/api/v1/conversations/{state.conversation_id}/messages",
                    expected=(202,),
                    json={"client_request_id": uuid.uuid4().hex, "text": "wait", "file_ids": []},
                )
                await wait_run(client, sent["run_id"], config.timeout, statuses={"running"})
                started = time.perf_counter()
                cancelled = await client.post(f"/api/v1/runs/{sent['run_id']}/cancel")
                require(cancelled.status_code == 200, f"cancel returned HTTP {cancelled.status_code}")
                final = await wait_run(client, sent["run_id"], config.timeout)
                elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
                require(final["status"] == "cancelled", f"cancelled run ended as {final['status']}")
                conversation = await request_json(
                    client, "GET", f"/api/v1/conversations/{state.conversation_id}"
                )
                require(conversation.get("active_run_id") is None, "conversation still has an active run after cancellation")
                followup = await request_json(
                    client,
                    "POST",
                    f"/api/v1/conversations/{state.conversation_id}/messages",
                    expected=(202,),
                    json={"client_request_id": uuid.uuid4().hex, "text": "取消后继续", "file_ids": []},
                )
                followup_run = await wait_run(client, followup["run_id"], config.timeout)
                require(followup_run["status"] == "succeeded", "conversation is not reusable after cancellation")
                return {"run_id": sent["run_id"], "cancel_ms": elapsed_ms, "reusable": True}

            async def conversation_isolation():
                require(state.assistant_id is not None and state.conversation_id is not None, "conversation prerequisites are missing")
                marker = "隔离标记-SESSION-B"
                isolated = await request_json(
                    client,
                    "POST",
                    "/api/v1/conversations",
                    expected=(201,),
                    json={"assistant_id": state.assistant_id, "title": "华微试用会话隔离"},
                )
                sent = await request_json(
                    client,
                    "POST",
                    f"/api/v1/conversations/{isolated['id']}/messages",
                    expected=(202,),
                    json={"client_request_id": uuid.uuid4().hex, "text": marker, "file_ids": []},
                )
                run = await wait_run(client, sent["run_id"], config.timeout)
                require(run["status"] == "succeeded", "isolated conversation run did not succeed")
                first_messages = await request_json(
                    client, "GET", f"/api/v1/conversations/{state.conversation_id}/messages"
                )
                second_messages = await request_json(
                    client, "GET", f"/api/v1/conversations/{isolated['id']}/messages"
                )
                require(all(marker not in item["text"] for item in first_messages), "conversation A leaked conversation B's marker")
                require(any(marker in item["text"] for item in second_messages), "conversation B did not retain its marker")
                first_run_ids = {item["run_id"] for item in first_messages if item.get("run_id")}
                second_run_ids = {item["run_id"] for item in second_messages if item.get("run_id")}
                require(first_run_ids.isdisjoint(second_run_ids), "conversation histories share run ids")
                return {
                    "conversation_a": state.conversation_id,
                    "conversation_b": isolated["id"],
                    "marker_visible_in_a": False,
                    "marker_visible_in_b": True,
                    "run_ids_disjoint": True,
                }

            async def usage_endpoint():
                payload = await request_json(client, "GET", "/api/v1/usage")
                require(payload["total_runs"] >= 3, "usage did not aggregate the synthetic runs")
                known = payload["known_subtotals"]
                require(known["input_tokens"] >= 24, "known synthetic input tokens are missing")
                # The cancelled wait never emits usage. Its contribution is
                # unknown, so the account total cannot be the known subtotal.
                require(payload["missing_usage_runs"] >= 1, "cancelled usage was not kept missing")
                require(payload["usage_complete"] is False, "partial usage was marked complete")
                require(all(payload[field] is None for field in ("input_tokens", "output_tokens", "estimated_cost_usd")),
                        "unknown cancelled usage became a complete total")
                return {
                    "total_runs": payload["total_runs"],
                    "successful_runs": payload["successful_runs"],
                    "input_tokens": payload["input_tokens"],
                    "known_subtotals": known,
                    "missing_usage_runs": payload["missing_usage_runs"],
                    "usage_complete": payload["usage_complete"],
                }

            async def member_isolation():
                require(state.kb_id is not None, "knowledge base prerequisite is missing")
                await request_json(
                    client,
                    "POST",
                    "/api/v1/users",
                    expected=(201,),
                    json={
                        "username": "synthetic-member",
                        "display_name": "合成试用成员",
                        "password": PASSWORD,
                        "role": "member",
                    },
                )
                async with make_client(server.base_url) as member:
                    response = await member.post(
                        "/api/v1/auth/login",
                        json={"username": "synthetic-member", "password": PASSWORD},
                    )
                    require(response.status_code == 200, f"member login returned HTTP {response.status_code}")
                    bases = await request_json(member, "GET", "/api/v1/knowledge-bases")
                    require(bases == [], "member can see the admin private knowledge base")
                    denied = await member.post(
                        f"/api/v1/knowledge-bases/{state.kb_id}/search",
                        json={"query": SYNTHETIC_QUERY, "mode": "bm25"},
                    )
                    require(denied.status_code == 404, "member search on a private knowledge base was not denied")
                    await request_json(member, "POST", "/api/v1/auth/logout")
                return {"private_kb_hidden": True, "search_denied": True}

            async def logout_admin():
                await request_json(client, "POST", "/api/v1/auth/logout")
                response = await client.get("/api/v1/auth/me")
                require(response.status_code == 401, "session is still authenticated after logout")
                return {"status": 401}

            checks = (
                ("health_live", health_live),
                ("health_ready", health_ready),
                ("login_admin", login_admin),
                ("model_catalog", model_catalog),
                ("create_knowledge_base", create_knowledge_base),
                ("upload_synthetic_document", upload_synthetic_document),
                ("retrieval_bm25", retrieval_bm25),
                ("create_assistant", create_assistant),
                ("upload_attachment", upload_attachment),
                ("create_conversation", create_conversation),
                ("chat_with_attachment", chat_with_attachment),
                ("sse_stream", sse_stream),
                ("cancel_run", cancel_run),
                ("conversation_isolation", conversation_isolation),
                ("usage_endpoint", usage_endpoint),
                ("member_isolation", member_isolation),
                ("logout_admin", logout_admin),
            )
            for name, operation in checks:
                await _check(report, name, operation)
        normal_runtime = {
            "connects": SyntheticRuntime.connects,
            "executions": SyntheticRuntime.executions,
            "model_calls": SyntheticRuntime.model_calls,
        }
        await server.stop()
        server = None
        trial_runtime = await _run_trial_policy_phase(data_dir, report, state)
        report["runtime"] = {
            "connects": normal_runtime["connects"] + trial_runtime["connects"],
            "executions": normal_runtime["executions"] + trial_runtime["executions"],
            "model_calls": normal_runtime["model_calls"] + trial_runtime["model_calls"],
            "phases": {"normal": normal_runtime, "trial": trial_runtime},
        }
    finally:
        if server is not None:
            await server.stop()
        report["summary"] = {
            "passed": sum(item["status"] == "passed" for item in report["checks"]),
            "failed": sum(item["status"] == "failed" for item in report["checks"]),
            "total": len(report["checks"]),
        }
        report["status"] = "passed" if report["summary"]["failed"] == 0 else "failed"
        report["data_dir"] = str(data_dir)
        if temp_owned and not config.keep_data:
            shutil.rmtree(data_dir, ignore_errors=True)
            report["data_dir_retained"] = False
        else:
            report["data_dir_retained"] = True
        if config.report_path:
            path = Path(config.report_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the synthetic Huawei trial vertical-slice smoke.")
    parser.add_argument("--report", type=Path, help="Write the JSON report to this path.")
    parser.add_argument("--data-dir", type=Path, help="Use this temporary data directory instead of an owned temp dir.")
    parser.add_argument("--keep-data", action="store_true", help="Keep a script-owned temporary data directory after the run.")
    parser.add_argument("--timeout", type=float, default=10.0, help="Per-run terminal timeout in seconds.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = asyncio.run(
        run_trial_smoke(
            TrialSmokeConfig(
                data_dir=args.data_dir,
                keep_data=args.keep_data,
                report_path=args.report,
                timeout=args.timeout,
            )
        )
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "passed": report["summary"]["passed"],
                "failed": report["summary"]["failed"],
                "model_calls": report["runtime"].get("model_calls", 0),
                "report": str(args.report) if args.report else None,
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
