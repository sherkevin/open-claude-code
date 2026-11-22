"""Durable lifecycle for bounded synthetic children and isolated Harness workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from .db import dump, now, offload, offload_write, uid
from .security import AppError
from .subagent_title import display_title, display_title_key


CHILD_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _task_digest(task: dict) -> str:
    """Bind idempotency to the complete synthetic task specification."""
    return _digest(dump(task))


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise AppError(422, "SUBAGENT_INVALID_TASK", "子任务必须是文本")
    value = value.strip()
    if not value:
        raise AppError(422, "SUBAGENT_INVALID_TASK", "子任务不能为空")
    if len(value) > limit:
        raise AppError(422, "SUBAGENT_TASK_TOO_LARGE", "子任务超过长度限制")
    return value


class SubagentOrchestrator:
    """Persist and execute one bounded child plan per parent run."""

    def __init__(
        self,
        service,
        executor: Callable[[dict, dict], Awaitable[dict]] | None = None,
        *,
        max_children: int = 4,
        max_depth: int = 1,
        max_task_chars: int = 2000,
        max_result_chars: int = 4000,
        child_timeout_seconds: float = 5.0,
    ):
        self.service = service
        self.executor = executor or self._synthetic_executor
        self.max_children = max(1, min(int(max_children), 4))
        self.max_depth = max(0, min(int(max_depth), 1))
        self.max_task_chars = max(1, int(max_task_chars))
        self.max_result_chars = max(256, int(max_result_chars))
        self.child_timeout_seconds = max(0.1, float(child_timeout_seconds))
        self._plan_tasks: dict[str, dict[str, asyncio.Task]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._cancelled_plans: set[str] = set()
        self._worker_capacity = asyncio.Semaphore(max(1, min(2, service.settings.max_active_sessions - 1)))

    # ------------------------------------------------------------------
    # Contract and persistence
    # ------------------------------------------------------------------
    def _parent(self, parent_run_id: str, user_id: str | None = None, *, allow_terminal: bool = False) -> dict:
        parent = self.service.db.one("SELECT * FROM runs WHERE id=?", (parent_run_id,))
        if not parent or (user_id is not None and parent["user_id"] != user_id):
            raise AppError(404, "NOT_FOUND", "父任务不存在或无访问权限")
        owner = self.service.db.one("SELECT enabled FROM users WHERE id=?", (parent["user_id"],))
        if not owner or not owner["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        if not allow_terminal and parent["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise AppError(409, "SUBAGENT_PARENT_TERMINAL", "父任务已结束，不能再拆分子任务")
        return parent

    def _parent_depth(self, parent_run_id: str) -> int:
        row = self.service.db.one("SELECT depth FROM subagent_children WHERE child_id=?", (parent_run_id,))
        return int(row["depth"]) if row else 0

    def _normalize_tasks(self, tasks: Iterable[Any]) -> list[dict]:
        if isinstance(tasks, (str, bytes)) or not isinstance(tasks, Iterable):
            raise AppError(422, "SUBAGENT_INVALID_TASKS", "子任务必须是列表")
        values = list(tasks)
        if not values or len(values) > self.max_children:
            raise AppError(422, "SUBAGENT_CHILD_LIMIT", f"子任务数量必须在 1 到 {self.max_children} 之间")
        normalized = []
        for item in values:
            if isinstance(item, str):
                task = item
                behavior = "success"
                delay_ms = 0
                result = None
            elif isinstance(item, dict):
                task = item.get("task", item.get("prompt"))
                behavior = item.get("behavior", "success")
                delay_ms = item.get("delay_ms", 0)
                result = item.get("result")
                tool = item.get("tool")
                arguments = item.get("arguments", {})
            else:
                raise AppError(422, "SUBAGENT_INVALID_TASK", "子任务格式无效")
            if isinstance(item, str):
                tool, arguments = None, {}
            task = _bounded_text(task, self.max_task_chars)
            if behavior not in {"success", "fail", "delay", "cancel"}:
                raise AppError(422, "SUBAGENT_INVALID_TASK", "合成子任务行为不受支持")
            if isinstance(delay_ms, bool) or not isinstance(delay_ms, (int, float)) or not math.isfinite(float(delay_ms)) or delay_ms < 0 or delay_ms > 5000:
                raise AppError(422, "SUBAGENT_INVALID_TASK", "延迟参数超出范围")
            if result is not None:
                try:
                    encoded = dump(result)
                except (TypeError, ValueError) as exc:
                    raise AppError(422, "SUBAGENT_INVALID_TASK", "结果必须是可序列化对象") from exc
                if len(encoded) > self.max_result_chars:
                    raise AppError(422, "SUBAGENT_RESULT_TOO_LARGE", "子任务结果超过长度限制")
            if tool is not None:
                if not isinstance(tool, str) or not tool.strip() or len(tool) > 200:
                    raise AppError(422, "SUBAGENT_INVALID_TASK", "子任务工具标识无效")
                if not isinstance(arguments, dict):
                    raise AppError(422, "SUBAGENT_INVALID_TASK", "子任务工具参数必须是对象")
                try:
                    encoded_arguments = dump(arguments)
                except (TypeError, ValueError) as exc:
                    raise AppError(422, "SUBAGENT_INVALID_TASK", "子任务工具参数不可序列化") from exc
                if len(encoded_arguments) > self.max_task_chars:
                    raise AppError(422, "SUBAGENT_TASK_TOO_LARGE", "子任务工具参数超过长度限制")
                tool = tool.strip()
            normalized.append({"task": task, "behavior": behavior, "delay_ms": delay_ms, "result": result,
                               "tool": tool, "arguments": arguments})
        return normalized

    def create_plan(self, parent_run_id: str, user_id: str, tasks: Iterable[Any], mode: str = "synthetic") -> dict:
        with self.service.db.lock:
            return self._create_plan(parent_run_id, user_id, tasks, mode)

    def _create_plan(self, parent_run_id: str, user_id: str, tasks: Iterable[Any], mode: str) -> dict:
        if mode not in {"synthetic", "harness"}:
            raise AppError(422, "SUBAGENT_MODE_DISABLED", "不支持所请求的子任务模式")
        parent = self._parent(parent_run_id, user_id)
        harness = mode == "harness"
        if harness:
            settings = self.service.settings
            metadata = json.loads(parent.get("execution_meta") or "{}")
            owner = self.service.require("users", user_id)
            conversation = self.service.require("conversations", parent["conversation_id"], owner)
            self.service.usable_assistant(conversation["assistant_id"], owner)
            if (self.service.policy.is_trial or conversation["archived"] or conversation["active_run_id"] != parent_run_id
                    or conversation["runtime_type"] != "harness" or parent.get("kind", "chat") != "chat"
                    or parent["status"] != "running" or metadata.get("recovery_mode") == "answer_only" or metadata.get("schedule_id")
                    or not settings.harness_source_dir or not settings.harness_node_path or settings.max_active_sessions < 2):
                raise AppError(409, "SUBAGENT_PARENT_INACTIVE", "当前运行不支持创建分析子任务")
            if not isinstance(tasks, list) or any(not isinstance(task, str) for task in tasks):
                raise AppError(422, "SUBAGENT_INVALID_TASK", "分析子任务仅接受文字说明")
        depth = self._parent_depth(parent_run_id) + 1
        if depth > self.max_depth:
            raise AppError(422, "SUBAGENT_DEPTH_LIMIT", "子任务嵌套层级超过限制")
        normalized = self._normalize_tasks(tasks)
        existing = self.service.db.all("SELECT * FROM subagent_children WHERE parent_run_id=? ORDER BY rowid", (parent_run_id,))
        if existing:
            # Idempotent plan creation is safe only when the requested shape is
            # identical; never create a second fan-out for the same parent.
            requested = [_task_digest(item) for item in normalized]
            if json.loads(existing[0]["capability_snapshot"])["mode"] != mode or [row["task_digest"] for row in existing] != requested:
                raise AppError(409, "SUBAGENT_PLAN_EXISTS", "父任务已有不同的子任务计划")
            return self.plan_view(parent_run_id)

        timestamp = now()
        capability = {
            "mode": mode,
            "tools": [] if harness else ["Read", "Glob", "Grep"],
            "writes": False,
            "network": False,
            "mcp": False,
            "parent_history": False,
            "parent_memory": False,
        }
        child_root = self.service.settings.data_dir / "workspaces" / parent["conversation_id"] / "subagents"
        rows = []
        created_roots = []
        try:
            for item in normalized:
                child_id = uid()
                child_directory = child_root / child_id
                workspace_directory = child_directory / "workspace"
                memory_directory = child_directory / "memory"
                workspace_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
                memory_directory.mkdir(parents=False, exist_ok=False, mode=0o700)
                workspace_directory.chmod(0o700)
                memory_directory.chmod(0o700)
                created_roots.append(child_directory)
                rows.append((
                    child_id,
                    parent_run_id,
                    parent.get("root_run_id") or parent_run_id,
                    parent["user_id"],
                    parent["conversation_id"],
                    depth,
                    "harness_analysis" if harness else "synthetic_readonly",
                    "queued",
                    None,
                    _task_digest(item),
                    dump(capability),
                    f"subagents/{child_id}/workspace",
                    f"subagents/{child_id}/memory",
                    0.0,
                    dump({}),
                    timestamp,
                    None,
                    None,
                    None,
                    None,
                    display_title_key(item["task"]),
                ))
            with self.service.db.transaction():
                self.service.db.executemany(
                    "INSERT INTO subagent_children(child_id,parent_run_id,root_run_id,user_id,conversation_id,depth,role,status,stop_reason,task_digest,capability_snapshot,workspace_id,memory_scope_id,reserved_budget,observed_usage,created_at,started_at,completed_at,cancelled_at,result_json,display_title_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
        except BaseException:
            for directory in created_roots:
                shutil.rmtree(directory, ignore_errors=True)
            raise
        self.remember_inputs(parent_run_id, normalized)
        self._emit(parent_run_id, "subagent.plan.created", {
            "plan_id": parent_run_id,
            "mode": mode,
            "depth": depth,
            "max_children": self.max_children,
            "children": [self._safe_child_summary(row[0]) for row in rows],
        })
        return self.plan_view(parent_run_id)

    def _emit(self, parent_run_id: str, event_type: str, data: dict) -> None:
        # Every payload is a fixed, redacted summary.  Task text, private
        # history, credentials and executor diagnostics never enter parent SSE.
        self.service.db.event(parent_run_id, event_type, data)

    def _plan_completed(self, parent_run_id: str) -> bool:
        return any(event["type"] == "subagent.plan.completed" and event["data"].get("plan_id") == parent_run_id for event in self.service.db.events(parent_run_id, 0))

    def _safe_child_summary(self, child_id: str) -> dict:
        row = self.service.db.one("SELECT * FROM subagent_children WHERE child_id=?", (child_id,))
        if not row:
            return {"child_id": child_id}
        return {
            "child_id": row["child_id"],
            "status": row["status"],
            "depth": row["depth"],
            "role": row["role"],
            "task_digest": row["task_digest"],
        }

    def child_view(self, row: dict) -> dict:
        result = {
            "child_id": row["child_id"],
            "parent_run_id": row["parent_run_id"],
            "root_run_id": row["root_run_id"],
            "conversation_id": row["conversation_id"],
            "depth": row["depth"],
            "role": row["role"],
            "status": row["status"],
            "stop_reason": row["stop_reason"],
            "task_digest": row["task_digest"],
            "capability_snapshot": json.loads(row["capability_snapshot"]),
            "workspace_id": row["workspace_id"],
            "memory_scope_id": row["memory_scope_id"],
            "isolation": "dedicated_workspace_and_memory",
            "reserved_budget": row["reserved_budget"],
            "observed_usage": json.loads(row["observed_usage"] or "{}"),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "cancelled_at": row["cancelled_at"],
        }
        if row.get("result_json"):
            result["result"] = json.loads(row["result_json"])
        return result

    def list_children(self, parent_run_id: str, user_id: str | None = None, *, include_display_titles: bool = False) -> list[dict]:
        self._parent(parent_run_id, user_id, allow_terminal=True)
        children = []
        for row in self.service.db.all("SELECT * FROM subagent_children WHERE parent_run_id=? ORDER BY rowid", (parent_run_id,)):
            child = self.child_view(row)
            if include_display_titles:
                title = display_title(row.get("display_title_key"))
                if title:
                    child["display_title"] = title
            children.append(child)
        return children

    def plan_view(self, parent_run_id: str, user_id: str | None = None, *, include_display_titles: bool = False) -> dict:
        children = self.list_children(parent_run_id, user_id, include_display_titles=include_display_titles)
        counts = {status: sum(1 for child in children if child["status"] == status) for status in ("queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted")}
        return {"plan_id": parent_run_id, "mode": children[0]["capability_snapshot"]["mode"] if children else "synthetic", "children": children, "counts": counts}

    # ------------------------------------------------------------------
    # Deterministic local executor and lifecycle
    # ------------------------------------------------------------------
    async def _synthetic_executor(self, child: dict, task: dict) -> dict:
        behavior = task["behavior"]
        delay = float(task.get("delay_ms", 0)) / 1000.0
        if behavior == "delay":
            # Deliberately exceeds the default timeout when no custom delay is
            # supplied, giving tests a deterministic timeout path.
            delay = max(delay, self.child_timeout_seconds * 2)
        if delay:
            await asyncio.sleep(delay)
        if behavior == "fail":
            raise RuntimeError("synthetic child failure")
        if behavior == "cancel":
            raise asyncio.CancelledError()
        result = task.get("result")
        if result is None:
            result = {"status": "ok", "task_digest": child["task_digest"]}
        return {"output": result, "stop_reason": "completed", "usage": {"input_units": 0, "output_units": 0}}

    def _child_high_risk_spec(self, task: dict) -> tuple[str | None, dict] | None:
        """Flag high-risk requests for approval, or deny undeclared tools."""
        tool = task.get("tool")
        arguments = task.get("arguments") or {}
        if tool is None:
            candidate = str(task.get("task", ""))
            if any(marker in candidate.lower() for marker in {
                "delete", "destroy", "drop", "remove", "bulk", "batch",
                "publish", "release", "external", "credential", "secret",
                "token", "password",
            }):
                return None, {}
            return None
        if self.service.high_risk_approval.is_high_risk(tool, arguments):
            return tool, arguments
        return None

    def _insert_child_approval(self, run: dict, child_id: str, tool: str, arguments: dict, snapshot: dict | None) -> str:
        """Persist a redacted-by-view approval snapshot owned by the parent."""
        approval_id = uid()
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=self.service.settings.approval_seconds)).isoformat()
        if snapshot:
            pending = bool(snapshot.get("tool_allowed"))
            values = (
                approval_id, run["id"], run["user_id"], tool, dump(arguments), "pending" if pending else "denied", now(), expiry,
                "dual", snapshot["approval_mode"], snapshot["required_approvals"], dump(snapshot["approver_allowlist"]), "[]",
                snapshot["approval_policy_id"], snapshot["approval_policy_revision"], snapshot["approval_policy_digest"],
                snapshot["approval_request_digest"], child_id,
            )
        else:
            values = (
                approval_id, run["id"], run["user_id"], tool, dump(arguments), "denied", now(), expiry,
                "dual", "dual", 2, "[]", "[]", None, None, None, None, child_id,
            )
        self.service.db.execute(
            "INSERT INTO approvals(id,run_id,user_id,tool_name,arguments,status,created_at,expires_at,strategy,approval_mode,required_approvals,approver_allowlist,approver_ids,approval_policy_id,approval_policy_revision,approval_policy_digest,approval_request_digest,subagent_child_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        return approval_id

    def _approval_view(self, approval_id: str) -> dict:
        # Import lazily to avoid the service -> subagents import cycle.
        from .service import approval_view
        return approval_view(self.service.require("approvals", approval_id))

    async def _await_child_approval(self, parent_run_id: str, child_id: str, task: dict) -> bool:
        spec = self._child_high_risk_spec(task)
        if not spec:
            return True
        run = self.service.db.one("SELECT * FROM runs WHERE id=?", (parent_run_id,))
        if not run:
            return False
        tool, arguments = spec
        snapshot = self.service.high_risk_approval.request_snapshot(run, tool, arguments) if tool is not None else None
        # Dual approval requires a declared tool and an enabled parent policy.
        # Record a fixed identifier for suspicious tasks with no declared tool.
        approval_id = self._insert_child_approval(run, child_id, tool or "subagent_undeclared_tool", arguments, snapshot)
        if not snapshot or not snapshot["tool_allowed"]:
            reason = "父任务未配置高风险双人审批策略，默认拒绝" if not snapshot else "工具不在父任务高风险审批白名单，默认拒绝"
            if tool is None:
                reason = "子任务未声明可审批的工具，默认拒绝"
            decided_at = now()
            self.service.db.execute(
                "UPDATE approvals SET decided_at=?,decision_source='policy',approval_decision_reason=? WHERE id=?",
                (decided_at, reason, approval_id),
            )
            self._emit(parent_run_id, "subagent.approval.denied", {
                "plan_id": parent_run_id, "child_id": child_id, "approval_id": approval_id,
                "decision": "deny", "decision_source": "policy",
            })
            return False

        future = asyncio.get_running_loop().create_future()
        manager = getattr(self.service, "manager", None)
        if not manager:
            self.service.db.execute(
                "UPDATE approvals SET status='denied',decided_at=?,decision_source='policy',approval_decision_reason=? WHERE id=? AND status='pending'",
                (now(), "审批管理器不可用，默认拒绝", approval_id),
            )
            self._emit(parent_run_id, "subagent.approval.denied", {
                "plan_id": parent_run_id, "child_id": child_id, "approval_id": approval_id,
                "decision": "deny", "decision_source": "policy",
            })
            return False
        manager.approvals[approval_id] = future
        self._emit(parent_run_id, "subagent.approval.required", {
            "plan_id": parent_run_id, "child_id": child_id, "approval_id": approval_id,
            "approval": self._approval_view(approval_id),
        })
        decision = "deny"
        source = "timeout"
        try:
            decision = await asyncio.wait_for(future, self.service.settings.approval_seconds)
            source = "dual_admin"
            # The final vote can change the durable row to ``allowed`` before
            # this child waiter resumes.  Revalidate the inherited parent
            # policy and exact argument digest at that boundary as well, so a
            # revocation racing with the wake-up cannot run a stale child.
            if decision == "allow" and (parent_run_id in self._cancelled_plans or (snapshot and not self.service.high_risk_approval.snapshot_current(run, tool, arguments, snapshot))):
                reason = "父任务已取消，审批结果不再有效" if parent_run_id in self._cancelled_plans else "高风险审批策略在最终投票后发生变化，已安全拒绝"
                source = "parent_cancelled" if parent_run_id in self._cancelled_plans else "policy"
                invalidated = self.service.db.execute(
                    "UPDATE approvals SET status='denied',decision_source=?,approval_decision_reason=? WHERE id=? AND status='allowed'",
                    (source, reason, approval_id),
                )
                # Even if another terminal transition won the update race,
                # this child must not treat its stale allow as executable.
                decision = "deny"
        except asyncio.TimeoutError:
            source = "timeout"
        except asyncio.CancelledError:
            source = "parent_cancelled" if parent_run_id in self._cancelled_plans else "cancelled"
            raise
        finally:
            manager.approvals.pop(approval_id, None)
            self.service.db.execute(
                "UPDATE approvals SET status=?,decided_at=COALESCE(decided_at,?),decision_source=COALESCE(decision_source,?),approval_decision_reason=COALESCE(approval_decision_reason,?) WHERE id=? AND status='pending'",
                ("allowed" if decision == "allow" else "expired", now(), source, None if decision == "allow" else "高风险子任务审批未获批准", approval_id),
            )
            row = self.service.db.one("SELECT status,decision_source,decided_at FROM approvals WHERE id=?", (approval_id,))
            self._emit(parent_run_id, "subagent.approval.resolved", {
                "plan_id": parent_run_id, "child_id": child_id, "approval_id": approval_id,
                "decision": decision if row and row["status"] == "allowed" else "deny",
                "decision_source": row["decision_source"] if row else source,
                "decision_at": row["decided_at"] if row else None,
            })
        return decision == "allow"

    async def _run_child(self, parent_run_id: str, child_id: str, task: dict, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            row = self.service.db.one("SELECT * FROM subagent_children WHERE child_id=?", (child_id,))
            if not row:
                return
            if parent_run_id in self._cancelled_plans:
                self._mark_child(child_id, "cancelled", "parent_cancelled", cancelled=True)
                self._emit_child(parent_run_id, child_id, "subagent.cancelled", "parent_cancelled")
                return
            started = self.service.db.execute("UPDATE subagent_children SET status='running',started_at=? WHERE child_id=? AND status='queued'", (now(), child_id))
            if started.rowcount != 1:
                return
            self._emit_child(parent_run_id, child_id, "subagent.started", "running", progress=0)
            self._emit_child(parent_run_id, child_id, "subagent.progress", "running", progress=10)
            child = self.service.db.one("SELECT * FROM subagent_children WHERE child_id=?", (child_id,))
            try:
                if not await self._await_child_approval(parent_run_id, child_id, task):
                    self._mark_child(child_id, "failed", "approval_denied")
                    self._emit_child(parent_run_id, child_id, "subagent.failed", "approval_denied")
                    return
                if child["role"] == "harness_analysis":
                    from .harness_child import execute_harness_child

                    async with self._worker_capacity:
                        result = await asyncio.wait_for(execute_harness_child(self.service, child, task, self.max_result_chars - 256), timeout=90)
                else:
                    result = await asyncio.wait_for(self.executor(child, task), timeout=self.child_timeout_seconds)
                encoded = dump(result)
                if len(encoded) > self.max_result_chars:
                    raise ValueError("child result exceeds bound")
                usage = result.get("usage", {}) if isinstance(result, dict) else {}
                updated = self.service.db.execute("UPDATE subagent_children SET status='succeeded',stop_reason='completed',observed_usage=?,result_json=?,completed_at=? WHERE child_id=? AND status='running'", (dump(usage), dump({"output": result.get("output") if isinstance(result, dict) else result, "stop_reason": "completed"}), now(), child_id))
                if updated.rowcount != 1:
                    # A concurrent parent cancellation wins over a result that
                    # finished at the same instant; never resurrect a child.
                    latest = self.service.db.one("SELECT status FROM subagent_children WHERE child_id=?", (child_id,))
                    if latest and latest["status"] in {"cancelling", "queued", "running"}:
                        self._mark_child(child_id, "cancelled", "parent_cancelled", cancelled=True)
                        self._emit_child(parent_run_id, child_id, "subagent.cancelled", "parent_cancelled")
                    return
                self._emit_child(parent_run_id, child_id, "subagent.progress", "succeeded", progress=100)
                self._emit_child(parent_run_id, child_id, "subagent.completed", "completed", progress=100)
            except asyncio.TimeoutError:
                self._mark_child(child_id, "interrupted", "timeout")
                self._emit_child(parent_run_id, child_id, "subagent.failed", "timeout")
            except asyncio.CancelledError:
                reason = "parent_cancelled" if parent_run_id in self._cancelled_plans else "cancelled"
                self._mark_child(child_id, "cancelled", reason, cancelled=True)
                self._emit_child(parent_run_id, child_id, "subagent.cancelled", reason)
                raise
            except Exception:
                self._mark_child(child_id, "failed", "executor_error")
                self._emit_child(parent_run_id, child_id, "subagent.failed", "executor_error")

    def _mark_child(self, child_id: str, status: str, reason: str, *, cancelled: bool = False) -> None:
        stamp = now()
        self.service.db.execute("UPDATE subagent_children SET status=?,stop_reason=?,completed_at=?,cancelled_at=CASE WHEN ? THEN COALESCE(cancelled_at,?) ELSE cancelled_at END WHERE child_id=? AND status NOT IN ('succeeded','failed','cancelled','interrupted')", (status, reason, stamp, int(cancelled), stamp, child_id))

    def _emit_child(self, parent_run_id: str, child_id: str, event_type: str, stage: str, progress: int | None = None) -> None:
        data = {"plan_id": parent_run_id, "child_id": child_id, "stage": stage}
        if progress is not None:
            data["progress"] = progress
        self._emit(parent_run_id, event_type, data)

    async def run_plan(self, parent_run_id: str, user_id: str | None = None) -> dict:
        # Once a plan is durably accepted, its local children may finish even
        # if the chat parent reaches a terminal state in the meantime.
        parent = self._parent(parent_run_id, user_id, allow_terminal=True)
        children = self.service.db.all("SELECT * FROM subagent_children WHERE parent_run_id=? ORDER BY rowid", (parent_run_id,))
        if not children:
            raise AppError(409, "SUBAGENT_PLAN_MISSING", "父任务没有子任务计划")
        if parent_run_id in self._cancelled_plans or all(row["status"] in CHILD_TERMINAL for row in children):
            return self.plan_view(parent_run_id, user_id)
        if parent_run_id in self._plan_tasks:
            await asyncio.gather(*self._plan_tasks[parent_run_id].values(), return_exceptions=True)
            return self.plan_view(parent_run_id, user_id)
        # Task prompts are intentionally not persisted.  The deterministic API
        # accepts a second invocation only for already queued plans via the
        # in-memory plan payload retained for this process.
        payload = getattr(self, "_plan_inputs", {}).get(parent_run_id)
        if payload is None:
            self._mark_unreplayable(parent_run_id)
            return self.plan_view(parent_run_id, user_id)
        semaphore = asyncio.Semaphore(max(1, min(self.max_children, int(self.service.settings.max_active_sessions) - 1)))
        self._plan_tasks[parent_run_id] = {}
        for row, task in zip(children, payload, strict=True):
            self._plan_tasks[parent_run_id][row["child_id"]] = asyncio.create_task(self._run_child(parent_run_id, row["child_id"], task, semaphore))
        await asyncio.gather(*self._plan_tasks[parent_run_id].values(), return_exceptions=True)
        self._plan_tasks.pop(parent_run_id, None)
        children = self.service.db.all("SELECT * FROM subagent_children WHERE parent_run_id=? ORDER BY created_at,child_id", (parent_run_id,))
        if parent_run_id in self._cancelled_plans or self._plan_completed(parent_run_id):
            # cancel_plan owns the single terminal plan event for cascaded
            # cancellation; do not emit a duplicate from this waiter.
            getattr(self, "_plan_inputs", {}).pop(parent_run_id, None)
            return self.plan_view(parent_run_id, user_id)
        summary = {"succeeded": sum(row["status"] == "succeeded" for row in children), "failed": sum(row["status"] == "failed" for row in children), "cancelled": sum(row["status"] == "cancelled" for row in children), "interrupted": sum(row["status"] == "interrupted" for row in children), "total": len(children)}
        status = "succeeded" if summary["succeeded"] == summary["total"] else "partial" if summary["succeeded"] else "failed"
        self._emit(parent_run_id, "subagent.plan.completed", {"plan_id": parent_run_id, "status": status, "summary": summary})
        getattr(self, "_plan_inputs", {}).pop(parent_run_id, None)
        return self.plan_view(parent_run_id, user_id)

    async def cancel_plan(self, parent_run_id: str, user_id: str | None = None) -> dict:
        # Cancellation runs on the event loop and its SQLite work is offloaded
        # like the parent run's own terminal transaction, so cancelling a run
        # with synthetic children cannot stall SSE or other sessions.
        await offload(lambda: self._parent(parent_run_id, user_id, allow_terminal=True))
        rows = await offload(self.service.db.all, "SELECT child_id,status FROM subagent_children WHERE parent_run_id=?", (parent_run_id,))
        if not rows:
            return {"plan_id": parent_run_id, "mode": "synthetic", "children": [], "counts": {}}
        if all(row["status"] in CHILD_TERMINAL for row in rows):
            return await offload(self.plan_view, parent_run_id, user_id)
        if parent_run_id in self._cancelled_plans or await offload(self._plan_completed, parent_run_id):
            return await offload(self.plan_view, parent_run_id, user_id)
        self._cancelled_plans.add(parent_run_id)
        await offload_write(self.service.db.execute, "UPDATE subagent_children SET status='cancelling' WHERE parent_run_id=? AND status IN ('queued','running')", (parent_run_id,))
        for task in list(self._plan_tasks.get(parent_run_id, {}).values()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*(self._plan_tasks.get(parent_run_id, {}).values()), return_exceptions=True)
        remaining = await offload(self.service.db.all, "SELECT child_id,status FROM subagent_children WHERE parent_run_id=? AND status IN ('queued','running','cancelling')", (parent_run_id,))
        for row in remaining:
            await offload_write(lambda row=row: self._mark_child(row["child_id"], "cancelled", "parent_cancelled", cancelled=True))
            await offload_write(self._emit_child, parent_run_id, row["child_id"], "subagent.cancelled", "parent_cancelled")
        if not await offload(self._plan_completed, parent_run_id):
            await offload_write(self._emit, parent_run_id, "subagent.plan.completed", {"plan_id": parent_run_id, "status": "cancelled", "summary": await offload(self._summary, parent_run_id)})
        getattr(self, "_plan_inputs", {}).pop(parent_run_id, None)
        self._cancelled_plans.discard(parent_run_id)
        return await offload(self.plan_view, parent_run_id, user_id)

    def _summary(self, parent_run_id: str) -> dict:
        rows = self.service.db.all("SELECT status FROM subagent_children WHERE parent_run_id=?", (parent_run_id,))
        return {"succeeded": sum(row["status"] == "succeeded" for row in rows), "failed": sum(row["status"] == "failed" for row in rows), "cancelled": sum(row["status"] == "cancelled" for row in rows), "interrupted": sum(row["status"] == "interrupted" for row in rows), "total": len(rows)}

    def _mark_unreplayable(self, parent_run_id: str) -> None:
        """Close accepted work after restart without replaying task input."""
        for row in self.service.db.all("SELECT child_id FROM subagent_children WHERE parent_run_id=? AND status IN ('queued','running','cancelling')", (parent_run_id,)):
            self._mark_child(row["child_id"], "interrupted", "not_replayable")
            self._emit_child(parent_run_id, row["child_id"], "subagent.failed", "not_replayable")
        if not self._plan_completed(parent_run_id):
            self._emit(parent_run_id, "subagent.plan.completed", {"plan_id": parent_run_id, "status": "interrupted", "summary": self._summary(parent_run_id)})
        getattr(self, "_plan_inputs", {}).pop(parent_run_id, None)

    def recover(self) -> None:
        parents = set()
        for row in self.service.db.all("SELECT child_id,parent_run_id FROM subagent_children WHERE status IN ('queued','running','cancelling')"):
            parents.add(row["parent_run_id"])
            self._mark_child(row["child_id"], "interrupted", "recovered_after_restart")
            self._emit_child(row["parent_run_id"], row["child_id"], "subagent.failed", "recovered_after_restart")
        for parent_run_id in parents:
            if not self._plan_completed(parent_run_id):
                self._emit(parent_run_id, "subagent.plan.completed", {"plan_id": parent_run_id, "status": "interrupted", "summary": self._summary(parent_run_id)})

    async def close(self) -> None:
        for parent_run_id in list(self._plan_tasks):
            self._cancelled_plans.add(parent_run_id)
            for task in self._plan_tasks[parent_run_id].values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._plan_tasks[parent_run_id].values(), return_exceptions=True)
        pending_background = list(self._background_tasks)
        for task in pending_background:
            if not task.done():
                task.cancel()
        if pending_background:
            await asyncio.gather(*pending_background, return_exceptions=True)
        self._background_tasks.clear()
        self._plan_tasks.clear()
        self._cancelled_plans.clear()
        if hasattr(self, "_plan_inputs"):
            self._plan_inputs.clear()

    def start_plan(self, parent_run_id: str, user_id: str | None = None) -> asyncio.Task:
        """Schedule a plan while retaining its task until it settles."""
        task = asyncio.create_task(self.run_plan(parent_run_id, user_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled():
            # Consume an unexpected exception so a detached HTTP task cannot
            # produce an unhandled-task warning or silently leak its state.
            task.exception()

    def remember_inputs(self, parent_run_id: str, tasks: list[dict]) -> None:
        """Retain normalized synthetic prompts only for this process run."""
        if not hasattr(self, "_plan_inputs"):
            self._plan_inputs: dict[str, list[dict]] = {}
        self._plan_inputs[parent_run_id] = tasks
