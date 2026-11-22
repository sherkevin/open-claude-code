"""Harness business tools using the existing official Python MCP transport.

Only the trusted host holds routes and credentials. Model-facing definitions
describe their source and original tool names, while dispatch uses bounded
aliases and approval and evidence retain the canonical tool name.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import anyio
import httpx
from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from referencing import Registry
from referencing.exceptions import NoSuchResource

from .db import dump
from .security import AppError, redact, validate_mcp_url

MAX_ARGUMENT_BYTES = 512_000
MAX_RESPONSE_BYTES = 1_000_000
# A transport limit is not a model-input budget. This counts UTF-8 bytes,
# never provider tokens, and refuses the whole result instead of truncating it.
MAX_MODEL_RESULT_BYTES = 32_000
MAX_SCHEMA_BYTES = 128_000
MCP_TIMEOUT_SECONDS = 60
_transport_logging = ContextVar("harness_mcp_transport_logging", default=False)


class TransportLogFilter(logging.Filter):
    def filter(self, record):
        if _transport_logging.get():
            # Upstream logs can embed untrusted protocol bodies, response
            # headers and exception input before result sanitization runs.
            record.msg = "MCP transport diagnostic withheld; see sanitized host tool result"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


_transport_log_filter = TransportLogFilter()


@contextmanager
def transport_logs():
    # Filters are installed once per logger; the context flag is inherited by
    # this call's AnyIO children, never by unrelated concurrent requests.
    names = {"client", "mcp.client.streamable_http", "mcp.shared.tool_name_validation", "httpx",
             "httpcore.connection", "httpcore.http11", "httpcore.http2", "httpcore.proxy", "httpcore.socks"}
    names.update(name for name in tuple(logging.Logger.manager.loggerDict)
                 if name.startswith(("mcp.", "httpcore.")))
    for name in names:
        logging.getLogger(name).addFilter(_transport_log_filter)
    token = _transport_logging.set(True)
    try:
        yield
    finally:
        _transport_logging.reset(token)


def external_tool_name(canonical):
    return "external_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def include_structured_text(body):
    """Expose structured-only MCP results without repeating an existing copy.

    Harness renders content blocks, not structuredContent. Keep all distinct
    blocks and the structured payload for provenance; identical complete JSON
    copies appear once. Append text only if no block contains that whole value.
    This is exact representation de-duplication, not summarization or row merging.
    """
    structured = body.get("structuredContent")
    if not isinstance(structured, dict):
        return body
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def invalid_constant(_):
        raise ValueError("non-finite JSON value")

    def signature(text):
        parsed = json.loads(text, object_pairs_hook=unique_object,
                            parse_int=Decimal, parse_float=Decimal,
                            parse_constant=invalid_constant)

        def typed(value):
            if isinstance(value, dict):
                return ("object", tuple((key, typed(item)) for key, item in sorted(value.items())))
            if isinstance(value, list):
                return ("array", tuple(typed(item) for item in value))
            return (type(value).__name__, value)
        return typed(parsed)

    canonical = signature(encoded(structured))
    content, found = [], False
    for block in body.get("content", []):
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            content.append(block)
            continue
        try:
            # Decimal comparison avoids rounding text to the same float;
            # typed signatures also keep True distinct from the number 1.
            if signature(block["text"]) == canonical:
                if not found:
                    content.append(block)
                found = True
                continue
        except (ValueError, TypeError, RecursionError, InvalidOperation):
            pass
        content.append(block)
    if not found:
        content.append({"type": "text", "text": dump(structured)})
    return {**body, "content": content}


def enforce_model_result_budget(body):
    """Reject excessive model-visible results without modifying any rows.

    The same complete structured payload stays available for evidence and UI.
    Only explicit tabular containers get row/column limits; arbitrary prose is
    bounded by bytes and is never parsed as a business table.
    """
    from .business_results import MAX_COLUMNS, MAX_ROWS, table_payload
    content = body.get("content", [])
    if not isinstance(content, list) or any(not isinstance(item, dict)
            or item.get("type") != "text" or not isinstance(item.get("text"), str)
            for item in content):
        raise AppError(422, "MCP_RESULT_UNSUPPORTED", "业务工具返回了尚未验收的非文本内容")
    structured = body.get("structuredContent", body.get("structured_content"))
    pair = table_payload(structured)
    if pair is not None:
        payload, rows = pair
        columns = payload.get("columns")
        row_width = max((len(row) for row in rows if isinstance(row, dict)), default=0)
        if len(rows) > MAX_ROWS or row_width > MAX_COLUMNS or isinstance(columns, list) and len(columns) > MAX_COLUMNS:
            raise AppError(422, "MCP_RESULT_TOO_LARGE", "业务结果超过 200 行或 24 列上限，未送入模型；本次未取得可用结果，匹配记录、完整性与原因均未核实。请使用过滤、后台聚合或分页后重试，不能据截取样本计算全量。")
    try:
        size = len("\n".join(item["text"] for item in content).encode("utf-8"))
        structured_size = len(encoded(structured).encode("utf-8")) if structured is not None else 0
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise AppError(422, "MCP_RESULT_INVALID", "业务结果不是可核验的有限 JSON 数据") from None
    if max(size, structured_size) > MAX_MODEL_RESULT_BYTES:
        raise AppError(422, "MCP_RESULT_TOO_LARGE", "业务结果超过 32000 字节入模上限，未送入模型；本次未取得可用结果，匹配记录、完整性与原因均未核实。请缩小字段和时间范围，或在业务系统先聚合后重试。未自动截断或计算全量。")
    return size


def sdk_business_result(response, credential=None):
    """Normalize known SDK MCP output shapes for a pre-model replacement.

    Raw text is not parsed into business rows. The SDK can expose either an
    MCP envelope, content blocks, or plain text; unknown shapes fail closed.
    """
    if isinstance(response, dict):
        if "content" in response or "structuredContent" in response or "structured_content" in response:
            body = {**response, "content": response.get("content", [])}
            if "structuredContent" not in body and isinstance(body.get("structured_content"), dict):
                body["structuredContent"] = body.pop("structured_content")
        else:
            body = {"content": [{"type": "text", "text": encoded(response)}]}
    elif isinstance(response, list):
        body = {"content": response}
    elif isinstance(response, str):
        body = {"content": [{"type": "text", "text": response}]}
    else:
        raise AppError(422, "MCP_RESULT_UNSUPPORTED", "业务工具回包格式未核验，未作为业务结果使用")
    body = include_structured_text(body)
    body = clean_mcp_value(body, credential)
    enforce_model_result_budget(body)
    return body


def contains_credential(value, credential):
    if isinstance(value, str):
        if credential in value:
            return True
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return False
        return isinstance(parsed, (dict, list, str)) and parsed != value and contains_credential(parsed, credential)
    if isinstance(value, dict):
        return any(contains_credential(key, credential) or contains_credential(child, credential)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(contains_credential(child, credential) for child in value)
    return False


def clean_mcp_value(value, credential=None):
    """Scrub known transport credentials before evidence or model projection."""
    if isinstance(value, str):
        if credential:
            value = value.replace(f"Bearer {credential}", "[redacted]").replace(credential, "[redacted]")
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        return dump(clean_mcp_value(parsed, credential)) if isinstance(parsed, (dict, list, str)) and parsed != value else value
    if isinstance(value, dict):
        return redact({clean_mcp_value(str(key), credential): clean_mcp_value(child, credential) for key, child in value.items()}, truncate=False)
    if isinstance(value, list):
        return [clean_mcp_value(child, credential) for child in value]
    return value


def offline_resource(uri):
    raise NoSuchResource(ref=uri)


def schema_validator(schema):
    try:
        if type(schema) is not dict or schema.get("type") != "object" or len(encoded(schema).encode()) > MAX_SCHEMA_BYTES:
            raise ValueError()
        pending = [(schema, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > 64:
                raise ValueError()
            if isinstance(value, dict):
                if "$id" in value or any(key in value and (not isinstance(value[key], str) or not value[key].startswith("#"))
                                         for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                    raise ValueError()
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        cls = validator_for(schema, default=Draft202012Validator)
        # Unknown dialects must not silently receive another draft's semantics.
        if "$schema" in schema and schema["$schema"].rstrip("#") != cls.META_SCHEMA["$id"].rstrip("#"):
            raise ValueError()
        cls.check_schema(schema)
        return cls(schema, registry=Registry(retrieve=offline_resource))
    except Exception:
        raise AppError(422, "HARNESS_MCP_SCHEMA_UNSUPPORTED", "业务工具参数定义无效或包含不支持的外部引用") from None


class BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise httpx.DecodingError("MCP response exceeded the host limit")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


async def bound_response(response):
    # The official MCP transport can follow same-origin redirects itself even
    # when httpx.follow_redirects is false. Reject before it can replay a POST.
    if response.is_redirect:
        await response.aclose()
        raise httpx.HTTPStatusError("MCP redirects are not supported", request=response.request, response=response)
    if response.headers.get("Content-Encoding", "identity").lower().strip() != "identity":
        await response.aclose()
        raise httpx.DecodingError("Compressed MCP responses are not supported")
    response.stream = BoundedStream(response.stream)


class MCPBusinessTool:
    def __init__(self, runtime, connection, definition, argument_constraint=None):
        self.runtime = runtime
        self.connection_id = connection["id"]
        self.server_tool = definition["name"]
        self.canonical_name = f"mcp__c{self.connection_id}__{self.server_tool}"
        self.name = external_tool_name(self.canonical_name)
        self.stored_definition = encoded(definition)
        self.input_schema = definition["inputSchema"]
        self.validator = schema_validator(self.input_schema)
        from .tool_constraint_context import constrained_input_schema
        self.model_input_schema = constrained_input_schema(self.input_schema, argument_constraint)
        self.route = dict(runtime.config["mcp_servers"][f"c{self.connection_id}"])
        self.oauth = connection.get("auth_type") == "oauth"
        self.credential = runtime.service.secrets.decrypt(connection.get("secret"))
        # Schema constants/defaults must not smuggle a transport credential to
        # Node or the model. Descriptions may be safely scrubbed as plain text.
        if self.credential and contains_credential(self.input_schema, self.credential):
            raise AppError(422, "HARNESS_MCP_SCHEMA_UNSUPPORTED", "业务工具定义包含连接凭据，请修正连接后重新测试")
        connection_name = " ".join(self.clean(connection.get("name") or "").split())
        connection_name = re.sub(r"https?://\S+", "[redacted]", connection_name, flags=re.IGNORECASE)[:100] or "业务连接"
        label = f"MCP 服务 {dump(connection_name)} 的工具 {dump(self.clean(self.server_tool))}。"
        description = self.clean(definition.get("description") or "已授权业务工具")
        self.description = f"{label}\n{description}"[:4000]

    def clean(self, value):
        return clean_mcp_value(value, self.credential)

    def definition(self):
        return {"name": self.name, "description": self.description, "input_schema": self.model_input_schema}

    def arguments(self, value):
        try:
            if type(value) is not dict or len(encoded(value).encode()) > MAX_ARGUMENT_BYTES:
                raise ValueError()
            self.validator.validate(value)
        except Exception:
            raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "业务工具参数不符合连接声明的格式") from None
        return value

    def current(self):
        runtime, service = self.runtime, self.runtime.service
        conversation = service.require("conversations", runtime.conversation_id)
        user = service.require("users", conversation["user_id"])
        row = service.require_resource("connections", self.connection_id, user)
        definitions = json.loads(row["tools"])
        matching = [item for item in definitions if item.get("name") == self.server_tool]
        if (not row["enabled"] or row["status"] != "connected" or len(matching) != 1
                or encoded(matching[0]) != self.stored_definition):
            raise AppError(409, "HARNESS_MCP_CONFIG_CHANGED", "业务工具定义或连接状态已变化，请重新测试连接并发送任务")
        validate_mcp_url(self.route["url"], service.settings.mcp_hosts)
        return row

    async def invoke(self, arguments, recheck):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self.runtime.service.policy.require_tool(self.canonical_name)
        row = self.current()
        recheck()
        try:
            with transport_logs(), anyio.fail_after(MCP_TIMEOUT_SECONDS):
                headers = {**self.route.get("headers", {}), "Accept-Encoding": "identity"}
                credential = self.credential
                if self.oauth:
                    credential = await self.runtime.service.mcp_oauth.access_token(row)
                    headers["Authorization"] = f"Bearer {credential}"
                    self.current()
                    recheck()
                async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(MCP_TIMEOUT_SECONDS, connect=10),
                        follow_redirects=False, max_redirects=0, trust_env=False, event_hooks={"response": [bound_response]}) as http:
                    async with streamable_http_client(self.route["url"], http_client=http) as streams:
                        async with ClientSession(streams[0], streams[1]) as session:
                            await session.initialize()
                            matches, cursor = [], None
                            for _ in range(20):
                                response = await session.list_tools(cursor=cursor)
                                matches.extend(tool for tool in response.tools if tool.name == self.server_tool)
                                cursor = response.nextCursor
                                if not cursor:
                                    break
                            if cursor or len(matches) != 1 or encoded(matches[0].inputSchema) != encoded(self.input_schema):
                                raise AppError(409, "HARNESS_MCP_SCHEMA_CHANGED", "业务工具的远端定义已变化，请重新测试连接并发送任务")
                            confirmation = getattr(recheck, "definition_digest", None)
                            if confirmation:
                                from .read_only_tools import require_eligible
                                if require_eligible(matches[0]) != confirmation:
                                    raise AppError(409, "READ_ONLY_CONFIRMATION_STALE", "远端工具定义已变化，请重新测试连接并确认只读用途")
                            self.current()
                            # Network initialization is an await boundary: an
                            # old approval cannot survive revocation during it.
                            recheck()
                            result = await session.call_tool(self.server_tool, arguments=arguments,
                                read_timeout_seconds=timedelta(seconds=MCP_TIMEOUT_SECONDS))
            if result.isError:
                raise ValueError("remote tool error")
            body = result.model_dump(mode="json", exclude_none=True)
            if len(encoded(body).encode()) > MAX_RESPONSE_BYTES:
                raise ValueError("large result")
            if any(item.get("type") != "text" for item in body.get("content", [])):
                raise AppError(422, "HARNESS_MCP_RESULT_UNSUPPORTED", "此业务工具返回了尚未验收的非文本内容")
            body = include_structured_text(body)
            # Decide equivalence before clean() normalizes JSON text, so a
            # duplicate-key/precision ambiguity cannot become false equality.
            body = self.clean(clean_mcp_value(body, credential)) if self.oauth else self.clean(body)
            if len(encoded(body).encode()) > MAX_RESPONSE_BYTES:
                raise ValueError("large normalized result")
            enforce_model_result_budget(body)
            return body
        except asyncio.CancelledError:
            raise
        except AppError:
            raise
        except Exception:
            # Never persist arbitrary remote exceptions, headers or URLs.
            raise AppError(502, "HARNESS_MCP_TOOL_FAILED", "业务工具未完成，外部操作不会自动重试；请核对业务状态后再决定是否重发") from None


def build_mcp_tools(runtime):
    service = runtime.service
    # Trial deployments never mount business MCP servers or even read their
    # connection credentials. Execution is checked again by the bridge and by
    # MCPBusinessTool.invoke for callers that construct the registry directly.
    if getattr(service, "policy", None) is not None and service.policy.is_trial:
        return {}
    from .tool_constraint_context import request_constraint_rules
    # Both execution paths cap the combined added policy, not just each tool.
    request_constraint_rules(runtime)
    tools = {}
    user = service.require("users", service.require("conversations", runtime.conversation_id)["user_id"])
    for binding in runtime.config.get("assistant_snapshot", {}).get("connections", []):
        if not binding["allowed_tools"]:
            continue
        connection = service.require_resource("connections", binding["connection_id"], user)
        if not connection["enabled"] or f"c{connection['id']}" not in runtime.config.get("mcp_servers", {}):
            continue
        if connection["status"] != "connected":
            continue
        definitions = json.loads(connection["tools"])
        if binding.get("argument_constraints"):
            from .tool_constraints import validate_definitions
            validate_definitions(binding, connection, service.secrets.decrypt(connection.get("secret")))
        for name in binding["allowed_tools"]:
            matching = [item for item in definitions if item.get("name") == name]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name) or len(matching) != 1:
                # A removed grant is unavailable until the operator updates
                # the assistant. Other business and workspace tools can run.
                continue
            tool = MCPBusinessTool(runtime, connection, matching[0], binding.get("argument_constraints", {}).get(name))
            if tool.name in tools:
                raise AppError(409, "HARNESS_MCP_TOOL_UNAVAILABLE", "业务工具定义重复，请重新配置助手")
            tools[tool.name] = tool
    return tools
