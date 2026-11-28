"""Narrow SDK transport adapter for the existing describe/query MCP contract.

Experimental candidate: use the host's existing MCP client and one-time approval
ticket, rather than interpreting flattened SDK callback text.
"""
import asyncio
import json

from .mcp_tools import MCPBusinessTool
from .security import AppError


def _matrix_query_definition(definition):
    schema = definition.get("inputSchema") or {}
    props = schema.get("properties") or {}
    return (definition.get("name") == "query" and isinstance(props, dict)
            and isinstance(props.get("dataset"), dict)
            and isinstance(props.get("select"), dict)
            and props["dataset"].get("type") == "string"
            and props["select"].get("type") == "array")


def selected_connections(runtime):
    """Only mount a verified query-shaped connection granting describe/query.

    Preserve the original canonical names and current binding. Do not replace
    arbitrary servers with mixed write tools or other connection contracts.
    """
    service = runtime.service
    if service.policy.is_trial or runtime.config.get("recovery_only"):
        return {}
    user = service.require("users", runtime.config["conversation"]["user_id"])
    groups = {}
    for binding in runtime.config["assistant_snapshot"]["connections"]:
        names = set(binding["allowed_tools"])
        if "query" not in names or not names <= {"describe", "query"}:
            continue
        cid = binding["connection_id"]
        server_name = f"c{cid}"
        if server_name not in runtime.config["mcp_servers"]:
            continue
        connection = service.require_resource("connections", cid, user)
        if connection["auth_type"] == "oauth" or connection["status"] != "connected":
            continue
        defs = json.loads(connection["tools"])
        selected = [d for d in defs if d.get("name") in names]
        if len(selected) != len(names) or not any(_matrix_query_definition(d) for d in selected):
            continue
        groups[server_name] = [MCPBusinessTool(runtime, connection, definition,
            binding.get("argument_constraints", {}).get(definition["name"])) for definition in selected]
    return groups


def create_servers(runtime, groups):
    # No eligible connection: retain the original SDK transport unchanged.
    if not groups:
        return {}
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from mcp.types import CallToolRequest, CallToolResult, ServerResult, ToolAnnotations
    from .capabilities import CAPABILITY_TICKET_FIELD
    from .runtime import _arguments_digest

    servers = {}
    for server_name, group in groups.items():
        by_name = {entry.server_tool: entry for entry in group}

        async def unused(_):
            return {"content": [{"type":"text","text":"Host dispatch required"}], "isError":True}

        def annotations(entry):
            raw = json.loads(entry.stored_definition).get("annotations")
            return ToolAnnotations.model_validate(raw) if isinstance(raw, dict) else None
        sdk_tools = [tool(entry.server_tool, entry.description, entry.model_input_schema,
                     annotations=annotations(entry))(unused) for entry in group]
        config = create_sdk_mcp_server(server_name, "1.0.0", tools=sdk_tools)
        if not isinstance(config, dict):
            raise AppError(503, "SDK_MCP_BRIDGE_UNAVAILABLE", "业务查询执行器不可用")

        async def dispatch(request, by_name=by_name):
            entry = by_name.get(request.params.name)
            args = dict(request.params.arguments or {})
            ticket = args.pop(CAPABILITY_TICKET_FIELD, None)
            binding = runtime.consume_capability_ticket(ticket, entry.canonical_name, args) if entry else None
            if not binding:
                return ServerResult(CallToolResult(content=[], isError=True))
            key = (binding["run_id"], binding["tool_use_id"], entry.canonical_name)
            if key in runtime._sdk_business_started or len(runtime._sdk_business_started) >= 256:
                return ServerResult(CallToolResult(content=[], isError=True))
            runtime._sdk_business_started.add(key)
            def recheck():
                runtime.require_active_tool_result(binding["run_id"])
                runtime.check_tool(entry.canonical_name, args)
                if runtime.tool_run_ids.get(binding["tool_use_id"]) != binding["run_id"]:
                    raise AppError(409, "MCP_RESULT_STALE", "业务查询任务已变更")
            try:
                entry.arguments(args)
                recheck()
                result = await entry.invoke(args, recheck)
                recheck()
                if len(runtime._sdk_business_results) >= 32:
                    raise AppError(409, "SDK_MCP_RECEIPT_LIMIT", "业务回执等待上限")
                sdk_result = ServerResult(CallToolResult.model_validate(result))
                runtime._sdk_business_results[key] = (_arguments_digest(args), result)
                # Return the same sanitized host result, never repeat the query.
                return sdk_result
            except asyncio.CancelledError:
                raise
            except AppError as e:
                return ServerResult(CallToolResult(content=[{"type":"text","text":e.message}], isError=True))
            except Exception:
                return ServerResult(CallToolResult(content=[{"type":"text","text":"业务查询未完成"}], isError=True))

        config["instance"].request_handlers[CallToolRequest] = dispatch
        servers[server_name] = config
    return servers
