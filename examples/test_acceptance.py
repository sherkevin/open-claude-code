"""Unit checks for fixture invariants; does not call a model or real business API."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


demo = load("acceptance_demo", ROOT / "examples" / "demo_mcp.py")
smoke = load("acceptance_smoke", ROOT / "scripts" / "smoke_api.py")


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_dir = demo.DATA_DIR
        demo.DATA_DIR = Path(self.temp.name)

    def tearDown(self):
        demo.DATA_DIR = self.previous_dir
        self.temp.cleanup()

    def test_knowledge_has_known_answer_and_source(self):
        result = demo.search_knowledge("设备报修")
        self.assertTrue(result["demo"])
        self.assertEqual(result["documents"][0]["id"], "DEMO-KB-001")
        self.assertIn("4 个工作小时", result["documents"][0]["content"])

    def test_ticket_idempotency_and_content_conflict(self):
        first = demo.create_ticket("演示标题", "演示描述", "验收", "request-1")
        again = demo.create_ticket("演示标题", "演示描述", "验收", "request-1")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["ticket"]["id"], again["ticket"]["id"])
        with self.assertRaises(ValueError):
            demo.create_ticket("不同标题", "演示描述", "验收", "request-1")
        self.assertEqual(len(demo.list_tickets("request-1")["tickets"]), 1)
        self.assertEqual(demo.list_tickets("missing")["tickets"], [])

    def test_invalid_ticket_does_not_write(self):
        with self.assertRaises(ValueError):
            demo.create_ticket("", "演示描述", "验收", "invalid-request")
        self.assertEqual(demo.list_tickets()["tickets"], [])

    def test_external_binding_requires_strong_token(self):
        previous = demo.HOST, demo.TOKEN
        try:
            demo.HOST, demo.TOKEN = "0.0.0.0", ""
            with self.assertRaises(ValueError):
                demo.validate_binding()
            demo.TOKEN = "test-only-token-at-least-24-characters"
            demo.validate_binding()
        finally:
            demo.HOST, demo.TOKEN = previous


class StreamTests(unittest.TestCase):
    def test_sse_fields_agree(self):
        payload = {"id": 7, "type": "run.completed", "run_id": "run-1", "data": {"status": "succeeded"}}
        events = list(smoke.parse_sse(iter([": heartbeat", "id: 7", "event: run.completed", "data: " + json.dumps(payload), ""])))
        self.assertEqual(events, [payload])

    def test_sse_id_mismatch_is_an_error(self):
        with self.assertRaises(smoke.CheckFailure):
            list(smoke.parse_sse(iter(["id: 3", 'data: {"id":4}', ""])))

    def test_only_exact_tool_suffix_matches(self):
        self.assertTrue(smoke.tool_matches("mcp__demo__create_ticket", "create_ticket"))
        self.assertTrue(smoke.tool_matches("create_ticket", "create_ticket"))
        self.assertFalse(smoke.tool_matches("not_create_ticket", "create_ticket"))


if __name__ == "__main__":
    unittest.main()
