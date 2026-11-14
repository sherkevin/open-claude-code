"""Preserve tool evidence while avoiding the host's redundant JSON copy."""
import copy
import json

import pytest

from app.mcp_tools import include_structured_text


@pytest.mark.parametrize("text", [
    '{"label":"泵","rows":[{"pressure":7,"enabled":true}]}',
    '{ "rows" : [{ "enabled": true, "pressure": 7 }], "label":"\\u6cf5" }',
    '{\n"label": "泵",\n"rows": [{"pressure": 7, "enabled": true}]\n}',
])
def test_entire_equivalent_json_is_not_duplicated(text):
    block = {"type": "text", "text": text, "annotations": {"audience": ["assistant"]}}
    payload = {"label": "泵", "rows": [{"pressure": 7, "enabled": True}]}
    body = {"content": [block], "structuredContent": payload}
    original = copy.deepcopy(body)
    result = include_structured_text(body)
    assert result == original and body == original
    assert result["content"][0] == block


@pytest.mark.parametrize("text,structured", [
    ('{"value":1}', {"value": True}),
    ('{"value":true}', {"value": 1}),
    ('{"rows":[2,1]}', {"rows": [1, 2]}),
    ('{"x":0,"x":1}', {"x": 1}),
    ('{"nested":{"x":0,"x":1}}', {"nested": {"x": 1}}),
    ('{"value":NaN}', {"value": None}),
    ('{"value":Infinity}', {"value": None}),
    ('{"value":1e10000}', {"value": None}),
    ('{"value":1e999999999999999999999999999999}', {"value": 1}),
    ('{"value":1.00000000000000001}', {"value": 1.0}),
    ('{"value":9007199254740993}', {"value": 9007199254740992}),
    ('{"row":1}', {"row": 1, "warning": "partial"}),
    ('The count is one.', {"count": 1}),
    ('```json\n{"count":1}\n```', {"count": 1}),
])
def test_nonidentical_ambiguous_or_invalid_json_keeps_both(text, structured):
    original = {"content": [{"type": "text", "text": text}], "structuredContent": structured}
    before = copy.deepcopy(original)
    result = include_structured_text(original)
    assert original == before
    assert result["content"][0] == before["content"][0]
    assert json.loads(result["content"][1]["text"]) == structured
    assert result["structuredContent"] == structured


def test_explanations_and_warnings_keep_their_original_order():
    blocks = [{"type": "text", "text": "Result is partial."},
              {"type": "text", "text": '{"rows":[]}'},
              {"type": "text", "text": "Do not infer the missing rows."}]
    body = {"content": blocks, "structuredContent": {"rows": []}}
    assert include_structured_text(body)["content"] == blocks


def test_split_blocks_are_not_merged_to_guess_equivalence():
    blocks = [{"type": "text", "text": '{"rows":'}, {"type": "text", "text": '[]}'}]
    result = include_structured_text({"content": blocks, "structuredContent": {"rows": []}})
    assert result["content"][:2] == blocks and len(result["content"]) == 3


@pytest.mark.parametrize("structured", [{}, {"rows": []}, {"value": 0}])
def test_structured_only_including_empty_object_reaches_model(structured):
    result = include_structured_text({"content": [], "structuredContent": structured})
    assert len(result["content"]) == 1
    assert json.loads(result["content"][0]["text"]) == structured


def test_text_only_result_is_unchanged():
    body = {"content": [{"type": "text", "text": "No structured result."}]}
    assert include_structured_text(body) == body


def test_duplicate_rows_in_the_payload_are_never_deduplicated():
    payload = {"rows": [{"order": "same"}, {"order": "same"}]}
    text = json.dumps(payload)
    result = include_structured_text({"content": [{"type": "text", "text": text}], "structuredContent": payload})
    assert result["content"][0]["text"] == text
    assert len(json.loads(result["content"][0]["text"])["rows"]) == 2
