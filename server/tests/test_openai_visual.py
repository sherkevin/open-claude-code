import asyncio
import json

import pytest

from app.openai_visual import call_visual_json
from app.security import AppError


class _Response:
    status_code = 200
    content = b'{"choices":[{"message":{"content":"{\\"entities\\":[{\\"label\\":\\"P-101\\"}],\\"relations\\":[]}"}}],"usage":{"total_tokens":9}}'

    def json(self):
        return json.loads(self.content)


class _Client:
    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _Client.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, endpoint, json):
        self.endpoint, self.payload = endpoint, json
        return _Response()


def test_visual_adapter_uses_openai_image_blocks_and_redacted_evidence(monkeypatch):
    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", _Client)
    result = asyncio.run(call_visual_json(
        base_url="http://127.0.0.1:3425/v1",
        model="tencent-cloud/deepseek-v4-flash-202605",
        image=b"fake-png",
        mime_type="image/png",
        prompt="只返回 JSON",
        auth_token="do-not-log",
    ))
    assert result["json"]["entities"][0]["label"] == "P-101"
    assert len(result["response_sha256"]) == 64
    assert result["usage"] == {"total_tokens": 9}
    assert _Client.last.endpoint.endswith("/chat/completions")
    assert _Client.last.payload["model"] == "tencent-cloud/deepseek-v4-flash-202605"
    image_url = _Client.last.payload["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url.startswith("data:image/png;base64,")
    assert _Client.last.kwargs["headers"]["authorization"] == "Bearer do-not-log"
    assert "do-not-log" not in json.dumps(result, ensure_ascii=False)


def test_visual_adapter_rejects_oversized_input_before_network(monkeypatch):
    called = False

    class Never:
        def __init__(self, **_):
            nonlocal called
            called = True

    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", Never)
    with pytest.raises(AppError) as error:
        asyncio.run(call_visual_json(base_url="http://127.0.0.1:3425/v1", model="vision", image=b"x" * (8 * 1024 * 1024 + 1), mime_type="image/png", prompt="x"))
    assert error.value.code == "VISUAL_INPUT_TOO_LARGE" and called is False


def test_visual_adapter_rejects_invalid_endpoint_before_network(monkeypatch):
    called = False

    class Never:
        def __init__(self, **_):
            nonlocal called
            called = True

    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", Never)
    with pytest.raises(AppError) as error:
        asyncio.run(call_visual_json(
            base_url="file:///tmp/provider?secret=1", model="vision", image=b"x", mime_type="image/png", prompt="x"))
    assert error.value.code == "VISUAL_ENDPOINT_INVALID"
    assert called is False


def test_visual_adapter_rejects_direct_external_provider_before_network(monkeypatch):
    called = False

    class Never:
        def __init__(self, **_):
            nonlocal called
            called = True

    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", Never)
    with pytest.raises(AppError) as error:
        asyncio.run(call_visual_json(
            base_url="https://api.deepseek.com/anthropic", model="vision", image=b"x", mime_type="image/png", prompt="x"))
    assert error.value.code == "TOKENHUB_ROUTE_REQUIRED"
    assert called is False


def test_visual_adapter_rejects_oversized_response(monkeypatch):
    class HugeResponse:
        status_code = 200
        content = b"{" + b"x" * (1024 * 1024) + b"}"

        def json(self):
            return {}

    class HugeClient(_Client):
        async def post(self, endpoint, json):
            return HugeResponse()

    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", HugeClient)
    with pytest.raises(AppError) as error:
        asyncio.run(call_visual_json(
            base_url="http://127.0.0.1:3425/v1", model="vision", image=b"x", mime_type="image/png", prompt="x"))
    assert error.value.code == "VISUAL_MODEL_RESPONSE_TOO_LARGE"


def test_visual_adapter_rejects_oversized_message_content(monkeypatch):
    class LongResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"choices": [{"message": {"content": "x" * 16001}}]}

    class LongClient(_Client):
        async def post(self, endpoint, json):
            return LongResponse()

    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", LongClient)
    with pytest.raises(AppError) as error:
        asyncio.run(call_visual_json(
            base_url="http://127.0.0.1:3425/v1", model="vision", image=b"x", mime_type="image/png", prompt="x"))
    assert error.value.code == "VISUAL_MODEL_RESPONSE_TOO_LARGE"


def test_visual_adapter_maps_auth_and_timeout_statuses(monkeypatch):
    class ErrorResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.content = b"{}"

        def json(self):
            return {}

    class ErrorClient(_Client):
        status = 401

        async def post(self, endpoint, json):
            return ErrorResponse(self.status)

    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", ErrorClient)
    with pytest.raises(AppError) as auth_error:
        asyncio.run(call_visual_json(
            base_url="http://127.0.0.1:3425/v1", model="vision", image=b"x", mime_type="image/png", prompt="x"))
    assert auth_error.value.code == "VISUAL_MODEL_AUTHORIZATION"

    ErrorClient.status = 408
    with pytest.raises(AppError) as timeout_error:
        asyncio.run(call_visual_json(
            base_url="http://127.0.0.1:3425/v1", model="vision", image=b"x", mime_type="image/png", prompt="x"))
    assert timeout_error.value.code == "VISUAL_MODEL_TIMEOUT"


def test_visual_adapter_requires_structured_choice_content(monkeypatch):
    class EmptyResponse:
        status_code = 200
        content = b'{"choices":[]}'

        def json(self):
            return {"choices": []}

    class EmptyClient(_Client):
        async def post(self, endpoint, json):
            return EmptyResponse()

    monkeypatch.setattr("app.openai_visual.httpx.AsyncClient", EmptyClient)
    with pytest.raises(AppError) as error:
        asyncio.run(call_visual_json(
            base_url="http://127.0.0.1:3425/v1", model="vision", image=b"x", mime_type="image/png", prompt="x"))
    assert error.value.code == "VISUAL_MODEL_RESPONSE_INVALID"
