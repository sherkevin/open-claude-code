"""SDK compatibility wrapper for the workspace business-tool registry.

Capability tools (OCR / image search) are registered like ordinary workspace
tools, because the SDK's MCP layer validates the wire schema before invoking a
handler.  Their wire schema therefore cannot name the host nonce.  A dedicated
dispatch consumes that nonce first, strips it, and then applies the strict
public argument schema itself.  A request that did not pass the host
permission callback cannot reach these handlers at all.
"""
from __future__ import annotations

from .workspace_tools import APPROVAL_ID, TOOL_CALL_ID, build_workspace_tools

CAPABILITY_TOOL_NAMES = frozenset({
    "mcp__workspace__web_search",
    "mcp__workspace__web_read",
    "mcp__workspace__ocr",
    "mcp__workspace__image_search",
})
CAPABILITY_TOOL_SHORT_NAMES = frozenset(name.rsplit("__", 1)[-1] for name in CAPABILITY_TOOL_NAMES)
# Invisible transport field injected by the permission callback together with
# PermissionResultAllow(updated_input=...).  It is never part of the model-facing
# documented schema and is stripped before the handler or any log sees it.
CAPABILITY_TICKET_FIELD = "__oc_capability_ticket"
_CAPABILITY_SCHEMAS = {
    "ocr": {
        "type": "object",
        "properties": {"file_id": {"type": "string"}, "purpose": {"type": "string"}},
        "required": ["file_id"],
        "additionalProperties": False,
    },
    "image_search": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["text_search", "reverse_image"]},
            "query": {"type": "string"},
            "file_id": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "additionalProperties": False,
    },
    "web_search": {
        "type": "object", "required": ["query"], "additionalProperties": False,
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 500},
                       "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
    },
    "web_read": {
        "type": "object", "required": ["url"], "additionalProperties": False,
        "properties": {"url": {"type": "string", "minLength": 1, "maxLength": 4096},
                       "query": {"type": "string", "maxLength": 500}},
    },
}


def _error(message: str) -> dict:
    return {"is_error": True, "content": [{"type": "text", "text": message}]}


def _validated(definition, arguments):
    """Strict public schema after removing the host ticket."""
    if type(arguments) is not dict:
        return None
    expected = definition.input_schema
    if set(arguments) - set(expected):
        return None
    for key, value in arguments.items():
        if type(value) is not expected[key]:
            return None
    if definition.name == "ocr" and not arguments.get("file_id"):
        return None
    if definition.name in _CAPABILITY_SCHEMAS:
        import jsonschema
        try:
            jsonschema.validate(arguments, _CAPABILITY_SCHEMAS[definition.name])
        except jsonschema.ValidationError:
            return None
    return arguments


def _capability_handler(definition, runtime):
    async def dispatch(arguments):
        if type(arguments) is not dict:
            return _error("工具参数无效")
        ticket = arguments.get(CAPABILITY_TICKET_FIELD)
        public = {key: value for key, value in arguments.items() if key != CAPABILITY_TICKET_FIELD}
        public = _validated(definition, public)
        if public is None:
            return _error("工具参数无效或超出允许范围")
        consume = getattr(runtime, "consume_capability_ticket", None)
        canonical = f"mcp__workspace__{definition.name}"
        binding = consume(ticket, canonical, public) if callable(consume) else None
        if not binding:
            # Missing, expired, replayed, mismatched or foreign ticket: no host
            # side effect and no provider egress.
            return _error("工具调用未通过主机审批，请重新发起")
        call_token = TOOL_CALL_ID.set(binding["tool_use_id"])
        approval_token = APPROVAL_ID.set(binding.get("approval_id"))
        try:
            return await definition.handler(public)
        finally:
            APPROVAL_ID.reset(approval_token)
            TOOL_CALL_ID.reset(call_token)

    return dispatch


def create_workspace_tools(runtime):
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from mcp.types import ToolAnnotations

    definitions = build_workspace_tools(runtime)
    tools = []
    for definition in definitions:
        schema = (_CAPABILITY_SCHEMAS[definition.name]
                  if definition.name in CAPABILITY_TOOL_SHORT_NAMES else definition.input_schema)
        handler = (_capability_handler(definition, runtime)
                   if definition.name in CAPABILITY_TOOL_SHORT_NAMES else definition.handler)
        tools.append(tool(
            definition.name,
            definition.description,
            schema,
            annotations=ToolAnnotations(
                readOnlyHint=definition.read_only,
                destructiveHint=False,
            ),
        )(handler))
    if not tools:
        return None
    config = create_sdk_mcp_server("workspace", "0.4.0", tools=tools)
    if not isinstance(config, dict):
        return config  # Registry-only test doubles have no MCP dispatcher.
    from mcp.types import CallToolRequest, CallToolResult, ServerResult
    server = config["instance"]
    original = server.request_handlers[CallToolRequest]
    handlers = {definition.name: _capability_handler(definition, runtime)
                for definition in definitions if definition.name in CAPABILITY_TOOL_SHORT_NAMES}

    async def dispatch(request):
        handler = handlers.get(request.params.name)
        if handler is None:
            return await original(request)
        # Strip/verify the host nonce before public-schema validation. The
        # advertised schema remains strict and never exposes a nonce field.
        result = await handler(request.params.arguments or {})
        return ServerResult(CallToolResult.model_validate({
            "content": result.get("content", []), "isError": bool(result.get("is_error"))}))

    server.request_handlers[CallToolRequest] = dispatch
    return config
