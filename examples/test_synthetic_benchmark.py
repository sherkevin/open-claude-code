"""Offline checks for the stage-0 synthetic industrial benchmark."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "synthetic_benchmark.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("synthetic_benchmark", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = load_runner()


class SyntheticBenchmarkTests(unittest.TestCase):
    def test_fixture_is_versioned_and_hash_checked(self):
        manifest, manifest_hash = runner.load_fixture()
        self.assertTrue(manifest["synthetic"])
        self.assertEqual(manifest["fixture_id"], "synthetic-industrial-v1")
        self.assertEqual(len(manifest["files"]), 5)
        self.assertEqual(len(manifest_hash), 64)

    def test_local_tools_keep_source_marker(self):
        manifest, _ = runner.load_fixture()
        mcp = runner.SyntheticIndustrialMCP(runner.FIXTURE_DIR, manifest)
        quality = mcp.query_quality_metrics()
        self.assertEqual((quality["inspected_count"], quality["qualified_count"]), (4700, 4537))
        self.assertTrue(quality["synthetic"])
        self.assertTrue(quality["sources"][0]["synthetic"])
        self.assertEqual(mcp.inspect_image()["visual_quality_status"], "not_measured")

    def test_cli_run_verify_and_clean(self):
        with tempfile.TemporaryDirectory(prefix="zhixing-synthetic-test-") as directory:
            output = Path(directory) / "report.json"
            run = subprocess.run([sys.executable, str(SCRIPT), "run", "--output", str(output)], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["aggregate"]["passed"], 4)
            self.assertEqual(len(report["inputs"]["comparison_key"]), 24)
            self.assertEqual(report["inputs"]["task_ids"], ["quality-yield", "knowledge-procedure", "session-memory", "image-source"])
            self.assertEqual({task["status"] for task in report["tasks"]}, {"passed"})
            verify = subprocess.run([sys.executable, str(SCRIPT), "verify", "--report", str(output)], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertEqual(json.loads(verify.stdout)["status"], "passed")
            clean = subprocess.run([sys.executable, str(SCRIPT), "clean", "--output", str(output)], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertFalse(output.exists())

    def test_plan_is_explicitly_request_free(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "plan"], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        plan = json.loads(result.stdout)
        self.assertFalse(plan["network"])
        self.assertFalse(plan["model"])
        self.assertTrue(plan["synthetic"])

    def test_clean_never_removes_a_directory(self):
        with tempfile.TemporaryDirectory(prefix="zhixing-synthetic-clean-") as directory:
            path = Path(directory) / "report.json"
            path.mkdir()
            result = subprocess.run([sys.executable, str(SCRIPT), "clean", "--output", str(path)], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(path.is_dir())


if __name__ == "__main__":
    unittest.main()
