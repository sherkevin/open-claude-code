"""Two-stage usage ledger for optional context compression.

The ledger keeps provider-reported usage separate from local text estimates and
never turns an unknown provider number into zero.  Compression calls and answer
calls are recorded as distinct stages so a later product view can show them
side by side without pretending the compression stage is free.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


USAGE_FIELDS = ("input_tokens", "output_tokens")
KNOWN_STAGES = frozenset({"compression", "answer"})


def estimate_text_tokens(text: str) -> dict:
    """Deterministic, explicitly non-authoritative local estimate.

    Four characters per token is a sizing heuristic used only to compare
    synthetic inputs.  It is never reported as provider usage.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return {
        "characters": len(text),
        "estimated_tokens": (len(text) + 3) // 4,
        "method": "chars_div_4",
        "authoritative": False,
    }


def _known_int(value: Any) -> int | None:
    # bool is an int subclass; a boolean is never valid token usage.
    if type(value) is int and value >= 0:
        return value
    return None


def normalize_usage(raw: dict | None) -> dict:
    """Split provider usage into known integers and explicitly unknown fields."""
    if raw is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "unknown_fields": list(USAGE_FIELDS),
            "invalid_fields": [],
            "provided": False,
        }
    if not isinstance(raw, dict):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "unknown_fields": list(USAGE_FIELDS),
            "invalid_fields": ["usage_not_object"],
            "provided": True,
        }
    known: dict[str, int | None] = {}
    unknown: list[str] = []
    invalid: list[str] = []
    for field in USAGE_FIELDS:
        if field not in raw:
            unknown.append(field)
            known[field] = None
            continue
        value = _known_int(raw[field])
        known[field] = value
        if value is None:
            unknown.append(field)
            if raw[field] is not None:
                invalid.append(field)
    return {
        **known,
        "unknown_fields": unknown,
        "invalid_fields": invalid,
        "provided": True,
    }


@dataclass
class _Stage:
    name: str
    source: str
    provider_call: bool
    usage: dict
    local_estimate: dict | None
    note: str


class UsageLedger:
    """Small, serializable ledger; deliberately not a billing authority."""

    def __init__(self) -> None:
        self._stages: list[_Stage] = []
        self._warnings: list[str] = []

    def record(
        self,
        stage: str,
        usage: dict | None = None,
        *,
        source: str = "provider",
        provider_call: bool = True,
        local_estimate: dict | None = None,
        note: str = "",
    ) -> dict:
        if stage not in KNOWN_STAGES:
            raise ValueError(f"unknown usage stage: {stage}")
        normalized = normalize_usage(usage)
        if normalized["invalid_fields"]:
            self._warnings.append(
                f"{stage}: invalid provider usage fields kept unknown: "
                + ",".join(normalized["invalid_fields"])
            )
        if not normalized["provided"]:
            self._warnings.append(f"{stage}: provider usage not reported; kept unknown")
        if local_estimate is not None:
            if local_estimate.get("authoritative") is not False:
                raise ValueError("local estimates must declare authoritative=false")
            local_estimate = dict(local_estimate)
        entry = _Stage(
            name=stage,
            source=source,
            provider_call=provider_call,
            usage=normalized,
            local_estimate=local_estimate,
            note=note,
        )
        self._stages.append(entry)
        return self._stage_view(entry)

    @staticmethod
    def _stage_view(stage: _Stage) -> dict:
        return {
            "stage": stage.name,
            "source": stage.source,
            "provider_call": stage.provider_call,
            "provider_usage": {
                "input_tokens": stage.usage["input_tokens"],
                "output_tokens": stage.usage["output_tokens"],
                "unknown_fields": list(stage.usage["unknown_fields"]),
                "invalid_fields": list(stage.usage["invalid_fields"]),
            },
            "local_estimate": stage.local_estimate,
            "note": stage.note,
        }

    def summary(self) -> dict:
        known_totals = {"input_tokens": 0, "output_tokens": 0}
        unknown_by_field = {field: False for field in USAGE_FIELDS}
        unknown_stages: list[dict] = []
        provider_billed_stages: list[str] = []
        for stage in self._stages:
            if stage.usage["unknown_fields"]:
                unknown_stages.append({
                    "stage": stage.name,
                    "fields": list(stage.usage["unknown_fields"]),
                })
                for field in stage.usage["unknown_fields"]:
                    unknown_by_field[field] = True
            if stage.provider_call and not stage.usage["unknown_fields"]:
                provider_billed_stages.append(stage.name)
            for field in USAGE_FIELDS:
                value = stage.usage[field]
                if value is not None:
                    known_totals[field] += value
        # A field with any unknown contribution is reported as unknown, never
        # as zero.  The known subtotal stays separately visible.
        provider_totals = {
            field: None if unknown_by_field[field] else known_totals[field]
            for field in USAGE_FIELDS
        }
        return {
            "stages": [self._stage_view(stage) for stage in self._stages],
            "provider_totals": provider_totals,
            "known_subtotals": known_totals,
            "provider_usage_complete": not unknown_stages,
            "unknown_stages": unknown_stages,
            "provider_billed_stages": provider_billed_stages,
            "warnings": list(self._warnings),
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "estimate_is_authoritative": False,
        }

    def reset(self) -> None:
        self._stages.clear()
        self._warnings.clear()
