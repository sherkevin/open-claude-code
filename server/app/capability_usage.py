"""Persistent, atomic per-attempt accounting for external capabilities.

Every real provider attempt reserves one unit in a single ``BEGIN IMMEDIATE``
transaction that also checks the current run/day budget.  Two concurrent calls
racing for the last unit therefore produce exactly one winner, across processes
and restarts, because the counters live in SQLite rather than process memory.

Logical tool calls and provider attempts are tracked separately: a retry or
fallback is a new attempt even though it belongs to the same logical call.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .db import now, uid
from .security import AppError


def day_key() -> str:
    """UTC day boundary, exposed explicitly so the UI can state the window."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class CapabilityUsage:
    def __init__(self, service):
        self.service = service
        self.db = service.db

    def claim_call(self, *, run_id: str, tool_call_id: str, tool_name: str,
                   arguments_digest: str, owner_token: str) -> None:
        # This durable claim also protects automatically approved calls and
        # survives a crash with an unknown provider outcome. Never replay it.
        with self.db.transaction():
            if self.db.one("SELECT 1 FROM capability_dispatch_claims WHERE run_id=? AND tool_call_id=?",
                           (run_id, tool_call_id)):
                raise AppError(403, "CAPABILITY_REPLAY", "该工具调用已经执行，请勿重复提交")
            self.db.execute(
                "INSERT INTO capability_dispatch_claims VALUES(?,?,?,?,?,?)",
                (run_id, tool_call_id, tool_name, arguments_digest, owner_token, now()))

    def reserve_attempt(self, *, run_id: str, assistant_id: str, user_id: str, capability: str,
                        call_id: str, attempt_index: int, provider: str, settings_revision: int,
                        calls_per_run: int, calls_per_day: int, provider_revision: int = 0) -> str:
        usage_id = uid()
        started = now()
        window = day_key()
        with self.db.transaction():
            if self.db.one("SELECT 1 FROM capability_attempts WHERE call_id=? AND attempt_index=?",
                           (call_id, attempt_index)):
                raise AppError(403, "CAPABILITY_REPLAY", "该工具请求已经派发")
            self.db.execute(
                "INSERT OR IGNORE INTO capability_calls(call_id,run_id,actor_id,assistant_id,capability,started_at,state) "
                "VALUES(?,?,?,?,?,?,'started')",
                (call_id, run_id, user_id, assistant_id, capability, started))
            run_row = self.db.one("SELECT calls FROM capability_run_counters WHERE run_id=? AND capability=?",
                                  (run_id, capability))
            day_row = self.db.one("SELECT calls FROM capability_day_counters WHERE actor_id=? AND capability=? AND day_key=?",
                                  (user_id, capability, window))
            if (run_row["calls"] if run_row else 0) >= calls_per_run:
                raise AppError(429, "CAPABILITY_QUOTA_EXCEEDED", "本次任务的能力调用次数已达上限")
            if (day_row["calls"] if day_row else 0) >= calls_per_day:
                raise AppError(429, "CAPABILITY_QUOTA_EXCEEDED", "本账号今日的能力调用次数已达上限")
            self.db.execute(
                "INSERT INTO capability_run_counters(run_id,capability,calls) VALUES(?,?,1) "
                "ON CONFLICT(run_id,capability) DO UPDATE SET calls=calls+1", (run_id, capability))
            self.db.execute(
                "INSERT INTO capability_day_counters(actor_id,capability,day_key,calls) VALUES(?,?,?,1) "
                "ON CONFLICT(actor_id,capability,day_key) DO UPDATE SET calls=calls+1",
                (user_id, capability, window))
            self.db.execute(
                "INSERT INTO capability_attempts(id,call_id,attempt_index,run_id,actor_id,assistant_id,capability,"
                "provider,provider_revision,settings_revision,state,started_at) VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?)",
                (usage_id, call_id, attempt_index, run_id, user_id, assistant_id, capability, provider,
                 provider_revision, settings_revision, started))
        return usage_id

    def settle_attempt(self, usage_id: str, *, state: str, error_code: str | None = None,
                       duration_ms: int | None = None, bytes_sent: int = 0, bytes_received: int = 0) -> None:
        """Record the terminal state of one attempt; never release a sent one."""
        finished = now()
        with self.db.transaction():
            row = self.db.one("SELECT * FROM capability_attempts WHERE id=?", (usage_id,))
            if not row:
                return
            self.db.execute(
                "UPDATE capability_attempts SET state=?,finished_at=?,duration_ms=?,error_code=?,"
                "bytes_sent=?,bytes_received=? WHERE id=?",
                (state, finished, duration_ms, error_code, int(bytes_sent or 0), int(bytes_received or 0), usage_id))
            self.db.execute("UPDATE capability_calls SET state=? WHERE call_id=?",
                            (state, row["call_id"]))

    def release_unsent(self, usage_id: str) -> None:
        """Release a reservation only when no request was ever sent."""
        with self.db.transaction():
            row = self.db.one("SELECT * FROM capability_attempts WHERE id=?", (usage_id,))
            if not row or row["state"] != "reserved":
                return
            self.db.execute("DELETE FROM capability_attempts WHERE id=?", (usage_id,))
            self.db.execute(
                "UPDATE capability_run_counters SET calls=MAX(calls-1,0) WHERE run_id=? AND capability=?",
                (row["run_id"], row["capability"]))
            self.db.execute(
                "UPDATE capability_day_counters SET calls=MAX(calls-1,0) WHERE actor_id=? AND capability=? AND day_key=?",
                (row["actor_id"], row["capability"], row["started_at"][:10]))
            remaining = self.db.one("SELECT COUNT(*) AS n FROM capability_attempts WHERE call_id=?", (row["call_id"],))
            if not remaining or not remaining["n"]:
                self.db.execute("DELETE FROM capability_calls WHERE call_id=?", (row["call_id"],))

    def attempt_counts(self, run_id: str, capability: str) -> int:
        row = self.db.one("SELECT calls FROM capability_run_counters WHERE run_id=? AND capability=?",
                          (run_id, capability))
        return int(row["calls"]) if row else 0
