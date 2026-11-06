"""Fail-closed administrator and dual-approval policy for high-risk tools.

The policy is deliberately local and deterministic.  It never calls a model,
MCP server, provider, or credential store.  An enabled policy snapshots its
administrator allowlist into each approval request; changing or revoking the
policy therefore cannot widen an in-flight request.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .db import dump, now, uid
from .security import AppError, tool_risk_profile


HIGH_RISK_MARKERS = {
    "delete", "destroy", "drop", "remove", "bulk", "batch", "publish",
    "release", "external", "credential", "secret", "token", "password",
}


def _digest(value: Any) -> str:
    return hashlib.sha256(dump(value).encode("utf-8")).hexdigest()


class HighRiskApprovalService:
    """Own policy CRUD, request snapshots and dual-vote validation."""

    def __init__(self, service):
        self.service = service

    def policy(self, assistant_id: str) -> dict | None:
        row = self.service.db.one("SELECT * FROM high_risk_approval_policies WHERE assistant_id=?", (assistant_id,))
        if not row:
            return None
        return {
            "assistant_id": row["assistant_id"],
            "enabled": bool(row["enabled"]),
            "policy_id": row["policy_id"],
            "allowed_tools": json.loads(row["allowed_tools"] or "[]"),
            "admin_allowlist": json.loads(row["admin_allowlist"] or "[]"),
            "required_approvals": int(row["required_approvals"]),
            "revision": int(row["revision"]),
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    @staticmethod
    def _digest_policy(policy: dict) -> str:
        selected = {key: policy[key] for key in (
            "assistant_id", "enabled", "policy_id", "allowed_tools",
            "admin_allowlist", "required_approvals", "revision",
        )}
        return _digest(selected)

    def policy_view(self, assistant_id: str) -> dict:
        value = self.policy(assistant_id)
        if value:
            return value
        return {
            "assistant_id": assistant_id,
            "enabled": False,
            "policy_id": f"high-risk:{assistant_id}",
            "allowed_tools": [],
            "admin_allowlist": [],
            "required_approvals": 2,
            "revision": 0,
            "updated_at": None,
            "updated_by": None,
        }

    def save_policy(self, assistant_id: str, data: dict, user: dict) -> dict:
        self.service.require_admin(user)
        self.service.require("assistants", assistant_id)
        enabled = bool(data.get("enabled", False))
        policy_id = data.get("policy_id") or f"high-risk:{assistant_id}"
        if not isinstance(policy_id, str) or not policy_id.strip() or len(policy_id) > 100:
            raise AppError(422, "INVALID_HIGH_RISK_POLICY", "高风险审批策略标识无效")

        def strings(name: str, limit: int) -> list[str]:
            values = data.get(name, [])
            if not isinstance(values, list) or len(values) > limit:
                raise AppError(422, "INVALID_HIGH_RISK_POLICY", f"{name} 配置无效")
            result = []
            for value in values:
                if not isinstance(value, str) or not value.strip() or len(value) > 200:
                    raise AppError(422, "INVALID_HIGH_RISK_POLICY", f"{name} 配置无效")
                result.append(value.strip())
            return list(dict.fromkeys(result))

        allowed_tools = strings("allowed_tools", 100)
        # The dual policy must only name tools already classified as high risk;
        # otherwise an administrator could accidentally route an ordinary read
        # through an unrelated approval path and weaken the documented model.
        for tool in allowed_tools:
            profile = tool_risk_profile(tool)
            if profile["risk_level"] != "high" or profile["approval_policy"] == "deny_by_default":
                raise AppError(422, "HIGH_RISK_TOOL_REQUIRED", "高风险双人审批只能配置高风险工具")
        allowlist = strings("admin_allowlist", 100)
        # A configured dual policy must name at least one administrator.  An
        # empty list is an intentional default-deny configuration, never a
        # wildcard granting every administrator access.
        for user_id in allowlist:
            row = self.service.db.one("SELECT id,role,enabled FROM users WHERE id=?", (user_id,))
            if not row or row["role"] != "admin" or not row["enabled"]:
                raise AppError(422, "INVALID_HIGH_RISK_POLICY", "管理员白名单只能包含启用的管理员")
        if enabled and allowed_tools and len(allowlist) < 2:
            # A live high-risk grant must be actionable only when two distinct
            # administrators can actually satisfy the required vote count;
            # otherwise it would create an approval that can never resolve.
            raise AppError(422, "HIGH_RISK_ADMIN_ALLOWLIST_REQUIRED", "启用高风险工具时管理员白名单至少需要两人")
        required = data.get("required_approvals", 2)
        if required != 2:
            raise AppError(422, "INVALID_HIGH_RISK_POLICY", "高风险策略必须要求两名不同管理员")
        previous = self.policy(assistant_id)
        revision = int(previous["revision"] if previous else 0) + 1
        self.service.db.execute(
            "INSERT INTO high_risk_approval_policies(assistant_id,enabled,policy_id,allowed_tools,admin_allowlist,required_approvals,revision,updated_at,updated_by) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(assistant_id) DO UPDATE SET enabled=excluded.enabled,policy_id=excluded.policy_id,allowed_tools=excluded.allowed_tools,admin_allowlist=excluded.admin_allowlist,required_approvals=excluded.required_approvals,revision=excluded.revision,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
            (assistant_id, int(enabled), policy_id.strip(), dump(allowed_tools), dump(allowlist), required, revision, now(), user["id"]),
        )
        return self.policy_view(assistant_id)

    @staticmethod
    def _operation(tool: str, arguments: dict) -> str:
        value = arguments.get("operation", arguments.get("action"))
        if isinstance(value, str) and value.strip():
            return value.strip().lower()[:100]
        return tool.rsplit("__", 1)[-1].lower()[:100]

    @classmethod
    def _explicit_high_risk(cls, tool: str, arguments: dict) -> bool:
        if tool_risk_profile(tool)["risk_level"] == "high":
            return True
        if tool.startswith("mcp__"):
            return True
        values = [tool]
        values.extend(str(arguments.get(key, "")) for key in ("operation", "action", "risk", "data_scope"))
        return any(marker in value.lower() for value in values for marker in HIGH_RISK_MARKERS)

    @classmethod
    def is_high_risk(cls, tool: str, arguments: dict) -> bool:
        """Expose the conservative classifier to local child orchestration.

        This is classification only; authorization still requires the current
        assistant policy and, for an enabled policy, two distinct allow votes.
        """
        return cls._explicit_high_risk(tool, arguments)

    def requires_dual(self, run: dict, tool: str, arguments: dict) -> bool:
        conversation = self.service.require("conversations", run["conversation_id"])
        policy = self.policy(conversation["assistant_id"])
        # The explicit policy is the production gate.  Dangerous operation
        # markers are also gated when a policy is enabled; absent policy keeps
        # legacy human approval compatible while remaining non-automatic.
        return bool(policy and policy["enabled"] and self._explicit_high_risk(tool, arguments))

    def request_snapshot(self, run: dict, tool: str, arguments: dict) -> dict | None:
        conversation = self.service.require("conversations", run["conversation_id"])
        policy = self.policy(conversation["assistant_id"])
        if not policy or not policy["enabled"] or not self._explicit_high_risk(tool, arguments):
            return None
        return {
            "approval_mode": "dual",
            "tool_allowed": tool in policy["allowed_tools"],
            "required_approvals": policy["required_approvals"],
            "approver_allowlist": policy["admin_allowlist"],
            "approval_policy_id": policy["policy_id"],
            "approval_policy_revision": policy["revision"],
            "approval_policy_digest": self._digest_policy(policy),
            "approval_request_digest": _digest({"tool": tool, "arguments": arguments}),
        }

    def snapshot_current(self, run: dict, tool: str, arguments: dict, snapshot: dict | None) -> bool:
        """Verify an approval snapshot immediately before high-risk execution.

        A final vote may wake the runtime just as an administrator changes the
        policy.  Rebuilding the snapshot here binds execution to the same
        policy revision, allowlist, tool and immutable argument digest that
        the two administrators actually reviewed.
        """
        if not snapshot:
            return False
        current = self.request_snapshot(run, tool, arguments)
        return current == snapshot

    def _row_policy_current(self, row: dict) -> bool:
        run = self.service.db.one("SELECT * FROM runs WHERE id=?", (row["run_id"],))
        if not run:
            return False
        conversation = self.service.db.one("SELECT assistant_id FROM conversations WHERE id=?", (run["conversation_id"],))
        policy = self.policy(conversation["assistant_id"]) if conversation else None
        return bool(policy and policy["enabled"] and policy["revision"] == row.get("approval_policy_revision") and self._digest_policy(policy) == row.get("approval_policy_digest"))

    def authorize_decider(self, row: dict, user: dict) -> None:
        if row.get("approval_mode") != "dual":
            return
        # The immutable request snapshot is checked against the current policy
        # before either vote.  A revoked policy or tool grant cannot be
        # rescued by a previously opened approval card.
        if not self._row_policy_current(row):
            raise AppError(409, "APPROVAL_POLICY_CHANGED", "高风险审批策略已变更，请重新发起任务")
        run = self.service.db.one("SELECT conversation_id FROM runs WHERE id=?", (row["run_id"],))
        conversation = self.service.db.one("SELECT assistant_id FROM conversations WHERE id=?", (run["conversation_id"],)) if run else None
        policy = self.policy(conversation["assistant_id"]) if conversation else None
        if policy and (not policy["allowed_tools"] or row["tool_name"] not in policy["allowed_tools"]):
            # A member/requester may still explicitly deny a request outside
            # the whitelist, but no identity may turn it into an allow.
            if row["user_id"] != user.get("id") or row.get("decision") == "allow":
                raise AppError(403, "HIGH_RISK_TOOL_NOT_ALLOWED", "工具不在高风险审批白名单")
        if user.get("role") != "admin" or not user.get("enabled"):
            if user.get("id") == row["user_id"]:
                return
            raise AppError(403, "ADMIN_APPROVAL_REQUIRED", "高风险操作只能由管理员审批")
        allowlist = json.loads(row.get("approver_allowlist") or "[]")
        if user["id"] not in allowlist and user["id"] != row["user_id"]:
            raise AppError(403, "ADMIN_APPROVAL_NOT_ALLOWED", "当前管理员不在高风险审批白名单")

    def record_vote(self, row: dict, user: dict, decision: str) -> dict:
        row = dict(row)
        if decision not in {"allow", "deny"}:
            raise AppError(422, "INVALID_APPROVAL_DECISION", "审批决定无效")
        row["decision"] = decision
        self.authorize_decider(row, user)
        if row.get("approval_mode") != "dual":
            return {"terminal": True, "status": "allowed" if decision == "allow" else "denied"}
        if row["status"] != "pending" or row["expires_at"] <= now():
            raise AppError(409, "APPROVAL_EXPIRED", "此审批已处理或过期")
        # A requester can terminate their own high-risk request with an
        # explicit deny, but can never contribute an allow vote.
        if user["id"] == row["user_id"] and decision == "allow":
            raise AppError(403, "REQUESTER_CANNOT_APPROVE", "任务发起人不能批准自己的高风险操作")
        if user["id"] not in json.loads(row.get("approver_allowlist") or "[]") and not (user["id"] == row["user_id"] and decision == "deny"):
            raise AppError(403, "ADMIN_APPROVAL_NOT_ALLOWED", "当前管理员不在高风险审批白名单")
        existing = self.service.db.one("SELECT * FROM approval_votes WHERE approval_id=? AND voter_id=?", (row["id"], user["id"]))
        if existing:
            raise AppError(409, "APPROVAL_DUPLICATE_VOTE", "同一管理员不能重复审批")
        stamp = now()
        self.service.db.execute("INSERT INTO approval_votes(approval_id,voter_id,decision,decided_at,role) VALUES(?,?,?,?,?)", (row["id"], user["id"], decision, stamp, user["role"]))
        if decision == "deny":
            self.service.db.execute("UPDATE approvals SET status='denied',decided_at=?,decided_by=?,decision_source='dual_admin',approval_decision_reason=? WHERE id=? AND status='pending'", (stamp, user["id"], "管理员拒绝", row["id"]))
            self._wake_waiter(row["id"], "deny")
            return {"terminal": True, "status": "denied", "votes": 1, "required": row["required_approvals"]}
        voters = self.service.db.all("SELECT voter_id FROM approval_votes WHERE approval_id=? AND decision='allow' ORDER BY decided_at", (row["id"],))
        count = len(voters)
        if count < int(row["required_approvals"]):
            ids = [item["voter_id"] for item in voters]
            self.service.db.execute("UPDATE approvals SET approver_ids=? WHERE id=? AND status='pending'", (dump(ids), row["id"]))
            return {"terminal": False, "status": "pending", "votes": count, "required": row["required_approvals"]}
        ids = [item["voter_id"] for item in voters]
        self.service.db.execute("UPDATE approvals SET status='allowed',decided_at=?,decided_by=?,decision_source='dual_admin',approver_ids=? WHERE id=? AND status='pending'", (stamp, ids[-1], dump(ids), row["id"]))
        self._wake_waiter(row["id"], "allow")
        return {"terminal": True, "status": "allowed", "votes": count, "required": row["required_approvals"]}

    def _wake_waiter(self, approval_id: str, decision: str) -> None:
        """Wake an in-process child waiter when a dual vote becomes terminal.

        The HTTP route also performs this check for ordinary runtime approvals;
        keeping it here makes the same durable vote contract safe for the
        synthetic sub-agent path and remains idempotent when both paths run.
        """
        manager = getattr(self.service, "manager", None)
        future = getattr(manager, "approvals", {}).get(approval_id) if manager else None
        if future and not future.done():
            future.set_result(decision)
