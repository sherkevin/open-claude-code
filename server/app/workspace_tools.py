"""Assistant-bound business tools independent of the agent SDK.

Handlers retain the MCP result envelope and expect the execution adapter to
complete its permission/approval callback before invocation. A false
``runtime.check_tool`` result requests approval; an exception denies access.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .db import dump
from .file_tools import WorkspaceFiles
from .security import AppError
from .web_tools import WebTools
from .artifacts import write_xlsx, image_generation_capability
from .image_generation import create_job, finish_job, generate_image as run_image_adapter, write_image


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
    web = WebTools(service)
    files = WorkspaceFiles(service, runtime.conversation_id, user["id"])
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
            # A false result means the SDK permission callback must obtain an
            # approval; it is not a denial after that callback has allowed the
            # call.  Re-checking it as a hard failure would make write tools
            # impossible to use even after user approval.
            runtime.check_tool(f"mcp__workspace__{name}", arguments)
            active, current_user, run_id = context()
            result = await callback(active, current_user, run_id)
            return {"content": [{"type": "text", "text": dump(result)}]}
        except AppError as exc:
            return {"is_error": True, "content": [{"type": "text", "text": exc.message}]}
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
        async def create(_active, current_user, _run_id):
            prompt = str(arguments.get("prompt") or "")
            model_id = str(arguments.get("model_id") or "") or None
            capability = image_generation_capability(service.settings, service.catalog)
            job_id = create_job(service, user_id=current_user["id"], conversation_id=runtime.conversation_id, prompt=prompt, model_id=model_id or capability.get("model_id") or "")
            if not capability["available"]:
                finish_job(service, job_id, status="unavailable", error_code="IMAGE_MODEL_UNAVAILABLE", error_message=capability["reason"])
                raise AppError(503, "IMAGE_MODEL_UNAVAILABLE", capability["reason"])
            try:
                result = await run_image_adapter(service.settings, service.catalog, prompt, model_id=model_id)
            except AppError as error:
                finish_job(service, job_id, status="failed", error_code=error.code, error_message=error.message)
                raise
            root = service.settings.data_dir / "workspaces" / runtime.conversation_id
            path, size = write_image(root, f"图片-{job_id[:8]}{result['extension']}", result["image_bytes"])
            finish_job(service, job_id, status="succeeded", nbytes=size, usage=result["usage"])
            return {"name": path.name, "format": result["extension"].lstrip("."), "bytes": size, "status": "created", "job_id": job_id, "usage": result["usage"]}
        return await invoke("generate_image", arguments, create)

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
            WorkspaceTool("knowledge_search", "检索当前助手授权的公司及业务知识库，返回文件、页码、摘录和真实 source 引用标记。关于 Better AI 的问题先检索公司资料。", {"query": str, "limit": int}, True, search),
            WorkspaceTool("knowledge_read", "按文件 ID 和页码读取授权的知识库原文。document_id 来自检索结果，page 为页码；0 表示读取文件开头。返回可引用 source 标记。", {"document_id": str, "page": int}, True, read),
        ])
    if assistant.get("web_search_enabled"):
        async def search_web(arguments):
            return await invoke("web_search", arguments, lambda active, current_user, run_id:
                web.search_for_run(current_user, active, run_id, arguments["query"], limit=arguments.get("limit", 5)))

        async def read_web(arguments):
            return await invoke("web_read", arguments, lambda active, current_user, run_id:
                web.read_for_run(current_user, active, run_id, arguments["url"], query=arguments.get("query", "")))
        tools.extend([
            WorkspaceTool("web_search", "搜索公开互联网信息，返回原网页链接、搜索摘要及 source 标记。仅用公开检索词，不发送内部文档、凭据或个人敏感数据。", {"query": str, "limit": int}, True, search_web),
            WorkspaceTool("web_read", "读取公开网页正文并生成可核验来源。url 是原网页；query 可用于定位正文，不需要时传空字符串。", {"url": str, "query": str}, True, read_web),
        ])

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
    return tools
