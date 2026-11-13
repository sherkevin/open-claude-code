"""Offline checks for the request-free evaluation gate."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluation_gate.py"


def load_gate():
    spec = importlib.util.spec_from_file_location("evaluation_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(module)
    return module


gate = load_gate()


class EvaluationGateTests(unittest.TestCase):
    def test_official_hipporag_changes_require_evaluation(self):
        for path in ("server/app/hipporag.py", "server/app/hipporag_process.py"):
            with self.subTest(path=path), patch.object(gate, "changed_files", return_value=[path]):
                report = gate.run_gate(ROOT)
            self.assertTrue(report["evaluation_required"])
            self.assertEqual(report["governed_changes"], [path])

    def test_gate_replays_synthetic_fixture_and_marks_governed_changes(self):
        with tempfile.TemporaryDirectory(prefix="zhixing-gate-") as directory:
            report = gate.run_gate(ROOT)
            output = Path(directory) / "gate.json"
            output.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["synthetic"])
            self.assertEqual(report["benchmark"]["passed"], report["benchmark"]["task_count"])
            self.assertIn("server/app/knowledge.py", report["governed_changes"])
            self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["evaluation_required"])

    def test_gate_rejects_malformed_or_partial_benchmark_report(self):
        # A green status alone is not sufficient evidence: aggregate counts
        # must prove that every task passed and the report is synthetic.
        import synthetic_benchmark

        malformed = {
            "status": "passed",
            "synthetic": True,
            "fixture": {"id": "synthetic-industrial-v1"},
            "inputs": {"comparison_key": "fixture"},
            "aggregate": {"passed": 1, "task_count": 2},
        }
        with patch.object(synthetic_benchmark, "build_report", return_value=malformed):
            report = gate.run_gate(ROOT)
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["benchmark"]["aggregate_green"])


if __name__ == "__main__":
    unittest.main()
