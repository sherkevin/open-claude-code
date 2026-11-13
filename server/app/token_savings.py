"""Auditable token accounting primitives for cost-saving experiments.

This module deliberately has no FastAPI, database, or provider dependency. It
is the small boundary shared by reports and runtime adapters. Provider usage is
kept separate from local estimates, and missing provider fields remain unknown
instead of being converted to zero.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
)
STAGES = frozenset({"answer", "compression", "routing", "retry", "tool"})


def _token_value(value: Any) -> int | None:
    """Return a valid provider count, preserving missing and invalid values."""

    return value if type(value) is int and value >= 0 else None


def normalize_provider_usage(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize one provider usage object without fabricating missing values."""

    if raw is None:
        return {
            **{field: None for field in TOKEN_FIELDS},
            "unknown_fields": list(TOKEN_FIELDS),
            "invalid_fields": [],
            "provided": False,
        }
    if not isinstance(raw, Mapping):
        return {
            **{field: None for field in TOKEN_FIELDS},
            "unknown_fields": list(TOKEN_FIELDS),
            "invalid_fields": ["usage_not_object"],
            "provided": True,
        }

    values: dict[str, int | None] = {}
    unknown: list[str] = []
    invalid: list[str] = []
    for field in TOKEN_FIELDS:
        if field not in raw:
            values[field] = None
            unknown.append(field)
            continue
        value = _token_value(raw[field])
        values[field] = value
        if value is None:
            unknown.append(field)
            if raw[field] is not None:
                invalid.append(field)
    return {**values, "unknown_fields": unknown, "invalid_fields": invalid, "provided": True}


@dataclass(frozen=True)
class TokenStage:
    """One provider-call stage. Values are provider-reported, not estimates."""

    stage: str
    source: str
    provider_call: bool
    usage: dict[str, Any]
    note: str = ""

    def view(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "source": self.source,
            "provider_call": self.provider_call,
            "provider_usage": {
                field: self.usage[field] for field in TOKEN_FIELDS
            }
            | {
                "unknown_fields": list(self.usage["unknown_fields"]),
                "invalid_fields": list(self.usage["invalid_fields"]),
            },
            "note": self.note,
        }


class TokenAccounting:
    """Serializable stage ledger for cost-saving comparisons.

    The ledger reports a known subtotal separately from provider totals. A
    provider total is ``None`` whenever any stage has an unknown contribution
    for that field, which prevents partial observability from looking like a
    complete bill.
    """

    def __init__(self) -> None:
        self._stages: list[TokenStage] = []
        self._warnings: list[str] = []

    def record(
        self,
        stage: str,
        usage: Mapping[str, Any] | None = None,
        *,
        source: str = "provider",
        provider_call: bool = True,
        note: str = "",
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown token accounting stage: {stage}")
        normalized = normalize_provider_usage(usage)
        if normalized["invalid_fields"]:
            self._warnings.append(
                f"{stage}: invalid provider usage fields kept unknown: "
                + ",".join(normalized["invalid_fields"])
            )
        if not normalized["provided"]:
            self._warnings.append(f"{stage}: provider usage not reported; kept unknown")
        entry = TokenStage(
            stage=stage,
            source=str(source),
            provider_call=bool(provider_call),
            usage=normalized,
            note=str(note),
        )
        self._stages.append(entry)
        return entry.view()

    def summary(self) -> dict[str, Any]:
        known_subtotals = {field: 0 for field in TOKEN_FIELDS}
        unknown_by_field = {field: False for field in TOKEN_FIELDS}
        unknown_stages: list[dict[str, Any]] = []
        provider_stages: list[str] = []

        for stage in self._stages:
            unknown = list(stage.usage["unknown_fields"])
            if unknown:
                unknown_stages.append({"stage": stage.stage, "fields": unknown})
                for field in unknown:
                    unknown_by_field[field] = True
            elif stage.provider_call:
                provider_stages.append(stage.stage)
            for field in TOKEN_FIELDS:
                value = stage.usage[field]
                if value is not None:
                    known_subtotals[field] += value

        provider_totals = {
            field: None if unknown_by_field[field] else known_subtotals[field]
            for field in TOKEN_FIELDS
        }
        cache_read = provider_totals["cache_read_input_tokens"]
        input_tokens = provider_totals["input_tokens"]
        cache_hit_ratio = None
        if (
            cache_read is not None
            and input_tokens is not None
            and input_tokens > 0
        ):
            cache_hit_ratio = round(cache_read / input_tokens, 6)

        return {
            "stages": [stage.view() for stage in self._stages],
            "provider_totals": provider_totals,
            "known_subtotals": known_subtotals,
            "provider_usage_complete": not unknown_stages,
            "unknown_stages": unknown_stages,
            "provider_stages_with_complete_usage": provider_stages,
            "cache_hit_ratio": cache_hit_ratio,
            "warnings": list(self._warnings),
            "measured_at": datetime.now(timezone.utc).isoformat(),
            "cost_is_authoritative": False,
        }

    def reset(self) -> None:
        self._stages.clear()
        self._warnings.clear()
