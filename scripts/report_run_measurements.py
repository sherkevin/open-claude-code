#!/usr/bin/env python3
"""Summarize explicitly selected run trace JSON files, entirely offline."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from app.run_measurements import MeasurementError, build_report  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="+", type=Path, help="explicit /runs/{id}/trace JSON file list; no directory scan")
    parser.add_argument("--experiment-id", required=True, help="opaque identifier for externally controlled task/model/scope/permission cohort")
    parser.add_argument("--source-kind", required=True, choices=("synthetic", "customer"), help="operator-supplied provenance label, not automatically verified")
    parser.add_argument("--baseline-reference", help="optional opaque external baseline reference; never calculates savings")
    parser.add_argument("--output", type=Path, help="new report file (0600 permissions); omit to write JSON to stdout")
    args = parser.parse_args(argv)
    traces, fingerprints = [], []
    try:
        for index, path in enumerate(args.traces, 1):
            try:
                raw = path.read_bytes()
                trace = json.loads(raw)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raise MeasurementError(f"cannot read valid trace JSON at input position {index}") from None
            traces.append(trace)
            fingerprints.append({"input_position": index, "sha256": hashlib.sha256(raw).hexdigest()})
        report = build_report(traces, experiment_id=args.experiment_id, source_kind=args.source_kind,
                              baseline_reference=args.baseline_reference)
        report["input_fingerprints"] = fingerprints
        rendered = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            try:
                descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(rendered)
            except OSError:
                raise MeasurementError("cannot create new report file; output must not already exist") from None
        else:
            sys.stdout.write(rendered)
    except MeasurementError as exc:
        print(f"measurement error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
