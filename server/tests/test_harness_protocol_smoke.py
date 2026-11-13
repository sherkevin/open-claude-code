"""Offline SDK contract checks; no installed Harness runtime or model is needed."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify_harness_sdk_protocol.py"
SOURCE = ROOT / ".runtime/third_party/deepseek-harness"


def test_missing_source_fails_without_launching_runtime(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--source", str(tmp_path / "absent")],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["status"] == "FAIL"
    assert report["scope"] == "fake-runtime-only"
    assert report["real_runtime_started"] is False
    assert report["model_calls"] == 0


@pytest.mark.skipif(not (SOURCE / ".git").exists(), reason="Pinned upstream checkout is not present")
def test_upstream_sdk_against_fake_peer():
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT)], capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["scope"] == "fake-runtime-only"
    assert report["real_runtime_started"] is False
    assert report["model_calls"] == 0
    assert len(report["source"]["sdk_source_sha256"]) == 64
    assert len(report["checks"]) == 8
    assert all(check["status"] == "PASS" for check in report["checks"])


@pytest.mark.skipif(not (SOURCE / ".git").exists(), reason="Pinned upstream checkout is not present")
def test_unreviewed_upstream_commit_is_rejected():
    spec = importlib.util.spec_from_file_location("harness_protocol_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="reviewed pin"):
        module.source_identity(SOURCE, "0" * 40)
