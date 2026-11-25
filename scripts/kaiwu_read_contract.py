"""Operator-only lifecycle for trusted MCP read contracts.

No business calls, model calls, secret decryption, permission changes or new
trust inferred from tool names. Reuses the live Harness policy format.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.connection_read_review import require_session_eligible
from app.security import AppError
from app.trusted_mcp_read import (
    MAX_POLICY_BYTES, POLICY_FILENAME, _load_policy, _utc_time,
    connection_identity, trusted_read_decision,
)

DIGEST = re.compile(r"^[a-f0-9]{64}$")
CONNECTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
MANIFEST_KEYS = frozenset({
    "version", "connection_id", "expected_identity", "expected_policy_sha256",
    "policy_id", "review_reference", "expires_at", "authorization_expires_at",
    "tools",
})


class ContractError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def utc_now():
    return datetime.now(timezone.utc)


def require(condition, code):
    if not condition:
        raise ContractError(code)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        require(key not in value, "DUPLICATE_JSON_KEY")
        value[key] = item
    return value


def read_manifest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o077 and info.st_size <= MAX_POLICY_BYTES,
                "MANIFEST_NOT_PRIVATE")
        raw = stream.read(MAX_POLICY_BYTES + 1)
    require(len(raw) <= MAX_POLICY_BYTES, "MANIFEST_TOO_LARGE")
    return json.loads(raw, object_pairs_hook=unique_object)


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory).resolve(strict=True)
        info = self.directory.stat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and not info.st_mode & 0o022, "DATA_DIRECTORY_NOT_OPERATOR_OWNED")
        self.path = self.directory / POLICY_FILENAME
        self.service = SimpleNamespace(settings=SimpleNamespace(data_dir=self.directory))

    def load(self):
        try:
            policy = _load_policy(self.service)
        except FileNotFoundError:
            return {"version": 1, "connections": {}}, "absent"
        except (OSError, ValueError, TypeError, RecursionError):
            raise ContractError("POLICY_INVALID_OR_UNREADABLE") from None
        return policy, digest(policy)

    def connection(self, cid):
        require(isinstance(cid, str) and CONNECTION_ID.fullmatch(cid), "INVALID_CONNECTION_ID")
        uri = (self.directory / "app.sqlite3").as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM connections WHERE id=? AND deleted=0", (cid,)).fetchone()
        require(row is not None, "CONNECTION_NOT_FOUND")
        return dict(row)

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.directory / ".trusted-mcp-read-contracts.lock",
                     os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "rb+") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and not info.st_mode & 0o077, "LOCK_NOT_PRIVATE")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def write(self, policy):
        raw = canonical(policy) + b"\n"
        require(len(raw) <= MAX_POLICY_BYTES, "POLICY_TOO_LARGE")
        fd, temporary = tempfile.mkstemp(prefix=".trusted-mcp-read-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def inspect(self, cid):
        row = self.connection(cid)
        policy, policy_sha = self.load()
        entry = policy["connections"].get(cid)
        now = utc_now()
        reason = "not_registered"
        expiry = None
        if entry is not None:
            try:
                expiry = _utc_time(entry["expires_at"])
                if entry["identity"] != connection_identity(row):
                    reason = "identity_changed"
                elif expiry <= now:
                    reason = "expired"
                elif _utc_time(entry["registered_at"]) > now:
                    reason = "not_yet_active"
                else:
                    reason = "registered"
            except (KeyError, TypeError, ValueError, AttributeError):
                reason = "invalid_entry"
        if row.get("read_only_review_revision", 0):
            reason = "manual_override"
        elif not row.get("enabled") or row.get("status") != "connected":
            reason = "connection_not_ready"

        try:
            definitions = json.loads(row.get("tools") or "[]")
            require(isinstance(definitions, list), "INVALID_TOOL_DIRECTORY")
        except (ValueError, TypeError):
            raise ContractError("INVALID_TOOL_DIRECTORY") from None

        results = []
        reviewed = entry.get("tools", {}) if isinstance(entry, dict) else {}
        if not isinstance(reviewed, dict):
            reviewed = {}
        names = sorted({t["name"] for t in definitions
                        if isinstance(t, dict) and isinstance(t.get("name"), str)
                        and CONNECTION_ID.fullmatch(t["name"].replace(".", "_"))})
        for name in names:
            try:
                definition_sha = require_session_eligible(row, name)
                eligible = True
            except AppError:
                definition_sha, eligible = None, False
            actual = bool(trusted_read_decision(self.service, row, name, now.isoformat()))
            state = ("platform_auto" if actual else
                     "not_eligible" if not eligible else
                     "manual_override" if reason == "manual_override" else
                     "needs_review")
            tool_reason = ("trusted_contract_current" if actual else
                           "not_eligible_for_review" if not eligible else
                           reason if reason != "registered" else
                           "not_in_reviewed_contract" if name not in reviewed else
                           "definition_changed" if reviewed[name] != definition_sha else
                           "policy_not_effective")
            results.append({"name": name, "definition_digest": definition_sha,
                            "eligible_for_review": eligible, "platform_default": state,
                            "reason": tool_reason})
        return {
            "connection_id": cid, "connection_revision": row["revision"],
            "connection_identity": connection_identity(row),
            "policy_sha256": policy_sha, "registry_status": reason,
            "registered_policy_id": entry.get("policy_id") if isinstance(entry, dict) else None,
            "expires_at": expiry.isoformat() if expiry else None,
            "expires_within_7_days": bool(expiry and now < expiry <= now + timedelta(days=7)),
            "platform_auto_count": sum(t["platform_default"] == "platform_auto" for t in results),
            "registered_tool_count": len(reviewed),
            "missing_reviewed_tool_count": len(set(reviewed) - set(names)),
            "tools": results,
            "notice": "Eligibility is not approval; other ACLs and approval policies still apply.",
        }

    def build_entry(self, manifest, row, stamp):
        require(type(manifest) is dict and set(manifest) == MANIFEST_KEYS
                and type(manifest["version"]) is int and manifest["version"] == 1,
                "INVALID_MANIFEST")
        require(manifest["connection_id"] == row["id"], "CONNECTION_MISMATCH")
        require(isinstance(manifest["expected_identity"], str)
                and DIGEST.fullmatch(manifest["expected_identity"])
                and manifest["expected_identity"] == connection_identity(row), "IDENTITY_CHANGED")
        require(row.get("enabled") and row.get("status") == "connected",
                "CONNECTION_NOT_READY")
        require(not row.get("read_only_review_revision", 0), "MANUAL_OVERRIDE_ACTIVE")
        for field in ("policy_id", "review_reference"):
            require(isinstance(manifest[field], str) and REFERENCE.fullmatch(manifest[field]),
                    "INVALID_REVIEW_REFERENCE")
        try:
            expiry = _utc_time(manifest["expires_at"])
            authority_expiry = _utc_time(manifest["authorization_expires_at"])
        except (ValueError, TypeError, AttributeError):
            raise ContractError("INVALID_EXPIRY") from None
        require(stamp < expiry <= authority_expiry, "EXPIRY_OUTSIDE_REVIEWED_AUTHORIZATION")
        tools = manifest["tools"]
        require(type(tools) is dict and 1 <= len(tools) <= 128, "INVALID_TOOL_SELECTION")
        for name, expected in tools.items():
            require(isinstance(name, str) and isinstance(expected, str)
                    and DIGEST.fullmatch(expected), "INVALID_TOOL_DIGEST")
            try:
                actual = require_session_eligible(row, name)
            except AppError:
                raise ContractError("TOOL_NOT_ELIGIBLE") from None
            require(actual == expected, "TOOL_DEFINITION_CHANGED")
        return {"identity": manifest["expected_identity"], "policy_id": manifest["policy_id"],
                "review_reference": manifest["review_reference"],
                "registered_at": stamp.isoformat(), "expires_at": expiry.isoformat(),
                "authorization_expires_at": authority_expiry.isoformat(), "tools": dict(tools)}

    def apply(self, manifest, *, commit=False):
        # A prepared plan never authorizes a call. Commit requires an independently
        # reviewed manifest, exact identity/digests and current policy CAS.
        require(type(manifest) is dict and set(manifest) == MANIFEST_KEYS, "INVALID_MANIFEST")
        cid = manifest["connection_id"]
        with self.lock():
            policy, before = self.load()
            require(manifest["expected_policy_sha256"] == before, "POLICY_CHANGED")
            row = self.connection(cid)
            entry = self.build_entry(manifest, row, utc_now())
            prior = policy["connections"].get(cid)
            unchanged = isinstance(prior, dict) and {
                k: v for k, v in prior.items() if k != "registered_at"
            } == {k: v for k, v in entry.items() if k != "registered_at"}
            if unchanged:
                entry = prior
            result = {**policy, "connections": {**policy["connections"], cid: entry}}
            require(len(canonical(result)) + 1 <= MAX_POLICY_BYTES, "POLICY_TOO_LARGE")
            if commit and not unchanged:
                self.write(result)
            return {"action": "apply", "committed": commit, "changed": not unchanged, "connection_id": cid,
                    "policy_id": entry["policy_id"], "review_reference": entry["review_reference"],
                    "expires_at": entry["expires_at"], "tools": sorted(entry["tools"]),
                    "before_policy_sha256": before, "after_policy_sha256": digest(result)}

    def revoke(self, cid, expected_policy_sha256, *, commit=False):
        require(isinstance(cid, str) and CONNECTION_ID.fullmatch(cid), "INVALID_CONNECTION_ID")
        # Revocation can remove a stale/deleted connection's rule without
        # re-reading its credentials. It does not edit separate manual reviews.
        with self.lock():
            policy, before = self.load()
            require(expected_policy_sha256 == before, "POLICY_CHANGED")
            require(cid in policy["connections"], "POLICY_ENTRY_NOT_FOUND")
            result = {**policy, "connections": {k: v for k, v in policy["connections"].items() if k != cid}}
            if commit:
                self.write(result)
            return {"action": "revoke_platform_default", "committed": commit,
                    "connection_id": cid, "before_policy_sha256": before,
                    "after_policy_sha256": digest(result)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Read-only current registry and tool eligibility status")
    inspect.add_argument("--connection-id", required=True)
    apply = sub.add_parser("apply", help="Validate an independently reviewed manifest; dry run unless --commit")
    apply.add_argument("--manifest", type=Path, required=True)
    apply.add_argument("--commit", action="store_true")
    revoke = sub.add_parser("revoke", help="Remove one platform-default entry; dry run unless --commit")
    revoke.add_argument("--connection-id", required=True)
    revoke.add_argument("--expected-policy-sha256", required=True)
    revoke.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)
    try:
        registry = Registry(args.data_dir)
        if args.command == "inspect":
            result = registry.inspect(args.connection_id)
        elif args.command == "apply":
            result = registry.apply(read_manifest(args.manifest), commit=args.commit)
        else:
            result = registry.revoke(args.connection_id, args.expected_policy_sha256, commit=args.commit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ContractError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    except (OSError, sqlite3.Error, TypeError, ValueError, KeyError, RecursionError):
        # Do not echo raw exception strings, remote descriptions, SQL, URLs
        # or credential-bearing database rows.
        print('{"ok":false,"error":"REGISTRY_OPERATION_FAILED"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
