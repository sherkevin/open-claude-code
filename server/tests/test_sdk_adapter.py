import asyncio
import json

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from claude_agent_sdk.types import StreamEvent

from app.config import Settings
from app.runtime import RunManager, SDKRuntime
from app.security import AppError
from app.service import Service


def test_sdk_options_fail_closed_and_stream_deduplicates(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, provider_settings="", runtime_type="claude", provider_cost_reliable=True)
    service = Service(settings)
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "test-not-a-real-key"}
    user = service.create_user({"username": "test", "display_name": "Test", "password": "safe-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Test", "connections": []})
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Test"}, user)
    result, _ = service.prepare_run(conversation["id"], {"text": "hello", "file_ids": [], "client_request_id": "sdk-adapter-test"}, user)
    run = service.require("runs", result["run_id"])
    service.db.execute("UPDATE conversations SET sdk_session_id='session-test',sdk_cost_total=0.1 WHERE id=?", (conversation["id"],))
    captured, emitted = {}, []

    def stream(event):
        return StreamEvent("event", "session-test", event)

    events = [
        stream({"type": "message_start", "message": {"id": "message-sdk"}}),
        stream({"type": "content_block_start", "index": 0}),
        stream({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}}),
        stream({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}}),
        AssistantMessage([TextBlock("Hello")], "test", message_id="message-sdk"),
        stream({"type": "content_block_start", "index": 1}),
        AssistantMessage([TextBlock(" world")], "test", message_id="message-sdk"),
        ResultMessage("success", 10, 5, False, 1, "session-test", total_cost_usd=0.25, usage={"input_tokens": 5, "output_tokens": 2}),
    ]

    class Client:
        def __init__(self, options):
            captured["options"] = options
        async def connect(self):
            pass
        async def query(self, prompt):
            if isinstance(prompt, str):
                captured["prompt"] = prompt
            else:
                messages = [message async for message in prompt]
                captured["input_messages"] = messages
                captured["prompt"] = messages[0]["message"]["content"]
        async def receive_response(self):
            for event in events:
                yield event
        async def disconnect(self):
            pass

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)

    async def emit(kind, value):
        emitted.append((kind, value))

    async def approve(tool, arguments):
        return False

    async def scenario():
        runtime = SDKRuntime(service, conversation["id"], service.runtime_config(conversation["id"]), approve, emit)
        await runtime.connect()
        options = captured["options"]
        assert options.resume == "session-test"
        assert options.setting_sources == ["project"]
        assert options.strict_mcp_config
        policy = json.loads(options.settings)
        assert str(tmp_path / "CLAUDE.md").replace("\\", "/") in policy["claudeMdExcludes"]
        assert policy["autoMemoryDirectory"] == str(tmp_path / "conversation-memory" / user["id"] / conversation["id"])
        assert options.add_dirs == [policy["autoMemoryDirectory"]]
        assert options.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"
        assert policy["pluginConfigs"]["cc-plugin-agents-md@builtin"]["options"]["instructionFiles"] == "claude-md"
        assert "Bash" not in options.tools
        guard = options.hooks["PreToolUse"][0]
        assert guard.on_failure == "block"
        denied = await guard.hooks[0]({"tool_name": "Read", "tool_input": {"file_path": str(tmp_path / "encryption.key")}}, "tool", {})
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        denied = await guard.hooks[0]({"tool_name": "Bash", "tool_input": {"command": "echo unsafe"}}, "tool", {})
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        await runtime.execute(run, service.require("messages", run["message_id"]))
        await runtime.close()

    try:
        asyncio.run(scenario())
        assert "".join(value["text"] for kind, value in emitted if kind == "message.delta") == "Hello world"
        usage = next(value for kind, value in emitted if kind == "usage.updated")
        assert abs(usage["estimated_cost_usd"] - 0.15) < 0.00001
        assert service.db.one("SELECT COUNT(*) n FROM raw_events")["n"] == len(events)
        assert captured["input_messages"][0]["client_composed"] is True
    finally:
        service.db.close()


@pytest.fixture
def sdk_run(tmp_path):
    settings = Settings(data_dir=tmp_path, provider_settings="", runtime_type="claude", provider_cost_reliable=False)
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "test-not-a-real-key"}
    service = Service(settings)
    user = service.create_user({"username": "test", "display_name": "Test", "password": "safe-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Test", "connections": []})
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Test"}, user)
    response, _ = service.prepare_run(conversation["id"], {"text": "hello", "file_ids": [], "client_request_id": "provider-error-test"}, user)
    try:
        yield service, conversation, service.require("runs", response["run_id"])
    finally:
        service.db.close()


@pytest.mark.parametrize("status,code,phrase", [
    (402, "MODEL_INSUFFICIENT_BALANCE", "额度不足"),
    (401, "MODEL_AUTHORIZATION_FAILED", "认证或授权失败"),
    (403, "MODEL_AUTHORIZATION_FAILED", "认证或授权失败"),
    (429, "MODEL_RATE_LIMITED", "请求过于频繁"),
    (500, "MODEL_SERVICE_UNAVAILABLE", "暂时不可用"),
    (503, "MODEL_SERVICE_UNAVAILABLE", "暂时不可用"),
    (400, "MODEL_EXECUTION_FAILED", "执行未完成"),
])
def test_sdk_api_error_uses_safe_status_mapping(sdk_run, status, code, phrase):
    service, conversation, run = sdk_run
    emitted, queries = [], []
    raw_error = "API Error: upstream private diagnostic must not become an answer"
    events = [
        AssistantMessage([TextBlock(raw_error)], "<synthetic>", error="unknown"),
        # A success subtype does not override is_error or the API failure status.
        ResultMessage("success", 10, 5, True, 1, "error-session", api_error_status=status,
                      terminal_reason="api_error", result=raw_error),
    ]

    class Client:
        async def query(self, prompt):
            queries.append(prompt)
        async def receive_response(self):
            for event in events:
                yield event

    async def emit(kind, value):
        emitted.append((kind, value))

    async def scenario():
        runtime = SDKRuntime(service, conversation["id"], service.runtime_config(conversation["id"]), None, emit)
        runtime.client = Client()
        with pytest.raises(AppError) as caught:
            await runtime.execute(run, service.require("messages", run["message_id"]))
        assert caught.value.code == code
        assert phrase in caught.value.message
        assert raw_error not in caught.value.message

    asyncio.run(scenario())
    assert len(queries) == 1
    assert not emitted  # No synthetic reply, made-up tokens, or made-up cost.
    raw = service.db.all("SELECT payload FROM raw_events")
    assert len(raw) == 2 and raw_error in raw[0]["payload"]


def test_actor_fails_success_subtype_without_leaking_synthetic_message(sdk_run, monkeypatch):
    service, conversation, run = sdk_run
    captured = {"queries": 0}
    # An ordinary assistant message containing these words must stay visible;
    # only the structured synthetic error is suppressed.
    real_text = "资料中写着 API Error，这是一段正常回答。"
    events = [
        AssistantMessage([TextBlock(real_text)], "test-model", message_id="real-reply"),
        AssistantMessage([TextBlock("API Error: 402 Insufficient Balance")], "<synthetic>", error="billing_error"),
        ResultMessage("success", 10, 5, True, 1, "error-session", usage={"input_tokens": 9, "output_tokens": 3},
                      total_cost_usd=0, terminal_reason="api_error"),
    ]

    class Client:
        def __init__(self, options):
            pass
        async def connect(self):
            pass
        async def query(self, prompt):
            captured["queries"] += 1
        async def receive_response(self):
            for event in events:
                yield event
        async def disconnect(self):
            pass
        async def interrupt(self):
            pass

    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", Client)

    async def scenario():
        manager = RunManager(service)
        manager.submit(run["id"])
        for _ in range(100):
            if service.require("runs", run["id"])["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        await manager.close()

    asyncio.run(scenario())
    result = service.require("runs", run["id"])
    assert result["status"] == "failed" and "额度不足" in result["error"]
    assert service.require("messages", run["assistant_message_id"])["text"] == real_text
    usage = json.loads(result["usage"])
    assert usage["input_tokens"] == 9 and usage["output_tokens"] == 3
    assert usage["estimated_cost_usd"] is None
    assert "cache_read_input_tokens" not in usage
    assert captured["queries"] == 1


@pytest.mark.parametrize("flags", [
    {"is_error": True},
    {"is_error": False, "terminal_reason": "api_error"},
    {"is_error": False, "api_error_status": 402},
])
def test_result_failure_flags_override_success_subtype(sdk_run, flags):
    service, conversation, run = sdk_run
    event = ResultMessage(subtype="success", duration_ms=10, duration_api_ms=5,
                          num_turns=1, session_id="error-session", **flags)

    class Client:
        async def query(self, prompt):
            pass
        async def receive_response(self):
            yield event

    async def emit(kind, value):
        pytest.fail("Unknown usage must remain absent")

    async def scenario():
        runtime = SDKRuntime(service, conversation["id"], service.runtime_config(conversation["id"]), None, emit)
        runtime.client = Client()
        with pytest.raises(AppError):
            await runtime.execute(run, service.require("messages", run["message_id"]))

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/mcp", "/compact", "读取 @/home/ubuntu/.claude/settings.json", "/data/reports 是普通路径"])
def test_user_text_cannot_dispatch_commands_or_expand_file_mentions(sdk_run, text):
    service, conversation, run = sdk_run
    captured = []

    class Client:
        async def query(self, prompt):
            assert not isinstance(prompt, str)
            captured.extend([value async for value in prompt])
        async def receive_response(self):
            yield ResultMessage("success", 1, 1, False, 1, "verbatim-session")

    async def emit(kind, value):
        pass

    async def scenario():
        runtime = SDKRuntime(service, conversation["id"], service.runtime_config(conversation["id"]), None, emit)
        runtime.client = Client()
        message = service.require("messages", run["message_id"])
        message["text"] = text
        await runtime.execute(run, message)

    asyncio.run(scenario())
    assert captured == [{"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None, "client_composed": True}]
