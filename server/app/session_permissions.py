"""Session approval preferences inside the current host authorization boundary."""
from __future__ import annotations

import json

from .db import now, uid
from .read_only_tools import require_snapshot_confirmation, validate_confirmation
from .security import AppError

MODES = ("manual", "auto", "full")
LOCAL_WRITES = frozenset({"mcp__workspace__file_write", "mcp__workspace__generate_xlsx"})


def change(service, conversation, user, mode, *, creating=False):
    if mode is None or mode == conversation.get("permission_mode", "manual"):
        return
    if mode not in MODES:
        raise AppError(422, "INVALID_PERMISSION_MODE", "请选择有效的会话审批方式")
    if (service.policy.is_trial or conversation.get("runtime_type") != "harness") and mode != "manual":
        raise AppError(403, "PERMISSION_MODE_UNAVAILABLE", "当前会话只支持原有手动审批")
    if mode == "full" and user.get("role") != "admin":
        raise AppError(403, "PERMISSION_MODE_ADMIN_REQUIRED", "完全权限需要管理员设置")
    if not creating and (conversation.get("active_run_id") or conversation.get("archived")):
        raise AppError(409, "PERMISSION_MODE_SESSION_BUSY", "请在空闲且未归档的会话中切换审批方式")
    prior = conversation.get("permission_mode", "manual")
    service.db.execute("UPDATE conversations SET permission_mode=?, permission_mode_by=? WHERE id=?",
                       (mode, user["id"], conversation["id"]))
    service.db.execute("INSERT INTO conversation_permission_changes VALUES(?,?,?,?,?,?)",
                       (uid(), conversation["id"], user["id"], prior, mode, now()))


def record_certifications(service, assistant_id, revision, bindings, user):
    if not user or user.get("role") != "admin":
        return
    for binding in bindings:
        confirmations = binding.get("read_only_confirmations") or {}
        if not confirmations:
            continue
        try:
            connection = service.require_resource("connections", binding["connection_id"], user)
        except AppError:
            continue
        for name in confirmations:
            try:
                digest = validate_confirmation(binding, connection, name)
            except AppError:
                # Saving unrelated edits may retain an inactive confirmation.
                continue
            service.db.execute(
                "INSERT INTO session_read_only_certifications VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(assistant_id,connection_id,tool_name) DO UPDATE SET "
                "assistant_revision=excluded.assistant_revision,confirmation_digest=excluded.confirmation_digest,admin_id=excluded.admin_id",
                (assistant_id, revision, connection["id"], name, digest, user["id"]),
            )


def automatic_decision(service, run, tool_name):
    """Apply only after ordinary ACL, argument and approval-policy checks."""
    metadata = json.loads(run.get("execution_meta") or "{}")
    mode = metadata.get("permission_mode", "manual")
    if mode not in {"auto", "full"} or service.policy.is_trial:
        return None
    conversation = service.require("conversations", run["conversation_id"])
    actor_id = conversation.get("permission_mode_by")
    if (conversation.get("permission_mode") != mode or actor_id != metadata.get("permission_mode_by")
            or actor_id != run["user_id"] or conversation["active_run_id"] != run["id"]):
        return None
    actor = service.require("users", actor_id)
    if not actor["enabled"] or mode == "full" and actor["role"] != "admin":
        return None
    decision = {"mode": mode, "decided_by": actor_id, "conversation_id": conversation["id"],
                "scope": "workspace", "reason": "管理员选择了会话工作区完全权限"}
    if mode == "full" and tool_name in LOCAL_WRITES:
        return decision
    if not tool_name.startswith("mcp__c"):
        return None
    _, binding, connection = service._business_tool_access(conversation["id"], tool_name)
    name = tool_name[len(f"mcp__c{connection['id']}__"):]
    assistant = service.usable_assistant(conversation["assistant_id"], actor)
    snapshot = json.loads(run.get("capability_snapshot") or "null") or {}
    previous = next((item for item in snapshot.get("connections", []) if item["connection_id"] == connection["id"]), None)
    try:
        digest = require_snapshot_confirmation(binding, previous, connection, name)
    except AppError:
        return None
    signed = service.db.one(
        "SELECT admin_id FROM session_read_only_certifications WHERE assistant_id=? AND assistant_revision=? "
        "AND connection_id=? AND tool_name=? AND confirmation_digest=?",
        (assistant["id"], assistant["revision"], connection["id"], name, digest),
    )
    if not signed:
        return None
    reviewer = service.require("users", signed["admin_id"])
    if not reviewer["enabled"] or reviewer["role"] != "admin":
        return None
    return {**decision, "scope": "certified_read_only_connection", "reason": "管理员核验了当前只读工具定义",
            "connection_id": connection["id"], "certified_by": reviewer["id"],
            "definition_digest": binding["read_only_confirmations"][name]["definition_digest"]}
