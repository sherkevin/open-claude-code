"""Composition boundary checks; these never import or start a model runtime."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

from harness.profile import KERNEL_MODULES, MAX_TOOL_DEFINITION_BYTES, TOOL_NAMES, write_profile


REPOSITORY = Path(__file__).resolve().parents[2]
EXTERNAL_NAME = "external_" + "ab" * 16
EXTERNAL_SCHEMA = {
    "type": "object", "$defs": {"identifier": {"type": "string", "minLength": 1}},
    "properties": {"id": {"$ref": "#/$defs/identifier"}},
    "required": ["id"], "additionalProperties": False,
}


@pytest.fixture
def arguments(tmp_path):
    source = tmp_path / "source"
    for _, package in KERNEL_MODULES:
        path = source / "packages" / package / "src" / "index.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// module path fixture; never executed\n")
    cwd = tmp_path / "work"
    cwd.mkdir()
    return {
        "profile_root": tmp_path / "private-home", "source_dir": source, "cwd": cwd,
        "session_id": "harness-session-1", "system_prompt": "literal !!js process.env.SECRET",
        "provider": {"baseURL": "http://127.0.0.1:9/v1", "model": "fixture", "reasoningEffort": "off"},
        "tools": [{"name": "knowledge_search", "description": "Search authorized documents",
                   "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                                    "required": ["query"], "additionalProperties": False}}],
    }


def test_profile_has_no_inherited_bundles_or_native_tools(arguments):
    result = write_profile(**arguments)
    directory = Path(result["profile_dir"])
    assert json.loads((directory / "package.json").read_text())["dsh"]["profile"]["bundles"] == []
    entries = json.loads((directory / "cordis.patch.yml").read_text())[0]["insert"]
    assert [entry["id"] for entry in entries] == [name for name, _ in KERNEL_MODULES] + ["workspace-compaction", "workspace-host"]
    assert all("shell" not in entry["name"] and "tool-fs" not in entry["name"] for entry in entries)
    prompt = next(entry for entry in entries if entry["id"] == "system-prompt")["config"]
    assert prompt["personaPrefix"] == arguments["system_prompt"]
    assert prompt["includeRuntimeContext"] is False
    provider = next(entry for entry in entries if entry["id"] == "llm-deepseek")["config"]
    assert provider["baseURL"] == "http://127.0.0.1:9/v1"
    assert next(entry for entry in entries if entry["id"] == "workspace-host")["config"]["usageEndpoint"] == "http://127.0.0.1:9/v1/messages"
    assert provider["apiKeyEnv"] == "WORKSPACE_MODEL_API_KEY"
    assert provider["models"][0]["inputModalities"] == ["text", "image"]
    assert provider["retryPolicy"] == {"mode": "normal", "maxRetries": 3,
        "retryableCodes": ["TRANSPORT", "RATE_LIMIT", "SERVER", "TIMEOUT"],
        "backoff": {"initialDelayMs": 2000, "maxDelayMs": 30000, "jitterRatio": 0.1}}
    assert next(entry for entry in entries if entry["id"] == "workspace-host")["config"]["requestRateLimitRetries"] == 3
    assert next(entry for entry in entries if entry["id"] == "workspace-compaction")["config"]["auto"] is True
    attachments = next(entry for entry in entries if entry["id"] == "attachments")["config"]
    assert attachments["dshHome"] == str(arguments["profile_root"])
    assert attachments["maxImagesPerMessage"] == 20
    assert attachments["maxImageBytes"] == 5 * 1024 * 1024
    assert attachments["imageCompressionConcurrency"] == 1
    assert result["args"] == ["--profile", "workspace"]
    assert result["initialize"]["approvalRequests"] is True
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "cordis.patch.yml").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("name", [
    "Bash", "Read", "plugin_install", "mcp__workspace__knowledge_search", "working_directory",
    "mcp__c123__search", "external_" + "a" * 31, "external_" + "a" * 33,
    "external_" + "A" * 32, "external_" + "g" * 32, EXTERNAL_NAME + "\n",
])
def test_unreviewed_tools_are_rejected_before_writing(arguments, name):
    arguments["tools"][0]["name"] = name
    with pytest.raises(ValueError, match="allowlist"):
        write_profile(**arguments)
    assert not arguments["profile_root"].exists()


@pytest.mark.parametrize("field", ["apiKey", "authToken", "headers", "plugins"])
def test_profile_cannot_persist_credentials_or_inject_plugins(arguments, field):
    arguments["provider"][field] = "PRIVATE_TEST_MARKER"
    with pytest.raises(ValueError, match="credentials"):
        write_profile(**arguments)
    assert not arguments["profile_root"].exists()


@pytest.mark.parametrize("path", ["home-env", "home-patch", "workspace-env"])
def test_ambient_configuration_is_refused(arguments, path):
    if path == "workspace-env":
        file = arguments["cwd"] / ".env"
    else:
        arguments["profile_root"].mkdir()
        file = arguments["profile_root"] / (".env" if path == "home-env" else "cordis.patch.yml")
    file.write_text("PRIVATE_TEST_MARKER")
    with pytest.raises(ValueError, match="ambient"):
        write_profile(**arguments)
    assert file.read_text() == "PRIVATE_TEST_MARKER"


def test_all_six_workspace_tool_schemas_remain_host_owned(arguments):
    arguments["tools"] = [{"name": name, "description": name, "input_schema": {"type": "object"}}
                          for name in sorted(TOOL_NAMES)]
    result = write_profile(**arguments)
    entries = json.loads((Path(result["profile_dir"]) / "cordis.patch.yml").read_text())[0]["insert"]
    assert entries[-1]["config"]["tools"] == arguments["tools"]
    assert entries[-1]["config"]["sessionId"] == arguments["session_id"]


def test_external_alias_keeps_local_references_without_native_mcp(arguments):
    arguments["tools"].append({"name": EXTERNAL_NAME, "description": "Search the selected business source",
                               "input_schema": EXTERNAL_SCHEMA})
    result = write_profile(**arguments)
    entries = json.loads((Path(result["profile_dir"]) / "cordis.patch.yml").read_text())[0]["insert"]
    assert entries[-1]["config"]["tools"] == arguments["tools"]
    assert len(EXTERNAL_NAME) == 41
    # Test module composition, not incidental names of the temporary directory.
    assert [entry["id"] for entry in entries] == [name for name, _ in KERNEL_MODULES] + ["workspace-compaction", "workspace-host"]


@pytest.mark.parametrize("field,value", [
    ("$ref", "https://example.invalid/schema"), ("$ref", "file:///etc/passwd"),
    ("$ref", "relative.json#/$defs/id"), ("$ref", 1),
    ("$dynamicRef", "https://example.invalid/schema#anchor"),
    ("$recursiveRef", "external.json#"), ("$id", "https://example.invalid/base"),
])
def test_external_schema_cannot_resolve_remote_resources(arguments, field, value):
    arguments["tools"] = [{"name": EXTERNAL_NAME, "description": "Synthetic remote tool",
                           "input_schema": {"type": "object", "properties": {"id": {field: value}}}}]
    with pytest.raises(ValueError, match="reference"):
        write_profile(**arguments)
    assert not arguments["profile_root"].exists()


@pytest.mark.parametrize("field", ["url", "headers", "connection_id", "canonical_name", "execute"])
def test_external_metadata_cannot_supply_routing_or_code(arguments, field):
    arguments["tools"] = [{"name": EXTERNAL_NAME, "description": "Synthetic remote tool",
                           "input_schema": EXTERNAL_SCHEMA, field: "PRIVATE_TEST_MARKER"}]
    with pytest.raises(ValueError, match="definition requires"):
        write_profile(**arguments)
    assert not arguments["profile_root"].exists()


def test_external_schemas_and_definitions_are_bounded(arguments):
    schema = {"type": "object"}
    for _ in range(65):
        schema = {"type": "object", "properties": {"child": schema}}
    arguments["tools"] = [{"name": EXTERNAL_NAME, "description": "Synthetic remote tool", "input_schema": schema}]
    with pytest.raises(ValueError, match="depth"):
        write_profile(**arguments)
    arguments["tools"][0]["input_schema"] = {"type": "object"}
    arguments["tools"][0]["description"] = "x" * MAX_TOOL_DEFINITION_BYTES
    with pytest.raises(ValueError, match="size"):
        write_profile(**arguments)
    assert not arguments["profile_root"].exists()


def test_external_aliases_cannot_be_duplicated(arguments):
    tool = {"name": EXTERNAL_NAME, "description": "Synthetic remote tool", "input_schema": EXTERNAL_SCHEMA}
    arguments["tools"] = [tool, tool]
    with pytest.raises(ValueError, match="duplicated"):
        write_profile(**arguments)


@pytest.mark.parametrize("value", [0, -1, True, 1001])
def test_steps_require_a_bounded_positive_integer(arguments, value):
    arguments["max_steps"] = value
    with pytest.raises(ValueError, match="max_steps"):
        write_profile(**arguments)


@pytest.mark.parametrize("effort", [None, "medium", "xhigh"])
def test_reasoning_effort_never_silently_changes(arguments, effort):
    arguments["provider"]["reasoningEffort"] = effort
    with pytest.raises(ValueError, match="reasoning effort"):
        write_profile(**arguments)


@pytest.fixture
def plugin_runner(tmp_path):
    """Run the actual plugin with a memory transport, without booting Harness.

    Fake modules expose only the upstream interfaces the plugin consumes. The
    real Node/Harness composition and provider are covered separately by the
    live integration tests; these cases isolate the plugin's own guards.
    """
    node = Path(os.getenv("HARNESS_TEST_NODE", str(REPOSITORY / ".runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node")))
    if not node.is_file():
        pytest.skip("Plugin guard checks require the isolated Node 24 executable")
    source = tmp_path / "fake-upstream"
    modules = {
        "packages/sdk/protocol/src/index.ts": """
export class JsonRpcLineTransport {
  constructor() { globalThis.transport = this; this.notifications = []; this.requests = [] }
  onRequest(callback) { this.handle = callback }
  start() {}
  close() {}
  async flush() {}
  notify(method, params) { this.notifications.push({method, params}) }
  request(method, params, signal) {
    this.requests.push({method, params, signal})
    if (!globalThis.blockHost) return Promise.resolve(globalThis.hostResult)
    return new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), {once:true})
    })
  }
}
""",
        "packages/sdk/server/src/server.ts": """
export class HarnessSdkJsonRpcServer {
  async handleRequest(method, params) { return {method} }
  async shutdown() { return {} }
}
""",
        "packages/llm/llm/src/message.ts": """
export function createUserMessage(input) { return {id:'fixture-message', ...input} }
""",
        "packages/core/tools/src/json-schema.ts": """
export function assertObjectJsonSchema(schema) {
  if (schema.type !== 'object') throw new Error('invalid schema')
  if (schema.$defs || schema.$ref) throw new Error('upstream subset rejects references')
}
export function validateJsonSchemaValue() {
  globalThis.inputValidationCount = (globalThis.inputValidationCount ?? 0) + 1
  return []
}
""",
    }
    source.mkdir()
    (source / "package.json").write_text('{"type":"module"}')
    for name, contents in modules.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    setup = """
import assert from 'node:assert/strict'
import { pathToFileURL } from 'node:url'
const { apply } = await import(pathToFileURL(process.argv[2]).href)
const events = new Map(), handlers = new Map(), tools = [], guards = []
const agent = {session:{id:'only-session'},status:'idle'}
const ctx = {
  tools: { register: tool => tools.push(tool), definitions: () => tools, guard: guard => guards.push(guard) },
  // The first registration keeps the historic single-handler test contract;
  // `handlers` preserves the real ordered middleware chain for Goal tests.
  on: (name, handler) => {
    if (!events.has(name)) events.set(name, handler)
    const list = handlers.get(name) ?? []
    list.push(handler)
    handlers.set(name, list)
  },
  agents: new Map([['only-session', agent]]),
  goals: { get: () => globalThis.goalView, pause: () => undefined, resume: () => undefined },
  effect: callback => callback(), loader: { await: async () => {} },
  root: { fiber: { dispose: async () => {} } },
}
const config = {
  sourceDir: process.argv[3], sourceCommit: 'fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159',
  sessionId: 'only-session', cwd: process.cwd(), model: 'fixture', maxSteps: 1, toolTimeoutMs: 1000,
  usageEndpoint: 'http://127.0.0.1:9/v1/messages',
  tools: [{ name:'knowledge_search', description:'synthetic', input_schema:{type:'object'} }],
}
globalThis.hostResult = {content:[{type:'text',text:'authorized result'}]}
globalThis.handlers = handlers
"""

    def run(body, *, configure="", start=True):
        script = tmp_path / "plugin-check.mjs"
        initialize = """
await apply(ctx, config)
await transport.handle('initialize', {cwd:config.cwd, provider:'deepseek-official', model:'fixture', approvalRequests:true})
""" if start else ""
        script.write_text(setup + configure + initialize + body + "\nprocess.stdout.write('PASS\\n')\n")
        result = subprocess.run(
            [str(node), str(script), str(REPOSITORY / "server/harness/workspace_plugin.ts"), str(source)],
            cwd=tmp_path, capture_output=True, text=True, timeout=15,
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "PASS\n"
    return run


def test_plugin_max_steps_one_allows_first_model_step_only(plugin_runner):
    plugin_runner("""
let entered = 0
const next = async () => { entered++; return {kind:'enter'} }
await events.get('agent/pre-step')({agent, step:1}, next)
await assert.rejects(events.get('agent/pre-step')({agent, step:2}, next), /step limit/)
assert.equal(entered, 1)
assert.deepEqual(transport.notifications.at(-1), {
  method:'workspace.limit', params:{sessionId:'only-session',code:'MAX_STEPS'},
})
""")


def test_plugin_stream_reports_missing_finish_and_abandonment(plugin_runner):
    plugin_runner("""
const emit = frame => events.get('agent/assistant-stream')({agent,frame})
emit({type:'start',attemptId:'unfinished',revision:1,turn:1,step:1})
emit({type:'chunk',attemptId:'unfinished',revision:2,chunk:{type:'text-delta',text:'partial'}})
emit({type:'end',attemptId:'unfinished',revision:3,outcome:{kind:'committed',eventType:'assistant/message',seq:4}})
assert.equal(transport.notifications.at(-1).params.finishObserved, false)
assert.equal(transport.notifications.at(-2).params.text, 'partial')
for (const reason of ['stop','tool-calls']) {
  emit({type:'start',attemptId:reason,revision:4,turn:1,step:1})
  emit({type:'chunk',attemptId:reason,revision:5,chunk:{type:'finish',reason:{kind:reason}}})
  emit({type:'end',attemptId:reason,revision:6,outcome:{kind:'committed',eventType:'assistant/message',seq:7}})
  assert.equal(transport.notifications.at(-1).params.finishObserved, true)
}
emit({type:'start',attemptId:'aborted',revision:7,turn:2,step:1})
emit({type:'end',attemptId:'aborted',revision:8,outcome:{kind:'abandoned'}})
assert.equal(transport.notifications.at(-1).params.outcome.kind, 'abandoned')
assert.equal(transport.notifications.at(-1).params.finishObserved, false)
""")


def test_plugin_passes_complete_arguments_and_trusted_session_to_host(plugin_runner):
    plugin_runner("""
const args = {query:'synthetic full argument',limit:3}
const result = await tools[0].execute(args, {agent,callId:'call-1',signal:new AbortController().signal})
assert.deepEqual(result, {text:'authorized result'})
assert.deepEqual(transport.requests[0].params, {
  schema:'workspace-tool/v1',sessionId:'only-session',callId:'call-1',toolName:'knowledge_search',arguments:args,
})
assert.equal(transport.requests[0].method, 'workspace/tool')
assert.equal(guards[0]({agent,name:'Bash'}), 'Tool is outside the locked workspace capability set')
await assert.rejects(tools[0].execute(args, {agent:{session:{id:'foreign'}},callId:'call-2',signal:new AbortController().signal}), /not authorized/)
assert.equal(transport.requests.length, 1)
""")


def test_plugin_cancel_notifies_host_and_rejects_result(plugin_runner):
    plugin_runner("""
globalThis.blockHost = true
const abort = new AbortController()
const pending = tools[0].execute({query:'cancel'}, {agent,callId:'cancel-1',signal:abort.signal})
abort.abort(new Error('cancelled'))
await assert.rejects(pending, /cancelled/)
assert.deepEqual(transport.notifications.at(-1), {
  method:'workspace.tool_cancelled',params:{sessionId:'only-session',callId:'cancel-1'},
})
""")


def test_plugin_rejects_commands_foreign_sessions_and_unvalidated_images(plugin_runner):
    plugin_runner("""
await assert.rejects(transport.handle('session/query',{sessionId:'only-session',contentBlocks:[{type:'text',text:'/permission danger-full-access'}]}), /unauthorized/)
await assert.rejects(transport.handle('session/prompt',{sessionId:'foreign',contentBlocks:[{type:'text',text:'hello'}]}), /unauthorized/)
await assert.rejects(transport.handle('session/prompt',{sessionId:'only-session',contentBlocks:[{type:'image',data:'secret'}]}), /literal text/)
const result = await transport.handle('session/prompt',{sessionId:'only-session',contentBlocks:[{type:'text',text:'/permission literal'}]})
assert.equal(result.method,'session/prompt')
""")


def test_plugin_allows_only_bounded_encoded_images_not_host_paths(plugin_runner):
    plugin_runner("""
const image = {type:'image', data:'YWJj', mimeType:'image/png', name:'fixture.png'}
const result = await transport.handle('session/prompt', {sessionId:'only-session',contentBlocks:[image]})
assert.equal(result.method,'session/prompt')
for (const block of [
  {...image, path:'/etc/passwd'}, {...image, uri:'file:///etc/passwd'},
  {type:'image',url:'https://example.invalid/image.png'}, {...image,mimeType:'image/svg+xml'},
  {...image,data:'YR=='}, {...image,data:'YWJj\\n'}, {...image,name:'../secret'},
  {...image,name:'bad\\\\name'}, {...image,data:''},
]) await assert.rejects(transport.handle('session/prompt',{sessionId:'only-session',contentBlocks:[block]}), /encoded raster/)
assert.equal((await transport.handle('session/prompt',{sessionId:'only-session',contentBlocks:Array(20).fill(image)})).method,'session/prompt')
await assert.rejects(transport.handle('session/prompt',{sessionId:'only-session',contentBlocks:Array(21).fill(image)}), /encoded raster/)
""")


def test_plugin_canonical_base64_at_five_mib_does_not_overflow_regex_stack(plugin_runner):
    plugin_runner("""
const image = {type:'image', data:Buffer.alloc(5*1024*1024).toString('base64'), mimeType:'image/png'}
const result = await transport.handle('session/prompt',{sessionId:'only-session',contentBlocks:[image]})
assert.equal(result.method,'session/prompt')
const oversized = {...image,data:Buffer.alloc(5*1024*1024+1).toString('base64')}
await assert.rejects(transport.handle('session/prompt',{sessionId:'only-session',contentBlocks:[oversized]}), /encoded raster/)
""")


def test_plugin_forwards_external_alias_schema_and_complete_arguments(plugin_runner):
    tool = {"name": EXTERNAL_NAME, "description": "Selected business source", "input_schema": EXTERNAL_SCHEMA}
    plugin_runner("""
assert.deepEqual(tools[0].parameters, config.tools[0].input_schema)
const args = {id:'authorized-record',nested:{items:[1,'two']}}
const result = await tools[0].execute(args,{agent,callId:'external-1',signal:new AbortController().signal})
assert.deepEqual(result,{text:'authorized result'})
assert.deepEqual(transport.requests[0].params, {
  schema:'workspace-tool/v1',sessionId:'only-session',callId:'external-1',toolName:config.tools[0].name,arguments:args,
})
assert.equal(globalThis.inputValidationCount ?? 0, 0)
assert.equal(guards[0]({agent,name:config.tools[0].name}), undefined)
assert.match(guards[0]({agent,name:'external_' + 'f'.repeat(32)}), /outside/)
assert.match(guards[0]({agent,name:'mcp__c123__search'}), /outside/)
for (const invalid of [[],null,'wrong']) {
  await assert.rejects(tools[0].execute(invalid,{agent,callId:'invalid',signal:new AbortController().signal}), /arguments/)
}
assert.equal(transport.requests.length,1)
globalThis.hostResult = {is_error:true,content:[{type:'text',text:'Host schema validation failed'}]}
await assert.rejects(tools[0].execute({id:7},{agent,callId:'schema-rejected',signal:new AbortController().signal}), /Host schema validation failed/)
""", configure="config.tools = " + json.dumps([tool]) + "\n")


@pytest.mark.parametrize("change,error", [
    ("config.tools[0].name = 'mcp__c123__read'", "Unreviewed"),
    ("config.tools[0].name += '\\n'", "Unreviewed"),
    ("config.tools.push(config.tools[0])", "duplicate"),
    ("config.tools[0].url = 'https://example.invalid/mcp'", "metadata"),
    ("config.tools[0].input_schema.properties.id.$ref = 'https://example.invalid/schema'", "references"),
    ("config.tools[0].input_schema.$id = 'https://example.invalid/base'", "reference base"),
    ("config.tools[0].input_schema.$defs = {secret:{$dynamicRef:'file:///etc/passwd'}}", "references"),
    ("config.tools[0].description = 'x'.repeat(524288)", "metadata"),
    ("for (let i=0;i<65;i++) config.tools[0].input_schema = {type:'object',properties:{child:config.tools[0].input_schema}}", "depth"),
])
def test_plugin_independently_rejects_untrusted_external_metadata(plugin_runner, change, error):
    tool = {"name": EXTERNAL_NAME, "description": "Selected business source", "input_schema": EXTERNAL_SCHEMA}
    plugin_runner(f"await assert.rejects(apply(ctx,config), /{error}/)\n",
                  configure="config.tools = " + json.dumps([tool]) + "\n" + change + "\n", start=False)


def test_plugin_refuses_unexpected_registry_entries_before_initialization(plugin_runner):
    plugin_runner("""
await apply(ctx,config)
tools.push({name:'external_'+'f'.repeat(32)})
await assert.rejects(transport.handle('initialize', {
  cwd:config.cwd,provider:'deepseek-official',model:'fixture',approvalRequests:true,
}), /Unexpected tools/)
""", start=False)


def test_official_native_schema_projection_preserves_local_references(tmp_path):
    """Inspect pinned official helpers without starting a model or application."""
    node = Path(os.getenv("HARNESS_TEST_NODE", str(REPOSITORY / ".runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node")))
    source = Path(os.getenv("HARNESS_TEST_SOURCE", str(REPOSITORY / ".runtime/third_party/deepseek-harness")))
    tsx = source / "node_modules/tsx/dist/esm/index.mjs"
    if not node.is_file() or not tsx.is_file():
        pytest.skip("Official schema projection check requires the pinned source dependencies")
    script = tmp_path / "official-schema-check.mjs"
    script.write_text("""
import assert from 'node:assert/strict'
import {pathToFileURL} from 'node:url'
const base = process.argv[2]
const {assertObjectJsonSchema} = await import(pathToFileURL(base+'/packages/core/tools/src/json-schema.ts').href)
const {projectToolSchema} = await import(pathToFileURL(base+'/packages/core/tools/src/schema.ts').href)
const definition = """ + json.dumps({"name": EXTERNAL_NAME, "description": "Synthetic schema", "parameters": EXTERNAL_SCHEMA}) + """
assert.throws(() => assertObjectJsonSchema(definition.parameters), /supported keyword/)
for (const detach of [false,true]) {
  assert.deepEqual(projectToolSchema(definition,detach),definition)
}
process.stdout.write('PASS\\n')
""")
    result = subprocess.run(
        [str(node), "--import", str(tsx), str(script), str(source)],
        cwd=tmp_path, capture_output=True, text=True, timeout=20,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TSX_TSCONFIG_PATH": str(source / "tsconfig.json")},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "PASS\n"


def goal_round_config(session_id="harness-session-1", **overrides):
    value = {
        "goal_id": "goal-00000000-0000-4000-8000-000000000001",
        "pre_arm_revision": 3, "expected_goal_revision": 5,
        "arm_action": "pause_resume", "round_index": 1,
        "native_rounds_before": 0, "max_goal_rounds": 8,
        "objective_sha256": "a" * 64, "lease_id": "lease-0001",
        "run_id": "run-0001", "session_id": session_id,
    }
    value.update(overrides)
    return value


def goal_round_fixture(arguments):
    module = arguments["source_dir"] / "packages/goal/goal-round-driver/src/index.ts"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("// module path fixture; never executed\n")
    return module


def test_goal_round_profile_mounts_the_official_driver_before_startup(arguments):
    goal_round_fixture(arguments)
    result = write_profile(**arguments, goal_execution=goal_round_config())
    entries = json.loads((Path(result["profile_dir"]) / "cordis.patch.yml").read_text())[0]["insert"]
    identifiers = [entry["id"] for entry in entries]
    assert identifiers.count("goal-round-driver") == 1
    assert identifiers.index("goal-round-driver") < identifiers.index("sdk-app-startup")
    assert identifiers.index("goal-round-driver") > identifiers.index("goals")
    driver = next(entry for entry in entries if entry["id"] == "goal-round-driver")
    assert driver["name"].endswith("packages/goal/goal-round-driver/src/index.ts")
    host = next(entry for entry in entries if entry["id"] == "workspace-host")["config"]
    assert host["goalRound"] == {
        "goalId": "goal-00000000-0000-4000-8000-000000000001",
        "preArmRevision": 3, "expectedRevision": 5, "armAction": "pause_resume",
        "roundIndex": 1, "nativeRoundsBefore": 0, "maxGoalRounds": 8,
        "objectiveSha256": "a" * 64, "leaseId": "lease-0001", "runId": "run-0001",
    }
    # The goal text never crosses the profile boundary, only its digest.
    assert "synthetic continuation objective" not in json.dumps(entries)


def test_goal_round_profile_is_absent_in_an_ordinary_run(arguments):
    result = write_profile(**arguments)
    entries = json.loads((Path(result["profile_dir"]) / "cordis.patch.yml").read_text())[0]["insert"]
    assert "goal-round-driver" not in [entry["id"] for entry in entries]
    assert "goalRound" not in next(entry for entry in entries if entry["id"] == "workspace-host")["config"]


def test_goal_round_resume_action_uses_one_revision_edge(arguments):
    goal_round_fixture(arguments)
    result = write_profile(**arguments, goal_execution=goal_round_config(
        arm_action="resume", pre_arm_revision=4, expected_goal_revision=5))
    entries = json.loads((Path(result["profile_dir"]) / "cordis.patch.yml").read_text())[0]["insert"]
    assert next(entry for entry in entries if entry["id"] == "workspace-host")["config"]["goalRound"]["armAction"] == "resume"


@pytest.mark.parametrize("change", [
    {"expected_goal_revision": 4},
    {"expected_goal_revision": 6},
    {"arm_action": "disarm"},
    {"objective_sha256": "not-a-digest"},
    {"objective_sha256": "a" * 63},
    {"goal_id": "goal-not-a-uuid"},
    {"round_index": 0}, {"round_index": 33},
    {"native_rounds_before": 8},
    {"max_goal_rounds": True},
    {"lease_id": "has space"}, {"run_id": ""},
    {"session_id": "other-session"},
    {"objective": "plain text must never enter the profile"},
])
def test_goal_round_profile_rejects_an_inconsistent_reservation(arguments, change):
    goal_round_fixture(arguments)
    with pytest.raises(ValueError):
        write_profile(**arguments, goal_execution=goal_round_config(**change))
    assert not arguments["profile_root"].exists()


def test_goal_round_profile_requires_the_pinned_driver_module(arguments):
    with pytest.raises(ValueError, match="goal/goal-round-driver"):
        write_profile(**arguments, goal_execution=goal_round_config())
    # The private home may already exist, but no executable profile was written.
    assert not (arguments["profile_root"] / "profiles/workspace/cordis.patch.yml").exists()


GOAL_ROUND_ID = "goal-" + "a" * 36
GOAL_OBJECTIVE = "fixture objective"


def goal_round_plugin_config():
    return {
        "goalId": GOAL_ROUND_ID, "preArmRevision": 3, "expectedRevision": 5,
        "armAction": "pause_resume", "roundIndex": 1, "nativeRoundsBefore": 0,
        "maxGoalRounds": 8,
        "objectiveSha256": hashlib.sha256(GOAL_OBJECTIVE.encode()).hexdigest(),
        "leaseId": "lease-1", "runId": "run-1",
    }


GOAL_VIEW_JS = (
    "globalThis.goalView={id:'" + GOAL_ROUND_ID + "',revision:3,roundsStarted:0,"
    "maxGoalRounds:8,phase:'active',activation:'disarmed',objective:'" + GOAL_OBJECTIVE + "'}\n"
)


def test_plugin_arms_only_the_reserved_native_revision(plugin_runner):
    plugin_runner(GOAL_VIEW_JS + """
globalThis.pauses=0;globalThis.resumes=0
ctx.goals.pause=(agent,ref)=>{globalThis.pauses+=1;globalThis.goalView={...globalThis.goalView,phase:'paused',revision:ref.revision+1};return globalThis.goalView}
ctx.goals.resume=(agent,ref)=>{globalThis.resumes+=1;globalThis.goalView={...globalThis.goalView,phase:'active',revision:ref.revision+1,activation:'armed'};return globalThis.goalView}
const receipt=await transport.handle('workspace/goal-round',{sessionId:'only-session'})
assert.equal(receipt.schema,'workspace-goal-round/v1')
assert.equal(receipt.goalId,'""" + GOAL_ROUND_ID + """')
assert.equal(receipt.revision,5)
assert.equal(receipt.roundsStarted,0)
assert.equal(receipt.round,1)
assert.equal(receipt.armed,true)
assert.equal(globalThis.pauses,1)
assert.equal(globalThis.resumes,1)
assert.equal(globalThis.goalView.revision,5)
await assert.rejects(transport.handle('workspace/goal-round',{sessionId:'only-session'}),/already prepared/)
""", configure="config.goalRound=" + json.dumps(goal_round_plugin_config()) + "\n")


def test_plugin_goal_pre_step_rejects_an_unarmed_goal_message(plugin_runner):
    plugin_runner(GOAL_VIEW_JS + """
const fence=globalThis.handlers.get('agent/pre-step')[1]
assert.equal(typeof fence,'function')
let entered=0
const next=async()=>{entered+=1;return{kind:'enter'}}
const message={source:{kind:'goal',goalId:'""" + GOAL_ROUND_ID + """',revision:5,round:1}}
assert.deepEqual(await fence({agent,messages:[message]},next),{kind:'reject'})
assert.equal(entered,0)
assert.equal(transport.notifications.at(-1).method,'workspace.goal_round')
assert.equal(transport.notifications.at(-1).params.code,'GOAL_ROUND_NOT_ARMED')
""", configure="config.goalRound=" + json.dumps(goal_round_plugin_config()) + "\n")


def test_plugin_goal_pre_step_requires_host_admission_and_one_round(plugin_runner):
    plugin_runner(GOAL_VIEW_JS + """
ctx.goals.pause=(agent,ref)=>{globalThis.goalView={...globalThis.goalView,phase:'paused',revision:ref.revision+1};return globalThis.goalView}
ctx.goals.resume=(agent,ref)=>{globalThis.goalView={...globalThis.goalView,phase:'active',revision:ref.revision+1,activation:'armed'};return globalThis.goalView}
await transport.handle('workspace/goal-round',{sessionId:'only-session'})
const fence=globalThis.handlers.get('agent/pre-step')[1]
assert.equal(typeof fence,'function')
const message={source:{kind:'goal',goalId:'""" + GOAL_ROUND_ID + """',revision:5,round:1}}
let entered=0
const next=async()=>{entered+=1;return{kind:'enter'}}
globalThis.hostResult={admitted:true,leaseId:'lease-1',round:1}
assert.deepEqual(await fence({agent,messages:[message]},next),{kind:'enter'})
assert.equal(entered,1)
assert.equal(transport.requests.at(-1).method,'workspace/goal-admission')
assert.deepEqual(transport.requests.at(-1).params,{
  schema:'workspace-goal-admission/v1',sessionId:'only-session',runId:'run-1',leaseId:'lease-1',
  goalId:'""" + GOAL_ROUND_ID + """',goalRevision:5,round:1,roundsStarted:0,maxGoalRounds:8,
  phase:'active',activation:'armed',
})
assert.equal(transport.notifications.at(-1).params.state,'admitted')
// A second goal round in the same host run is vetoed without another RPC.
const second={source:{kind:'goal',goalId:'""" + GOAL_ROUND_ID + """',revision:5,round:2}}
assert.deepEqual(await fence({agent,messages:[second]},next),{kind:'reject'})
assert.equal(entered,1)
assert.equal(transport.requests.length,1)
""", configure="config.goalRound=" + json.dumps(goal_round_plugin_config()) + "\n")


def test_team_profile_mounts_only_read_only_services(arguments):
    source=arguments["source_dir"]
    for package in ("subagent/subagent","team/agent-team"):
        path=source/"packages"/package/"src"/"index.ts"
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text("// test fixture")
    result=write_profile(**arguments,team_state=True)
    entries=json.loads((Path(result["profile_dir"])/"cordis.patch.yml").read_text())[0]["insert"]
    assert [entry["id"] for entry in entries]==[name for name,_ in KERNEL_MODULES]+[
        "team-subagent-service","team-state-service","workspace-compaction","workspace-host"]
    assert next(e for e in entries if e["id"]=="team-state-service")["config"]=={"defaultEnabled":False,"maxTasks":256}
    host=next(e for e in entries if e["id"]=="workspace-host")["config"]
    assert host["teamState"] is True
    assert host["tools"]==arguments["tools"]
    assert all("tool-subagent" not in e["name"] and "tool-agent-team" not in e["name"] and "spawn-in-process" not in e["name"] for e in entries)
