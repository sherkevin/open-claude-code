"""Reject misleading lifecycle evidence; no HTTP, provider or product DB writes."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lifecycle_smoke as lifecycle


def snapshot(server_pid=10, worker_pid=20, start_ticks=100, session="sdk-session"):
    return {"sdk_session_id": session, "server_process": {"pid": server_pid, "start_ticks": 50},
            "workers": [{"pid": worker_pid, "start_ticks": start_ticks}]}


class LifecycleEvidenceTests(unittest.TestCase):
    def test_warm_requires_actual_worker_identity(self):
        first = snapshot()
        lifecycle.verify_transition(first, copy.deepcopy(first), "warm")
        for changed in (snapshot(worker_pid=21), snapshot(start_ticks=101)):
            with self.assertRaises(lifecycle.CheckFailure):
                lifecycle.verify_transition(first, changed, "warm")

    def test_idle_requires_same_session_and_server_but_new_worker(self):
        first = snapshot()
        lifecycle.verify_transition(first, snapshot(worker_pid=21), "idle")
        for invalid in (snapshot(), snapshot(server_pid=11, worker_pid=21), snapshot(worker_pid=21, session="different")):
            with self.assertRaises(lifecycle.CheckFailure):
                lifecycle.verify_transition(first, invalid, "idle")

    def test_restart_requires_new_server_and_worker(self):
        first = snapshot()
        lifecycle.verify_transition(first, snapshot(server_pid=11, worker_pid=21), "restart")
        for invalid in (snapshot(worker_pid=21), snapshot(server_pid=11), snapshot(server_pid=11, worker_pid=21, session="different")):
            with self.assertRaises(lifecycle.CheckFailure):
                lifecycle.verify_transition(first, invalid, "restart")

    def test_missing_process_evidence_cannot_pass(self):
        missing = snapshot()
        missing["workers"] = []
        with self.assertRaises(lifecycle.CheckFailure):
            lifecycle.verify_transition(snapshot(), missing, "idle")

    def test_proc_stat_parser_handles_parentheses_in_process_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "42").mkdir()
            fields = ["S", "10"] + ["0"] * 17 + ["7654"] + ["0"] * 5
            (root / "42" / "stat").write_text("42 (name (with spaces)) " + " ".join(fields))
            self.assertEqual(lifecycle.proc_identity(42, root), {"pid": 42, "start_ticks": 7654, "parent_pid": 10})

    def test_no_opt_in_never_constructs_api_or_observer(self):
        with patch.object(lifecycle, "Lifecycle") as runner, redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            for phase in lifecycle.MODEL_PHASES:
                self.assertEqual(lifecycle.main([phase]), 2)
            self.assertEqual(lifecycle.main(["plan"]), 0)
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
