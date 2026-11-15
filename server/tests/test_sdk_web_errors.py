"""Host workspace errors reach SDK completion events without external text."""
import asyncio
import json

import pytest

from app.config import Settings
from app.runtime import SDKRuntime
from app.security import AppError
from app.service import Service
from app.workspace_tools import TOOL_CALL_ID, build_workspace_tools


@pytest.fixture
def sdk_web(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, runtime_type="claude", provider_settings="", model_catalog="")
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "synthetic-callback-key", "ANTHROPIC_BASE_URL": "https://gateway.example"}
    service = Service(settings)
    user = service.create_user({"username": "web-errors", "display_name": "Web errors", "password": "synthetic-test-password", "role": "admin"})
    assistant = service.save_assistant({"name": "Web", "web_search_enabled": True}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Web"}, user)
    result, _ = service.prepare_run(conversation["id"], {"text": "read", "file_ids": [], "client_request_id": "error-test"}, user)
    service.db.execute("UPDATE runs SET status='running' WHERE id=?", (result["run_id"],))
    captured, events = {}, []

    class Client:
        def __init__(self, options):
            captured["options"] = options
        async def connect(self):
            pass
        async def disconnect(self):
            pass

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)
    async def emit(kind, data):
        events.append((kind, data))
    runtime = SDKRuntime(service, conversation["id"], service.runtime_config(conversation["id"]), None, emit)
    runtime.current_run = service.require("runs", result["run_id"])
    yield runtime, captured, events
    service.db.close()


def exercise(sdk_web, monkeypatch, error, *, tool="web_read", call_id="read-1"):
    runtime, captured, events = sdk_web
    async def fail(*args, **kwargs):
        if callable(error):
            return await error()
        raise error
    monkeypatch.setattr("app.web_tools.WebTools.read_for_run" if tool == "web_read" else "app.web_tools.WebTools.search_for_run", fail)

    async def scenario():
        await runtime.connect()
        runtime.tool_run_ids[call_id] = runtime.current_run["id"]
        handler = next(item.handler for item in build_workspace_tools(runtime) if item.name == tool)
        token = TOOL_CALL_ID.set(call_id)
        try:
            response = await handler({"url": "https://example.com/public", "query": ""} if tool == "web_read" else {"query": "public", "limit": 1})
        finally:
            TOOL_CALL_ID.reset(token)
        output = await captured["options"].hooks["PostToolUse"][0].hooks[0](
            {"tool_name": f"mcp__workspace__{tool}", "tool_response": response}, call_id, {})
        await runtime.close()
        return response, output
    response, output = asyncio.run(scenario())
    event = next(data for kind, data in events if kind == "tool.completed")
    return response, output, event


@pytest.mark.parametrize("code,message,expected", [
    ("WEB_FETCH_FAILED", "网页返回 HTTP 403，可尝试其他来源", "HTTP 403"),
    ("WEB_FETCH_FAILED", "网页返回 HTTP 404，可尝试其他来源", "HTTP 404"),
    ("WEB_FETCH_FAILED", "网页返回 HTTP 429，可尝试其他来源", "HTTP 429"),
    ("WEB_TIMEOUT", "private internal timeout context", "超时"),
    ("WEB_NETWORK_UNAVAILABLE", "private connection context", "网络"),
    ("WEB_DNS_UNAVAILABLE", "private resolver context", "解析"),
    ("WEB_URL_DENIED", "private address", "公开"),
    ("CAPABILITY_DOMAIN_DENIED", "private domain rule", "允许"),
    ("CAPABILITY_EGRESS_DENIED", "private rule", "禁止"),
    ("CAPABILITY_TIMEOUT", "private upstream timeout", "超时"),
    ("WEB_FORMAT_UNSUPPORTED", "private content type", "文本"),
    ("WEB_EMPTY", "private body", "正文"),
])
def test_host_app_error_is_visible_without_publishing_its_arbitrary_message(sdk_web, monkeypatch, code, message, expected):
    response, output, event = exercise(sdk_web, monkeypatch, AppError(502, code, message))
    assert event["success"] is False
    assert event["error_code"] == code
    assert expected in event["error_message"]
    assert event["source_ids"] == [] and event["result_facts"] == {}
    assert "private" not in json.dumps([response, output, event])


@pytest.mark.parametrize("code,message", [
    ("UNRECOGNIZED_EXTERNAL_ERROR", "Bearer private-token https://private.example/path"),
    ("WEB_FETCH_FAILED", "网页返回 HTTP 403，可尝试其他来源\nprivate-token"),
])
def test_unknown_or_extended_error_messages_never_leak(sdk_web, monkeypatch, code, message):
    response, output, event = exercise(sdk_web, monkeypatch, AppError(502, code, message))
    assert "private" not in json.dumps([response, output, event])
    assert "HTTP 403" not in event["error_message"]
    assert event["error_code"] == ("WEB_FETCH_FAILED" if code == "WEB_FETCH_FAILED" else "WEB_READ_FAILED")


def test_search_failure_has_independent_safe_summary(sdk_web, monkeypatch):
    _, _, event = exercise(sdk_web, monkeypatch, AppError(504, "WEB_SEARCH_TIMEOUT", "private details"), tool="web_search")
    assert event["error_code"] == "WEB_SEARCH_TIMEOUT" and "搜索" in event["error_message"]


@pytest.mark.parametrize("tool_name", ["mcp__workspace__web_read", "mcp__remote__web_read"])
def test_untrusted_tool_response_cannot_supply_a_host_error(sdk_web, tool_name):
    runtime, captured, events = sdk_web
    async def scenario():
        await runtime.connect()
        runtime.tool_run_ids["untrusted"] = runtime.current_run["id"]
        await captured["options"].hooks["PostToolUseFailure"][0].hooks[0]({
            "tool_name": tool_name, "hook_event_name": "PostToolUseFailure",
            "error": "private exception stack",
            "tool_response": {"isError": True, "error_code": "WEB_TIMEOUT", "error_message": "private-token",
                              "content": [{"type": "text", "text": "网页返回 HTTP 403，可尝试其他来源"}]},
        }, "untrusted", {})
        await runtime.close()
    asyncio.run(scenario())
    event = next(data for kind, data in events if kind == "tool.completed")
    assert "private" not in json.dumps(event)
    assert "HTTP 403" not in json.dumps(event)
    if tool_name.startswith("mcp__workspace__"):
        assert event["error_code"] == "WEB_READ_FAILED"
    else:
        assert "error_code" not in event


@pytest.mark.parametrize("case", ["wrong_call", "wrong_run", "late_callback"])
def test_host_error_receipt_is_bound_to_run_and_call(sdk_web, case):
    runtime, captured, events = sdk_web
    run_id = runtime.current_run["id"]
    async def scenario():
        await runtime.connect()
        runtime.tool_run_ids["call"] = run_id
        runtime.record_workspace_error("web_read", "other" if case == "wrong_call" else "call",
            "other-run" if case == "wrong_run" else run_id, AppError(504, "WEB_TIMEOUT", "private"))
        if case == "late_callback":
            runtime.current_run = {"id": "another-run"}
        await captured["options"].hooks["PostToolUse"][0].hooks[0](
            {"tool_name": "mcp__workspace__web_read", "tool_response": {"isError": True}}, "call", {})
        await runtime.close()
    asyncio.run(scenario())
    completed = [data for kind, data in events if kind == "tool.completed"]
    if case == "late_callback":
        assert completed == []
    else:
        assert completed[0]["error_code"] == "WEB_READ_FAILED"
    assert runtime._workspace_errors == {}


@pytest.mark.parametrize("failure,code", [("timeout", "WEB_TIMEOUT"), ("network", "WEB_NETWORK_UNAVAILABLE")])
def test_actual_fetch_wrapper_classification_reaches_completion(sdk_web, monkeypatch, failure, code):
    import httpx
    from app.web_tools import fetch_public
    async def failed_fetch(*args, **kwargs):
        raise (httpx.ConnectTimeout("private address") if failure == "timeout" else httpx.ConnectError("private address"))
    monkeypatch.setattr("app.web_tools._fetch_public", failed_fetch)
    async def operation():
        return await fetch_public("https://example.com/public")
    response, output, event = exercise(sdk_web, monkeypatch, operation)
    assert event["error_code"] == code and "private" not in json.dumps([response, output, event])


def test_actual_http_403_classification_reaches_completion(sdk_web, monkeypatch):
    import httpx
    from app.web_tools import fetch_public
    class Response:
        async def __aenter__(self):
            return httpx.Response(403, content=b"private challenge body", request=httpx.Request("GET", "https://example.com"))
        async def __aexit__(self, *args):
            pass
    class HTTP:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def stream(self, *args):
            return Response()
    monkeypatch.setattr("app.web_tools.httpx.AsyncClient", HTTP)
    async def operation():
        return await fetch_public("https://example.com/public")
    response, output, event = exercise(sdk_web, monkeypatch, operation)
    assert event["error_code"] == "WEB_FETCH_FAILED" and "HTTP 403" in event["error_message"]
    assert "private" not in json.dumps([response, output, event])

