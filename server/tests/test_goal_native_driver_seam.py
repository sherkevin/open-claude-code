"""B2.3 isolated probe: real pinned Goal round driver + synthetic host lease.

The probe mounts the official upstream ``dsh-goal-round-driver``, ``dsh-goal``,
official session projection/checkpoint service and ``agent-loop-testkit`` from
the pinned checkout, with an in-process synthetic LLM adapter and a synthetic
host lease. It never starts the product, a real model, a network client, or any
customer/business data path.

Run with the pinned toolchain (temporary paths under data3):

    cd /media/data3/dengkw/projects/kaiwu-goal-native-driver-seam-20261126/server
    TMPDIR=/media/data3/dengkw/acceptance/kaiwu-goal-native-driver-seam-20261126/pytest \
    HARNESS_TEST_NODE=/media/data3/dengkw/projects/open-claude-code/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node \
    HARNESS_TEST_SOURCE=/media/data3/dengkw/projects/open-claude-code/.runtime/third_party/deepseek-harness \
    python -m pytest tests/test_goal_native_driver_seam.py -q
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
PROBE = REPOSITORY / "scripts/oss/harness_goal_native_driver_seam_probe.mts"
PIN = "fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"
SCENARIOS = {
    "unauthorized-positive-round-is-refused",
    "single-authorized-round-then-host-cap",
    "resume-revision-binding",
    "unknown-usage-blocks-automatic-continuation",
    "no-replay-after-driver-rebuild",
}


def _pinned_runtime():
    """Locate the reviewed Node/tsx/source without inheriting any credentials."""
    node = Path(os.getenv(
        "HARNESS_TEST_NODE",
        str(REPOSITORY / ".runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node"),
    ))
    source = Path(os.getenv(
        "HARNESS_TEST_SOURCE",
        str(REPOSITORY / ".runtime/third_party/deepseek-harness"),
    ))
    tsx = source / "node_modules/tsx/dist/esm/index.mjs"
    if not node.is_file() or not tsx.is_file() or not PROBE.is_file():
        pytest.skip("requires the pinned Node Harness checkout and its tsx resolver")
    return node, source, tsx


def _clean_environment(tmp_path: Path, source: Path) -> dict[str, str]:
    """Drop ambient credentials and keep every temporary path under the test root."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "TSX_TSCONFIG_PATH": str(source / "tsconfig.json"),
        "NO_COLOR": "1",
    }


def test_real_pinned_goal_round_driver_host_lease_seam(tmp_path):
    node, source, tsx = _pinned_runtime()
    work = tmp_path / "goal-driver-seam"
    work.mkdir()
    result = subprocess.run(
        [str(node), "--import", str(tsx), str(PROBE), "--source", str(source), "--work-dir", str(work)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
        env=_clean_environment(tmp_path, source),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True, report
    assert report["sourceCommit"] == PIN
    assert report["paidModelCalls"] == 0
    assert report["network"] == "none: in-process synthetic adapter only"
    assert report["usageStatus"] == "unknown"
    assert report["settledCostMicros"] is None
    assert {scenario["name"] for scenario in report["scenarios"]} == SCENARIOS
    assert all(scenario["ok"] for scenario in report["scenarios"]), report["scenarios"]

    # The host round record is written before the step enters history; the
    # official session log remains the source of native roundsStarted.
    checkpoint = json.loads((work / "single-authorized-round-checkpoint.json").read_text())
    assert checkpoint["schema"] == "host-goal-round-checkpoint/v1"
    assert checkpoint["usageStatus"] == "unknown"
    assert checkpoint["settledCostMicros"] is None
    assert [round_["roundIndex"] for round_ in checkpoint["rounds"]] == [1]
    assert checkpoint["rounds"][0]["goalRevision"] == 1
    assert any(rejection["code"] == "GOAL_ROUND_LIMIT" for rejection in checkpoint["rejections"])
