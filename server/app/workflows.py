"""Versioned workflow contracts and bounded agent-node execution.

Most node types remain declarative and local-only.  Agent nodes can opt into a
provider-backed mode after selecting a public catalog model; the call is still
bounded, permission-pinned and recorded in the workflow run trace.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import defaultdict, deque
from datetime import timedelta
from time import perf_counter

import httpx
from fastapi import APIRouter, Depends, Query

from .db import dump, now, uid
from .models import (
    WorkflowCreate,
    WorkflowDraftPatch,
    WorkflowGraph,
    WorkflowNodeDebugRequest,
    WorkflowRollback,
    WorkflowRunRequest,
    WorkflowTemplateCreate,
)
from .security import AppError, SENSITIVE


_NODE_TYPES = {
    "input",
    "agent",
    "skill",
    "knowledge_search",
    "mcp_read",
    "condition",
    "approval",
    "artifact",
}
_NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_MAX_JSON = 16_000
_FIELD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_REASONING_LEVEL = re.compile(r"^[A-Za-z0-9._-]{1,30}$")
_NODE_OUTPUT_SOURCE = re.compile(r"^node\.([A-Za-z0-9_-]{1,64})\.output(?:\.([A-Za-z][A-Za-z0-9_-]{0,63}))?$")
_BINDING_SOURCE = re.compile(r"^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)$")
_REFERENCE_SOURCE = r"(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?)"
_KNOWLEDGE_TEMPLATE = re.compile(r"\{\{\s*(" + _REFERENCE_SOURCE + r")\s*\}\}")
_MAX_AGENT_CONTEXT = 12_000
_MAX_AGENT_OUTPUT = 12_000
_OUTPUT_TYPES = {"text", "number", "boolean", "object", "array"}
_MAX_OUTPUT_SCHEMA_FIELDS = 20
_MAX_OUTPUT_SCHEMA_DESCRIPTION = 240
_MAX_KNOWLEDGE_QUERY = 2_000
_MAX_KNOWLEDGE_CONTEXT = 12_000
_MAX_TOOL_OUTPUT = 12_000
_MAX_TOOL_ARGUMENTS = 4_000
_CONDITION_OPERATORS = {"exists", "eq", "neq", "gt", "gte", "lt", "lte", "contains", "in"}
_MCP_TOOL_NAME = re.compile(r"^mcp__c([A-Za-z0-9_-]{1,100})__([A-Za-z0-9_.-]{1,100})$")
_TOOL_TEMPLATE = re.compile(r"\{\{\s*(" + _REFERENCE_SOURCE + r")\s*\}\}")


_TEMPLATE_DEFINITIONS = {
    "finance-application-v1": {
        "template_id": "finance-application-v1",
        "name": "财务申请（合成模板）",
        "description": "表单填报、政策检索、金额条件和人工审批，全部使用合成字段。",
        "synthetic": True,
        "graph": {
            "nodes": [
                {"id": "start", "type": "input", "label": "开始 / 输入", "config": {"mode": "synthetic", "form_fields": [{"id": "purpose", "type": "text", "label": "申请事项", "required": True}, {"id": "amount", "type": "number", "label": "申请金额", "required": True}, {"id": "date", "type": "date", "label": "申请日期", "required": True}]}},
                {"id": "form", "type": "skill", "label": "整理申请信息", "config": {"mode": "synthetic", "template_field": "申请事项"}},
                {"id": "policy", "type": "knowledge_search", "label": "检索财务政策", "config": {"mode": "synthetic", "knowledge_scope": "synthetic-finance"}},
                {"id": "amount", "type": "condition", "label": "判断金额边界", "config": {"mode": "synthetic", "amount_limit": 5000, "currency": "CNY"}},
                {"id": "approval", "type": "approval", "label": "部门负责人审批", "config": {"mode": "synthetic", "role": "department_owner"}},
                {"id": "export", "type": "artifact", "label": "生成申请表", "config": {"mode": "synthetic", "format": "xlsx"}},
            ],
            "edges": [
                {"source": "start", "target": "form", "label": "提交申请"},
                {"source": "form", "target": "policy", "label": "补充依据"},
                {"source": "policy", "target": "amount", "label": "核对规则"},
                {"source": "amount", "target": "approval", "label": "提交审批"},
                {"source": "approval", "target": "export", "label": "审批结果"},
            ],
        },
    },
    "finance-reimbursement-v1": {
        "template_id": "finance-reimbursement-v1",
        "name": "财务报销（合成模板）",
        "description": "票据填报、费用政策检索、合规条件、双人审批和 Excel 产物的合成流程。",
        "synthetic": True,
        "graph": {
            "nodes": [
                {"id": "start", "type": "input", "label": "开始 / 输入", "config": {"mode": "synthetic", "form_fields": [{"id": "reason", "type": "text", "label": "报销事由", "required": True}, {"id": "amount", "type": "number", "label": "报销金额", "required": True}, {"id": "date", "type": "date", "label": "发生日期", "required": True}]}},
                {"id": "receipt", "type": "skill", "label": "整理报销信息", "config": {"mode": "synthetic", "template_field": "报销事项"}},
                {"id": "policy", "type": "knowledge_search", "label": "检索费用政策", "config": {"mode": "synthetic", "knowledge_scope": "synthetic-finance"}},
                {"id": "compliance", "type": "condition", "label": "检查合规条件", "config": {"mode": "synthetic", "required_fields": ["金额", "事由", "日期"]}},
                {"id": "dual", "type": "approval", "label": "双人审批", "config": {"mode": "synthetic", "required_approvals": 2}},
                {"id": "export", "type": "artifact", "label": "生成报销台账", "config": {"mode": "synthetic", "format": "xlsx"}},
            ],
            "edges": [
                {"source": "start", "target": "receipt", "label": "提交报销单"},
                {"source": "receipt", "target": "policy", "label": "查找依据"},
                {"source": "policy", "target": "compliance", "label": "核对字段"},
                {"source": "compliance", "target": "dual", "label": "进入审批"},
                {"source": "dual", "target": "export", "label": "留存结果"},
            ],
        },
    },
}

_WORKFLOW_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_approvals(
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    workflow_id TEXT NOT NULL REFERENCES workflows(id),
    node_id TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    status TEXT NOT NULL,
    decision TEXT,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT REFERENCES users(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS workflow_approvals_run_node ON workflow_approvals(workflow_run_id,node_id);
CREATE INDEX IF NOT EXISTS workflow_approvals_pending ON workflow_approvals(status,created_at);
CREATE TABLE IF NOT EXISTS workflow_tool_evidence(
    id TEXT PRIMARY KEY,
    workflow_run_id TEXT NOT NULL REFERENCES workflow_runs(id),
    node_id TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_tool_evidence_run ON workflow_tool_evidence(workflow_run_id,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS workflow_tool_evidence_digest ON workflow_tool_evidence(workflow_run_id,node_id,digest);
"""


def _template(template_id: str) -> dict:
    value = _TEMPLATE_DEFINITIONS.get(template_id)
    if not value:
        raise AppError(404, "WORKFLOW_TEMPLATE_NOT_FOUND", "合成工作流模板不存在")
    return json.loads(json.dumps(value, ensure_ascii=False))


def _graph_payload(graph: WorkflowGraph | dict) -> dict:
    if isinstance(graph, WorkflowGraph):
        return graph.model_dump()
    return graph


def _walk_values(value, depth=0):
    if depth > 6:
        raise AppError(422, "WORKFLOW_CONFIG_TOO_DEEP", "工作流节点配置层级过深")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 100:
                raise AppError(422, "WORKFLOW_CONFIG_INVALID", "节点配置字段无效")
            if SENSITIVE.search(key):
                raise AppError(422, "WORKFLOW_SECRET_FORBIDDEN", "工作流图中不能保存凭据或敏感字段")
            yield from _walk_values(child, depth + 1)
    elif isinstance(value, list):
        if len(value) > 100:
            raise AppError(422, "WORKFLOW_CONFIG_INVALID", "节点配置列表过长")
        for child in value:
            yield from _walk_values(child, depth + 1)
    elif isinstance(value, str):
        if len(value) > 4000:
            raise AppError(422, "WORKFLOW_CONFIG_INVALID", "节点配置文本过长")
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise AppError(422, "WORKFLOW_CONFIG_INVALID", "节点配置只能包含 JSON 基础类型")


def _validate_form_fields(config: dict) -> list[dict]:
    fields = config.get("form_fields", [])
    if fields is None:
        return []
    if not isinstance(fields, list) or len(fields) > 30:
        raise AppError(422, "WORKFLOW_FORM_INVALID", "表单字段数量超出限制")
    result = []
    seen = set()
    for field in fields:
        if not isinstance(field, dict):
            raise AppError(422, "WORKFLOW_FORM_INVALID", "表单字段格式无效")
        field_id = field.get("id")
        field_type = field.get("type", "text")
        if not isinstance(field_id, str) or not _FIELD_ID.fullmatch(field_id) or field_id in seen:
            raise AppError(422, "WORKFLOW_FORM_INVALID", "表单字段标识必须唯一且格式正确")
        if field_type not in {"text", "number", "date", "choice"}:
            raise AppError(422, "WORKFLOW_FORM_INVALID", "表单字段类型不在允许范围内")
        label = field.get("label", field_id)
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise AppError(422, "WORKFLOW_FORM_INVALID", "表单字段名称无效")
        if field_type == "choice" and (not isinstance(field.get("options"), list) or not field["options"] or len(field["options"]) > 30):
            raise AppError(422, "WORKFLOW_FORM_INVALID", "选择字段必须配置选项")
        seen.add(field_id)
        result.append({"id": field_id, "type": field_type, "label": label, "required": bool(field.get("required", False)), **({"options": field["options"]} if field_type == "choice" else {})})
    return result


def _output_schema(config: dict) -> dict | None:
    """Return the bounded structured-output contract for an agent node.

    The editor stores the contract as JSON text so a user can type it without
    the UI having to maintain a half-valid object on every keystroke.  The
    server accepts the object form as well for API clients.
    """

    raw = config.get("output_schema")
    if raw is None:
        raw = config.get("output_schema_json")
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        if len(raw) > _MAX_JSON:
            raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出结构定义过长")
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出结构必须是有效的 JSON 对象") from error
    if not isinstance(raw, dict):
        raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出结构必须是 JSON 对象")
    fields = raw.get("fields", [])
    if not isinstance(fields, list) or len(fields) > _MAX_OUTPUT_SCHEMA_FIELDS:
        raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出字段最多配置 20 项")
    normalized_fields = []
    seen = set()
    for field in fields:
        if not isinstance(field, dict):
            raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出字段格式无效")
        name = field.get("name", field.get("id"))
        field_type = field.get("type", "text")
        if not isinstance(name, str) or not _FIELD_ID.fullmatch(name) or name in seen:
            raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出字段名称必须唯一且使用英文标识")
        if field_type not in _OUTPUT_TYPES:
            raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出字段类型不在允许范围内")
        required = field.get("required", False)
        if not isinstance(required, bool):
            raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出字段 required 必须是布尔值")
        description = field.get("description", "")
        if not isinstance(description, str) or len(description) > _MAX_OUTPUT_SCHEMA_DESCRIPTION:
            raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出字段说明过长或格式无效")
        seen.add(name)
        normalized_fields.append({"name": name, "type": field_type, "required": required, **({"description": description} if description else {})})
    allow_extra = raw.get("allow_extra", False)
    if not isinstance(allow_extra, bool):
        raise AppError(422, "WORKFLOW_AGENT_OUTPUT_SCHEMA_INVALID", "智能体输出结构 allow_extra 必须是布尔值")
    if not normalized_fields and not allow_extra:
        return None
    return {"fields": normalized_fields, "allow_extra": allow_extra}


def _validate_agent_config(config: dict, node_type: str) -> None:
    """Validate the public, non-secret portion of an agent node contract."""

    mode = config.get("mode", "synthetic")
    if mode not in {"synthetic", "model"}:
        raise AppError(422, "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN", "智能体节点只允许 synthetic 或 model 执行模式")
    if mode == "model" and node_type != "agent":
        raise AppError(422, "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN", "只有智能体节点可以调用模型")
    for key, limit, code in (("role", 200, "WORKFLOW_AGENT_ROLE_INVALID"), ("system_prompt", 4000, "WORKFLOW_AGENT_PROMPT_INVALID"), ("prompt", 1000, "WORKFLOW_AGENT_PROMPT_INVALID")):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > limit):
            raise AppError(422, code, "智能体节点的角色或提示词无效")
    model_id = config.get("model_id")
    if model_id is not None and (not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id)):
        raise AppError(422, "WORKFLOW_AGENT_MODEL_INVALID", "智能体节点模型必须来自公开模型目录")
    reasoning_level = config.get("reasoning_level")
    if reasoning_level is not None and (not isinstance(reasoning_level, str) or not _REASONING_LEVEL.fullmatch(reasoning_level)):
        raise AppError(422, "WORKFLOW_AGENT_REASONING_INVALID", "智能体节点推理级别无效")
    temperature = config.get("temperature")
    if temperature is not None and (isinstance(temperature, bool) or not isinstance(temperature, (int, float)) or not 0 <= temperature <= 2):
        raise AppError(422, "WORKFLOW_AGENT_PARAMETER_INVALID", "温度必须在 0 到 2 之间")
    max_tokens = config.get("max_tokens")
    if max_tokens is not None and (isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 32 <= max_tokens <= 4096):
        raise AppError(422, "WORKFLOW_AGENT_PARAMETER_INVALID", "输出长度必须在 32 到 4096 之间")
    max_retries = config.get("max_retries", 0)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or not 0 <= max_retries <= 3:
        raise AppError(422, "WORKFLOW_AGENT_RETRY_INVALID", "模型失败重试次数必须在 0 到 3 次之间")
    retry_backoff_ms = config.get("retry_backoff_ms", 250)
    if isinstance(retry_backoff_ms, bool) or not isinstance(retry_backoff_ms, int) or not 0 <= retry_backoff_ms <= 5_000:
        raise AppError(422, "WORKFLOW_AGENT_RETRY_INVALID", "模型重试间隔必须在 0 到 5000 毫秒之间")
    _output_schema(config)
    bindings = config.get("input_bindings", [])
    if bindings is None:
        return
    if not isinstance(bindings, list) or len(bindings) > 20:
        raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", "智能体输入变量最多配置 20 项")
    seen = set()
    for binding in bindings:
        if not isinstance(binding, dict):
            raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", "智能体输入变量格式无效")
        key, source = binding.get("key"), binding.get("source")
        if not isinstance(key, str) or not _FIELD_ID.fullmatch(key) or key in seen:
            raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", "智能体输入变量名称必须唯一且使用英文标识")
        if not isinstance(source, str) or not _BINDING_SOURCE.fullmatch(source):
            raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", "变量来源必须是 input、input.字段、node.节点.output 或 node.节点.output.字段")
        seen.add(key)


def _node_output_source(source: str) -> tuple[str, str | None] | None:
    match = _NODE_OUTPUT_SOURCE.fullmatch(source)
    return (match.group(1), match.group(2)) if match else None


def _knowledge_query_sources(query: str | None) -> list[str]:
    """Return only the bounded input/output references allowed in a query template."""

    if query is None or (isinstance(query, str) and not query.strip()):
        return []
    if not isinstance(query, str) or len(query) > _MAX_KNOWLEDGE_QUERY:
        raise AppError(422, "WORKFLOW_KNOWLEDGE_QUERY_INVALID", "知识检索问题必须是有效的文本模板")
    sources = _KNOWLEDGE_TEMPLATE.findall(query)
    # Do not silently leave a Jinja-like expression in a prompt.  The workflow
    # contract supports only the two explicit, read-only reference forms above.
    if ("{{" in query or "}}" in query) and len(sources) != query.count("{{"):
        raise AppError(422, "WORKFLOW_KNOWLEDGE_QUERY_INVALID", "知识检索问题只能引用 input 或上游 node 输出")
    return list(sources)


def _validate_knowledge_config(config: dict) -> None:
    _knowledge_query_sources(config.get("query"))
    top_k = config.get("top_k", 6)
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 20:
        raise AppError(422, "WORKFLOW_KNOWLEDGE_CONFIG_INVALID", "知识检索召回条数必须在 1 到 20 之间")
    retrieval_mode = config.get("retrieval_mode", "hybrid")
    if retrieval_mode not in {"hybrid", "bm25", "vector"}:
        raise AppError(422, "WORKFLOW_KNOWLEDGE_CONFIG_INVALID", "知识检索模式只能是 hybrid、bm25 或 vector")
    context_chars = config.get("context_chars", _MAX_KNOWLEDGE_CONTEXT)
    if isinstance(context_chars, bool) or not isinstance(context_chars, int) or not 0 <= context_chars <= _MAX_KNOWLEDGE_CONTEXT:
        raise AppError(422, "WORKFLOW_KNOWLEDGE_CONFIG_INVALID", "知识检索上下文长度超出限制")


def _validate_condition_config(config: dict) -> None:
    operator = config.get("operator")
    if operator is not None and operator not in _CONDITION_OPERATORS:
        raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", "条件分支运算符不在允许范围内")
    field = config.get("field")
    if field is not None and (not isinstance(field, str) or not _FIELD_ID.fullmatch(field)):
        raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", "条件分支字段必须使用英文标识")
    value_source = config.get("value_source")
    if value_source not in (None, "") and (not isinstance(value_source, str) or not _BINDING_SOURCE.fullmatch(value_source)):
        raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", "条件分支值来源必须是 input、input.字段、node.节点.output 或 node.节点.output.字段")
    required_fields = config.get("required_fields")
    if required_fields is not None:
        if not isinstance(required_fields, list) or len(required_fields) > 30 or any(not isinstance(item, str) or not item.strip() or len(item) > 120 for item in required_fields):
            raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", "条件分支必填字段配置无效")
    for key in ("true_label", "false_label"):
        value = config.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 120):
            raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", "条件分支标签配置无效")


def _parse_mcp_tool_name(tool_name: str) -> tuple[str, str]:
    match = _MCP_TOOL_NAME.fullmatch(tool_name)
    if not match:
        raise AppError(422, "WORKFLOW_TOOL_NAME_INVALID", "只读工具必须使用已绑定的 MCP 工具名称")
    return match.group(1), match.group(2)


def _validate_mcp_read_config(config: dict) -> None:
    mode = config.get("mode", "synthetic")
    if mode not in {"synthetic", "live"}:
        raise AppError(422, "WORKFLOW_TOOL_MODE_INVALID", "只读工具节点只能使用合成或实时调用模式")
    tool_name = config.get("tool_name")
    if tool_name is not None:
        if not isinstance(tool_name, str) or len(tool_name) > 220:
            raise AppError(422, "WORKFLOW_TOOL_NAME_INVALID", "只读工具名称无效")
        _parse_mcp_tool_name(tool_name)
    if mode == "live" and not tool_name:
        raise AppError(422, "WORKFLOW_TOOL_NAME_REQUIRED", "实时只读工具必须选择已授权的 MCP 工具")
    arguments_json = config.get("arguments_json", "{}")
    if not isinstance(arguments_json, str) or len(arguments_json) > _MAX_TOOL_ARGUMENTS:
        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数模板过长")
    try:
        arguments = json.loads(arguments_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数必须是有效的 JSON 对象") from error
    if not isinstance(arguments, dict):
        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数必须是 JSON 对象")
    list(_walk_values(arguments))
    _tool_argument_sources(arguments)
    timeout = config.get("timeout_seconds", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
        raise AppError(422, "WORKFLOW_TOOL_TIMEOUT_INVALID", "工具超时时间必须在 1 到 60 秒之间")


def _tool_argument_sources(value, depth=0) -> list[str]:
    if depth > 6:
        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数模板层级过深")
    if isinstance(value, str):
        matches = list(_TOOL_TEMPLATE.finditer(value))
        if ("{{" in value or "}}" in value) and len(matches) != value.count("{{"):
            raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数只能引用 input 或上游 node 输出")
        return [match.group(1) for match in matches]
    if isinstance(value, dict):
        sources = []
        for item in value.values():
            sources.extend(_tool_argument_sources(item, depth + 1))
        return sources
    if isinstance(value, list):
        sources = []
        for item in value:
            sources.extend(_tool_argument_sources(item, depth + 1))
        return sources
    return []


def _agent_selection(service, assistant: dict, config: dict) -> dict:
    """Resolve a node's public model selection and pin its provider digest."""

    model_id = config.get("model_id") or assistant.get("default_model_id") or service.catalog.default_model_id
    level = config.get("reasoning_level")
    if not config.get("model_id"):
        level = level or assistant.get("default_reasoning_level")
    selection = service.catalog.select(model_id, level)
    return service.pin_provider(selection)


def _validate_model_nodes(service, assistant: dict, graph: dict) -> None:
    for node in graph.get("nodes", []):
        config = node.get("config") or {}
        if config.get("mode", "synthetic") != "model":
            continue
        if node.get("type") != "agent":
            raise AppError(422, "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN", "只有智能体节点可以调用模型")
        _agent_selection(service, assistant, config)


def validate_graph(graph: WorkflowGraph | dict) -> tuple[dict, str, list[str]]:
    """Validate a graph and return its canonical payload, hash and order."""

    payload = _graph_payload(graph)
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    edges = payload.get("edges", []) if isinstance(payload, dict) else None
    if not isinstance(nodes, list) or not nodes or len(nodes) > 64:
        raise AppError(422, "WORKFLOW_GRAPH_INVALID", "工作流至少需要一个节点，且不能超过 64 个节点")
    if not isinstance(edges, list) or len(edges) > 128:
        raise AppError(422, "WORKFLOW_GRAPH_INVALID", "工作流连线数量超出限制")

    node_map = {}
    for node in nodes:
        if not isinstance(node, dict):
            raise AppError(422, "WORKFLOW_NODE_INVALID", "工作流节点格式无效")
        node_id, node_type = node.get("id"), node.get("type")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id) or node_id in node_map:
            raise AppError(422, "WORKFLOW_NODE_INVALID", "节点标识必须唯一且只含字母、数字、下划线或短横线")
        if node_type not in _NODE_TYPES:
            raise AppError(422, "WORKFLOW_NODE_INVALID", "节点类型不在安全允许范围内")
        config = node.get("config", {})
        if not isinstance(config, dict):
            raise AppError(422, "WORKFLOW_CONFIG_INVALID", "节点配置必须是对象")
        list(_walk_values(config))
        _validate_form_fields(config)
        if node_type == "knowledge_search":
            _validate_knowledge_config(config)
        if node_type == "condition":
            _validate_condition_config(config)
        mode = config.get("mode")
        if mode is not None and mode not in {"synthetic", "model", "live"}:
            raise AppError(422, "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN", "当前工作流只允许 synthetic、model 或 live 执行模式")
        if node_type == "mcp_read":
            _validate_mcp_read_config(config)
        elif node_type == "agent" or mode == "model":
            _validate_agent_config(config, node_type)
        elif mode == "live":
            raise AppError(422, "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN", "只有只读工具节点可以使用 live 执行模式")
        node_map[node_id] = {
            "id": node_id,
            "type": node_type,
            "label": str(node.get("label") or "")[:120],
            "config": config,
        }

    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: 0 for node_id in node_map}
    edge_seen = set()
    canonical_edges = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise AppError(422, "WORKFLOW_EDGE_INVALID", "工作流连线格式无效")
        source, target = edge.get("source"), edge.get("target")
        key = (source, target)
        if source not in node_map or target not in node_map or source == target or key in edge_seen:
            raise AppError(422, "WORKFLOW_EDGE_INVALID", "工作流连线必须连接两个不同且存在的节点")
        edge_seen.add(key)
        adjacency[source].append(target)
        indegree[target] += 1
        canonical_edges.append({"source": source, "target": target, "label": str(edge.get("label") or "")[:120]})

    if len(node_map) > 1:
        isolated = [node_id for node_id, degree in {key: indegree[key] + len(adjacency[key]) for key in node_map}.items() if degree == 0]
        if isolated:
            raise AppError(422, "WORKFLOW_GRAPH_DISCONNECTED", "不能保存孤立节点")

    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    order = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for target in sorted(adjacency[node_id]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(order) != len(node_map):
        raise AppError(422, "WORKFLOW_GRAPH_CYCLE", "工作流不能包含循环依赖")

    order_index = {node_id: index for index, node_id in enumerate(order)}
    for node_id, node in node_map.items():
        for binding in (node["config"].get("input_bindings") or []):
            source = binding.get("source", "")
            if not source.startswith("node."):
                continue
            parsed_source = _node_output_source(source)
            if not parsed_source:
                raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", "变量来源格式无效")
            source_id, field_name = parsed_source
            if source_id not in node_map:
                raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", f"变量来源节点不存在：{source_id}")
            if source_id == node_id or order_index[source_id] >= order_index[node_id]:
                raise AppError(422, "WORKFLOW_AGENT_BINDING_ORDER", "变量只能引用已经完成的上游节点输出")
            if field_name:
                source_node = node_map[source_id]
                source_config = source_node.get("config") or {}
                source_schema = _output_schema(source_config) if source_node.get("type") == "agent" and source_config.get("mode", "synthetic") == "model" else None
                declared_fields = {item["name"] for item in (source_schema or {}).get("fields", [])}
                if not source_schema or (field_name not in declared_fields and not source_schema.get("allow_extra")):
                    raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", f"变量来源节点未声明可用的模型输出字段：{source_id}.{field_name}")
        value_source = node["config"].get("value_source") if node["type"] == "condition" else None
        if isinstance(value_source, str) and value_source.startswith("node."):
            parsed_source = _node_output_source(value_source)
            if not parsed_source:
                raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", "条件分支值来源格式无效")
            source_id, field_name = parsed_source
            if source_id not in node_map:
                raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", f"条件分支值来源节点不存在：{source_id}")
            if source_id == node_id or order_index[source_id] >= order_index[node_id]:
                raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", "条件分支只能引用已经完成的上游节点输出")
            if field_name:
                source_node = node_map[source_id]
                source_config = source_node.get("config") or {}
                source_schema = _output_schema(source_config) if source_node.get("type") == "agent" and source_config.get("mode", "synthetic") == "model" else None
                declared_fields = {item["name"] for item in (source_schema or {}).get("fields", [])}
                if not source_schema or (field_name not in declared_fields and not source_schema.get("allow_extra")):
                    raise AppError(422, "WORKFLOW_CONDITION_CONFIG_INVALID", f"条件分支值来源未声明可用的模型输出字段：{source_id}.{field_name}")
        if node["type"] == "mcp_read":
            try:
                tool_arguments = json.loads(node["config"].get("arguments_json", "{}") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数必须是有效的 JSON 对象") from error
            for source in _tool_argument_sources(tool_arguments):
                if not source.startswith("node."):
                    continue
                parsed_source = _node_output_source(source)
                if not parsed_source:
                    raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数引用的来源格式无效")
                source_id, field_name = parsed_source
                if source_id not in node_map:
                    raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", f"工具参数引用的来源节点不存在：{source_id}")
                if source_id == node_id or order_index[source_id] >= order_index[node_id]:
                    raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_ORDER", "工具参数只能引用已经完成的上游节点输出")
                if field_name:
                    source_node = node_map[source_id]
                    source_config = source_node.get("config") or {}
                    source_schema = _output_schema(source_config) if source_node.get("type") == "agent" and source_config.get("mode", "synthetic") == "model" else None
                    declared_fields = {item["name"] for item in (source_schema or {}).get("fields", [])}
                    if not source_schema or (field_name not in declared_fields and not source_schema.get("allow_extra")):
                        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", f"工具参数引用的字段未声明可用的模型输出：{source_id}.{field_name}")
        if node["type"] == "knowledge_search":
            for source in _knowledge_query_sources(node["config"].get("query")):
                if not source.startswith("node."):
                    continue
                parsed_source = _node_output_source(source)
                if not parsed_source:
                    raise AppError(422, "WORKFLOW_KNOWLEDGE_QUERY_INVALID", "知识检索问题引用的来源格式无效")
                source_id, field_name = parsed_source
                if source_id not in node_map:
                    raise AppError(422, "WORKFLOW_KNOWLEDGE_QUERY_INVALID", f"知识检索问题引用的来源节点不存在：{source_id}")
                if source_id == node_id or order_index[source_id] >= order_index[node_id]:
                    raise AppError(422, "WORKFLOW_KNOWLEDGE_QUERY_ORDER", "知识检索问题只能引用已经完成的上游节点输出")
                if field_name:
                    source_node = node_map[source_id]
                    source_config = source_node.get("config") or {}
                    source_schema = _output_schema(source_config) if source_node.get("type") == "agent" and source_config.get("mode", "synthetic") == "model" else None
                    declared_fields = {item["name"] for item in (source_schema or {}).get("fields", [])}
                    if not source_schema or (field_name not in declared_fields and not source_schema.get("allow_extra")):
                        raise AppError(422, "WORKFLOW_KNOWLEDGE_QUERY_INVALID", f"知识检索问题引用的字段未声明可用的模型输出：{source_id}.{field_name}")

    condition_edges: dict[str, list[dict]] = defaultdict(list)
    for edge in canonical_edges:
        condition_edges[edge["source"]].append(edge)
    for node_id, node in node_map.items():
        if node["type"] != "condition" or len(condition_edges[node_id]) <= 1:
            continue
        # Validate both outcomes while saving the graph so a published run
        # cannot discover a missing branch label only after receiving input.
        _condition_selected_edges(node, condition_edges[node_id], True)
        _condition_selected_edges(node, condition_edges[node_id], False)

    canonical = {"nodes": [node_map[node_id] for node_id in sorted(node_map)], "edges": sorted(canonical_edges, key=lambda item: (item["source"], item["target"], item["label"]))}
    serialized = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _MAX_JSON:
        raise AppError(422, "WORKFLOW_GRAPH_TOO_LARGE", "工作流定义超过大小限制")
    return canonical, hashlib.sha256(serialized.encode("utf-8")).hexdigest(), order


def _view(service, row, user, include_graph=True):
    assistant = service.require("assistants", row["assistant_id"])
    versions = service.db.all("SELECT * FROM workflow_versions WHERE workflow_id=? ORDER BY version DESC", (row["id"],))
    execution_modes = set()
    for item in versions:
        try:
            graph = json.loads(item["graph"])
            execution_modes.update((node.get("config") or {}).get("mode") or "synthetic" for node in graph.get("nodes", []))
        except (TypeError, ValueError, json.JSONDecodeError):
            execution_modes.add("unknown")
    result = {
        "id": row["id"],
        "assistant_id": row["assistant_id"],
        "name": row["name"],
        "description": row["description"],
        "scope": row["scope"],
        "owner_id": row["owner_id"],
        "revision": row["revision"],
        "draft_version": row["draft_version"],
        "published_version": row["published_version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "status": "published" if row["published_version"] else "draft",
        "versions": [
            {
                "version": item["version"],
                "status": item["status"],
                "graph_hash": item["graph_hash"],
                "created_by": item["created_by"],
                "created_at": item["created_at"],
                "published_at": item["published_at"],
                **({"graph": json.loads(item["graph"])} if include_graph else {}),
            }
            for item in versions
        ],
        "synthetic_only": execution_modes <= {"synthetic"},
        "execution_modes": sorted(execution_modes),
        "model_execution_enabled": True,
    }
    return result


def _editable(service, workflow_id, user):
    row = service.require("workflows", workflow_id)
    assistant = service.require_resource("assistants", row["assistant_id"], user, edit=True)
    if not service.assistant_access(assistant, user):
        raise AppError(403, "ASSISTANT_UNAVAILABLE", "您没有编辑此助手工作流的权限")
    return row


def _readable(service, workflow_id, user):
    row = service.require("workflows", workflow_id)
    assistant = service.usable_assistant(row["assistant_id"], user)
    if not service.assistant_access(assistant, user):
        raise AppError(403, "ASSISTANT_UNAVAILABLE", "您没有使用此助手工作流的权限")
    return row


def _insert_version(service, workflow_id, version, status, graph, graph_hash, user_id, published_at=None):
    timestamp = now()
    service.db.execute(
        "INSERT INTO workflow_versions(workflow_id,version,status,graph,graph_hash,created_by,created_at,published_at) VALUES(?,?,?,?,?,?,?,?)",
        (workflow_id, version, status, dump(graph), graph_hash, user_id, timestamp, published_at),
    )


def create_workflow(service, assistant_id, body, user):
    assistant = service.require_resource("assistants", assistant_id, user, edit=True)
    if not service.assistant_access(assistant, user):
        raise AppError(403, "ASSISTANT_UNAVAILABLE", "您没有编辑此助手工作流的权限")
    graph, graph_hash, _ = validate_graph(body.graph)
    _validate_model_nodes(service, assistant, graph)
    timestamp, workflow_id = now(), uid()
    with service.db.transaction():
        service.db.execute(
            "INSERT INTO workflows(id,assistant_id,name,description,owner_id,scope,revision,draft_version,created_at,updated_at) VALUES(?,?,?,?,?,?,1,1,?,?)",
            (workflow_id, assistant_id, body.name.strip(), body.description, assistant.get("owner_id") or user["id"], assistant.get("scope", "private"), timestamp, timestamp),
        )
        _insert_version(service, workflow_id, 1, "draft", graph, graph_hash, user["id"])
    return _view(service, service.require("workflows", workflow_id), user)


def create_workflow_from_template(service, assistant_id, body: WorkflowTemplateCreate, user):
    template = _template(body.template_id)
    graph = WorkflowGraph.model_validate(template["graph"])
    payload = WorkflowCreate(
        name=(body.name or template["name"]).strip(),
        description=template["description"],
        graph=graph,
    )
    result = create_workflow(service, assistant_id, payload, user)
    result["template_id"] = template["template_id"]
    result["synthetic_template"] = True
    return result


def patch_draft(service, workflow_id, body, user):
    row = _editable(service, workflow_id, user)
    assistant = service.require_resource("assistants", row["assistant_id"], user, edit=True)
    observed_revision = row["revision"]
    if body.expected_revision is not None and body.expected_revision != observed_revision:
        raise AppError(409, "WORKFLOW_CONFLICT", "工作流已更新，请重新加载后再保存")
    graph, graph_hash, _ = validate_graph(body.graph)
    _validate_model_nodes(service, assistant, graph)
    latest = service.db.one("SELECT COALESCE(MAX(version),0) version FROM workflow_versions WHERE workflow_id=?", (workflow_id,))["version"]
    version = latest + 1
    timestamp = now()
    with service.db.transaction():
        service.db.execute("UPDATE workflow_versions SET status='superseded' WHERE workflow_id=? AND status='draft'", (workflow_id,))
        _insert_version(service, workflow_id, version, "draft", graph, graph_hash, user["id"])
        updated = service.db.execute("UPDATE workflows SET name=COALESCE(?,name),description=COALESCE(?,description),revision=revision+1,draft_version=?,updated_at=? WHERE id=? AND revision=?", (body.name, body.description, version, timestamp, workflow_id, observed_revision))
        if updated.rowcount != 1:
            raise AppError(409, "WORKFLOW_CONFLICT", "工作流已更新，请重新加载后再保存")
    return _view(service, service.require("workflows", workflow_id), user)


def publish(service, workflow_id, user):
    row = _editable(service, workflow_id, user)
    assistant = service.require_resource("assistants", row["assistant_id"], user, edit=True)
    draft = service.db.one("SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?", (workflow_id, row["draft_version"]))
    if not draft or draft["status"] not in {"draft", "superseded"}:
        raise AppError(409, "WORKFLOW_DRAFT_MISSING", "没有可发布的工作流草稿")
    graph, graph_hash, _ = validate_graph(json.loads(draft["graph"]))
    _validate_model_nodes(service, assistant, graph)
    if graph_hash != draft["graph_hash"]:
        raise AppError(409, "WORKFLOW_VERSION_CORRUPT", "工作流版本校验失败")
    timestamp = now()
    with service.db.transaction():
        service.db.execute("UPDATE workflow_versions SET status='archived' WHERE workflow_id=? AND status='published'", (workflow_id,))
        service.db.execute("UPDATE workflow_versions SET status='published',published_at=? WHERE workflow_id=? AND version=?", (timestamp, workflow_id, draft["version"]))
        service.db.execute("UPDATE workflows SET published_version=?,revision=revision+1,updated_at=? WHERE id=?", (draft["version"], timestamp, workflow_id))
    return _view(service, service.require("workflows", workflow_id), user)


def rollback(service, workflow_id, body: WorkflowRollback, user):
    row = _editable(service, workflow_id, user)
    assistant = service.require_resource("assistants", row["assistant_id"], user, edit=True)
    target = service.db.one("SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?", (workflow_id, body.version))
    if not target:
        raise AppError(404, "WORKFLOW_VERSION_NOT_FOUND", "工作流版本不存在")
    graph, graph_hash, _ = validate_graph(json.loads(target["graph"]))
    _validate_model_nodes(service, assistant, graph)
    latest = service.db.one("SELECT COALESCE(MAX(version),0) version FROM workflow_versions WHERE workflow_id=?", (workflow_id,))["version"]
    version = latest + 1
    timestamp = now()
    with service.db.transaction():
        service.db.execute("UPDATE workflow_versions SET status='superseded' WHERE workflow_id=? AND status='draft'", (workflow_id,))
        _insert_version(service, workflow_id, version, "draft", graph, graph_hash, user["id"])
        service.db.execute("UPDATE workflows SET revision=revision+1,draft_version=?,updated_at=? WHERE id=?", (version, timestamp, workflow_id))
    return _view(service, service.require("workflows", workflow_id), user)


def _validated_input(graph: dict, value: dict) -> dict:
    fields = []
    seen = set()
    for node in graph["nodes"]:
        for field in _validate_form_fields(node.get("config", {})):
            if field["id"] not in seen:
                seen.add(field["id"])
                fields.append(field)
    if not fields:
        return value
    missing = [field["id"] for field in fields if field["required"] and (field["id"] not in value or value[field["id"]] in (None, "", []))]
    if missing:
        raise AppError(422, "WORKFLOW_FORM_REQUIRED", "缺少必填字段：" + "、".join(missing))
    for field in fields:
        if field["id"] not in value or value[field["id"]] in (None, ""):
            continue
        actual = value[field["id"]]
        if field["type"] == "number" and (isinstance(actual, bool) or not isinstance(actual, (int, float))):
            raise AppError(422, "WORKFLOW_FORM_INVALID", f"字段 {field['id']} 必须是数字")
        if field["type"] == "choice" and actual not in field["options"]:
            raise AppError(422, "WORKFLOW_FORM_INVALID", f"字段 {field['id']} 不在允许选项内")
    # Template runs persist only declared fields. Unknown metadata is not
    # copied into a durable artifact preview.
    return {field["id"]: value[field["id"]] for field in fields if field["id"] in value}


def _artifact_previews(graph: dict, value: dict) -> list[dict]:
    previews = []
    for node in graph["nodes"]:
        if node["type"] != "artifact":
            continue
        config = node.get("config", {})
        columns = config.get("columns") if isinstance(config.get("columns"), list) else list(value)
        columns = [column for column in columns if isinstance(column, str)][:50]
        previews.append({
            "node_id": node["id"],
            "kind": "structured_preview",
            "format": config.get("format", "structured"),
            "synthetic": True,
            "columns": columns,
            "rows": [[value.get(column) for column in columns]],
            "written": False,
        })
    return previews


def _bind_conversation(service, workflow_row: dict, conversation_id: str | None, user: dict):
    """Resolve an optional synthetic run session without crossing assistants."""

    if not conversation_id:
        return None
    conversation = service.require("conversations", conversation_id, user)
    if conversation["assistant_id"] != workflow_row["assistant_id"]:
        raise AppError(409, "WORKFLOW_SESSION_MISMATCH", "工作流只能绑定到同一助手的会话")
    if conversation["archived"]:
        raise AppError(409, "WORKFLOW_SESSION_ARCHIVED", "归档会话不能运行工作流")
    return conversation


def _completion_text(body) -> str:
    """Read text from the Anthropic-compatible and OpenAI-compatible shapes."""

    if not isinstance(body, dict):
        return ""
    content = body.get("content")
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if parts:
            return "".join(parts)
    if isinstance(content, str):
        return content
    choices = body.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                return message["content"]
            if isinstance(choice.get("text"), str):
                return choice["text"]
    return ""


def _binding_trace(config: dict) -> list[dict[str, str]]:
    return [{"key": item["key"], "source": item["source"]} for item in (config.get("input_bindings") or [])]


def _agent_system_prompt(assistant: dict, config: dict) -> tuple[str, str]:
    role = (config.get("role") or "工作流智能体").strip()
    instructions = (config.get("system_prompt") or assistant.get("instructions") or "请根据输入完成本节点任务，并明确区分事实、推断和待确认信息。").strip()
    return role, f"你是知行工作台工作流中的{role}。\n{instructions}\n只完成当前节点任务，不调用未授权的工具，不虚构外部数据。"[:6000]


def _bounded_structured_value(value, depth=0):
    """Bound model-produced JSON before persisting it in a workflow run."""

    if depth > 6:
        raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出层级过深")
    if isinstance(value, dict):
        if len(value) > 100:
            raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出字段过多")
        bounded = {}
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 100:
                raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出字段名称无效")
            bounded[key] = "[redacted]" if SENSITIVE.search(key) else _bounded_structured_value(child, depth + 1)
        return bounded
    if isinstance(value, list):
        if len(value) > 100:
            raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出数组过长")
        return [_bounded_structured_value(child, depth + 1) for child in value]
    if isinstance(value, str):
        return value[:4_000]
    if isinstance(value, float) and not math.isfinite(value):
        raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出包含非有限数字")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出包含不支持的值")


def _structured_value_matches(value, field_type: str) -> bool:
    if field_type == "text":
        return isinstance(value, str)
    if field_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type == "boolean":
        return isinstance(value, bool)
    if field_type == "object":
        return isinstance(value, dict)
    if field_type == "array":
        return isinstance(value, list)
    return False


def _parse_structured_agent_output(text: str, schema: dict | None) -> dict | None:
    """Parse and validate a model response against the node's output contract."""

    if not schema:
        return None
    candidate = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", candidate, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体未返回符合要求的 JSON 对象") from error
    if not isinstance(parsed, dict):
        raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出必须是 JSON 对象")
    fields = {field["name"]: field for field in schema.get("fields", [])}
    missing = [name for name, field in fields.items() if field.get("required") and name not in parsed]
    if missing:
        raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出缺少必填字段：" + "、".join(missing))
    if not schema.get("allow_extra"):
        extras = sorted(set(parsed) - set(fields))
        if extras:
            raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出包含未声明字段：" + "、".join(extras[:10]))
    for name, value in parsed.items():
        field = fields.get(name)
        if field and not _structured_value_matches(value, field["type"]):
            raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", f"智能体输出字段 {name} 的类型不符合约定")
    bounded = _bounded_structured_value(parsed)
    if not isinstance(bounded, dict):
        raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出格式无效")
    if len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))) > _MAX_AGENT_OUTPUT:
        raise AppError(502, "WORKFLOW_AGENT_OUTPUT_INVALID", "智能体结构化输出超过大小限制")
    return bounded


def _resolve_workflow_reference(source: str, safe_input: dict, outputs: dict[str, str] | None = None, structured_outputs: dict[str, dict] | None = None):
    outputs = outputs or {}
    structured_outputs = structured_outputs or {}
    if source == "input":
        return safe_input
    if source.startswith("input."):
        return safe_input.get(source[6:])
    source_id, field_name = _node_output_source(source) or (source[5:-7], None)
    return structured_outputs.get(source_id, {}).get(field_name) if field_name else outputs.get(source_id)


def _resolve_input_bindings(config: dict, safe_input: dict, outputs: dict[str, str], structured_outputs: dict[str, dict] | None = None) -> dict | None:
    bindings = config.get("input_bindings") or []
    if not bindings:
        return None
    resolved = {}
    for binding in bindings:
        value = _resolve_workflow_reference(binding["source"], safe_input, outputs, structured_outputs)
        resolved[binding["key"]] = value
    return resolved


def _tool_argument_value(source: str, safe_input: dict, outputs: dict[str, str], structured_outputs: dict[str, dict] | None = None):
    value = _resolve_workflow_reference(source, safe_input, outputs, structured_outputs)
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return value
    return value


def _render_tool_argument_value(value, safe_input: dict, outputs: dict[str, str], structured_outputs: dict[str, dict] | None = None, depth=0):
    if depth > 6:
        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数模板层级过深")
    if isinstance(value, str):
        matches = list(_TOOL_TEMPLATE.finditer(value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            return _tool_argument_value(matches[0].group(1), safe_input, outputs, structured_outputs)
        rendered = value
        for match in reversed(matches):
            replacement = _tool_argument_value(match.group(1), safe_input, outputs, structured_outputs)
            if isinstance(replacement, (dict, list)):
                replacement = json.dumps(replacement, ensure_ascii=False, separators=(",", ":"))
            rendered = rendered[:match.start()] + str(replacement if replacement is not None else "") + rendered[match.end():]
        return rendered
    if isinstance(value, dict):
        return {key: _render_tool_argument_value(item, safe_input, outputs, structured_outputs, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_tool_argument_value(item, safe_input, outputs, structured_outputs, depth + 1) for item in value]
    return value


def _tool_arguments(node: dict, safe_input: dict, outputs: dict[str, str], structured_outputs: dict[str, dict] | None = None) -> dict:
    config = node.get("config") or {}
    try:
        arguments = json.loads(config.get("arguments_json", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数模板不是有效 JSON") from error
    if not isinstance(arguments, dict):
        raise AppError(422, "WORKFLOW_TOOL_ARGUMENTS_INVALID", "工具参数必须是 JSON 对象")
    rendered = _render_tool_argument_value(arguments, safe_input, outputs, structured_outputs)
    list(_walk_values(rendered))
    return rendered


def _workflow_tool_scope(service, assistant: dict, user: dict, conversation: dict | None, nodes: list[dict]) -> dict[str, dict]:
    live_nodes = [node for node in nodes if node.get("type") == "mcp_read" and (node.get("config") or {}).get("mode", "synthetic") == "live"]
    if not live_nodes:
        return {}
    if not conversation:
        raise AppError(409, "WORKFLOW_TOOL_SESSION_REQUIRED", "实时工具节点必须绑定同一助手的运行会话")
    raw_bindings = assistant.get("connections", [])
    if isinstance(raw_bindings, str):
        try:
            raw_bindings = json.loads(raw_bindings or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AppError(409, "WORKFLOW_TOOL_SCOPE_INVALID", "助手的工具绑定配置无法读取") from error
    bindings = {item.get("connection_id"): item for item in raw_bindings if isinstance(item, dict) and item.get("connection_id")}
    scope = {}
    for node in live_nodes:
        tool_name = (node.get("config") or {}).get("tool_name")
        connection_id, server_tool = _parse_mcp_tool_name(tool_name)
        connection = service.require_resource("connections", connection_id, user)
        binding = bindings.get(connection_id)
        if not binding or server_tool not in binding.get("allowed_tools", []):
            raise AppError(403, "TOOL_DENIED", "只读工具不在助手的授权工具清单中")
        if server_tool not in binding.get("auto_approved_tools", []):
            raise AppError(403, "WORKFLOW_TOOL_NOT_AUTO_APPROVED", "工作流实时只读工具必须在助手中标记为自动批准")
        # authorize_tool applies the conversation's pinned capability snapshot
        # when one exists, so a workflow cannot widen an already-running chat.
        if service.authorize_tool(conversation["id"], tool_name, {}) is not True:
            raise AppError(403, "WORKFLOW_TOOL_NOT_AUTO_APPROVED", "当前会话未自动批准此只读工具")
        try:
            discovered = json.loads(connection.get("tools") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AppError(409, "WORKFLOW_TOOL_UNAVAILABLE", "连接工具清单无法读取，请重新测试连接") from error
        if not any(isinstance(item, dict) and item.get("name") == server_tool for item in discovered):
            raise AppError(409, "WORKFLOW_TOOL_UNAVAILABLE", "只读工具已不在连接的最新工具清单中，请重新测试连接")
        scope[node["id"]] = {
            "node_id": node["id"],
            "tool_name": tool_name,
            "server_tool": server_tool,
            "connection_id": connection_id,
            "connection_revision": connection["revision"],
        }
    return scope


def _bounded_tool_value(value, depth=0):
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:100]:
            if SENSITIVE.search(str(key)):
                continue
            result[str(key)[:100]] = _bounded_tool_value(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_bounded_tool_value(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return value[:12000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:200]


def _tool_result_body(result) -> dict:
    try:
        body = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    except (TypeError, ValueError):
        body = {"content": str(result)}
    bounded = _bounded_tool_value(body)
    return bounded if isinstance(bounded, dict) else {"content": bounded}


def _tool_sources(value, depth=0) -> list[dict]:
    if depth > 5:
        return []
    found = []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return _tool_sources(parsed, depth + 1)
    if isinstance(value, dict):
        sources = value.get("sources")
        if isinstance(sources, list):
            found.extend(item for item in sources[:30] if isinstance(item, dict))
        for key in ("structuredContent", "content", "data", "result"):
            if key in value:
                found.extend(_tool_sources(value[key], depth + 1))
    elif isinstance(value, list):
        for item in value[:40]:
            found.extend(_tool_sources(item, depth + 1))
    result, seen = [], set()
    for item in found[:30]:
        cleaned = _bounded_tool_value(item)
        digest = hashlib.sha256(json.dumps(cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        result.append({"digest": digest, "title": str(item.get("title") or item.get("name") or "业务数据")[:200], "excerpt": str(item.get("excerpt") or item.get("text") or "")[:2000], "locator": _bounded_tool_value(item.get("locator") if isinstance(item.get("locator"), dict) else {})})
    return result


async def _run_mcp_read(service, user: dict, conversation: dict | None, node: dict, tool_scope: dict, safe_input: dict, outputs: dict[str, str], structured_outputs: dict[str, dict] | None = None) -> tuple[dict, str, list[dict]]:
    if not conversation:
        raise AppError(409, "WORKFLOW_TOOL_SESSION_REQUIRED", "实时工具节点必须绑定同一助手的运行会话")
    config = node.get("config") or {}
    scope = tool_scope.get(node["id"])
    if not scope:
        raise AppError(409, "WORKFLOW_TOOL_SCOPE_INVALID", "实时工具节点权限快照不存在")
    arguments = _tool_arguments(node, safe_input, outputs, structured_outputs)
    if service.authorize_tool(conversation["id"], scope["tool_name"], arguments) is not True:
        raise AppError(403, "WORKFLOW_TOOL_NOT_AUTO_APPROVED", "当前会话未自动批准此只读工具")
    connection = service.require_resource("connections", scope["connection_id"], user)
    if connection["revision"] != scope["connection_revision"] or not connection["enabled"]:
        raise AppError(409, "WORKFLOW_TOOL_CONFIG_CHANGED", "工具连接配置已改变，请重新运行工作流")
    from .security import validate_mcp_url
    validate_mcp_url(connection["url"], service.settings.mcp_hosts)
    headers = {}
    credential = service.secrets.decrypt(connection.get("secret"))
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    timeout = max(1.0, min(60.0, float(config.get("timeout_seconds", 30))))
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)), follow_redirects=False, trust_env=False) as http:
            async with streamable_http_client(connection["url"], http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    result = await session.call_tool(scope["server_tool"], arguments=arguments, read_timeout_seconds=timedelta(seconds=timeout))
    except AppError:
        raise
    except Exception as error:
        raise AppError(502, "WORKFLOW_TOOL_FAILED", "实时只读工具暂时无法连接，请检查连接或稍后重试") from error
    if getattr(result, "isError", False):
        raise AppError(502, "WORKFLOW_TOOL_FAILED", "实时只读工具返回了错误")
    body = _tool_result_body(result)
    sources = _tool_sources(body)
    payload = {"kind": "mcp_read", "status": "completed", "tool_name": scope["tool_name"], "connection_id": scope["connection_id"], "result": body, "evidence": sources}
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized.encode("utf-8")) > _MAX_TOOL_OUTPUT:
        payload = {
            "kind": "mcp_read",
            "status": "completed",
            "tool_name": scope["tool_name"],
            "connection_id": scope["connection_id"],
            "result_preview": json.dumps(body, ensure_ascii=False, separators=(",", ":"))[:5_000],
            "evidence": sources[:5],
            "truncated": True,
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(serialized.encode("utf-8")) > _MAX_TOOL_OUTPUT:
            payload["result_preview"] = payload["result_preview"][:2_500]
            payload["evidence"] = sources[:2]
            serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return payload, serialized, sources


def _assistant_knowledge_scope(service, assistant: dict, user: dict) -> list[dict]:
    """Resolve the assistant's current, user-authorized knowledge bindings.

    A workflow graph never chooses arbitrary knowledge-base identifiers.  The
    assistant binding is the capability boundary, and the revision is carried
    into the run permission snapshot so a later audit can see what was used.
    """

    raw_ids = assistant.get("knowledge_base_ids", [])
    if isinstance(raw_ids, str):
        try:
            raw_ids = json.loads(raw_ids or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AppError(409, "WORKFLOW_KNOWLEDGE_SCOPE_INVALID", "助手的知识库绑定配置无法读取") from error
    if not isinstance(raw_ids, list):
        raise AppError(409, "WORKFLOW_KNOWLEDGE_SCOPE_INVALID", "助手的知识库绑定配置无效")
    scope = []
    seen = set()
    for kb_id in raw_ids[:100]:
        if not isinstance(kb_id, str) or not kb_id or kb_id in seen:
            continue
        # require() enforces the same resource and team/private access rules
        # as the normal knowledge tool.  Never trust a graph-supplied scope.
        kb = service.knowledge.require(kb_id, user)
        scope.append({
            "id": kb["id"],
            "revision": kb["revision"],
            "scope": kb["scope"],
            "enabled": bool(kb["enabled"]),
        })
        seen.add(kb_id)
    return scope


def _knowledge_query_value(source: str, safe_input: dict, outputs: dict[str, str], structured_outputs: dict[str, dict] | None = None):
    value = _resolve_workflow_reference(source, safe_input, outputs, structured_outputs)
    return "" if value is None else value


def _render_knowledge_query(node: dict, safe_input: dict, outputs: dict[str, str], structured_outputs: dict[str, dict] | None = None) -> str:
    config = node.get("config") or {}
    template = config.get("query")
    if not isinstance(template, str) or not template.strip():
        template = node.get("label") or "检索与当前节点相关的已授权知识"
    _knowledge_query_sources(template)

    def replace(match):
        value = _knowledge_query_value(match.group(1), safe_input, outputs, structured_outputs)
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value)[:4000]

    query = _KNOWLEDGE_TEMPLATE.sub(replace, template).strip()
    if not query:
        raise AppError(422, "WORKFLOW_KNOWLEDGE_QUERY_INVALID", "知识检索问题渲染后不能为空")
    return query[:_MAX_KNOWLEDGE_QUERY]


def _knowledge_source_view(item: dict) -> dict:
    """Keep source-bound retrieval evidence useful without persisting raw rows."""

    if not isinstance(item, dict):
        return {"title": "", "excerpt": ""}
    source = {
        "document_id": item.get("document_id"),
        "version": item.get("version"),
        "title": str(item.get("title") or item.get("name") or "")[:200],
        "knowledge_base_id": item.get("kb_id"),
        "knowledge_base": str(item.get("knowledge_base") or "")[:160],
        "page": item.get("page"),
        "section": str(item.get("section") or "")[:200],
        "chunk_id": item.get("chunk_id"),
        "sha256": item.get("sha256"),
        "rank": item.get("rank"),
        "score": item.get("score"),
        "retrieval_mode": item.get("retrieval_mode"),
        "evidence_type": item.get("evidence_type", "source_chunk"),
        "content_url": item.get("content_url"),
        "source_id": item.get("source_id"),
        "source_url": item.get("source_url"),
        "citation": item.get("citation"),
        "excerpt": str(item.get("excerpt") or item.get("text") or "")[:1200],
    }
    media = item.get("media")
    if isinstance(media, dict):
        media_view = {key: media.get(key) for key in ("kind", "document_id", "version", "sha256", "mime_type", "width", "height", "thumbnail_url", "ocr_status", "visual_index_status", "evidence_boundary") if media.get(key) is not None}
        regions = media.get("regions")
        if isinstance(regions, list):
            media_view["regions"] = regions[:4]
        if media_view:
            source["media"] = media_view
    return {key: value for key, value in source.items() if value is not None}


def _knowledge_result_payload(result: dict, query: str, retrieval_mode: str, context_chars: int, scope: list[dict]) -> dict:
    if not isinstance(result, dict):
        result = {}
    context_value = result.get("context") or {}
    if isinstance(context_value, dict):
        context_text = context_value.get("text") or ""
    else:
        context_text = str(context_value)
    context_text = str(context_text)[:context_chars]
    raw_results = result.get("results") if isinstance(result.get("results"), list) else []
    authorized_ids = {item["id"] for item in scope if item.get("enabled")}
    sources = [_knowledge_source_view(item) for item in raw_results if isinstance(item, dict) and item.get("kb_id") in authorized_ids][:20]
    retrieval = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
    retrieval = {key: value for key, value in retrieval.items() if isinstance(key, str) and len(key) <= 80 and isinstance(value, (str, int, float, bool, type(None)))}
    retrieval.setdefault("mode", retrieval_mode)
    retrieval["authorization"] = "assistant_knowledge_bindings"
    return {
        "kind": "knowledge_search",
        "status": "completed" if sources else "empty",
        "query": query,
        "context": context_text,
        "sources": sources,
        "source_count": len(sources),
        "retrieval": retrieval,
        "knowledge_base_ids": [item["id"] for item in scope if item.get("enabled")],
    }


def _serialize_knowledge_payload(payload: dict) -> tuple[dict, str]:
    """Bound structured retrieval output without cutting JSON in the middle."""

    for source in payload.get("sources", []):
        source["excerpt"] = str(source.get("excerpt") or "")[:600]
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > _MAX_AGENT_OUTPUT:
        payload["context"] = str(payload.get("context") or "")[:6_000]
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    while len(serialized) > _MAX_AGENT_OUTPUT and payload.get("sources"):
        payload["sources"].pop()
        payload["source_count"] = len(payload["sources"])
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return payload, serialized


async def _run_knowledge_search(service, assistant: dict, user: dict, node: dict, safe_input: dict, outputs: dict[str, str], scope: list[dict], structured_outputs: dict[str, dict] | None = None) -> tuple[dict, str]:
    config = node.get("config") or {}
    query = _render_knowledge_query(node, safe_input, outputs, structured_outputs)
    top_k = config.get("top_k", 6)
    retrieval_mode = config.get("retrieval_mode", "hybrid")
    context_chars = config.get("context_chars", _MAX_KNOWLEDGE_CONTEXT)
    enabled_ids = [item["id"] for item in scope if item.get("enabled")]
    try:
        if enabled_ids:
            result = await asyncio.to_thread(
                service.knowledge.search,
                enabled_ids,
                user,
                query,
                top_k,
                None,
                retrieval_mode,
                context_chars,
            )
        else:
            result = {"results": [], "retrieval": {"mode": retrieval_mode}, "context": {"text": "", "characters": 0, "truncated": False}}
    except AppError:
        raise
    except Exception as error:
        raise AppError(502, "WORKFLOW_KNOWLEDGE_FAILED", "工作流知识检索暂时失败，请稍后重试") from error
    payload = _knowledge_result_payload(result, query, retrieval_mode, context_chars, scope)
    return _serialize_knowledge_payload(payload)


def _upstream_output_preview(value: str) -> str:
    """Prefer the beginning of structured retrieval output, where context lives."""

    if isinstance(value, str) and value.startswith('{"kind":"knowledge_search"'):
        return value[:8_000]
    return value[-4_000:]


def _agent_user_prompt(node: dict, safe_input: dict, outputs: dict[str, str], config: dict, mapped_inputs: dict | None = None) -> str:
    task = (config.get("prompt") or node.get("label") or "完成当前节点任务").strip()
    upstream = {key: _upstream_output_preview(value) for key, value in outputs.items()}
    payload = {"input": safe_input, "upstream_outputs": upstream} if mapped_inputs is None else {"mapped_inputs": mapped_inputs}
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    schema = _output_schema(config)
    if schema:
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        return f"节点任务：{task}\n以下是本节点被明确授权读取的工作流上下文，请基于它们完成任务。\n{serialized}\n本节点有严格的结构化输出约定：{schema_text}\n请只返回一个 JSON 对象，不要使用 Markdown 代码块或补充说明；字段类型和 required 约束必须严格遵守。"[:_MAX_AGENT_CONTEXT]
    return f"节点任务：{task}\n以下是本节点被明确授权读取的工作流上下文，请基于它们完成任务。\n{serialized}\n请直接返回本节点的最终结果。"[:_MAX_AGENT_CONTEXT]


def _simulated_node_output(node: dict) -> str:
    """Expose an explicit bounded marker when a non-model node has no runtime yet."""

    return json.dumps({"status": "simulated", "node_id": node["id"], "node_type": node["type"]}, ensure_ascii=False, separators=(",", ":"))


def _skipped_node_output(node: dict, reason: str = "condition_branch") -> str:
    return json.dumps({"status": "skipped", "node_id": node["id"], "node_type": node["type"], "reason": reason}, ensure_ascii=False, separators=(",", ":"))


def _condition_value(config: dict, safe_input: dict, outputs: dict[str, str] | None = None, structured_outputs: dict[str, dict] | None = None):
    value_source = config.get("value_source")
    if isinstance(value_source, str) and value_source:
        return value_source, _resolve_workflow_reference(value_source, safe_input, outputs, structured_outputs)
    field = config.get("field")
    if not field and "amount_limit" in config:
        field = "amount"
    return field, safe_input.get(field) if isinstance(field, str) else None


def _condition_result(node: dict, safe_input: dict, outputs: dict[str, str] | None = None, structured_outputs: dict[str, dict] | None = None) -> dict:
    """Evaluate a deterministic, input-only branch without invoking a model."""

    config = node.get("config") or {}
    required_fields = config.get("required_fields")
    if isinstance(required_fields, list) and required_fields:
        aliases = {"金额": "amount", "事由": "reason", "日期": "date"}
        missing = []
        for field in required_fields:
            key = aliases.get(field, field)
            if key not in safe_input or safe_input[key] in (None, "", []):
                missing.append(field)
        return {"kind": "condition", "result": not missing, "operator": "required", "missing": missing, "reason": "required_fields"}

    field, actual = _condition_value(config, safe_input, outputs, structured_outputs)
    expected = config.get("value", config.get("amount_limit"))
    operator = config.get("operator") or ("lte" if "amount_limit" in config else "exists")
    result = False
    if operator == "exists":
        result = actual not in (None, "", [])
    elif operator == "eq":
        result = actual == expected
    elif operator == "neq":
        result = actual != expected
    elif operator in {"gt", "gte", "lt", "lte"}:
        try:
            if isinstance(actual, bool) or isinstance(expected, bool):
                result = False
            elif operator == "gt":
                result = actual > expected
            elif operator == "gte":
                result = actual >= expected
            elif operator == "lt":
                result = actual < expected
            else:
                result = actual <= expected
        except TypeError:
            result = False
    elif operator == "contains":
        try:
            result = expected in actual
        except TypeError:
            result = False
    elif operator == "in":
        result = isinstance(expected, list) and actual in expected
    return {"kind": "condition", "result": bool(result), "operator": operator, "field": field, "expected": expected, "actual": actual, "reason": "comparison"}


def _condition_edge_matches(label: str, result: bool, config: dict) -> bool:
    value = label.strip()
    if config.get("true_label") and value == config["true_label"]:
        return result
    if config.get("false_label") and value == config["false_label"]:
        return not result
    normalized = value.casefold()
    true_labels = {"true", "yes", "y", "pass", "passed", "approved", "ok", "1", "是", "通过", "满足", "符合"}
    false_labels = {"false", "no", "n", "fail", "failed", "rejected", "deny", "0", "否", "不通过", "不满足", "不符合"}
    return normalized in (true_labels if result else false_labels)


def _condition_selected_edges(node: dict, outgoing: list[dict], result: bool) -> set[tuple[str, str]]:
    if len(outgoing) <= 1 or not any(str(edge.get("label") or "").strip() for edge in outgoing):
        return {(edge["source"], edge["target"]) for edge in outgoing}
    selected = {(edge["source"], edge["target"]) for edge in outgoing if _condition_edge_matches(str(edge.get("label") or ""), result, node.get("config") or {})}
    if not selected:
        fallback = {(edge["source"], edge["target"]) for edge in outgoing if not str(edge.get("label") or "").strip()}
        selected = fallback
    if not selected:
        raise AppError(422, "WORKFLOW_CONDITION_BRANCH_INVALID", "条件分支没有与结果匹配的出口标签")
    return selected


def _graph_edges(graph: dict) -> tuple[dict[str, list[dict]], dict[str, list[dict]], set[tuple[str, str]]]:
    by_source: dict[str, list[dict]] = defaultdict(list)
    by_target: dict[str, list[dict]] = defaultdict(list)
    all_edges = set()
    for edge in graph.get("edges", []):
        by_source[edge["source"]].append(edge)
        by_target[edge["target"]].append(edge)
        all_edges.add((edge["source"], edge["target"]))
    return by_source, by_target, all_edges


def _node_reachable(node_id: str, incoming: dict[str, list[dict]], active_edges: set[tuple[str, str]]) -> bool:
    edges = incoming.get(node_id, [])
    return not edges or any((edge["source"], edge["target"]) in active_edges for edge in edges)


async def _invoke_agent_model(service, selection: dict, system_prompt: str, user_prompt: str, config: dict) -> str:
    provider = service.validated_provider(selection)
    token = provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        raise AppError(503, "WORKFLOW_MODEL_UNAVAILABLE", "当前工作流节点的模型线路尚未配置授权")
    base = provider.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    endpoint = base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if provider.get("ANTHROPIC_API_KEY"):
        headers["x-api-key"] = provider["ANTHROPIC_API_KEY"]
    else:
        headers["authorization"] = f"Bearer {token}"
    max_tokens = config.get("max_tokens", 512)
    temperature = config.get("temperature", 0.2)
    payload = {
        "model": selection["upstream_model"],
        "system": system_prompt,
        "max_tokens": max(32, min(4096, int(max_tokens))),
        "temperature": max(0, min(2, float(temperature))),
        "messages": [{"role": "user", "content": user_prompt}],
    }
    timeout = max(10.0, min(180.0, float(getattr(service.settings, "workflow_model_timeout_seconds", 90))))
    try:
        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)), follow_redirects=False, trust_env=False) as http:
            response = await http.post(endpoint, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as error:
        raise AppError(502, "WORKFLOW_MODEL_FAILED", "工作流智能体调用模型失败，请检查模型线路或稍后重试") from error
    except (httpx.HTTPError, ValueError, TypeError) as error:
        raise AppError(502, "WORKFLOW_MODEL_FAILED", "工作流智能体暂时无法连接模型，请稍后重试") from error
    text = _completion_text(body).strip()
    if not text:
        raise AppError(502, "WORKFLOW_MODEL_EMPTY", "工作流智能体未返回可用结果")
    return text[:_MAX_AGENT_OUTPUT]


async def _invoke_agent_model_with_retry(service, selection: dict, system_prompt: str, user_prompt: str, config: dict, retry_trace: list[dict] | None = None) -> tuple[str, int]:
    """Retry only transient provider failures and record bounded attempt metadata."""

    max_retries = int(config.get("max_retries", 0) or 0)
    backoff_ms = int(config.get("retry_backoff_ms", 250) or 0)
    attempts = 0
    events = retry_trace if retry_trace is not None else []
    while True:
        attempts += 1
        attempt_started_at = perf_counter()
        try:
            text = await _invoke_agent_model(service, selection, system_prompt, user_prompt, config)
        except AppError as error:
            event = {"attempt": attempts, "status": "failed", "error_code": error.code, "duration_ms": _model_duration_ms(attempt_started_at)}
            if error.code not in {"WORKFLOW_MODEL_FAILED", "WORKFLOW_MODEL_EMPTY"} or attempts > max_retries:
                events.append(event)
                raise
            delay_ms = min(5_000, backoff_ms * (2 ** (attempts - 1)))
            event["retry_backoff_ms"] = delay_ms
            events.append(event)
            delay = delay_ms / 1_000
            if delay:
                await asyncio.sleep(delay)
            continue
        events.append({"attempt": attempts, "status": "succeeded", "duration_ms": _model_duration_ms(attempt_started_at)})
        return text, attempts


def _model_duration_ms(started_at: float) -> int:
    return max(0, int(round((perf_counter() - started_at) * 1000)))


def _ensure_workflow_runtime_schema(service) -> None:
    with service.db.lock:
        service.db.conn.executescript(_WORKFLOW_RUNTIME_SCHEMA)
        columns = {row["name"] for row in service.db.conn.execute("PRAGMA table_info(workflow_approvals)")}
        if "decided_by" not in columns:
            service.db.conn.execute("ALTER TABLE workflow_approvals ADD COLUMN decided_by TEXT REFERENCES users(id)")


def _insert_workflow_run(service, *, run_id: str, workflow_id: str, conversation_id: str | None, version: int, graph_hash: str, permission_digest: str, permission_snapshot: dict, user_id: str, status: str, safe_input: dict, output: dict, trace: list[dict], timestamp: str, error: str | None = None, completed: bool = True) -> None:
    service.db.execute(
        "INSERT INTO workflow_runs(id,workflow_id,conversation_id,version,graph_hash,permission_digest,permission_snapshot_json,user_id,status,input_json,output_json,trace_json,error,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, workflow_id, conversation_id, version, graph_hash, permission_digest, dump(permission_snapshot), user_id, status, dump(safe_input), dump(output), dump(trace), error, timestamp, now() if completed else None),
    )
    _persist_workflow_tool_evidence(service, run_id, output)


def _update_workflow_run(service, run_id: str, *, status: str, output: dict, trace: list[dict], error: str | None = None, completed: bool = True) -> None:
    service.db.execute(
        "UPDATE workflow_runs SET status=?,output_json=?,trace_json=?,error=?,completed_at=? WHERE id=?",
        (status, dump(output), dump(trace), error, now() if completed else None, run_id),
    )
    _persist_workflow_tool_evidence(service, run_id, output)


def _persist_workflow_tool_evidence(service, run_id: str, output: dict) -> None:
    if not service.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_tool_evidence'"):
        return
    for node_id, payload in (output.get("tool_outputs") or {}).items():
        if not isinstance(payload, dict):
            continue
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
        body = {"node_id": node_id, "tool_name": payload.get("tool_name"), "connection_id": payload.get("connection_id"), "evidence": evidence[:30]}
        serialized = dump(body)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        service.db.execute("INSERT OR IGNORE INTO workflow_tool_evidence(id,workflow_run_id,node_id,connection_id,tool_name,payload_json,digest,created_at) VALUES(?,?,?,?,?,?,?,?)", (uid(), run_id, node_id, str(payload.get("connection_id") or ""), str(payload.get("tool_name") or "")[:220], serialized, digest, now()))


def _workflow_approval_view(row: dict) -> dict:
    return {key: row.get(key) for key in ("id", "workflow_run_id", "workflow_id", "node_id", "status", "decision", "comment", "created_at", "decided_at", "decided_by")}


def _workflow_approvals(service, run_id: str) -> list[dict]:
    _ensure_workflow_runtime_schema(service)
    return [_workflow_approval_view(item) for item in service.db.all("SELECT * FROM workflow_approvals WHERE workflow_run_id=? ORDER BY created_at", (run_id,))]


async def debug_node(service, workflow_id: str, node_id: str, body: WorkflowNodeDebugRequest, user):
    """Execute one draft agent node and persist the bounded debug trace."""

    row = _editable(service, workflow_id, user)
    if len(json.dumps(body.input, ensure_ascii=False).encode("utf-8")) > _MAX_JSON:
        raise AppError(422, "WORKFLOW_INPUT_TOO_LARGE", "工作流输入超过大小限制")
    list(_walk_values(body.input))
    version = service.db.one("SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?", (workflow_id, row["draft_version"]))
    if not version:
        raise AppError(409, "WORKFLOW_DRAFT_MISSING", "没有可调试的工作流草稿")
    graph, graph_hash, _ = validate_graph(json.loads(version["graph"]))
    if graph_hash != version["graph_hash"]:
        raise AppError(409, "WORKFLOW_VERSION_CORRUPT", "工作流版本校验失败")
    node = next((item for item in graph["nodes"] if item["id"] == node_id), None)
    if not node:
        raise AppError(404, "WORKFLOW_NODE_NOT_FOUND", "工作流节点不存在")
    config = node.get("config") or {}
    if node["type"] != "agent" or config.get("mode", "synthetic") != "model":
        raise AppError(409, "WORKFLOW_NODE_NOT_MODEL", "只有模型执行的智能体节点可以调试")

    assistant = service.usable_assistant(row["assistant_id"], user)
    selection = _agent_selection(service, assistant, config)
    safe_input = _validated_input(graph, body.input)
    public_model = {key: selection[key] for key in ("model_id", "model_label", "reasoning_level")}
    permission_payload = {
        "assistant_id": assistant["id"],
        "assistant_revision": assistant["revision"],
        "workflow_revision": row["revision"],
        "workflow_version": version["version"],
        "debug_node_id": node_id,
        "user_id": user["id"],
        "models": [public_model],
    }
    permission_digest = hashlib.sha256(json.dumps(permission_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    permission_snapshot = {
        "schema_version": 2,
        "mode": "model",
        "debug": True,
        "debug_node_id": node_id,
        "assistant_id": assistant["id"],
        "assistant_revision": assistant["revision"],
        "assistant_scope": assistant.get("scope", "private"),
        "workflow_id": workflow_id,
        "workflow_revision": row["revision"],
        "workflow_version": version["version"],
        "user_id": user["id"],
        "user_role": user.get("role"),
        "models": [{"node_id": node_id, **public_model}],
    }
    run_id, timestamp = uid(), now()
    role, system_prompt = _agent_system_prompt(assistant, config)
    trace: list[dict] = []
    model_duration_ms: int | None = None
    model_attempts = 0
    model_retry_trace: list[dict] = []
    try:
        mapped_inputs = _resolve_input_bindings(config, safe_input, {})
        model_started_at = perf_counter()
        try:
            text, model_attempts = await _invoke_agent_model_with_retry(service, selection, system_prompt, _agent_user_prompt(node, safe_input, {}, config, mapped_inputs), config, model_retry_trace)
        finally:
            model_duration_ms = _model_duration_ms(model_started_at)
        structured = _parse_structured_agent_output(text, _output_schema(config))
        trace.append({
            "sequence": 1,
            "node_id": node_id,
            "node_type": "agent",
            "status": "completed",
            "external_call": True,
            "approval_required": False,
            "model_id": selection["model_id"],
            "model_label": selection["model_label"],
            "reasoning_level": selection["reasoning_level"],
            "role": role,
            "model_attempts": model_attempts,
            "model_retries": max(0, model_attempts - 1),
            "model_retry_trace": model_retry_trace,
            "duration_ms": model_duration_ms,
            "input_bindings": _binding_trace(config),
            **({"structured_output": True, "structured_fields": sorted(structured)} if structured is not None else {}),
            "output_preview": text[:600],
        })
        output = {
            "debug": True,
            "synthetic": False,
            "execution_mode": "model",
            "workflow_id": workflow_id,
            "version": version["version"],
            "graph_hash": graph_hash,
            "completed_nodes": 1,
            "node_id": node_id,
            "input": safe_input,
            "node_output": text,
            "agent_outputs": {node_id: text},
            "agent_structured_outputs": {node_id: structured} if structured is not None else {},
            "model_attempts": model_attempts,
            "permission_snapshot": permission_snapshot,
        }
        with service.db.transaction():
            _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="succeeded", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp)
        return {"id": run_id, "workflow_id": workflow_id, "conversation_id": None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "succeeded", "synthetic": False, "execution_mode": "model", "output": output, "trace": trace, "created_at": timestamp, "completed_at": now()}
    except AppError as error:
        model_attempts = max(model_attempts, len(model_retry_trace))
        trace.append({
            "sequence": 1,
            "node_id": node_id,
            "node_type": "agent",
            "status": "failed",
            "external_call": True,
            "approval_required": False,
            "model_id": selection["model_id"],
            "model_label": selection["model_label"],
            "reasoning_level": selection["reasoning_level"],
            "role": role,
            **({"model_attempts": model_attempts, "model_retries": max(0, model_attempts - 1), "model_retry_trace": model_retry_trace} if model_attempts else {}),
            **({"duration_ms": model_duration_ms} if model_duration_ms is not None else {}),
            "input_bindings": _binding_trace(config),
            "error_code": error.code,
        })
        output = {
            "debug": True,
            "synthetic": False,
            "execution_mode": "model",
            "workflow_id": workflow_id,
            "version": version["version"],
            "graph_hash": graph_hash,
            "completed_nodes": 0,
            "node_id": node_id,
            "input": safe_input,
            "agent_outputs": {},
            "agent_structured_outputs": {},
            "permission_snapshot": permission_snapshot,
        }
        with service.db.transaction():
            _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="failed", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp, error=error.message)
        raise


async def run_model(service, workflow_id, body: WorkflowRunRequest, user, resume_run: dict | None = None, approved_node_id: str | None = None):
    row = _readable(service, workflow_id, user)
    conversation = _bind_conversation(service, row, body.conversation_id, user)
    if row["published_version"] is None:
        raise AppError(409, "WORKFLOW_NOT_PUBLISHED", "工作流尚未发布，不能运行")
    if len(json.dumps(body.input, ensure_ascii=False).encode("utf-8")) > _MAX_JSON:
        raise AppError(422, "WORKFLOW_INPUT_TOO_LARGE", "工作流输入超过大小限制")
    list(_walk_values(body.input))
    version = service.db.one("SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?", (workflow_id, row["published_version"]))
    graph, graph_hash, order = validate_graph(json.loads(version["graph"]))
    if graph_hash != version["graph_hash"]:
        raise AppError(409, "WORKFLOW_VERSION_CORRUPT", "工作流版本校验失败")
    assistant = service.usable_assistant(row["assistant_id"], user)
    _validate_model_nodes(service, assistant, graph)
    safe_input = _validated_input(graph, body.input)
    node_map = {node["id"]: node for node in graph["nodes"]}
    artifact_previews = _artifact_previews(graph, safe_input)
    artifact_index = {preview["node_id"]: index for index, preview in enumerate(artifact_previews)}
    model_nodes = [node for node in graph["nodes"] if node["type"] == "agent" and (node.get("config") or {}).get("mode", "synthetic") == "model"]
    selections = {node["id"]: _agent_selection(service, assistant, node.get("config") or {}) for node in model_nodes}
    knowledge_nodes = [node for node in graph["nodes"] if node["type"] == "knowledge_search"]
    knowledge_scope = _assistant_knowledge_scope(service, assistant, user) if knowledge_nodes else []
    tool_nodes = [node for node in graph["nodes"] if node["type"] == "mcp_read"]
    tool_scope = _workflow_tool_scope(service, assistant, user, conversation, tool_nodes)
    if tool_scope:
        _ensure_workflow_runtime_schema(service)
    public_models = [{key: selection[key] for key in ("model_id", "model_label", "reasoning_level")} for selection in selections.values()]
    public_tools = [{key: item[key] for key in ("node_id", "tool_name", "connection_id", "connection_revision")} for item in tool_scope.values()]
    permission_payload = {"assistant_id": assistant["id"], "assistant_revision": assistant["revision"], "workflow_revision": row["revision"], "conversation_id": conversation["id"] if conversation else None, "user_id": user["id"], "models": public_models, "knowledge_bases": knowledge_scope, "tools": public_tools, "mode": "model"}
    permission_digest = hashlib.sha256(json.dumps(permission_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    permission_snapshot = {
        "schema_version": 2,
        "mode": "model",
        "assistant_id": assistant["id"],
        "assistant_revision": assistant["revision"],
        "assistant_scope": assistant.get("scope", "private"),
        "workflow_id": workflow_id,
        "workflow_revision": row["revision"],
        "workflow_version": version["version"],
        "conversation_id": conversation["id"] if conversation else None,
        "user_id": user["id"],
        "user_role": user.get("role"),
        "models": [{"node_id": node["id"], **{key: selections[node["id"]][key] for key in ("model_id", "model_label", "reasoning_level")}} for node in model_nodes],
        "knowledge_bases": knowledge_scope,
        "knowledge_evidence": "source_bound_workflow_retrieval",
        "tools": public_tools,
        "tool_mode": "live" if tool_scope else "none",
        "tool_evidence": "workflow_bound_structured_results",
    }
    trace: list[dict] = []
    outputs: dict[str, str] = {}
    agent_structured_outputs: dict[str, dict] = {}
    knowledge_outputs: dict[str, dict] = {}
    condition_outputs: dict[str, dict] = {}
    tool_outputs: dict[str, dict] = {}
    tool_evidence: dict[str, list[dict]] = {}
    outgoing, incoming, all_edges = _graph_edges(graph)
    active_edges = set(all_edges)
    start_index = 0
    run_id, timestamp = uid(), now()
    if resume_run:
        if resume_run.get("workflow_id") != workflow_id or resume_run.get("status") != "awaiting_approval":
            raise AppError(409, "WORKFLOW_RUN_NOT_RESUMABLE", "此工作流运行当前不能继续")
        if resume_run.get("graph_hash") != graph_hash or resume_run.get("version") != version["version"]:
            raise AppError(409, "WORKFLOW_VERSION_CHANGED", "工作流版本已改变，不能继续旧运行")
        if resume_run.get("permission_digest") != permission_digest:
            raise AppError(409, "WORKFLOW_PERMISSION_CHANGED", "工作流权限或模型配置已改变，请重新运行")
        try:
            prior_output = json.loads(resume_run.get("output_json") or "{}")
            trace = json.loads(resume_run.get("trace_json") or "[]")
            outputs = {str(key): str(value) for key, value in (prior_output.get("node_outputs") or {}).items()}
            outputs.update({str(key): str(value) for key, value in (prior_output.get("agent_outputs") or {}).items()})
            outputs.update({str(key): str(value) for key, value in (prior_output.get("knowledge_outputs") or {}).items() if isinstance(value, str)})
            outputs.update({str(key): json.dumps(value, ensure_ascii=False, separators=(",", ":")) for key, value in (prior_output.get("condition_outputs") or {}).items()})
            agent_structured_outputs = {str(key): value for key, value in (prior_output.get("agent_structured_outputs") or {}).items() if isinstance(value, dict)}
            knowledge_outputs = prior_output.get("knowledge_outputs") or {}
            condition_outputs = prior_output.get("condition_outputs") or {}
            tool_outputs = prior_output.get("tool_outputs") or {}
            tool_evidence = prior_output.get("tool_evidence") or {}
            start_index = int(prior_output.get("next_node_index", 0))
            active_edges = {tuple(edge) for edge in (prior_output.get("active_edges") or [list(edge) for edge in all_edges]) if isinstance(edge, list) and len(edge) == 2}
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AppError(409, "WORKFLOW_RUN_CORRUPT", "待审批工作流运行记录无法读取") from error
        permission_snapshot = json.loads(resume_run.get("permission_snapshot_json") or "{}")
        run_id, timestamp = resume_run["id"], resume_run["created_at"]
        if approved_node_id:
            approved_preview = json.dumps({"kind": "workflow_approval", "status": "approved", "node_id": approved_node_id}, ensure_ascii=False, separators=(",", ":"))
            for item in trace:
                if item.get("node_id") == approved_node_id and item.get("status") == "awaiting_approval":
                    item["status"] = "approved"
                    item["approval_decision"] = "approve"
                    item["decided_at"] = now()
                    item["output_preview"] = approved_preview
            if approved_node_id in outputs:
                outputs[approved_node_id] = approved_preview

    def run_output(next_node_index: int | None = None) -> dict:
        return {"synthetic": False, "execution_mode": "model", "workflow_id": workflow_id, "version": version["version"], "graph_hash": graph_hash, "completed_nodes": len(trace), "input": safe_input, "node_outputs": outputs, "agent_outputs": {key: value for key, value in outputs.items() if node_map.get(key, {}).get("type") == "agent"}, "agent_structured_outputs": agent_structured_outputs, "knowledge_outputs": knowledge_outputs, "condition_outputs": condition_outputs, "tool_outputs": tool_outputs, "tool_evidence": tool_evidence, "artifacts": artifact_previews, "permission_snapshot": permission_snapshot, "active_edges": [list(edge) for edge in sorted(active_edges)], **({"next_node_index": next_node_index} if next_node_index is not None else {})}

    current_node = None
    model_duration_ms: int | None = None
    try:
        for index, node_id in enumerate(order, 1):
            if resume_run and index <= start_index:
                continue
            node = node_map[node_id]
            config = node.get("config") or {}
            model_duration_ms = None
            model_attempts = 0
            model_retry_trace: list[dict] = []
            if not _node_reachable(node_id, incoming, active_edges):
                outputs[node_id] = _skipped_node_output(node)
                for edge in outgoing.get(node_id, []):
                    active_edges.discard((edge["source"], edge["target"]))
                trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "skipped", "external_call": False, "approval_required": False, "branch_skipped": True, "output_preview": outputs[node_id]})
                continue
            if node["type"] == "approval":
                current_node = node
                outputs[node_id] = json.dumps({"kind": "workflow_approval", "status": "pending", "node_id": node_id}, ensure_ascii=False, separators=(",", ":"))
                trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "awaiting_approval", "external_call": False, "approval_required": True, "approval_role": config.get("role"), "output_preview": outputs[node_id]})
                approval_id = uid()
                output = run_output(index)
                _ensure_workflow_runtime_schema(service)
                with service.db.transaction():
                    if resume_run:
                        _update_workflow_run(service, run_id, status="awaiting_approval", output=output, trace=trace, completed=False)
                    else:
                        _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=conversation["id"] if conversation else None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="awaiting_approval", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp, completed=False)
                    service.db.execute("INSERT INTO workflow_approvals(id,workflow_run_id,workflow_id,node_id,user_id,status,decision,comment,created_at,decided_at,decided_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (approval_id, run_id, workflow_id, node_id, user["id"], "pending", None, "", timestamp, None, None))
                current_node = None
                approval = {"id": approval_id, "workflow_run_id": run_id, "workflow_id": workflow_id, "node_id": node_id, "status": "pending", "decision": None, "comment": "", "created_at": timestamp, "decided_at": None}
                return {"id": run_id, "workflow_id": workflow_id, "conversation_id": conversation["id"] if conversation else None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "awaiting_approval", "synthetic": False, "execution_mode": "model", "approval": approval, "approvals": [approval], "output": output, "trace": trace, "created_at": timestamp, "completed_at": None}
            if node_id not in selections:
                if node["type"] == "condition":
                    current_node = node
                    result = _condition_result(node, safe_input, outputs, agent_structured_outputs)
                    selected_edges = _condition_selected_edges(node, outgoing.get(node_id, []), bool(result["result"]))
                    outputs[node_id] = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                    condition_outputs[node_id] = result
                    for edge in outgoing.get(node_id, []):
                        if (edge["source"], edge["target"]) not in selected_edges:
                            active_edges.discard((edge["source"], edge["target"]))
                    trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "completed", "external_call": False, "approval_required": False, "condition_result": bool(result["result"]), "condition_operator": result.get("operator"), "selected_targets": [edge["target"] for edge in outgoing.get(node_id, []) if (edge["source"], edge["target"]) in selected_edges], "output_preview": outputs[node_id][:600]})
                    current_node = None
                    continue
                if node["type"] == "knowledge_search":
                    current_node = node
                    payload, serialized = await _run_knowledge_search(service, assistant, user, node, safe_input, outputs, knowledge_scope, agent_structured_outputs)
                    outputs[node_id] = serialized
                    knowledge_outputs[node_id] = payload
                    trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": payload["status"], "external_call": False, "approval_required": False, "knowledge_retrieval": True, "query": payload["query"], "source_count": payload["source_count"], "retrieval_mode": payload["retrieval"].get("mode"), "output_preview": json.dumps({"query": payload["query"], "source_count": payload["source_count"]}, ensure_ascii=False, separators=(",", ":"))})
                    current_node = None
                    continue
                if node["type"] == "mcp_read" and config.get("mode", "synthetic") == "live":
                    current_node = node
                    payload, serialized, sources = await _run_mcp_read(service, user, conversation, node, tool_scope, safe_input, outputs, agent_structured_outputs)
                    outputs[node_id] = serialized
                    tool_outputs[node_id] = payload
                    tool_evidence[node_id] = sources
                    trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "completed", "external_call": True, "approval_required": False, "tool_call": True, "tool_name": payload["tool_name"], "connection_id": payload["connection_id"], "evidence_count": len(sources), "output_preview": serialized[:600]})
                    current_node = None
                    continue
                outputs[node_id] = _simulated_node_output(node)
                trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "simulated", "external_call": False, "approval_required": node["type"] == "approval", "output_preview": outputs[node_id], **({"artifact_preview": artifact_index[node_id]} if node_id in artifact_index else {})})
                continue
            current_node = node
            role, system_prompt = _agent_system_prompt(assistant, config)
            mapped_inputs = _resolve_input_bindings(config, safe_input, outputs, agent_structured_outputs)
            model_started_at = perf_counter()
            try:
                text, model_attempts = await _invoke_agent_model_with_retry(service, selections[node_id], system_prompt, _agent_user_prompt(node, safe_input, outputs, config, mapped_inputs), config, model_retry_trace)
            finally:
                model_duration_ms = _model_duration_ms(model_started_at)
            structured = _parse_structured_agent_output(text, _output_schema(config))
            outputs[node_id] = text
            if structured is not None:
                agent_structured_outputs[node_id] = structured
            trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "completed", "external_call": True, "approval_required": False, "model_id": selections[node_id]["model_id"], "model_label": selections[node_id]["model_label"], "reasoning_level": selections[node_id]["reasoning_level"], "role": role, "model_attempts": model_attempts, "model_retries": max(0, model_attempts - 1), "model_retry_trace": model_retry_trace, "duration_ms": model_duration_ms, "input_bindings": _binding_trace(config), **({"structured_output": True, "structured_fields": sorted(structured)} if structured is not None else {}), "output_preview": text[:600]})
            current_node = None
        output = run_output()
        with service.db.transaction():
            if resume_run:
                _update_workflow_run(service, run_id, status="succeeded", output=output, trace=trace, completed=True)
            else:
                _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=conversation["id"] if conversation else None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="succeeded", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp)
        return {"id": run_id, "workflow_id": workflow_id, "conversation_id": conversation["id"] if conversation else None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "succeeded", "synthetic": False, "execution_mode": "model", "approvals": _workflow_approvals(service, run_id), "output": output, "trace": trace, "created_at": timestamp, "completed_at": now()}
    except AppError as error:
        if current_node:
            if current_node["id"] in selections:
                model_attempts = max(model_attempts, len(model_retry_trace))
                trace.append({"sequence": len(trace) + 1, "node_id": current_node["id"], "node_type": current_node["type"], "status": "failed", "external_call": True, "approval_required": False, "model_id": selections[current_node["id"]]["model_id"], **({"model_label": selections[current_node["id"]]["model_label"], "reasoning_level": selections[current_node["id"]]["reasoning_level"], "duration_ms": model_duration_ms} if model_duration_ms is not None else {}), **({"model_attempts": model_attempts, "model_retries": max(0, model_attempts - 1), "model_retry_trace": model_retry_trace} if model_attempts else {}), "input_bindings": _binding_trace(current_node.get("config") or {}), "error_code": error.code})
            else:
                trace.append({"sequence": len(trace) + 1, "node_id": current_node["id"], "node_type": current_node["type"], "status": "failed", "external_call": current_node["type"] == "mcp_read", "approval_required": False, "knowledge_retrieval": current_node["type"] == "knowledge_search", "tool_call": current_node["type"] == "mcp_read", "tool_name": (current_node.get("config") or {}).get("tool_name") if current_node["type"] == "mcp_read" else None, "error_code": error.code})
        output = run_output()
        with service.db.transaction():
            if resume_run:
                _update_workflow_run(service, run_id, status="failed", output=output, trace=trace, error=error.message, completed=True)
            else:
                _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=conversation["id"] if conversation else None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="failed", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp, error=error.message)
        raise


async def run_workflow(service, workflow_id, body: WorkflowRunRequest, user):
    row = _readable(service, workflow_id, user)
    if row["published_version"] is None:
        raise AppError(409, "WORKFLOW_NOT_PUBLISHED", "工作流尚未发布，不能运行")
    version = service.db.one("SELECT graph FROM workflow_versions WHERE workflow_id=? AND version=?", (workflow_id, row["published_version"]))
    graph = json.loads(version["graph"])
    if any(node.get("type") == "agent" and (node.get("config") or {}).get("mode", "synthetic") == "model" for node in graph.get("nodes", [])):
        return await run_model(service, workflow_id, body, user)
    return await run_synthetic(service, workflow_id, body, user)


async def run_synthetic(service, workflow_id, body: WorkflowRunRequest, user, resume_run: dict | None = None, approved_node_id: str | None = None):
    row = _readable(service, workflow_id, user)
    conversation = _bind_conversation(service, row, body.conversation_id, user)
    if row["published_version"] is None:
        raise AppError(409, "WORKFLOW_NOT_PUBLISHED", "工作流尚未发布，不能运行")
    if len(json.dumps(body.input, ensure_ascii=False).encode("utf-8")) > _MAX_JSON:
        raise AppError(422, "WORKFLOW_INPUT_TOO_LARGE", "工作流输入超过大小限制")
    list(_walk_values(body.input))
    version = service.db.one("SELECT * FROM workflow_versions WHERE workflow_id=? AND version=?", (workflow_id, row["published_version"]))
    graph, graph_hash, order = validate_graph(json.loads(version["graph"]))
    if graph_hash != version["graph_hash"]:
        raise AppError(409, "WORKFLOW_VERSION_CORRUPT", "工作流版本校验失败")
    safe_input = _validated_input(graph, body.input)
    node_map = {node["id"]: node for node in graph["nodes"]}
    artifact_previews = _artifact_previews(graph, safe_input)
    artifact_index = {preview["node_id"]: index for index, preview in enumerate(artifact_previews)}
    assistant = service.require("assistants", row["assistant_id"])
    tool_nodes = [node for node in graph["nodes"] if node["type"] == "mcp_read"]
    tool_scope = _workflow_tool_scope(service, assistant, user, conversation, tool_nodes)
    if tool_scope:
        _ensure_workflow_runtime_schema(service)
    public_tools = [{key: item[key] for key in ("node_id", "tool_name", "connection_id", "connection_revision")} for item in tool_scope.values()]
    permission_payload = {
        "assistant_id": assistant["id"],
        "assistant_revision": assistant["revision"],
        "workflow_revision": row["revision"],
        "conversation_id": conversation["id"] if conversation else None,
        "user_id": user["id"],
        "tools": public_tools,
        "mode": "tool" if tool_scope else "synthetic",
    }
    permission_digest = hashlib.sha256(json.dumps(permission_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    permission_snapshot = {
        "schema_version": 1,
        "mode": "tool" if tool_scope else "synthetic",
        "assistant_id": assistant["id"],
        "assistant_revision": assistant["revision"],
        "assistant_scope": assistant.get("scope", "private"),
        "workflow_id": workflow_id,
        "workflow_revision": row["revision"],
        "workflow_version": version["version"],
        "conversation_id": conversation["id"] if conversation else None,
        "user_id": user["id"],
        "user_role": user.get("role"),
        "tools": public_tools,
        "tool_evidence": "workflow_bound_structured_results",
    }
    trace: list[dict] = []
    outputs: dict[str, str] = {}
    condition_outputs: dict[str, dict] = {}
    tool_outputs: dict[str, dict] = {}
    tool_evidence: dict[str, list[dict]] = {}
    outgoing, incoming, all_edges = _graph_edges(graph)
    active_edges = set(all_edges)
    start_index = 0
    run_id, timestamp = uid(), now()
    if resume_run:
        if resume_run.get("workflow_id") != workflow_id or resume_run.get("status") != "awaiting_approval":
            raise AppError(409, "WORKFLOW_RUN_NOT_RESUMABLE", "此工作流运行当前不能继续")
        if resume_run.get("graph_hash") != graph_hash or resume_run.get("version") != version["version"]:
            raise AppError(409, "WORKFLOW_VERSION_CHANGED", "工作流版本已改变，不能继续旧运行")
        if resume_run.get("permission_digest") != permission_digest:
            raise AppError(409, "WORKFLOW_PERMISSION_CHANGED", "工作流权限配置已改变，请重新运行")
        try:
            prior_output = json.loads(resume_run.get("output_json") or "{}")
            trace = json.loads(resume_run.get("trace_json") or "[]")
            outputs = {str(key): str(value) for key, value in (prior_output.get("node_outputs") or {}).items()}
            condition_outputs = prior_output.get("condition_outputs") or {}
            tool_outputs = prior_output.get("tool_outputs") or {}
            tool_evidence = prior_output.get("tool_evidence") or {}
            start_index = int(prior_output.get("next_node_index", 0))
            active_edges = {tuple(edge) for edge in (prior_output.get("active_edges") or [list(edge) for edge in all_edges]) if isinstance(edge, list) and len(edge) == 2}
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise AppError(409, "WORKFLOW_RUN_CORRUPT", "待审批工作流运行记录无法读取") from error
        permission_snapshot = json.loads(resume_run.get("permission_snapshot_json") or "{}")
        run_id, timestamp = resume_run["id"], resume_run["created_at"]
        if approved_node_id:
            approved_preview = json.dumps({"kind": "workflow_approval", "status": "approved", "node_id": approved_node_id}, ensure_ascii=False, separators=(",", ":"))
            for item in trace:
                if item.get("node_id") == approved_node_id and item.get("status") == "awaiting_approval":
                    item["status"] = "approved"
                    item["approval_decision"] = "approve"
                    item["decided_at"] = now()
                    item["output_preview"] = approved_preview
            if approved_node_id in outputs:
                outputs[approved_node_id] = approved_preview

    def run_output(next_node_index: int | None = None) -> dict:
        return {"synthetic": not bool(tool_scope), "execution_mode": "tool" if tool_scope else "synthetic", "workflow_id": workflow_id, "version": version["version"], "graph_hash": graph_hash, "completed_nodes": len(trace), "input": safe_input, "node_outputs": outputs, "condition_outputs": condition_outputs, "tool_outputs": tool_outputs, "tool_evidence": tool_evidence, "artifacts": artifact_previews, "permission_snapshot": permission_snapshot, "active_edges": [list(edge) for edge in sorted(active_edges)], **({"next_node_index": next_node_index} if next_node_index is not None else {})}

    for index, node_id in enumerate(order, 1):
        if resume_run and index <= start_index:
            continue
        node = node_map[node_id]
        if not _node_reachable(node_id, incoming, active_edges):
            outputs[node_id] = _skipped_node_output(node)
            for edge in outgoing.get(node_id, []):
                active_edges.discard((edge["source"], edge["target"]))
            trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "skipped", "external_call": False, "approval_required": False, "branch_skipped": True, "output_preview": outputs[node_id]})
            continue
        if node["type"] == "condition":
            # Conditions in a synthetic or live-tool workflow can still read
            # the serialized output of an already completed upstream node.
            # Keep this path consistent with model workflows; otherwise a
            # valid `node.<id>.output` reference silently evaluates to null.
            result = _condition_result(node, safe_input, outputs)
            selected_edges = _condition_selected_edges(node, outgoing.get(node_id, []), bool(result["result"]))
            outputs[node_id] = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            condition_outputs[node_id] = result
            for edge in outgoing.get(node_id, []):
                if (edge["source"], edge["target"]) not in selected_edges:
                    active_edges.discard((edge["source"], edge["target"]))
            trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "completed", "external_call": False, "approval_required": False, "condition_result": bool(result["result"]), "condition_operator": result.get("operator"), "selected_targets": [edge["target"] for edge in outgoing.get(node_id, []) if (edge["source"], edge["target"]) in selected_edges], "output_preview": outputs[node_id][:600]})
            continue
        if node["type"] == "mcp_read" and (node.get("config") or {}).get("mode", "synthetic") == "live":
            payload, serialized, sources = await _run_mcp_read(service, user, conversation, node, tool_scope, safe_input, outputs)
            outputs[node_id] = serialized
            tool_outputs[node_id] = payload
            tool_evidence[node_id] = sources
            trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "completed", "external_call": True, "approval_required": False, "tool_call": True, "tool_name": payload["tool_name"], "connection_id": payload["connection_id"], "evidence_count": len(sources), "output_preview": serialized[:600]})
            continue
        if node["type"] == "approval":
            outputs[node_id] = json.dumps({"kind": "workflow_approval", "status": "pending", "node_id": node_id}, ensure_ascii=False, separators=(",", ":"))
            trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "awaiting_approval", "external_call": False, "approval_required": True, "approval_role": (node.get("config") or {}).get("role"), "output_preview": outputs[node_id]})
            approval_id = uid()
            output = run_output(index)
            _ensure_workflow_runtime_schema(service)
            with service.db.transaction():
                if resume_run:
                    _update_workflow_run(service, run_id, status="awaiting_approval", output=output, trace=trace, completed=False)
                else:
                    _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=conversation["id"] if conversation else None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="awaiting_approval", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp, completed=False)
                service.db.execute("INSERT INTO workflow_approvals(id,workflow_run_id,workflow_id,node_id,user_id,status,decision,comment,created_at,decided_at,decided_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (approval_id, run_id, workflow_id, node_id, user["id"], "pending", None, "", timestamp, None, None))
            approval = {"id": approval_id, "workflow_run_id": run_id, "workflow_id": workflow_id, "node_id": node_id, "status": "pending", "decision": None, "comment": "", "created_at": timestamp, "decided_at": None, "decided_by": None}
            return {"id": run_id, "workflow_id": workflow_id, "conversation_id": conversation["id"] if conversation else None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "awaiting_approval", "synthetic": not bool(tool_scope), "execution_mode": "tool" if tool_scope else "synthetic", "approval": approval, "approvals": [approval], "output": output, "trace": trace, "created_at": timestamp, "completed_at": None}
        outputs[node_id] = _simulated_node_output(node)
        trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "simulated", "external_call": False, "approval_required": False, "output_preview": outputs[node_id], **({"artifact_preview": artifact_index[node_id]} if node_id in artifact_index else {})})
    output = run_output()
    with service.db.transaction():
        if resume_run:
            _update_workflow_run(service, run_id, status="succeeded", output=output, trace=trace, completed=True)
        else:
            _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=conversation["id"] if conversation else None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="succeeded", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp)
    return {"id": run_id, "workflow_id": workflow_id, "conversation_id": conversation["id"] if conversation else None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "succeeded", "synthetic": not bool(tool_scope), "execution_mode": "tool" if tool_scope else "synthetic", "approvals": _workflow_approvals(service, run_id), "output": output, "trace": trace, "created_at": timestamp, "completed_at": now()}


def run_view(service, row, user):
    if row["user_id"] != user["id"]:
        # Access is checked through the owning assistant, not by exposing all
        # runs to team members who did not start them.
        _readable(service, row["workflow_id"], user)
    output = json.loads(row["output_json"])
    approvals = _workflow_approvals(service, row["id"])
    evidence_rows = service.db.all("SELECT node_id,connection_id,tool_name,payload_json,digest,created_at FROM workflow_tool_evidence WHERE workflow_run_id=? ORDER BY created_at,id", (row["id"],)) if service.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='workflow_tool_evidence'") else []
    tool_evidence_records = []
    for item in evidence_rows:
        try:
            payload = json.loads(item["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        tool_evidence_records.append({"node_id": item["node_id"], "connection_id": item["connection_id"], "tool_name": item["tool_name"], "evidence": payload.get("evidence", []), "digest": item["digest"], "created_at": item["created_at"]})
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "conversation_id": row.get("conversation_id"),
        "version": row["version"],
        "graph_hash": row["graph_hash"],
        "permission_digest": row["permission_digest"],
        "permission_snapshot": json.loads(row.get("permission_snapshot_json") or "{}"),
        "status": row["status"],
        "synthetic": bool(output.get("synthetic", False)),
        "execution_mode": output.get("execution_mode", "synthetic"),
        "output": output,
        "tool_evidence_records": tool_evidence_records,
        "trace": json.loads(row["trace_json"]),
        "approvals": approvals,
        **({"approval": next((item for item in approvals if item["status"] == "pending"), None)} if any(item["status"] == "pending" for item in approvals) else {}),
        "error": row["error"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


def run_summary(service, row, user):
    """Return a bounded history entry without replaying full run payloads."""
    if row["user_id"] != user["id"]:
        _readable(service, row["workflow_id"], user)
    try:
        output = json.loads(row.get("output_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        output = {}
    try:
        trace = json.loads(row.get("trace_json") or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        trace = []
    pending = next((item for item in _workflow_approvals(service, row["id"]) if item["status"] == "pending"), None)
    return {
        "id": row["id"],
        "workflow_id": row["workflow_id"],
        "version": row["version"],
        "status": row["status"],
        "synthetic": bool(output.get("synthetic", False)),
        "execution_mode": output.get("execution_mode", "synthetic"),
        "trace_count": len(trace) if isinstance(trace, list) else 0,
        "approval_node_id": pending.get("node_id") if pending else None,
        "error": row.get("error"),
        "created_at": row["created_at"],
        "completed_at": row.get("completed_at"),
    }


def _approval_decision_body(body: dict) -> tuple[str, str]:
    if not isinstance(body, dict):
        raise AppError(422, "WORKFLOW_APPROVAL_INVALID", "审批决定格式无效")
    decision = body.get("decision")
    if decision not in {"approve", "reject"}:
        raise AppError(422, "WORKFLOW_APPROVAL_INVALID", "审批决定必须是 approve 或 reject")
    comment = body.get("comment", "")
    if not isinstance(comment, str) or len(comment) > 1000:
        raise AppError(422, "WORKFLOW_APPROVAL_INVALID", "审批说明不能超过 1000 个字符")
    return decision, comment.strip()


def _record_approval_resume_failure(service, run_id: str, approval_id: str, node_id: str, decided_at: str, user: dict, error: Exception) -> None:
    message = error.message if isinstance(error, AppError) else "工作流审批后继续执行失败"
    code = error.code if isinstance(error, AppError) else "WORKFLOW_RESUME_FAILED"
    current = service.db.one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    with service.db.transaction():
        if current and current["status"] == "awaiting_approval":
            failed_output = json.loads(current["output_json"] or "{}")
            failed_trace = json.loads(current["trace_json"] or "[]")
            failed_output["approval_status"] = "approved_but_resume_failed"
            failed_trace.append({"sequence": len(failed_trace) + 1, "node_id": node_id, "node_type": "approval", "status": "failed", "external_call": False, "approval_required": True, "error_code": code, "output_preview": message})
            _update_workflow_run(service, run_id, status="failed", output=failed_output, trace=failed_trace, error=message, completed=True)
        service.db.execute("UPDATE workflow_approvals SET status='approved',decided_at=?,decided_by=? WHERE id=?", (decided_at, user["id"], approval_id))


async def decide_workflow_approval(service, run_id: str, node_id: str, body: dict, user):
    _ensure_workflow_runtime_schema(service)
    run_row = service.db.one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
    if not run_row:
        raise AppError(404, "NOT_FOUND", "工作流运行不存在")
    _readable(service, run_row["workflow_id"], user)
    approval_row = service.db.one("SELECT * FROM workflow_approvals WHERE workflow_run_id=? AND node_id=?", (run_id, node_id))
    if not approval_row:
        raise AppError(404, "WORKFLOW_APPROVAL_NOT_FOUND", "工作流审批节点不存在")
    if approval_row["status"] != "pending" or run_row["status"] != "awaiting_approval":
        raise AppError(409, "WORKFLOW_APPROVAL_DECIDED", "此审批已经处理，不能重复决定")
    decision, comment = _approval_decision_body(body)
    decided_at = now()
    if decision == "reject":
        output = json.loads(run_row["output_json"] or "{}")
        trace = json.loads(run_row["trace_json"] or "[]")
        for item in trace:
            if item.get("node_id") == node_id and item.get("status") == "awaiting_approval":
                item["status"] = "rejected"
                item["approval_decision"] = "reject"
                item["approval_comment"] = comment
                item["decided_at"] = decided_at
        output["approval_status"] = "rejected"
        with service.db.transaction():
            service.db.execute("UPDATE workflow_approvals SET status='rejected',decision=?,comment=?,decided_at=?,decided_by=? WHERE id=? AND status='pending'", (decision, comment, decided_at, user["id"], approval_row["id"]))
            _update_workflow_run(service, run_id, status="failed", output=output, trace=trace, error="工作流审批被拒绝", completed=True)
        return run_view(service, service.db.one("SELECT * FROM workflow_runs WHERE id=?", (run_id,)), user)

    with service.db.transaction():
        updated = service.db.execute("UPDATE workflow_approvals SET status='deciding',decision=?,comment=? WHERE id=? AND status='pending'", (decision, comment, approval_row["id"]))
        if updated.rowcount != 1:
            raise AppError(409, "WORKFLOW_APPROVAL_DECIDED", "此审批已经处理，不能重复决定")
    try:
        stored_input = json.loads(run_row["input_json"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        failure = AppError(409, "WORKFLOW_RUN_CORRUPT", "工作流运行输入无法读取")
        _record_approval_resume_failure(service, run_id, approval_row["id"], node_id, decided_at, user, failure)
        raise failure from error
    resume_body = WorkflowRunRequest(input=stored_input, conversation_id=run_row.get("conversation_id"))
    output = json.loads(run_row["output_json"] or "{}")
    try:
        if output.get("execution_mode") == "model":
            resumed = await run_model(service, run_row["workflow_id"], resume_body, user, resume_run=run_row, approved_node_id=node_id)
        else:
            resumed = await run_synthetic(service, run_row["workflow_id"], resume_body, user, resume_run=run_row, approved_node_id=node_id)
    except Exception as error:
        _record_approval_resume_failure(service, run_id, approval_row["id"], node_id, decided_at, user, error)
        raise
    with service.db.transaction():
        service.db.execute("UPDATE workflow_approvals SET status='approved',decided_at=?,decided_by=? WHERE id=?", (decided_at, user["id"], approval_row["id"]))
    return run_view(service, service.db.one("SELECT * FROM workflow_runs WHERE id=?", (run_id,)), user)


def create_workflow_router(service_dependency, authenticated):
    router = APIRouter(prefix="/api/v1", tags=["Workflows"])

    @router.get("/workflow-templates")
    def templates(user=Depends(authenticated)):
        return [
            {
                "template_id": value["template_id"],
                "name": value["name"],
                "description": value["description"],
                "synthetic": True,
                "graph": value["graph"],
            }
            for value in (_template(template_id) for template_id in _TEMPLATE_DEFINITIONS)
        ]

    @router.get("/assistants/{assistant_id}/workflows")
    def list_for_assistant(assistant_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        assistant = service.usable_assistant(assistant_id, user)
        return [_view(service, row, user, include_graph=False) for row in service.db.all("SELECT * FROM workflows WHERE assistant_id=? AND deleted=0 ORDER BY updated_at DESC", (assistant["id"],))]

    @router.post("/assistants/{assistant_id}/workflows", status_code=201)
    def create(assistant_id: str, body: WorkflowCreate, user=Depends(authenticated), service=Depends(service_dependency)):
        return create_workflow(service, assistant_id, body, user)

    @router.post("/assistants/{assistant_id}/workflows/from-template", status_code=201)
    def create_from_template(assistant_id: str, body: WorkflowTemplateCreate, user=Depends(authenticated), service=Depends(service_dependency)):
        return create_workflow_from_template(service, assistant_id, body, user)

    @router.get("/workflows/{workflow_id}")
    def get(workflow_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        return _view(service, _readable(service, workflow_id, user), user)

    @router.get("/workflows/{workflow_id}/runs")
    def list_runs(workflow_id: str, limit: int = Query(default=20, ge=1, le=50), user=Depends(authenticated), service=Depends(service_dependency)):
        _readable(service, workflow_id, user)
        rows = service.db.all("SELECT * FROM workflow_runs WHERE workflow_id=? ORDER BY created_at DESC,id DESC LIMIT ?", (workflow_id, limit))
        return [run_summary(service, row, user) for row in rows]

    @router.patch("/workflows/{workflow_id}/draft")
    def draft(workflow_id: str, body: WorkflowDraftPatch, user=Depends(authenticated), service=Depends(service_dependency)):
        return patch_draft(service, workflow_id, body, user)

    @router.post("/workflows/{workflow_id}/publish")
    def publish_route(workflow_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        return publish(service, workflow_id, user)

    @router.post("/workflows/{workflow_id}/rollback")
    def rollback_route(workflow_id: str, body: WorkflowRollback, user=Depends(authenticated), service=Depends(service_dependency)):
        return rollback(service, workflow_id, body, user)

    @router.post("/workflows/{workflow_id}/runs", status_code=201)
    async def run(workflow_id: str, body: WorkflowRunRequest, user=Depends(authenticated), service=Depends(service_dependency)):
        return await run_workflow(service, workflow_id, body, user)

    @router.post("/workflows/{workflow_id}/nodes/{node_id}/debug", status_code=201)
    async def debug(workflow_id: str, node_id: str, body: WorkflowNodeDebugRequest, user=Depends(authenticated), service=Depends(service_dependency)):
        return await debug_node(service, workflow_id, node_id, body, user)

    @router.get("/workflow-runs/{run_id}")
    def get_run(run_id: str, user=Depends(authenticated), service=Depends(service_dependency)):
        row = service.db.one("SELECT * FROM workflow_runs WHERE id=?", (run_id,))
        if not row:
            raise AppError(404, "NOT_FOUND", "工作流运行不存在")
        return run_view(service, row, user)

    @router.post("/workflow-runs/{run_id}/approvals/{node_id}/decision")
    async def decide_approval(run_id: str, node_id: str, body: dict, user=Depends(authenticated), service=Depends(service_dependency)):
        return await decide_workflow_approval(service, run_id, node_id, body, user)

    return router
