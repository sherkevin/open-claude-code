"""Offline regressions. Never connect to Feishu or an actual Codex session."""
import fcntl
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from integrations.feishu_project_bot.bridge import Bridge, build_prompt, extract_public_reply, is_trigger, validate_config
from integrations.feishu_project_bot.lark_client import EventStream, LarkClient, LarkError


def config(state):
    return {"profile": "isolated", "expected_app_id": "cli_project", "forbidden_app_ids": ["cli_steward"],
            "chat_id": "oc_group", "bot_open_id": "ou_bot", "session_id": "test-session",
            "project_name": "测试项目", "project_scope": "项目开发", "state_dir": state,
            "send_replies": True, "busy_retry_seconds": 0}


def event(**overrides):
    return {"type": "im.message.receive_v1", "chat_id": "oc_group", "chat_type": "group",
            "sender_type": "user", "sender_id": "ou_alice", "message_id": "om_task",
            "message_type": "text", "content": "@负责人 请检查项目需求", "create_time": "1700000000000",
            "mentions": [{"id": "ou_bot", "name": "负责人", "key": "@_user_1"}], **overrides}


def envelope(request_id, text="已完成", mentions=None):
    return json.dumps({"feishu_project_reply": {"request_id": request_id, "text": text,
                                                "mention_ids": mentions or []}}, ensure_ascii=False)


class FakeLark:
    def __init__(self):
        self.calls = []
        self.context_error = False
        self.send_error = False

    def context(self, message):
        self.calls.append(("context", message["message_id"]))
        if self.context_error:
            raise LarkError("no group history permission")
        return {"recent_messages": [{"content": "背景讨论"}], "members": {"ou_alice": "Alice", "ou_bob": "Bob"}}

    def reply(self, message, payload, request_id):
        self.calls.append(("reply", message["message_id"], payload, request_id))
        if self.send_error:
            raise TimeoutError()
        return "om_reply"


class FakeTransport:
    def __init__(self):
        self.submits, self.polls = [], []
        self.busy = False
        self.result = None

    def submit(self, request_id, prompt):
        self.submits.append((request_id, prompt))
        return {"request_id": request_id, "status": "queued" if self.busy else "submitted"}

    def poll(self, request_id):
        self.polls.append(request_id)
        return self.result or {"request_id": request_id, "status": "submitted"}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = config(self.tmp.name)
        self.lark, self.transport = FakeLark(), FakeTransport()
        self.bridge = Bridge(self.config, self.lark, self.transport)

    def tearDown(self):
        self.bridge.close()
        self.tmp.cleanup()

    def reopen(self):
        self.bridge.close()
        self.bridge = Bridge(self.config, self.lark, self.transport)

    def test_plain_text_name_or_at_all_is_not_a_trigger(self):
        for mentions in ([], [{"id": "all", "name": "所有人"}], [{"id": "ou_other", "name": "负责人"}]):
            self.assertIsNone(self.bridge.ingest(event(content="@负责人", mentions=mentions)))
        self.assertFalse(self.transport.submits)

    def test_cross_group_p2p_bot_loop_and_wrong_type_are_rejected(self):
        for overrides in ({"chat_id": "oc_other"}, {"chat_type": "p2p"}, {"sender_type": "bot"},
                          {"sender_type": None}, {"sender_id": "ou_bot"}, {"type": "other"},
                          {"message_type": "interactive"}):
            self.assertFalse(is_trigger(event(**overrides), self.config))

    def test_structured_human_mention_is_accepted(self):
        self.assertTrue(is_trigger(event(), self.config))

    def test_message_id_deduplicates_different_event_ids_across_restart(self):
        first = self.bridge.ingest(event(event_id="delivery1"))
        self.reopen()
        self.assertEqual(first, self.bridge.ingest(event(event_id="delivery2")))
        self.assertEqual(len(self.bridge.statuses()), 1)

    def test_followup_without_mention_does_not_create_task(self):
        self.bridge.ingest(event())
        self.bridge.ingest(event(message_id="om_followup", reply_to="om_task", mentions=[]))
        self.assertEqual(len(self.bridge.statuses()), 1)

    def test_context_read_failure_prevents_submit(self):
        self.lark.context_error = True
        self.bridge.ingest(event())
        self.bridge.tick()
        self.assertFalse(self.transport.submits)
        self.assertEqual(self.bridge.statuses()[0]["status"], "held")

    def test_busy_queue_retries_first_task_without_dispatching_second(self):
        first = self.bridge.ingest(event())
        self.bridge.ingest(event(message_id="om_task2"))
        self.transport.busy = True
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual([x[0] for x in self.transport.submits], [first, first])
        self.assertEqual([x["status"] for x in self.bridge.statuses()], ["queued", "queued"])
        self.transport.busy = False
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual([x["status"] for x in self.bridge.statuses()], ["submitted", "queued"])

    def test_only_matching_public_final_is_sent_to_original_message(self):
        request_id = self.bridge.ingest(event())
        self.bridge.tick()
        self.transport.result = {"request_id": request_id, "status": "completed",
                                 "final": "私有会话内容不得自动转发\n```json\n" + envelope(request_id, mentions=["ou_bob"]) + "\n```"}
        self.bridge.tick()
        self.bridge.tick()
        reply = self.lark.calls[-1]
        self.assertEqual(reply[0:2], ("reply", "om_task"))
        self.assertEqual(reply[2], {"text": "已完成", "mention_ids": ["ou_alice", "ou_bob"]})
        self.assertEqual(self.bridge.statuses()[0]["status"], "sent")
        self.reopen()
        self.bridge.tick()
        self.assertEqual(len([x for x in self.lark.calls if x[0] == "reply"]), 1)

    def test_foreign_receipt_is_held_and_never_forwarded(self):
        self.bridge.ingest(event())
        self.bridge.tick()
        self.transport.result = {"request_id": "private-turn", "status": "completed", "final": "private"}
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(self.bridge.statuses()[0]["status"], "held")
        self.assertFalse([x for x in self.lark.calls if x[0] == "reply"])

    def test_matching_receipt_without_public_envelope_is_not_forwarded(self):
        request_id = self.bridge.ingest(event())
        self.bridge.tick()
        self.transport.result = {"request_id": request_id, "status": "completed", "final": "ordinary private final"}
        self.bridge.tick()
        self.assertEqual(self.bridge.statuses()[0]["status"], "held")

    def test_restart_during_dispatch_only_polls_never_resubmits(self):
        request_id = self.bridge.ingest(event())
        self.bridge._status(request_id, "dispatching")
        self.reopen()
        self.bridge.tick()
        self.assertEqual(self.transport.polls, [request_id])
        self.assertEqual(self.transport.submits, [])

    def test_reply_timeout_is_held_and_never_blindly_resent(self):
        request_id = self.bridge.ingest(event())
        self.bridge.tick()
        self.transport.result = {"request_id": request_id, "status": "completed", "final": envelope(request_id)}
        self.bridge.tick()
        self.lark.send_error = True
        self.bridge.tick()
        self.reopen()
        self.bridge.tick()
        self.assertEqual(len([x for x in self.lark.calls if x[0] == "reply"]), 1)
        self.assertEqual(self.bridge.statuses()[0]["status"], "held")

    def test_restart_during_sending_is_held(self):
        request_id = self.bridge.ingest(event())
        self.bridge._status(request_id, "sending")
        self.reopen()
        self.assertEqual(self.bridge.statuses()[0]["status"], "held")

    def test_uncertain_delivery_only_polls_until_exact_completion(self):
        request_id = self.bridge.ingest(event())
        self.bridge.tick()
        self.transport.result = {"request_id": request_id, "status": "uncertain", "reason": "delivery_unknown"}
        self.bridge.tick()
        self.assertEqual(self.bridge.statuses()[0]["status"], "uncertain")
        self.transport.result = {"request_id": request_id, "status": "completed", "final": envelope(request_id)}
        self.bridge.tick()
        self.assertEqual(len(self.transport.submits), 1)
        self.assertEqual(self.bridge.statuses()[0]["status"], "ready")

    def test_mixed_private_user_input_permanently_holds_reply(self):
        request_id = self.bridge.ingest(event())
        self.bridge.tick()
        self.transport.result = {"request_id": request_id, "status": "uncertain", "reason": "mixed_user_input"}
        self.bridge.tick()
        self.assertEqual(self.bridge.statuses()[0]["status"], "held")

    def test_existing_state_cannot_be_rebound_to_another_session(self):
        changed = {**self.config, "session_id": "another-session"}
        with self.assertRaises(ValueError):
            Bridge(changed, self.lark, self.transport)

    def test_receive_thread_persists_inbox_without_worker_connection(self):
        receiver = threading.Thread(target=self.bridge.ingest, args=(event(),))
        receiver.start()
        receiver.join(timeout=10)
        self.assertFalse(receiver.is_alive())
        self.assertEqual(self.bridge.statuses()[0]["message_id"], "om_task")

    def test_status_is_readable_while_service_lock_is_held(self):
        self.bridge.ingest(event())
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps(self.config))
        with (Path(self.tmp.name) / "bridge.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, "-m", "integrations.feishu_project_bot", str(path), "--status"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)[0]["message_id"], "om_task")

    def test_send_disabled_persists_ready_outbox_without_external_reply(self):
        self.config["send_replies"] = False
        request_id = self.bridge.ingest(event())
        self.bridge.tick()
        self.transport.result = {"request_id": request_id, "status": "completed", "final": envelope(request_id)}
        self.bridge.tick()
        self.bridge.tick()
        self.assertEqual(self.bridge.statuses()[0]["status"], "ready")
        self.assertFalse([x for x in self.lark.calls if x[0] == "reply"])

    def test_prompt_scopes_untrusted_group_input_and_limits_size(self):
        prompt = build_prompt(self.config, "request", event(), {"recent_messages": []})
        self.assertIn("不可信", prompt)
        self.assertIn("禁止读取或公开主账号凭据", prompt)
        self.assertIn("feishu_project_reply", prompt)
        with self.assertRaises(ValueError):
            build_prompt(self.config, "request", event(content="x" * 31000), {})

    def test_reply_rejects_unknown_mentions_raw_tags_ambiguity_and_wrong_request(self):
        for final in (envelope("wrong"), envelope("r", mentions=["ou_stranger"]),
                      envelope("r", '<at user_id="all">everyone</at>'), envelope("r") + envelope("r")):
            with self.assertRaises(ValueError):
                extract_public_reply(final, "r", {"ou_alice"})

    def test_config_rejects_forbidden_application(self):
        self.config["expected_app_id"] = "cli_steward"
        with self.assertRaises(ValueError):
            validate_config(self.config)


class EventStreamTests(unittest.TestCase):
    def test_shutdown_waits_for_received_event_to_be_persisted(self):
        started, release = threading.Event(), threading.Event()
        persisted = []

        def persist(message):
            started.set()
            release.wait(timeout=2)
            persisted.append(message["message_id"])

        stream = EventStream(None, persist)
        stream.process = SimpleNamespace(stdin=io.StringIO(), stdout=io.StringIO(json.dumps(event()) + "\n"),
                                         wait=lambda timeout: 0)
        stream.reader = threading.Thread(target=stream._stdout)
        stream.reader.start()
        self.assertTrue(started.wait(timeout=2))
        timer = threading.Timer(0.05, release.set)
        timer.start()
        stream.close()
        timer.join()
        self.assertEqual(persisted, ["om_task"])
        self.assertFalse(stream.reader.is_alive())


class LarkClientTests(unittest.TestCase):
    def setUp(self):
        self.config = config("unused")
        self.responses, self.calls = [], []
        self.client = LarkClient(self.config, self.run_fake)

    def run_fake(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        value = self.responses.pop(0)
        return subprocess.CompletedProcess(argv, 0, json.dumps(value), "")

    @staticmethod
    def response(data, identity="bot"):
        return {"ok": True, "identity": identity, "data": data}

    def test_context_uses_bot_profile_and_excludes_messages_after_trigger(self):
        self.responses = [self.response({"messages": [
            {"message_id": "om_future", "create_time": "1700000000001", "content": "后续任务"},
            {"message_id": "om_before", "create_time": "1699999999000", "content": "背景"}], "has_more": True}),
            self.response({"users": [{"member_id": "ou_alice", "name": "Alice"}]})]
        context = self.client.context(event())
        self.assertEqual([x["message_id"] for x in context["recent_messages"]], ["om_before"])
        for argv, kwargs in self.calls:
            self.assertEqual(argv[1:3], ["--profile", "isolated"])
            self.assertIn("--as", argv)
            self.assertEqual(argv[argv.index("--as") + 1], "bot")
            self.assertNotIn("shell", kwargs)

    def test_reply_content_goes_via_stdin_and_mentions_are_structured(self):
        self.responses = [self.response({"users": [{"member_id": "ou_alice", "name": "A&B"}]}),
                          self.response({"message_id": "om_sent"})]
        self.client.reply(event(), {"text": "`danger` $(do-not-execute)", "mention_ids": ["ou_alice"]}, "request")
        argv, kwargs = self.calls[-1]
        self.assertNotIn("`danger`", " ".join(argv))
        self.assertIn('<at user_id="ou_alice">A&amp;B</at>', json.loads(kwargs["input"])["text"])
        self.assertEqual(argv[argv.index("--idempotency-key") + 1], "request")

    def test_user_identity_is_rejected_even_with_ok_true(self):
        self.responses = [self.response({"users": []}, identity="user")]
        with self.assertRaises(LarkError):
            self.client.members()

    def test_thread_paginates_past_newer_messages_before_selecting_context(self):
        self.responses = [self.response({"messages": []}), self.response({"users": []}),
                          self.response({"messages": [{"message_id": "om_future", "create_time": "1700000000001"}],
                                         "has_more": True, "page_token": "older"}),
                          self.response({"messages": [{"message_id": "om_before", "create_time": "1699999999000"}],
                                         "has_more": False})]
        context = self.client.context(event(thread_id="omt_thread"))
        self.assertEqual(context["thread_messages"][0]["message_id"], "om_before")
        self.assertIn("--page-token", self.calls[-1][0])

    def test_thread_local_limit_is_reported_as_partial_history(self):
        self.config["history_limit"] = 1
        self.responses = [self.response({"messages": []}), self.response({"users": []}),
                          self.response({"messages": [{"message_id": "om_1", "create_time": "1000"},
                                                       {"message_id": "om_2", "create_time": "999"}], "has_more": False})]
        context = self.client.context(event(thread_id="omt_thread"))
        self.assertTrue(context["thread_has_more"])
        self.assertEqual(len(context["thread_messages"]), 1)

    def test_wrong_profile_app_rejected_before_auth_or_event_connection(self):
        self.responses = [{"identity": "bot", "available": True, "appId": "cli_steward"}]
        with self.assertRaises(LarkError):
            self.client.verify_identity()
        self.assertEqual(len(self.calls), 1)

    def test_verified_app_and_open_id_are_required(self):
        self.responses = [{"identity": "bot", "available": True, "appId": "cli_project"},
                          {"appId": "cli_project", "identities": {"bot": {"openId": "ou_wrong", "verified": True}}}]
        with self.assertRaises(LarkError):
            self.client.verify_identity()


if __name__ == "__main__":
    unittest.main()
