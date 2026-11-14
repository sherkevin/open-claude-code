"""Model-visible request rules complement, but never replace, host checks."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from app.db import dump
from app.harness_runtime import HarnessRuntime
from app.runtime import SDKRuntime
from app.security import AppError
from app.tool_constraint_context import compact_constraint, constrained_input_schema, sdk_constraint_context
from test_tool_constraints import CONSTRAINT, REMOTE, VALID, env, prepare  # noqa: F401
from test_tool_constraints_runtime import capture_sdk
from test_harness_profile import plugin_runner  # noqa: F401


def test_annotation_removal_preserves_business_fields_and_schema_semantics():
    value = {"type": "object", "title": "Do not repeat this annotation", "description": "not a rule",
             "properties": {"description": {"type": "string", "const": "business value", "description": "annotation"},
                            "items": {"type": "array", "items": {"type": "string", "enum": ["description", "title"]}}}}
    before = copy.deepcopy(value)
    compact = compact_constraint(value)
    assert "title" not in compact and "description" not in compact
    assert compact["properties"]["description"] == {"type": "string", "const": "business value"}
    assert compact["properties"]["items"]["items"]["enum"] == ["description", "title"]
    assert value == before


@pytest.mark.parametrize("arguments", [VALID, {**VALID, "limit": 11}, {**VALID, "fields": ["private_cost"]},
                                      {**VALID, "unknown": 1}, {**VALID, "date_to": "not-a-date"}])
def test_effective_schema_is_intersection_not_a_merge_that_widens_permissions(arguments):
    remote, policy = copy.deepcopy(REMOTE), copy.deepcopy(CONSTRAINT)
    projected = constrained_input_schema(remote, policy)
    check = lambda schema: Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(arguments)
    assert check(projected) == (check(remote) and check(policy))
    assert remote == REMOTE and policy == CONSTRAINT
    assert projected["allOf"][0] is not remote


def test_additional_and_unevaluated_property_rules_keep_their_own_scope():
    remote = {"type": "object", "properties": {"code": {"type": "string"}}, "unevaluatedProperties": False}
    policy = {"type": "object", "properties": {"code": {"const": "SAFE"}}}
    projected = constrained_input_schema(remote, policy)
    assert Draft202012Validator(projected).is_valid({"code": "SAFE"})
    assert not Draft202012Validator(projected).is_valid({"code": "SAFE", "extra": 1})
    assert not Draft202012Validator(projected).is_valid({"code": "OTHER"})


def test_real_sdk_options_include_pinned_rules_before_any_tool_invocation(env, monkeypatch):
    env.service.db.execute("UPDATE conversations SET runtime_type='claude' WHERE id=?", (env.conversation["id"],))
    prepared = prepare(env)
    capture = capture_sdk(monkeypatch)

    async def unused(*args, **kwargs):
        raise AssertionError("Context preparation must not invoke or approve a tool")

    async def scenario():
        config = env.service.runtime_config(env.conversation["id"])
        config["provider_env"] = {"ANTHROPIC_API_KEY": "synthetic-key", "ANTHROPIC_BASE_URL": "https://gateway.example"}
        runtime = SDKRuntime(env.service, env.conversation["id"], config, unused, unused)
        runtime.current_run_id = prepared["run_id"]
        runtime.current_run = env.service.require("runs", prepared["run_id"])
        await runtime.connect()
        prompt = capture["options"].system_prompt["prompt"]
        assert env.tool in prompt and '"maximum":10' in prompt
        assert '"enum":["id","status"]' in prompt
        assert '"const":"SYNTHETIC-F01"' in prompt
        assert "synthetic-key" not in prompt and "gateway.example" not in prompt
        assert "不能编造值" in prompt and "不授予权限" in prompt
        await runtime.close()

    asyncio.run(scenario())


def test_harness_definition_reaches_actual_plugin_registration_with_constraints(env, plugin_runner):
    prepare(env)
    async def unused(*args, **kwargs):
        raise AssertionError("No execution is needed to inspect tool registration")
    runtime = HarnessRuntime(env.service, env.conversation["id"], env.service.runtime_config(env.conversation["id"]), unused, unused)
    tool = next(iter(runtime.bridge.business_tools.values()))
    definition = tool.definition()
    assert tool.input_schema == REMOTE
    assert definition["input_schema"] == constrained_input_schema(REMOTE, CONSTRAINT)
    plugin_runner("""
assert.deepEqual(tools[0].parameters, config.tools[0].input_schema)
assert.equal(tools[0].parameters.allOf[1].properties.limit.maximum, 10)
assert.deepEqual(tools[0].parameters.allOf[1].properties.fields.items.enum, ['id','status'])
assert.equal(transport.requests.length, 0)
""", configure="config.tools = " + json.dumps([definition]) + "\n")


def test_revoked_policy_is_rejected_before_sdk_prompt_is_built(env):
    prepare(env)
    runtime = SimpleNamespace(service=env.service, conversation_id=env.conversation["id"], config=env.service.runtime_config(env.conversation["id"]))
    changed = copy.deepcopy(env.binding)
    changed["argument_constraints"]["read_orders"]["properties"]["limit"]["maximum"] = 9
    env.service.save_assistant({"connections": [changed]}, env.assistant["id"], user=env.user)
    with pytest.raises(AppError, match="请求约束已变化"):
        sdk_constraint_context(runtime)


def test_large_combined_rules_fail_explicitly_before_entering_model_context(env):
    properties = {f"field{i}": {"const": str(i) + "x" * 950} for i in range(28)}
    policy = {"type": "object", "properties": properties}
    remote = {"type": "object", "properties": {name: {"type": "string"} for name in properties}}
    names = ["read_a", "read_b", "read_c"]
    env.service.db.execute("UPDATE connections SET tools=? WHERE id=?", (dump([{"name": name, "inputSchema": remote} for name in names]), env.connection["id"]))
    binding = {**env.binding, "allowed_tools": names, "auto_approved_tools": [], "read_only_confirmations": {}, "argument_constraints": {name: policy for name in names}}
    env.service.save_assistant({"connections": [binding]}, env.assistant["id"], user=env.user)
    prepare(env)
    runtime = SimpleNamespace(service=env.service, conversation_id=env.conversation["id"], config=env.service.runtime_config(env.conversation["id"]))
    with pytest.raises(AppError) as error:
        sdk_constraint_context(runtime)
    assert error.value.code == "TOOL_CONSTRAINT_CONTEXT_TOO_LARGE"
    async def unused(*args, **kwargs):
        raise AssertionError("No execution expected")
    with pytest.raises(AppError) as harness_error:
        HarnessRuntime(env.service, runtime.conversation_id, runtime.config, unused, unused)
    assert harness_error.value.code == "TOOL_CONSTRAINT_CONTEXT_TOO_LARGE"


def test_trial_context_has_no_business_config_or_secret_access():
    runtime = SimpleNamespace(service=SimpleNamespace(policy=SimpleNamespace(is_trial=True)))
    assert sdk_constraint_context(runtime) == ""
