"""Authorization for user-owned and team-managed capability resources."""
import json
from .security import AppError


TABLES = {"connections", "assistants", "skills", "knowledge_bases"}


def resource_access(row, user):
    return bool(row and not row.get("deleted") and (row.get("scope", "team") == "team" or user.get("id") and row.get("owner_id") == user.get("id")))


def delegated_assistant_editor(row, user):
    # This column exists only on assistants. A grant never changes the user's
    # role or grants edit rights to the assistant's knowledge/MCP/Skill objects.
    if not row or row.get("scope", "team") != "team" or not user.get("id"):
        return False
    try:
        editors = json.loads(row.get("editor_user_ids") or "[]")
    except (TypeError, ValueError):
        return False
    return isinstance(editors, list) and all(isinstance(item, str) for item in editors) and user["id"] in editors


def resource_editable(row, user):
    return bool(resource_access(row, user) and ((user["role"] == "admin" or delegated_assistant_editor(row, user)) if row.get("scope", "team") == "team" else row.get("owner_id") == user.get("id")))


def scope_fields(row, user=None):
    return {"owner_id": row.get("owner_id"), "scope": row.get("scope", "team"), "can_edit": resource_editable(row, user) if user else False}


def choose_scope(data, old, user):
    scope = data.get("scope") or (old or {}).get("scope") or ("private" if user else "team")
    if scope not in {"private", "team"}:
        raise AppError(422, "INVALID_SCOPE", "配置范围无效")
    if user and scope == "team" and user["role"] != "admin" and not (old and old.get("scope", "team") == "team" and resource_editable(old, user)):
        raise AppError(403, "ADMIN_REQUIRED", "团队共享配置由管理员管理")
    owner = old.get("owner_id") if old else (user or {}).get("id")
    # Taking a legacy team object private gives it a concrete owner.
    if scope == "private" and not owner:
        owner = (user or {}).get("id")
    if scope == "private" and not owner:
        raise AppError(422, "OWNER_REQUIRED", "个人配置需要明确的所有者")
    return scope, owner
