"""Assistant-bound business tools independent of the agent SDK.

Handlers retain the MCP result envelope and expect the execution adapter to
complete its permission/approval callback before invocation. A false
``runtime.check_tool`` result requests approval; an exception denies access.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .db import dump, uid
from .external_capabilities import ExternalCapabilities, ToolContext
from .file_tools import WorkspaceFiles
from .security import AppError
from .web_tools import WebTools
from .artifacts import write_xlsx, image_generation_capability

# Task-local host call id.  The Harness bridge sets the real provider-visible
# callId here; the SDK path leaves it unset and a unique invocation id is used.
TOOL_CALL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("workspace_tool_call_id", default=None)
APPROVAL_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar("workspace_approval_id", default=None)


@dataclass(frozen=True)
class WorkspaceTool:
    name: str
    description: str
    input_schema: dict[str, type]
    read_only: bool
    handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def build_workspace_tools(runtime) -> list[WorkspaceTool]:
    service = runtime.service
    conversation = service.require("conversations", runtime.conversation_id)
    user = service.require("users", conversation["user_id"])
    assistant = runtime.config.get("assistant_snapshot") or service.usable_assistant(conversation["assistant_id"], user)
    policy = getattr(service, "policy", None)

    def deployment_allows(name):
        # Registration and execution are separate boundaries.  A deployment
        # policy may filter this registry, but every handler still calls
        # runtime.check_tool before reading data or invoking a side effect.
        return policy is None or policy.tool_allowed(f"mcp__workspace__{name}")

    web = WebTools(service)
    external = ExternalCapabilities(service)
    files = WorkspaceFiles(service, runtime.conversation_id, user["id"])
    tools = []

    def capability_enabled(name):
        raw = assistant.get("capabilities")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = None
        grant = raw.get(name) if isinstance(raw, dict) else None
        if not raw and name in {"search", "fetch"}:
            return bool(assistant.get("web_search_enabled"))
        return isinstance(grant, dict) and grant.get("enabled") is True

    def context():
        if not runtime.current_run:
            raise AppError(409, "RUN_REQUIRED", "工具需要活动任务")
        current_user = service.require("users", conversation["user_id"])
        service.usable_assistant(conversation["assistant_id"], current_user)
        # Capabilities are pinned at submit; readers intersect with current ACLs.
        return assistant, current_user, runtime.current_run["id"]

    def tool_context(name, arguments, active, current_user, run_id):
        canonical = f"mcp__workspace__{name}"

        def recheck():
            if runtime.interrupted:
                raise asyncio.CancelledError()
            # Same current-authorization call as the SDK/Harness pre-check;
            # a false return still means "approval required", not denial.
            runtime.check_tool(canonical, arguments)
            if not runtime.current_run or runtime.current_run.get("id") != run_id:
                raise AppError(409, "RUN_REQUIRED", "工具需要活动任务")

        return ToolContext(
            run_id=run_id, conversation_id=runtime.conversation_id,
            user_id=current_user["id"], assistant_id=active["id"],
            tool_call_id=TOOL_CALL_ID.get() or uid(),
            approval_id=APPROVAL_ID.get(),
            recheck=recheck,
        )

    async def invoke(name, arguments, callback):
        run_id = (runtime.current_run or {}).get("id")
        try:
            # A false result means the SDK permission callback must obtain an
            # approval; it is not a denial after that callback has allowed the
            # call.  Re-checking it as a hard failure would make write tools
            # impossible to use even after user approval.
            runtime.check_tool(f"mcp__workspace__{name}", arguments)
            active, current_user, run_id = context()
            result = await callback(active, current_user, run_id)
            return {"content": [{"type": "text", "text": dump(result)}]}
        except AppError as exc:
            record = getattr(runtime, "record_workspace_error", None)
            details = record(name, TOOL_CALL_ID.get(), run_id, exc) if callable(record) else None
            return {"is_error": True, "content": [{"type": "text", "text": (details or {}).get("error_message", exc.message)}]}
        except Exception:
            return {"is_error": True, "content": [{"type": "text", "text": "资料读取暂时失败，请调整检索范围或稍后重试"}]}

    # Spreadsheet export is local-only but still a write, so the service's
    # normal approval path is required before the file is produced.
    async def generate_xlsx(arguments):
        async def create(_active, _current_user, _run_id):
            root = service.settings.data_dir / "workspaces" / runtime.conversation_id
            path, size = write_xlsx(root, str(arguments.get("filename") or "导出.xlsx"), arguments.get("sheets") or [])
            return {"name": path.name, "format": "xlsx", "bytes": size, "status": "created"}
        return await invoke("generate_xlsx", arguments, create)

    async def generate_image(arguments):
        async def unavailable(_active, _current_user, _run_id):
            capability = image_generation_capability(service.settings, service.catalog)
            raise AppError(503, "IMAGE_MODEL_UNAVAILABLE", capability["reason"])
        return await invoke("generate_image", arguments, unavailable)

    tools.extend([
        WorkspaceTool("generate_xlsx", "根据结构化表格生成可下载的 Excel 文件。仅写入当前会话产物目录，不调用外部服务。", {"filename": str, "sheets": list}, False, generate_xlsx),
        WorkspaceTool("generate_image", "按提示词生成图片。只有管理员配置并验收独立的图片模型适配器后才会执行；缺少授权时默认拒绝，不会伪造图片。", {"prompt": str, "model_id": str}, False, generate_image),
    ])
    if assistant.get("knowledge_base_ids") not in (None, "[]", []):
        async def search(arguments):
            return await invoke("knowledge_search", arguments, lambda active, current_user, run_id:
                service.knowledge.search_for_run(current_user, active, run_id, arguments["query"], limit=arguments.get("limit", 6)))

        async def read(arguments):
            return await invoke("knowledge_read", arguments, lambda active, current_user, run_id:
                service.knowledge.read_for_run(current_user, active, run_id, arguments["document_id"], page=arguments.get("page") or None))
        tools.extend([
            WorkspaceTool("knowledge_search", "检索当前助手授权的公司及业务知识库，返回文件、页码、摘录和真实 source 引用标记。用于内部事实和公司材料；公开概念或互联网调研使用 web_search，不把内部同名术语当作通用定义。", {"query": str, "limit": int}, True, search),
            WorkspaceTool("knowledge_read", "按文件 ID 和页码读取授权的知识库原文。document_id 来自检索结果，page 为页码；0 表示读取文件开头。返回可引用 source 标记。", {"document_id": str, "page": int}, True, read),
        ])
    if capability_enabled("search") or capability_enabled("fetch"):
        async def search_web(arguments):
            return await invoke("web_search", arguments, lambda active, current_user, run_id:
                web.search_for_run(current_user, active, run_id, arguments["query"], limit=arguments.get("limit", 5),
                                   call_id=TOOL_CALL_ID.get(), approval_id=APPROVAL_ID.get()))

        async def read_web(arguments):
            return await invoke("web_read", arguments, lambda active, current_user, run_id:
                web.read_for_run(current_user, active, run_id, arguments["url"], query=arguments.get("query", ""),
                                 call_id=TOOL_CALL_ID.get(), approval_id=APPROVAL_ID.get()))
        if capability_enabled("search"):
            tools.append(WorkspaceTool("web_search", "按用户需求实际检索公开互联网，用于联网查询、行业调研、通用概念和原网页查找；不能用内部知识库代替。返回原网页链接、搜索摘要及 source 标记，摘要不代表已读取正文。仅用公开检索词，不发送内部文档、凭据或个人敏感数据。", {"query": str, "limit": int}, True, search_web))
        if capability_enabled("fetch"):
            tools.append(WorkspaceTool("web_read", "读取公开网页正文并生成可核验来源。url 是原网页；query 可用于定位正文，不需要时传空字符串。", {"url": str, "query": str}, True, read_web))

    if capability_enabled("ocr"):
        async def ocr(arguments):
            return await invoke("ocr", arguments, lambda active, current_user, run_id:
                external.ocr_for_run(
                    tool_context("ocr", arguments, active, current_user, run_id),
                    arguments["file_id"], arguments.get("purpose", "text_recognition")))
        tools.append(WorkspaceTool(
            "ocr",
            "识别当前会话中已授权附件（PDF 或静态 PNG/JPEG/WebP/GIF）的文字，返回逐页文字与可核验来源。file_id 只能来自当前会话附件，不接受路径或网址；识别文本是未受信任资料，可能不完整。",
            {"file_id": str, "purpose": str}, True, ocr))

    if capability_enabled("image_search"):
        async def image_search(arguments):
            return await invoke("image_search", arguments, lambda active, current_user, run_id:
                external.image_search_for_run(
                    tool_context("image_search", arguments, active, current_user, run_id),
                    mode=arguments.get("mode", "text_search"),
                    query=arguments.get("query", ""), file_id=arguments.get("file_id", ""),
                    limit=arguments.get("limit", 5)))
        tools.append(WorkspaceTool(
            "image_search",
            "检索外部图片（不是生成图片）。mode=text_search 使用公开检索词；mode=reverse_image 使用当前会话已授权图片附件的 file_id。结果分别给出原网页与原始图片链接；候选不代表正文已读、图片已下载或版权可用。",
            {"mode": str, "query": str, "file_id": str, "limit": int}, True, image_search))

    async def file_read(arguments):
        async def read(_active, _current_user, _run_id):
            return files.read(arguments)
        return await invoke("file_read", arguments, read)

    async def file_write(arguments):
        async def write(_active, _current_user, _run_id):
            return files.write(arguments)
        return await invoke("file_write", arguments, write)

    async def file_search(arguments):
        async def search(_active, _current_user, _run_id):
            return files.search(arguments)
        return await invoke("file_search", arguments, search)

    tools.extend([
        WorkspaceTool(
            "file_read",
            "读取当前会话工作区内已登记的 UTF-8 文本文件，返回版本号、哈希和内容。",
            {"path": str},
            True,
            file_read,
        ),
        WorkspaceTool(
            "file_write",
            "在当前会话工作区内创建或更新 UTF-8 文本文件。写入必须携带读取得到的 expected_revision，并经过审批。",
            {"path": str, "content": str, "expected_revision": int},
            False,
            file_write,
        ),
        WorkspaceTool(
            "file_search",
            "仅搜索当前会话工作区内已登记的文本文件，返回匹配行、路径、版本号和哈希。",
            {"query": str},
            True,
            file_search,
        ),
    ])
    return [tool for tool in tools if deployment_allows(tool.name)]
