"""Optional real upstream process/recovery test, distinct from contract fakes."""
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not (ROOT / ".runtime/hipporag-env/installation.json").exists(),
                    reason="Optional isolated official runtime is absent")
def test_actual_official_recovery_timeout_and_cancel(tmp_path):
    result = subprocess.run(
        [sys.executable, "-B", str(ROOT / "scripts/verify_hipporag_recovery.py"), "--work-root", str(tmp_path)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
             "PYTHONPYCACHEPREFIX": str(tmp_path / "pycache")},
        capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "PASS"
    assert len(report["checks"]) == 6
    assert report["source_count"] == report["returned_count"] == 6
    assert report["unique_text_count"] == 5
    assert report["real_index_calls"] == 1
    assert report["worker_processes"] == 3
    assert report["all_workers_reaped"] is True
    assert report["external_model_calls"] == 0
