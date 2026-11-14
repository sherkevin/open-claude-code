"""Opt-in, validate-only argument boundaries for an assistant's MCP tools.

These schemas narrow requests; they do not rewrite arguments or attest that a
remote service actually projected rows/fields or respected a business scope.
"""
from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache

from jsonschema import Draft202012Validator, FormatChecker

from .security import AppError

MAX_CONSTRAINT_BYTES = 32_000
MAX_BINDING_CONSTRAINT_BYTES = 128_000
MAX_CONSTRAINED_TOOLS = 64
MAX_ARGUMENT_BYTES = 512_000
MAX_REMOTE_SCHEMA_BYTES = 128_000
_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "const",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "format", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "allOf", "anyOf", "oneOf", "not", "title", "description",
})
_REMOTE_KEYWORDS = _KEYWORDS | frozenset({
    "$schema", "$comment", "default", "examples", "deprecated", "readOnly", "writeOnly",
    "prefixItems", "unevaluatedProperties", "unevaluatedItems", "contains", "minContains", "maxContains",
    "propertyNames", "if", "then", "else", "dependentSchemas", "dependentRequired",
})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _invalid():
    return AppError(422, "TOOL_CONSTRAINT_INVALID", "工具请求约束无效、过大或使用了不支持的规则")


def _bounded_json(value, depth=0):
    if depth > 12:
        raise _invalid()
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if isinstance(value, str) and len(value) <= 4000:
        return
    if isinstance(value, list) and len(value) <= 100:
        for item in value:
            _bounded_json(item, depth + 1)
        return
    if isinstance(value, dict) and len(value) <= 100:
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 200:
                raise _invalid()
            _bounded_json(item, depth + 1)
        return
    raise _invalid()


def _schema_subset(schema, depth=0, budget=None):
    budget = [128] if budget is None else budget
    budget[0] -= 1
    if depth > 8 or budget[0] < 0 or type(schema) is not dict or set(schema) - _KEYWORDS:
        raise _invalid()
    # No refs, regexes, executable expressions or default injection. Restrict
    # combinators as well as serialized size so validation stays bounded.
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            items = schema[key]
            if not isinstance(items, list) or not 1 <= len(items) <= 8:
                raise _invalid()
            for item in items:
                _schema_subset(item, depth + 1, budget)
    for key in ("items", "not"):
        if key in schema:
            _schema_subset(schema[key], depth + 1, budget)
    if "properties" in schema:
        if type(schema["properties"]) is not dict:
            raise _invalid()
        for item in schema["properties"].values():
            _schema_subset(item, depth + 1, budget)
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise _invalid()
    if "format" in schema and schema["format"] not in {"date", "date-time"}:
        raise _invalid()
    if "const" in schema and isinstance(schema["const"], (dict, list)):
        raise _invalid()
    if "enum" in schema and (not isinstance(schema["enum"], list) or any(isinstance(item, (dict, list)) for item in schema["enum"])):
        raise _invalid()
    if schema.get("uniqueItems") is True and (type(schema.get("maxItems")) is not int or not 0 <= schema["maxItems"] <= 100):
        raise _invalid()


def _safe_remote_validation(schema, depth=0, budget=None):
    """Additional local validation must not execute remote regexes/ref graphs."""
    budget = [128] if budget is None else budget
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "工具参数定义过于复杂，无法启用请求约束")
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "工具参数定义未核验，无法启用请求约束")
    if set(schema) - _REMOTE_KEYWORDS:
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "工具参数定义包含当前不支持本地约束验证的规则")
    if "$schema" in schema and schema["$schema"].rstrip("#") != Draft202012Validator.META_SCHEMA["$id"]:
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "请求约束当前仅支持 Draft 2020-12 工具参数定义")
    for key in ("properties", "dependentSchemas"):
        for child in (schema.get(key) or {}).values():
            _safe_remote_validation(child, depth + 1, budget)
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in schema.get(key, []):
            _safe_remote_validation(child, depth + 1, budget)
    for key in ("items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "contains", "propertyNames", "not", "if", "then", "else"):
        if key in schema:
            children = schema[key] if isinstance(schema[key], list) else [schema[key]]
            for child in children:
                _safe_remote_validation(child, depth + 1, budget)


def _bounded_arguments(arguments):
    pending = [(arguments, 0)]
    remaining = 2048
    while pending:
        value, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > 12:
            raise ValueError("request structure exceeds validation budget")
        if isinstance(value, (list, dict)):
            if len(value) > 100:
                raise ValueError("request collection exceeds validation budget")
            pending.extend((item, depth + 1) for item in (value.values() if isinstance(value, dict) else value))


def _declared_parameters(constraint, remote):
    """Refuse a policy referring to fields the discovery schema did not name.

    Complex/ref-based remote schemas remain supported by normal MCP calls,
    but cannot opt in until their constrained paths are explicitly declared.
    """
    properties = remote.get("properties") if isinstance(remote, dict) else None
    names = set(constraint.get("properties", {})) | set(constraint.get("required", []))
    if names and (not isinstance(properties, dict) or not names <= properties.keys()):
        raise AppError(422, "TOOL_CONSTRAINT_PARAMETER_UNKNOWN", "请求约束引用了工具定义未明确声明的参数，请核对工具 Schema")
    for name, child in constraint.get("properties", {}).items():
        _declared_parameters(child, properties[name])
    if "items" in constraint:
        remote_items = remote.get("items") if isinstance(remote, dict) else None
        if not isinstance(remote_items, dict):
            raise AppError(422, "TOOL_CONSTRAINT_PARAMETER_UNKNOWN", "数组参数的元素定义不明确，无法启用此约束")
        _declared_parameters(constraint["items"], remote_items)
    for key in ("allOf", "anyOf", "oneOf"):
        for child in constraint.get(key, []):
            _declared_parameters(child, remote)
    if "not" in constraint:
        _declared_parameters(constraint["not"], remote)


def constraint_map(binding):
    value = binding.get("argument_constraints", {})
    if type(value) is not dict:
        raise _invalid()
    try:
        if len(value) > MAX_CONSTRAINED_TOOLS or len(_json(value).encode()) > MAX_BINDING_CONSTRAINT_BYTES:
            raise _invalid()
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None
    if not set(value) <= set(binding.get("allowed_tools", [])):
        raise AppError(422, "TOOL_CONSTRAINT_UNBOUND", "只能为助手已允许的工具设置请求约束")
    return value


@lru_cache(maxsize=64)
def _compiled_schemas(constraint_json, remote_json):
    """Cache only public schema compilation, never authority or arguments.

    The caller checks current connection status and credentials before entering
    this cache. Canonical JSON keys change on every substantive schema change.
    """
    from .mcp_tools import schema_validator

    schema, remote = json.loads(constraint_json), json.loads(remote_json)
    try:
        _bounded_json(schema)
        if type(schema) is not dict or schema.get("type") != "object":
            raise _invalid()
        _schema_subset(schema)
        Draft202012Validator.check_schema(schema)
    except AppError:
        raise
    except Exception:
        raise _invalid() from None
    try:
        remote_validator = schema_validator(remote)
        _safe_remote_validation(remote)
    except Exception:
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "工具参数定义未核验或不受支持，无法启用请求约束") from None
    _declared_parameters(schema, remote)
    return (Draft202012Validator(schema, format_checker=FormatChecker()), remote_validator,
            hashlib.sha256(remote_json.encode()).hexdigest())


def _schema_keys(schema, remote):
    try:
        constraint_json, remote_json = _json(schema), _json(remote)
    except (TypeError, ValueError, RecursionError):
        raise _invalid() from None
    if len(constraint_json.encode()) > MAX_CONSTRAINT_BYTES:
        raise _invalid()
    if len(remote_json.encode()) > MAX_REMOTE_SCHEMA_BYTES:
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "工具参数定义过大，无法启用请求约束")
    return constraint_json, remote_json


def validate_definitions(binding, connection, credential=None):
    """Validate configuration and return the discovered input-schema digests."""
    constraints = constraint_map(binding)
    if not constraints:
        return {}
    if not connection.get("enabled") or connection.get("status") != "connected":
        raise AppError(409, "TOOL_CONSTRAINT_CONNECTION_UNVERIFIED", "请先成功测试并启用连接，再使用工具请求约束")
    from .mcp_tools import contains_credential

    try:
        definitions = json.loads(connection.get("tools") or "[]")
    except (TypeError, ValueError):
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "工具参数定义未核验，请重新测试连接") from None
    if not isinstance(definitions, list):
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_UNVERIFIED", "工具参数定义未核验，请重新测试连接")
    digests = {}
    for name, schema in constraints.items():
        matching = [item for item in definitions if isinstance(item, dict) and item.get("name") == name]
        remote = matching[0].get("inputSchema") if len(matching) == 1 else None
        keys = _schema_keys(schema, remote)
        if credential and (contains_credential(schema, credential) or contains_credential(remote, credential)):
            raise AppError(422, "TOOL_CONSTRAINT_SECRET", "工具请求约束或参数定义包含连接凭据，请移除后重试")
        digests[name] = _compiled_schemas(*keys)[2]
    return digests


def assert_snapshot(binding, prior_binding, connection, schema_snapshot, credential=None):
    current = constraint_map(binding)
    prior = constraint_map(prior_binding) if prior_binding is not None else {}
    if _json(current) != _json(prior):
        raise AppError(409, "TOOL_CONSTRAINT_CHANGED", "工具请求约束已变化，请重新发送任务")
    digests = validate_definitions(binding, connection, credential)
    if digests and digests != schema_snapshot:
        raise AppError(409, "TOOL_CONSTRAINT_SCHEMA_CHANGED", "工具参数定义已变化，请重新核对请求约束并发送任务")


def validate_arguments(binding, connection, tool_name, arguments, credential=None):
    constraints = constraint_map(binding)
    if tool_name not in constraints:
        return
    validate_definitions(binding, connection, credential)
    remote = next(item["inputSchema"] for item in json.loads(connection["tools"]) if item.get("name") == tool_name)
    constraint_validator, remote_validator, _ = _compiled_schemas(*_schema_keys(constraints[tool_name], remote))
    try:
        if type(arguments) is not dict or len(_json(arguments).encode()) > MAX_ARGUMENT_BYTES:
            raise ValueError()
        _bounded_arguments(arguments)
        remote_validator.validate(arguments)
        constraint_validator.validate(arguments)
    except Exception:
        # ValidationError contains the rejected value: do not expose it.
        raise AppError(422, "TOOL_ARGUMENT_CONSTRAINT_VIOLATION", "取数参数不符合工具定义或已配置范围，请收窄字段、范围和数量后重试") from None


def binding_digest(binding, connection, credential=None):
    schemas = validate_definitions(binding, connection, credential)
    return hashlib.sha256(_json({"constraints": constraint_map(binding), "schemas": schemas}).encode()).hexdigest()
