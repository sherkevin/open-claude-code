#!/usr/bin/env python3
"""Offline rollback gate for legacy Kaiwu versions (through 320e9d7).

Inspect is read-only. Prepare requires a stopped, isolated workbench and a
matching inspection token. This script never stops services, deletes history,
executes business tools, restores backups or starts the old binary.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

PAUSE_REASON = "版本回退保护：高级周期已暂停；升级后请核对时间及历史，再手动恢复。"


class GuardError(Exception):
    pass


def inspect(conn):
    # Include unknown recurrence values: the legacy worker treats all
    # non-daily recurrences as weekly. Only supported active rows may survive.
    unsupported = conn.execute("""SELECT id,recurrence,status,next_at
        FROM conversation_schedule_series WHERE recurrence NOT IN ('daily','weekly')
        AND status='scheduled' ORDER BY id""").fetchall()
    active_runs = conn.execute("""SELECT id,status FROM runs
        WHERE status NOT IN ('succeeded','failed','cancelled','interrupted')
        ORDER BY id""").fetchall()
    pending = conn.execute("""SELECT conversation_id,id,status,revision FROM message_queue
        WHERE status='pending' ORDER BY conversation_id,id""").fetchall()
    active_conversations = conn.execute("""SELECT id,active_run_id FROM conversations
        WHERE active_run_id IS NOT NULL ORDER BY id""").fetchall()
    undelivered = conn.execute("""SELECT s.id,s.status
        FROM conversation_schedules s
        JOIN conversation_schedule_series p ON p.id=s.series_id
        LEFT JOIN message_queue q ON q.id=s.id AND q.conversation_id=s.conversation_id
        LEFT JOIN runs r ON r.id=q.run_id AND r.conversation_id=s.conversation_id
        WHERE p.recurrence NOT IN ('daily','weekly')
          AND (s.status='scheduled' OR
            (s.status='queued' AND (q.id IS NULL OR
              (q.status='accepted' AND r.id IS NULL))))
        ORDER BY s.id""").fetchall()
    # The token contains no message bodies or credentials and reveals no IDs.
    states = [unsupported,active_runs,pending,active_conversations,undelivered]
    token=hashlib.sha256(json.dumps([list(map(tuple,rows)) for rows in states],
                                   separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
    counts={"unsupported_active_series":len(unsupported),"active_runs":len(active_runs),
            "pending_queue":len(pending),"active_conversations":len(active_conversations),
            "undelivered_advanced_occurrences":len(undelivered)}
    quiescent=not any(counts[k] for k in counts if k!="unsupported_active_series")
    return {**counts,"quiescent":quiescent,
            "safe_for_legacy_scheduler":quiescent and not unsupported,"token":token}


def prepare(conn,expected_token):
    """Must be called while the workbench is stopped; compare within a write txn."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        before=inspect(conn)
        if before["token"]!=expected_token:
            raise GuardError("STALE_INSPECTION: inspect again while workbench remains stopped")
        if not before["quiescent"]:
            raise GuardError("NOT_QUIESCENT: drain or reconcile existing runs and queue; do not replay")
        changed=conn.execute("""UPDATE conversation_schedule_series SET status='paused',error=?
            WHERE recurrence NOT IN ('daily','weekly') AND status='scheduled'""",(PAUSE_REASON,)).rowcount
        after=inspect(conn)
        if changed!=before["unsupported_active_series"] or not after["safe_for_legacy_scheduler"]:
            raise GuardError("ROLLBACK_GUARD_FAILED: state changed or unsupported active plans remain")
        conn.execute("COMMIT")
        return {"paused":changed,"before":before,"after":after}
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def open_database(path,readonly):
    path=Path(path)
    if not path.is_file() or path.is_symlink():
        raise GuardError("DATABASE_NOT_FOUND: specify the exact regular database file")
    uri=path.resolve().as_uri()+("?mode=ro" if readonly else "?mode=rw")
    conn=sqlite3.connect(uri,uri=True,isolation_level=None,timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    else:
        conn.execute("PRAGMA synchronous=FULL")
    return conn


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("inspect","prepare"))
    parser.add_argument("--db",type=Path,required=True)
    parser.add_argument("--expected-token")
    parser.add_argument("--confirm-workbench-stopped",action="store_true")
    args=parser.parse_args()
    if args.action=="prepare" and (not args.confirm_workbench_stopped or not args.expected_token):
        parser.error("prepare requires a stopped workbench, fresh inspection token, and explicit confirmation")
    try:
        with closing(open_database(args.db,readonly=args.action=="inspect")) as conn:
            result=inspect(conn) if args.action=="inspect" else prepare(conn,args.expected_token)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0 if (result["safe_for_legacy_scheduler"] if args.action=="inspect" else result["after"]["safe_for_legacy_scheduler"]) else 2
    except (GuardError,sqlite3.Error) as exc:
        print(json.dumps({"error":str(exc)},ensure_ascii=False))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
