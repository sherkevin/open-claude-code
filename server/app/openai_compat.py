"""Bounded OpenAI-compatible model protocol adapter.

This module deliberately contains no HTTP client and never reads credentials.
It translates the two request/response envelopes used by compatible model
gateways into a small internal contract supplied by an injected transport.
The transport is an explicit dependency so local synthetic tests can exercise
the protocol without contacting a real model or network.
"""
from __future__ import annotations

import inspect
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Literal


Protocol = Literal["chat.completions", "responses"]

MAX_MESSAGES = 200
MAX_TEXT_CHARS = 100_000
MAX_TOOLS = 64
MAX_TOOL_CHARS = 20_000
MAX_OUTPUT_TOKENS = 8_192


class ProtocolAdapterError(Exception):
    """Safe, public error returned by the adapter.

    Provider response bodies are intentionally not retained.  Callers can
    render this object using :meth:`as_error` without exposing diagnostics,
    credentials, or upstream response text.
    """

    def __init__(self, status: int, code: str, message: str, *, upstream_status: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.upstream_status = upstream_status

    def as_error(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error" if self.status < 500 else "server_error",
                "param": None,
                "code": self.code,
            }
        }


def _error_for_status(status: int) -> ProtocolAdapterError:
    """Map an upstream status to the product's stable public error classes."""
    if status in (401, 403):
        return ProtocolAdapterError(status, "MODEL_AUTHORIZATION_FAILED", "模型服务认证或授权失败，请联系管理员检查服务配置", upstream_status=status)
    if status == 404:
        return ProtocolAdapterError(404, "MODEL_NOT_FOUND", "请求的模型或资源不存在", upstream_status=status)
    if status in (408, 504):
        return ProtocolAdapterError(504, "MODEL_TIMEOUT", "模型服务响应超时，请稍后重试", upstream_status=status)
    if status == 429:
        return ProtocolAdapterError(429, "MODEL_RATE_LIMITED", "模型服务请求过于频繁或已达到用量限制，请稍后重试", upstream_status=status)
    if status == 402:
        return ProtocolAdapterError(402, "MODEL_INSUFFICIENT_BALANCE", "模型服务额度不足，请联系管理员充值或调整服务配置", upstream_status=status)
    if 500 <= status <= 599:
        return ProtocolAdapterError(503, "MODEL_SERVICE_UNAVAILABLE", "模型服务暂时不可用，请稍后重试", upstream_status=status)
    return ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型执行未完成，请联系管理员检查模型配置或用量限制", upstream_status=status)


@dataclass(frozen=True)
class NormalizedMessage:
    role: str
    content: Any
    name: str | None = None
    tool_call_id: str | None = None


@dataclass(frozen=True)
class NormalizedTool:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class NormalizedRequest:
    protocol: Protocol
    model: str
    messages: tuple[NormalizedMessage, ...]
    tools: tuple[NormalizedTool, ...] = ()
    max_output_tokens: int | None = None
    temperature: float | None = None
    stream: bool = False
    request_id: str = ""


@dataclass(frozen=True)
class NormalizedToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class NormalizedUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None

    def public(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for key, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("total_tokens", self.total_tokens),
            ("cache_read_input_tokens", self.cache_read_input_tokens),
        ):
            if type(value) is int and value >= 0:
                values[key] = value
        return values


@dataclass(frozen=True)
class NormalizedResult:
    """Transport result independent from either public protocol envelope."""

    text: str = ""
    tool_calls: tuple[NormalizedToolCall, ...] = ()
    finish_reason: str = "stop"
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    chunks: tuple[str, ...] = ()


def _bounded_text(value: Any, field_name: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", f"{field_name} 必须是字符串")
    if len(value) > limit:
        raise ProtocolAdapterError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制")
    return value


def _bounded_json(value: Any, field_name: str, *, limit: int = MAX_TOOL_CHARS) -> Any:
    """Bound tool metadata without recursively accepting arbitrary objects."""
    if isinstance(value, (dict, list)):
        # repr is only a bounded size check; the original structured value is
        # retained for the injected transport and never serialized to logs.
        try:
            size = len(repr(value))
        except (RecursionError, MemoryError):
            raise ProtocolAdapterError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制") from None
        if size > limit:
            raise ProtocolAdapterError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制")
        return value
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > limit:
            raise ProtocolAdapterError(413, "REQUEST_TOO_LARGE", f"{field_name} 超过长度限制")
        return value
    raise ProtocolAdapterError(400, "INVALID_REQUEST", f"{field_name} 格式无效")


def _content(value: Any, field_name: str) -> Any:
    if isinstance(value, str):
        return _bounded_text(value, field_name)
    if not isinstance(value, list):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", f"{field_name} 格式无效")
    if len(value) > 64:
        raise ProtocolAdapterError(400, "INVALID_REQUEST", f"{field_name} 片段过多")
    blocks: list[dict[str, Any]] = []
    for index, block in enumerate(value):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ProtocolAdapterError(400, "INVALID_REQUEST", f"{field_name}[{index}] 格式无效")
        kind = block["type"]
        if kind in {"text", "input_text", "output_text"}:
            text = block.get("text")
            blocks.append({"type": kind, "text": _bounded_text(text, f"{field_name}[{index}].text")})
        elif kind in {"image_url", "input_image"}:
            image = block.get("image_url", block.get("image"))
            if isinstance(image, dict):
                image = dict(image)
                if "url" in image:
                    image["url"] = _bounded_text(image["url"], f"{field_name}[{index}].url", limit=8192)
            elif isinstance(image, str):
                image = _bounded_text(image, f"{field_name}[{index}].image", limit=8192)
            else:
                raise ProtocolAdapterError(400, "INVALID_REQUEST", f"{field_name}[{index}] 图片格式无效")
            blocks.append({"type": kind, "image": image})
        else:
            # Unknown content parts are rejected rather than forwarded to an
            # upstream provider with an ambiguous security meaning.
            raise ProtocolAdapterError(400, "UNSUPPORTED_CONTENT", f"{field_name}[{index}] 内容类型不受支持")
    return blocks


def _message(role: Any, content: Any, index: int, *, name: Any = None, tool_call_id: Any = None) -> NormalizedMessage:
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        raise ProtocolAdapterError(400, "INVALID_REQUEST", f"messages[{index}].role 无效")
    if role == "tool" and not isinstance(tool_call_id, str):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", f"messages[{index}].tool_call_id 缺失")
    if name is not None and not isinstance(name, str):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", f"messages[{index}].name 无效")
    # Chat Completions uses ``content: null`` on assistant messages that only
    # contain tool calls. Preserve that valid shape instead of rejecting it;
    # the surrounding tool-call fields remain bounded by the caller contract.
    normalized_content = None if content is None and role == "assistant" else _content(content, f"messages[{index}].content")
    return NormalizedMessage(role, normalized_content, name=name, tool_call_id=tool_call_id)


def _tools(value: Any, protocol: Protocol) -> tuple[NormalizedTool, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_TOOLS:
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "tools 数量超出限制")
    result: list[NormalizedTool] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict) or item.get("type") != "function":
            raise ProtocolAdapterError(400, "INVALID_REQUEST", f"tools[{index}] 仅支持 function")
        function = item.get("function") if protocol == "chat.completions" else item
        if not isinstance(function, dict):
            raise ProtocolAdapterError(400, "INVALID_REQUEST", f"tools[{index}] function 无效")
        name = function.get("name")
        if not isinstance(name, str) or not (1 <= len(name) <= 128):
            raise ProtocolAdapterError(400, "INVALID_REQUEST", f"tools[{index}].name 无效")
        description = function.get("description", "")
        if not isinstance(description, str):
            raise ProtocolAdapterError(400, "INVALID_REQUEST", f"tools[{index}].description 无效")
        parameters = function.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(parameters, dict):
            raise ProtocolAdapterError(400, "INVALID_REQUEST", f"tools[{index}].parameters 无效")
        _bounded_json(parameters, f"tools[{index}].parameters")
        result.append(NormalizedTool(name, description[:2000], parameters))
    names = [item.name for item in result]
    if len(names) != len(set(names)):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "tools 不能包含重复名称")
    return tuple(result)


def parse_request(payload: Any, protocol: Protocol | None = None, *, allowed_models: set[str] | None = None) -> NormalizedRequest:
    if not isinstance(payload, dict):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "请求必须是 JSON 对象")
    has_messages, has_input = "messages" in payload, "input" in payload
    if protocol is None:
        if has_messages == has_input:
            raise ProtocolAdapterError(400, "INVALID_REQUEST", "必须明确使用 messages 或 input 之一")
        protocol = "chat.completions" if has_messages else "responses"
    if protocol not in {"chat.completions", "responses"}:
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "协议类型不受支持")
    model = payload.get("model")
    if not isinstance(model, str) or not (1 <= len(model) <= 200):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "model 必须是非空字符串")
    if allowed_models is not None and model not in allowed_models:
        raise ProtocolAdapterError(404, "MODEL_NOT_ALLOWED", "请求的模型未被管理员授权")
    if protocol == "chat.completions":
        raw_messages = payload.get("messages")
    else:
        raw_messages = payload.get("input")
        if isinstance(raw_messages, str):
            raw_messages = [{"role": "user", "content": raw_messages}]
    if not isinstance(raw_messages, list) or not (1 <= len(raw_messages) <= MAX_MESSAGES):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "消息数量无效")
    messages: list[NormalizedMessage] = []
    for index, item in enumerate(raw_messages):
        if isinstance(item, str):
            item = {"role": "user", "content": item}
        if not isinstance(item, dict):
            raise ProtocolAdapterError(400, "INVALID_REQUEST", f"messages[{index}] 格式无效")
        messages.append(_message(item.get("role"), item.get("content"), index, name=item.get("name"), tool_call_id=item.get("tool_call_id")))
    raw_max = payload.get("max_tokens", payload.get("max_output_tokens"))
    if raw_max is not None and (type(raw_max) is not int or not 1 <= raw_max <= MAX_OUTPUT_TOKENS):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "max_tokens 超出限制")
    temperature = payload.get("temperature")
    if temperature is not None and (type(temperature) not in (int, float) or not 0 <= temperature <= 2):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "temperature 超出限制")
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise ProtocolAdapterError(400, "INVALID_REQUEST", "stream 必须是布尔值")
    return NormalizedRequest(protocol, model, tuple(messages), _tools(payload.get("tools"), protocol), raw_max, float(temperature) if temperature is not None else None, stream, secrets.token_hex(8))


def _usage(value: Any) -> NormalizedUsage:
    if not isinstance(value, dict):
        return NormalizedUsage()
    def integer(*keys: str) -> int | None:
        for key in keys:
            candidate = value.get(key)
            if type(candidate) is int and candidate >= 0:
                return candidate
        return None
    return NormalizedUsage(
        input_tokens=integer("input_tokens", "prompt_tokens"),
        output_tokens=integer("output_tokens", "completion_tokens"),
        total_tokens=integer("total_tokens"),
        cache_read_input_tokens=integer("cache_read_input_tokens", "cache_read_tokens"),
    )


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return _bounded_json(value, "tool_arguments")
    if isinstance(value, str):
        if len(value) > MAX_TOOL_CHARS:
            raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型返回工具参数超出限制")
        try:
            parsed = __import__("json").loads(value)
        except (ValueError, TypeError, RecursionError):
            raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型返回工具参数格式无效") from None
        if isinstance(parsed, dict):
            return parsed
    raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型返回工具参数格式无效")


def _result(value: Any) -> NormalizedResult:
    if isinstance(value, NormalizedResult):
        return value
    if not isinstance(value, dict):
        raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型返回格式无效")
    # A synthetic transport may return the small internal result contract, but
    # accepting either compatible response envelope here keeps the adapter
    # useful for a future approved gateway without coupling this module to an
    # HTTP client. Only documented output fields are read.
    if isinstance(value.get("choices"), list) and value["choices"]:
        choice = value["choices"][0]
        message = choice.get("message", {}) if isinstance(choice, dict) else {}
        calls = []
        for item in message.get("tool_calls", []) if isinstance(message, dict) else []:
            function = item.get("function", {}) if isinstance(item, dict) else {}
            arguments = _arguments(function.get("arguments", {}) if isinstance(function, dict) else {})
            if isinstance(item, dict) and isinstance(function, dict) and isinstance(function.get("name"), str) and isinstance(arguments, dict):
                calls.append({"id": item.get("id"), "name": function["name"], "arguments": arguments})
        return _result({"text": message.get("content", "") if isinstance(message, dict) and isinstance(message.get("content", ""), str) else "",
                        "tool_calls": calls, "finish_reason": choice.get("finish_reason", "stop") if isinstance(choice, dict) else "stop", "usage": value.get("usage")})
    if isinstance(value.get("output"), list):
        text_parts, calls = [], []
        for item in value["output"]:
            if not isinstance(item, dict):
                continue
            for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
            if item.get("type") == "function_call" and isinstance(item.get("name"), str):
                arguments = _arguments(item.get("arguments", {}))
                calls.append({"id": item.get("call_id") or item.get("id"), "name": item["name"], "arguments": arguments})
        return _result({"text": "".join(text_parts), "tool_calls": calls, "finish_reason": "tool_calls" if calls else "stop", "usage": value.get("usage")})
    text = value.get("text", "")
    if not isinstance(text, str) or len(text) > MAX_TEXT_CHARS:
        raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型返回文本格式无效")
    calls: list[NormalizedToolCall] = []
    raw_calls = value.get("tool_calls", [])
    if not isinstance(raw_calls, list) or len(raw_calls) > MAX_TOOLS:
        raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型返回工具调用格式无效")
    for index, call in enumerate(raw_calls):
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", f"模型返回工具调用 {index} 格式无效")
        call_id = call.get("id") if isinstance(call.get("id"), str) else f"call_{secrets.token_hex(6)}"
        calls.append(NormalizedToolCall(call_id, call["name"], _arguments(call.get("arguments", {}))))
    reason = value.get("finish_reason", "tool_calls" if calls else "stop")
    if reason not in {"stop", "length", "tool_calls", "content_filter"}:
        reason = "stop"
    chunks = value.get("chunks", ())
    if chunks and (not isinstance(chunks, (list, tuple)) or not all(isinstance(item, str) for item in chunks)):
        raise ProtocolAdapterError(502, "MODEL_EXECUTION_FAILED", "模型流式返回格式无效")
    return NormalizedResult(text, tuple(calls), reason, _usage(value.get("usage")), tuple(chunks))


Transport = Callable[[NormalizedRequest], Any]


class OpenAICompatibleAdapter:
    """Translate compatible envelopes through an injected local transport."""

    def __init__(self, transport: Transport, *, allowed_models: set[str] | None = None):
        self.transport = transport
        self.allowed_models = set(allowed_models) if allowed_models is not None else None

    async def _call(self, request: NormalizedRequest) -> NormalizedResult:
        try:
            value = self.transport(request)
            if inspect.isawaitable(value):
                value = await value
            return _result(value)
        except ProtocolAdapterError:
            raise
        except TimeoutError:
            raise _error_for_status(504) from None
        except (ConnectionError, OSError):
            raise _error_for_status(503) from None
        except Exception as exc:
            # Do not expose transport exception text, which may contain a URL,
            # credential or private provider diagnostic.
            status = getattr(exc, "status_code", None)
            if type(status) is int and 100 <= status <= 599:
                raise _error_for_status(status) from None
            raise _error_for_status(502) from None

    async def complete(self, payload: Any, protocol: Protocol | None = None) -> dict[str, Any]:
        request = parse_request(payload, protocol, allowed_models=self.allowed_models)
        if request.stream:
            raise ProtocolAdapterError(400, "STREAM_REQUIRED", "流式请求请使用 stream_events")
        result = await self._call(request)
        return _render_response(request, result)

    async def stream_events(self, payload: Any, protocol: Protocol | None = None) -> AsyncIterator[dict[str, Any]]:
        request = parse_request(payload, protocol, allowed_models=self.allowed_models)
        if not request.stream:
            raise ProtocolAdapterError(400, "STREAM_DISABLED", "非流式请求请使用 complete")
        result = await self._call(request)
        chunks = result.chunks or tuple(result.text[index:index + 256] for index in range(0, len(result.text), 256))
        if request.protocol == "chat.completions":
            response_id = f"chatcmpl-{secrets.token_hex(12)}"
            for index, chunk in enumerate(chunks):
                yield {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": request.model,
                       "choices": [{"index": 0, "delta": {"role": "assistant"} if index == 0 else {"content": chunk}, "finish_reason": None}]}
            yield {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": request.model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": result.finish_reason}], "usage": _chat_usage(result.usage)}
            return
        response_id = f"resp_{secrets.token_hex(12)}"
        yield {"type": "response.created", "response": {"id": response_id, "object": "response", "model": request.model, "status": "in_progress"}}
        for chunk in chunks:
            yield {"type": "response.output_text.delta", "item_id": f"msg_{response_id[5:]}", "delta": chunk}
        yield {"type": "response.completed", "response": _responses_body(response_id, request, result)}


def _chat_usage(usage: NormalizedUsage) -> dict[str, int]:
    values = usage.public()
    return {"prompt_tokens": values["input_tokens"], "completion_tokens": values["output_tokens"], "total_tokens": values["total_tokens"]} if {"input_tokens", "output_tokens", "total_tokens"} <= values.keys() else {}


def _responses_usage(usage: NormalizedUsage) -> dict[str, int]:
    values = usage.public()
    output: dict[str, int] = {}
    for source, target in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"), ("total_tokens", "total_tokens"), ("cache_read_input_tokens", "input_cached_tokens")):
        if source in values:
            output[target] = values[source]
    return output


def _render_response(request: NormalizedRequest, result: NormalizedResult) -> dict[str, Any]:
    if request.protocol == "chat.completions":
        response_id = f"chatcmpl-{secrets.token_hex(12)}"
        message: dict[str, Any] = {"role": "assistant", "content": result.text or None}
        if result.tool_calls:
            message["tool_calls"] = [{"id": item.id, "type": "function", "function": {"name": item.name, "arguments": __import__("json").dumps(item.arguments, ensure_ascii=False, separators=(",", ":"))}} for item in result.tool_calls]
        return {"id": response_id, "object": "chat.completion", "created": int(time.time()), "model": request.model,
                "choices": [{"index": 0, "message": message, "finish_reason": result.finish_reason}], "usage": _chat_usage(result.usage)}
    response_id = f"resp_{secrets.token_hex(12)}"
    return _responses_body(response_id, request, result)


def _responses_body(response_id: str, request: NormalizedRequest, result: NormalizedResult) -> dict[str, Any]:
    output: list[dict[str, Any]] = []
    if result.text:
        output.append({"id": f"msg_{response_id[5:]}", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": result.text, "annotations": []}]})
    for call in result.tool_calls:
        output.append({"id": call.id, "type": "function_call", "call_id": call.id, "name": call.name, "arguments": __import__("json").dumps(call.arguments, ensure_ascii=False, separators=(",", ":"))})
    return {"id": response_id, "object": "response", "created_at": int(time.time()), "model": request.model,
            "status": "completed", "output": output, "output_text": result.text, "usage": _responses_usage(result.usage),
            "incomplete_details": None if result.finish_reason == "stop" else {"reason": result.finish_reason}}


__all__ = [
    "MAX_MESSAGES", "MAX_OUTPUT_TOKENS", "NormalizedMessage", "NormalizedRequest", "NormalizedResult",
    "NormalizedTool", "NormalizedToolCall", "NormalizedUsage", "OpenAICompatibleAdapter", "ProtocolAdapterError",
    "parse_request",
]
