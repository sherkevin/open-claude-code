#!/usr/bin/env python3
"""Build a read-only token savings report from exported run JSON.

Input is either a JSON list of run records or an object with a ``runs`` list.
The command never opens a database, contacts a provider, or rewrites input.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.token_savings import TokenAccounting


GROUP_FIELDS = ("user_id", "assistant_id", "model", "runtime")


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        records = value
    elif isinstance(value, Mapping) and isinstance(value.get("runs"), list):
        records = value["runs"]
    else:
        raise ValueError("input must be a JSON list or an object with a runs list")
    if any(not isinstance(item, Mapping) for item in records):
        raise ValueError("every run record must be a JSON object")
    return list(records)


def _group_key(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(record.get(field) or "unknown") for field in GROUP_FIELDS)


def _stage_records(record: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any] | None, str, bool, str]]:
    stages = record.get("stages")
    if isinstance(stages, list):
        for item in stages:
            if not isinstance(item, Mapping):
                continue
            stage = str(item.get("stage") or "answer")
            usage = item.get("usage")
            if not isinstance(usage, Mapping):
                usage = item.get("provider_usage")
            yield stage, usage if isinstance(usage, Mapping) else None, str(item.get("source") or "export"), bool(item.get("provider_call", True)), str(item.get("note") or "")
        return
    usage = record.get("usage")
    yield "answer", usage if isinstance(usage, Mapping) else None, "run_export", True, "legacy run usage"


def build_report(value: Any) -> dict[str, Any]:
    records = _records(value)
    grouped: dict[tuple[str, ...], TokenAccounting] = defaultdict(TokenAccounting)
    for record in records:
        ledger = grouped[_group_key(record)]
        for stage, usage, source, provider_call, note in _stage_records(record):
            ledger.record(stage, usage, source=source, provider_call=provider_call, note=note)

    groups = []
    for key in sorted(grouped):
        groups.append({
            **dict(zip(GROUP_FIELDS, key)),
            "accounting": grouped[key].summary(),
        })
    total = TokenAccounting()
    for record in records:
        for stage, usage, source, provider_call, note in _stage_records(record):
            total.record(stage, usage, source=source, provider_call=provider_call, note=note)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "record_count": len(records),
        "groups": groups,
        "total": total.summary(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON export containing runs")
    parser.add_argument("--output", type=Path, help="optional JSON output path")
    args = parser.parse_args(argv)
    report = build_report(json.loads(args.input.read_text(encoding="utf-8")))
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
