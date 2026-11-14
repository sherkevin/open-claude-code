"""Exact, one-time approval receipts for capability invocations.

The existing approval API returns a boolean.  That is enough for legacy tools
but not enough to prove that *this* invocation was approved: two calls in the
same run with identical tool and arguments would both be satisfied by the same
allowed row.  This module stores a receipt keyed by the actual host tool-call
id and a canonical digest of the exact public arguments.  The first dispatch
claims it atomically; later rechecks of the same logical call may read their own
claim, while a different call, mutated arguments, expiry or replay is refused.

Orange owns producing receipts from the real approval chain.  Purple owns the
table, the claim/verify predicates and the executor checks.  A receipt never
grants anything by itself: business authorization and the host recheck still
run.
"""
from __future__ import annotations

import hashlib
import json

from .db import now, uid
from .security import AppError

ALLOWED_SOURCES = {"user", "dual", "human_egress", "model", "host"}
HUMAN_SOURCES = {"user", "dual", "human_egress"}


def arguments_digest(arguments: dict) -> str:
    canonical = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _key(run_id: str, tool_call_id: str, tool_name: str, digest: str):
    return (run_id, tool_call_id, tool_name, digest)


def _row(service, run_id, tool_call_id, tool_name, digest):
    return service.db.one(
        "SELECT * FROM capability_approval_receipts WHERE run_id=? AND tool_call_id=? AND tool_name=? AND arguments_digest=?",
        _key(run_id, tool_call_id, tool_name, digest))


def issue_approval_receipt(service, *, run_id: str, tool_call_id: str, tool_name: str, arguments: dict,
                           decision: str, source: str, approval_id: str | None = None,
                           expires_at: str | None = None) -> str:
    """Record the real approval decision for exactly this host invocation."""
    if decision not in {"allowed", "denied"}:
        raise AppError(422, "APPROVAL_RECEIPT_INVALID", "审批回执结果无效")
    if source not in ALLOWED_SOURCES:
        raise AppError(422, "APPROVAL_RECEIPT_INVALID", "审批回执来源无效")
    if not all(isinstance(value, str) and value for value in (run_id, tool_call_id, tool_name)):
        raise AppError(422, "APPROVAL_RECEIPT_INVALID", "审批回执上下文无效")
    digest = arguments_digest(arguments)
    with service.db.transaction():
        existing = _row(service, run_id, tool_call_id, tool_name, digest)
        if existing:
            if existing["claimed_by_call"]:
                raise AppError(403, "APPROVAL_REPLAY", "该调用的审批回执已被使用")
            if existing["decision"] != decision or existing["source"] != source:
                raise AppError(409, "APPROVAL_RECEIPT_CONFLICT", "审批回执已存在且内容不同")
            return existing["receipt_id"]
        receipt_id = uid()
        service.db.execute(
            "INSERT INTO capability_approval_receipts(receipt_id,run_id,tool_call_id,tool_name,arguments_digest,"
            "decision,source,approval_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, run_id, tool_call_id, tool_name, digest, decision, source, approval_id, now(), expires_at))
    return receipt_id


def _validate(service, *, run_id, tool_call_id, tool_name, arguments, call_scope_id,
              require_human, expected_receipt_id):
    digest = arguments_digest(arguments)
    row = _row(service, run_id, tool_call_id, tool_name, digest)
    if not row:
        raise AppError(403, "APPROVAL_REQUIRED", "此操作需要针对本次调用的审批回执")
    if expected_receipt_id and row["receipt_id"] != expected_receipt_id:
        raise AppError(403, "APPROVAL_MISMATCH", "审批回执与本次调用不匹配")
    if row["decision"] != "allowed":
        raise AppError(403, "APPROVAL_DENIED", "审批未通过")
    if require_human and row["source"] not in HUMAN_SOURCES:
        raise AppError(403, "APPROVAL_HUMAN_REQUIRED", "此数据外发需要人工或双人审批")
    expires = row.get("expires_at")
    if expires and expires < now():
        raise AppError(403, "APPROVAL_EXPIRED", "审批回执已过期")
    if row["claimed_by_call"] and row["claimed_by_call"] != call_scope_id:
        raise AppError(403, "APPROVAL_REPLAY", "审批回执已被另一调用使用")
    return row


def claim_approval_receipt(service, *, run_id: str, tool_call_id: str, tool_name: str, arguments: dict,
                           call_scope_id: str, require_human: bool = False,
                           expected_receipt_id: str | None = None) -> str:
    """Claim the receipt for this invocation, or accept its own prior claim."""
    with service.db.transaction():
        row = _validate(service, run_id=run_id, tool_call_id=tool_call_id, tool_name=tool_name,
                        arguments=arguments, call_scope_id=call_scope_id, require_human=require_human,
                        expected_receipt_id=expected_receipt_id)
        if not row["claimed_by_call"]:
            service.db.execute(
                "UPDATE capability_approval_receipts SET claimed_by_call=?,claimed_at=? WHERE receipt_id=?",
                (call_scope_id, now(), row["receipt_id"]))
        return row["receipt_id"]


def verify_approval_receipt(service, *, run_id: str, tool_call_id: str, tool_name: str, arguments: dict,
                            call_scope_id: str, require_human: bool = False,
                            expected_receipt_id: str | None = None) -> None:
    """Read-only recheck of the receipt already claimed by this invocation."""
    row = _validate(service, run_id=run_id, tool_call_id=tool_call_id, tool_name=tool_name,
                    arguments=arguments, call_scope_id=call_scope_id, require_human=require_human,
                    expected_receipt_id=expected_receipt_id)
    if row["claimed_by_call"] != call_scope_id:
        raise AppError(403, "APPROVAL_REQUIRED", "审批回执尚未由本次调用领取")
