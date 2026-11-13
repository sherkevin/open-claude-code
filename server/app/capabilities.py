"""SDK compatibility wrapper for the workspace business-tool registry."""
from __future__ import annotations

from .workspace_tools import build_workspace_tools


def create_workspace_tools(runtime):
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from mcp.types import ToolAnnotations

    tools = [
        tool(
            definition.name,
            definition.description,
            definition.input_schema,
            annotations=ToolAnnotations(
                readOnlyHint=definition.read_only,
                destructiveHint=False,
            ),
        )(definition.handler)
        for definition in build_workspace_tools(runtime)
    ]
    return create_sdk_mcp_server("workspace", "0.4.0", tools=tools) if tools else None
