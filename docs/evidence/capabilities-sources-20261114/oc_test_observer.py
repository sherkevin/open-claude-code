"""External pytest observer: preserve failure details before session teardown."""

import json
import os
from pathlib import Path


def pytest_runtest_logreport(report):
    target = Path(os.environ["OC_TEST_REPORT_DIR"])
    target.mkdir(parents=True, exist_ok=True)
    with (target / "reports.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "duration": report.duration,
        }) + "\n")
    if report.failed:
        with (target / "failures.txt").open("a", encoding="utf-8") as stream:
            stream.write(f"\n=== {report.nodeid} [{report.when}] ===\n")
            stream.write(report.longreprtext + "\n")
