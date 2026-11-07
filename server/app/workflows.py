"""Versioned workflow contracts and bounded agent-node execution.

Most node types remain declarative and local-only.  Agent nodes can opt into a
provider-backed mode after selecting a public catalog model; the call is still
bounded, permission-pinned and recorded in the workflow run trace.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict, deque

import httpx
from fastapi import APIRouter, Depends

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
_BINDING_SOURCE = re.compile(r"^(?:input(?:\.[A-Za-z][A-Za-z0-9_-]{0,63})?|node\.[A-Za-z0-9_-]{1,64}\.output)$")
_MAX_AGENT_CONTEXT = 12_000
_MAX_AGENT_OUTPUT = 12_000


_TEMPLATE_DEFINITIONS = {
    "finance-application-v1": {
        "template_id": "finance-application-v1",
        "name": "财务申请（合成模板）",
        "description": "表单填报、政策检索、金额条件和人工审批，全部使用合成字段。",
        "synthetic": True,
        "graph": {
            "nodes": [
                {"id": "form", "type": "skill", "label": "填报申请表", "config": {"mode": "synthetic", "template_field": "申请事项", "form_fields": [{"id": "purpose", "type": "text", "label": "申请事项", "required": True}, {"id": "amount", "type": "number", "label": "申请金额", "required": True}, {"id": "date", "type": "date", "label": "申请日期", "required": True}]}},
                {"id": "policy", "type": "knowledge_search", "label": "检索财务政策", "config": {"mode": "synthetic", "knowledge_scope": "synthetic-finance"}},
                {"id": "amount", "type": "condition", "label": "判断金额边界", "config": {"mode": "synthetic", "amount_limit": 5000, "currency": "CNY"}},
                {"id": "approval", "type": "approval", "label": "部门负责人审批", "config": {"mode": "synthetic", "role": "department_owner"}},
                {"id": "export", "type": "artifact", "label": "生成申请表", "config": {"mode": "synthetic", "format": "xlsx"}},
            ],
            "edges": [
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
                {"id": "receipt", "type": "skill", "label": "填报报销单", "config": {"mode": "synthetic", "template_field": "报销事项", "form_fields": [{"id": "reason", "type": "text", "label": "报销事由", "required": True}, {"id": "amount", "type": "number", "label": "报销金额", "required": True}, {"id": "date", "type": "date", "label": "发生日期", "required": True}]}},
                {"id": "policy", "type": "knowledge_search", "label": "检索费用政策", "config": {"mode": "synthetic", "knowledge_scope": "synthetic-finance"}},
                {"id": "compliance", "type": "condition", "label": "检查合规条件", "config": {"mode": "synthetic", "required_fields": ["金额", "事由", "日期"]}},
                {"id": "dual", "type": "approval", "label": "双人审批", "config": {"mode": "synthetic", "required_approvals": 2}},
                {"id": "export", "type": "artifact", "label": "生成报销台账", "config": {"mode": "synthetic", "format": "xlsx"}},
            ],
            "edges": [
                {"source": "receipt", "target": "policy", "label": "查找依据"},
                {"source": "policy", "target": "compliance", "label": "核对字段"},
                {"source": "compliance", "target": "dual", "label": "进入审批"},
                {"source": "dual", "target": "export", "label": "留存结果"},
            ],
        },
    },
}


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
            raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", "变量来源必须是 input、input.字段或 node.节点.output")
        seen.add(key)


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
        mode = config.get("mode")
        if mode is not None and mode not in {"synthetic", "model"}:
            raise AppError(422, "WORKFLOW_EXTERNAL_EXECUTION_FORBIDDEN", "当前工作流只允许 synthetic 或 model 执行模式")
        if node_type == "agent" or mode == "model":
            _validate_agent_config(config, node_type)
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
            source_id = source[5:-7]
            if source_id not in node_map:
                raise AppError(422, "WORKFLOW_AGENT_BINDING_INVALID", f"变量来源节点不存在：{source_id}")
            if source_id == node_id or order_index[source_id] >= order_index[node_id]:
                raise AppError(422, "WORKFLOW_AGENT_BINDING_ORDER", "变量只能引用已经完成的上游节点输出")

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


def _resolve_input_bindings(config: dict, safe_input: dict, outputs: dict[str, str]) -> dict | None:
    bindings = config.get("input_bindings") or []
    if not bindings:
        return None
    resolved = {}
    for binding in bindings:
        source = binding["source"]
        if source == "input":
            value = safe_input
        elif source.startswith("input."):
            value = safe_input.get(source[6:])
        else:
            value = outputs.get(source[5:-7])
        resolved[binding["key"]] = value
    return resolved


def _agent_user_prompt(node: dict, safe_input: dict, outputs: dict[str, str], config: dict, mapped_inputs: dict | None = None) -> str:
    task = (config.get("prompt") or node.get("label") or "完成当前节点任务").strip()
    upstream = {key: value[-4000:] for key, value in outputs.items()}
    payload = {"input": safe_input, "upstream_outputs": upstream} if mapped_inputs is None else {"mapped_inputs": mapped_inputs}
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"节点任务：{task}\n以下是本节点被明确授权读取的工作流上下文，请基于它们完成任务。\n{serialized}\n请直接返回本节点的最终结果。"[:_MAX_AGENT_CONTEXT]


def _simulated_node_output(node: dict) -> str:
    """Expose an explicit bounded marker when a non-model node has no runtime yet."""

    return json.dumps({"status": "simulated", "node_id": node["id"], "node_type": node["type"]}, ensure_ascii=False, separators=(",", ":"))


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


def _insert_workflow_run(service, *, run_id: str, workflow_id: str, conversation_id: str | None, version: int, graph_hash: str, permission_digest: str, permission_snapshot: dict, user_id: str, status: str, safe_input: dict, output: dict, trace: list[dict], timestamp: str, error: str | None = None) -> None:
    service.db.execute(
        "INSERT INTO workflow_runs(id,workflow_id,conversation_id,version,graph_hash,permission_digest,permission_snapshot_json,user_id,status,input_json,output_json,trace_json,error,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, workflow_id, conversation_id, version, graph_hash, permission_digest, dump(permission_snapshot), user_id, status, dump(safe_input), dump(output), dump(trace), error, timestamp, now()),
    )


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
    try:
        mapped_inputs = _resolve_input_bindings(config, safe_input, {})
        text = await _invoke_agent_model(service, selection, system_prompt, _agent_user_prompt(node, safe_input, {}, config, mapped_inputs), config)
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
            "input_bindings": _binding_trace(config),
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
            "permission_snapshot": permission_snapshot,
        }
        with service.db.transaction():
            _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="succeeded", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp)
        return {"id": run_id, "workflow_id": workflow_id, "conversation_id": None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "succeeded", "synthetic": False, "execution_mode": "model", "output": output, "trace": trace, "created_at": timestamp, "completed_at": now()}
    except AppError as error:
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
            "permission_snapshot": permission_snapshot,
        }
        with service.db.transaction():
            _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="failed", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp, error=error.message)
        raise


async def run_model(service, workflow_id, body: WorkflowRunRequest, user):
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
    public_models = [{key: selection[key] for key in ("model_id", "model_label", "reasoning_level")} for selection in selections.values()]
    permission_payload = {"assistant_id": assistant["id"], "assistant_revision": assistant["revision"], "workflow_revision": row["revision"], "conversation_id": conversation["id"] if conversation else None, "user_id": user["id"], "models": public_models}
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
    }
    trace: list[dict] = []
    outputs: dict[str, str] = {}
    run_id, timestamp = uid(), now()
    current_node = None
    try:
        for index, node_id in enumerate(order, 1):
            node = node_map[node_id]
            config = node.get("config") or {}
            if node_id not in selections:
                outputs[node_id] = _simulated_node_output(node)
                trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "simulated", "external_call": False, "approval_required": node["type"] == "approval", "output_preview": outputs[node_id], **({"artifact_preview": artifact_index[node_id]} if node_id in artifact_index else {})})
                continue
            current_node = node
            role, system_prompt = _agent_system_prompt(assistant, config)
            mapped_inputs = _resolve_input_bindings(config, safe_input, outputs)
            text = await _invoke_agent_model(service, selections[node_id], system_prompt, _agent_user_prompt(node, safe_input, outputs, config, mapped_inputs), config)
            outputs[node_id] = text
            trace.append({"sequence": index, "node_id": node_id, "node_type": node["type"], "status": "completed", "external_call": True, "approval_required": False, "model_id": selections[node_id]["model_id"], "model_label": selections[node_id]["model_label"], "reasoning_level": selections[node_id]["reasoning_level"], "role": role, "input_bindings": _binding_trace(config), "output_preview": text[:600]})
        output = {"synthetic": False, "execution_mode": "model", "workflow_id": workflow_id, "version": version["version"], "graph_hash": graph_hash, "completed_nodes": len(trace), "input": safe_input, "agent_outputs": outputs, "artifacts": artifact_previews, "permission_snapshot": permission_snapshot}
        with service.db.transaction():
            _insert_workflow_run(service, run_id=run_id, workflow_id=workflow_id, conversation_id=conversation["id"] if conversation else None, version=version["version"], graph_hash=graph_hash, permission_digest=permission_digest, permission_snapshot=permission_snapshot, user_id=user["id"], status="succeeded", safe_input=safe_input, output=output, trace=trace, timestamp=timestamp)
        return {"id": run_id, "workflow_id": workflow_id, "conversation_id": conversation["id"] if conversation else None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "succeeded", "synthetic": False, "execution_mode": "model", "output": output, "trace": trace, "created_at": timestamp, "completed_at": now()}
    except AppError as error:
        if current_node:
            trace.append({"sequence": len(trace) + 1, "node_id": current_node["id"], "node_type": current_node["type"], "status": "failed", "external_call": True, "approval_required": False, "model_id": selections[current_node["id"]]["model_id"], "input_bindings": _binding_trace(current_node.get("config") or {}), "error_code": error.code})
        output = {"synthetic": False, "execution_mode": "model", "workflow_id": workflow_id, "version": version["version"], "graph_hash": graph_hash, "completed_nodes": len(trace), "input": safe_input, "agent_outputs": outputs, "artifacts": artifact_previews, "permission_snapshot": permission_snapshot}
        with service.db.transaction():
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
    return run_synthetic(service, workflow_id, body, user)


def run_synthetic(service, workflow_id, body: WorkflowRunRequest, user):
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
    trace = [
        {
            "sequence": index,
            "node_id": node_id,
            "node_type": node_map[node_id]["type"],
            "status": "simulated",
            "external_call": False,
            "approval_required": node_map[node_id]["type"] == "approval",
            **({"artifact_preview": artifact_index[node_id]} if node_id in artifact_index else {}),
        }
        for index, node_id in enumerate(order, 1)
    ]
    run_id, timestamp = uid(), now()
    assistant = service.require("assistants", row["assistant_id"])
    permission_payload = {
        "assistant_id": assistant["id"],
        "assistant_revision": assistant["revision"],
        "workflow_revision": row["revision"],
        "conversation_id": conversation["id"] if conversation else None,
        "user_id": user["id"],
    }
    permission_digest = hashlib.sha256(json.dumps(permission_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    permission_snapshot = {
        "schema_version": 1,
        "mode": "synthetic",
        "assistant_id": assistant["id"],
        "assistant_revision": assistant["revision"],
        "assistant_scope": assistant.get("scope", "private"),
        "workflow_id": workflow_id,
        "workflow_revision": row["revision"],
        "workflow_version": version["version"],
        "conversation_id": conversation["id"] if conversation else None,
        "user_id": user["id"],
        "user_role": user.get("role"),
    }
    output = {"synthetic": True, "workflow_id": workflow_id, "version": version["version"], "graph_hash": graph_hash, "completed_nodes": len(trace), "input": safe_input, "artifacts": artifact_previews, "permission_snapshot": permission_snapshot}
    with service.db.transaction():
        service.db.execute(
            "INSERT INTO workflow_runs(id,workflow_id,conversation_id,version,graph_hash,permission_digest,permission_snapshot_json,user_id,status,input_json,output_json,trace_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, workflow_id, conversation["id"] if conversation else None, version["version"], graph_hash, permission_digest, dump(permission_snapshot), user["id"], "succeeded", dump(safe_input), dump(output), dump(trace), timestamp, timestamp),
        )
    return {"id": run_id, "workflow_id": workflow_id, "conversation_id": conversation["id"] if conversation else None, "version": version["version"], "graph_hash": graph_hash, "permission_digest": permission_digest, "permission_snapshot": permission_snapshot, "status": "succeeded", "synthetic": True, "output": output, "trace": trace, "created_at": timestamp, "completed_at": timestamp}


def run_view(service, row, user):
    if row["user_id"] != user["id"]:
        # Access is checked through the owning assistant, not by exposing all
        # runs to team members who did not start them.
        _readable(service, row["workflow_id"], user)
    output = json.loads(row["output_json"])
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
        "trace": json.loads(row["trace_json"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
    }


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

    return router
