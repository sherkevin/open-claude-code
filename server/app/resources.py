"""Authorization for user-owned and team-managed capability resources."""
from .security import AppError


TABLES = {"connections", "assistants", "skills", "knowledge_bases"}


def resource_access(row, user):
    return bool(row and not row.get("deleted") and (row.get("scope", "team") == "team" or user.get("id") and row.get("owner_id") == user.get("id")))


def resource_editable(row, user):
    return bool(resource_access(row, user) and (user["role"] == "admin" if row.get("scope", "team") == "team" else row.get("owner_id") == user.get("id")))


def scope_fields(row, user=None):
    return {"owner_id": row.get("owner_id"), "scope": row.get("scope", "team"), "can_edit": resource_editable(row, user) if user else False}


def choose_scope(data, old, user):
    scope = data.get("scope") or (old or {}).get("scope") or ("private" if user else "team")
    if scope not in {"private", "team"}:
        raise AppError(422, "INVALID_SCOPE", "配置范围无效")
    if user and scope == "team" and user["role"] != "admin":
        raise AppError(403, "ADMIN_REQUIRED", "团队共享配置由管理员管理")
    owner = old.get("owner_id") if old else (user or {}).get("id")
    # Taking a legacy team object private gives it a concrete owner.
    if scope == "private" and not owner:
        owner = (user or {}).get("id")
    if scope == "private" and not owner:
        raise AppError(422, "OWNER_REQUIRED", "个人配置需要明确的所有者")
    return scope, owner
