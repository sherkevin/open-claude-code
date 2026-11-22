"""Account-level usage: recorded model receipts, never guessed prices or tokens."""
from __future__ import annotations

import asyncio
import json
import math
from collections import defaultdict
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import dump, now, offload_write, uid
from .security import AppError

usage_owner = ContextVar("usage_owner", default=None)
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
FIELDS = (*TOKEN_FIELDS, "estimated_cost_usd")
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _number(value, field):
    if field == "estimated_cost_usd":
        return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e15
    return type(value) is int and 0 <= value <= 10**15


def _object(value):
    try:
        value = json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _openai_receipt(reported):
    """Normalize total prompt counts to the ledger's disjoint input buckets.

    Standard Chat Completions reports cache reads inside prompt_tokens. Do not
    mix that aggregate with Anthropic/Harness uncached input. A provider which
    additionally declares cache_creation_input_tokens puts those writes inside
    its prompt total too. Missing read counters leave uncached input unknown;
    absent nonstandard write counters remain unknown, never a fabricated zero.
    """
    valid = lambda value: value if _number(value, "input_tokens") else None
    total = valid(reported.get("prompt_tokens"))
    read = valid(_object(reported.get("prompt_tokens_details")).get("cached_tokens"))
    write = valid(reported.get("cache_creation_input_tokens"))
    has_write = "cache_creation_input_tokens" in reported
    uncached = None
    if total is not None and read is not None and (not has_write or write is not None):
        cached = read + (write if has_write else 0)
        if cached <= total:
            uncached = total - cached
        else:
            # Internally inconsistent counters cannot be treated as measured
            # disjoint buckets; retain the aggregate separately for audit.
            read = write = None
    return {"input_tokens": uncached, "output_tokens": valid(reported.get("completion_tokens")),
            "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
            "reported_prompt_tokens": total}


async def metered_post(http, endpoint, *, service, kind, selection, payload, user_id=None, usage_protocol="anthropic"):
    """Record only actual HTTP attempts; cached/joined followups never enter here.

    Begin before sending so cancellation/crash still leaves an unknown receipt.
    Store only public model identity and numeric usage, never drafts or routes.
    """
    if usage_protocol not in {"anthropic", "openai"}:
        raise ValueError("Unsupported usage protocol")
    owner = user_id or usage_owner.get()
    call_id = uid() if owner else None
    if call_id:
        try:
            await offload_write(service.db.execute,
                "INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)",
                (call_id, owner, kind, selection.get("model_id", "legacy-model"), selection.get("model_label", "历史模型"), "started", None, now(), None))
        except asyncio.CancelledError:
            # offload_write settles a durable insert before propagating normal
            # cancellation. No HTTP method has been entered at this point.
            # A process crash/second cancellation may still leave 'started';
            # that incomplete record is explicitly unknown, not one billed call.
            await offload_write(service.db.execute,
                "UPDATE model_usage_calls SET status='not_sent',usage=?,completed_at=? WHERE id=?",
                (dump({field: 0 for field in FIELDS}), now(), call_id))
            raise
    receipt, status = None, "failed"
    try:
        response = await http.post(endpoint, json=payload)
        status = "succeeded" if 200 <= response.status_code < 300 else "failed"
        try:
            reported = _object(response.json()).get("usage")
            if isinstance(reported, dict):
                if usage_protocol == "openai":
                    reported = _openai_receipt(reported)
                receipt = {field: reported[field] if _number(reported.get(field), field) else None for field in TOKEN_FIELDS}
                if usage_protocol == "openai":
                    receipt["reported_prompt_tokens"] = reported["reported_prompt_tokens"]
                # Provider token counters are not a price table or an invoice.
                receipt["estimated_cost_usd"] = None
        except (TypeError, ValueError, AttributeError):
            pass
        return response
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        if call_id:
            await offload_write(service.db.execute,
                "UPDATE model_usage_calls SET status=?,usage=?,completed_at=? WHERE id=?",
                (status, dump(receipt) if receipt is not None else None, now(), call_id))


def usage_entry(row, *, auxiliary=False):
    usage = _object(row.get("usage"))
    metadata = _object(row.get("execution_meta"))
    selection = _object(metadata.get("selection"))
    ledger = _object(usage.get("token_accounting"))
    totals = _object(ledger.get("provider_totals"))
    subtotals = _object(ledger.get("known_subtotals"))
    stages = ledger.get("stages")
    http_capture = ledger.get("receipt_schema") == "harness-http-usage/v1"
    verified_empty = http_capture and ledger.get("capture_complete") is True and stages == [] and ledger.get("known_model_calls") == 0
    product = metadata.get("execution_kind") == "product_metadata" or usage.get("source") == "product_metadata"
    amounts, known = {}, {}
    for field in FIELDS:
        ledger_field = "cache_write_input_tokens" if field == "cache_creation_input_tokens" else field
        amount = totals.get(ledger_field) if field in TOKEN_FIELDS and ledger else usage.get(field)
        # A zero-initialized, empty ledger is not a measured provider receipt.
        if ledger and stages == [] and not product and not verified_empty:
            amount = None
        if product:
            amount = 0
        amounts[field] = amount if _number(amount, field) else None
        part = subtotals.get(ledger_field) if field in TOKEN_FIELDS else None
        known[field] = amounts[field] if amounts[field] is not None else part if _number(part, field) else 0
    if auxiliary:
        calls = (0 if row.get("status") == "not_sent" else 1
                 if row.get("status") in {"succeeded", "failed", "cancelled"} else None)
    elif product:
        calls = 0
    elif http_capture:
        valid_stages = isinstance(stages, list) and all(isinstance(stage, dict) and stage.get("provider_call") is True and stage.get("source") == "harness_provider" for stage in stages)
        calls = len(stages) if valid_stages and ledger.get("capture_complete") is True else None
    elif isinstance(stages, list) and stages:
        # Harness commits one stage per actual attempt. Legacy SDK result
        # stages can aggregate several requests, so their call count is unknown.
        calls = len(stages) if all(isinstance(stage, dict) and stage.get("provider_call") is True and stage.get("source") == "harness_provider" for stage in stages) else None
    else:
        calls = None
    buckets = [amounts[field] for field in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    reported = usage.get("total_input_tokens") if http_capture else usage.get("reported_prompt_tokens")
    total_input = reported if _number(reported, "input_tokens") else sum(buckets) if all(value is not None for value in buckets) else None
    if total_input is not None and (sum(value for value in buckets if value is not None) > total_input
            or all(value is not None for value in buckets) and sum(buckets) != total_input):
        total_input = None
    if product:
        total_input = 0
    partial_total = usage.get("known_total_input_tokens")
    known_total = total_input if total_input is not None else partial_total if http_capture and _number(partial_total, "input_tokens") else 0
    known_calls = calls if calls is not None else len(stages) if http_capture and isinstance(stages, list) else 0
    return {"id": row["id"], "user_id": row["user_id"], "display_name": row.get("display_name", ""),
            "model_id": row.get("model_id", "legacy-model") if auxiliary else selection.get("model_id", "legacy-model"),
            "model_label": row.get("model_label", "历史模型") if auxiliary else selection.get("model_label", "历史模型"),
            "day": datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).astimezone(REPORT_TIMEZONE).date().isoformat(), "status": row["status"], "auxiliary": auxiliary,
            "kind": row.get("kind", "chat"), "amounts": amounts, "known": known,
            "reported": bool(usage) or product, "call_count": calls, "known_call_count": known_calls,
            "total_input_tokens": total_input, "known_total_input_tokens": known_total}


def _aggregate(items):
    count = len(items)
    known = {field: sum(item["known"][field] for item in items) for field in FIELDS}
    coverage = {field: sum(item["amounts"][field] is not None for item in items) for field in FIELDS}
    call_known = sum(item["known_call_count"] for item in items)
    call_unknown = sum(item["call_count"] is None for item in items)
    total_input_known = sum(item["total_input_tokens"] is not None for item in items)
    total_input_subtotal = sum(item["known_total_input_tokens"] for item in items)
    return {"total_runs": sum(not item["auxiliary"] for item in items),
            "successful_runs": sum(not item["auxiliary"] and item["status"] == "succeeded" for item in items),
            "usage_records": count, "auxiliary_calls": sum(item["call_count"] or 0 for item in items if item["auxiliary"]),
            "model_call_count": call_known if not call_unknown else None,
            "known_model_calls": call_known, "unknown_call_records": call_unknown,
            "total_input_tokens": total_input_subtotal if total_input_known == count else None,
            "known_total_input_tokens": total_input_subtotal,
            "total_input_coverage": {"known_runs": total_input_known, "unknown_runs": count - total_input_known},
            **{field: known[field] if coverage[field] == count and (count or field != "estimated_cost_usd") else None for field in FIELDS},
            "known_subtotals": known,
            "field_coverage": {field: {"known_runs": coverage[field], "unknown_runs": count - coverage[field]} for field in FIELDS},
            "reported_usage_runs": sum(item["reported"] for item in items),
            "missing_usage_runs": sum(not item["reported"] for item in items),
            "usage_complete": bool(items) and all(coverage[field] == count for field in TOKEN_FIELDS), "currency": "USD"}


def usage_summary(service, user, *, scope="self", model_id=None, date_from=None, date_to=None):
    if scope not in {"self", "all"}:
        raise AppError(422, "USAGE_SCOPE_INVALID", "请选择个人或全体成员范围")
    if scope == "all" and user["role"] != "admin":
        raise AppError(403, "ADMIN_REQUIRED", "只有管理员可以查看全体成员用量")
    try:
        lower = date.fromisoformat(date_from) if date_from else None
        upper = date.fromisoformat(date_to) if date_to else None
        if (lower and date_from != lower.isoformat()) or (upper and date_to != upper.isoformat()) or (lower and upper and lower > upper):
            raise ValueError()
    except (ValueError, TypeError):
        raise AppError(422, "USAGE_DATE_INVALID", "请选择有效的起止日期，开始日期不能晚于结束日期")
    where, values = [], []
    if scope == "self":
        where.append("t.user_id=?"); values.append(user["id"])
    try:
        if lower:
            where.append("t.created_at>=?"); values.append(datetime.combine(lower, time.min, REPORT_TIMEZONE).astimezone(timezone.utc).isoformat())
        if upper:
            where.append("t.created_at<?"); values.append(datetime.combine(upper + timedelta(days=1), time.min, REPORT_TIMEZONE).astimezone(timezone.utc).isoformat())
    except (ValueError, OverflowError):
        raise AppError(422, "USAGE_DATE_INVALID", "日期超出可统计范围")
    predicate = " WHERE " + " AND ".join(where) if where else ""
    entries = []
    for table, auxiliary in (("runs", False), ("model_usage_calls", True)):
        rows = service.db.all(f"SELECT t.*,users.display_name FROM {table} t JOIN users ON users.id=t.user_id" + predicate, tuple(values))
        entries.extend(usage_entry(row, auxiliary=auxiliary) for row in rows)
    available = {item["model_id"]: item["model_label"] for item in entries}
    if model_id:
        entries = [item for item in entries if item["model_id"] == model_id]
    result = _aggregate(entries)
    groups = {}
    for field, output in (("model_id", "by_model"), ("day", "by_day"), ("kind", "by_kind")):
        grouped = defaultdict(list)
        for item in entries:
            grouped[item[field]].append(item)
        groups[output] = [{field: key, **({"model_label": items[0]["model_label"]} if field == "model_id" else {}), **_aggregate(items)} for key, items in sorted(grouped.items())]
    users = defaultdict(list)
    if scope == "all":
        for item in entries:
            users[item["user_id"]].append(item)
    result.update(groups)
    result.update(scope=scope, timezone="Asia/Shanghai", date_from=date_from, date_to=date_to, model_id=model_id,
                  available_models=[{"id": key, "label": value} for key, value in sorted(available.items())],
                  users=[{"user_id": key, "display_name": items[0]["display_name"], **_aggregate(items)} for key, items in sorted(users.items())],
                  auxiliary_history_note="Tab补全、选模、后续问题与知识图片解析从各自启用用量记录后开始统计；此前的独立调用没有历史账本，不能追补。",
                  cost_note="仅展示服务已报告的估算值；未报告价格或费用时不换算金额，不代表实际账单。")
    return result
