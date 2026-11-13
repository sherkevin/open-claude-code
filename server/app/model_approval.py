"""Bounded, fail-closed model-as-user approval policy.

The first implementation is deliberately local-only.  A policy is written by
an administrator, evaluated against a fixed tool/data/operation/amount
allowlist, and then passed to an injectable reviewer.  The default reviewer is
disabled and therefore denies; tests may inject a deterministic reviewer.  No
provider, network, MCP connection, or credential is accessed here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Awaitable, Callable

from .db import dump, now, offload, uid
from .security import AppError, redact, tool_risk_profile, workspace_path


REVIEWER_MODE = "synthetic"
MAX_REASON_CHARS = 2000
MAX_RESULT_CHARS = 4000
AMOUNT_KEYS = ("amount", "amount_usd", "total", "total_amount", "price", "value")


def _digest(value: Any) -> str:
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


def _safe_text(value: Any, limit: int = MAX_REASON_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


@dataclass(frozen=True)
class ModelReview:
    """A normalized decision that can be persisted without raw arguments."""

    decision: str
    model_id: str
    policy_id: str
    policy_revision: int
    policy_digest: str
    reason: str
    result: dict[str, Any]
    operation: str
    data_scope: str
    amount: float | None
    argument_digest: str
    confidence: float | None = None
    uncertain: bool = False
    source: str = "model"


class ModelApprovalService:
    """Evaluate administrator policy and call an injected local reviewer."""

    def __init__(self, service, reviewer: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None):
        self.service = service
        self.reviewer = reviewer or self._disabled_reviewer
        self.timeout_seconds = 2.0

    async def _disabled_reviewer(self, _request: dict[str, Any]) -> dict[str, Any]:
        # Production cannot accidentally turn this feature into an implicit
        # provider call.  A deployment must explicitly inject an approved
        # reviewer implementation in a later phase.
        return {"decision": "deny", "reason": "本地代审器未启用模型调用"}

    def requires_review(self, conversation_id: str, tool: str) -> bool:
        """Whether a low-risk call must enter the model policy path.

        The SDK pre-tool hook otherwise auto-allows safe reads before its
        asynchronous permission callback can run.
        """
        conversation = self.service.require("conversations", conversation_id)
        policy = self.policy(conversation["assistant_id"])
        return bool(policy and policy["enabled"] and tool_risk_profile(tool)["risk_level"] == "low")

    def policy(self, assistant_id: str) -> dict | None:
        row = self.service.db.one("SELECT * FROM model_approval_policies WHERE assistant_id=?", (assistant_id,))
        if not row:
            return None
        return {
            "assistant_id": row["assistant_id"],
            "enabled": bool(row["enabled"]),
            "reviewer_mode": row["reviewer_mode"],
            "model_id": row["model_id"],
            "allowed_tools": json.loads(row["allowed_tools"] or "[]"),
            "allowed_data_scopes": json.loads(row["allowed_data_scopes"] or "[]"),
            "max_amount": row["max_amount"],
            "allowed_operations": json.loads(row["allowed_operations"] or "[]"),
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    def policy_view(self, assistant_id: str) -> dict:
        value = self.policy(assistant_id)
        if value:
            return value
        return {
            "assistant_id": assistant_id,
            "enabled": False,
            "reviewer_mode": REVIEWER_MODE,
            "model_id": "synthetic-policy-v1",
            "allowed_tools": [],
            "allowed_data_scopes": [],
            "max_amount": 0.0,
            "allowed_operations": [],
            "revision": 0,
            "updated_at": None,
            "updated_by": None,
        }

    def save_policy(self, assistant_id: str, data: dict, user: dict) -> dict:
        self.service.require_admin(user)
        self.service.require("assistants", assistant_id)
        if data.get("reviewer_mode", REVIEWER_MODE) != REVIEWER_MODE:
            raise AppError(422, "MODEL_REVIEWER_DISABLED", "当前只允许本地合成代审器")
        model_id = data.get("model_id", "synthetic-policy-v1")
        if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 100:
            raise AppError(422, "INVALID_MODEL_REVIEW_POLICY", "代审模型标识无效")
        if not model_id.strip().startswith("synthetic-"):
            raise AppError(422, "MODEL_REVIEW_MODEL_NOT_ALLOWED", "当前只允许本地合成代审模型")
        def strings(name: str, limit: int) -> list[str]:
            values = data.get(name, [])
            if not isinstance(values, list) or len(values) > limit or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in values):
                raise AppError(422, "INVALID_MODEL_REVIEW_POLICY", f"{name} 配置无效")
            return list(dict.fromkeys(v.strip() for v in values))
        allowed_tools = strings("allowed_tools", 100)
        for tool in allowed_tools:
            # Model-as-user is limited to the explicit, low-risk local read
            # tools.  A policy cannot turn a write, MCP, or unknown tool into
            # a model-approved operation.
            profile = tool_risk_profile(tool)
            if tool not in {"Read", "Glob", "Grep"} or profile["risk_level"] != "low":
                raise AppError(422, "MODEL_REVIEW_TOOL_NOT_ALLOWED", "模型代审只能配置 Read、Glob、Grep 低风险工具")
        allowed_data_scopes = strings("allowed_data_scopes", 100)
        allowed_operations = strings("allowed_operations", 100)
        try:
            max_amount = float(data.get("max_amount", 0))
        except (TypeError, ValueError):
            raise AppError(422, "INVALID_MODEL_REVIEW_POLICY", "金额上限无效")
        if not math.isfinite(max_amount) or max_amount < 0 or max_amount > 1_000_000_000:
            raise AppError(422, "INVALID_MODEL_REVIEW_POLICY", "金额上限超出范围")
        enabled = bool(data.get("enabled", False))
        previous = self.policy(assistant_id)
        revision = int(previous["revision"] if previous else 0) + 1
        stamp = now()
        self.service.db.execute(
            "INSERT INTO model_approval_policies(assistant_id,enabled,reviewer_mode,model_id,allowed_tools,allowed_data_scopes,max_amount,allowed_operations,revision,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(assistant_id) DO UPDATE SET enabled=excluded.enabled,reviewer_mode=excluded.reviewer_mode,model_id=excluded.model_id,allowed_tools=excluded.allowed_tools,allowed_data_scopes=excluded.allowed_data_scopes,max_amount=excluded.max_amount,allowed_operations=excluded.allowed_operations,revision=excluded.revision,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
            (assistant_id, int(enabled), REVIEWER_MODE, model_id.strip(), dump(allowed_tools), dump(allowed_data_scopes), max_amount, dump(allowed_operations), revision, stamp, user["id"]),
        )
        return self.policy_view(assistant_id)

    @staticmethod
    def _operation(tool: str, arguments: dict[str, Any]) -> str:
        value = arguments.get("operation", arguments.get("action"))
        if isinstance(value, str) and value.strip():
            return value.strip().lower()[:100]
        return tool.rsplit("__", 1)[-1].lower()[:100]

    @staticmethod
    def _review_summary(arguments: dict[str, Any]) -> dict[str, Any]:
        """Return only bounded fields needed by the local reviewer.

        Raw prompt/content values and arbitrary tool parameters never cross
        the reviewer boundary; the full immutable request remains server-side
        and is represented to audit consumers by its digest.
        """
        allowed = {"operation", "action", "data_scope", "scope", "knowledge_base_id", "knowledge_id", "amount", "amount_usd", "total", "total_amount", "price", "value"}
        return {key: redact(arguments[key]) for key in sorted(allowed) if key in arguments}

    def _data_scope(self, conversation_id: str, tool: str, arguments: dict[str, Any]) -> str:
        explicit = arguments.get("data_scope") or arguments.get("scope")
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip()[:200]
        for key in ("knowledge_base_id", "knowledge_id"):
            if isinstance(arguments.get(key), str) and arguments[key]:
                return f"knowledge:{arguments[key]}"[:200]
        for key in ("file_path", "path"):
            raw = arguments.get(key)
            if not isinstance(raw, str):
                continue
            root = self.service.settings.data_dir / "workspaces" / conversation_id
            try:
                resolved = workspace_path(root, raw)
                if resolved.is_relative_to(root.resolve()):
                    return "workspace"
            except (AppError, OSError, RuntimeError):
                return "unknown"
        # Tools without a data argument still need an explicit policy scope;
        # "none" is a deliberate administrator choice rather than a wildcard.
        return "none"

    @staticmethod
    def _amount(arguments: dict[str, Any]) -> float | None:
        found = [arguments[key] for key in AMOUNT_KEYS if key in arguments]
        if not found:
            return None
        if len(found) > 1 and any(value != found[0] for value in found[1:]):
            return float("nan")
        try:
            value = Decimal(str(found[0]))
        except (InvalidOperation, ValueError, TypeError):
            return float("nan")
        if not value.is_finite() or value < 0:
            return float("nan")
        return float(value)

    @staticmethod
    def _policy_digest(policy: dict) -> str:
        selected = {key: policy[key] for key in ("assistant_id", "enabled", "reviewer_mode", "model_id", "allowed_tools", "allowed_data_scopes", "max_amount", "allowed_operations", "revision")}
        return _digest(selected)

    def _denied(self, policy: dict, reason: str, operation: str, scope: str, amount: float | None, argument_digest: str, *, result: dict | None = None) -> ModelReview:
        return ModelReview("deny", policy["model_id"], policy["assistant_id"], policy["revision"], self._policy_digest(policy), reason[:MAX_REASON_CHARS], result or {"status": "policy_denied"}, operation, scope, None if amount is None or not math.isfinite(amount) else amount, argument_digest)

    def verify(self, review: ModelReview, run: dict, tool: str, arguments: dict[str, Any]) -> bool:
        """Revalidate the exact grant and policy snapshot immediately before execution."""
        if review.decision != "allow":
            return False
        conversation = self.service.require("conversations", run["conversation_id"])
        policy = self.policy(conversation["assistant_id"])
        if not policy or not policy["enabled"] or policy["revision"] != review.policy_revision or self._policy_digest(policy) != review.policy_digest:
            return False
        if tool_risk_profile(tool)["risk_level"] != "low" or tool not in policy["allowed_tools"]:
            return False
        operation = self._operation(tool, arguments)
        scope = self._data_scope(run["conversation_id"], tool, arguments)
        amount = self._amount(arguments)
        if operation != review.operation or scope != review.data_scope or amount != review.amount or operation not in policy["allowed_operations"] or scope not in policy["allowed_data_scopes"]:
            return False
        if amount is not None and (not math.isfinite(amount) or amount > float(policy["max_amount"])):
            return False
        try:
            # Authorization helpers return ``False`` for a non-auto-approved
            # operation (rather than always raising).  A model review must
            # never turn that false result into an allow, so require the
            # explicit boolean grant here and in the pre-review gate below.
            if self.service.authorize_tool(run["conversation_id"], tool, arguments) is not True:
                return False
        except Exception:
            return False
        return _digest(arguments) == review.argument_digest

    async def review(self, run: dict, tool: str, arguments: dict[str, Any]) -> ModelReview | None:
        conversation = await offload(self.service.require, "conversations", run["conversation_id"])
        policy = await offload(self.policy, conversation["assistant_id"])
        if not policy or not policy["enabled"]:
            return None
        operation = self._operation(tool, arguments)
        scope = self._data_scope(run["conversation_id"], tool, arguments)
        amount = self._amount(arguments)
        argument_digest = _digest(arguments)
        profile = tool_risk_profile(tool)
        # A model may never decide a medium/high-risk or unknown tool. Return
        # None so the caller falls through to the existing human approval path.
        if profile["risk_level"] != "low":
            return None
        try:
            # Grants may be revoked after a policy was saved.  Re-evaluate the
            # current conversation/assistant/connection scope immediately
            # before invoking the reviewer.
            if await offload(self.service.authorize_tool, run["conversation_id"], tool, arguments) is not True:
                return self._denied(policy, "服务端授权未明确允许，已安全拒绝", operation, scope, amount, argument_digest, result={"status": "authorization_denied"})
        except Exception:
            return self._denied(policy, "服务端授权复核失败，已安全拒绝", operation, scope, amount, argument_digest, result={"status": "authorization_denied"})
        if tool not in policy["allowed_tools"]:
            return self._denied(policy, "工具不在管理员代审白名单", operation, scope, amount, argument_digest)
        if scope not in policy["allowed_data_scopes"]:
            return self._denied(policy, "数据范围不在管理员代审白名单", operation, scope, amount, argument_digest)
        if operation not in policy["allowed_operations"]:
            return self._denied(policy, "操作范围不在管理员代审白名单", operation, scope, amount, argument_digest)
        if amount is not None and (not math.isfinite(amount) or amount > float(policy["max_amount"])):
            return self._denied(policy, "金额超过管理员代审上限", operation, scope, amount, argument_digest)
        request = {
            "model_id": policy["model_id"],
            "policy_id": policy["assistant_id"],
            "policy_revision": policy["revision"],
            "tool_name": tool,
            "operation": operation,
            "data_scope": scope,
            "amount": amount,
            "arguments": self._review_summary(arguments),
            "argument_digest": argument_digest,
        }
        try:
            raw = await asyncio.wait_for(self.reviewer(request), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._denied(policy, "模型代审失败，已安全拒绝", operation, scope, amount, argument_digest, result={"status": "reviewer_error", "error_type": type(exc).__name__})
        if not isinstance(raw, dict):
            return self._denied(policy, "模型代审返回无效结果，已安全拒绝", operation, scope, amount, argument_digest, result={"status": "invalid_result"})
        allowed_fields = {"decision", "reason", "confidence", "uncertain", "result"}
        if set(raw) - allowed_fields:
            return self._denied(policy, "模型代审返回字段超出协议，已安全拒绝", operation, scope, amount, argument_digest, result={"status": "invalid_result"})
        decision = raw.get("decision")
        if decision not in {"allow", "deny"}:
            return self._denied(policy, "模型代审返回无效决策，已安全拒绝", operation, scope, amount, argument_digest, result={"status": "invalid_decision"})
        confidence = raw.get("confidence")
        uncertain = raw.get("uncertain")
        if type(confidence) not in (int, float) or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1 or type(uncertain) is not bool or uncertain:
            return self._denied(policy, "模型代审结果不确定，已安全拒绝", operation, scope, amount, argument_digest, result={"status": "uncertain"})
        reason = _safe_text(raw.get("reason")) or ("模型代审允许" if decision == "allow" else "模型代审拒绝")
        result = {"status": "reviewed", "decision": decision, "reason": reason, "confidence": float(confidence), "uncertain": uncertain}
        encoded = dump(result)
        if len(encoded) > MAX_RESULT_CHARS:
            result = {"status": "reviewed", "decision": decision, "reason": reason}
        return ModelReview(decision, policy["model_id"], policy["assistant_id"], policy["revision"], self._policy_digest(policy), reason, result, operation, scope, amount, argument_digest, float(confidence), uncertain)

    def audit_payload(self, review: ModelReview) -> dict[str, Any]:
        return {
            "strategy": "model",
            "model_id": review.model_id,
            "policy_id": review.policy_id,
            "policy_revision": review.policy_revision,
            "policy_digest": review.policy_digest,
            "operation": review.operation,
            "data_scope": review.data_scope,
            "amount": review.amount,
            "argument_digest": review.argument_digest,
            "reason": review.reason,
            "result": review.result,
            "confidence": review.confidence,
            "uncertain": review.uncertain,
            "decided_at": now(),
            "decision_source": review.source,
        }
