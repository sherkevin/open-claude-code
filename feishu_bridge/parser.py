from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    open_id: str
    text: str


class UnsupportedMessage(ValueError):
    pass


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        import lark_oapi as lark

        return json.loads(lark.JSON.marshal(value))
    except Exception as exc:  # pragma: no cover - SDK-specific fallback
        raise UnsupportedMessage("event cannot be converted to JSON") from exc


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        try:
            decoded = json.loads(content)
        except (TypeError, ValueError):
            return content
    else:
        decoded = content
    if isinstance(decoded, dict) and isinstance(decoded.get("text"), str):
        return decoded["text"]
    raise UnsupportedMessage("only text messages are supported")


def _strip_mentions(text: str) -> str:
    # Feishu sends group mentions as XML-like tags in the text payload.  The
    # bridge is only called after the event subscription has delivered the
    # message, so removing the tags is presentation cleanup, not auth logic.
    text = re.sub(r"<at\b[^>]*>.*?</at>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(text.split()).strip()


def parse_message(value: Any) -> IncomingMessage:
    payload = _as_dict(value)
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else sender
    message_type = message.get("message_type") or event.get("message_type") or "text"
    if message_type != "text":
        raise UnsupportedMessage("only text messages are supported")
    text = _strip_mentions(_text_from_content(message.get("content") or event.get("content")))
    if not text:
        raise UnsupportedMessage("empty text message")
    event_id = str(header.get("event_id") or payload.get("event_id") or message.get("message_id") or "").strip()
    message_id = str(message.get("message_id") or event.get("message_id") or event_id).strip()
    chat_id = str(message.get("chat_id") or event.get("chat_id") or "").strip()
    open_id = str(sender_id.get("open_id") or sender_id.get("user_id") or "").strip()
    chat_type = str(message.get("chat_type") or event.get("chat_type") or "p2p").strip()
    if not event_id or not message_id or not chat_id or not open_id:
        raise UnsupportedMessage("message event is missing an identity field")
    return IncomingMessage(event_id, message_id, chat_id, chat_type, open_id, text)
