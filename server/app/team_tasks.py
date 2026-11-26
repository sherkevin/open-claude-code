"""Host authorization and audit for the native Team task lifecycle pilot.

The pinned official ``agentTeams`` service and its JSONL session projection
remain the sole task-board authority. This module never creates a second board,
task graph or agent manager. It records an owner's explicit, bounded approval
for metadata operations (enable/disable a board, create/claim/complete a task),
requires the exact native session, and keeps an append-only operation receipt
for restart recovery and audit.

Nothing here starts a model turn, spawns a subagent, executes a shell, writes a
business system or widens a permission. "Cancel" therefore means revoking the
host approval and refusing future metadata mutations; it cannot cancel native
work because this slice never starts any. The official board and its task
history stay intact and readable.
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

SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
CLIENT_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
STOP_REASON = re.compile(r"^[a-z0-9_]{1,64}$")
ACTIONS = frozenset({"enable", "disable", "create", "claim", "complete"})
TASK_ACTIONS = frozenset({"create", "claim", "complete"})
MAX_OPERATIONS = 100
MIN_EXPIRES_SECONDS = 60
MAX_EXPIRES_SECONDS = 7 * 24 * 3600
MAX_SUBJECT_CHARS = 200
MAX_SUBJECT_BYTES = 800
MAX_DESCRIPTION_CHARS = 4000
MAX_DESCRIPTION_BYTES = 16000
MAX_DEPENDENCIES = 64
MAX_WRITE_SCOPES = 64
MAX_WRITE_SCOPE_CHARS = 300
MAX_WRITE_SCOPE_BYTES = 900
MAX_REVISION = 10**9
HISTORY_LIMIT = 50

# These codes are emitted by our own pinned Node seam for a request that was
# rejected before any board mutation, or by the official service with a stable
# ``TEAM_*`` code. Anything else after the RPC was sent is treated as uncertain,
# never as a safe retry.
REJECTION_MESSAGES = {
    "TEAM_TASK_STALE_REVISION": "任务已被更新，请刷新任务板后重试",
    "TEAM_TASK_NOT_FOUND": "任务不存在，请刷新任务板",
    "TEAM_BOARD_DISABLED": "任务板尚未启用",
    "TEAM_TASK_BLOCKED": "任务依赖尚未完成",
    "TEAM_TASK_INVALID": "任务内容无效",
    "TEAM_TASK_LIMIT": "任务数量已达上限",
    "TEAM_TASK_TRANSITION": "当前任务状态不允许此操作",
    "TEAM_DISABLED": "原生任务板服务未启用",
}
CONTROLLED_REJECTION_CODES = frozenset(REJECTION_MESSAGES) | {
    "TEAM_REQUEST_INVALID", "TEAM_ROOT_UNAVAILABLE", "TEAM_OPERATION_REJECTED",
}


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


def _expires_at(authorization: Mapping) -> datetime | None:
    created = _timestamp(authorization.get("created_at"))
    seconds = authorization.get("expires_in_seconds")
    if created is None or not _integer(seconds, MIN_EXPIRES_SECONDS, MAX_EXPIRES_SECONDS):
        return None
    return created + timedelta(seconds=seconds)


def _expired(authorization: Mapping) -> bool:
    expires = _expires_at(authorization)
    return expires is None or expires <= datetime.now(timezone.utc)


def team_tasks_enabled(service) -> bool:
    settings = service.settings
    return bool(not service.policy.is_trial
                and getattr(settings, "harness_team_state_enabled", False)
                and getattr(settings, "harness_team_tasks_enabled", False))


def require_team_tasks_enabled(service) -> None:
    if not team_tasks_enabled(service):
        raise AppError(404, "TEAM_TASKS_DISABLED", "任务板协作试点尚未开启")


def require_team_tasks_owner(service, conversation_id: str, user) -> dict:
    """Owner-only even after archive/session change; viewing history stays possible."""
    row = service.require("conversations", conversation_id, user)
    owner = service.require("users", row["user_id"])
    if not owner["enabled"]:
        raise AppError(403, "USER_DISABLED", "账号已停用")
    return {**row, "owner": owner}


def require_team_task_session(service, conversation_id: str, user,
                              expected_session: str | None = None) -> dict:
    """Strict idle, owner, live-session and assistant gate for every mutation."""
    require_team_tasks_enabled(service)
    row = require_team_tasks_owner(service, conversation_id, user)
    if row.get("runtime_type") != "harness" or not row.get("harness_session_id"):
        raise AppError(409, "TEAM_SESSION_NOT_READY", "请先完成一次 Harness 对话")
    if row["archived"] or row["active_run_id"]:
        raise AppError(409, "TEAM_SESSION_BUSY", "任务板仅支持未归档的空闲会话")
    if expected_session is not None and row["harness_session_id"] != expected_session:
        raise AppError(409, "TEAM_SESSION_CHANGED", "原生会话已变化，请刷新任务板")
    if service.db.one(
            "SELECT conversation_id FROM conversation_memory_scopes "
            "WHERE conversation_id=? AND mutation_id IS NOT NULL", (conversation_id,)):
        raise AppError(409, "MEMORY_BUSY", "记忆正在更新，请稍后重试")
    service.usable_assistant(row["assistant_id"], user)
    return row


def _normalize_text(value, *, label: str, max_chars: int, max_bytes: int,
                    single_line: bool) -> str:
    if not isinstance(value, str):
        raise AppError(422, "TEAM_REQUEST_INVALID", label + "无效")
    text = value.strip() if single_line else value.strip()
    if not text:
        raise AppError(422, "TEAM_REQUEST_INVALID", label + "不能为空")
    if len(text) > max_chars or len(text.encode("utf-8")) > max_bytes:
        raise AppError(422, "TEAM_REQUEST_INVALID", label + "过长")
    allowed = "\n\r\t" if not single_line else ""
    if any(ord(char) < 32 and char not in allowed or ord(char) == 127 for char in text):
        raise AppError(422, "TEAM_REQUEST_INVALID", label + "包含无效字符")
    return text


def _normalize_dependencies(value) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_DEPENDENCIES:
        raise AppError(422, "TEAM_REQUEST_INVALID", "依赖任务列表无效")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or TASK_ID.fullmatch(item) is None:
            raise AppError(422, "TEAM_REQUEST_INVALID", "依赖任务标识无效")
        if item not in result:
            result.append(item)
    return result


def _normalize_scopes(value) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_WRITE_SCOPES:
        raise AppError(422, "TEAM_REQUEST_INVALID", "写入范围列表无效")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise AppError(422, "TEAM_REQUEST_INVALID", "写入范围无效")
        text = item.strip()
        if (not text or len(text) > MAX_WRITE_SCOPE_CHARS
                or len(text.encode("utf-8")) > MAX_WRITE_SCOPE_BYTES
                or any(ord(char) < 32 or ord(char) == 127 for char in text)):
            raise AppError(422, "TEAM_REQUEST_INVALID", "写入范围无效")
        if text not in result:
            result.append(text)
    return result


def _normalize_authorization(request: Mapping) -> dict:
    if not isinstance(request, Mapping):
        raise AppError(422, "TEAM_AUTHORIZATION_INVALID", "任务板授权内容无效")
    if set(request) != {"harness_session_id", "max_operations",
                        "expires_in_seconds", "confirm"} or request.get("confirm") is not True:
        raise AppError(422, "TEAM_AUTHORIZATION_INVALID", "请明确确认任务板协作授权范围")
    session = request.get("harness_session_id")
    if not isinstance(session, str) or SESSION_ID.fullmatch(session) is None:
        raise AppError(422, "TEAM_AUTHORIZATION_INVALID", "原生会话标识无效")
    if (not _integer(request.get("max_operations"), 1, MAX_OPERATIONS)
            or not _integer(request.get("expires_in_seconds"),
                            MIN_EXPIRES_SECONDS, MAX_EXPIRES_SECONDS)):
        raise AppError(422, "TEAM_AUTHORIZATION_INVALID", "授权操作次数或有效期无效")
    return {"harness_session_id": session,
            "max_operations": request["max_operations"],
            "expires_in_seconds": request["expires_in_seconds"]}


def _normalize_operation(request: Mapping) -> dict:
    if not isinstance(request, Mapping):
        raise AppError(422, "TEAM_REQUEST_INVALID", "任务板操作内容无效")
    action = request.get("action")
    if action not in ACTIONS:
        raise AppError(422, "TEAM_REQUEST_INVALID", "任务板操作类型无效")
    session = request.get("harness_session_id")
    client_request_id = request.get("client_request_id")
    if not isinstance(session, str) or SESSION_ID.fullmatch(session) is None:
        raise AppError(422, "TEAM_REQUEST_INVALID", "原生会话标识无效")
    if (not isinstance(client_request_id, str)
            or CLIENT_REQUEST_ID.fullmatch(client_request_id) is None):
        raise AppError(422, "TEAM_REQUEST_INVALID", "请求标识无效")
    common = {"harness_session_id": session, "client_request_id": client_request_id}
    if action in {"enable", "disable"}:
        if set(request) != {"action", "harness_session_id",
                            "client_request_id", "confirm"} or request.get("confirm") is not True:
            raise AppError(422, "TEAM_REQUEST_INVALID", "请明确确认任务板启停操作")
        return {"action": action, **common, "native": {"action": action}}
    if action == "create":
        if set(request) != {"action", "harness_session_id", "client_request_id",
                            "subject", "description", "blocked_by", "write_scopes"}:
            raise AppError(422, "TEAM_REQUEST_INVALID", "任务创建字段无效")
        subject = _normalize_text(request.get("subject"), label="任务标题",
                                  max_chars=MAX_SUBJECT_CHARS, max_bytes=MAX_SUBJECT_BYTES,
                                  single_line=True)
        description = _normalize_text(request.get("description"), label="任务说明",
                                      max_chars=MAX_DESCRIPTION_CHARS,
                                      max_bytes=MAX_DESCRIPTION_BYTES, single_line=False)
        blocked_by = _normalize_dependencies(request.get("blocked_by"))
        write_scopes = _normalize_scopes(request.get("write_scopes"))
        native = {"action": action, "subject": subject, "description": description,
                  "blockedBy": blocked_by, "writeScopes": write_scopes}
        return {"action": action, **common, "native": native}
    task_id = request.get("task_id")
    if not isinstance(task_id, str) or TASK_ID.fullmatch(task_id) is None:
        raise AppError(422, "TEAM_REQUEST_INVALID", "任务标识无效")
    if (set(request) != {"action", "harness_session_id", "client_request_id",
                         "task_id", "expected_revision"}
            or not _integer(request.get("expected_revision"), 1, MAX_REVISION)):
        raise AppError(422, "TEAM_REQUEST_INVALID", "任务版本无效")
    native = {"action": action, "taskId": task_id,
              "expectedRevision": request["expected_revision"]}
    return {"action": action, **common, "task_id": task_id,
            "expected_revision": request["expected_revision"], "native": native}


def _digest(value: Mapping) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _active_authorization(service, conversation_id: str):
    return service.db.one(
        "SELECT * FROM team_task_authorizations "
        "WHERE conversation_id=? AND status='active'", (conversation_id,))


def _latest_authorization(service, conversation_id: str):
    return service.db.one(
        "SELECT * FROM team_task_authorizations WHERE conversation_id=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1", (conversation_id,))


def _authorization_view(row: Mapping) -> dict:
    expires = _expires_at(row)
    return {
        "id": row["id"], "harness_session_id": row["harness_session_id"],
        "assistant_revision": row.get("assistant_revision"),
        "permission_mode": row["permission_mode"],
        "max_operations": row["max_operations"],
        "expires_in_seconds": row["expires_in_seconds"],
        "status": row["status"], "created_at": row["created_at"],
        "expires_at": expires.isoformat() if expires else None,
        "revoked_at": row.get("revoked_at"),
        "revoked_reason": row.get("revoked_reason"),
    }


def _operation_result(row: Mapping) -> dict:
    raw = row.get("result")
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _operation_view(row: Mapping) -> dict:
    result = _operation_result(row)
    task = result.get("task") if isinstance(result.get("task"), dict) else {}
    return {
        "id": row["id"], "client_request_id": row["client_request_id"],
        "action": row["action"], "status": row["status"],
        "error_code": row.get("error_code"),
        "task_id": task.get("id"), "task_revision": task.get("revision"),
        "task_status": task.get("status"),
        "board_enabled": result.get("board_enabled"),
        "acknowledged_at": row.get("acknowledged_at"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _operation_row(service, operation_id: str):
    return service.db.one("SELECT * FROM team_task_operations WHERE id=?", (operation_id,))


def operation_view_by_id(service, operation_id: str) -> dict:
    row = _operation_row(service, operation_id)
    if row is None:
        raise AppError(404, "TEAM_OPERATION_NOT_FOUND", "任务板操作记录不存在")
    return _operation_view(row)


def _count_used(service, authorization_id: str) -> int:
    # An uncertain operation may have reached the board, so it consumes one
    # approved slot until a human acknowledges it; it is never treated as free.
    return service.db.one(
        "SELECT count(*) n FROM team_task_operations "
        "WHERE authorization_id=? AND status!='rejected'", (authorization_id,))["n"]


def _pending_operation(service, conversation_id: str):
    return service.db.one(
        "SELECT id FROM team_task_operations WHERE conversation_id=? AND status='pending'",
        (conversation_id,))


def _unresolved_operation(service, conversation_id: str):
    return service.db.one(
        "SELECT * FROM team_task_operations WHERE conversation_id=? "
        "AND status='uncertain' AND acknowledged_at IS NULL "
        "ORDER BY created_at DESC,rowid DESC LIMIT 1", (conversation_id,))


def _binding(service, conversation: Mapping, authorization: Mapping) -> dict:
    session_matches = authorization["harness_session_id"] == conversation.get("harness_session_id")
    permission_matches = False
    try:
        assistant = service.usable_assistant(conversation["assistant_id"], conversation["owner"])
        mode = conversation.get("permission_mode", "manual")
        permission_matches = bool(
            authorization["assistant_revision"] == assistant.get("revision")
            and authorization["permission_mode"] == mode
            and not (mode == "full" and conversation["owner"].get("role") != "admin"))
    except AppError:
        permission_matches = False
    return {"session_matches": session_matches, "permission_matches": permission_matches}


def team_collaboration_view(service, conversation_id: str, user) -> dict:
    """Owner-only authorization, bounded budget and operation history."""
    require_team_tasks_enabled(service)
    conversation = require_team_tasks_owner(service, conversation_id, user)
    authorization = _latest_authorization(service, conversation_id)
    operations = service.db.all(
        "SELECT * FROM team_task_operations WHERE conversation_id=? "
        "ORDER BY created_at DESC,rowid DESC LIMIT ?", (conversation_id, HISTORY_LIMIT))
    unresolved = _unresolved_operation(service, conversation_id)
    result = {
        "schema": "team-collaboration/v1",
        "harness_session_id": conversation.get("harness_session_id"),
        "authorization": None,
        "authorization_active": False,
        "session_matches": False,
        "permission_matches": False,
        "operations_used": 0,
        "operations_remaining": None,
        "operations": [_operation_view(row) for row in operations],
        "unresolved_operation": _operation_view(unresolved) if unresolved else None,
        "execution_enabled": False,
        "task_execution_enabled": False,
        "subagent_execution_enabled": False,
    }
    if authorization is None:
        return result
    binding = _binding(service, conversation, authorization)
    used = _count_used(service, authorization["id"])
    active = bool(authorization["status"] == "active" and not _expired(authorization)
                  and binding["session_matches"] and binding["permission_matches"])
    result.update(
        authorization=_authorization_view(authorization),
        authorization_active=active,
        **binding,
        operations_used=used,
        operations_remaining=max(0, authorization["max_operations"] - used) if active else 0,
    )
    return result


def authorize_team_tasks(service, conversation_id: str, user, request: Mapping) -> dict:
    """Record a bounded owner approval; this never mutates the native board."""
    require_team_tasks_enabled(service)
    bounded = _normalize_authorization(request)
    conversation = require_team_task_session(
        service, conversation_id, user, bounded["harness_session_id"])
    assistant = service.usable_assistant(conversation["assistant_id"], user)
    try:
        with service.db.transaction():
            current = require_team_task_session(
                service, conversation_id, user, bounded["harness_session_id"])
            current_assistant = service.usable_assistant(current["assistant_id"], user)
            if (current_assistant.get("revision") != assistant.get("revision")
                    or current.get("permission_mode", "manual")
                    != conversation.get("permission_mode", "manual")
                    or current["owner"].get("role") != conversation["owner"].get("role")):
                raise AppError(409, "TEAM_PERMISSION_CHANGED",
                               "授权期间权限或助手配置已变化，请重新授权")
            if _active_authorization(service, conversation_id) is not None:
                raise AppError(409, "TEAM_AUTHORIZATION_EXISTS",
                               "已有生效的任务板授权，请先撤销")
            if _pending_operation(service, conversation_id) is not None:
                raise AppError(409, "TEAM_OPERATION_IN_PROGRESS",
                               "已有任务板操作正在进行，请稍后授权")
            service.db.execute(
                "INSERT INTO team_task_authorizations"
                "(id,conversation_id,user_id,harness_session_id,assistant_revision,"
                "permission_mode,max_operations,expires_in_seconds,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,'active',?)",
                (uid(), conversation_id, conversation["user_id"],
                 bounded["harness_session_id"], assistant["revision"],
                 current.get("permission_mode", "manual"), bounded["max_operations"],
                 bounded["expires_in_seconds"], now()),
            )
    except sqlite3.IntegrityError:
        raise AppError(409, "TEAM_AUTHORIZATION_EXISTS",
                       "已有生效的任务板授权，请先撤销") from None
    return team_collaboration_view(service, conversation_id, user)


def begin_team_operation(service, conversation_id: str, user, request: Mapping) -> dict:
    """Authorize one metadata operation before the native RPC is attempted.

    The returned ``native`` payload contains only fields reviewed by the pinned
    Node seam. The row is committed as ``pending`` first, so a crash cannot make
    an attempted mutation look like it never happened.
    """
    require_team_tasks_enabled(service)
    bounded = _normalize_operation(request)
    conversation = require_team_tasks_owner(service, conversation_id, user)
    digest = _digest(bounded)
    existing = service.db.one(
        "SELECT * FROM team_task_operations WHERE conversation_id=? AND client_request_id=?",
        (conversation_id, bounded["client_request_id"]))
    if existing is not None:
        if existing["request_digest"] != digest:
            raise AppError(409, "TEAM_REQUEST_CONFLICT",
                           "该请求标识已用于不同的任务板操作，请刷新后重试")
        if existing["status"] == "confirmed":
            return {"id": existing["id"], "replay": True,
                    "operation": _operation_view(existing), "native": None}
        if existing["status"] == "pending":
            raise AppError(409, "TEAM_OPERATION_IN_PROGRESS",
                           "该任务板操作仍在进行，请稍后刷新")
        if existing["status"] == "uncertain":
            raise AppError(409, "TEAM_OPERATION_UNCERTAIN",
                           "该任务板操作结果未确认，请先核对任务板并确认结果")
        raise AppError(409, existing.get("error_code") or "TEAM_OPERATION_REJECTED",
                       "上次任务板操作已被拒绝，请刷新后重新发起")
    conversation = require_team_task_session(
        service, conversation_id, user, bounded["harness_session_id"])
    authorization = _active_authorization(service, conversation_id)
    if authorization is None:
        raise AppError(409, "TEAM_NOT_AUTHORIZED", "请先明确授权任务板协作操作")
    if _expired(authorization):
        raise AppError(409, "TEAM_AUTHORIZATION_EXPIRED", "任务板授权已过期，请重新授权")
    binding = _binding(service, conversation, authorization)
    if not binding["session_matches"]:
        raise AppError(409, "TEAM_SESSION_CHANGED", "原生会话已变化，请重新授权")
    if not binding["permission_matches"]:
        raise AppError(409, "TEAM_PERMISSION_CHANGED", "权限或助手配置已变化，请重新授权")
    if _pending_operation(service, conversation_id) is not None:
        raise AppError(409, "TEAM_OPERATION_IN_PROGRESS", "已有任务板操作正在进行")
    unresolved = _unresolved_operation(service, conversation_id)
    if unresolved is not None:
        raise AppError(409, "TEAM_OPERATION_UNCERTAIN",
                       "上次任务板操作结果未确认，请先核对任务板并确认结果")
    if _count_used(service, authorization["id"]) >= authorization["max_operations"]:
        raise AppError(409, "TEAM_AUTHORIZATION_EXHAUSTED",
                       "本次任务板授权的操作次数已用尽，请重新授权")
    operation_id = uid()
    try:
        with service.db.transaction():
            current = _active_authorization(service, conversation_id)
            if current is None or current["id"] != authorization["id"]:
                raise AppError(409, "TEAM_NOT_AUTHORIZED",
                               "任务板授权已撤销，请重新授权")
            if _pending_operation(service, conversation_id) is not None:
                raise AppError(409, "TEAM_OPERATION_IN_PROGRESS",
                               "已有任务板操作正在进行")
            if _count_used(service, current["id"]) >= current["max_operations"]:
                raise AppError(409, "TEAM_AUTHORIZATION_EXHAUSTED",
                               "本次任务板授权的操作次数已用尽，请重新授权")
            service.db.execute(
                "INSERT INTO team_task_operations"
                "(id,conversation_id,authorization_id,user_id,client_request_id,action,"
                "request_digest,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'pending',?,?)",
                (operation_id, conversation_id, current["id"], conversation["user_id"],
                 bounded["client_request_id"], bounded["action"], digest, now(), now()),
            )
    except sqlite3.IntegrityError:
        raise AppError(409, "TEAM_OPERATION_IN_PROGRESS",
                       "该任务板操作已在处理中，请刷新后确认") from None
    return {"id": operation_id, "replay": False,
            "operation": operation_view_by_id(service, operation_id),
            "native": bounded["native"]}


def require_pending_team_operation(service, conversation_id: str, user,
                                   session_id: str, operation_id: str) -> None:
    """Recheck the persisted approval after process startup, before native RPC."""
    with service.db.lock:
        conversation = require_team_task_session(service, conversation_id, user, session_id)
        row = _operation_row(service, operation_id)
        authorization = _active_authorization(service, conversation_id)
        if (row is None or row["conversation_id"] != conversation_id
                or row["user_id"] != user["id"] or row["status"] != "pending"
                or authorization is None or row["authorization_id"] != authorization["id"]):
            raise AppError(409, "TEAM_NOT_AUTHORIZED", "任务板操作授权已失效")
        if _expired(authorization):
            raise AppError(409, "TEAM_AUTHORIZATION_EXPIRED", "任务板授权已过期，请重新授权")
        binding = _binding(service, conversation, authorization)
        if not binding["session_matches"] or not binding["permission_matches"]:
            raise AppError(409, "TEAM_PERMISSION_CHANGED", "任务板授权绑定已变化")
        if _count_used(service, authorization["id"]) > authorization["max_operations"]:
            raise AppError(409, "TEAM_AUTHORIZATION_EXHAUSTED", "任务板授权次数已用尽")


def _sanitized_result(receipt: Mapping) -> dict:
    board_enabled = receipt.get("board_enabled")
    if type(board_enabled) is not bool:
        raise AppError(502, "TEAM_RECEIPT_INVALID", "任务板回执无效")
    task = receipt.get("task")
    clean = None
    if task is not None:
        if (not isinstance(task, Mapping)
                or not isinstance(task.get("id"), str)
                or TASK_ID.fullmatch(task["id"]) is None
                or not _integer(task.get("revision"), 1, MAX_REVISION)
                or task.get("status") not in {"pending", "in_progress", "completed"}):
            raise AppError(502, "TEAM_RECEIPT_INVALID", "任务板回执无效")
        clean = {"id": task["id"], "revision": task["revision"],
                 "status": task["status"]}
    return {"board_enabled": board_enabled, "task": clean}


def confirm_team_operation(service, operation_id: str, receipt: Mapping) -> dict:
    """Commit a validated native receipt. Only a pending row may confirm."""
    result = _sanitized_result(receipt)
    with service.db.transaction():
        row = _operation_row(service, operation_id)
        if row is None:
            raise AppError(404, "TEAM_OPERATION_NOT_FOUND", "任务板操作记录不存在")
        if row["status"] == "confirmed":
            return _operation_view(row)
        if row["status"] != "pending":
            raise AppError(409, "TEAM_OPERATION_SETTLED",
                           "任务板操作已被其他结果结算，不能改写")
        service.db.execute(
            "UPDATE team_task_operations SET status='confirmed',result=?,error_code=NULL,"
            "updated_at=? WHERE id=? AND status='pending'",
            (json.dumps(result, ensure_ascii=False, separators=(",", ":")), now(), operation_id))
        return _operation_view(_operation_row(service, operation_id))


def reject_team_operation(service, operation_id: str, code: str | None) -> dict | None:
    """Record a request rejected before any board mutation, or a stable native rejection."""
    if code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        code = "TEAM_OPERATION_REJECTED"
    with service.db.transaction():
        row = _operation_row(service, operation_id)
        if row is None:
            return None
        if row["status"] == "pending":
            service.db.execute(
                "UPDATE team_task_operations SET status='rejected',error_code=?,updated_at=? "
                "WHERE id=? AND status='pending'", (code, now(), operation_id))
        return _operation_view(_operation_row(service, operation_id))


def uncertain_team_operation(service, operation_id: str, code: str | None) -> dict | None:
    """Record that the RPC may have reached the board; never auto-replay it."""
    if code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
        code = "TEAM_TASK_UNCERTAIN"
    with service.db.transaction():
        row = _operation_row(service, operation_id)
        if row is None:
            return None
        if row["status"] == "pending":
            service.db.execute(
                "UPDATE team_task_operations SET status='uncertain',error_code=?,updated_at=? "
                "WHERE id=? AND status='pending'", (code, now(), operation_id))
        return _operation_view(_operation_row(service, operation_id))


def acknowledge_team_operation(service, conversation_id: str, user,
                               operation_id: str) -> dict:
    """Require an explicit human check before further writes after uncertainty."""
    require_team_tasks_enabled(service)
    require_team_tasks_owner(service, conversation_id, user)
    with service.db.transaction():
        row = _operation_row(service, operation_id)
        if row is None or row["conversation_id"] != conversation_id:
            raise AppError(404, "TEAM_OPERATION_NOT_FOUND", "任务板操作记录不存在")
        if row["status"] != "uncertain":
            raise AppError(409, "TEAM_OPERATION_NOT_UNCERTAIN",
                           "该任务板操作不需要确认结果")
        if row.get("acknowledged_at") is None:
            service.db.execute(
                "UPDATE team_task_operations SET acknowledged_at=?,updated_at=? "
                "WHERE id=? AND status='uncertain' AND acknowledged_at IS NULL",
                (now(), now(), operation_id))
    return _operation_view(_operation_row(service, operation_id))


def is_controlled_rejection(error: AppError) -> bool:
    return error.code in CONTROLLED_REJECTION_CODES


def rejection_error(code: str) -> AppError:
    if code in REJECTION_MESSAGES:
        return AppError(409, code, REJECTION_MESSAGES[code])
    return AppError(409, "TEAM_OPERATION_REJECTED",
                    "原生任务板拒绝了本次操作，请刷新后核对任务板")


def stop_team_tasks_locked(service, conversation_id: str, reason: str) -> dict:
    """Revoke approval and fence pending operations inside a caller transaction."""
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation id is required")
    if not isinstance(reason, str) or STOP_REASON.fullmatch(reason) is None:
        raise ValueError("stop reason must be a short fixed code")
    timestamp = now()
    revoked = service.db.execute(
        "UPDATE team_task_authorizations SET status='revoked',revoked_at=?,revoked_reason=? "
        "WHERE conversation_id=? AND status='active'", (timestamp, reason, conversation_id))
    code = {"service_restarted": "SERVICE_RESTARTED",
            "user_revoked": "AUTHORIZATION_REVOKED",
            "permission_changed": "AUTHORIZATION_REVOKED",
            "conversation_archived": "AUTHORIZATION_REVOKED",
            "user_disabled": "AUTHORIZATION_REVOKED",
            "user_role_changed": "AUTHORIZATION_REVOKED"}.get(reason, "AUTHORIZATION_REVOKED")
    fenced = service.db.execute(
        "UPDATE team_task_operations SET status='uncertain',error_code=?,updated_at=? "
        "WHERE conversation_id=? AND status='pending'", (code, timestamp, conversation_id))
    return {"revoked": revoked.rowcount, "fenced": fenced.rowcount}


def stop_team_tasks(service, conversation_id: str, reason: str) -> dict:
    with service.db.lock:
        if service.db.conn.in_transaction:
            return stop_team_tasks_locked(service, conversation_id, reason)
        with service.db.transaction():
            return stop_team_tasks_locked(service, conversation_id, reason)


def recover_team_tasks(service) -> dict:
    """A restart is not a new approval: fence pending work and require re-auth.

    The official board and its JSONL history are not touched; the owner can
    still read the board and the recorded operation history after restart.
    """
    rows = service.db.all(
        "SELECT DISTINCT conversation_id FROM team_task_authorizations WHERE status='active' "
        "UNION SELECT DISTINCT conversation_id FROM team_task_operations WHERE status='pending'")
    if not rows:
        return {"revoked": 0, "fenced": 0}
    revoked = fenced = 0
    with service.db.transaction():
        for row in rows:
            result = stop_team_tasks_locked(service, row["conversation_id"], "service_restarted")
            revoked += result["revoked"]
            fenced += result["fenced"]
    return {"revoked": revoked, "fenced": fenced}


def revoke_user_team_tasks(service, user_id: str, reason: str) -> dict:
    """Durably revoke approvals after account disable or a role downgrade."""
    rows = service.db.all(
        "SELECT DISTINCT conversation_id FROM team_task_authorizations "
        "WHERE user_id=? AND status='active' "
        "UNION SELECT DISTINCT conversation_id FROM team_task_operations "
        "WHERE user_id=? AND status='pending'", (user_id, user_id))
    revoked = fenced = 0
    for row in rows:
        result = stop_team_tasks(service, row["conversation_id"], reason)
        revoked += result["revoked"]
        fenced += result["fenced"]
    return {"revoked": revoked, "fenced": fenced}
