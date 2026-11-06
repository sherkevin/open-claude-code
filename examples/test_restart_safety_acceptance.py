"""Check acceptance failure boundaries only; never contacts a model or server."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import restart_safety_smoke as safety


def trace(status="interrupted"):
    return {"events": [
        {"id": 1, "type": "tool.started", "data": {"tool_call_id": "call-1", "tool_name": "mcp__demo__slow_lookup", "arguments": {"delay_seconds": 30}}},
        {"id": 2, "type": "run.completed", "data": {"status": status}},
    ]}


class RestartSafetyAssertions(unittest.TestCase):
    def test_restart_modes_and_automatic_replay_are_not_interchangeable(self):
        saved = safety.started_calls(trace())
        safety.assert_terminal("crash", "slow-read", {"status": "interrupted", "approvals": []}, trace(), saved)
        with self.assertRaises(safety.CheckFailure):
            safety.assert_terminal("graceful", "slow-read", {"status": "interrupted", "approvals": []}, trace(), saved)
        repeated = trace()
        repeated["events"].insert(1, {"id": 3, "type": "tool.started", "data": repeated["events"][0]["data"]})
        with self.assertRaises(safety.CheckFailure):
            safety.assert_terminal("crash", "slow-read", {"status": "interrupted", "approvals": []}, repeated, saved)

    def test_completed_tool_is_not_evidence_of_inflight_restart(self):
        completed = trace()
        completed["events"].insert(1, {"id": 3, "type": "tool.completed", "data": {"success": True}})
        with self.assertRaises(safety.CheckFailure):
            safety.assert_terminal("crash", "slow-read", {"status": "interrupted", "approvals": []}, completed, safety.started_calls(completed))
        with self.assertRaises(safety.CheckFailure):
            safety.assert_prepared("slow-read", {"status": "running", "approvals": []}, completed, {"delay_seconds": 30})

    def test_history_preserves_user_text_and_streamed_prefix(self):
        messages = [{"id": "m1", "role": "user", "run_id": "r1", "text": "用户请求", "file_ids": []},
                    {"id": "m2", "role": "assistant", "run_id": "r1", "text": "已开始", "file_ids": []}]
        checkpoint = safety.history_checkpoint(messages)
        after = copy.deepcopy(messages)
        after[1]["text"] += "处理"
        safety.assert_history_preserved(checkpoint, after)
        after[0]["text"] = "其他请求"
        with self.assertRaises(safety.CheckFailure):
            safety.assert_history_preserved(checkpoint, after)

    def test_stale_approval_probe_can_only_deny(self):
        api = Mock()
        safety.reject_stale_approval(api, "old-approval")
        api.request.assert_called_once_with("POST", "/approvals/old-approval/decision", expected=(409,), json={"decision": "deny"})

    def test_no_opt_in_does_not_construct_a_network_client(self):
        with patch.object(safety, "RestartSafety") as runner, redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            self.assertEqual(safety.main(["prepare"]), 2)
            self.assertEqual(safety.main(["check"]), 2)
            self.assertEqual(safety.main(["plan"]), 0)
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
