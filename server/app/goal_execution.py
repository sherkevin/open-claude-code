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

import json
import math
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from .db import now, uid
from .security import AppError

GOAL_ID = re.compile(r"^goal-[a-f0-9-]{36}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
STOP_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_ROUNDS = 32
MAX_TOKENS = 1_000_000_000_000
MAX_COST_MICROS = 1_000_000_000_000
MAX_TIME_SECONDS = 30 * 24 * 3600
TERMINAL_RUNS = frozenset({"succeeded", "failed", "cancelled", "interrupted"})
OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "interrupted"})


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


def _goal_snapshot(goal: Mapping, harness_session_id: str) -> dict:
    if not isinstance(goal, Mapping):
        raise AppError(422, "GOAL_SNAPSHOT_INVALID", "原生目标快照无效")
    goal_id, revision = goal.get("id"), goal.get("revision")
    phase, started = goal.get("phase"), goal.get("roundsStarted")
    maximum, activation = goal.get("maxGoalRounds"), goal.get("activation")
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
            "roundsStarted": started, "maxGoalRounds": maximum}


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
    return service.db.all(
        "SELECT * FROM goal_execution_leases WHERE authorization_id=? ORDER BY round_index",
        (authorization_id,))


def _active_lease(service, conversation_id: str):
    return service.db.one(
        "SELECT * FROM goal_execution_leases WHERE conversation_id=? AND state='admitted'",
        (conversation_id,))


def _totals(rows: list[dict]) -> dict:
    # A lease without a complete receipt may have consumed provider resources.
    # Its usage stays unknown and therefore blocks the next admission.
    complete = all(row.get("usage_status") == "complete" for row in rows)
    return {
        "rounds_used": len(rows),
        "usage_complete": complete,
        "tokens_used": (sum(row.get("total_tokens") or 0 for row in rows) if complete else None),
        "cost_micros_used": (sum(row.get("cost_micros") or 0 for row in rows) if complete else None),
        "unknown_rounds": [row["round_index"] for row in rows
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
        "harness_session_id": row["harness_session_id"],
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
        "finished_at": row.get("finished_at"), "outcome": row.get("outcome"),
        "usage_status": row.get("usage_status"), "total_tokens": row.get("total_tokens"),
        "cost_micros": row.get("cost_micros"), "abort_reason": row.get("abort_reason"),
        "error_code": row.get("error_code"),
    }


def goal_execution_view(service, conversation_id: str, user) -> dict:
    """Owner-only read of the latest approval and its budget consumption."""
    conversation = require_goal_owner(service, conversation_id, user)
    authorization = _authorization_row(service, conversation_id)
    result = {"authorization": None, "budget": None, "active_lease": None,
              "continuation_driver": False, "unattended_execution": False,
              "driver_integrated": False}
    if authorization is None:
        return result
    rows = _lease_rows(service, authorization["id"])
    active = next((row for row in rows if row["state"] == "admitted"), None)
    result["authorization"] = _authorization_view(authorization)
    result["budget"] = _budget_view(authorization, rows)
    result["active_lease"] = _lease_view(active) if active else None
    result["session_matches"] = authorization["harness_session_id"] == conversation["harness_session_id"]
    result["permission_matches"] = (_permission_still_valid(conversation, authorization)
                                    and _assistant_matches(service, conversation, authorization))
    return result


def authorize_goal_execution(service, conversation_id: str, user, request: Mapping,
                             goal: Mapping) -> dict:
    """Record the owner's explicit approval; never start execution."""
    require_goal_execution_enabled(service)
    bounded = _normalize_request(request)
    conversation = _conversation(service, conversation_id, user)
    assistant = _require_assistant_access(service, conversation)
    if conversation["harness_session_id"] != bounded["harness_session_id"]:
        raise AppError(409, "GOAL_SESSION_CHANGED", "原生会话已变化，请刷新后重新授权")
    snapshot = _goal_snapshot(goal, bounded["harness_session_id"])
    if snapshot["id"] != bounded["goal_id"] or snapshot["revision"] != bounded["goal_revision"]:
        raise AppError(409, "GOAL_STALE_REVISION", "目标已变化，请刷新后重新授权")
    if snapshot["phase"] != "active":
        raise AppError(409, "GOAL_GOAL_NOT_ACTIVE", "只有进行中的目标可以授权执行")
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
                "(id,conversation_id,user_id,goal_id,goal_revision,harness_session_id,"
                "native_rounds_started,native_max_rounds,assistant_revision,permission_mode,max_rounds,"
                "token_budget,cost_budget_micros,time_budget_seconds,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                (uid(), conversation_id, conversation["user_id"], snapshot["id"], snapshot["revision"],
                 bounded["harness_session_id"], snapshot["roundsStarted"], snapshot["maxGoalRounds"],
                 assistant["revision"], current.get("permission_mode", "manual"), bounded["max_rounds"],
                 bounded["token_budget"], bounded["cost_budget_micros"],
                 bounded["time_budget_seconds"], timestamp),
            )
    except sqlite3.IntegrityError:
        raise AppError(409, "GOAL_AUTHORIZATION_EXISTS", "已有生效的执行授权，请先撤销") from None
    return goal_execution_view(service, conversation_id, user)


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
        lease_id = uid()
        try:
            service.db.execute(
                "INSERT INTO goal_execution_leases"
                "(id,authorization_id,conversation_id,user_id,run_id,goal_id,goal_revision,"
                "harness_session_id,round_index,state,admitted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'admitted',?)",
                (lease_id, authorization["id"], conversation_id, conversation["user_id"], run_id,
                 snapshot["id"], snapshot["revision"], harness_session_id, round_index, now()),
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


def _run_usage(run: Mapping) -> tuple[int | None, int | None, str, str | None]:
    raw = run.get("usage")
    if not raw:
        return None, None, "unknown", "USAGE_MISSING"
    try:
        usage = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None, None, "unknown", "USAGE_INVALID"
    if not isinstance(usage, dict):
        return None, None, "unknown", "USAGE_INVALID"
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
    cost = usage.get("cost_micros")
    if not _integer(cost, 0, MAX_COST_MICROS * 1000):
        cost = ledger.get("cost_micros")
    if not _integer(cost, 0, MAX_COST_MICROS * 1000):
        cost = None
        usd, estimate = usage.get("estimated_cost_usd"), usage.get("cost_is_estimate")
        if usd is None:
            usd, estimate = ledger.get("estimated_cost_usd"), ledger.get("cost_is_estimate")
        # An explicitly estimated amount is not a settled cost. Only a
        # non-estimate numeric amount can satisfy the monetary budget.
        if (type(usd) in {int, float} and math.isfinite(usd) and usd >= 0
                and estimate is False):
            micros = int(round(float(usd) * 1_000_000))
            cost = micros if _integer(micros, 0, MAX_COST_MICROS * 1000) else None
    if not complete or tokens is None or cost is None:
        return None, None, "unknown", ("USAGE_INCOMPLETE" if complete else "USAGE_CAPTURE_INCOMPLETE")
    return tokens, cost, "complete", None


def settle_goal_round(service, lease_id: str, outcome: str) -> dict:
    """Record the real run's usage after it reached a terminal state.

    The run row is the only usage source. A caller cannot supply a fabricated
    receipt, and a missing receipt stays unknown instead of becoming zero.
    """
    if outcome not in OUTCOMES:
        raise AppError(422, "GOAL_SETTLEMENT_INVALID", "目标轮次结果无效")
    with service.db.transaction():
        lease = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
        if lease is None:
            raise AppError(409, "GOAL_LEASE_NOT_FOUND", "目标轮次租约不存在")
        if lease.get("usage_status") is not None:
            if lease.get("outcome") == outcome:
                authorization = service.db.one(
                    "SELECT * FROM goal_execution_authorizations WHERE id=?",
                    (lease["authorization_id"],))
                return {"lease": _lease_view(lease),
                        "budget": _budget_view(authorization,
                                               _lease_rows(service, authorization["id"])),
                        "execution_started": False, "continuation_driver": False}
            raise AppError(409, "GOAL_LEASE_SETTLED", "目标轮次已结算，结果不能改写")
        if lease["state"] not in {"admitted", "aborted"}:
            raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约已失效")
        run = service.db.one("SELECT * FROM runs WHERE id=?", (lease["run_id"],))
        if run is None:
            raise AppError(409, "GOAL_RUN_INVALID", "目标轮次任务不存在")
        if run["status"] not in TERMINAL_RUNS:
            raise AppError(409, "GOAL_RUN_NOT_SETTLED", "任务尚未结束，不能结算目标轮次")
        if run["status"] != outcome:
            raise AppError(409, "GOAL_SETTLEMENT_MISMATCH", "目标轮次结果与真实任务不一致")
        tokens, cost, usage_status, usage_code = _run_usage(run)
        state = "settled" if lease["state"] == "admitted" else "aborted"
        service.db.execute(
            "UPDATE goal_execution_leases SET state=?,finished_at=?,outcome=?,usage_status=?,"
            "total_tokens=?,cost_micros=?,error_code=? WHERE id=?",
            (state, now(), outcome, usage_status, tokens, cost,
             run.get("error_code") or usage_code, lease_id))
        service.db.event(lease["run_id"], "goal.execution.settled", {
            "lease_id": lease_id, "round_index": lease["round_index"], "outcome": outcome,
            "usage_status": usage_status, "total_tokens": tokens, "cost_micros": cost,
            "aborted": lease["state"] == "aborted",
        })
        current = service.db.one("SELECT * FROM goal_execution_leases WHERE id=?", (lease_id,))
        authorization = service.db.one(
            "SELECT * FROM goal_execution_authorizations WHERE id=?", (lease["authorization_id"],))
        return {"lease": _lease_view(current),
                "budget": _budget_view(authorization,
                                       _lease_rows(service, authorization["id"])),
                "execution_started": False, "continuation_driver": False}


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
    if run_id is not None and lease["run_id"] != run_id:
        raise AppError(409, "GOAL_LEASE_INACTIVE", "目标轮次租约与任务不匹配")
    if goal is not None:
        snapshot = _goal_snapshot(goal, lease["harness_session_id"])
        if snapshot["id"] != lease["goal_id"] or snapshot["revision"] != lease["goal_revision"]:
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
