"""Host-only capped attempt ledger for a closed, explicitly injected loopback provider.

Public request bodies can specify only the round ceiling. The test provider
defines an enforceable upper bound per request, including children and retries.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .security import AppError

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


@dataclass(frozen=True)
class LoopbackGoalMeter:
    endpoint: str
    parent: tuple[int, ...]
    child: tuple[int, ...] = ()
    retry: tuple[int, ...] = ()


def _unpriced():
    return AppError(409, "GOAL_ROUND_PRICE_UNAVAILABLE",
                    "当前模型没有可核验的单轮费用上限，无法覆盖主任务、子任务及重试；本轮未放行。")


def _test_meter(service, conversation_id):
    meter = getattr(service, "goal_round_meter", None)
    if not service.settings.provider_test_mode or type(meter) is not LoopbackGoalMeter:
        raise _unpriced()
    try:
        env = service.runtime_config(conversation_id, provider_only=True)["provider_env"]
        base = urlsplit(env.get("ANTHROPIC_BASE_URL", ""))
        if (base.scheme != "http" or base.hostname not in {"127.0.0.1", "::1"}
                or base.username is not None or base.password is not None or base.query
                or base.fragment or not base.port):
            raise _unpriced()
        # Bind exactly the native Anthropic request path. No DNS or off-host route.
        root = base.geturl().rstrip("/")
        endpoint = root + ("" if base.path.rstrip("/").endswith("/v1") else "/v1") + "/messages"
        if endpoint != meter.endpoint:
            raise _unpriced()
    except (AppError, TypeError, ValueError, KeyError):
        raise _unpriced() from None
    groups = (meter.parent, meter.child, meter.retry)
    if (not all(type(group) is tuple for group in groups) or not meter.parent
            or sum(map(len, groups)) > 128
            or any(type(value) is not int or not 0 <= value <= 10**12
                   for group in groups for value in group)):
        raise _unpriced()
    return meter


def _slots(meter):
    return [(kind, value) for kind in ("parent", "child", "retry")
            for value in getattr(meter, kind)]


def price_round(service, authorization, cost_used):
    cap = authorization.get("round_cost_budget_micros")
    if cap is None:
        return None
    meter = _test_meter(service, authorization["conversation_id"])
    required = sum(value for _, value in _slots(meter))
    if cost_used is None:
        raise AppError(409, "GOAL_USAGE_UNVERIFIED", "前序轮次用量不完整，须先核验剩余预算")
    if required > min(cap, authorization["cost_budget_micros"] - cost_used):
        raise AppError(409, "GOAL_ROUND_RESERVE_EXCEEDED",
                       "覆盖主任务、子任务及重试所需的预留费用超过单轮上限或剩余总预算。")
    return meter


def reserve_round_locked(service, run_id, meter):
    if meter is not None:
        service.db.executemany(
            "INSERT INTO goal_round_cost_attempts(run_id,slot,kind,reserved_micros,endpoint) "
            "VALUES(?,?,?,?,?)", [(run_id, slot, kind, value, meter.endpoint)
                                  for slot, (kind, value) in enumerate(_slots(meter), 1)])


def _entries(service, run_id):
    return service.db.all("SELECT * FROM goal_round_cost_attempts WHERE run_id=? ORDER BY slot",
                          (run_id,))


def reservation_view(service, run_id):
    rows = _entries(service, run_id)
    if not rows:
        return None
    fields = ("slot", "kind", "reserved_micros", "attempt_id", "child_id", "retry_of",
              "state", "settled_micros")
    known = all(row["state"] in ("settled", "unused") for row in rows)
    return {"source": "loopback_test", "reserved_micros": sum(r["reserved_micros"] for r in rows),
            "settled_micros": sum(r["settled_micros"] for r in rows if r["state"] == "settled")
            if known else None,
            "attempts": [{**{k: r[k] for k in fields}, "parent_attempt_id": r["retry_of"]} for r in rows]}


def goal_attempt_endpoint(service, run_id):
    lease = service.db.one(
        "SELECT l.conversation_id,a.round_cost_budget_micros FROM goal_execution_leases l "
        "JOIN goal_execution_authorizations a ON a.id=l.authorization_id WHERE l.run_id=?", (run_id,))
    if lease is None:
        # Missing storage for a continuation must never select the unmetered transport.
        run = service.db.one("SELECT execution_meta FROM runs WHERE id=?", (run_id,))
        if run and json.loads(run["execution_meta"] or "{}").get("goal_continuation") is True:
            raise AppError(409, "GOAL_ROUND_NOT_RESERVED", "当前持续目标未绑定预留轮次")
        return None
    if lease["round_cost_budget_micros"] is None:
        return None
    meter = _test_meter(service, lease["conversation_id"])
    rows = _entries(service, run_id)
    if ([(r["kind"], r["reserved_micros"]) for r in rows] != _slots(meter)
            or any(r["endpoint"] != meter.endpoint for r in rows)):
        raise _unpriced()
    return meter.endpoint


def _require_meter(service, run_id):
    if not goal_attempt_endpoint(service, run_id):
        raise _unpriced()


def begin_goal_attempt(service, run_id, attempt_id, kind, *, child_id=None, retry_of=None):
    """Return the reserved upper bound once. A repeated begin cannot send again."""
    if (not isinstance(attempt_id, str) or not UUID.fullmatch(attempt_id)
            or kind not in ("parent", "child", "retry")
            or (kind == "retry") != (retry_of is not None)
            or (kind == "child" and child_id is None) or (kind == "parent" and child_id is not None)):
        raise AppError(409, "GOAL_ATTEMPT_INVALID", "请求与已预留的调用类型不符")
    with service.db.transaction():
        _require_meter(service, run_id)
        lease = service.db.one(
            "SELECT l.*,a.status AS approval_state,c.active_run_id,r.status AS run_state "
            "FROM goal_execution_leases l JOIN goal_execution_authorizations a ON a.id=l.authorization_id "
            "JOIN conversations c ON c.id=l.conversation_id JOIN runs r ON r.id=l.run_id WHERE l.run_id=?",
            (run_id,))
        if (lease["state"] != "admitted" or not lease["armed_at"] or lease["approval_state"] != "active"
                or lease["active_run_id"] != run_id or lease["run_state"] != "running"):
            raise AppError(409, "GOAL_LEASE_INACTIVE", "本轮授权已失效，不能发起请求")
        from .goal_execution import require_goal_lease
        require_goal_lease(service, lease["id"], run_id)
        if child_id is not None and not service.db.one(
                "SELECT 1 FROM subagent_children WHERE child_id=? AND parent_run_id=? "
                "AND user_id=? AND conversation_id=? AND status='running'",
                (child_id, run_id, lease["user_id"], lease["conversation_id"])):
            raise AppError(409, "GOAL_ATTEMPT_INVALID", "子任务未绑定到当前轮次")
        if retry_of is not None and not service.db.one(
                "SELECT 1 FROM goal_round_cost_attempts WHERE run_id=? AND attempt_id=? "
                "AND child_id IS ? AND retryable=1 AND state IN ('started','settled')",
                (run_id, retry_of, child_id)):
            raise AppError(409, "GOAL_ATTEMPT_INVALID", "重试未绑定到同一任务的失败请求")
        if service.db.one("SELECT 1 FROM goal_round_cost_attempts WHERE attempt_id=? "
                          "OR (run_id=? AND retry_of=?)", (attempt_id, run_id, retry_of)):
            raise AppError(409, "GOAL_ATTEMPT_REPLAY", "此请求或重试已提交，禁止重复执行")
        row = service.db.one(
            "SELECT slot,reserved_micros FROM goal_round_cost_attempts WHERE run_id=? "
            "AND kind=? AND state='reserved' ORDER BY slot LIMIT 1", (run_id, kind))
        if row is None:
            raise AppError(409, "GOAL_ATTEMPT_NOT_RESERVED", "该类调用已无预留额度，本次请求未发出")
        service.db.execute("UPDATE goal_round_cost_attempts SET state='started',attempt_id=?,child_id=?,"
                           "retry_of=? WHERE run_id=? AND slot=?",
                           (attempt_id, child_id, retry_of, run_id, row["slot"]))
        return row["reserved_micros"]


def mark_goal_attempt_retryable(service, run_id, attempt_id):
    """Record only transport-observed retryable failure, independently of cost."""
    with service.db.transaction():
        _require_meter(service, run_id)
        changed = service.db.execute(
            "UPDATE goal_round_cost_attempts SET retryable=1 WHERE run_id=? AND attempt_id=? "
            "AND state IN ('started','settled')", (run_id, attempt_id))
        if changed.rowcount != 1:
            raise AppError(409, "GOAL_ATTEMPT_INVALID", "失败回执未绑定到已发出的请求")


def settle_goal_attempt(service, run_id, attempt_id, cost_micros, *, retryable=False):
    """A missing/inconsistent bill leaves the started request explicitly unknown."""
    with service.db.transaction():
        _require_meter(service, run_id)
        row = service.db.one("SELECT * FROM goal_round_cost_attempts WHERE run_id=? AND attempt_id=?",
                             (run_id, attempt_id))
        if (not row or type(cost_micros) is not int or not 0 <= cost_micros <= row["reserved_micros"]
                or row["state"] not in ("started", "settled")
                or (row["state"] == "settled" and row["settled_micros"] != cost_micros)):
            raise AppError(409, "GOAL_ATTEMPT_RECEIPT_INVALID", "费用回执不一致或超过该笔预留")
        service.db.execute("UPDATE goal_round_cost_attempts SET state='settled',settled_micros=?,"
                           "retryable=MAX(retryable,?) WHERE run_id=? AND attempt_id=?",
                           (cost_micros, int(retryable is True), run_id, attempt_id))


def release_unused_locked(service, run_id):
    # Only untouched slots can be released. Started, unacknowledged calls remain unknown.
    service.db.execute("UPDATE goal_round_cost_attempts SET state='unused' "
                       "WHERE run_id=? AND state='reserved'", (run_id,))


def _attempt_tokens(stage):
    usage = stage.get("provider_usage")
    if not isinstance(usage, dict) or usage.get("invalid_fields"):
        return None
    output = usage.get("output_tokens")
    prompt = stage.get("reported_prompt_tokens")
    buckets = [usage.get(k) for k in ("input_tokens", "cache_read_input_tokens", "cache_write_input_tokens")]
    def valid(x):
        return type(x) is int and 0 <= x <= 10**15
    if not valid(output) or any(x is not None and not valid(x) for x in buckets):
        return None
    if prompt is None:
        return sum(buckets) + output if all(valid(x) for x in buckets) else None
    known = sum(x for x in buckets if x is not None)
    if not valid(prompt) or known > prompt or (None not in buckets and known != prompt):
        return None
    return prompt + output


def _receipt_tokens(raw, expected):
    try:
        receipt = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    ledger = receipt.get("token_accounting") if isinstance(receipt, dict) else None
    if (not isinstance(ledger, dict) or ledger.get("receipt_schema") != "harness-http-usage/v1"
            or ledger.get("capture_complete") is not True or receipt.get("capture_complete") is False
            or not isinstance(ledger.get("stages"), list)):
        return None
    remaining = set(expected)
    total = 0
    for stage in ledger["stages"]:
        if not isinstance(stage, dict) or stage.get("provider_call") is not True:
            return None
        attempt = stage.get("attempt_id")
        if not isinstance(attempt, str) or attempt not in remaining or stage.get("source") != "harness_provider":
            return None
        count = _attempt_tokens(stage)
        if count is None:
            return None
        remaining.remove(attempt)
        total += count
    return None if remaining else total


def round_tokens(service, run, reservation):
    """Require one complete official token receipt per billed HTTP attempt, in its own session."""
    by_child = {}
    for attempt in reservation["attempts"]:
        if attempt["state"] in ("settled", "started"):
            by_child.setdefault(attempt["child_id"], set()).add(attempt["attempt_id"])
    if None not in by_child:
        return None
    total = _receipt_tokens(run.get("usage"), by_child.pop(None))
    if total is None:
        return None
    for child_id, ids in by_child.items():
        row = service.db.one(
            "SELECT u.usage FROM model_usage_calls u JOIN subagent_children c ON c.child_id=u.id "
            "WHERE c.child_id=? AND c.parent_run_id=? AND c.user_id=u.user_id AND u.kind='subagent' "
            "AND u.completed_at IS NOT NULL", (child_id, run["run_id"]))
        count = _receipt_tokens(row["usage"], ids) if row else None
        if count is None:
            return None
        total += count
    return total
