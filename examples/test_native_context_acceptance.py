"""Focused evidence assertions only; no HTTP, SDK execution or DB mutation."""
from contextlib import redirect_stdout, redirect_stderr
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_context_smoke as native
import native_boundary_smoke as boundary


class NativeEvidenceTests(unittest.TestCase):
    def test_success_without_native_boundary_cannot_pass(self):
        run = {"kind": "compact", "compaction": {"status": "completed"}}
        with self.assertRaises(native.CheckFailure):
            native.verify_compaction(run, [], "session", "session")

    def test_native_boundary_requires_same_session_and_manual_trigger(self):
        run = {"kind": "compact", "compaction": {"status": "completed"}}
        event = {"value": {"subtype": "compact_boundary", "data": {"compact_metadata": {"trigger": "manual", "pre_tokens": 123}}}}
        native.verify_compaction(run, [event], "session", "session")
        with self.assertRaises(native.CheckFailure):
            native.verify_compaction(run, [event], "session", "other")
        event["value"]["data"]["compact_metadata"]["trigger"] = "auto"
        with self.assertRaises(native.CheckFailure):
            native.verify_compaction(run, [event], "session", "session")

    def test_partial_context_preserves_known_values_without_faking_unknown(self):
        value = {"source": "native_runtime", "status": "unavailable", "total_tokens": 100, "max_tokens": None, "percentage": None}
        self.assertEqual(native.verify_context(value)["total_tokens"], 100)
        with self.assertRaises(native.CheckFailure):
            native.verify_context(value, required=True)
        with self.assertRaises(native.CheckFailure):
            native.verify_context({**value, "percentage": float("nan")})

    def test_memory_file_or_tool_alone_cannot_pass(self):
        trace = {"events": [{"type": "tool.completed", "data": {"tool_name": "Write", "success": True}}]}
        native.verify_memory_write(trace, ["synthetic-marker"], "synthetic-marker")
        with self.assertRaises(native.CheckFailure):
            native.verify_memory_write({"events": []}, ["synthetic-marker"], "synthetic-marker")
        with self.assertRaises(native.CheckFailure):
            native.verify_memory_write(trace, ["other-value"], "synthetic-marker")

    def test_default_plan_and_missing_opt_in_never_construct_runner(self):
        with patch.object(native, "NativeAcceptance") as runner, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(native.main([]), 0)
            self.assertEqual(native.main(["run"]), 2)
            runner.assert_not_called()

    def test_synthetic_boundary_rejects_answer_or_raw_event_disclosure(self):
        boundary.assert_not_exposed("NO_ATTACHMENT", [], "unknown-synthetic-sentinel")
        for answer, events in (("unknown-synthetic-sentinel", []), ("NO_ATTACHMENT", [{"value": "unknown-synthetic-sentinel"}])):
            with self.assertRaises(native.CheckFailure):
                boundary.assert_not_exposed(answer, events, "unknown-synthetic-sentinel")


if __name__ == "__main__":
    unittest.main()
