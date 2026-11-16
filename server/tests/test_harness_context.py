"""Read-only official context projection, with no external model requests."""
import asyncio
import json
import os
import subprocess
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import PROVIDER_KEYS, Settings
from app.context import cached_context, context_binding, create_context_router, fresh, harness_context_snapshot
from app.db import dump
from app.harness_runtime import HarnessRuntime, PINNED_COMMIT, _ContextResult
from app.main import create_app
from app.runtime import RunManager
from app.service import Service
from test_api import PASSWORD, login, send
from test_harness_profile import plugin_runner
from test_harness_real_runtime import wait_run


def wire(**changes):
    return {"schema": "workspace-context/v1", "sourceCommit": PINNED_COMMIT, "sessionId": "owned",
            "routeMatches": True, "projectionSeq": 10, "totalTokens": 123,
            "observedPromptTokens": None, "baselineKind": "estimated",
            "breakdown": {"systemTokens": 50, "toolsTokens": 70, "messageTokens": 8}, **changes}


@pytest.mark.parametrize("key,value", [
    ("totalTokens", True), ("totalTokens", -1), ("totalTokens", float("nan")),
    ("totalTokens", float("inf")), ("totalTokens", "123"), ("totalTokens", 1.5),
    ("totalTokens", 10**13), ("observedPromptTokens", False), ("projectionSeq", True),
    ("observedPromptTokens", 0), ("observedPromptTokens", 12), ("observedPromptTokens", 1000000),
    ("projectionSeq", -1), ("projectionSeq", 100_000), ("sourceCommit", "foreign"),
    ("schema", "untrusted"), ("baselineKind", "exact"), ("routeMatches", False),
    ("routeMatches", 1), ("totalTokens", None), ("breakdown", None),
    ("breakdown", {"systemTokens": -1, "toolsTokens": 0, "messageTokens": 1}),
    ("breakdown", {"systemTokens": 1, "toolsTokens": 0, "messageTokens": 1, "path": "/private"}),
    ("sessionId", "x" * 129), ("raw_session", {"secret": "forbidden"}),
])
def test_context_wire_rejects_malformed_or_unbound_measurements(key, value):
    with pytest.raises(ValidationError):
        _ContextResult.model_validate(wire(**{key: value}))


def test_projection_is_estimated_capacity_unknown_and_not_a_bill():
    value = harness_context_snapshot(_ContextResult.model_validate(wire(baselineKind="usage")).model_dump(by_alias=True))
    assert value["status"] == "partial" and value["precision"] == "estimated"
    assert value["total_tokens"] == 123 and value["observed_prompt_tokens"] is None
    assert sum(value["breakdown"].values()) != value["total_tokens"]
    assert value["max_tokens"] is value["percentage"] is value["observed_at"] is None
    assert value["manual_compact_available"] is value["auto_compact_enabled"] is False
    assert "sourceCommit" not in value and "sessionId" not in value


@pytest.mark.parametrize("matched", [True, False])
def test_unknown_projection_never_claims_initial_zero_is_observed(matched):
    raw = wire(routeMatches=matched, totalTokens=None, observedPromptTokens=None, baselineKind="none", breakdown=None)
    value = harness_context_snapshot(_ContextResult.model_validate(raw).model_dump(by_alias=True))
    assert value["total_tokens"] is value["observed_prompt_tokens"] is None
    assert value["stale"] and value["precision"] == "unknown"


@pytest.mark.parametrize("time", [None, "bad", "2026-01-01", "2999-01-01T00:00:00+00:00"])
def test_malformed_or_future_cache_time_is_not_fresh(time):
    assert not fresh({"status": "partial", "measured_at": time})


def test_errors_and_stale_results_never_suppress_retry():
    for extra in ({"error": "safe error"}, {"stale": True}, {"status": "unavailable"}):
        assert not fresh({"status": "partial", "measured_at": datetime.now(timezone.utc).isoformat(), **extra})


@pytest.fixture
def context_service(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(data_dir=tmp_path, runtime_type="harness", model="fixture", provider_settings="")
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "synthetic-only"}
    service = Service(settings)
    user = service.create_user({"username": "owner", "display_name": "Fixture", "password": PASSWORD, "role": "admin"})
    assistant = service.save_assistant({"name": "Context fixture"}, user=user)
    cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Context fixture"}, user)["id"]
    run, _ = service.prepare_run(cid, {"text": "synthetic", "file_ids": [], "client_request_id": "context"}, user)
    service.db.execute("UPDATE conversations SET harness_session_id='owned',active_run_id=NULL WHERE id=?", (cid,))
    service.db.execute("UPDATE runs SET status='succeeded' WHERE id=?", (run["run_id"],))
    config = service.runtime_config(cid)
    metadata = json.loads(service.require("runs", run["run_id"])["execution_meta"])
    metadata["context_binding"] = context_binding(config)
    service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump(metadata), run["run_id"]))
    try:
        yield service, cid, run["run_id"], config
    finally:
        service.db.close()


def context_runtime(fixture, raw=None):
    service, cid, _run, config = fixture
    runtime = HarnessRuntime(service, cid, config, None, None)
    calls = []

    def request(method, params, **kwargs):
        calls.append((method, params, kwargs))
        return _ContextResult.model_validate(raw or wire())

    runtime.client = SimpleNamespace(request=request)
    return runtime, calls


def test_context_rpc_is_bounded_and_cache_never_exposes_binding(context_service):
    runtime, calls = context_runtime(context_service)
    value = asyncio.run(runtime.context_usage())
    assert len(calls) == 1 and calls[0][0:2] == ("workspace/context", {"sessionId": "owned"})
    assert calls[0][2]["timeout_seconds"] == 15
    assert value["source"] == "harness_token_meter" and "_binding" not in value
    service, cid, run, _ = context_service
    service.db.execute("UPDATE runs SET usage=? WHERE id=?", (dump({"input_tokens": 99999999}), run))
    cached = cached_context(service, cid)
    assert cached["cached"] and cached["total_tokens"] == 123 and "_binding" not in cached
    assert "owned" not in dump(cached) and "synthetic-only" not in dump(cached)
    service.db.execute("UPDATE assistants SET revision=revision+1 WHERE id=(SELECT assistant_id FROM conversations WHERE id=?)", (cid,))
    stale = cached_context(service, cid)
    assert stale["stale_reason"] == "route_changed" and stale["total_tokens"] is None


@pytest.mark.parametrize("previous_observation", [0, 19])
def test_old_cached_normalized_usage_is_not_exposed_as_provider_observation(context_service, previous_observation):
    service, cid, *_ = context_service
    runtime, calls = context_runtime(context_service)
    asyncio.run(runtime.context_usage())
    stored = json.loads(service.require("conversations", cid)["context_usage"])
    stored["observed_prompt_tokens"] = previous_observation
    service.db.execute("UPDATE conversations SET context_usage=? WHERE id=?", (dump(stored), cid))
    value = cached_context(service, cid)
    assert value["observed_prompt_tokens"] is None
    assert value["cached"] and value["status"] == "partial" and value["total_tokens"] == 123
    assert len(calls) == 1
    assert json.loads(service.require("conversations", cid)["context_usage"])["observed_prompt_tokens"] == previous_observation


@pytest.mark.parametrize("change", ["receipt", "failed", "disabled"])
def test_cached_context_rechecks_receipt_terminal_and_authorization(context_service, change):
    service, cid, rid, _ = context_service
    runtime, _ = context_runtime(context_service)
    asyncio.run(runtime.context_usage())
    if change == "receipt":
        service.db.execute("UPDATE runs SET execution_meta='{}' WHERE id=?", (rid,))
    elif change == "failed":
        service.db.execute("UPDATE runs SET status='failed' WHERE id=?", (rid,))
    else:
        service.db.execute("UPDATE assistants SET enabled=0 WHERE id=(SELECT assistant_id FROM conversations WHERE id=?)", (cid,))
    value = cached_context(service, cid)
    assert value["total_tokens"] is None and not fresh(value)
    assert value["manual_compact_available"] is (change != "disabled")


@pytest.mark.parametrize("change", ["old_run", "failed_run", "system", "foreign_session", "same_model_endpoint"])
def test_unproven_binding_or_changed_config_cannot_relabel_old_statistics(context_service, change):
    service, cid, rid, _ = context_service
    runtime, calls = context_runtime(context_service)
    if change == "old_run":
        service.db.execute("UPDATE runs SET execution_meta='{}' WHERE id=?", (rid,))
    elif change == "failed_run":
        service.db.execute("UPDATE runs SET status='failed' WHERE id=?", (rid,))
    elif change == "system":
        service.db.execute("UPDATE assistants SET revision=revision+1 WHERE id=(SELECT assistant_id FROM conversations WHERE id=?)", (cid,))
    elif change == "same_model_endpoint":
        service.settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/v1"}
        service._save_model_selection(cid, service.catalog.select())
    else:
        runtime, calls = context_runtime(context_service, wire(sessionId="foreign"))
        with pytest.raises(Exception, match="会话校验"):
            asyncio.run(runtime.context_usage())
        return
    value = asyncio.run(runtime.context_usage())
    assert value["stale"] and value["total_tokens"] is None and not calls


def test_revoke_during_rpc_discards_measurement(context_service):
    service, cid, *_ = context_service
    runtime, _ = context_runtime(context_service)

    def revoke(*_args, **_kwargs):
        service.db.execute("UPDATE users SET enabled=0 WHERE id=(SELECT user_id FROM conversations WHERE id=?)", (cid,))
        return _ContextResult.model_validate(wire())

    runtime.client.request = revoke
    with pytest.raises(Exception, match="停用"):
        asyncio.run(runtime.context_usage())
    assert service.require("conversations", cid)["context_usage"] is None


def test_metadata_only_answer_does_not_invalidate_native_receipt(context_service):
    service, cid, *_ = context_service
    user = service.require("users", service.require("conversations", cid)["user_id"])
    extra, _ = service.prepare_run(cid, {"text": "你是什么模型", "file_ids": [], "client_request_id": "metadata-only"}, user)
    service.db.execute("UPDATE runs SET status='succeeded',execution_meta=? WHERE id=?", (dump({"execution_kind": "product_metadata", "model_call_count": 0}), extra["run_id"]))
    service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (cid,))
    runtime, calls = context_runtime(context_service)
    assert asyncio.run(runtime.context_usage())["status"] == "partial" and len(calls) == 1
    assert cached_context(service, cid)["status"] == "partial"


@pytest.mark.parametrize("fail", [False, True])
def test_actor_closes_statistics_process_and_errors_are_not_fresh(context_service, fail):
    service, cid, *_ = context_service
    closed, calls = [], []

    class Runtime:
        close_after_run = True
        def __init__(self, *_args):
            pass
        async def connect(self):
            calls.append("connect")
        async def context_usage(self):
            if fail:
                raise TimeoutError("private provider/path/credential detail")
            return harness_context_snapshot(wire())
        async def close(self):
            closed.append(True)

    async def scenario():
        manager = RunManager(service, runtime_factory=Runtime)
        service.manager = manager
        result = await manager.context(cid)
        assert manager.actors[cid].runtime is None and len(closed) == 1
        if fail:
            assert result["source"] == "harness_token_meter" and result["precision"] == "unknown"
            assert result["error"] and "private" not in dump(result) and not fresh(result)
            await manager.context(cid)
            assert len(calls) == 2
        await manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["assistant", "user"])
def test_http_refresh_rechecks_after_awaited_process_close(context_service, change):
    service, cid, *_ = context_service
    calls = []
    owner = service.require("users", service.require("conversations", cid)["user_id"])

    class Runtime(HarnessRuntime):
        async def connect(self):
            def request(method, params, **kwargs):
                calls.append(method)
                return _ContextResult.model_validate(wire())
            self.client = SimpleNamespace(request=request)

        async def close(self):
            await asyncio.sleep(0)
            if change == "assistant":
                service.db.execute("UPDATE assistants SET revision=revision+1 WHERE id=(SELECT assistant_id FROM conversations WHERE id=?)", (cid,))
            else:
                service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (owner["id"],))
            self.client = None

    service.manager = RunManager(service, runtime_factory=Runtime)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await service.manager.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(create_context_router(lambda: service, lambda: owner))
    with TestClient(app) as client:
        response = client.post(f"/api/v1/conversations/{cid}/context/refresh")
        assert response.status_code == 200
        value = response.json()
        assert value["total_tokens"] is value["observed_prompt_tokens"] is value["breakdown"] is None
        assert value["precision"] == "unknown" and not value["cached"]
        assert calls == ["workspace/context"]


def test_node_rpc_rejects_foreign_extra_or_uninitialized_requests(plugin_runner):
    configure = """
const selection = {provider:'deepseek-official',model:'fixture',reasoningEffort:'off'}
agent.session.seq = 11
agent.session.requestHeader = () => ({config:selection})
agent.model = {current: () => selection}
ctx.agents = {get: id => id === config.sessionId ? agent : undefined}
ctx.sessionProjections = {snapshot:(session,keys) => {
  assert.deepEqual(keys,['contextPressure','contextBreakdown'])
  return {asOfSeq:10,values:{contextPressure:{pressureTokens:12,projectedTokens:20,contextWindow:1000000},
    contextBreakdown:{systemTokens:50,toolsTokens:70,messageTokens:8}}}
}}
ctx.tokenMeter = {measure: session => ({logRevision:11,totalTokens:123,baseline:{kind:'usage',usage:{private:'forbidden'}},nodes:[{private:'forbidden'}]})}
"""
    plugin_runner("""
for (const params of [null,[],{}, {sessionId:'foreign'}, {sessionId:config.sessionId,prompt:'run'}, {sessionId:config.sessionId,keys:['tokenUsage'] }]) {
  await assert.rejects(transport.handle('workspace/context',params), /context request/)
}
const value = await transport.handle('workspace/context',{sessionId:config.sessionId})
assert.equal(value.totalTokens,123)
assert.equal(value.observedPromptTokens,null)
assert.equal(value.baselineKind,'usage')
assert.equal(value.projectionSeq,10)
assert.equal(JSON.stringify(value).includes('forbidden'),false)
assert.equal(JSON.stringify(value).includes('contextWindow'),false)
assert.equal(JSON.stringify(value).includes('tokenUsage'),false)
assert.equal(transport.requests.length,0)
agent.model.current = () => ({...selection,model:'new-model'})
const stale = await transport.handle('workspace/context',{sessionId:config.sessionId})
assert.equal(stale.routeMatches,false)
assert.equal(stale.totalTokens,null)
""", configure=configure)
    plugin_runner("""
await apply(ctx,config)
await assert.rejects(transport.handle('workspace/context',{sessionId:config.sessionId}), /context request/)
""", configure=configure, start=False)


@pytest.mark.parametrize("bad", ["true", "NaN", "Infinity", "-1", "1000000000001"])
def test_node_rejects_invalid_official_numbers(plugin_runner, bad):
    plugin_runner("""
await assert.rejects(transport.handle('workspace/context',{sessionId:config.sessionId}), /Invalid context/)
""", configure="""
agent.session.seq=11
const route={provider:'deepseek-official',model:'fixture'}
agent.session.requestHeader=()=>({config:route})
agent.model={current:()=>route}
ctx.agents={get:()=>agent}
ctx.sessionProjections={snapshot:()=>({asOfSeq:10,values:{contextPressure:{pressureTokens:12},contextBreakdown:{systemTokens:1,toolsTokens:1,messageTokens:1}}})}
ctx.tokenMeter={measure:()=>({logRevision:11,totalTokens:BAD,baseline:{kind:'estimated'}})}
""".replace("BAD", bad))


@pytest.mark.parametrize("change", ["agent.session.seq=100001", "agent.session.id='foreign'", "agent.session.seq=0"])
def test_node_refuses_unbounded_or_foreign_session_before_projection(plugin_runner, change):
    plugin_runner("""
await assert.rejects(transport.handle('workspace/context',{sessionId:config.sessionId}), /Context session unavailable/)
""", configure="""
agent.session.seq=11
ctx.agents={get:()=>agent}
ctx.sessionProjections={snapshot:()=>assert.fail('projection must not run')}
ctx.tokenMeter={measure:()=>assert.fail('measurement must not run')}
""" + change)


def test_official_translate_erases_missing_vs_reported_zero_presence(tmp_path):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    script = tmp_path / "usage-presence.mjs"
    script.write_text("""
import assert from 'node:assert/strict'
import {pathToFileURL} from 'node:url'
import net from 'node:net'
globalThis.fetch=async()=>assert.fail('network forbidden')
net.Socket.prototype.connect=function(){assert.fail('network forbidden')}
const {translate}=await import(pathToFileURL(process.argv[2]).href)
async function result(usage) {
  async function* events() {
    yield {type:'message_start',message:{usage}}
    yield {type:'content_block_start',index:0,content_block:{type:'text',text:''}}
    yield {type:'content_block_delta',index:0,delta:{type:'text_delta',text:'offline fixture'}}
    yield {type:'content_block_stop',index:0}
    yield {type:'message_delta',delta:{stop_reason:'end_turn'}}
    yield {type:'message_stop'}
  }
  const chunks=[]
  for await (const chunk of translate(events(),'synthetic-offline')) chunks.push(chunk)
  assert.equal(chunks.at(-1).type,'finish')
  return chunks.find(chunk=>chunk.type==='usage').usage
}
assert.deepEqual(await result({}),{inputTokens:0,outputTokens:0,totalTokens:0})
assert.deepEqual(await result({}),await result({input_tokens:0,output_tokens:0}))
assert.deepEqual(await result({input_tokens:12}),{inputTokens:12,outputTokens:0,totalTokens:12})
process.stdout.write('PASS\\n')
""")
    source = Path(source)
    result = subprocess.run([node, "--import", str(source / "node_modules/tsx/dist/esm/index.mjs"), str(script),
        str(source / "packages/llm/llm-deepseek/src/translate.ts")], cwd=tmp_path, capture_output=True, text=True,
        timeout=30, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
                        "XDG_CACHE_HOME": str(tmp_path / "cache"), "TSX_TSCONFIG_PATH": str(source / "tsconfig.json")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "PASS\n"


@pytest.fixture
def context_model_server(usage_fields):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            if self.path != "/v1/messages":
                self.send_error(404)
                return
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            start_usage = {} if usage_fields == "missing" else {"input_tokens": 12}
            if usage_fields == "complete":
                start_usage["output_tokens"] = 0
            events = [
                {"type": "message_start", "message": {"id": "fixture", "type": "message", "role": "assistant", "model": "fixture", "content": [], "usage": start_usage}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Synthetic context reply"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                 **({"usage": {"output_tokens": 8}} if usage_fields == "complete" else {})},
                {"type": "message_stop"},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in events:
                self.wfile.write((f"event: {event['type']}\ndata: " + json.dumps(event) + "\n\n").encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize("usage_fields", ["complete", "missing", "partial"])
def test_real_harness_context_restore_route_change_and_zero_extra_model_calls(tmp_path, monkeypatch, context_model_server, usage_fields, record_property):
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("requires explicitly installed pinned Harness runtime")
    for name in PROVIDER_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    base, requests = context_model_server
    catalog = tmp_path / "catalog.json"
    catalog.write_text(dump({"default_model_id": "fixture", "models": [
        {"id": name, "label": "Synthetic context " + name, "upstream_model": name,
         "reasoning_levels": {"default": None},
         "provider_env": {"ANTHROPIC_API_KEY": "synthetic-only", "ANTHROPIC_BASE_URL": base}}
        for name in ("fixture", "other-fixture")]}))
    settings = Settings(data_dir=tmp_path / "data", runtime_type="harness", provider_test_mode=True, harness_node_path=node, harness_source_dir=source,
        model_catalog=str(catalog), default_model_id="", provider_settings="", origins=("http://testserver",),
        model_router_enabled=False, knowledge_vision_enabled=False, image_generation_enabled=False)
    app = create_app(settings)
    original, processes = HarnessRuntime.connect, []

    async def connect(runtime):
        await original(runtime)
        processes.append(runtime.client._proc)

    monkeypatch.setattr(HarnessRuntime, "connect", connect)
    with TestClient(app) as client:
        service = app.state.service
        user = service.create_user({"username": "admin", "display_name": "Fixture", "password": PASSWORD, "role": "admin"})
        login(client)
        assistant = service.save_assistant({"name": "Context"}, user=user)
        cid = client.post("/api/v1/conversations", json={"assistant_id": assistant["id"]}).json()["id"]
        url = f"/api/v1/conversations/{cid}/context"
        empty = client.get(url).json()
        assert empty["source"] == "harness_token_meter" and not empty["manual_compact_available"]
        assert client.post(f"/api/v1/conversations/{cid}/compact", json={"client_request_id": "no-session"}).status_code == 409
        assert client.post(url + "/refresh").json()["total_tokens"] is None
        assert not requests and not processes
        rid = send(client, cid, "Literal context fixture", "context-first")["run_id"]
        assert wait_run(client, rid)["status"] == "succeeded"
        service.db.execute("UPDATE runs SET usage=? WHERE id=?", (dump({"input_tokens": 999999999}), rid))
        assert len(requests) == 1
        measured = client.post(url + "/refresh").json()
        assert measured["manual_compact_available"]
        assert measured["status"] == "partial", measured
        assert measured["precision"] == "estimated" and measured["total_tokens"] > 0
        assert measured["observed_prompt_tokens"] is measured["max_tokens"] is measured["percentage"] is None
        assert len(requests) == 1 and len(processes) == 2
        assert processes[-1].poll() is not None
        assert client.post(url + "/refresh").json()["cached"]
        assert len(processes) == 2 and len(requests) == 1
        stored = json.loads(service.require("conversations", cid)["context_usage"])
        stored["measured_at"] = (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        service.db.execute("UPDATE conversations SET context_usage=? WHERE id=?", (dump(stored), cid))
        assert client.get(url).json()["stale_reason"] == "snapshot_expired"
        restored = client.post(url + "/refresh").json()
        assert restored["total_tokens"] == measured["total_tokens"]
        assert len(processes) == 3 and len(requests) == 1
        service._save_model_selection(cid, service.catalog.select("other-fixture", "default"))
        changed = client.post(url + "/refresh").json()
        assert changed["stale_reason"] == "route_changed" and changed["total_tokens"] is None
        assert len(requests) == 1
        # A genuine SDK request timeout must reap the owned Node process, not
        # merely abandon an asyncio/to_thread waiter. The next refresh retries.
        service._save_model_selection(cid, service.catalog.select("fixture", "default"))
        original_context = HarnessRuntime.context_usage

        async def timed_out_context(runtime):
            original_request = runtime.client.request
            def request(method, params, **kwargs):
                if method == "workspace/context":
                    kwargs["timeout_seconds"] = 1e-9
                return original_request(method, params, **kwargs)
            runtime.client.request = request
            return await original_context(runtime)

        monkeypatch.setattr(HarnessRuntime, "context_usage", timed_out_context)
        failure = client.post(url + "/refresh").json()
        assert failure["status"] == "unavailable" and failure["total_tokens"] is None and failure["error"]
        assert processes[-1].poll() is not None and len(requests) == 1
        monkeypatch.setattr(HarnessRuntime, "context_usage", original_context)
        assert client.post(url + "/refresh").json()["status"] == "partial"
        assert len(requests) == 1
        service.create_user({"username": "outsider", "display_name": "Other", "password": PASSWORD, "role": "member"})
        login(client, "outsider")
        assert client.get(url).status_code == client.post(url + "/refresh").status_code == 404
        assert len(requests) == 1
    assert all(process.poll() is not None for process in processes)
    record_property("extra_provider_requests_for_context", 0)
    record_property("external_model_calls", 0)
    record_property("local_synthetic_model_requests", 1)
    record_property("reaped_processes", len(processes))
    record_property("provider_usage_fixture", usage_fields)
