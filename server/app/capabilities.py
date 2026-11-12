"""Assistant-bound tools hosted in the existing native execution session."""
from __future__ import annotations

from .db import dump
from .file_tools import build_file_tools
from .security import AppError
from .web_tools import WebTools


def create_workspace_tools(runtime):
    from claude_agent_sdk import create_sdk_mcp_server, tool
    from mcp.types import ToolAnnotations

    service = runtime.service
    conversation = service.require("conversations", runtime.conversation_id)
    user = service.require("users", conversation["user_id"])
    assistant = runtime.config.get("assistant_snapshot") or service.usable_assistant(conversation["assistant_id"], user)
    web = WebTools(service)
    tools = []

    def context():
        if not runtime.current_run:
            raise AppError(409, "RUN_REQUIRED", "工具需要活动任务")
        current_user = service.require("users", conversation["user_id"])
        service.usable_assistant(conversation["assistant_id"], current_user)
        # Capabilities are pinned at submit; readers intersect with current ACLs.
        return assistant, current_user, runtime.current_run["id"]

    async def invoke(name, arguments, callback):
        try:
            runtime.check_tool(f"mcp__workspace__{name}", arguments)
            active, current_user, run_id = context()
            result = await callback(active, current_user, run_id)
            return {"content": [{"type": "text", "text": dump(result)}]}
        except AppError as exc:
            return {"is_error": True, "content": [{"type": "text", "text": exc.message}]}
        except Exception:
            return {"is_error": True, "content": [{"type": "text", "text": "资料读取暂时失败，请调整检索范围或稍后重试"}]}

    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    if assistant.get("knowledge_base_ids") not in (None, "[]", []):
        @tool("knowledge_search", "检索当前助手授权的公司及业务知识库，返回文件、页码、摘录和真实 source 引用标记。关于 Better AI 的问题先检索公司资料。", {"query": str, "limit": int}, annotations=annotations)
        async def search(arguments):
            return await invoke("knowledge_search", arguments, lambda active, current_user, run_id:
                service.knowledge.search_for_run(current_user, active, run_id, arguments["query"], limit=arguments.get("limit", 6)))

        @tool("knowledge_read", "按文件 ID 和页码读取授权的知识库原文。document_id 来自检索结果，page 为页码；0 表示读取文件开头。返回可引用 source 标记。", {"document_id": str, "page": int}, annotations=annotations)
        async def read(arguments):
            return await invoke("knowledge_read", arguments, lambda active, current_user, run_id:
                service.knowledge.read_for_run(current_user, active, run_id, arguments["document_id"], page=arguments.get("page") or None))
        tools.extend([search, read])
    if assistant.get("web_search_enabled"):
        @tool("web_search", "搜索公开互联网信息，返回原网页链接、搜索摘要及 source 标记。仅用公开检索词，不发送内部文档、凭据或个人敏感数据。", {"query": str, "limit": int}, annotations=annotations)
        async def search_web(arguments):
            return await invoke("web_search", arguments, lambda active, current_user, run_id:
                web.search_for_run(current_user, active, run_id, arguments["query"], limit=arguments.get("limit", 5)))

        @tool("web_read", "读取公开网页正文并生成可核验来源。url 是原网页；query 可用于定位正文，不需要时传空字符串。", {"url": str, "query": str}, annotations=annotations)
        async def read_web(arguments):
            return await invoke("web_read", arguments, lambda active, current_user, run_id:
                web.read_for_run(current_user, active, run_id, arguments["url"], query=arguments.get("query", "")))
        tools.extend([search_web, read_web])

    def sdk_tool(definition):
        annotations = ToolAnnotations(
            readOnlyHint=definition.read_only,
            destructiveHint=False,
        )

        async def handler(arguments):
            return await definition.handler(arguments)

        return tool(definition.name, definition.description, definition.input_schema, annotations=annotations)(handler)

    tools.extend(sdk_tool(definition) for definition in build_file_tools(runtime))
    return create_sdk_mcp_server("workspace", "0.4.0", tools=tools) if tools else None
