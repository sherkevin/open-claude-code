"""Bot-only lark-cli adapter; credentials stay inside the selected CLI profile."""
from __future__ import annotations

import html
import json
import os
import queue
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Callable

from .bridge import EVENT_TYPE, OPEN_ID


class LarkError(RuntimeError):
    pass


class LarkClient:
    def __init__(self, config: dict, runner: Callable = subprocess.run):
        self.config, self.runner = config, runner
        self.base = [config.get("lark_cli", "lark-cli"), "--profile", config["profile"]]
        self.env = {**os.environ, "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1", "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1"}

    def verify_identity(self):
        result = self.runner(self.base + ["whoami", "--as", "bot"], text=True, capture_output=True,
                             timeout=45, env=self.env)
        if result.returncode:
            raise LarkError("cannot verify profile identity")
        identity = json.loads(result.stdout)
        if (identity.get("identity") != "bot" or identity.get("available") is not True
                or identity.get("appId") != self.config["expected_app_id"]
                or identity.get("appId") in self.config.get("forbidden_app_ids", [])):
            raise LarkError("profile application identity mismatch")
        result = self.runner(self.base + ["auth", "status", "--json", "--verify"], text=True,
                             capture_output=True, timeout=45, env=self.env)
        if result.returncode:
            raise LarkError("cannot verify bot open_id")
        status = json.loads(result.stdout)
        bot = status.get("identities", {}).get("bot", {})
        if (status.get("appId") != self.config["expected_app_id"] or bot.get("verified") is not True
                or bot.get("openId") != self.config["bot_open_id"]):
            raise LarkError("configured bot open_id is not verified")

    def _call(self, args: list[str], payload: str | None = None) -> dict:
        result = self.runner(self.base + args + ["--as", "bot", "--format", "json"],
                             input=payload, text=True, capture_output=True, timeout=45, env=self.env)
        if result.returncode:
            # Never echo a whole external error or a task's message into service logs.
            raise LarkError(f"lark-cli failed (exit {result.returncode})")
        envelope = json.loads(result.stdout)
        if envelope.get("ok") is not True or envelope.get("identity") != "bot":
            raise LarkError("bot success envelope required")
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise LarkError("invalid lark-cli data envelope")
        return data

    def members(self) -> dict[str, str]:
        data = self._call(["im", "+chat-members-list", "--chat-id", self.config["chat_id"],
                           "--member-types", "user", "--member-id-type", "open_id", "--page-all", "--page-limit", "10"])
        members = {}
        for member in data.get("users", []):
            member_id = member.get("member_id")
            if isinstance(member_id, str) and OPEN_ID.fullmatch(member_id):
                members[member_id] = str(member.get("name") or member_id)[:100]
        if data.get("has_more") or data.get("truncations"):
            # Partial membership is still useful: only positively observed users can be mentioned.
            print("[bridge] member list incomplete; mentions limited to observed members", file=sys.stderr)
        return members

    @staticmethod
    def _messages(data: dict, chat_id: str, before_ms: int) -> list[dict]:
        messages = data.get("messages")
        if not isinstance(messages, list):
            raise LarkError("history messages array missing")
        selected = []
        for message in messages:
            if message.get("deleted") or (message.get("chat_id") and message["chat_id"] != chat_id):
                continue
            try:
                if int(message.get("create_time", 0)) > before_ms:
                    continue
            except (ValueError, TypeError):
                continue
            selected.append({key: message[key] for key in (
                "message_id", "msg_type", "create_time", "sender", "content", "mentions", "thread_id"
            ) if key in message})
        return selected

    def context(self, event: dict) -> dict:
        if event["chat_id"] != self.config["chat_id"]:
            raise LarkError("cross-group context forbidden")
        chat_id = self.config["chat_id"]
        before_ms = int(event["create_time"])
        end = datetime.fromtimestamp(before_ms / 1000, tz=timezone.utc).isoformat()
        limit = str(self.config.get("history_limit", 40))
        recent = self._call(["im", "+chat-messages-list", "--chat-id", chat_id, "--order", "desc",
                             "--page-size", limit, "--end", end, "--no-reactions"])
        context = {"recent_messages": list(reversed(self._messages(recent, chat_id, before_ms))),
                   "history_has_more": bool(recent.get("has_more")), "related_messages": [],
                   "thread_messages": [], "members": self.members(), "context_cutoff_ms": before_ms}
        related = list(dict.fromkeys(event[x] for x in ("reply_to", "root_id") if event.get(x, "").startswith("om_")))
        if related:
            data = self._call(["im", "+messages-mget", "--message-ids", ",".join(related), "--no-reactions"])
            context["related_messages"] = self._messages(data, chat_id, before_ms)
        thread = event.get("thread_id")
        if isinstance(thread, str) and thread.startswith(("om_", "omt_")):
            # Thread APIs have no time filter. Walk past newer discussion until
            # the task's own pre-trigger context is found, with a bounded budget.
            thread_messages, page_token = [], None
            for _ in range(5):
                args = ["im", "+threads-messages-list", "--thread", thread, "--order", "desc",
                        "--page-size", "50", "--no-reactions"]
                if page_token:
                    args += ["--page-token", page_token]
                data = self._call(args)
                thread_messages.extend(self._messages(data, chat_id, before_ms))
                if not data.get("has_more") or len(thread_messages) >= int(limit):
                    break
                page_token = data.get("page_token")
                if not page_token:
                    raise LarkError("incomplete thread pagination receipt")
            if not thread_messages and data.get("has_more"):
                raise LarkError("thread history before trigger exceeds bounded lookup")
            context["thread_messages"] = list(reversed(thread_messages[:int(limit)]))
            context["thread_has_more"] = bool(data.get("has_more")) or len(thread_messages) > int(limit)
        # Bound input, and make truncation explicit instead of silently cutting JSON.
        char_limit = self.config.get("context_char_limit", 18000)
        for bucket in ("recent_messages", "related_messages", "thread_messages"):
            for message in context[bucket]:
                if isinstance(message.get("content"), str) and len(message["content"]) > 4000:
                    message["content"] = message["content"][:4000] + "\n[消息过长，内容已截断]"
        while len(json.dumps(context, ensure_ascii=False)) > char_limit:
            bucket = next((key for key in ("recent_messages", "thread_messages", "related_messages") if context[key]), None)
            if bucket is None:
                raise LarkError("member context exceeds configured input size")
            context[bucket].pop(0)
            context["context_truncated"] = True
        return context

    def reply(self, event: dict, payload: dict, request_id: str) -> str:
        if not self.config.get("send_replies", False):
            raise LarkError("reply sending is disabled")
        if event["chat_id"] != self.config["chat_id"]:
            raise LarkError("cross-group reply forbidden")
        members = self.members()
        mention_ids = payload["mention_ids"]
        if any(member_id not in members for member_id in mention_ids):
            raise LarkError("mention member no longer present")
        tags = [f'<at user_id="{member_id}">{html.escape(members[member_id])}</at>' for member_id in mention_ids]
        text = " ".join(tags) + "\n" + payload["text"]
        args = ["im", "+messages-reply", "--message-id", event["message_id"],
                "--msg-type", "text", "--content", "-", "--idempotency-key", request_id]
        if event.get("thread_id"):
            args.append("--reply-in-thread")
        data = self._call(args, json.dumps({"text": text}, ensure_ascii=False))
        message_id = data.get("message_id")
        if not isinstance(message_id, str) or not message_id.startswith("om_"):
            raise LarkError("send succeeded without identifiable receipt")
        return message_id


class EventStream:
    """A single CLI subscriber, with its stdin held open and readiness observed."""
    def __init__(self, client: LarkClient, on_event: Callable | None = None):
        self.client = client
        self.on_event = on_event
        self.events: queue.Queue = queue.Queue(maxsize=1000)
        self.ready = threading.Event()
        self.failed = threading.Event()
        self.process = None
        self.reader = None
        self.error = None

    def start(self):
        self.process = subprocess.Popen(
            self.client.base + ["event", "consume", EVENT_TYPE, "--as", "bot"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=self.client.env)
        threading.Thread(target=self._stderr, daemon=True).start()
        if not self.ready.wait(30):
            self.close()
            raise LarkError("event stream did not emit ready marker")
        if self.failed.is_set():
            self.close()
            raise LarkError("event stream exited before startup")
        self.reader = threading.Thread(target=self._stdout, daemon=True)
        self.reader.start()

    def _stderr(self):
        for line in self.process.stderr:
            if line.strip() == f"[event] ready event_key={EVENT_TYPE}":
                self.ready.set()
            # CLI readiness, drop and failure diagnostics must remain visible.
            print(line.rstrip(), file=sys.stderr)
        self.failed.set()
        self.ready.set()

    def _stdout(self):
        try:
            for line in self.process.stdout:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise LarkError("invalid event shape")
                if self.on_event:
                    self.on_event(event)
                else:
                    self.events.put(event, timeout=5)
        except Exception as exc:
            self.error = type(exc).__name__
            print(f"[bridge] event intake failed: {self.error}", file=sys.stderr)
        finally:
            self.failed.set()

    def close(self):
        if self.process is not None:
            if self.process.stdin and not self.process.stdin.closed:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
            if self.reader:
                self.reader.join(timeout=15)
