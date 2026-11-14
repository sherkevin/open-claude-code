"""Single orchestration point for one external-capability tool call.

The executor is deliberately the *only* place that mixes authorization, route
selection, a total deadline, per-attempt quota accounting, bounded retry,
exact approval-receipt consumption and audit.  Provider adapters receive one
real request at a time and may not retry internally.

``Service.authorize_tool`` stays the authorization entry point; this module
only delegates to it and to the pure policy helpers.  ``authorize_tool``
returning ``False`` means "approval required", never "already approved": the
executor then consumes an exact, one-time receipt bound to this host
tool-call id and these exact public arguments.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .capability_policy import (
    CAPABILITY_TOOLS,
    CapabilityPolicy,
    classify_error,
    required_egress,
)
from .capability_credentials import CapabilityCredentials, KNOWN_PROVIDERS
from .capability_receipts import arguments_digest, claim_approval_receipt, verify_approval_receipt
from .capability_usage import CapabilityUsage
from .db import uid
from .security import AppError

Recheck = Callable[[], None]
Operation = Callable[[str, float, Recheck], Awaitable[Any]]


@dataclass(frozen=True)
class RunContext:
    """Host-constructed identity binding for one logical tool call.

    ``tool_call_id`` must be the actual host tool-call id (or a freshly generated
    unique invocation id), never a value derived from run id plus tool name,
    so repeated calls in the same run stay distinct.  ``approval_id`` points at
    an exact receipt and is only needed when authorization reports that
    approval is required.
    """
    run_id: str
    conversation_id: str
    user_id: str
    assistant_id: str
    tool_call_id: str
    recheck: Recheck
    approval_id: str | None = None


@dataclass(frozen=True)
class RouteResult:
    value: Any
    provider: str
    requested_provider: str
    fallback: bool
    attempts: int
    usage_id: str
    settings_revision: int


def new_call_id() -> str:
    """Unique invocation id for callers that have no host tool-call id."""
    return f"cap-{uid()}"


def _estimated_bytes(value: Any) -> int:
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", errors="replace"))
    if isinstance(value, dict):
        reported = value.get("bytes_received")
        if isinstance(reported, int) and reported >= 0:
            return reported
    return 0


class CapabilityExecutor:
    def __init__(self, service):
        self.service = service
        self.usage = CapabilityUsage(service)

    def _load(self, context: RunContext):
        service = self.service
        run = service.require("runs", context.run_id)
        conversation = service.require("conversations", context.conversation_id)
        user = service.require("users", context.user_id)
        if (run["conversation_id"] != conversation["id"]
                or conversation["user_id"] != user["id"]
                or run["user_id"] != user["id"]
                or context.assistant_id != conversation["assistant_id"]):
            raise AppError(403, "CAPABILITY_CONTEXT_DENIED", "工具上下文与当前会话不匹配")
        if conversation.get("active_run_id") != run["id"]:
            raise AppError(403, "CAPABILITY_RUN_REQUIRED", "当前任务已失效，请重新发送请求")
        if run.get("status") not in {"running", "awaiting_approval"}:
            raise AppError(403, "CAPABILITY_RUN_REQUIRED", "当前任务已结束")
        assistant = service.usable_assistant(conversation["assistant_id"], user)
        snapshot = json.loads(run.get("capability_snapshot") or "null") if run.get("capability_snapshot") else None
        if isinstance(snapshot, dict) and snapshot.get("id") not in {None, assistant["id"]}:
            raise AppError(403, "CAPABILITY_CONTEXT_DENIED", "工具上下文与助手快照不匹配")
        return run, conversation, user, assistant, snapshot

    def effective(self, context: RunContext, capability: str, arguments: dict) -> dict:
        """Current effective capability without consuming quota or claiming."""
        _, conversation, user, assistant, snapshot = self._load(context)
        if capability not in CAPABILITY_TOOLS:
            raise AppError(422, "CAPABILITY_UNKNOWN", "未知能力")
        return CapabilityPolicy(self.service).current(
            conversation=conversation, user=user, assistant=assistant, snapshot=snapshot)[capability]

    def _authorize(self, context: RunContext, capability: str, arguments: dict, *,
                   claim: bool, owner_token: str) -> dict:
        tool_name = CAPABILITY_TOOLS[capability]
        # A False result means approval is required.  It is never proof that an
        # approval already happened.
        effective = self.effective(context, capability, arguments)
        automatic = self.service.authorize_tool(context.conversation_id, tool_name, arguments)
        if automatic is False:
            egress_mode = effective["egress"][required_egress(capability, arguments)]
            require_human = egress_mode == "approval"
            if claim:
                claim_approval_receipt(
                    self.service, run_id=context.run_id, tool_call_id=context.tool_call_id,
                    tool_name=tool_name, arguments=arguments, call_scope_id=owner_token,
                    require_human=require_human, expected_receipt_id=context.approval_id)
            else:
                verify_approval_receipt(
                    self.service, run_id=context.run_id, tool_call_id=context.tool_call_id,
                    tool_name=tool_name, arguments=arguments, call_scope_id=owner_token,
                    require_human=require_human, expected_receipt_id=context.approval_id)
        return effective

    def _check_target(self, effective: dict, arguments: dict, target_url: str | None) -> None:
        from .capability_policy import domain_allows
        target = arguments.get("url")
        if target is not None and not domain_allows(effective["domains"], target):
            raise AppError(403, "CAPABILITY_DOMAIN_DENIED", "目标地址不在当前能力允许的域名范围内")
        if target_url is not None and not domain_allows(effective["domains"], target_url):
            raise AppError(403, "CAPABILITY_DOMAIN_DENIED", "目标地址不在当前能力允许的域名范围内")
        mode = effective["egress"][required_egress(effective["capability"], arguments)]
        if mode == "deny":
            raise AppError(403, "CAPABILITY_EGRESS_DENIED", "当前能力的数据外发已被管理员禁止")

    async def execute_for_run(
        self,
        context: RunContext,
        capability: str,
        arguments: dict,
        operation: Operation,
        *,
        target_url: str | None = None,
        external_upload: bool = False,
    ) -> RouteResult:
        if capability not in CAPABILITY_TOOLS:
            raise AppError(422, "CAPABILITY_UNKNOWN", "未知能力")
        if not isinstance(arguments, dict):
            raise AppError(422, "CAPABILITY_ARGUMENT_INVALID", "工具参数无效")
        # Ownership belongs to this execute entry, never the reusable host id
        # or executor instance. The closure alone can verify this receipt.
        owner_token = uid()
        effective = self._authorize(context, capability, arguments, claim=True, owner_token=owner_token)
        if not effective["enabled"]:
            raise AppError(403, "CAPABILITY_DISABLED", "当前能力未对本次任务或助手开放")
        self._check_target(effective, arguments, target_url)
        # Host-provided preflight recheck runs before any attempt.
        context.recheck()
        self.usage.claim_call(
            run_id=context.run_id, tool_call_id=context.tool_call_id,
            tool_name=CAPABILITY_TOOLS[capability], arguments_digest=arguments_digest(arguments),
            owner_token=owner_token)

        providers = [effective["preferred"], *effective["fallbacks"]]
        providers = [item for item in providers if item]
        if not providers:
            raise AppError(503, "CAPABILITY_PROVIDER_UNAVAILABLE", "当前能力没有可用的提供方")
        max_attempts = max(1, min(int(effective["max_attempts"]), 3))
        deadline = time.monotonic() + max(1, int(effective["timeout_seconds"]))
        settings_revision = int(effective["settings_revision"])
        limits = effective["limits"]
        last_error: AppError | None = None
        attempts = 0
        credentials = CapabilityCredentials(self.service)
        credential_binding: tuple[str, tuple[str | None, int]] | None = None

        def recheck() -> None:
            # Re-run the full current-authorization chain, route revision and
            # this invocation's own already-claimed receipt (no re-claim).
            current = self._authorize(context, capability, arguments, claim=False, owner_token=owner_token)
            if not current["enabled"]:
                raise AppError(403, "CAPABILITY_DISABLED", "当前能力已被停用")
            if int(current["settings_revision"]) != settings_revision:
                raise AppError(409, "CAPABILITY_CHANGED", "能力配置已更新，请重新发起请求")
            self._check_target(current, arguments, target_url)
            if credential_binding is not None:
                provider_id, generation = credential_binding
                if credentials.snapshot(provider_id) != generation:
                    raise AppError(409, "CAPABILITY_CREDENTIAL_CHANGED", "能力凭据已更新，请重新发起请求")
            context.recheck()

        for attempt_index in range(max_attempts):
            if time.monotonic() >= deadline:
                raise AppError(504, "CAPABILITY_TIMEOUT", "外部能力调用超时，请稍后重试")
            provider = providers[attempt_index] if attempt_index < len(providers) else providers[-1]
            credential_binding = (provider, credentials.snapshot(provider)) if provider in KNOWN_PROVIDERS else None
            recheck()
            usage_id = self.usage.reserve_attempt(
                run_id=context.run_id, assistant_id=context.assistant_id, user_id=context.user_id,
                capability=capability, call_id=owner_token, attempt_index=attempt_index,
                provider=provider, settings_revision=settings_revision,
                provider_revision=credential_binding[1][1] if credential_binding is not None else 0,
                calls_per_run=int(limits.get("calls_per_run") or 1),
                calls_per_day=int(limits.get("calls_per_day") or 1))
            attempts = attempt_index + 1
            started = time.monotonic()
            try:
                remaining = deadline - started
                if remaining <= 0:
                    raise TimeoutError()
                # The total deadline also bounds a single provider call, so an
                # adapter that ignores its own timeout cannot exceed the budget.
                value = await asyncio.wait_for(operation(provider, deadline, recheck), timeout=remaining)
            except asyncio.CancelledError:
                # The request may have been sent; keep the reservation and
                # record an unknown outcome rather than pretending nothing ran.
                self.usage.settle_attempt(
                    usage_id, state="unknown", error_code="cancelled",
                    duration_ms=int((time.monotonic() - started) * 1000))
                raise
            except AppError as exc:
                self.usage.settle_attempt(
                    usage_id, state="failed", error_code=exc.code,
                    duration_ms=int((time.monotonic() - started) * 1000))
                last_error = exc
                if classify_error(exc) != "transient" or attempt_index + 1 >= max_attempts:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppError(504, "CAPABILITY_TIMEOUT", "外部能力调用超时，请稍后重试") from None
                delay = min(0.15 * (2 ** attempt_index), 1.0, max(0.0, remaining - 0.05))
                if delay > 0:
                    await asyncio.sleep(delay)
                continue
            except (TimeoutError, asyncio.TimeoutError):
                self.usage.settle_attempt(
                    usage_id, state="failed", error_code="timeout",
                    duration_ms=int((time.monotonic() - started) * 1000))
                last_error = AppError(504, "CAPABILITY_TIMEOUT", "外部能力调用超时，请稍后重试")
                if attempt_index + 1 >= max_attempts:
                    raise last_error
                continue
            except Exception as exc:  # provider adapter contract break
                self.usage.settle_attempt(
                    usage_id, state="failed", error_code=type(exc).__name__,
                    duration_ms=int((time.monotonic() - started) * 1000))
                raise AppError(502, "CAPABILITY_PROVIDER_FAILED", "外部能力服务暂时不可用") from exc
            # A successful response is accepted only if the same live policy
            # still holds; otherwise it is discarded and no evidence is made.
            try:
                recheck()
            except AppError as exc:
                self.usage.settle_attempt(
                    usage_id, state="discarded", error_code=exc.code,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    bytes_received=_estimated_bytes(value))
                raise
            self.usage.settle_attempt(
                usage_id, state="ok", duration_ms=int((time.monotonic() - started) * 1000),
                bytes_received=_estimated_bytes(value))
            return RouteResult(
                value=value, provider=provider, requested_provider=providers[0],
                fallback=provider != providers[0], attempts=attempts, usage_id=usage_id,
                settings_revision=settings_revision)
        if last_error:
            raise last_error
        raise AppError(502, "CAPABILITY_PROVIDER_FAILED", "外部能力服务暂时不可用")
