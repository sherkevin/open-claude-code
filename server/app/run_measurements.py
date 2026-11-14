"""Offline, privacy-minimal statistics for schema-version 1 run trace exports.

Only recorded server timings, provider token counts and business-cache flags are
read. No message, tool argument, error text or credential is copied to a report.
"""
from __future__ import annotations

import math
import re
from typing import Any


TIMING_FIELDS = (
    "first_server_progress_ms", "first_tool_attempt_ms", "first_tool_success_ms",
    "first_text_ms", "total_ms", "runtime_ready_ms", "first_model_event_ms",
)
TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens",
)
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")
NONTERMINAL_STATUSES = ("queued", "running", "awaiting_approval", "cancelling", "unknown")
CACHE_GROUPS = ("true", "false", "unknown", "mixed")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


class MeasurementError(ValueError):
    """Invalid measurement input. Error messages deliberately exclude input data."""


def _number(value: Any, *, integer: bool = False) -> int | float | None:
    if type(value) not in ((int,) if integer else (int, float)):
        return None
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return value


def nearest_rank(values: list[int | float], percentile: int) -> int | float | None:
    """Nearest rank: sorted_values[ceil(p / 100 * n) - 1], no interpolation."""
    if percentile < 1 or percentile > 100:
        raise ValueError("percentile must be between 1 and 100")
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered) / 100) - 1)]


def _distribution(values: list[int | float | None]) -> dict[str, Any]:
    known = [value for value in values if value is not None]
    return {
        "sample_count": len(values), "known_count": len(known),
        "missing_count": len(values) - len(known),
        "p50": nearest_rank(known, 50), "p95": nearest_rank(known, 95),
        "max": max(known) if known else None,
    }


def _cache_state(events: list[Any]) -> tuple[str, dict[str, int]]:
    # A missing flag remains unknown, including tool failures. A mix of known
    # and unknown tool completions is not advertised as an all-hit cache run.
    counts = {key: 0 for key in ("true", "false", "unknown")}
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "tool.completed":
            continue
        data = event.get("data")
        facts = data.get("result_facts") if isinstance(data, dict) else None
        value = (facts.get("cache_hit")
                 if isinstance(data, dict) and data.get("success") is True and isinstance(facts, dict)
                 else None)
        key = "true" if value is True else "false" if value is False else "unknown"
        counts[key] += 1
    present = [key for key, count in counts.items() if count]
    return (present[0] if len(present) == 1 else "mixed" if present else "unknown"), counts


def _tokens(usage: Any) -> dict[str, dict[str, int | None]]:
    usage = usage if isinstance(usage, dict) else {}
    ledger = usage.get("token_accounting")
    has_ledger = isinstance(ledger, dict)
    totals = ledger.get("provider_totals") if has_ledger else None
    subtotals = ledger.get("known_subtotals") if has_ledger else None
    has_stages = has_ledger and isinstance(ledger.get("stages"), list) and bool(ledger["stages"])
    result = {}
    for field in TOKEN_FIELDS:
        if has_ledger:
            # Do not fall back to an accumulated known subtotal when a ledger
            # explicitly says some provider calls did not report this field.
            # TokenAccounting starts its known subtotal at zero even when no
            # stage supplied a value. Require a real per-stage observation so
            # "never reported" is not mistaken for an explicit provider zero.
            reported = has_stages and any(
                isinstance(stage, dict) and isinstance(stage.get("provider_usage"), dict)
                and _number(stage["provider_usage"].get(field), integer=True) is not None
                for stage in ledger["stages"]
            )
            total = _number(totals.get(field), integer=True) if reported and isinstance(totals, dict) else None
            subtotal = _number(subtotals.get(field), integer=True) if reported and isinstance(subtotals, dict) else None
            if total is not None:
                subtotal = total
        else:
            raw = usage.get(field)
            if field == "cache_write_input_tokens":
                alias = usage.get("cache_creation_input_tokens")
                if raw is None:
                    raw = alias
                elif alias is not None and alias != raw:
                    raw = None
            # Legacy flat snapshots do not demonstrate that every provider
            # call contributed its usage. Preserve observations as subtotals.
            total = None
            subtotal = _number(raw, integer=True)
        result[field] = {"total": total, "known_subtotal": subtotal}
    return result


def _project(trace: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(trace, dict) or type(trace.get("schema_version")) is not int or trace["schema_version"] != 1:
        raise MeasurementError("trace must have integer schema_version 1")
    run, events = trace.get("run"), trace.get("events")
    if not isinstance(run, dict) or not isinstance(events, list):
        raise MeasurementError("trace requires run object and events array")
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise MeasurementError("trace requires a nonempty run id")
    if any(isinstance(event, dict) and "run_id" in event and event["run_id"] != run_id for event in events):
        raise MeasurementError("trace contains an event from a different run")
    raw_status = run.get("status")
    status = raw_status if raw_status in TERMINAL_STATUSES + NONTERMINAL_STATUSES else "unknown"
    raw_timings = run.get("timings")
    raw_timings = raw_timings if isinstance(raw_timings, dict) else {}
    timings = {field: _number(raw_timings.get(field)) for field in TIMING_FIELDS}
    # duration_ms on an unfinished export is only elapsed age, not latency.
    # Never derive final duration or front-end feedback from it or from events.
    if status not in TERMINAL_STATUSES:
        timings["total_ms"] = None
    cache, observations = _cache_state(events)
    tokens = _tokens(trace.get("usage"))
    if status != "succeeded":
        # Failed, cancelled and interrupted runs may omit the last provider
        # usage event just like unfinished runs. Keep only their known subtotal.
        # A succeeded run's reported total is still not a settlement invoice.
        for field in TOKEN_FIELDS:
            tokens[field]["total"] = None
    return run_id, {
        "status": status, "terminal": status in TERMINAL_STATUSES,
        "timings": timings, "tokens": tokens,
        "business_cache_state": cache, "business_cache_observations": observations,
    }


def _group(samples: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = {}
    for field in TOKEN_FIELDS:
        values = [sample["tokens"][field]["total"] for sample in samples]
        known_subtotals = [sample["tokens"][field]["known_subtotal"] for sample in samples]
        distribution = _distribution(values)
        distribution.update(
            known_subtotal=(sum(value for value in known_subtotals if value is not None)
                            if any(value is not None for value in known_subtotals) else None),
            subtotal_reporting_count=sum(value is not None for value in known_subtotals),
            complete_total=sum(values) if values and all(value is not None for value in values) else None,
        )
        tokens[field] = distribution
    return {
        "run_count": len(samples),
        "timings_ms": {field: _distribution([sample["timings"][field] for sample in samples]) for field in TIMING_FIELDS},
        "provider_tokens": tokens,
    }


def build_report(traces: list[Any], *, experiment_id: str, source_kind: str,
                 baseline_reference: str | None = None) -> dict[str, Any]:
    """Aggregate explicit traces; experiment/baseline labels are user assertions.

    This function does not verify equivalence of tasks, models, scopes or roles.
    It intentionally calculates no speedup, token saving or business success.
    """
    if not isinstance(experiment_id, str) or not IDENTIFIER.fullmatch(experiment_id):
        raise MeasurementError("experiment id must be a 1-96 character opaque identifier")
    if source_kind not in {"synthetic", "customer"}:
        raise MeasurementError("source kind must be synthetic or customer")
    if baseline_reference is not None and (not isinstance(baseline_reference, str) or not IDENTIFIER.fullmatch(baseline_reference)):
        raise MeasurementError("baseline reference must be a 1-96 character opaque identifier")
    if not traces:
        raise MeasurementError("at least one explicit trace is required")
    seen, samples = set(), []
    for trace in traces:
        run_id, sample = _project(trace)
        if run_id in seen:
            raise MeasurementError("duplicate run id; each run may contribute only once")
        seen.add(run_id)
        samples.append(sample)
    count = len(samples)
    by_status = {status: _group([sample for sample in samples if sample["status"] == status])
                 for status in TERMINAL_STATUSES + NONTERMINAL_STATUSES}
    return {
        "schema_version": "run-measurements/v1", "experiment_id": experiment_id,
        "source_kind": source_kind, "source_kind_verified": False,
        "percentile_method": "nearest_rank", "percentiles_use_known_values_only": True,
        "input_run_count": count,
        "status_denominator": {
            "all_runs": count,
            "terminal_runs": sum(sample["terminal"] for sample in samples),
            "nonterminal_runs": sum(not sample["terminal"] for sample in samples),
            "by_status": {status: {"count": group["run_count"], "fraction_of_all_runs": group["run_count"] / count}
                          for status, group in by_status.items()},
        },
        "all_runs": _group(samples), "by_status": by_status,
        "by_business_cache": {state: _group([sample for sample in samples if sample["business_cache_state"] == state])
                              for state in CACHE_GROUPS},
        "business_cache_observations": {key: sum(sample["business_cache_observations"][key] for sample in samples)
                                        for key in ("true", "false", "unknown")},
        "baseline": {"status": "external_reference_unverified" if baseline_reference else "not_supplied",
                     "reference": baseline_reference, "savings": None},
        "limitations": [
            "Labels are supplied by the operator; same task, model, data scope and permissions require external experiment evidence.",
            "Server progress is not browser feedback; successful tool completion is not a validated business result.",
            "Only recorded allowed timing fields are used; legacy first_feedback/structure/valid and duration_ms are not substituted.",
            "Failure, cancellation, interruption and unfinished runs remain in denominators; runtime success is not business correctness.",
            "Business cache states require successful tool completion; mixed includes known and unknown flags. No tool observations means unknown.",
            "Provider cache token counts are separate from business cache hits. Known token subtotals are not a complete bill.",
            "Failed, cancelled, interrupted and nonterminal runs contribute known subtotals only; complete reported totals remain unknown.",
            "Succeeded runs with complete reported fields may contribute reported-usage totals; these are not provider settlement invoices.",
            "Legacy flat usage without a stage ledger contributes known subtotals only; completeness of all provider calls is unverified.",
            "No baseline comparison, token savings, ROI or customer acceptance is inferred.",
        ],
    }
