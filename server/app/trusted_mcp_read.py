"""Operator-managed, identity-bound read contracts for automatic MCP approval.

Not a classifier based on names, model output, remote hints, or user metadata.
The deployment owner supplies the exact approved contract independently of
ordinary chat/connection configuration. Unregistered or changed tools fall
back to normal approval. The remote definition is also checked before dispatch
by the existing Harness MCP transport.
"""
from __future__ import annotations

import hashlib
import hmac
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
    blocked = policy.get("blocked_reuse", [])
    if (type(blocked) is not list or any(not isinstance(item, str) or not item for item in blocked)
            or len(set(blocked)) != len(blocked)):
        raise ValueError("Invalid reuse exclusions")
    return policy


def _entry_decision(connection, name, run_created_at, entry):
    if (not connection.get("enabled") or connection.get("status") != "connected"
            or connection.get("deleted") or connection.get("read_only_review_revision", 0)):
        return None
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
    entry_digest = hashlib.sha256(json.dumps(entry, sort_keys=True,
                               separators=(",", ":")).encode()).hexdigest()
    return {"scope": "trusted_read_contract", "reason": "平台可信只读接口契约",
            "connection_id": connection["id"], "definition_digest": digest,
            "contract_policy_id": policy_id, "contract_policy_digest": entry_digest}


def _reuse_decision(service, connection, name, run_created_at, entries):
    """Explicitly opted-in reuse of the *same* reviewed source and identity.

    Never infer provider trust from a name, a host, or the client's metadata.
    A new bearer value, account, sharing scope, URL, or tool definition does
    not match. No OAuth or anonymous reuse. No credentials leave the service.
    The anchor must retain its current direct policy and exact identity.
    """
    if connection.get("auth_type") != "bearer" or not connection.get("secret"):
        return None
    matches = []
    for anchor_id, entry in entries.items():
        if (anchor_id == connection["id"] or not isinstance(entry, dict)
                or entry.get("allow_reimport") is not True
                or name not in entry.get("tools", {})):
            continue
        anchor = service.db.one("SELECT * FROM connections WHERE id=? AND deleted=0", (anchor_id,))
        if not anchor or any(anchor.get(k) != connection.get(k)
                             for k in ("owner_id", "scope", "url", "auth_type")):
            continue
        decision = _entry_decision(anchor, name, run_created_at, entry)
        if not decision or not anchor.get("secret"):
            continue
        if require_session_eligible(connection, name) != decision["definition_digest"]:
            continue
        try:
            old_credential = service.secrets.decrypt(anchor["secret"])
            new_credential = service.secrets.decrypt(connection["secret"])
        except Exception:
            continue
        if (not old_credential or not new_credential
                or not hmac.compare_digest(old_credential.encode("utf-8"), new_credential.encode("utf-8"))):
            continue
        # An exact duplicate has a different ID and randomized ciphertext.
        # Recheck binds both the original rule and the current target identity.
        matches.append({**decision, "connection_id": connection["id"],
                        "reason": "复用同一已审核业务来源的可信只读接口契约",
                        "contract_policy_digest": hashlib.sha256(
                            (decision["contract_policy_digest"] + connection_identity(connection)).encode()).hexdigest()})
        if len(matches) > 1:
            return None
    return matches[0] if matches else None


def trusted_read_decision(service, connection, name, run_created_at):
    """Return a narrowly scoped, revocable policy decision, or no approval."""
    if (not connection.get("enabled") or connection.get("status") != "connected"
            or connection.get("deleted") or connection.get("read_only_review_revision", 0)):
        return None
    try:
        policy = _load_policy(service)
        return _policy_decision(service, connection, name, run_created_at, policy)
    except (OSError, AppError, AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        return None

def _policy_decision(service, connection, name, run_created_at, policy):
    if (not connection.get("enabled") or connection.get("status") != "connected"
            or connection.get("deleted") or connection.get("read_only_review_revision", 0)):
        return None
    entries = policy["connections"]
    # Explicit revocation or a stale direct entry cannot silently fall back
    # to a reusable anchor. Revocation also works on derived connections.
    if connection["id"] in policy.get("blocked_reuse", []):
        return None
    if connection["id"] in entries:
        return _entry_decision(connection, name, run_created_at, entries[connection["id"]])
    return _reuse_decision(service, connection, name, run_created_at, entries)


def platform_read_defaults(service, connection, names, stamp):
    """Public view exposes only per-tool eligibility, not private policy data."""
    if not connection.get("enabled") or connection.get("status") != "connected":
        return set()
    try:
        policy = _load_policy(service)
    except (OSError, ValueError, TypeError, RecursionError):
        return set()
    result = set()
    for name in names:
        try:
            if _policy_decision(service, connection, name, stamp, policy):
                result.add(name)
        except (OSError, AppError, AttributeError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
            continue
    return result
