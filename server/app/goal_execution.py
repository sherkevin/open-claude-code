"""Host admission, budget and lease boundary for the Goal execution pilot.

This module is deliberately not a round driver. It records the owner's
explicit execution approval and lets a future pinned official
``dsh-goal-round-driver`` obtain exactly one durable lease per native Goal
round. It never creates a run, calls a model, mounts a tool, or changes a
business permission.

The native Goal log remains the sole Goal state authority. The authorization
row binds the user's approval to the exact native goal id/revision/session and
to four independent limits: rounds, tokens, cost and wall-clock time. A lease
binds one existing real host run (``runs.id``) to that approval and is the
round fence: a stopped, restarted or superseded lease can never authorize
another internal prompt.

Usage comes from the run's existing usage receipt. Missing or incomplete
usage is recorded as ``unknown`` and blocks further rounds; it is never
treated as zero. The pinned Harness usage ledger currently reports no
provider cost, so a real Harness round settles as ``unknown`` until an
authoritative cost source exists. That is an intentional fail-closed result.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from .db import now, uid
from .security import AppError

GOAL_ID = re.compile(r"^goal-[a-f0-9-]{36}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DIGEST = re.compile(r"^[a-f0-9]{64}$")
STOP_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_ROUNDS = 32
MAX_TOKENS = 1_000_000_000_000
MAX_COST_MICROS = 1_000_000_000_000
MAX_TIME_SECONDS = 30 * 24 * 3600
TERMINAL_RUNS = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
ARM_ACTIONS = frozenset({"pause_resume", "resume"})
# Receipts from a loopback/synthetic provider are explicitly not production
# billing. They can satisfy a synthetic budget only in provider test mode and
# are always reported as non-authoritative.
SYNTHETIC_COST_SOURCES = frozenset({"synthetic_fixture", "loopback_test", "host_synthetic"})


def _integer(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def goal_execution_enabled(service) -> bool:
    settings = service.settings
    return bool(not service.policy.is_trial
                and getattr(settings, "harness_goal_state_enabled", False)
                and getattr(settings, "harness_goal_execution_enabled", False))


def require_goal_execution_enabled(service) -> None:
    # The product gate hides the whole surface while the pilot is off. It is
    # independent of the native Goal state switch and the trial boundary.
    if not goal_execution_enabled(service):
        raise AppError(404, "GOAL_EXECUTION_DISABLED", "持续目标自动执行试点尚未开启")


def require_goal_owner(service, conversation_id: str, user) -> dict:
    """Owner-only check usable even after a session change or archive.

    Viewing and stopping host records must not depend on the native session
    still being ready. Authorization and admission use the stricter
    ``_conversation`` check instead.
    """
    row = service.require("conversations", conversation_id, user)
    owner = service.require("users", row["user_id"])
    if not owner["enabled"]:
        raise AppError(403, "USER_DISABLED", "账号已停用")
    return {**row, "owner": owner}


def _normalize_request(request: Mapping) -> dict:
    if not isinstance(request, Mapping):
        raise AppError(422, "GOAL_AUTHORIZATION_INVALID", "目标执行授权内容无效")
    allowed = {"goal_id", "goal_revision", "harness_session_id", "max_rounds",
               "token_budget", "cost_budget_micros", "time_budget_seconds", "confirm"}
    if set(request) != allowed or request.get("confirm") is not True:
        raise AppError(422, "GOAL_AUTHORIZATION_INVALID", "请确认目标、轮次、用量、费用和时间预算")
    goal_id, revision = request.get("goal_id"), request.get("goal_revision")
    session = request.get("harness_session_id")
    rounds, tokens = request.get("max_rounds"), request.get("token_budget")
    cost, seconds = request.get("cost_budget_micros"), request.get("time_budget_seconds")
    if (not isinstance(goal_id, str) or GOAL_ID.fullmatch(goal_id) is None
            or not _integer(revision, 1, 10**9)
            or not isinstance(session, str) or SESSION_ID.fullmatch(session) is None
            or not _integer(rounds, 1, MAX_ROUNDS)
            or not _integer(tokens, 1, MAX_TOKENS)
            or not _integer(cost, 1, MAX_COST_MICROS)
            or not _integer(seconds, 60, MAX_TIME_SECONDS)):
        raise AppError(422, "GOAL_AUTHORIZATION_INVALID", "目标执行授权的预算边界无效")
    return {"goal_id": goal_id, "goal_revision": revision, "harness_session_id": session,
            "max_rounds": rounds, "token_budget": tokens, "cost_budget_micros": cost,
            "time_budget_seconds": seconds}


def _goal_snapshot(goal: Mapping, harness_session_id: str, *, objective_required: bool = False) -> dict:
    if not isinstance(goal, Mapping):
        raise AppError(422, "GOAL_SNAPSHOT_INVALID", "原生目标快照无效")
    goal_id, revision = goal.get("id"), goal.get("revision")
    phase, started = goal.get("phase"), goal.get("roundsStarted")
    maximum, activation = goal.get("maxGoalRounds"), goal.get("activation")
    objective = goal.get("objective")
    if objective is not None and (not isinstance(objective, str) or not objective.strip()
                                  or len(objective) > 8000):
        raise AppError(422, "GOAL_SNAPSHOT_INVALID", "原生目标内容无效")
    if objective_required and objective is None:
        raise AppError(422, "GOAL_SNAPSHOT_INVALID", "原生目标内容不可核验")
    if (not isinstance(goal_id, str) or GOAL_ID.fullmatch(goal_id) is None
            or not _integer(revision, 1, 10**9)
            or not isinstance(harness_session_id, str)
            or SESSION_ID.fullmatch(harness_session_id) is None
            or not _integer(started, 0, MAX_ROUNDS)
            or not _integer(maximum, 1, MAX_ROUNDS)
            or (activation is not None and activation not in {"armed", "disarmed"})
            or started > maximum or phase not in {"active", "paused", "blocked", "complete"}):
        raise AppError(422, "GOAL_SNAPSHOT_INVALID", "原生目标快照无效")
    return {"id": goal_id, "revision": revision, "phase": phase,
            "roundsStarted": started, "maxGoalRounds": maximum,
            "objective": objective, "activation": activation}


def objective_digest(objective: str) -> str:
    """Bind an approval to the exact goal text without persisting that text."""
    return hashlib.sha256(objective.encode("utf-8")).hexdigest()


def _conversation(service, conversation_id: str, user=None) -> dict:
    row = (service.require("conversations", conversation_id, user) if user is not None
           else service.require("conversations", conversation_id))
    owner = service.require("users", row["user_id"])
    if not owner["enabled"]:
        raise AppError(403, "USER_DISABLED", "账号已停用")
    if row.get("runtime_type") != "harness" or not row.get("harness_session_id"):
        raise AppError(409, "GOAL_SESSION_NOT_READY", "请先完成一次 Harness 对话")
    if row.get("archived"):
        raise AppError(409, "GOAL_SESSION_ARCHIVED", "已归档会话不能继续目标")
    return {**row, "owner": owner}


def _permission_still_valid(conversation: Mapping, authorization: Mapping) -> bool:
    mode = conversation.get("permission_mode", "manual")
    if authorization["permission_mode"] != mode:
        return False
    # A downgraded account must not retain a full-permission continuation.
    if mode == "full" and conversation["owner"].get("role") != "admin":
        return False
    return True


def _require_assistant_access(service, conversation: Mapping) -> Mapping:
    try:
        return service.usable_assistant(conversation["assistant_id"], conversation["owner"])
    except AppError:
        # Disabling the assistant or removing its access is a permission
        # change and must stop future continuation, not merely fail one tool.
        raise AppError(409, "GOAL_PERMISSION_CHANGED", "助手或访问权限已变化，本目标不再续行") from None


def _require_assistant_revision(service, conversation: Mapping, authorization: Mapping) -> None:
    assistant = _require_assistant_access(service, conversation)
    if assistant.get("revision") != authorization["assistant_revision"]:
        raise AppError(409, "GOAL_PERMISSION_CHANGED",
                       "助手配置已变化，请重新授权目标执行")


def _assistant_matches(service, conversation: Mapping, authorization: Mapping) -> bool:
    try:
        assistant = service.usable_assistant(conversation["assistant_id"], conversation["owner"])
    except AppError:
        return False
    return assistant.get("revision") == authorization["assistant_revision"]


def _active_authorization(service, conversation_id: str):
    return service.db.one(
        "SELECT * FROM goal_execution_authorizations WHERE conversation_id=? AND status='active'",
        (conversation_id,))


def _authorization_row(service, conversation_id: str):
    return service.db.one(
        "SELECT * FROM goal_execution_authorizations WHERE conversation_id=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1", (conversation_id,))


def _lease_rows(service, authorization_id: str) -> list[dict]:
    # Older versions marked amounts with no billing provenance as complete.
    # Recheck completed receipts at the common read/admission boundary so an
    # existing approval (or a change out of test mode) cannot reuse them.
    rows = service.db.all(
        "SELECT l.*,r.usage FROM goal_execution_leases l LEFT JOIN runs r "
        "ON r.id=l.run_id AND ? AND l.usage_status='complete' AND l.cost_authoritative=0 "
        "WHERE l.authorization_id=? ORDER BY l.round_index",
        (service.settings.provider_test_mode, authorization_id))
    for row in rows:
        if row["usage_status"] == "complete":
            tokens, cost, status, _, source, _ = _run_usage_detail(
                row, synthetic_allowed=service.settings.provider_test_mode)
            if (status != "complete" or row["cost_authoritative"] != 0
                    or (tokens, cost, source) != (row["total_tokens"], row["cost_micros"], row["usage_source"])):
                row.update(usage_status="unknown", total_tokens=None, cost_micros=None,
                           cost_authoritative=None, error_code="USAGE_COST_NOT_AUTHORITATIVE")
        row.pop("usage")
    return rows


def _active_lease(service, conversation_id: str):
    return service.db.one(
        "SELECT * FROM goal_execution_leases WHERE conversation_id=? AND state='admitted'",
        (conversation_id,))


def _executed(rows: list[dict]) -> list[dict]:
    """Leases whose official positive round may have reached the native log.

    A reservation that failed before the Node seam armed the goal never
    dispatched a model request and has no usage to settle. It still consumes a
    host attempt slot, but it must not be treated as an unknown-cost round.
    """
    return [row for row in rows if row.get("armed_at") is not None or row.get("usage_status") is not None]


def _totals(rows: list[dict]) -> dict:
    executed = _executed(rows)
    # A lease without a complete receipt may have consumed provider resources.
    # Its usage stays unknown and therefore blocks the next admission.
    complete = all(row.get("usage_status") == "complete" for row in executed)
    authoritative = complete and all(row.get("cost_authoritative") in (None, 1) for row in executed)
    return {
        "rounds_used": len(rows),
        "rounds_executed": len(executed),
        "usage_complete": complete,
        "cost_authoritative": authoritative,
        "tokens_used": (sum(row.get("total_tokens") or 0 for row in executed) if complete else None),
        "cost_micros_used": (sum(row.get("cost_micros") or 0 for row in executed) if complete else None),
        "synthetic_rounds": sum(1 for row in executed if row.get("cost_authoritative") == 0),
        "unknown_rounds": [row["round_index"] for row in executed
                           if row.get("usage_status") != "complete"],
    }


def _expires_at(authorization: Mapping) -> datetime | None:
    created = _timestamp(authorization.get("created_at"))
    seconds = authorization.get("time_budget_seconds")
    if created is None or not _integer(seconds, 0, MAX_TIME_SECONDS):
        return None
    return created + timedelta(seconds=seconds)


def _time_remaining(authorization: Mapping) -> int | None:
    expires = _expires_at(authorization)
    if expires is None:
        return None
    return max(0, int((expires - datetime.now(timezone.utc)).total_seconds()))


def _budget_view(authorization: Mapping, rows: list[dict]) -> dict:
    totals = _totals(rows)
    tokens_used, cost_used = totals["tokens_used"], totals["cost_micros_used"]
    return {
        **totals,
        "tokens_remaining": (max(0, authorization["token_budget"] - tokens_used)
                             if tokens_used is not None else None),
        "cost_micros_remaining": (max(0, authorization["cost_budget_micros"] - cost_used)
                                  if cost_used is not None else None),
        "rounds_remaining": max(0, authorization["max_rounds"] - totals["rounds_used"]),
        "time_remaining_seconds": _time_remaining(authorization),
    }


def _authorization_view(row: Mapping) -> dict:
    return {
        "id": row["id"], "goal_id": row["goal_id"], "goal_revision": row["goal_revision"],
        "objective_sha256": row.get("objective_sha256"),
        "harness_session_id": row["harness_session_id"],
        "native_rounds_started": row["native_rounds_started"],
        "native_max_rounds": row["native_max_rounds"],
        "assistant_revision": row.get("assistant_revision"),
        "permission_mode": row["permission_mode"],
        "max_rounds": row["max_rounds"], "token_budget": row["token_budget"],
        "cost_budget_micros": row["cost_budget_micros"],
        "time_budget_seconds": row["time_budget_seconds"],
        "status": row["status"], "created_at": row["created_at"],
        "expires_at": (_expires_at(row).isoformat() if _expires_at(row) else None),
        "revoked_at": row.get("revoked_at"), "revoked_reason": row.get("revoked_reason"),
    }


def _lease_view(row: Mapping) -> dict:
    return {
        "id": row["id"], "state": row["state"], "run_id": row["run_id"],
        "round_index": row["round_index"], "goal_id": row["goal_id"],
        "goal_revision": row["goal_revision"], "admitted_at": row["admitted_at"],
        "armed": row.get("armed_at") is not None,
        "armed_at": row.get("armed_at"),
        "expected_goal_revision": row.get("expected_goal_revision"),
        "armed_goal_revision": row.get("armed_goal_revision"),
        "native_rounds_before": row.get("native_rounds_before"),
        "arm_action": row.get("arm_action"),
        "finished_at": row.get("finished_at"), "outcome": row.get("outcome"),
        "usage_status": row.get("usage_status"), "total_tokens": row.get("total_tokens"),
        "cost_micros": row.get("cost_micros"),
        "usage_source": row.get("usage_source"),
        "cost_authoritative": (None if row.get("cost_authoritative") is None
                               else bool(row.get("cost_authoritative"))),
        "abort_reason": row.get("abort_reason"),
        "error_code": row.get("error_code"),
    }


def goal_execution_view(service, conversation_id: str, user) -> dict:
    """Owner-only read of the latest approval, rounds and budget consumption."""
    conversation = require_goal_owner(service, conversation_id, user)
    authorization = _authorization_row(service, conversation_id)
    result = {"authorization": None, "budget": None, "active_lease": None,
              "rounds": [],
              "continuation_driver": False, "unattended_execution": False,
              "driver_integrated": bool(goal_execution_enabled(service)),
              "single_round_per_run": True,
              # The owner needs the exact native session marker to approve the
              # first round; it is already returned in the authorization view.
              "harness_session_id": conversation["harness_session_id"],
              "can_start": False}
    if authorization is None:
        return result
    rows = _lease_rows(service, authorization["id"])
    active = next((row for row in rows if row["state"] == "admitted"), None)
    totals = _totals(rows)
    result["authorization"] = _authorization_view(authorization)
    result["budget"] = _budget_view(authorization, rows)
    result["active_lease"] = _lease_view(active) if active else None
    result["rounds"] = [
        {**_lease_view(row),
         "assistant_message_id": _assistant_message_id(service, row["run_id"])}
        for row in rows
    ]
    result["session_matches"] = authorization["harness_session_id"] == conversation["harness_session_id"]
    result["permission_matches"] = (_permission_still_valid(conversation, authorization)
                                    and _assistant_matches(service, conversation, authorization))
    result["can_start"] = bool(
        authorization["status"] == "active" and active is None
        and result["session_matches"] and result["permission_matches"]
        and totals["usage_complete"] and totals["rounds_used"] < authorization["max_rounds"]
        and (_time_remaining(authorization) or 0) > 0
        and (totals["tokens_used"] is None
             or totals["tokens_used"] < authorization["token_budget"])
        and (totals["cost_micros_used"] is None
             or totals["cost_micros_used"] < authorization["cost_budget_micros"]))
    return result


def _assistant_message_id(service, run_id: str) -> str | None:
    row = service.db.one("SELECT assistant_message_id FROM runs WHERE id=?", (run_id,))
    return row["assistant_message_id"] if row else None


def authorize_goal_execution(service, conversation_id: str, user, request: Mapping,
                             goal: Mapping) -> dict:
    """Record the owner's explicit approval; never start execution."""
    require_goal_execution_enabled(service)
    bounded = _normalize_request(request)
    conversation = _conversation(service, conversation_id, user)
    assistant = _require_assistant_access(service, conversation)
    if conversation["harness_session_id"] != bounded["harness_session_id"]:
        raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，请刷新后重新授权")
    snapshot = _goal_snapshot(goal, bounded["harness_session_id"], objective_required=True)
    if snapshot["id"] != bounded["goal_id"] or snapshot["revision"] != bounded["goal_revision"]:
        raise AppError(409, "GOAL_STALE_REVISION", "目标已变化，请刷新后重新授权")
    # A blocked goal is a valid resumable native state. A paused or completed
    # goal still requires the user's explicit state transition first.
    if snapshot["phase"] not in {"active", "blocked"}:
        raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "只有进行中或等待恢复的目标可以授权执行")
    if conversation.get("active_run_id"):
        raise AppError(409, "GOAL_SESSION_BUSY", "目标执行授权仅支持空闲会话")
    if snapshot["roundsStarted"] + bounded["max_rounds"] > snapshot["maxGoalRounds"]:
        raise AppError(422, "GOAL_ROUNDS_EXCEED_NATIVE_CAP",
                       "授权轮次超过原生目标剩余轮次上限")
    if _active_lease(service, conversation_id):
        raise AppError(409, "GOAL_LEASE_ACTIVE", "已有目标轮次占用会话，请先结束")
    try:
        with service.db.transaction():
            current = _conversation(service, conversation_id, user)
            current_assistant = _require_assistant_access(service, current)
            if (current_assistant["revision"] != assistant["revision"]
                    or current.get("permission_mode","manual") != conversation.get("permission_mode","manual")
                    or current["owner"]["role"] != conversation["owner"]["role"]
                    or (current.get("permission_mode") == "full" and current["owner"]["role"] != "admin")):
                raise AppError(409, "GOAL_PERMISSION_CHANGED", "目标授权期间权限或助手配置已变化，请重新授权")
            if current.get("active_run_id") or current.get("archived"):
                raise AppError(409, "GOAL_SESSION_BUSY", "会话状态已变化，请刷新后重新授权")
            if current.get("harness_session_id") != bounded["harness_session_id"]:
                raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，请刷新后重新授权")
            if _active_authorization(service, conversation_id):
                raise AppError(409, "GOAL_AUTHORIZATION_EXISTS", "已有生效的执行授权，请先撤销")
            if _active_lease(service, conversation_id):
                raise AppError(409, "GOAL_LEASE_ACTIVE", "已有目标轮次占用会话，请先结束")
            timestamp = now()
            service.db.execute(
                "INSERT INTO goal_execution_authorizations"
                "(id,conversation_id,user_id,goal_id,goal_revision,objective_sha256,harness_session_id,"
                "native_rounds_started,native_max_rounds,assistant_revision,permission_mode,max_rounds,"
                "token_budget,cost_budget_micros,time_budget_seconds,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                (uid(), conversation_id, conversation["user_id"], snapshot["id"], snapshot["revision"],
                 objective_digest(snapshot["objective"]), bounded["harness_session_id"],
                 snapshot["roundsStarted"], snapshot["maxGoalRounds"],
                 assistant["revision"], current.get("permission_mode", "manual"), bounded["max_rounds"],
                 bounded["token_budget"], bounded["cost_budget_micros"],
                 bounded["time_budget_seconds"], timestamp),
            )
    except sqlite3.IntegrityError:
        raise AppError(409, "GOAL_AUTHORIZATION_EXISTS", "已有生效的执行授权，请先撤销") from None
    return goal_execution_view(service, conversation_id, user)


def _lease_by_run(service, run_id: str):
    return service.db.one("SELECT * FROM goal_execution_leases WHERE run_id=?", (run_id,))


def _armed_lease_rows(service, authorization_id: str) -> list[dict]:
    return service.db.all(
        "SELECT * FROM goal_execution_leases WHERE authorization_id=? AND armed_at IS NOT NULL "
        "ORDER BY round_index", (authorization_id,))


def _next_arm_action(snapshot: Mapping) -> tuple[str, int]:
    """Return the official state edge that arms exactly one round.

    Activation is process-local: a restored session starts disarmed. The pinned
    Goal service arms only on create/resume. An active goal is first paused,
    then resumed; a paused or blocked goal resumes directly. Both are official
    service operations with persisted revision edges, not a second state
    machine or a direct activation write.
    """
    phase, revision = snapshot["phase"], snapshot["revision"]
    if phase == "active":
        return "pause_resume", revision + 2
    if phase in {"paused", "blocked"}:
        return "resume", revision + 1
    raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "目标已完成，不能继续执行")


def _start_checks(service, conversation: Mapping, authorization: Mapping, snapshot: Mapping) -> list[dict]:
    """Validate the owner's approval against the live native snapshot.

    These checks run both before and after creating the host run. The second
    pass is authoritative; the Node seam independently verifies the exact arm
    revision before any positive round can enter the native log.
    """
    requested_session = snapshot.get("harness_session_id")
    if ((requested_session is not None
         and requested_session != conversation["harness_session_id"])
            or authorization["harness_session_id"] != conversation["harness_session_id"]):
        raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，本目标不再续行")
    if not _permission_still_valid(conversation, authorization):
        raise AppError(409, "GOAL_PERMISSION_CHANGED", "会话权限已变化，本目标不再续行")
    _require_assistant_revision(service, conversation, authorization)
    if snapshot["id"] != authorization["goal_id"]:
        raise AppError(409, "GOAL_STALE_REVISION", "当前目标不是已授权目标")
    approved = authorization.get("objective_sha256")
    if (not isinstance(approved, str) or DIGEST.fullmatch(approved) is None
            or snapshot.get("objective") is None
            or objective_digest(snapshot["objective"]) != approved):
        raise AppError(409, "GOAL_STALE_REVISION", "目标内容已变化，请重新授权")
    if snapshot["maxGoalRounds"] != authorization["native_max_rounds"]:
        raise AppError(409, "GOAL_STALE_REVISION", "原生目标轮次上限已变化")
    if snapshot["phase"] == "complete":
        raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "目标已完成")
    if snapshot["phase"] == "paused":
        raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "目标已暂停，请先恢复")
    if _active_lease(service, conversation["id"]):
        raise AppError(409, "GOAL_LEASE_ACTIVE", "已有目标轮次占用会话，请先结束")
    rows = _lease_rows(service, authorization["id"])
    if len(rows) >= authorization["max_rounds"]:
        raise AppError(409, "GOAL_ROUND_BUDGET_EXHAUSTED", "目标执行轮次预算已用尽")
    armed = [row for row in rows if row.get("armed_at") is not None]
    if snapshot["roundsStarted"] != authorization["native_rounds_started"] + len(armed):
        raise AppError(409, "GOAL_ROUND_FENCE_MISMATCH",
                       "原生轮次与宿主租约不一致，拒绝继续执行")
    if snapshot["roundsStarted"] >= snapshot["maxGoalRounds"]:
        raise AppError(409, "GOAL_NATIVE_ROUNDS_EXHAUSTED", "原生目标轮次上限已用尽")
    remaining = _time_remaining(authorization)
    if remaining is None:
        raise AppError(409, "GOAL_TIME_BUDGET_INVALID", "目标执行时间预算无法核验")
    if remaining <= 0:
        raise AppError(409, "GOAL_TIME_BUDGET_EXHAUSTED", "目标执行时间预算已用尽")
    totals = _totals(rows)
    if not totals["usage_complete"]:
        raise AppError(409, "GOAL_USAGE_UNVERIFIED",
                       "前序轮次用量不完整，不能假定为零并继续执行")
    if totals["tokens_used"] >= authorization["token_budget"]:
        raise AppError(409, "GOAL_TOKEN_BUDGET_EXHAUSTED", "目标执行 token 预算已用尽")
    if totals["cost_micros_used"] >= authorization["cost_budget_micros"]:
        raise AppError(409, "GOAL_COST_BUDGET_EXHAUSTED", "目标执行费用预算已用尽")
    return rows


def _pointer(service, run_id: str, metadata: Mapping) -> None:
    service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?",
                       (json.dumps(dict(metadata), ensure_ascii=False), run_id))


def _discard_reserved_run(service, run_id: str, code: str) -> None:
    """A run reserved for a round that could not be leased must never execute."""
    manager = getattr(service, "manager", None)
    if manager is not None:
        try:
            manager.finish(run_id, "failed", "目标轮次未能取得宿主租约，未执行模型。",
                           error_code=code)
            return
        except Exception:
            pass
    service.db.execute("UPDATE runs SET status='failed',error=?,error_code=?,completed_at=? WHERE id=?",
                       ("目标轮次未能取得宿主租约，未执行模型。", code, now(), run_id))
    service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE active_run_id=?", (run_id,))


def start_goal_round(service, conversation_id: str, user, goal: Mapping,
                     harness_session_id: str) -> dict:
    """Reserve one real host run for the next explicitly authorized Goal round.

    The run is created through the normal ``Service.prepare_run`` transaction
    and is submitted through the existing RunManager/FIFO. No model request is
    made here, and no native Goal state is mutated: the pinned official driver
    inside the run's own Harness process must arm the exact expected revision
    and pass the host pre-step admission before a round can enter history.
    """
    require_goal_execution_enabled(service)
    bounded = _goal_snapshot(goal, harness_session_id, objective_required=True)
    first = None
    with service.db.transaction():
        conversation = _conversation(service, conversation_id, user)
        authorization = _active_authorization(service, conversation_id)
        if authorization is None:
            raise AppError(409, "GOAL_NOT_AUTHORIZED", "当前没有生效的目标执行授权")
        bounded["harness_session_id"] = harness_session_id
        existing = _active_lease(service, conversation_id)
        if existing is not None:
            run = service.db.one("SELECT * FROM runs WHERE id=?", (existing["run_id"],))
            if (existing.get("armed_at") is None and run is not None
                    and run["status"] in {"queued", "running"}):
                rows = _lease_rows(service, authorization["id"])
                return {"run_id": existing["run_id"], "lease": _lease_view(existing),
                        "authorization": _authorization_view(authorization),
                        "budget": _budget_view(authorization, rows),
                        "round_started": True, "execution_started": False,
                        "continuation_driver": False,
                        "replayed": True}
            raise AppError(409, "GOAL_LEASE_ACTIVE", "已有目标轮次占用会话，请先结束")
        rows = _start_checks(service, conversation, authorization, bounded)
        action, expected_revision = _next_arm_action(bounded)
        first = {"authorization": dict(authorization), "rows": rows,
                 "action": action, "expected_revision": expected_revision,
                 "conversation": dict(conversation)}

    round_index = max((row["round_index"] for row in first["rows"]), default=0) + 1
    client_request_id = f"goal-round-{first['authorization']['id']}-{round_index}"
    prepared, created = service.prepare_run(
        conversation_id,
        {"client_request_id": client_request_id,
         "text": f"持续目标第 {round_index} 轮（宿主按授权执行，无需重复确认）",
         "file_ids": []},
        user, goal_continuation=True)
    run_id = prepared["run_id"]
    if not created:
        raise AppError(409, "GOAL_ROUND_CONFLICT", "目标轮次已存在，请刷新后查看")
    try:
        with service.db.transaction():
            conversation = _conversation(service, conversation_id, user)
            authorization = _active_authorization(service, conversation_id)
            if authorization is None or authorization["id"] != first["authorization"]["id"]:
                raise AppError(409, "GOAL_AUTHORIZATION_INACTIVE", "目标执行授权已撤销")
            bounded["harness_session_id"] = harness_session_id
            if _active_lease(service, conversation_id):
                raise AppError(409, "GOAL_LEASE_ACTIVE", "已有目标轮次占用会话，请先结束")
            rows = _start_checks(service, conversation, authorization, bounded)
            if max((row["round_index"] for row in rows), default=0) + 1 != round_index:
                raise AppError(409, "GOAL_ROUND_CONFLICT", "目标轮次已变化，请刷新后重试")
            action, expected_revision = _next_arm_action(bounded)
            lease_id, timestamp = uid(), now()
            service.db.execute(
                "INSERT INTO goal_execution_leases"
                "(id,authorization_id,conversation_id,user_id,run_id,goal_id,goal_revision,"
                "harness_session_id,round_index,state,admitted_at,expected_goal_revision,"
                "native_rounds_before,arm_action) "
                "VALUES(?,?,?,?,?,?,?,?,?,'admitted',?,?,?,?)",
                (lease_id, authorization["id"], conversation_id, conversation["user_id"], run_id,
                 bounded["id"], bounded["revision"], harness_session_id, round_index, timestamp,
                 expected_revision, bounded["roundsStarted"], action),
            )
            service.db.execute(
                "UPDATE goal_execution_authorizations SET goal_revision=? WHERE id=?",
                (bounded["revision"], authorization["id"]))
            run = service.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
            try:
                metadata = json.loads(run.get("execution_meta") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            metadata.update(goal_authorization_id=authorization["id"], goal_lease_id=lease_id,
                            goal_round_index=round_index,
                            goal_expected_revision=expected_revision,
                            goal_native_rounds_before=bounded["roundsStarted"],
                            goal_arm_action=action)
            _pointer(service, run_id, metadata)
            lease = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
            service.db.event(run_id, "goal.execution.reserved", {
                "lease_id": lease_id, "authorization_id": authorization["id"],
                "round_index": round_index, "goal_id": bounded["id"],
                "goal_revision": bounded["revision"],
                "expected_goal_revision": expected_revision,
                "native_rounds_before": bounded["roundsStarted"],
                "arm_action": action,
            })
    except BaseException as error:
        code = error.code if isinstance(error, AppError) else "GOAL_ROUND_RESERVATION_FAILED"
        _discard_reserved_run(service, run_id, code)
        raise
    current = service.db.one("SELECT * FROM goal_execution_authorizations WHERE id=?",
                              (lease["authorization_id"],))
    return {"run_id": run_id, "lease": _lease_view(lease),
            "authorization": _authorization_view(current),
            "budget": _budget_view(current, _lease_rows(service, current["id"])),
            "round_started": True, "execution_started": False,
            "continuation_driver": False, "replayed": False}


def stop_goal_execution_locked(service, conversation_id: str, reason: str) -> dict:
    """Revoke approvals and abort admitted leases inside a caller transaction."""
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation id is required")
    if not isinstance(reason, str) or STOP_REASON.fullmatch(reason) is None:
        raise ValueError("stop reason must be a short fixed code")
    timestamp = now()
    leases = service.db.all(
        "SELECT * FROM goal_execution_leases WHERE conversation_id=? AND state='admitted'",
        (conversation_id,))
    revoked = service.db.execute(
        "UPDATE goal_execution_authorizations SET status='revoked',revoked_at=?,revoked_reason=? "
        "WHERE conversation_id=? AND status='active'", (timestamp, reason, conversation_id))
    for lease in leases:
        service.db.execute(
            "UPDATE goal_execution_leases SET state='aborted',finished_at=?,outcome='aborted',"
            "abort_reason=? WHERE id=? AND state='admitted'",
            (timestamp, reason, lease["id"]))
        service.db.event(lease["run_id"], "goal.execution.aborted", {
            "lease_id": lease["id"], "round_index": lease["round_index"], "reason": reason,
        })
    return {"revoked": revoked.rowcount, "aborted": len(leases)}


def stop_goal_execution(service, conversation_id: str, reason: str) -> dict:
    """Stop all future continuation for one conversation; never runs work."""
    with service.db.lock:
        if service.db.conn.in_transaction:
            return stop_goal_execution_locked(service, conversation_id, reason)
        with service.db.transaction():
            return stop_goal_execution_locked(service, conversation_id, reason)


def recover_goal_execution(service) -> dict:
    """Abort every in-flight lease and revoke approvals after a restart.

    A restart is not a new user approval. Recovery therefore never resumes a
    round or keeps a lease armed; the owner must authorize again explicitly.
    """
    rows = service.db.all(
        "SELECT DISTINCT conversation_id FROM goal_execution_authorizations WHERE status='active' "
        "UNION SELECT DISTINCT conversation_id FROM goal_execution_leases WHERE state='admitted'")
    if not rows:
        return {"revoked": 0, "aborted": 0}
    revoked = aborted = 0
    with service.db.transaction():
        for row in rows:
            result = stop_goal_execution_locked(service, row["conversation_id"], "service_restarted")
            revoked += result["revoked"]
            aborted += result["aborted"]
    return {"revoked": revoked, "aborted": aborted}


def revoke_user_goal_execution(service, user_id: str, reason: str) -> dict:
    """Revoke a user's approvals after a role change or account disable.

    Restoring the role later must not revive an approval given under the old
    permission, so this is a durable revoke rather than a merely failing check.
    """
    rows = service.db.all(
        "SELECT DISTINCT conversation_id FROM goal_execution_authorizations "
        "WHERE user_id=? AND status='active' "
        "UNION SELECT DISTINCT conversation_id FROM goal_execution_leases "
        "WHERE user_id=? AND state='admitted'", (user_id, user_id))
    revoked = aborted = 0
    for row in rows:
        result = stop_goal_execution(service, row["conversation_id"], reason)
        revoked += result["revoked"]
        aborted += result["aborted"]
    return {"revoked": revoked, "aborted": aborted}


def admit_goal_round(service, conversation_id: str, run_id: str,
                     goal: Mapping, harness_session_id: str) -> dict:
    """Bind one existing real host run to one leased Goal round.

    This is the host admission seam a future official driver must call after
    the normal ``Service.prepare_run`` created the run. It does not enqueue,
    execute, approve a tool, or write usage. Those remain the existing run,
    FIFO, approval and usage paths.
    """
    require_goal_execution_enabled(service)
    if not isinstance(run_id, str) or RUN_ID.fullmatch(run_id) is None:
        raise AppError(422, "GOAL_RUN_INVALID", "目标轮次必须绑定真实任务")
    with service.db.transaction():
        conversation = _conversation(service, conversation_id)
        authorization = _active_authorization(service, conversation_id)
        if authorization is None:
            raise AppError(409, "GOAL_NOT_AUTHORIZED", "当前没有生效的目标执行授权")
        if authorization["harness_session_id"] != harness_session_id \
                or conversation["harness_session_id"] != harness_session_id:
            raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，本目标不再续行")
        if not _permission_still_valid(conversation, authorization):
            raise AppError(409, "GOAL_PERMISSION_CHANGED", "会话权限已变化，本目标不再续行")
        _require_assistant_revision(service, conversation, authorization)
        snapshot = _goal_snapshot(goal, harness_session_id)
        if snapshot["id"] != authorization["goal_id"]:
            raise AppError(409, "GOAL_STALE_REVISION", "当前目标不是已授权目标")
        if snapshot["revision"] != authorization["goal_revision"]:
            raise AppError(409, "GOAL_STALE_REVISION", "目标版本已变化，请重新授权")
        if snapshot["phase"] != "active":
            raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "目标已暂停、阻塞或完成")
        if snapshot["maxGoalRounds"] != authorization["native_max_rounds"]:
            raise AppError(409, "GOAL_STALE_REVISION", "原生目标轮次上限已变化")
        rows = _lease_rows(service, authorization["id"])
        if _active_lease(service, conversation_id):
            raise AppError(409, "GOAL_LEASE_ACTIVE", "已有目标轮次占用会话")
        if len(rows) >= authorization["max_rounds"]:
            raise AppError(409, "GOAL_ROUND_BUDGET_EXHAUSTED", "目标执行轮次预算已用尽")
        remaining = _time_remaining(authorization)
        if remaining is None:
            raise AppError(409, "GOAL_TIME_BUDGET_INVALID", "目标执行时间预算无法核验")
        if remaining <= 0:
            raise AppError(409, "GOAL_TIME_BUDGET_EXHAUSTED", "目标执行时间预算已用尽")
        totals = _totals(rows)
        if not totals["usage_complete"]:
            raise AppError(409, "GOAL_USAGE_UNVERIFIED",
                           "前序轮次用量不完整，不能假定为零并继续执行")
        if totals["tokens_used"] >= authorization["token_budget"]:
            raise AppError(409, "GOAL_TOKEN_BUDGET_EXHAUSTED", "目标执行 token 预算已用尽")
        if totals["cost_micros_used"] >= authorization["cost_budget_micros"]:
            raise AppError(409, "GOAL_COST_BUDGET_EXHAUSTED", "目标执行费用预算已用尽")
        started = snapshot["roundsStarted"]
        baseline = authorization["native_rounds_started"]
        if started != baseline + len(rows):
            raise AppError(409, "GOAL_ROUND_FENCE_MISMATCH",
                           "原生轮次与宿主租约不一致，拒绝继续执行")
        if started >= snapshot["maxGoalRounds"]:
            raise AppError(409, "GOAL_NATIVE_ROUNDS_EXHAUSTED", "原生目标轮次上限已用尽")
        run = service.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if run is None or run["conversation_id"] != conversation_id \
                or run["user_id"] != conversation["user_id"]:
            raise AppError(409, "GOAL_RUN_INVALID", "目标轮次必须绑定本会话的真实任务")
        try:
            metadata = json.loads(run.get("execution_meta") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict) or metadata.get("goal_continuation") is not True:
            raise AppError(409, "GOAL_RUN_NOT_CONTINUATION",
                           "目标轮次必须来自宿主续行路径，普通用户任务不能接纳")
        if run.get("kind", "chat") != "chat":
            raise AppError(409, "GOAL_RUN_INVALID", "目标轮次必须是普通对话任务")
        if run["status"] not in {"queued", "running"}:
            raise AppError(409, "GOAL_RUN_NOT_ADMISSIBLE", "任务已结束，不能接纳新的目标轮次")
        if conversation.get("active_run_id") != run_id:
            raise AppError(409, "GOAL_RUN_NOT_ACTIVE", "任务不是会话当前活动任务")
        round_index = len(rows) + 1
        lease_id, timestamp = uid(), now()
        try:
            service.db.execute(
                "INSERT INTO goal_execution_leases"
                "(id,authorization_id,conversation_id,user_id,run_id,goal_id,goal_revision,"
                "harness_session_id,round_index,state,admitted_at,armed_at,armed_goal_revision,"
                "expected_goal_revision,native_rounds_before,arm_action) "
                "VALUES(?,?,?,?,?,?,?,?,?,'admitted',?,?,?,?,?,'direct')",
                (lease_id, authorization["id"], conversation_id, conversation["user_id"], run_id,
                 snapshot["id"], snapshot["revision"], harness_session_id, round_index, timestamp,
                 timestamp, snapshot["revision"], snapshot["revision"],
                 snapshot["roundsStarted"]),
            )
        except sqlite3.IntegrityError:
            raise AppError(409, "GOAL_LEASE_ACTIVE", "已有目标轮次占用会话") from None
        lease = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
        service.db.event(run_id, "goal.execution.admitted", {
            "lease_id": lease_id, "authorization_id": authorization["id"],
            "round_index": round_index, "goal_id": snapshot["id"],
            "goal_revision": snapshot["revision"],
        })
        return {"lease": _lease_view(lease), "budget": _budget_view(authorization, rows + [lease]),
                "execution_started": False, "continuation_driver": False}


def admit_goal_round_armed(service, lease_id: str, run_id: str, goal: Mapping,
                           harness_session_id: str, round_number: int | None = None) -> dict:
    """Turn a reserved host lease into the one real admission for this run.

    The Node seam calls this from the official ``agent/pre-step`` middleware,
    downstream of the pinned driver's round reservation. The caller cannot
    choose the goal revision: it must equal the revision the host recorded when
    it reserved the run, so a concurrent edit, pause, resume or session swap
    fails before the positive round enters history.
    """
    require_goal_execution_enabled(service)
    with service.db.transaction():
        lease = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
        if lease is None or lease["run_id"] != run_id:
            raise AppError(409, "GOAL_LEASE_NOT_FOUND", "目标轮次租约不存在")
        if lease["state"] != "admitted":
            raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约已失效")
        if lease.get("armed_at") is not None:
            raise AppError(409, "GOAL_LEASE_ACTIVE", "目标轮次已经接纳，不能重复接纳")
        conversation = _conversation(service, lease["conversation_id"])
        authorization = service.db.one(
            "SELECT * FROM goal_execution_authorizations WHERE id=? AND status='active'",
            (lease["authorization_id"],))
        if authorization is None:
            raise AppError(409, "GOAL_AUTHORIZATION_INACTIVE", "目标执行授权已撤销")
        if (authorization["harness_session_id"] != harness_session_id
                or conversation["harness_session_id"] != harness_session_id):
            raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，本目标不再续行")
        if not _permission_still_valid(conversation, authorization):
            raise AppError(409, "GOAL_PERMISSION_CHANGED", "会话权限已变化，本目标不再续行")
        _require_assistant_revision(service, conversation, authorization)
        snapshot = _goal_snapshot(goal, harness_session_id)
        if snapshot["id"] != lease["goal_id"] or snapshot["id"] != authorization["goal_id"]:
            raise AppError(409, "GOAL_STALE_REVISION", "当前目标不是已授权目标")
        expected = lease.get("expected_goal_revision")
        if not _integer(expected, 1, 10**9) or snapshot["revision"] != expected:
            raise AppError(409, "GOAL_ARM_REVISION_MISMATCH",
                           "原生目标版本与宿主预留不一致，拒绝执行本轮")
        if snapshot["phase"] != "active":
            raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "目标已暂停、阻塞或完成")
        before = lease.get("native_rounds_before")
        if not _integer(before, 0, MAX_ROUNDS) or snapshot["roundsStarted"] != before:
            raise AppError(409, "GOAL_ROUND_FENCE_MISMATCH",
                           "原生轮次与宿主预留不一致，拒绝执行本轮")
        if snapshot["maxGoalRounds"] != authorization["native_max_rounds"]:
            raise AppError(409, "GOAL_STALE_REVISION", "原生目标轮次上限已变化")
        if round_number is not None and round_number != snapshot["roundsStarted"] + 1:
            raise AppError(409, "GOAL_ROUND_FENCE_MISMATCH", "本轮不是原生下一正轮次")
        rows = _lease_rows(service, authorization["id"])
        totals = _totals(rows)
        if not totals["usage_complete"]:
            raise AppError(409, "GOAL_USAGE_UNVERIFIED",
                           "前序轮次用量不完整，不能假定为零并继续执行")
        if totals["tokens_used"] >= authorization["token_budget"]:
            raise AppError(409, "GOAL_TOKEN_BUDGET_EXHAUSTED", "目标执行 token 预算已用尽")
        if totals["cost_micros_used"] >= authorization["cost_budget_micros"]:
            raise AppError(409, "GOAL_COST_BUDGET_EXHAUSTED", "目标执行费用预算已用尽")
        remaining = _time_remaining(authorization)
        if remaining is None or remaining <= 0:
            raise AppError(409, "GOAL_TIME_BUDGET_EXHAUSTED", "目标执行时间预算已用尽")
        run = service.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if run is None or run["conversation_id"] != lease["conversation_id"] \
                or run["user_id"] != lease["user_id"]:
            raise AppError(409, "GOAL_RUN_INVALID", "目标轮次必须绑定本会话的真实任务")
        try:
            metadata = json.loads(run.get("execution_meta") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict) or metadata.get("goal_continuation") is not True:
            raise AppError(409, "GOAL_RUN_NOT_CONTINUATION",
                           "目标轮次必须来自宿主续行路径，普通用户任务不能接纳")
        if run.get("kind", "chat") != "chat" or run["status"] not in {"queued", "running"}:
            raise AppError(409, "GOAL_RUN_NOT_ADMISSIBLE", "任务已结束，不能接纳新的目标轮次")
        if conversation.get("active_run_id") != run_id:
            raise AppError(409, "GOAL_RUN_NOT_ACTIVE", "任务不是会话当前活动任务")
        timestamp = now()
        service.db.execute(
            "UPDATE goal_execution_leases SET armed_at=?,armed_goal_revision=? "
            "WHERE id=? AND armed_at IS NULL AND state='admitted'",
            (timestamp, snapshot["revision"], lease_id))
        service.db.execute("UPDATE goal_execution_authorizations SET goal_revision=? WHERE id=?",
                           (snapshot["revision"], authorization["id"]))
        service.db.event(run_id, "goal.execution.admitted", {
            "lease_id": lease_id, "authorization_id": authorization["id"],
            "round_index": lease["round_index"], "goal_id": snapshot["id"],
            "goal_revision": snapshot["revision"],
            "native_rounds_before": snapshot["roundsStarted"],
        })
        current = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
        return {"lease": _lease_view(current),
                "budget": _budget_view(authorization, rows),
                "execution_started": False, "continuation_driver": False}


def require_goal_round_start(service, run_id: str) -> dict:
    """Fail closed before a reserved continuation run opens a Harness process."""
    require_goal_execution_enabled(service)
    run = service.require("runs", run_id)
    try:
        metadata = json.loads(run.get("execution_meta") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict) or metadata.get("goal_continuation") is not True:
        raise AppError(409, "GOAL_RUN_NOT_CONTINUATION", "任务不是目标续行任务")
    lease = _lease_by_run(service, run_id)
    if lease is None:
        raise AppError(409, "GOAL_ROUND_NOT_RESERVED", "目标轮次没有宿主预留，拒绝执行")
    if lease["state"] != "admitted":
        raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约已失效")
    if lease.get("armed_at") is not None:
        raise AppError(409, "GOAL_ROUND_ALREADY_ARMED", "目标轮次已经接纳，不能重复执行")
    authorization = service.db.one(
        "SELECT * FROM goal_execution_authorizations WHERE id=? AND status='active'",
        (lease["authorization_id"],))
    if authorization is None:
        raise AppError(409, "GOAL_AUTHORIZATION_INACTIVE", "目标执行授权已撤销")
    conversation = _conversation(service, lease["conversation_id"])
    if authorization["harness_session_id"] != conversation["harness_session_id"]:
        raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，本目标不再续行")
    if not _permission_still_valid(conversation, authorization):
        raise AppError(409, "GOAL_PERMISSION_CHANGED", "会话权限已变化，本目标不再续行")
    _require_assistant_revision(service, conversation, authorization)
    if conversation.get("active_run_id") != run_id or run["status"] not in {"queued", "running"}:
        raise AppError(409, "GOAL_RUN_NOT_ACTIVE", "目标任务已不是会话当前活动任务")
    rows = _lease_rows(service, authorization["id"])
    totals = _totals(rows)
    if not totals["usage_complete"]:
        raise AppError(409, "GOAL_USAGE_UNVERIFIED",
                       "前序轮次用量不完整，不能假定为零并继续执行")
    if totals["tokens_used"] >= authorization["token_budget"]:
        raise AppError(409, "GOAL_TOKEN_BUDGET_EXHAUSTED", "目标执行 token 预算已用尽")
    if totals["cost_micros_used"] >= authorization["cost_budget_micros"]:
        raise AppError(409, "GOAL_COST_BUDGET_EXHAUSTED", "目标执行费用预算已用尽")
    remaining = _time_remaining(authorization)
    if remaining is None or remaining <= 0:
        raise AppError(409, "GOAL_TIME_BUDGET_EXHAUSTED", "目标执行时间预算已用尽")
    return _lease_view(lease)


def goal_execution_runtime_config(service, run: Mapping) -> dict | None:
    """Private per-run configuration for the official driver seam.

    Only IDs, revisions, the objective digest and budgets cross into Node. The
    goal text, model output, credentials and business data never do.
    """
    try:
        metadata = json.loads(run.get("execution_meta") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict) or metadata.get("goal_continuation") is not True:
        return None
    if not goal_execution_enabled(service):
        raise AppError(409, "GOAL_EXECUTION_DISABLED", "持续目标自动执行试点尚未开启")
    lease = _lease_by_run(service, run["id"])
    if lease is None:
        raise AppError(409, "GOAL_ROUND_NOT_RESERVED", "目标轮次没有宿主预留，拒绝执行")
    authorization = service.db.one(
        "SELECT * FROM goal_execution_authorizations WHERE id=?",
        (lease["authorization_id"],))
    if authorization is None:
        raise AppError(409, "GOAL_AUTHORIZATION_INACTIVE", "目标执行授权已撤销")
    # Before the round is armed, connection requires a live approval. Once the
    # round is admitted, the per-run configuration is frozen so a mid-round
    # stop (which intentionally does not cancel the current run) cannot break
    # the runtime's own rechecks.
    if lease.get("armed_at") is None and authorization["status"] != "active":
        raise AppError(409, "GOAL_AUTHORIZATION_INACTIVE", "目标执行授权已撤销")
    expected = lease.get("expected_goal_revision")
    before = lease.get("native_rounds_before")
    digest = authorization.get("objective_sha256")
    if (not _integer(expected, 1, 10**9) or not _integer(before, 0, MAX_ROUNDS)
            or not isinstance(digest, str) or DIGEST.fullmatch(digest) is None
            or lease.get("arm_action") not in ARM_ACTIONS):
        raise AppError(409, "GOAL_ROUND_NOT_RESERVED", "目标轮次预留不完整，拒绝执行")
    return {
        "goal_id": lease["goal_id"],
        "pre_arm_revision": lease["goal_revision"],
        "expected_goal_revision": expected,
        "arm_action": lease["arm_action"],
        "round_index": lease["round_index"],
        "native_rounds_before": before,
        "max_goal_rounds": authorization["native_max_rounds"],
        "objective_sha256": digest,
        "lease_id": lease["id"],
        "run_id": run["id"],
        "session_id": lease["harness_session_id"],
    }


def _run_usage_detail(run: Mapping, *, synthetic_allowed: bool = False
                      ) -> tuple[int | None, int | None, str, str | None, str, bool]:
    """Read the run's real usage receipt; missing parts stay unknown.

    ``cost_authoritative`` is false for a loopback/synthetic receipt even when
    it carries an amount. Such a receipt can only satisfy a synthetic budget in
    provider test mode and is always reported separately from production
    billing.
    """
    raw = run.get("usage")
    if not raw:
        return None, None, "unknown", "USAGE_MISSING", "unknown", False
    try:
        usage = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None, None, "unknown", "USAGE_INVALID", "unknown", False
    if not isinstance(usage, dict):
        return None, None, "unknown", "USAGE_INVALID", "unknown", False
    ledger = usage.get("token_accounting")
    ledger = ledger if isinstance(ledger, dict) else {}
    flags = [value for value in (usage.get("capture_complete"),
                                 ledger.get("capture_complete"))
             if isinstance(value, bool)]
    # The real Harness receipt puts capture_complete under token_accounting.
    # Conflicting flags fail closed; a missing flag is not a complete capture.
    complete = bool(flags) and all(flags)
    tokens = usage.get("total_tokens")
    if not _integer(tokens, 0, MAX_TOKENS * 1000):
        total_input, output = usage.get("total_input_tokens"), usage.get("output_tokens")
        if _integer(total_input, 0, MAX_TOKENS * 1000) and _integer(output, 0, MAX_TOKENS * 1000):
            tokens = total_input + output
        else:
            provider = ledger.get("provider_totals")
            buckets = (provider.get(key) for key in
                       ("input_tokens", "cache_read_input_tokens", "cache_write_input_tokens"))
            buckets = list(buckets) + [provider.get("output_tokens")] if isinstance(provider, dict) else []
            tokens = (sum(buckets) if complete and buckets
                      and all(_integer(value, 0, MAX_TOKENS * 1000) for value in buckets)
                      else None)
    # The fixed Harness has no monetary receipt writer. Only an explicitly
    # identified loopback amount can exercise the budget in provider test mode.
    # The source and its amount must come from the same part of the receipt.
    amounts = [part for part in (usage, ledger) if any(
        part.get(field) is not None for field in ("cost_micros", "estimated_cost_usd"))]
    amount = amounts[0] if len(amounts) == 1 else {}
    source = amount.get("cost_source", amount.get("source")) if amounts else usage.get("source")
    if not isinstance(source, str) or not source or len(source) > 64:
        source = "unknown"
    if not complete or tokens is None or not amounts:
        return None, None, "unknown", ("USAGE_INCOMPLETE" if complete else "USAGE_CAPTURE_INCOMPLETE"), source, False
    if (not synthetic_allowed or source not in SYNTHETIC_COST_SOURCES
            or amount.get("currency", "USD") != "USD"
            or any(part.get("cost_is_estimate", False) is not False for part in (usage, ledger))):
        return None, None, "unknown", "USAGE_COST_NOT_AUTHORITATIVE", source, False
    cost = amount.get("cost_micros")
    if cost is None:
        usd = amount.get("estimated_cost_usd")
        if (amount.get("cost_is_estimate") is False
                and type(usd) in (int, float) and 0 <= usd <= MAX_COST_MICROS / 1000):
            cost = int(round(usd * 1_000_000))
    if not _integer(cost, 0, MAX_COST_MICROS * 1000):
        return None, None, "unknown", "USAGE_INCOMPLETE", source, False
    return tokens, cost, "complete", None, source, False


def _run_usage(run: Mapping) -> tuple[int | None, int | None, str, str | None]:
    tokens, cost, status, code, _source, _authoritative = _run_usage_detail(run)
    return tokens, cost, status, code


def _settle_goal_round_locked(service, lease_id: str, outcome: str) -> dict:
    if outcome not in OUTCOMES:
        raise AppError(422, "GOAL_SETTLEMENT_INVALID", "目标轮次结果无效")
    lease = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
    if lease is None:
        raise AppError(409, "GOAL_LEASE_NOT_FOUND", "目标轮次租约不存在")
    if lease.get("usage_status") is not None:
        if lease.get("outcome") == outcome:
            authorization = service.db.one(
                "SELECT * FROM goal_execution_authorizations WHERE id=?",
                (lease["authorization_id"],))
            rows = _lease_rows(service, authorization["id"])
            checked = next(row for row in rows if row["id"] == lease_id)
            return {"lease": _lease_view(checked),
                    "budget": _budget_view(authorization, rows),
                    "execution_started": False, "continuation_driver": False}
        raise AppError(409, "GOAL_LEASE_SETTLED", "目标轮次已结算，结果不能改写")
    if lease["state"] not in {"admitted", "aborted"}:
        raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约已失效")
    if lease.get("armed_at") is None:
        raise AppError(409, "GOAL_ROUND_NOT_ADMITTED", "目标轮次未进入原生历史，不能结算用量")
    run = service.db.one("SELECT * FROM runs WHERE id=?", (lease["run_id"],))
    if run is None:
        raise AppError(409, "GOAL_RUN_INVALID", "目标轮次任务不存在")
    if run["status"] not in TERMINAL_RUNS:
        raise AppError(409, "GOAL_RUN_NOT_SETTLED", "任务尚未结束，不能结算目标轮次")
    if run["status"] != outcome:
        raise AppError(409, "GOAL_SETTLEMENT_MISMATCH", "目标轮次结果与真实任务不一致")
    synthetic_allowed = bool(getattr(service.settings, "provider_test_mode", False))
    tokens, cost, usage_status, usage_code, source, authoritative = _run_usage_detail(
        run, synthetic_allowed=synthetic_allowed)
    state = "settled" if lease["state"] == "admitted" else "aborted"
    service.db.execute(
        "UPDATE goal_execution_leases SET state=?,finished_at=?,outcome=?,usage_status=?,"
        "total_tokens=?,cost_micros=?,usage_source=?,cost_authoritative=?,error_code=? WHERE id=?",
        (state, now(), outcome, usage_status, tokens, cost, source,
         None if usage_status != "complete" else int(authoritative),
         run.get("error_code") or usage_code, lease_id))
    service.db.event(lease["run_id"], "goal.execution.settled", {
        "lease_id": lease_id, "round_index": lease["round_index"], "outcome": outcome,
        "usage_status": usage_status, "total_tokens": tokens, "cost_micros": cost,
        "usage_source": source, "cost_authoritative": authoritative,
        "aborted": lease["state"] == "aborted",
    })
    current = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
    authorization = service.db.one(
        "SELECT * FROM goal_execution_authorizations WHERE id=?", (lease["authorization_id"],))
    return {"lease": _lease_view(current),
            "budget": _budget_view(authorization,
                                   _lease_rows(service, authorization["id"])),
            "execution_started": False, "continuation_driver": False}


def settle_goal_round(service, lease_id: str, outcome: str) -> dict:
    """Record the real run's usage after it reached a terminal state.

    The run row is the only usage source. A caller cannot supply a fabricated
    receipt, and a missing receipt stays unknown instead of becoming zero.
    """
    with service.db.lock:
        if service.db.conn.in_transaction:
            return _settle_goal_round_locked(service, lease_id, outcome)
        with service.db.transaction():
            return _settle_goal_round_locked(service, lease_id, outcome)


def finish_goal_round_locked(service, run_id: str, status: str,
                             *, approval_source: str = "run_finished") -> dict | None:
    """Terminal hook for one reserved Goal round, inside the run transaction.

    A successful run settles its authoritative receipt. A failed, cancelled,
    interrupted or never-admitted run aborts the lease and revokes the approval
    so the round can never be replayed. A never-admitted reservation has no
    usage and is not settled as a phantom zero-cost round.
    """
    settings = service.settings
    if not (getattr(settings, "harness_goal_state_enabled", False)
            and getattr(settings, "harness_goal_execution_enabled", False)):
        return None
    run = service.db.one("SELECT * FROM runs WHERE id=?", (run_id,))
    if run is None or run.get("kind", "chat") != "chat":
        return None
    try:
        metadata = json.loads(run.get("execution_meta") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict) or metadata.get("goal_continuation") is not True:
        return None
    lease = _lease_by_run(service, run_id)
    if lease is None:
        stop_goal_execution_locked(service, run["conversation_id"], "goal_lease_missing")
        return {"settled": False, "code": "GOAL_LEASE_MISSING"}
    if lease.get("armed_at") is None:
        stop_goal_execution_locked(
            service, run["conversation_id"],
            "service_restarted" if approval_source == "restart" else "goal_round_not_admitted")
        return {"settled": False, "code": "GOAL_ROUND_NOT_ADMITTED"}
    if status != "succeeded":
        reason = ("service_restarted" if approval_source == "restart"
                  else f"run_{status}")
        stop_goal_execution_locked(service, run["conversation_id"], reason)
    try:
        result = _settle_goal_round_locked(service, lease["id"], status)
    except AppError as error:
        stop_goal_execution_locked(service, run["conversation_id"], "goal_settlement_failed")
        return {"settled": False, "code": error.code}
    return {"settled": True, "code": None,
            "usage_status": result["lease"]["usage_status"],
            "cost_authoritative": result["lease"]["cost_authoritative"]}


def require_goal_lease(service, lease_id: str, run_id: str | None = None, *,
                       goal: Mapping | None = None) -> dict:
    """Round fence a future driver must pass before an internal prompt.

    It fails after stop, restart, permission change, user disable or usage
    invalidation. It never renews or recreates a lease. When the caller has a
    fresh native Goal snapshot it must pass it: a paused, blocked, complete or
    stale goal then fails before the internal prompt is built.
    """
    require_goal_execution_enabled(service)
    if not isinstance(lease_id, str) or not lease_id:
        raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约已失效")
    lease = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
    if lease is None or lease["state"] != "admitted":
        raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约已失效")
    if lease.get("armed_at") is None:
        raise AppError(409, "GOAL_ROUND_NOT_ADMITTED", "目标轮次尚未通过宿主接纳")
    if run_id is not None and lease["run_id"] != run_id:
        raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约与任务不匹配")
    if goal is not None:
        snapshot = _goal_snapshot(goal, lease["harness_session_id"])
        bound = lease.get("armed_goal_revision") or lease["goal_revision"]
        if snapshot["id"] != lease["goal_id"] or snapshot["revision"] != bound:
            raise AppError(409, "GOAL_STALE_REVISION", "目标版本已变化，本目标不再续行")
        if snapshot["phase"] != "active":
            raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "目标已暂停、阻塞或完成")
    authorization = service.db.one(
        "SELECT * FROM goal_execution_authorizations WHERE id=?", (lease["authorization_id"],))
    if authorization is None or authorization["status"] != "active":
        raise AppError(409, "GOAL_AUTHORIZATION_INACTIVE", "目标执行授权已撤销")
    conversation = _conversation(service, lease["conversation_id"])
    if authorization["harness_session_id"] != conversation["harness_session_id"]:
        raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，本目标不再续行")
    if not _permission_still_valid(conversation, authorization):
        raise AppError(409, "GOAL_PERMISSION_CHANGED", "会话权限已变化，本目标不再续行")
    _require_assistant_revision(service, conversation, authorization)
    if conversation.get("active_run_id") != lease["run_id"]:
        raise AppError(409, "GOAL_RUN_NOT_ACTIVE", "目标任务已不是会话当前活动任务")
    if _time_remaining(authorization) is None or _time_remaining(authorization) <= 0:
        raise AppError(409, "GOAL_TIME_BUDGET_EXHAUSTED", "目标执行时间预算已用尽")
    return _lease_view(lease)
