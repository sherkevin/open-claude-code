"""Real local MCP HTTP result through the actual plugin's model-text renderer.

The host transport, authorization, evidence registry and workspace_plugin.ts are
real. The plugin's upstream Harness interfaces use the existing isolated runner;
this does not boot pinned Harness, contact a model, or measure model tokens.
"""

import asyncio
import copy
import json

import pytest

from app.db import dump
from test_harness_mcp_tools import (
    SYNTHETIC_CREDENTIAL,
    alias,
    canonical,
    completed,
    mcp_server,  # noqa: F401 - imported pytest fixture used by system
    request as business_request,
    system,  # noqa: F401 - imported pytest fixture
)
from test_harness_profile import plugin_runner  # noqa: F401 - imported pytest fixture


@pytest.mark.parametrize("response_format", ["json", "sse"])
@pytest.mark.parametrize("echo_credential", [False, True])
def test_http_result_reaches_model_render_once_with_evidence(
    system, plugin_runner, record_property, response_format, echo_credential,
):
    system.server.response_format = response_format
    system.server.echo_credential = echo_credential
    request = business_request(system)
    result = asyncio.run(system.runtime.bridge.invoke(request))

    assert not result.get("is_error") and not result.get("isError"), result
    assert system.server.calls == [{"name": "read_records", "arguments": request["arguments"]}]
    assert all(system.server.authenticated)
    assert len(result["content"]) == 2, "One complete JSON block plus one citation block"
    structured = result["structuredContent"]
    payload, citations = [json.loads(block["text"]) for block in result["content"]]
    assert payload == structured
    assert payload["record"]["record_id"] == "P-900"
    assert payload["record"]["pressure"] == 7
    assert payload["sources"][0]["locator"] == {
        "database": "synthetic", "table": "pressure", "fields": ["record_id", "pressure"],
        "time_range": "2026-11-10", "timezone": "UTC", "queried_at": "2026-11-10T00:00:00Z",
    }
    assert payload["sources"][0]["excerpt"].startswith("P-900: 7 bar")

    sources = system.service.evidence.list(system.actor.current_run_id, system.owner)
    assert len(sources) == 1
    source_id = sources[0]["id"]
    citation = f"[[source:{source_id}]]"
    assert citations == [{"title": "Synthetic pressure record", "kind": "data", "citation": citation}]
    registered = system.service.db.one("SELECT * FROM evidence_sources WHERE id=?", (source_id,))
    assert registered["resource_id"] == system.connection["id"]
    assert registered["tool_name"] == "read_records"

    completion = completed(system)
    assert completion["success"] is True
    assert completion["source_ids"] == [source_id]
    assert completion["tool_name"] == canonical(system, "read_records")
    expected_facts = {
        "business_domain": "制造运营", "business_process": "库存异常分析",
        "business_object": "压力记录", "metrics": ["pressure"],
        "fields": ["record_id", "pressure"], "filters": {"record_id": "P-900"},
        "time_range": "2026-11-10", "aggregation": "latest", "row_count": 1,
        "page": 1, "page_size": 1, "returned_rows": 1, "original_rows": 1,
    }
    for key, value in expected_facts.items():
        assert structured[key] == value
        assert completion["result_facts"][key] == value
    assert completion["result_facts"]["cache_hit"] is False
    assert completion["result_facts"]["source_count"] == 1
    assert completion["result_facts"]["cache_key"] == "unknown"

    model_text = "\n".join(block["text"] for block in result["content"])
    # Reconstruct the previous host's unconditional append, before the bridge
    # adds citations. This is a same-payload counterfactual, not a model run.
    old_text = "\n".join([result["content"][0]["text"], dump(structured), result["content"][1]["text"]])
    old_bytes, current_bytes = len(old_text.encode("utf-8")), len(model_text.encode("utf-8"))
    assert old_bytes - current_bytes == len(dump(structured).encode("utf-8")) + 1
    assert old_bytes > current_bytes
    record_property("previous_model_text_utf8_bytes", old_bytes)
    record_property("current_model_text_utf8_bytes", current_bytes)
    record_property("measurement_unit", "UTF-8 bytes; not model tokens")
    print(f"model-text UTF-8 bytes ({response_format}, credential_echo={echo_credential}): "
          f"previous={old_bytes}, current={current_bytes}; not model tokens")

    definition = next(item for item in system.runtime.bridge.definitions()
                      if item["name"] == alias(system, "read_records"))
    before = copy.deepcopy(result)
    configure = (
        "config.tools = " + json.dumps([definition]) + "\n"
        "globalThis.hostResult = " + json.dumps(result) + "\n"
        "const expectedPayload = " + json.dumps(structured) + "\n"
        "const expectedCitations = " + json.dumps(citations) + "\n"
        "const expectedText = " + json.dumps(model_text) + "\n"
        "const oldText = " + json.dumps(old_text) + "\n"
        "const expectedBytes = " + str(current_bytes) + "\n"
        "const oldBytes = " + str(old_bytes) + "\n"
        "const secret = " + json.dumps(SYNTHETIC_CREDENTIAL) + "\n"
        "const args = " + json.dumps(request["arguments"]) + "\n"
    )
    plugin_runner(r"""
const before = structuredClone(globalThis.hostResult)
const output = await tools[0].execute(args, {
  agent, callId:'model-projection', signal:new AbortController().signal,
})
const rendered = tools[0].output.render(args, output)
assert.deepEqual(output, {text:expectedText})
assert.deepEqual(rendered, [{type:'text',text:expectedText}])
const blocks = rendered[0].text.split('\n').map(value => JSON.parse(value))
assert.equal(blocks.length, 2)
assert.deepEqual(blocks[0], expectedPayload)
assert.deepEqual(blocks[1], expectedCitations)
assert.equal(blocks.filter(value => value && !Array.isArray(value) && 'record' in value).length, 1)
assert.equal(rendered[0].text.split(expectedCitations[0].citation).length - 1, 1)
assert.equal(Buffer.byteLength(rendered[0].text, 'utf8'), expectedBytes)
assert.equal(Buffer.byteLength(oldText, 'utf8'), oldBytes)
assert.equal(oldText.split('\n').map(value => JSON.parse(value))
  .filter(value => value && !Array.isArray(value) && 'record' in value).length, 2)
assert.ok(!rendered[0].text.includes(secret))
assert.deepEqual(globalThis.hostResult, before)
assert.equal(transport.requests.length, 1)
assert.equal(transport.requests[0].method, 'workspace/tool')
assert.deepEqual(transport.requests[0].params, {
  schema:'workspace-tool/v1',sessionId:'only-session',callId:'model-projection',
  toolName:config.tools[0].name,arguments:args,
})
""", configure=configure)
    assert result == before
    assert completed(system) == completion
    assert system.service.evidence.list(system.actor.current_run_id, system.owner) == sources
    assert SYNTHETIC_CREDENTIAL not in dump(result)
    assert SYNTHETIC_CREDENTIAL not in dump(completion)
    assert SYNTHETIC_CREDENTIAL not in dump(sources)
