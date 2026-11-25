"""Once-per-connection, definition-bound review for interactive MCP sessions.

A review records an accountable human verification, not a guarantee inferred
from a tool name, annotation or model. Team resources require an administrator;
private resources may be reviewed only by their authenticated owner. Tool use
still requires ordinary per-assistant ACLs, constraints and run-time checks.
"""
from __future__ import annotations

import json
import re

from .db import dump, now, uid
from .read_only_tools import _current_definition, require_eligible
from .security import AppError

# A conservative deny guard, never an allow classifier. Generic dispatch or
# arbitrary command/SQL entrypoints must stay on explicit approval even if
# their remote hints claim read-only.
_GENERIC = frozenset({"call", "invoke", "execute", "exec", "run", "action", "dispatch"})
_MUTATIONS = frozenset({"write", "create", "update", "delete", "insert", "upsert",
                       "modify", "remove", "drop", "truncate", "alter", "patch",
                       "submit", "approve", "transfer", "send", "commit"})
_UNBOUNDED_PARAMETERS = frozenset({"sql", "command", "cmd", "script", "action", "operation", "method"})


def require_session_tool_eligible(tool):
    digest = require_eligible(tool)
    name = tool["name"]
    words = set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()))
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    if (name.lower() in _GENERIC or words & _MUTATIONS
            or any(str(key).lower() in _UNBOUNDED_PARAMETERS for key in props)):
        raise AppError(409, "READ_ONLY_TOOL_UNSAFE", "写入、通用动作或任意执行类工具必须逐次审批")
    return digest


def require_session_eligible(connection, name):
    return require_session_tool_eligible(_current_definition(connection, name))


def review_tool_view(tool):
    try:
        require_session_tool_eligible(tool)
        return {"session_read_only_review_eligible": True}
    except AppError as exc:
        return {"session_read_only_review_eligible": False, "session_read_only_review_reason": exc.message}


def _reviewer_allowed(service, connection, reviewer):
    return bool(reviewer and reviewer["enabled"] and service.resource_editable(connection, reviewer)
                and (connection.get("scope") == "private" or reviewer["role"] == "admin"))


def current_review(service, connection, name, run_created_at):
    try:
        record = json.loads(connection.get("read_only_certifications") or "{}").get(name)
        if not isinstance(record, dict):
            return None
        if (record["connection_revision"] != connection["revision"]
                or record["definition_digest"] != require_session_eligible(connection, name)
                or record["verified_at"] > run_created_at):
            return None
        reviewer = service.db.one("SELECT id,role,enabled FROM users WHERE id=?", (record["verified_by"],))
        if not _reviewer_allowed(service, connection, reviewer):
            return None
        return record
    except (AppError, TypeError, ValueError, KeyError, AttributeError):
        return None


def save_review(service, connection_id, data, user):
    from .mcp_tools import contains_credential
    from .service import connection_view

    with service.db.transaction():
        connection = service.require_resource("connections", connection_id, user, edit=True)
        reviewer = service.require("users", user["id"])
        if not _reviewer_allowed(service, connection, reviewer):
            raise AppError(403, "READ_ONLY_REVIEW_DENIED", "只有私人连接所有者或团队连接管理员可以核验")
        if (connection["revision"] != data["expected_connection_revision"]
                or connection.get("read_only_review_revision", 0) != data["expected_review_revision"]):
            raise AppError(409, "READ_ONLY_REVIEW_CONFLICT", "连接或核验记录已变更，请刷新后重试")
        if data["tools"] and (not connection["enabled"] or connection["status"] != "connected"):
            raise AppError(409, "READ_ONLY_CONNECTION_UNVERIFIED", "请先启用并测试连接")
        timestamp = now()
        credential = service.secrets.decrypt(connection.get("secret")) if data["tools"] else None
        prior = json.loads(connection.get("read_only_certifications") or "{}")
        reviews = {}
        for name, record in data["tools"].items():
            digest = require_session_eligible(connection, name)
            basis = record["basis"].strip()
            if (record["connection_revision"] != connection["revision"]
                    or record["definition_digest"] != digest):
                raise AppError(409, "READ_ONLY_CONFIRMATION_STALE", "工具定义已变化，请重新核验")
            if not basis or len(basis) > 500 or credential and contains_credential(basis, credential):
                raise AppError(422, "READ_ONLY_CONFIRMATION_INVALID", "核验依据须为1–500字，不得包含凭据")
            existing = prior.get(name)
            if (isinstance(existing, dict) and existing.get("connection_revision") == connection["revision"]
                    and existing.get("definition_digest") == digest and existing.get("basis") == basis
                    and existing.get("verified_by") == reviewer["id"] and existing.get("verified_at")):
                reviews[name] = existing
            else:
                reviews[name] = {"connection_revision": connection["revision"],
                                 "definition_digest": digest, "basis": basis,
                                 "verified_by": reviewer["id"], "verified_at": timestamp}
        revision = connection.get("read_only_review_revision", 0) + 1
        serialized = dump(reviews)
        service.db.execute("UPDATE connections SET read_only_certifications=?,read_only_review_revision=? WHERE id=?",
                           (serialized, revision, connection_id))
        service.db.execute("INSERT INTO connection_read_only_review_audit VALUES(?,?,?,?,?,?,?)",
                           (uid(), connection_id, connection["revision"], revision, reviewer["id"], serialized, timestamp))
        return connection_view(service.require_resource("connections", connection_id, reviewer), reviewer, service)
