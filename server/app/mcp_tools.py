"""Harness business tools using the existing official Python MCP transport.

Only the trusted host holds routes and credentials. Model-visible names are
opaque, bounded aliases; approval and evidence retain the canonical tool name.
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
    def __init__(self, runtime, connection, definition):
        self.runtime = runtime
        self.connection_id = connection["id"]
        self.server_tool = definition["name"]
        self.canonical_name = f"mcp__c{self.connection_id}__{self.server_tool}"
        self.name = external_tool_name(self.canonical_name)
        self.stored_definition = encoded(definition)
        self.input_schema = definition["inputSchema"]
        self.validator = schema_validator(self.input_schema)
        self.route = dict(runtime.config["mcp_servers"][f"c{self.connection_id}"])
        self.credential = runtime.service.secrets.decrypt(connection.get("secret"))
        # Schema constants/defaults must not smuggle a transport credential to
        # Node or the model. Descriptions may be safely scrubbed as plain text.
        if self.credential and contains_credential(self.input_schema, self.credential):
            raise AppError(422, "HARNESS_MCP_SCHEMA_UNSUPPORTED", "业务工具定义包含连接凭据，请修正连接后重新测试")
        self.description = self.clean(definition.get("description") or f"已授权业务工具 {self.server_tool}")[:4000]

    def clean(self, value):
        if isinstance(value, str):
            if self.credential:
                value = value.replace(f"Bearer {self.credential}", "[redacted]").replace(self.credential, "[redacted]")
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                return value
            return dump(self.clean(parsed)) if isinstance(parsed, (dict, list, str)) and parsed != value else value
        if isinstance(value, dict):
            return redact({self.clean(str(key)): self.clean(child) for key, child in value.items()}, truncate=False)
        if isinstance(value, list):
            return [self.clean(child) for child in value]
        return value

    def definition(self):
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

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

    async def invoke(self, arguments, recheck):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self.runtime.service.policy.require_tool(self.canonical_name)
        self.current()
        recheck()
        try:
            with transport_logs(), anyio.fail_after(MCP_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(headers={**self.route.get("headers", {}), "Accept-Encoding": "identity"}, timeout=httpx.Timeout(MCP_TIMEOUT_SECONDS, connect=10),
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
            body = self.clean(body)
            if body.get("structuredContent"):
                body.setdefault("content", []).append({"type": "text", "text": dump(body["structuredContent"])})
            if len(encoded(body).encode()) > MAX_RESPONSE_BYTES:
                raise ValueError("large normalized result")
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
    tools = {}
    user = service.require("users", service.require("conversations", runtime.conversation_id)["user_id"])
    for binding in runtime.config.get("assistant_snapshot", {}).get("connections", []):
        if not binding["allowed_tools"]:
            continue
        connection = service.require_resource("connections", binding["connection_id"], user)
        if not connection["enabled"] or f"c{connection['id']}" not in runtime.config.get("mcp_servers", {}):
            continue
        if connection["status"] != "connected":
            raise AppError(409, "HARNESS_MCP_NOT_TESTED", "请先测试并确认已绑定的业务连接")
        definitions = json.loads(connection["tools"])
        for name in binding["allowed_tools"]:
            matching = [item for item in definitions if item.get("name") == name]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name) or len(matching) != 1:
                raise AppError(409, "HARNESS_MCP_TOOL_UNAVAILABLE", "助手授权的业务工具已不存在或名称不受支持，请重新配置")
            tool = MCPBusinessTool(runtime, connection, matching[0])
            if tool.name in tools:
                raise AppError(409, "HARNESS_MCP_TOOL_UNAVAILABLE", "业务工具定义重复，请重新配置助手")
            tools[tool.name] = tool
    return tools
