import asyncio

import pytest

from app.openai_compat import (
    NormalizedResult,
    NormalizedToolCall,
    NormalizedUsage,
    OpenAICompatibleAdapter,
    ProtocolAdapterError,
    parse_request,
)


def _chat(text="hello", **extra):
    return {"model": "synthetic-model", "messages": [{"role": "user", "content": text}], **extra}


def _response(text="hello", **extra):
    return {"model": "synthetic-model", "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}], **extra}


def test_parse_both_protocols_and_reject_ambiguous_or_unbounded_requests():
    chat = parse_request(_chat(), allowed_models={"synthetic-model"})
    assert chat.protocol == "chat.completions" and chat.messages[0].content == "hello"
    responses = parse_request(_response(), allowed_models={"synthetic-model"})
    assert responses.protocol == "responses" and responses.messages[0].content[0]["text"] == "hello"
    with pytest.raises(ProtocolAdapterError) as caught:
        parse_request({"model": "synthetic-model", "messages": [], "input": []})
    assert caught.value.code == "INVALID_REQUEST"
    with pytest.raises(ProtocolAdapterError) as caught:
        parse_request(_chat("x", max_tokens=9000))
    assert caught.value.code == "INVALID_REQUEST"
    with pytest.raises(ProtocolAdapterError) as caught:
        parse_request(_chat(), allowed_models={"another-model"})
    assert caught.value.code == "MODEL_NOT_ALLOWED"


def test_chat_completion_maps_usage_and_tool_calls_without_network():
    calls = []

    async def transport(request):
        calls.append(request)
        assert request.protocol == "chat.completions"
        assert request.tools[0].name == "lookup"
        return NormalizedResult(
            text="",
            tool_calls=(NormalizedToolCall("call_1", "lookup", {"key": "value"}),),
            finish_reason="tool_calls",
            usage=NormalizedUsage(input_tokens=11, output_tokens=7, total_tokens=18),
        )

    payload = _chat(tools=[{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}])
    result = asyncio.run(OpenAICompatibleAdapter(transport, allowed_models={"synthetic-model"}).complete(payload))
    assert result["object"] == "chat.completion"
    assert result["choices"][0]["finish_reason"] == "tool_calls"
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"key":"value"}'
    assert result["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert len(calls) == 1


def test_responses_completion_and_stream_events_are_protocol_specific():
    def transport(request):
        assert request.protocol == "responses"
        return {"text": "hello world", "chunks": ["hello", " world"], "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}}

    adapter = OpenAICompatibleAdapter(transport, allowed_models={"synthetic-model"})
    result = asyncio.run(adapter.complete(_response()))
    assert result["object"] == "response"
    assert result["output_text"] == "hello world"
    assert result["output"][0]["content"][0]["type"] == "output_text"
    assert result["usage"] == {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}

    async def collect():
        return [event async for event in adapter.stream_events(_response(stream=True))]

    events = asyncio.run(collect())
    assert [event["type"] for event in events] == ["response.created", "response.output_text.delta", "response.output_text.delta", "response.completed"]
    assert events[-1]["response"]["status"] == "completed"


def test_adapter_accepts_compatible_upstream_response_envelopes():
    async def chat_transport(_request):
        return {"choices": [{"message": {"role": "assistant", "content": "ok", "tool_calls": []}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    async def responses_transport(_request):
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}

    chat = asyncio.run(OpenAICompatibleAdapter(chat_transport).complete(_chat()))
    responses = asyncio.run(OpenAICompatibleAdapter(responses_transport).complete(_response()))
    assert chat["choices"][0]["message"]["content"] == "ok"
    assert responses["output_text"] == "ok"


def test_assistant_null_content_is_allowed_but_malformed_tool_arguments_fail_closed():
    request = parse_request({"model": "synthetic-model", "messages": [{"role": "assistant", "content": None}]}, allowed_models={"synthetic-model"})
    assert request.messages[0].content is None

    async def transport(_request):
        return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "function": {"name": "lookup", "arguments": "not-json"}}]}, "finish_reason": "tool_calls"}]}

    with pytest.raises(ProtocolAdapterError) as caught:
        asyncio.run(OpenAICompatibleAdapter(transport).complete(_chat()))
    assert caught.value.code == "MODEL_EXECUTION_FAILED"


@pytest.mark.parametrize("status,code,public_status", [
    (401, "MODEL_AUTHORIZATION_FAILED", 401),
    (408, "MODEL_TIMEOUT", 504),
    (429, "MODEL_RATE_LIMITED", 429),
    (500, "MODEL_SERVICE_UNAVAILABLE", 503),
])
def test_upstream_failures_are_mapped_without_leaking_diagnostics(status, code, public_status):
    class UpstreamFailure(Exception):
        status_code = status

        def __str__(self):
            return "private-token https://private.example/diagnostic"

    async def transport(_request):
        raise UpstreamFailure()

    with pytest.raises(ProtocolAdapterError) as caught:
        asyncio.run(OpenAICompatibleAdapter(transport).complete(_chat()))
    assert caught.value.code == code
    assert caught.value.status == public_status
    assert "private-token" not in caught.value.message
    assert "private.example" not in str(caught.value)


def test_stream_shape_guard_and_unknown_content_are_fail_closed():
    async def transport(_request):
        return {"text": "never used"}

    adapter = OpenAICompatibleAdapter(transport)
    with pytest.raises(ProtocolAdapterError) as caught:
        asyncio.run(adapter.complete(_chat(stream=True)))
    assert caught.value.code == "STREAM_REQUIRED"
    with pytest.raises(ProtocolAdapterError) as caught:
        parse_request(_chat([{"type": "audio", "data": "x"}]))
    assert caught.value.code == "UNSUPPORTED_CONTENT"
