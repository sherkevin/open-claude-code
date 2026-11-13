import json

import pytest

from feishu_bridge.parser import UnsupportedMessage, parse_message


def test_parse_v2_text_and_strip_mention():
    value = {
        "header": {"event_id": "evt-1"},
        "event": {
            "message": {"message_id": "om-1", "chat_id": "oc-1", "chat_type": "group", "message_type": "text", "content": json.dumps({"text": "<at user_id=\"ou-bot\">机器人</at> 继续分析"})},
            "sender": {"sender_id": {"open_id": "ou-owner"}},
        },
    }
    message = parse_message(value)
    assert message.event_id == "evt-1"
    assert message.chat_type == "group"
    assert message.text == "继续分析"


def test_parse_rejects_non_text_and_missing_identity():
    with pytest.raises(UnsupportedMessage):
        parse_message({"header": {"event_id": "evt"}, "event": {"message": {"message_id": "m", "chat_id": "c", "message_type": "image", "content": "{}"}, "sender": {"sender_id": {"open_id": "ou"}}}})
    with pytest.raises(UnsupportedMessage):
        parse_message({"event": {"message": {"message_type": "text", "content": "hi"}}})
