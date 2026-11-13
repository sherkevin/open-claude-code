"""In-process, read-only Harness pilot; not a runtime or public API endpoint."""
from __future__ import annotations

import asyncio
import re

from .db import uid
from .security import AppError
from .workspace_tools import build_workspace_tools


_TOOLS = frozenset({"knowledge_search", "knowledge_read"})


def _arguments(name: str, value: dict) -> dict:
    def invalid():
        raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "知识工具参数无效或超出试点范围")

    if type(value) is not dict:
        invalid()
    if name == "knowledge_search":
        if set(value) - {"query", "limit"}:
            invalid()
        query, limit = value.get("query"), value.get("limit", 6)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            invalid()
        if type(limit) is not int or not 1 <= limit <= 20:
            invalid()
        return {"query": query, "limit": limit}
    if set(value) - {"document_id", "page"}:
        invalid()
    document, page = value.get("document_id"), value.get("page", 0)
    if not isinstance(document, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", document):
        invalid()
    if type(page) is not int or not 0 <= page <= 100000:
        invalid()
    return {"document_id": document, "page": page}


class HarnessKnowledgeBridge:
    """Trusted host code binds a single run; model arguments never carry identity.

    This pilot cannot approve anything. In particular, the legacy SDK wrapper's
    check_tool=False means "ask approval", NOT permission to execute here.
    """

    def __init__(self, runtime, *, enabled: bool = False):
        self.runtime = runtime
        self.enabled = enabled is True
        self.conversation_id = runtime.conversation_id
        conversation = runtime.service.require("conversations", self.conversation_id)
        self.user_id = conversation["user_id"]
        self.assistant_id = conversation["assistant_id"]
        self.run_id = (runtime.current_run or {}).get("id")
        if not self.run_id:
            raise AppError(409, "RUN_REQUIRED", "工具需要活动任务")
        run = runtime.service.require("runs", self.run_id)
        if run["conversation_id"] != self.conversation_id or run["user_id"] != self.user_id:
            raise AppError(403, "HARNESS_RUN_MISMATCH", "工具任务与会话不匹配")
        self.tools = {item.name: item for item in build_workspace_tools(runtime)
                      if item.name in _TOOLS and item.read_only}

    def _authorize(self, name: str, arguments: dict):
        runtime, service = self.runtime, self.runtime.service
        service.policy.require_tool_name(name)
        service.policy.require_tool(f"mcp__workspace__{name}")
        conversation = service.require("conversations", self.conversation_id)
        run = service.require("runs", self.run_id)
        if (runtime.conversation_id != self.conversation_id
                or (runtime.current_run or {}).get("id") != self.run_id
                or conversation["user_id"] != self.user_id
                or conversation["assistant_id"] != self.assistant_id
                or run["conversation_id"] != self.conversation_id
                or run["user_id"] != self.user_id):
            raise AppError(403, "HARNESS_RUN_MISMATCH", "工具任务与会话不匹配")
        if (conversation["archived"] or conversation["active_run_id"] != self.run_id
                or run["status"] != "running"):
            raise AppError(409, "HARNESS_RUN_INACTIVE", "此任务不再允许执行工具")
        tool_name = f"mcp__workspace__{name}"
        # check_tool refreshes configuration, user/assistant ACLs and run scope.
        if (runtime.check_tool(tool_name, arguments) is not True
                or service.model_approval.requires_review(self.conversation_id, tool_name)
                or service.high_risk_approval.requires_dual(run, tool_name, arguments)):
            raise AppError(403, "HARNESS_APPROVAL_REQUIRED", "此调用需要审批，请使用现有受控运行时")

    def _audit(self, call_id: str, name: str, outcome: str):
        try:
            self.runtime.service.db.event(self.run_id, "harness.bridge", {
                "schema": "harness-knowledge-bridge/v1", "call_id": call_id,
                "tool": name if isinstance(name, str) and name in _TOOLS else "unregistered",
                "outcome": outcome,
            })
        except Exception:
            raise AppError(503, "HARNESS_AUDIT_UNAVAILABLE", "工具审计不可用，本次调用已停止") from None

    async def invoke(self, name: str, arguments: dict) -> dict:
        call_id = uid()
        service = self.runtime.service
        name = service.policy.require_tool_name(name)
        service.policy.require_tool(f"mcp__workspace__{name}")
        try:
            if not self.enabled:
                raise AppError(403, "HARNESS_BRIDGE_DISABLED", "Harness 知识工具试点尚未启用")
            if name not in self.tools:
                raise AppError(403, "HARNESS_TOOL_DENIED", "此工具不在 Harness 只读试点白名单内")
            bounded = _arguments(name, arguments)
            self._authorize(name, bounded)
            self._audit(call_id, name, "started")
            result = await self.tools[name].handler(bounded)
            # Reads can await indexing; discard the result if access was revoked.
            self._authorize(name, bounded)
        except asyncio.CancelledError:
            try:
                self._audit(call_id, name, "cancelled")
            except AppError:
                # Cancellation must still propagate if the audit store is down.
                pass
            raise
        except AppError:
            self._audit(call_id, name, "denied")
            raise
        except Exception:
            self._audit(call_id, name, "failed")
            raise AppError(502, "HARNESS_TOOL_FAILED", "知识工具暂时不可用") from None
        self._audit(call_id, name, "failed" if result.get("is_error") else "succeeded")
        return result
