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
from .harness_usage import HarnessUsageLedger
from .identity import WEB_READ_GUIDANCE, WEB_SEARCH_GUIDANCE, build_system_prompt
from .runtime import WorkspaceRuntime, elapsed_ms, save_timing
from .provider_policy import TOKENHUB_BASE_URL, provider_route
from .run_recovery import recovery_prompt
from .run_steering import settle as settle_steer
from .security import AppError
from .token_savings import TokenAccounting
from .workspace_tools import DELEGATE_TASKS_GUIDANCE

PINNED_COMMIT = "fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"
MODEL_FAILURES = {
    "TRANSPORT": (502, "MODEL_TRANSPORT_ERROR", "模型服务连接中断，请稍后恢复任务。"),
    "RATE_LIMIT": (429, "MODEL_RATE_LIMITED", "模型服务正在限流，请稍后恢复任务。"),
    "SERVER": (502, "MODEL_PROVIDER_ERROR", "模型服务暂时不可用，请稍后恢复任务。"),
    "TIMEOUT": (504, "MODEL_REQUEST_TIMEOUT", "模型服务请求超时，请稍后恢复任务。"),
    "CONTEXT_WINDOW_EXCEEDED": (422, "MODEL_CONTEXT_LIMIT", "模型的上下文空间不足，请查看会话的压缩记录后继续。"),
}


class _GoalView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^goal-[a-f0-9-]{36}$")
    revision: int = Field(ge=1)
    objective: str = Field(min_length=1,max_length=8000)
    phase: Literal["active","paused","blocked","complete"]
    maxGoalRounds: int = Field(ge=1,le=32)
    roundsStarted: int = Field(ge=0,le=32)
    createdAt: int = Field(ge=0)
    updatedAt: int = Field(ge=0)
    activation: Literal["disarmed"]
    blockedReason: dict[str,str] | None = None

    @model_validator(mode="after")
    def coherent(self):
        if self.roundsStarted>self.maxGoalRounds or self.updatedAt<self.createdAt or (
                (self.phase=="blocked") != (self.blockedReason is not None)):
            raise ValueError("incoherent goal receipt")
        return self


class _GoalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    schema_: Literal["workspace-goal-state/v1"] = Field(alias="schema")
    sourceCommit: Literal["fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"]
    sessionId: str
    goal: _GoalView | None
    error: str | None
    continuationDriver: Literal[False]


class _TeamTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    revision: int = Field(ge=1)
    subject: str
    description: str
    status: Literal["pending", "in_progress", "completed"]
    blockedBy: list[str]
    writeScopes: list[str]
    ownerId: str | None = None
    ownerName: str | None = None
    ready: bool
    writeScopeWarnings: list[str]


class _TeamBoard(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    tasks: list[_TeamTask] = Field(max_length=256)

    @model_validator(mode="after")
    def unique_tasks(self):
        if len({task.id for task in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate Team task")
        return self


class _TeamReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_: Literal["workspace-team-state/v1"] = Field(alias="schema")
    sourceCommit: Literal["fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"]
    sessionId: str
    board: _TeamBoard
    executionEnabled: Literal[False]


class _EmptyResult(BaseModel):
    pass


class _SteerResult(BaseModel):
    accepted: bool = Field(strict=True)


class _AutoCompactionResult(BaseModel):
    trigger: Literal["pressure", "context-overflow"]
    beforeTokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    afterTokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)
    shadowedItems: int | None = Field(default=None, ge=0, le=100_000, strict=True)
    shadowedTokens: int | None = Field(default=None, ge=0, le=10**12, strict=True)


class _UsageSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_: Literal["workspace-usage/v1"] = Field(alias="schema")
    sessionId: str
    runId: str
    receipts: list[dict] = Field(max_length=4096)
    overflowed: bool


_ContextNumber = Annotated[int, Field(strict=True, ge=0, le=1_000_000_000_000)]


class _ContextBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    systemTokens: _ContextNumber
    toolsTokens: _ContextNumber
    messageTokens: _ContextNumber


class _ForkResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sessionId: str
    atSeq: int


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


def _compaction_cost_usage(usage):
    """One successful pinned summarizer stream, separate from answer usage.

    The pinned translator initializes input/output to zero even if absent on
    the wire. Positive values prove presence; ambiguous zeros remain unknown.
    Optional cache counters have no such default. Never use totalTokens (which
    also adds default zeros), nor add reasoning tokens to inclusive output.
    """
    raw = usage if isinstance(usage, dict) else {}
    tokens = {}
    for upstream, public in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                             ("cacheReadTokens", "cache_read_input_tokens"),
                             ("cacheWriteTokens", "cache_write_input_tokens")):
        value = raw.get(upstream)
        valid = type(value) is int and 0 <= value <= 1_000_000_000_000
        if public in {"input_tokens", "output_tokens"} and value == 0:
            valid = False
        tokens[public] = value if valid else None
    ledger = TokenAccounting()
    ledger.record("compression", tokens, source="harness_provider",
                  note="One committed manual summary; adapter-default input/output zeros remain unknown.")
    return {"input_tokens": tokens["input_tokens"], "output_tokens": tokens["output_tokens"],
            "cache_read_input_tokens": tokens["cache_read_input_tokens"],
            "cache_creation_input_tokens": tokens["cache_write_input_tokens"],
            "estimated_cost_usd": None, "currency": "USD", "cost_is_estimate": True,
            "source": "harness_provider", "token_accounting": ledger.summary()}


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


def prompt_with_selected_skills(text, skills):
    if not skills:
        return text
    return (text + "\n\n【本轮用户在客户端选择的 Skills】\n"
            "以下版本仅用于本轮任务；不扩大工具权限，不覆盖系统身份及数据真实性要求。\n" + dump(skills))


class HarnessRuntime(WorkspaceRuntime):
    # A process is owned by exactly one run. Resume from the private durable
    # session on the next turn; delayed RPCs cannot cross an approval boundary.
    close_after_run = True

    def __init__(self, *args, parent_bridge=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session_id = self.config.get("native_session_id") or f"workspace-{uid()}"
        self.interrupted = False
        self.request_task = None
        self.tool_task = None
        self.tool_call_id = None
        self.tool_name = None
        self.closing = asyncio.Lock()
        self.owned_process = None
        self.mounted_tools = None
        self.accepting_subagent_plans = False
        self.subagent_child_id = self.config.get("subagent_child_id")
        self.bridge = HarnessToolBridge(self, parent_bridge)

    def model_system_prompt(self):
        return build_system_prompt(public_model_id=self.config["public_model_id"],
            public_model_label=self.config["public_model_label"], assistant_instructions=self.config["instructions"],
            include_web_tool_guidance=False)

    def connection_layout(self):
        data_dir = self.service.settings.data_dir
        return data_dir / "workspaces" / self.conversation_id, data_dir / "harness" / self.conversation_id

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
        root, private = self.connection_layout()
        max_output = self.config.get("worker_max_output_tokens", settings.harness_max_output_tokens)
        for directory in (root, root / "artifacts", private):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls, config_cls = await asyncio.to_thread(official_client, source)
        tools = self.bridge.definitions()
        tool_names = {tool["name"] for tool in tools}
        self.mounted_tools = {tool["name"]: tool for tool in tools}
        system_prompt = self.model_system_prompt()
        if "web_search" in tool_names:
            system_prompt += "\n\n" + WEB_SEARCH_GUIDANCE
        if "web_read" in tool_names:
            system_prompt += "\n\n" + WEB_READ_GUIDANCE + "。"
        if "delegate_tasks" in tool_names:
            system_prompt += DELEGATE_TASKS_GUIDANCE
        profile = write_profile(
            profile_root=private, source_dir=source, cwd=root, session_id=self.session_id,
            system_prompt=system_prompt,
            provider={"baseURL": provider.get("ANTHROPIC_BASE_URL", TOKENHUB_BASE_URL),
                      "model": self.config["upstream_model"], "apiKeyEnv": "WORKSPACE_MODEL_API_KEY", "reasoningEffort": effort,
                      "maxTokens": max_output},
            tools=tools, max_steps=self.config.get("worker_max_steps", settings.max_turns),
            team_state=settings.harness_team_state_enabled and not self.subagent_child_id and not self.service.policy.is_trial,
            tool_timeout_ms=(settings.approval_seconds + 60) * 1000,
            delegation_timeout_ms=(self.service.subagents.delegation_timeout() * 1000
                                   if "delegate_tasks" in tool_names else None),
        )
        launcher = write_launcher(private, node, source, root)
        self.client = cls(config_cls(dsh_bin=str(launcher), profile=profile["profile"],
            dsh_home=str(private), cwd=str(root), env={"WORKSPACE_MODEL_API_KEY": api_key},
            initialize_timeout_seconds=60, request_timeout_seconds=60, shutdown_timeout_seconds=1))
        try:
            await offload_write(self.client.start)
            self.owned_process = self.client._proc
            self.request_task = asyncio.create_task(self._requests(self.client))
            await asyncio.to_thread(self.client.initialize, cwd=str(root), provider="deepseek-official",
                                    model=self.config["upstream_model"], reasoning_effort=effort,
                                    max_tokens=max_output, approval_requests=True)
            if self.config.get("native_session_id"):
                await asyncio.to_thread(self.client.resume_session, self.session_id)
            self.accepting_subagent_plans = "delegate_tasks" in tool_names
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
                        self.tool_name = request.payload.get("toolName")
                        self.tool_task = asyncio.create_task(self.bridge.invoke(request.payload))
                        try:
                            result = await self.tool_task
                            await asyncio.to_thread(self.bridge.respond, client, request.id, request.payload, result, expected_run_id)
                        except asyncio.CancelledError:
                            if self.interrupted or self.client is None:
                                raise
                            await asyncio.to_thread(client.respond_error, request.id, code=-32800, message="Workspace operation cancelled")
                        finally:
                            self.tool_task, self.tool_call_id, self.tool_name = None, None, None
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
        try:
            await self._run_with_usage(run, lambda: self._execute_owned(run, message))
        finally:
            self.accepting_subagent_plans = False

    async def steer(self, run_id, request_id, text):
        client = self.client
        if not client or self.interrupted or (self.current_run or {}).get("id") != run_id or self.config.get("recovery_only"):
            return False
        receipt = await asyncio.to_thread(client.request, "workspace/steer",
            {"sessionId": self.session_id, "requestId": request_id, "text": text}, response_model=_SteerResult, timeout_seconds=4)
        return receipt.accepted

    async def fork_session(self, child_id, boundary):
        requested = {"sessionId": self.session_id, "childSessionId": f"workspace-{child_id}", "atSeq": boundary}
        response = await asyncio.to_thread(self.client.request, "workspace/fork", requested,
                                           response_model=_ForkResult, timeout_seconds=30)
        if response.sessionId != requested["childSessionId"] or response.atSeq != boundary:
            raise AppError(502, "FORK_RECEIPT_INVALID", "无法验证原生会话分支")

    async def _publish_usage(self, run, ledger):
        receipt = ledger.receipt()
        def persist():
            self.service.db.execute("UPDATE runs SET usage=? WHERE id=?", (dump(receipt), run["id"]))
            if run.get("kind") == "compact":
                row = self.service.require("runs", run["id"])
                value = json.loads(row.get("compaction") or "{}")
                if value:
                    value["cost_usage"] = receipt
                    self.service.db.execute("UPDATE runs SET compaction=? WHERE id=?", (dump(value), run["id"]))
        await offload_write(persist)
        await self.emit("usage.updated", receipt)

    def _drain_usage(self, subscription, ledger):
        def observe(notification):
            if notification.method == "workspace.usage":
                ledger.observe(notification.payload)
        try:
            subscription.drain(observe)
        except Exception:
            # A closed transport can still have delivered valid started or
            # usage receipts before its queued terminal error. Keep those.
            ledger.complete = False

    async def _usage_snapshot(self, client, run, ledger, action="snapshot"):
        value = await asyncio.to_thread(client.request, "workspace/usage",
            {"sessionId": self.session_id, "runId": run["id"], "action": action},
            response_model=_UsageSnapshot, timeout_seconds=5)
        if value.sessionId != self.session_id or value.runId != run["id"]:
            raise AppError(502, "HARNESS_USAGE_INVALID", "用量回执与当前任务不一致")
        if value.overflowed:
            ledger.invalid = True
        if action == "snapshot":
            ledger.settle(value.receipts)

    async def _run_with_usage(self, run, operation):
        client = self.client
        if client is None:
            raise AppError(503, "HARNESS_NOT_CONFIGURED", "执行引擎不可用")
        ledger = HarnessUsageLedger(self.session_id, run["id"])
        # The pinned SDK sends subscribed notifications to subscribers instead
        # of its default queue. Only consume usage receipts here: ordinary
        # chat notifications must remain available to _execute_chat.
        subscription = client.subscribe_notifications(lambda notification:
            notification.method == "workspace.usage"
            and notification.payload.get("sessionId") == self.session_id
            and notification.payload.get("runId") == run["id"])
        self._usage_capture = ledger
        watcher = None
        cancelled = False
        try:
            await self._usage_snapshot(client, run, ledger, "begin")
            await self._publish_usage(run, ledger)
            async def poll():
                while True:
                    revision = ledger.revision
                    self._drain_usage(subscription, ledger)
                    if ledger.revision != revision:
                        await self._publish_usage(run, ledger)
                    await asyncio.sleep(0.05)
            watcher = asyncio.create_task(poll())
            try:
                return await operation()
            except asyncio.CancelledError:
                cancelled = True
                raise
        finally:
            if watcher:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
            try:
                if self.client is client and not cancelled:
                    await self._usage_snapshot(client, run, ledger)
            except Exception:
                ledger.complete = False
            self._drain_usage(subscription, ledger)
            if cancelled:
                ledger.complete = False
            try:
                await self._publish_usage(run, ledger)
            finally:
                subscription.close()
                self._usage_capture = None

    async def _execute_owned(self, run, message):
        self.current_run, self.interrupted = run, False
        if run.get("kind", "chat") == "compact":
            try:
                await self._execute_compact(run, message)
            finally:
                self.current_run = None
            return
        operation = watcher = None
        try:
            attached = bool(json.loads(message.get("file_ids") or "[]"))
            if attached:
                await self.emit("run.stage", {"stage": "preparing_attachments"})
            attachments = await asyncio.to_thread(AttachmentPrompt, self, run, message)
            if attached:
                await self.emit("run.stage", {"stage": "delivering_attachments"})

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

    async def _next_chat_notification(self, client, timeout):
        # Keep the normal idle deadline unless the host is actually waiting
        # on a delegated plan. Reuse the same SDK queue reader on extension.
        receive = asyncio.create_task(asyncio.to_thread(client.next_notification))
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(receive), timeout)
            except asyncio.TimeoutError:
                if self.tool_name != "delegate_tasks" or "delegate_tasks" not in (self.mounted_tools or {}):
                    raise
                remaining = self.service.subagents.delegation_timeout() + 10 - timeout
                return await asyncio.wait_for(receive, max(1, remaining))
        finally:
            if not receive.done():
                receive.cancel()

    async def _execute_chat(self, run, message, attachments):
        self.bridge.calls.clear()
        client = self.client
        prompt = prompt_with_selected_skills(recovery_prompt(run, message["text"]),
                                           json.loads(run.get("skill_snapshots") or "[]"))
        await offload_write(self.service.db.execute, "UPDATE conversations SET context_usage=NULL WHERE id=?", (self.conversation_id,))
        receipt = await asyncio.to_thread(attachments.dispatch, client, self.session_id, prompt)
        accepted, finish, model_started = False, None, False
        failure, model_attempts = None, 0
        incomplete_stream = False
        streams, committed, active_step = {}, set(), None
        published_steps = set()

        async def emit_step_text(step, text):
            if not text:
                return
            separator = "\n\n" if published_steps and step not in published_steps else ""
            published_steps.add(step)
            await self.emit("message.delta", {"message_id": run["assistant_message_id"], "text": separator + text})

        # The child is legitimately silent while a person considers approval.
        # Do not expire a 600-second approval with a shorter transport timer.
        notification_timeout = max(300, self.service.settings.approval_seconds + 60)
        try:
            while True:
                notification = await self._next_chat_notification(client, notification_timeout)
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
                        reason = data.get("reason") or {}
                        finish = reason.get("kind")
                        # Only a fixed public category crosses the provider boundary.
                        # Never persist its arbitrary message, route or response body.
                        failure = MODEL_FAILURES.get((reason.get("error") or {}).get("code")) if finish == "error" else None
                    elif kind == "llm/retry":
                        cause = MODEL_FAILURES.get((data.get("failure") or {}).get("code"))
                        if cause:
                            delay = data.get("delayMs")
                            await self.emit("model.retry", {"retry": 1, "max_retries": 1,
                                "delay_ms": delay if type(delay) is int and 0 <= delay <= 10_000 else None,
                                "code": cause[1]})
                            await self.emit("run.stage", {"stage": "model_retry"})
                    elif kind == "compaction/start" and data.get("sourceCommandId") is None:
                        await self.emit("context.compaction_started", {"trigger": "auto"})
                    elif kind in {"assistant/message", "assistant/attempt"}:
                        seq = event.get("seq")
                        if seq in committed:
                            continue
                        committed.add(seq)
                        # Normalized SDK usage erases raw field presence and
                        # hides transport retries. HTTP receipts are the sole
                        # ledger source; assistant events only deliver text.
                        if kind != "assistant/message":
                            continue
                        message_data = data.get("message") or data
                        text = "".join(block.get("text", "") for block in message_data.get("content", []) if block.get("type") == "text")
                        prior = streams.get(active_step, "")
                        remainder = text[len(prior):] if text.startswith(prior) else ("" if prior else text)
                        await emit_step_text(active_step or ("commit", seq), remainder)
                        active_step = None
                    # Keep only event types and counters in the application
                    # trace; provider frames may contain credential echoes.
                    await offload_write(self.service.db.event, run["id"], "harness.event", {"type": kind, "seq": event.get("seq")})
                elif notification.method == "workspace.compaction" and accepted:
                    value = _AutoCompactionResult.model_validate(payload)
                    result = {"status": "completed", "trigger": value.trigger, "pre_tokens": value.beforeTokens,
                              "post_tokens": value.afterTokens, "shadowed_items": value.shadowedItems,
                              "shadowed_tokens": value.shadowedTokens, "measurement_schema": "harness-estimated/v1"}
                    await offload_write(self.service.db.execute, "UPDATE runs SET compaction=? WHERE id=?", (dump(result), run["id"]))
                    await self.emit("context.compaction_result", result)
                elif notification.method == "workspace.steer" and accepted:
                    await offload_write(settle_steer, self.service, run["id"], payload.get("requestId"), "applied")
                elif notification.method == "workspace.stream" and accepted:
                    key = str(payload.get("stepId", ""))
                    if payload.get("event") == "start":
                        active_step = key
                        incomplete_stream = False
                        model_attempts += 1
                        await self.emit("model.attempt", {"attempt": model_attempts})
                    if not model_started:
                        model_started = True
                        timings = await offload_write(save_timing, self.service, run["id"], "first_model_event_ms", elapsed_ms(run))
                        await self.emit("timing.updated", timings)
                        await self.emit("run.stage", {"stage": "model_started"})
                    if payload.get("event") == "text_delta" and isinstance(payload.get("text"), str):
                        text = payload["text"]
                        streams[key] = streams.get(key, "") + text
                        await emit_step_text(key, text)
                    elif payload.get("event") == "end" and (payload.get("finishObserved") is not True or (payload.get("outcome") or {}).get("kind") == "abandoned"):
                        incomplete_stream = True
                        if streams.get(key):
                            raise AppError(502, "HARNESS_STREAM_INCOMPLETE", "模型回复提前结束，请查看任务的恢复选项。")
                elif notification.method == "session.status" and accepted and payload.get("status") == "idle":
                    if finish != "completed":
                        if failure:
                            raise AppError(*failure)
                        raise AppError(502, "HARNESS_TURN_FAILED", "本次任务未完成，请重试。若仍失败，请联系管理员并提供执行记录。")
                    if incomplete_stream:
                        raise AppError(502, "HARNESS_STREAM_INCOMPLETE", "模型回复提前结束，请查看任务的恢复选项。")
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

    async def _compact_usage(self, run, receipt):
        # A compact run has exactly one committed summary, not an answer stage.
        # Persist the same receipt in the run and its bound compaction record.
        await offload_write(self.service.db.execute, "UPDATE runs SET usage=? WHERE id=?", (dump(receipt), run["id"]))
        await self.emit("usage.updated", receipt)

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
        await offload(self._recheck_compaction_run, run)
        binding = context_binding(self.config, self.session_id)
        before = {**before, "_binding": binding}
        progress = {"status": "started", "trigger": "manual",
                    "pre_tokens": before.get("total_tokens") if isinstance(before, dict) else None,
                    "before": before, "after": None, "measurement_schema": "compaction-measurement/v1"}
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
                capture = getattr(self, "_usage_capture", None)
                if capture is not None:
                    await self._usage_snapshot(client, run, capture)
                    cost_usage = capture.receipt()
                else:
                    cost_usage = _compaction_cost_usage(summary.get("usage"))
                progress.update(boundary, status="completed", boundary_observed=True, cost_usage=cost_usage)
                await offload_write(self.service.db.execute, "UPDATE runs SET compaction=? WHERE id=?", (dump(progress), run["id"]))
                await self.emit("context.compacted", boundary)
                await self._compact_usage(run, cost_usage)

            await offload(self._recheck_compaction_run, run)
            after = await self._compact_before()
            await offload(self._recheck_compaction_run, run)
            after = {**after, "_binding": binding}
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

    async def team_state(self):
        if self.subagent_child_id or self.current_run or not self.service.settings.harness_team_state_enabled or self.service.policy.is_trial:
            raise AppError(409,"TEAM_SESSION_BUSY","任务板仅支持空闲主会话")
        receipt=await asyncio.to_thread(self.client.request,"workspace/team-state",
            {"action":"get","sessionId":self.session_id},response_model=_TeamReceipt,timeout_seconds=30)
        if receipt.sessionId!=self.session_id:
            raise AppError(502,"TEAM_RECEIPT_INVALID","任务板回执与会话不匹配")
        return {"board":receipt.board.model_dump(exclude_none=True),"execution_enabled":False}

    async def goal_state(self, request):
        if self.subagent_child_id or self.current_run or not self.service.settings.harness_goal_state_enabled:
            raise AppError(409,"GOAL_SESSION_BUSY","目标管理仅支持空闲主会话")
        receipt=await asyncio.to_thread(self.client.request,"workspace/goal-state",
            {**request,"sessionId":self.session_id},response_model=_GoalReceipt,timeout_seconds=30)
        if receipt.sessionId!=self.session_id:
            raise AppError(502,"GOAL_RECEIPT_INVALID","目标回执与会话不匹配")
        if receipt.error is not None:
            errors={"GOAL_ALREADY_EXISTS":"已有未完成目标","GOAL_NOT_FOUND":"没有当前目标",
                    "GOAL_STALE_REVISION":"目标已变化，请刷新后重试",
                    "GOAL_INVALID_TRANSITION":"当前目标状态不允许此操作",
                    "GOAL_INVALID_EDIT":"目标修改无效","GOAL_INVALID_OBJECTIVE":"目标内容无效",
                    "GOAL_INVALID_MAX_ROUNDS":"轮次限制无效"}
            if receipt.error not in errors or receipt.goal is not None:
                raise AppError(502,"GOAL_RECEIPT_INVALID","原生目标回执无法核验")
            raise AppError(409,receipt.error,errors[receipt.error])
        return {"goal":receipt.goal.model_dump(exclude_none=True) if receipt.goal else None,
                "continuation_driver":False}

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
