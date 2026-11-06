"""Owner-scoped MCP configuration import, independent of execution state.

Parsing/preview performs no network requests. Import creates the entire batch
atomically and never grants assistant access or automatic tool approval.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from .security import AppError, validate_mcp_url

MAX_CONFIG_BYTES = 256 * 1024
MAX_CONNECTIONS = 16


def invalid(message="MCP 配置格式无效，请使用远程 HTTP MCP 配置"):
    raise AppError(422, "INVALID_MCP_IMPORT", message)


def text_value(value, *, maximum=100, optional=False):
    if optional and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(char) < 32 for char in value):
        invalid("MCP 名称、地址或配置字段格式无效")
    return value.strip()


def token_value(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 8192:
        invalid("认证信息格式无效，请填写 Bearer Token")
    value = value.strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value or any(char.isspace() for char in value) or "${" in value:
        invalid("认证信息不能包含空白或环境变量占位符，请填写实际 Token")
    return value


def tool_filter(value):
    if value is None:
        return None
    if isinstance(value, list):
        if len(value) > 1000 or any(not isinstance(item, str) or not item or len(item) > 256 for item in value):
            invalid("工具过滤器必须包含有效工具名称")
        return list(dict.fromkeys(value))
    if isinstance(value, dict) and value and set(value) <= {"tool_names", "read_only"}:
        result = {}
        if "tool_names" in value:
            if not isinstance(value["tool_names"], list):
                invalid("tool_names 必须是工具名称数组")
            result["tool_names"] = tool_filter(value["tool_names"])
        if "read_only" in value:
            if not isinstance(value["read_only"], bool):
                invalid("read_only 必须是布尔值")
            result["read_only"] = value["read_only"]
        return result
    invalid("工具过滤器格式无效")


def approval_policy(value):
    if value in ("always", "never"):
        return value
    if isinstance(value, dict) and value and set(value) <= {"always", "never"}:
        result = {}
        for key, rule in value.items():
            if not isinstance(rule, dict):
                invalid("审批规则必须是工具过滤器对象")
            result[key] = tool_filter(rule)
        return result
    invalid("require_approval 必须是 always、never 或工具过滤规则")


@dataclass
class ImportedConnection:
    name: str
    url: str
    credential: str | None
    allowed_tools: Any = None
    require_approval: Any = "always"

    def preview(self):
        return {"name": self.name, "url": self.url, "transport": "http", "auth_type": "bearer" if self.credential else "none", "credential_present": bool(self.credential), "allowed_tools": self.allowed_tools, "require_approval": self.require_approval}

    def create_fields(self):
        return {"name": self.name, "url": self.url, "transport": "http", "auth_type": "bearer" if self.credential else "none", "credential": self.credential, "enabled": True}


@dataclass
class ImportPlan:
    format: str
    connections: list[ImportedConnection]
    warnings: list[str]

    def preview(self):
        return {"format": self.format, "connections": [item.preview() for item in self.connections], "warnings": self.warnings}


def parse_mcp_config(config, allowed_hosts: tuple[str, ...]) -> ImportPlan:
    try:
        size = len(json.dumps(config, ensure_ascii=False, allow_nan=False).encode())
    except (TypeError, ValueError, RecursionError):
        invalid()
    if size > MAX_CONFIG_BYTES:
        raise AppError(413, "MCP_IMPORT_TOO_LARGE", "MCP 配置不能超过 256 KiB")
    rows = []
    format_name = "openai_mcp"
    if isinstance(config, dict) and "mcpServers" in config:
        if set(config) != {"mcpServers"} or not isinstance(config["mcpServers"], dict):
            invalid("mcpServers 必须是按连接名称组织的对象")
        format_name = "mcp_servers"
        if not 1 <= len(config["mcpServers"]) <= MAX_CONNECTIONS:
            invalid("每次需要导入 1 至 16 个 MCP 连接")
        for name, entry in config["mcpServers"].items():
            if not isinstance(entry, dict) or set(entry) - {"type", "url", "headers"}:
                invalid("仅支持远程 HTTP MCP 的 type、url 和 Authorization 请求头；不执行 command 或 env")
            if entry.get("type", "http") not in ("http", "streamable-http"):
                invalid("当前导入仅支持 Streamable HTTP MCP")
            headers = entry.get("headers", {})
            if not isinstance(headers, dict) or len(headers) > 1 or any(not isinstance(key, str) or key.lower() != "authorization" for key in headers):
                invalid("当前仅支持 Authorization: Bearer Token 请求头")
            authorization = next(iter(headers.values()), None)
            if authorization is not None and (not isinstance(authorization, str) or not authorization.lower().startswith("bearer ")):
                invalid("Authorization 请求头必须使用 Bearer Token")
            rows.append(ImportedConnection(text_value(name), text_value(entry.get("url"), maximum=2048), token_value(authorization)))
    else:
        if isinstance(config, dict) and set(config) == {"tools"}:
            config = config["tools"]
        entries = [config] if isinstance(config, dict) else config
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CONNECTIONS:
            invalid("每次需要导入 1 至 16 个 MCP 连接")
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "mcp":
                invalid("这里只导入 MCP 工具配置，请提供 type 为 mcp 的对象")
            if set(entry) - {"type", "server_label", "server_url", "authorization", "allowed_tools", "require_approval", "server_description"}:
                invalid("MCP 配置含当前不支持的字段；请使用远程 server_url 配置")
            if "server_description" in entry and (not isinstance(entry["server_description"], str) or len(entry["server_description"]) > 1000):
                invalid("server_description 必须是不超过 1000 字的文本")
            rows.append(ImportedConnection(text_value(entry.get("server_label")), text_value(entry.get("server_url"), maximum=2048), token_value(entry.get("authorization")), tool_filter(entry.get("allowed_tools")), approval_policy(entry.get("require_approval", "always"))))
    names = [row.name for row in rows]
    if len(set(names)) != len(names):
        invalid("同一批配置中的连接名称不能重复")
    for row in rows:
        validate_mcp_url(row.url, allowed_hosts)
    warnings = ["导入仅保存连接。请先测试连接，再在助手设置中选择允许使用的工具；不会自动授予工具权限或免审批权限。"]
    if any(row.allowed_tools is not None or row.require_approval != "always" for row in rows):
        warnings.append("配置中的工具过滤和审批规则仅在预览中展示，持久导入后需在助手设置中确认；不会因 require_approval=never 自动放行工具。")
    if format_name == "openai_mcp":
        warnings.append("server_description 不作为业务规则导入；助手业务规则需在助手设置中维护。")
    return ImportPlan(format_name, rows, warnings)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: Any
    scope: Literal["private", "team"] = "private"


def create_import_router(service_dependency, authenticated):
    router = APIRouter(prefix="/api/v1/connections/import", tags=["MCP configuration"])

    def planned(body, service, user):
        if body.scope == "team":
            service.require_admin(user)
        plan = parse_mcp_config(body.config, service.settings.mcp_hosts)
        names = {item.name for item in plan.connections}
        if any(row["name"] in names and row.get("scope") == body.scope for row in service.db.all("SELECT * FROM connections WHERE deleted=0") if service.resource_access(row, user)):
            raise AppError(409, "MCP_IMPORT_NAME_EXISTS", "已有同名连接，请修改导入名称或在原连接中编辑；批量导入不会覆盖既有配置")
        return plan

    @router.post("/preview")
    def preview(body: ImportRequest, service=Depends(service_dependency), user=Depends(authenticated)):
        return {**planned(body, service, user).preview(), "scope": body.scope}

    @router.post("", status_code=201)
    def create(body: ImportRequest, service=Depends(service_dependency), user=Depends(authenticated)):
        # No awaits while holding the SQLite transaction. Validate again so stale
        # browser previews cannot overwrite an existing authorized connection.
        with service.db.transaction():
            plan = planned(body, service, user)
            connections = [service.save_connection({**item.create_fields(), "scope": body.scope}, user=user) for item in plan.connections]
        return {"connections": connections, "warnings": plan.warnings, "binding_required": True}

    return router
