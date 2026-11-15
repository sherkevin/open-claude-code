"""Opt-in official DeepSeek Harness transport with a private process per chat."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import context_binding, harness_context_receipt, harness_context_snapshot, save_context
from .db import dump, offload, offload_write, uid
from .harness_attachments import AttachmentPrompt
from .harness_tools import HarnessToolBridge
from .identity import build_system_prompt
from .runtime import WorkspaceRuntime, elapsed_ms, save_timing
from .provider_policy import TOKENHUB_BASE_URL, provider_route
from .security import AppError
from .token_savings import TokenAccounting

PINNED_COMMIT = "fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"


class _EmptyResult(BaseModel):
    pass


_ContextNumber = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000_000)]


class _ContextBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    systemTokens: _ContextNumber
    toolsTokens: _ContextNumber
    messageTokens: _ContextNumber


class _ContextResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_: Literal["workspace-context/v1"] = Field(alias="schema")
    sourceCommit: Literal[PINNED_COMMIT]
    sessionId: str = Field(min_length=1, max_length=128)
    routeMatches: bool
    projectionSeq: int = Field(ge=0, lt=100_000)
    totalTokens: _ContextNumber | None
    # The pinned adapter erases whether usage fields were originally present.
    observedPromptTokens: None
    baselineKind: Literal["usage", "estimated", "none"]
    breakdown: _ContextBreakdown | None

    @model_validator(mode="after")
    def consistent(self):
        known = self.routeMatches and self.baselineKind != "none"
        if known != (self.totalTokens is not None and self.breakdown is not None):
            raise ValueError("Inconsistent context measurement")
        if not known and (self.totalTokens is not None or self.observedPromptTokens is not None or self.breakdown is not None or self.baselineKind != "none"):
            raise ValueError("Unknown context must not contain measurements")
        return self


class _CompactCommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["success", "error"]
    text: str | None = Field(default=None, max_length=2048)
    sourceEventSeq: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.kind == "error" and (self.text is None or not self.text.strip()):
            raise ValueError("Compaction error requires text")
        if self.kind == "success" and self.sourceEventSeq is not None and self.text is None:
            raise ValueError("Compaction success source event requires text")
        return self


class _CompactExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    commandId: str = Field(min_length=1, max_length=128)
    result: _CompactCommandResult


class _CompactionUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    inputTokens: int = Field(ge=0, le=1_000_000_000_000)
    outputTokens: int = Field(ge=0, le=1_000_000_000_000)
    totalTokens: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
    cacheReadTokens: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
    cacheWriteTokens: int | None = Field(default=None, ge=0, le=1_000_000_000_000)
    reasoningTokens: int | None = Field(default=None, ge=0, le=1_000_000_000_000)


def _event_compaction(notifications, session_id, command_id):
    """Validate the lifecycle and summary frames without trusting model text."""
    command_run = compaction_start = summary = ended = done = None
    summary_seq = None
    saw_compaction_event = False
    for notification in notifications:
        if notification.method != "session.event":
            continue
        payload = notification.payload
        if not isinstance(payload, dict) or payload.get("sessionId") != session_id:
            continue
        event = payload.get("event")
        if not isinstance(event, dict):
            continue
        kind, data = event.get("type"), event.get("data")
        if not isinstance(data, dict):
            continue
        if kind in {"command/run", "command/done"}:
            if data.get("commandId") != command_id:
                continue
            if kind == "command/run":
                if (command_run is not None or data.get("name") != "compact"
                        or type(data.get("args", "")) is not str
                        or len(data.get("args", "")) > 2001):
                    raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩命令生命周期无效")
                command_run = data
            else:
                if (done is not None or data.get("kind") not in {"success", "error"}
                        or type(data.get("text", "")) is not str
                        or len(data.get("text", "")) > 2048
                        or (data.get("sourceEventSeq") is not None
                            and (type(data.get("sourceEventSeq")) is not int
                                 or data["sourceEventSeq"] < 0))):
                    raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩完成事件无效")
                done = data
            continue
        if kind not in {"compaction/start", "compaction/summary", "compaction/end"}:
            continue
        saw_compaction_event = True
        if data.get("sourceCommandId") != command_id:
            raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩事件与命令不匹配")
        compaction_id = data.get("compactionId")
        if not isinstance(compaction_id, str) or not 1 <= len(compaction_id) <= 128:
            raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩事务标识无效")
        if kind == "compaction/start":
            if compaction_start is not None or data.get("turn") is not None:
                raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩开始事件无效")
            compaction_start = data
        elif kind == "compaction/summary":
            if summary is not None or data.get("turn") is not None:
                raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩摘要事件重复或无效")
            blocks = data.get("summary")
            shadowed = data.get("shadowedSeqs")
            if (not isinstance(blocks, list) or not blocks or len(blocks) > 100
                    or any(not isinstance(block, dict) or set(block) != {"type", "text"}
                           or block.get("type") != "text"
                           or not isinstance(block.get("text"), str)
                           or len(block["text"]) > 1_000_000 for block in blocks)
                    or not isinstance(shadowed, list) or not shadowed or len(shadowed) > 10_000
                    or any(type(seq) is not int or seq < 0 for seq in shadowed)
                    or len(set(shadowed)) != len(shadowed)
                    or type(data.get("shadowedTokenCount")) is not int
                    or data["shadowedTokenCount"] < 0
                    or data["shadowedTokenCount"] > 1_000_000_000_000
                    or not isinstance(data.get("provider"), str) or not 1 <= len(data["provider"]) <= 256
                    or not isinstance(data.get("model"), str) or not 1 <= len(data["model"]) <= 256):
                raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩摘要数据无效")
            usage = data.get("usage")
            if usage is not None:
                try:
                    _CompactionUsage.model_validate(usage)
                except ValueError:
                    raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩用量数据无效") from None
            summary = data
            summary_seq = event.get("seq")
        else:
            if (ended is not None or data.get("error") is not None
                    and (not isinstance(data.get("error"), str) or len(data["error"]) > 2048)):
                raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩结束事件重复")
            ended = data
    if command_run is None or done is None:
        raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩命令生命周期不完整")
    if compaction_start is None and ended is not None:
        raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩结束事件缺少开始事件")
    if compaction_start is not None and ended is None:
        raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩事务缺少结束事件")
    if summary is not None:
        if compaction_start is None or ended is None or summary_seq is None:
            raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩摘要缺少完整事务")
        ids = {compaction_start.get("compactionId"), summary.get("compactionId"), ended.get("compactionId")}
        if len(ids) != 1:
            raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩事务事件不匹配")
    elif saw_compaction_event and (compaction_start is not None or ended is not None):
        ids = {compaction_start.get("compactionId"), ended.get("compactionId")}
        if len(ids) != 1:
            raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩事务事件不匹配")
    return {"command_run": command_run, "started": compaction_start, "summary": summary,
            "ended": ended, "done": done, "summary_seq": summary_seq}


def official_client(source: Path):
    """Reuse the reviewed upstream transport without importing a global SDK."""
    try:
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        if commit != PINNED_COMMIT:
            raise ValueError("wrong source revision")
        subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        package = source / "python/sdk/src/deepseek_harness"
        paths = list(package.glob("*.py"))
        if not paths:
            raise ValueError("missing Python SDK")
        for path in paths:
            expected = subprocess.check_output(["git", "-C", str(source), "show", f"{commit}:{path.relative_to(source).as_posix()}"], stderr=subprocess.DEVNULL)
            if path.read_bytes() != expected:
                raise ValueError("modified Python SDK")
        sys.path.insert(0, str(package.parent))
        try:
            module = importlib.import_module("deepseek_harness.client")
        finally:
            sys.path.remove(str(package.parent))
        if not Path(module.__file__).resolve().is_relative_to(package.resolve()):
            raise ValueError("unexpected Python SDK import")
        return module.HarnessClient, module.HarnessConfig
    except Exception:
        raise AppError(503, "HARNESS_SOURCE_UNAVAILABLE", "Harness 固定版本源码尚未安装或校验未通过") from None


def write_launcher(directory: Path, node: Path, source: Path, root: Path):
    """The SDK merges env; an isolated Python execve wrapper clears inheritance.

    No shell is involved. The credential stays in process memory/environment,
    never in the generated launcher, command arguments or Cordis configuration.
    """
    environment = {"PATH": f"{node.parent}:/usr/bin:/bin", "HOME": str(directory),
                   "DSH_HOME": str(directory), "XDG_CONFIG_HOME": str(directory / "config"),
                   "XDG_CACHE_HOME": str(directory / "cache"), "TMPDIR": str(directory / "tmp"),
                   "LANG": "C.UTF-8", "TSX_TSCONFIG_PATH": str(source / "tsconfig.json")}
    for name in ("config", "cache", "tmp"):
        (directory / name).mkdir(mode=0o700, exist_ok=True)
    arguments = [str(node), "--import", str(source / "node_modules/tsx/dist/esm/index.mjs"),
                 str(source / "apps/cli/src/bin.ts")]
    path = directory / "launch.py"
    path.write_text("#!/usr/bin/python3 -I\nimport os, sys\n"
                    f"env = {environment!r}\n"
                    "env['WORKSPACE_MODEL_API_KEY'] = os.environ.get('WORKSPACE_MODEL_API_KEY', '')\n"
                    f"os.chdir({str(root)!r})\n"
                    f"os.execve({str(node)!r}, {arguments!r} + sys.argv[1:], env)\n", encoding="utf-8")
    path.chmod(0o700)
    return path


class HarnessRuntime(WorkspaceRuntime):
    # A process is owned by exactly one run. Resume from the private durable
    # session on the next turn; delayed RPCs cannot cross an approval boundary.
    close_after_run = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = self.config.get("native_session_id") or f"workspace-{uid()}"
        self.interrupted = False
        self.request_task = None
        self.tool_task = None
        self.tool_call_id = None
        self.closing = asyncio.Lock()
        self.owned_process = None
        self.bridge = HarnessToolBridge(self)

    async def connect(self):
        from harness.profile import write_profile

        settings = self.service.settings
        if self.config.get("runtime_type") != "harness":
            raise AppError(409, "HARNESS_SESSION_MISMATCH", "此会话不属于 Harness 执行器")
        if not settings.harness_node_path or not settings.harness_source_dir:
            raise AppError(503, "HARNESS_NOT_CONFIGURED", "Harness 隔离运行环境尚未配置")
        source, node = Path(settings.harness_source_dir).resolve(), Path(settings.harness_node_path).resolve()
        if not node.is_file() or not os.access(node, os.X_OK):
            raise AppError(503, "HARNESS_NOT_CONFIGURED", "Harness 专用 Node 运行环境不可用")
        provider = self.config["provider_env"]
        route = provider_route(provider)
        test_mode = bool(getattr(settings, "provider_test_mode", False))
        if not (route.production_ready or (test_mode and route.allowed)):
            raise AppError(503, "TOKENHUB_ROUTE_REQUIRED", "模型服务线路未满足安全部署策略，请联系管理员")
        api_key = provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN")
        if not api_key:
            raise AppError(503, "HARNESS_PROVIDER_NOT_CONFIGURED", "Harness 需要已配置的模型线路，请联系管理员")
        effort = self.config.get("upstream_effort") or "off"
        if effort not in {"off", "low", "high", "max"}:
            raise AppError(409, "HARNESS_REASONING_UNSUPPORTED", "Harness 尚不支持此推理档位，请选择默认、low、high 或 max")
        root = settings.data_dir / "workspaces" / self.conversation_id
        private = settings.data_dir / "harness" / self.conversation_id
        for directory in (root, root / "artifacts", private):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls, config_cls = await asyncio.to_thread(official_client, source)
        profile = write_profile(
            profile_root=private, source_dir=source, cwd=root, session_id=self.session_id,
            system_prompt=build_system_prompt(public_model_id=self.config["public_model_id"],
                public_model_label=self.config["public_model_label"], assistant_instructions=self.config["instructions"]),
            provider={"baseURL": provider.get("ANTHROPIC_BASE_URL", TOKENHUB_BASE_URL),
                      "model": self.config["upstream_model"], "apiKeyEnv": "WORKSPACE_MODEL_API_KEY", "reasoningEffort": effort,
                      "maxTokens": settings.harness_max_output_tokens},
            tools=self.bridge.definitions(), max_steps=settings.max_turns,
            tool_timeout_ms=(settings.approval_seconds + 60) * 1000,
        )
        launcher = write_launcher(private, node, source, root)
        self.client = cls(config_cls(dsh_bin=str(launcher), profile=profile["profile"],
            dsh_home=str(private), cwd=str(root), env={"WORKSPACE_MODEL_API_KEY": api_key},
            initialize_timeout_seconds=60, request_timeout_seconds=60, shutdown_timeout_seconds=1))
        try:
            await asyncio.to_thread(self.client.start)
            self.owned_process = self.client._proc
            self.request_task = asyncio.create_task(self._requests(self.client))
            await asyncio.to_thread(self.client.initialize, cwd=str(root), provider="deepseek-official",
                                    model=self.config["upstream_model"], reasoning_effort=effort,
                                    max_tokens=settings.harness_max_output_tokens, approval_requests=True)
            if self.config.get("native_session_id"):
                await asyncio.to_thread(self.client.resume_session, self.session_id)
        except BaseException:
            await self.close()
            raise

    async def _requests(self, client):
        try:
            while True:
                request = await asyncio.to_thread(client.next_request)
                try:
                    if request.method == "workspace/tool":
                        expected_run_id = (self.current_run or {}).get("id")
                        self.tool_call_id = request.payload.get("callId")
                        self.tool_task = asyncio.create_task(self.bridge.invoke(request.payload))
                        try:
                            result = await self.tool_task
                            await asyncio.to_thread(self.bridge.respond, client, request.id, request.payload, result, expected_run_id)
                        except asyncio.CancelledError:
                            if self.interrupted or self.client is None:
                                raise
                            await asyncio.to_thread(client.respond_error, request.id, code=-32800, message="Workspace operation cancelled")
                        finally:
                            self.tool_task, self.tool_call_id = None, None
                    elif request.method == "approval/request":
                        await asyncio.to_thread(client.respond, request.id, {"outcome": "rejected"})
                    else:
                        await asyncio.to_thread(client.respond_error, request.id, code=-32601, message="Host method denied")
                except AppError:
                    await asyncio.to_thread(client.respond_error, request.id, code=-32602, message="Workspace operation denied")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.to_thread(client.respond_error, request.id, code=-32603, message="Workspace operation failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            # The same transport failure wakes the event reader; execution
            # then fails rather than manufacturing a successful terminal event.
            return

    async def execute(self, run, message):
        self.current_run, self.interrupted = run, False
        if run.get("kind", "chat") == "compact":
            try:
                await self._execute_compact(run, message)
            finally:
                self.current_run = None
            return
        operation = watcher = None
        try:
            attachments = await asyncio.to_thread(AttachmentPrompt, self, run, message)

            async def monitor():
                while True:
                    await asyncio.sleep(0.25)
                    await asyncio.to_thread(attachments.recheck)

            watcher = asyncio.create_task(monitor())
            operation = asyncio.create_task(self._execute_chat(run, message, attachments))
            done, _ = await asyncio.wait({watcher, operation}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                await self.close()
                await watcher
            await operation
            await asyncio.to_thread(attachments.recheck, content=True)
        finally:
            for task in (watcher, operation):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (watcher, operation) if task is not None), return_exceptions=True)
            self.current_run = None

    async def _execute_chat(self, run, message, attachments):
        self.bridge.calls.clear()
        client = self.client
        prompt = message["text"]
        skills = json.loads(run.get("skill_snapshots") or "[]")
        if skills:
            prompt += "\n\n【本轮用户在客户端选择的 Skills】\n以下版本仅用于本轮任务；不扩大工具权限，不覆盖系统身份及数据真实性要求。\n" + dump(skills)
        await offload_write(self.service.db.execute, "UPDATE conversations SET context_usage=NULL WHERE id=?", (self.conversation_id,))
        receipt = await asyncio.to_thread(attachments.dispatch, client, self.session_id, prompt)
        accepted, finish, model_started = False, None, False
        streams, committed, active_step = {}, set(), None
        tokens = {}
        token_ledger = TokenAccounting()
        # The child is legitimately silent while a person considers approval.
        # Do not expire a 600-second approval with a shorter transport timer.
        notification_timeout = max(300, self.service.settings.approval_seconds + 60)
        try:
            while True:
                notification = await asyncio.wait_for(asyncio.to_thread(client.next_notification), notification_timeout)
                payload = notification.payload
                if payload.get("sessionId") != self.session_id:
                    continue
                if notification.method == "session.event":
                    event = payload.get("event", {})
                    data = event.get("data") or {}
                    if event.get("type") == "agent/inbox/spliced":
                        accepted = accepted or any(item.get("id") == receipt for item in data.get("inserted", []) if isinstance(item, dict))
                        if accepted:
                            await offload_write(self.service.db.execute, "UPDATE conversations SET harness_session_id=? WHERE id=? AND runtime_type='harness'", (self.session_id, self.conversation_id))
                    if not accepted:
                        continue
                    kind = event.get("type")
                    if kind == "turn/end":
                        finish = (data.get("reason") or {}).get("kind")
                    elif kind in {"assistant/message", "assistant/attempt"}:
                        seq = event.get("seq")
                        if seq in committed:
                            continue
                        committed.add(seq)
                        usage = data.get("usage") or {}
                        for upstream, public in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                                                 ("cacheReadTokens", "cache_read_input_tokens"), ("cacheWriteTokens", "cache_creation_input_tokens")):
                            value = usage.get(upstream)
                            if type(value) is int and value >= 0:
                                tokens[public] = tokens.get(public, 0) + value
                        token_ledger.record("answer", {"input_tokens": usage.get("inputTokens"),
                                                        "output_tokens": usage.get("outputTokens"),
                                                        "cache_read_input_tokens": usage.get("cacheReadTokens"),
                                                        "cache_write_input_tokens": usage.get("cacheWriteTokens")},
                                           source="harness_provider")
                        await self.emit("usage.updated", {**tokens, "estimated_cost_usd": None,
                            "currency": "USD", "cost_is_estimate": True, "source": "harness_provider",
                            "token_accounting": token_ledger.summary()})
                        if kind != "assistant/message":
                            continue
                        message_data = data.get("message") or data
                        text = "".join(block.get("text", "") for block in message_data.get("content", []) if block.get("type") == "text")
                        prior = streams.get(active_step, "")
                        remainder = text[len(prior):] if text.startswith(prior) else ("" if prior else text)
                        if remainder:
                            await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": remainder})
                        active_step = None
                    # Keep only event types and counters in the application
                    # trace; provider frames may contain credential echoes.
                    await offload_write(self.service.db.event, run["id"], "harness.event", {"type": kind, "seq": event.get("seq")})
                elif notification.method == "workspace.stream" and accepted:
                    key = str(payload.get("stepId", ""))
                    if payload.get("event") == "start":
                        active_step = key
                    if not model_started:
                        model_started = True
                        timings = await offload_write(save_timing, self.service, run["id"], "first_model_event_ms", elapsed_ms(run))
                        await self.emit("timing.updated", timings)
                        await self.emit("run.stage", {"stage": "model_started"})
                    if payload.get("event") == "text_delta" and isinstance(payload.get("text"), str):
                        text = payload["text"]
                        streams[key] = streams.get(key, "") + text
                        await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": text})
                    elif payload.get("event") == "end" and (payload.get("finishObserved") is not True or (payload.get("outcome") or {}).get("kind") == "abandoned"):
                        raise AppError(502, "HARNESS_STREAM_INCOMPLETE", "模型回复提前结束，可重新发送消息继续会话")
                elif notification.method == "session.status" and accepted and payload.get("status") == "idle":
                    if finish != "completed":
                        raise AppError(502, "HARNESS_TURN_FAILED", "本次任务未完成，请重试。若仍失败，请联系管理员并提供执行记录。")
                    await asyncio.to_thread(client.request, "session/wait", {"sessionId": self.session_id}, response_model=_EmptyResult)
                    # This host receipt authenticates the configuration of the
                    # successful request; never backfill it for older sessions.
                    metadata = json.loads((await offload(self.service.require, "runs", run["id"])).get("execution_meta") or "{}")
                    metadata["context_binding"] = context_binding(self.config, self.session_id)
                    await offload_write(self.service.db.execute, "UPDATE runs SET execution_meta=? WHERE id=?", (dump(metadata), run["id"]))
                    return
                elif notification.method == "workspace.limit":
                    raise AppError(409, "HARNESS_STEP_LIMIT", "Harness 已达到本次任务的执行步数上限")
                elif notification.method == "workspace.tool_cancelled":
                    if self.tool_task and payload.get("callId") == self.tool_call_id:
                        self.tool_task.cancel()
        except Exception:
            if self.interrupted:
                raise asyncio.CancelledError() from None
            raise
        finally:
            self.current_run = None

    async def _compact_before(self):
        try:
            return await self.context_usage()
        except Exception:
            return harness_context_snapshot(error="压缩前上下文统计不可用")

    async def _compact_usage(self, run, usage):
        tokens = {}
        for upstream, public in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                                 ("cacheReadTokens", "cache_read_input_tokens"),
                                 ("cacheWriteTokens", "cache_creation_input_tokens")):
            value = usage.get(upstream) if isinstance(usage, dict) else None
            if type(value) is int and value >= 0:
                tokens[public] = tokens.get(public, 0) + value
        if not tokens:
            return
        row = await offload(self.service.require, "runs", run["id"])
        current = json.loads(row.get("usage") or "{}")
        await self.emit("usage.updated", {**tokens, "estimated_cost_usd": None, "currency": "USD",
                                          "cost_is_estimate": True, "source": "harness_provider"})
        for key, value in tokens.items():
            current[key] = current.get(key, 0) + value
        await offload_write(self.service.db.execute, "UPDATE runs SET usage=? WHERE id=?", (dump(current), run["id"]))

    def _recheck_compaction_run(self, run):
        current = self.service.require("runs", run["id"])
        conversation = self.service.require("conversations", self.conversation_id)
        if (current.get("kind") != "compact" or current.get("status") != "running"
                or current.get("user_id") != conversation.get("user_id")
                or conversation.get("active_run_id") != run["id"]
                or conversation.get("runtime_type") != "harness"
                or conversation.get("harness_session_id") != self.session_id):
            raise AppError(409, "CONTEXT_NOT_READY", "上下文压缩任务已失效或会话状态发生变化")
        config = self.service.runtime_config(self.conversation_id)
        if (config.get("native_session_id") != self.session_id
                or config.get("fingerprint") != self.config.get("fingerprint")):
            raise AppError(409, "CONFIG_CHANGED", "模型、权限或助手配置已变化，本次压缩不报告成功")
        return config

    @staticmethod
    def _consume_task_result(operation):
        try:
            operation.result()
        except BaseException:
            pass

    async def _settle_compaction_request(self, operation, timeout_seconds=5):
        """Close and reap the owned client before abandoning an in-flight RPC."""
        if not operation.done():
            try:
                await asyncio.wait_for(asyncio.shield(operation), timeout_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                # The SDK request has its own deadline and the closed transport
                # wakes its waiter. Consume any eventual exception even if the
                # host task itself cannot wait longer.
                operation.add_done_callback(self._consume_task_result)
        if operation.done():
            await asyncio.gather(operation, return_exceptions=True)

    async def _execute_compact(self, run, message):
        stored = await offload(self.service.require, "runs", run["id"])
        conversation = await offload(self.service.require, "conversations", self.conversation_id)
        if (stored["kind"] != "compact" or conversation.get("harness_session_id") != self.session_id
                or conversation.get("active_run_id") != run["id"]):
            raise AppError(409, "CONTEXT_NOT_READY", "此会话还没有可整理的原生上下文")
        await offload(self._recheck_compaction_run, run)
        metadata = json.loads(stored.get("execution_meta") or "{}")
        instructions = metadata.get("compaction_instructions", "")
        if (not isinstance(instructions, str) or len(instructions) > 2000
                or any(ord(char) < 32 and char not in "\t\n\r" for char in instructions)):
            raise AppError(422, "COMPACTION_INSTRUCTIONS_INVALID", "压缩要求无效")

        before = await self._compact_before()
        progress = {"status": "started", "trigger": "manual",
                    "pre_tokens": before.get("total_tokens") if isinstance(before, dict) else None,
                    "before": before, "after": None}
        await offload_write(self.service.db.execute, "UPDATE runs SET compaction=? WHERE id=?", (dump(progress), run["id"]))
        await self.emit("context.compaction_started", {"trigger": "manual"})

        client = self.client
        if client is None:
            raise AppError(503, "HARNESS_NOT_CONFIGURED", "Harness 上下文压缩执行器不可用")
        notifications = []
        subscription = client.subscribe_session_notifications(self.session_id)
        timeout = self.service.settings.harness_compaction_timeout_seconds
        operation = asyncio.create_task(asyncio.to_thread(
            client.request, "workspace/compact",
            {"sessionId": self.session_id, "instructions": instructions},
            response_model=_CompactExecution, timeout_seconds=timeout,
        ))
        settled = False
        try:
            try:
                response = await asyncio.wait_for(operation, timeout=timeout + 2)
            except asyncio.TimeoutError:
                await self.close()
                await self._settle_compaction_request(operation)
                settled = True
                raise AppError(504, "HARNESS_COMPACTION_TIMEOUT", "上下文压缩超时，执行进程已关闭") from None
            except asyncio.CancelledError:
                await self.close()
                await self._settle_compaction_request(operation)
                settled = True
                raise
            except Exception:
                if self.interrupted:
                    raise asyncio.CancelledError() from None
                await self.close()
                await self._settle_compaction_request(operation)
                settled = True
                raise AppError(502, "HARNESS_COMPACTION_FAILED", "上下文压缩执行失败，本次未报告为已压缩") from None

            subscription.drain(notifications.append)
            events = _event_compaction(notifications, self.session_id, response.commandId)
            if events["done"].get("kind") != response.result.kind:
                raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩结果与完成事件不一致")

            summary = events["summary"]
            ended = events["ended"]
            started = events["started"]
            if summary is None:
                if response.result.kind == "error":
                    if (started is None) != (ended is None):
                        raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩失败事务生命周期不完整")
                    if started is not None and ended.get("error") is None:
                        raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩失败事务缺少错误证据")
                    if response.result.sourceEventSeq is not None:
                        raise AppError(502, "HARNESS_COMPACTION_INVALID", "压缩失败时不能引用摘要事件")
                    await offload(self._recheck_compaction_run, run)
                    raise AppError(502, "HARNESS_COMPACTION_FAILED", "上下文压缩未完成，请检查会话状态后重试")
                if started is not None or ended is not None:
                    raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩事务证据不完整")
                if response.result.sourceEventSeq is not None:
                    raise AppError(502, "HARNESS_COMPACTION_INVALID", "未发生压缩时不能引用摘要事件")
            else:
                if (response.result.kind != "success" or response.result.sourceEventSeq != events["summary_seq"]
                        or ended.get("error") is not None
                        or events["done"].get("sourceEventSeq") != events["summary_seq"]):
                    raise AppError(502, "HARNESS_COMPACTION_INVALID", "上下文压缩成功证据不一致")
            boundary_observed = summary is not None
            if boundary_observed:
                boundary = {
                    "trigger": "manual",
                    "pre_tokens": progress["pre_tokens"],
                    "shadowed_items": len(summary["shadowedSeqs"]),
                    "shadowed_tokens": summary["shadowedTokenCount"],
                }
                progress.update(boundary, status="completed", boundary_observed=True)
                await offload_write(self.service.db.execute, "UPDATE runs SET compaction=? WHERE id=?", (dump(progress), run["id"]))
                await self.emit("context.compacted", boundary)
                await self._compact_usage(run, summary.get("usage") or {})

            await offload(self._recheck_compaction_run, run)
            after = await self._compact_before()
            await offload(self._recheck_compaction_run, run)
            status = "completed" if boundary_observed else "not_needed"
            value = {**progress, "status": status, "trigger": "manual",
                     "boundary_observed": boundary_observed, "after": after}
            if not boundary_observed:
                value.update(error=None, followup_status=None)
            await offload_write(self.service.db.execute, "UPDATE runs SET compaction=? WHERE id=?", (dump(value), run["id"]))
            await self.emit("context.compaction_result", value)
        finally:
            subscription.close()
            if settled or operation.done():
                await self._settle_compaction_request(operation)
            else:
                operation.add_done_callback(self._consume_task_result)

    async def context_usage(self):
        binding = context_binding(self.config, self.session_id)
        receipt = await offload(harness_context_receipt, self.service, self.conversation_id)
        if receipt != binding:
            return await offload_write(save_context, self.service, self.conversation_id, harness_context_snapshot(stale_reason="route_changed" if receipt else "request_unobserved"))
        current = await offload(self.service.runtime_config, self.conversation_id)
        if context_binding(current) != binding:
            return await offload_write(save_context, self.service, self.conversation_id, harness_context_snapshot(stale_reason="route_changed"))
        raw = await asyncio.to_thread(self.client.request, "workspace/context", {"sessionId": self.session_id},
                                      response_model=_ContextResult, timeout_seconds=15)
        if not isinstance(raw, _ContextResult) or raw.sessionId != self.session_id:
            raise AppError(502, "HARNESS_CONTEXT_INVALID", "Harness 上下文统计未通过会话校验")
        if context_binding(await offload(self.service.runtime_config, self.conversation_id)) != binding:
            return await offload_write(save_context, self.service, self.conversation_id, harness_context_snapshot(stale_reason="route_changed"))
        value = harness_context_snapshot(raw.model_dump(by_alias=True))
        return await offload_write(save_context, self.service, self.conversation_id, {**value, "_binding": binding})

    async def interrupt(self):
        self.interrupted = True
        await self.close()

    async def close(self):
        async with self.closing:
            client, self.client = self.client, None
            if client:
                process = getattr(client, "_proc", None)
                if process is not None:
                    self.owned_process = process
                await asyncio.to_thread(client.close)
            if self.request_task:
                self.request_task.cancel()
                await asyncio.gather(self.request_task, return_exceptions=True)
                self.request_task = None

    def require_process_reaped(self):
        process = self.owned_process
        if (not isinstance(process, subprocess.Popen) or process.poll() is None
                or process.wait(timeout=0) is None):
            raise AppError(409, "MEMORY_EXECUTOR_STILL_RUNNING", "执行进程尚未确认退出，记忆未提交")
