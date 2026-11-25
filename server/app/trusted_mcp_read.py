"""Operator-managed, identity-bound read contracts for automatic MCP approval.

Not a classifier based on names, model output, remote hints, or user metadata.
The deployment owner supplies the exact approved contract independently of
ordinary chat/connection configuration. Unregistered or changed tools fall
back to normal approval. The remote definition is also checked before dispatch
by the existing Harness MCP transport.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone

from .connection_read_review import require_session_eligible
from .security import AppError

POLICY_FILENAME = "trusted-mcp-read-contracts.json"
MAX_POLICY_BYTES = 128 * 1024


def connection_identity(connection):
    """Bind the policy to this exact account, route, credential and revision.

    The stored encrypted credential is hashed with other internal fields;
    neither the credential nor its hash alone is exposed to the model.
    """
    value = {key: connection.get(key) for key in (
        "id", "owner_id", "scope", "revision", "url", "auth_type", "secret")}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _utc_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timezone required")
    return parsed.astimezone(timezone.utc)


def _load_policy(service):
    # Only the service's private deployment data directory is used. No URL,
    # model-controlled path or mutable connection metadata can select a file.
    path = service.settings.data_dir / POLICY_FILENAME
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_size > MAX_POLICY_BYTES):
            raise ValueError("Policy must be a private service-owned regular file")
        raw = stream.read(MAX_POLICY_BYTES + 1)
    if len(raw) > MAX_POLICY_BYTES:
        raise ValueError("Oversized policy")
    policy = json.loads(raw)
    if type(policy) is not dict or policy.get("version") != 1 or type(policy.get("connections")) is not dict:
        raise ValueError("Invalid read policy")
    return policy


def trusted_read_decision(service, connection, name, run_created_at):
    """Return a narrowly scoped, revocable policy decision, or no approval."""
    if not connection.get("enabled") or connection.get("status") != "connected":
        return None
    # An explicit connection-level owner review/revocation takes precedence.
    if connection.get("read_only_review_revision", 0):
        return None
    try:
        entry = _load_policy(service)["connections"].get(connection["id"])
        if not isinstance(entry, dict) or entry["identity"] != connection_identity(connection):
            return None
        if _utc_time(entry["registered_at"]) > _utc_time(run_created_at):
            return None
        if _utc_time(entry["expires_at"]) <= datetime.now(timezone.utc):
            return None
        policy_id = entry["policy_id"]
        if not isinstance(policy_id, str) or not policy_id or len(policy_id) > 100:
            return None
        digest = require_session_eligible(connection, name)
        if entry["tools"].get(name) != digest:
            return None
        # Recheck compares this record after network awaits. Removing or
        # changing the policy never silently reuses the initial decision.
        entry_digest = hashlib.sha256(json.dumps(entry, sort_keys=True,
                                   separators=(",", ":")).encode()).hexdigest()
        return {"scope": "trusted_read_contract", "reason": "平台可信只读接口契约",
                "connection_id": connection["id"], "definition_digest": digest,
                "contract_policy_id": policy_id, "contract_policy_digest": entry_digest}
    except (OSError, AppError, AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None
