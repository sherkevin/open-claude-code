"""Host confirmations for the retired workflow surface's live mcp_read nodes.

Neither an automatic approval nor an MCP annotation is a host confirmation.
The confirmation is scoped to one assistant binding, a connection revision and
the observed tool definition. Actual read-only credentials remain a business
server responsibility.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import OrderedDict
from threading import Lock

from .security import AppError

ANNOTATIONS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
MAX_DEFINITION_BYTES = 128_000
_checked_digests: OrderedDict[str, None] = OrderedDict()
_digest_lock = Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def discovered_definition(tool):
    """Use one projection for saved discovery and the runtime live comparison.

    Only an actual True in the parsed MCP object is presented as a positive
    declaration. SDK parsing can normalize wire values; hints remain advisory.
    Generated digests and UI eligibility fields never enter their own hash.
    """
    if not isinstance(tool, dict):
        tool = tool.model_dump(mode="json", exclude_none=True)
    result = {"name": tool.get("name"), "description": (tool.get("description") or "")[:4000],
              "inputSchema": tool.get("inputSchema")}
    annotations = tool.get("annotations")
    if annotations is not None:
        if not isinstance(annotations, dict):
            result["annotations"] = None
        else:
            result["annotations"] = {key: annotations[key] for key in ANNOTATIONS if key in annotations}
    return result


def _definition_digest(serialized):
    from .mcp_tools import schema_validator

    digest = hashlib.sha256(serialized.encode()).hexdigest()
    # Cache successful shape checks by hash only. Never retain remote schema
    # text, sample values, credentials, authorization or connection state.
    with _digest_lock:
        if digest in _checked_digests:
            _checked_digests.move_to_end(digest)
            return digest
    definition = json.loads(serialized)
    if not isinstance(definition.get("name"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", definition["name"]):
        raise AppError(409, "READ_ONLY_DEFINITION_UNVERIFIED", "工具名称或参数定义未核验，请重新测试连接")
    try:
        schema_validator(definition.get("inputSchema"))
    except AppError:
        raise AppError(409, "READ_ONLY_DEFINITION_UNVERIFIED", "工具参数定义缺失或无法离线核验，请重新测试连接") from None
    annotations = definition.get("annotations", {})
    if not isinstance(annotations, dict) or any(type(value) is not bool for value in annotations.values()):
        raise AppError(409, "READ_ONLY_DEFINITION_UNVERIFIED", "工具注解格式无效，请核对工具定义")
    with _digest_lock:
        _checked_digests[digest] = None
        if len(_checked_digests) > 64:
            _checked_digests.popitem(last=False)
    return digest


def definition_digest(tool):
    try:
        definition = discovered_definition(tool)
        serialized = _json(definition)
        if len(serialized.encode()) > MAX_DEFINITION_BYTES:
            raise ValueError()
        return _definition_digest(serialized)
    except AppError:
        raise
    except Exception:
        raise AppError(409, "READ_ONLY_DEFINITION_UNVERIFIED", "工具定义缺失、过大或无法核验，请重新测试连接") from None


def require_eligible(tool):
    digest = definition_digest(tool)
    annotations = discovered_definition(tool).get("annotations", {})
    if annotations.get("readOnlyHint") is False or annotations.get("destructiveHint") is True:
        raise AppError(409, "READ_ONLY_DECLARATION_CONFLICT", "远端工具声明与只读用途冲突，不能确认用于只读工作流")
    return digest


def tool_view(tool):
    if not isinstance(tool, dict):
        return tool
    result = dict(tool)
    result["definition_digest"] = None
    try:
        digest = definition_digest(tool)
        result["definition_digest"] = digest
        require_eligible(tool)
        result["read_only_confirmation_eligible"] = True
        result["read_only_confirmation_reason"] = None
    except AppError as error:
        result["read_only_confirmation_eligible"] = False
        result["read_only_confirmation_reason"] = error.message
    return result


def confirmations(binding):
    value = binding.get("read_only_confirmations", {})
    if (type(value) is not dict or len(value) > 128
            or not set(value) <= set(binding.get("allowed_tools", []))):
        raise AppError(422, "READ_ONLY_CONFIRMATION_INVALID", "只能为当前助手已授权的工具保存只读确认")
    for record in value.values():
        if (type(record) is not dict or set(record) != {"connection_revision", "definition_digest", "basis"}
                or type(record["connection_revision"]) is not int or record["connection_revision"] < 1
                or not isinstance(record["definition_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["definition_digest"])
                or not isinstance(record["basis"], str) or not record["basis"].strip() or len(record["basis"]) > 500):
            raise AppError(422, "READ_ONLY_CONFIRMATION_INVALID", "只读确认需要当前连接版本、工具定义摘要和明确的人工核验依据")
    return value


def _current_definition(connection, name):
    if not connection.get("enabled") or connection.get("status") != "connected":
        raise AppError(409, "READ_ONLY_CONNECTION_UNVERIFIED", "只读工作流需要已启用且测试成功的连接")
    try:
        tools = json.loads(connection.get("tools") or "[]")
        matching = [item for item in tools if isinstance(item, dict) and item.get("name") == name]
    except (TypeError, ValueError):
        matching = []
    if len(matching) != 1:
        raise AppError(409, "READ_ONLY_DEFINITION_UNVERIFIED", "工具未发现或名称重复，请重新测试连接")
    return matching[0]


def validate_confirmation(binding, connection, name):
    record = confirmations(binding).get(name)
    if record is None:
        raise AppError(403, "WORKFLOW_READ_ONLY_UNCONFIRMED", "此工具尚未由宿主确认用于只读工作流；自动批准或远端只读提示不能替代确认")
    digest = require_eligible(_current_definition(connection, name))
    if record["connection_revision"] != connection["revision"] or record["definition_digest"] != digest:
        raise AppError(409, "READ_ONLY_CONFIRMATION_STALE", "连接或工具定义已变化，请重新核验并确认只读用途")
    return hashlib.sha256(_json(record).encode()).hexdigest()


def validate_saved_confirmations(binding, old_binding, connection, credential=None):
    new = confirmations(binding)
    if credential:
        from .mcp_tools import contains_credential
        if any(contains_credential(record["basis"], credential) for record in new.values()):
            raise AppError(422, "READ_ONLY_CONFIRMATION_INVALID", "只读核验依据不能包含连接凭据")
    old = (old_binding or {}).get("read_only_confirmations", {})
    if not isinstance(old, dict):
        old = {}
    for name, record in new.items():
        # Unrelated edits can preserve a stale confirmation for inspection.
        # It is inactive while the current revision/definition differs. An
        # identical definition restored at the same revision matches again;
        # this fingerprint is not an append-only discovery history.
        if old.get(name) != record:
            validate_confirmation(binding, connection, name)


def require_snapshot_confirmation(binding, prior_binding, connection, name):
    current = confirmations(binding).get(name)
    prior = confirmations(prior_binding or {}).get(name)
    if current != prior:
        raise AppError(409, "READ_ONLY_CONFIRMATION_CHANGED", "任务开始后只读确认已变化，请重新运行")
    return validate_confirmation(binding, connection, name)


async def verify_live_definition(session, name, expected_digest, recheck):
    """Compare the current remote definition before executing any operation."""
    matches, cursor, count = [], None, 0
    for _ in range(20):
        recheck()
        response = await session.list_tools(cursor=cursor)
        recheck()
        count += len(response.tools)
        if count > 1000:
            raise AppError(409, "READ_ONLY_DEFINITION_UNVERIFIED", "远端工具清单超出可核验范围")
        matches.extend(tool for tool in response.tools if (tool.get("name") if isinstance(tool, dict) else tool.name) == name)
        cursor = response.nextCursor
        if not cursor:
            break
    if cursor or len(matches) != 1 or require_eligible(matches[0]) != expected_digest:
        raise AppError(409, "READ_ONLY_CONFIRMATION_STALE", "远端工具定义已变化，请重新测试连接并确认只读用途")
