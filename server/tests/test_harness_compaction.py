"""Manual Harness compaction contracts using no external model or customer data."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import dump
from app.harness_runtime import HarnessRuntime, _CompactExecution, _compaction_cost_usage, _event_compaction
from app.runtime import RunManager
from app.security import AppError
from app.service import Service
from test_api import PASSWORD


COMMAND_ID = "cmd-fixture-1"
COMPACTION_ID = "compaction-fixture-1"
SUMMARY_SEQ = 12


def notification(kind, data, seq):
    return SimpleNamespace(method="session.event", payload={
        "sessionId": "owned",
        "event": {"type": kind, "data": data, "seq": seq},
    })


def lifecycle(*, result="success", boundary=True, include_end=True, source=True, usage=None):
    command = {"commandId": COMMAND_ID, "name": "compact", "args": " preserve P-900 "}
    done = {
        "commandId": COMMAND_ID,
        "kind": result,
        "text": "已整理 2 项历史" if result == "success" else "压缩未完成",
        **({"sourceEventSeq": SUMMARY_SEQ} if result == "success" and boundary else {}),
    }
    frames = [notification("command/run", command, 8)]
    if boundary:
        common = {"compactionId": COMPACTION_ID, "turn": None}
        if source:
            common["sourceCommandId"] = COMMAND_ID
        frames.append(notification("compaction/start", common, 10))
        frames.append(notification("compaction/summary", {
            **common,
            "summary": [{"type": "text", "text": "Synthetic bounded summary"}],
            "shadowedRange": {"start": 3, "end": 7},
            "shadowedSeqs": [3, 7],
            "shadowedTokenCount": 120,
            "provider": "deepseek-official",
            "model": "fixture",
            "usage": usage or {"inputTokens": 9, "outputTokens": 4},
        }, SUMMARY_SEQ))
        if include_end:
            frames.append(notification("compaction/end", {
                **common,
                **({} if result == "success" else {"error": "synthetic commit failure"}),
            }, 13))
    frames.append(notification("command/done", done, 14))
    return frames


def test_compaction_event_validator_requires_correlated_complete_boundary():
    events = _event_compaction(lifecycle(), "owned", COMMAND_ID)
    assert events["summary_seq"] == SUMMARY_SEQ
    assert events["started"]["compactionId"] == COMPACTION_ID
    assert events["summary"]["shadowedSeqs"] == [3, 7]

    no_boundary = _event_compaction(lifecycle(boundary=False), "owned", COMMAND_ID)
    assert no_boundary["summary"] is None and no_boundary["started"] is None


@pytest.mark.parametrize("frames,error", [
    (lifecycle(source=False), "与命令不匹配"),
    (lifecycle(include_end=False), "缺少结束事件"),
    (lifecycle(usage={"inputTokens": -1, "outputTokens": 2}), "用量数据无效"),
    ([*lifecycle(), notification("compaction/end", {
        "compactionId": COMPACTION_ID, "sourceCommandId": COMMAND_ID, "turn": None,
    }, 15)], "结束事件重复"),
])
def test_compaction_event_validator_fails_closed(frames, error):
    with pytest.raises(AppError, match=error):
        _event_compaction(frames, "owned", COMMAND_ID)


@pytest.mark.parametrize("usage,expected", [
    ({"inputTokens": 9, "outputTokens": 4, "cacheReadTokens": 80, "cacheWriteTokens": 0,
      "reasoningTokens": 3, "totalTokens": 99999}, [9, 4, 80, 0]),
    ({"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}, [None, None, None, None]),
    ({"inputTokens": True, "outputTokens": -1, "cacheReadTokens": 0}, [None, None, 0, None]),
    (None, [None, None, None, None]),
])
def test_compaction_cost_preserves_missing_usage_and_does_not_double_count_reasoning(usage, expected):
    cost = _compaction_cost_usage(usage)
    assert [cost[key] for key in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')] == expected
    assert cost['estimated_cost_usd'] is None
    stage = cost['token_accounting']['stages']
    assert len(stage) == 1 and stage[0]['stage'] == 'compression'
    assert stage[0]['provider_call'] is True
    assert cost['token_accounting']['provider_usage_complete'] == all(value is not None for value in expected)


@pytest.fixture
def compaction_service(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    settings = Settings(data_dir=tmp_path, runtime_type="harness", model="fixture", provider_settings="")
    settings.provider_env = lambda: {"ANTHROPIC_API_KEY": "synthetic-only"}
    service = Service(settings)
    user = service.create_user({
        "username": "owner", "display_name": "Fixture", "password": PASSWORD, "role": "admin",
    })
    assistant = service.save_assistant({"name": "Compaction fixture"}, user=user)
    cid = service.create_conversation({"assistant_id": assistant["id"], "title": "Compaction"}, user)["id"]
    try:
        yield service, cid, user
    finally:
        service.db.close()


def prepare_compaction(service, cid, user, instructions="preserve P-900"):
    result, _ = service.prepare_run(cid, {
        "text": "/compact " + instructions,
        "file_ids": [],
        "client_request_id": "compact-" + instructions.replace(" ", "-"),
    }, user)
    run = service.require("runs", result["run_id"])
    metadata = json.loads(run.get("execution_meta") or "{}")
    metadata["compaction_instructions"] = instructions
    service.db.execute(
        "UPDATE runs SET kind='compact',status='running',execution_meta=? WHERE id=?",
        (dump(metadata), run["id"]),
    )
    # A first Harness run establishes the memory epoch and clears any session
    # created before that binding. Simulate the post-run native session only
    # after prepare_run has completed this lifecycle transition.
    service.db.execute("UPDATE conversations SET harness_session_id='owned' WHERE id=?", (cid,))
    return service.require("runs", run["id"]), service.runtime_config(cid)


class Subscription:
    def __init__(self, frames):
        self.frames = frames
        self.closed = False

    def drain(self, callback):
        for frame in self.frames:
            callback(frame)

    def close(self):
        self.closed = True


class Client:
    def __init__(self, frames=None, *, wait=False, fail=None):
        self.frames = frames or []
        self.wait = wait
        self.fail = fail
        self.release = threading.Event()
        self.started = threading.Event()
        self.closed = False
        self.requests = []
        self.subscription = Subscription(self.frames)

    def subscribe_session_notifications(self, session_id):
        assert session_id == "owned"
        return self.subscription

    def request(self, method, params, **kwargs):
        self.requests.append((method, params, kwargs))
        self.started.set()
        if self.wait:
            self.release.wait(5)
            raise TimeoutError("synthetic request released by process close")
        if self.fail:
            raise self.fail
        return _CompactExecution(commandId=COMMAND_ID, result={
            "kind": "success",
            "text": "已整理 2 项历史",
            "sourceEventSeq": SUMMARY_SEQ,
        })

    def close(self):
        self.closed = True
        self.release.set()


async def context_before(runtime):
    return {"status": "partial", "total_tokens": 240, "error": None}


def runtime_with(service, cid, config, emit, client):
    runtime = HarnessRuntime(service, cid, config, None, emit)
    runtime.client = client
    runtime._compact_before = lambda: context_before(runtime)
    return runtime


def test_harness_compaction_success_usage_and_focus(compaction_service):
    service, cid, user = compaction_service
    run, config = prepare_compaction(service, cid, user)
    client = Client(lifecycle())
    emitted = []

    async def emit(kind, data):
        emitted.append((kind, data))
        service.db.event(run["id"], kind, data)

    runtime = runtime_with(service, cid, config, emit, client)
    asyncio.run(runtime._execute_compact(run, {"text": "/compact preserve P-900"}))

    stored = service.run_view(service.require("runs", run["id"]))
    assert stored["compaction"]["status"] == "completed"
    assert stored["compaction"]["boundary_observed"] is True
    assert stored["compaction"]["shadowed_items"] == 2
    assert stored["compaction"]["pre_tokens"] == 240
    assert stored["usage"]["input_tokens"] == 9
    assert stored["usage"]["output_tokens"] == 4
    assert client.requests == [(
        "workspace/compact",
        {"sessionId": "owned", "instructions": "preserve P-900"},
        {"response_model": _CompactExecution, "timeout_seconds": 600},
    )]
    assert [kind for kind, _ in emitted] == [
        "context.compaction_started",
        "context.compacted",
        "usage.updated",
        "context.compaction_result",
    ]
    assert client.subscription.closed


@pytest.mark.parametrize("usage,expected_input", [
    ({"inputTokens": 9, "outputTokens": 4, "cacheReadTokens": 80, "cacheWriteTokens": 0}, 9),
    ({"inputTokens": 0, "outputTokens": 4}, None),
])
def test_compaction_keeps_bound_measurements_and_its_own_cost_receipt(compaction_service, usage, expected_input):
    service, cid, user = compaction_service
    run, config = prepare_compaction(service, cid, user)
    emitted = []

    async def emit(kind, data):
        emitted.append((kind, data))
        service.db.event(run["id"], kind, data)

    runtime = runtime_with(service, cid, config, emit, Client(lifecycle(usage=usage)))
    snapshots = iter([1000, 200])

    async def snapshot():
        return {"source": "harness_token_meter", "status": "partial", "precision": "estimated",
                "total_tokens": next(snapshots), "model": "fixture", "stale": False}

    runtime._compact_before = snapshot
    asyncio.run(runtime._execute_compact(run, {"text": "/compact"}))
    stored = service.run_view(service.require("runs", run["id"]))
    record = stored["compaction"]
    assert record["measurement_schema"] == "compaction-measurement/v1"
    assert record["before"]["_binding"] == record["after"]["_binding"]
    assert len(record["before"]["_binding"]) == 64
    assert record["before"]["total_tokens"] == 1000 and record["after"]["total_tokens"] == 200
    cost = record["cost_usage"]
    assert cost == stored["usage"]
    assert cost["input_tokens"] == expected_input
    assert cost["output_tokens"] == 4
    assert cost["estimated_cost_usd"] is None
    stage = cost["token_accounting"]["stages"]
    assert len(stage) == 1 and stage[0]["stage"] == "compression"
    assert stage[0]["provider_call"] is True
    if expected_input is None:
        assert cost["cache_read_input_tokens"] is None and cost["cache_creation_input_tokens"] is None
    assert len(runtime.client.requests) == 1


def test_harness_compaction_not_needed_never_fabricates_boundary(compaction_service):
    service, cid, user = compaction_service
    run, config = prepare_compaction(service, cid, user)
    client = Client(lifecycle(boundary=False))
    emitted = []

    async def emit(kind, data):
        emitted.append((kind, data))
        service.db.event(run["id"], kind, data)

    client.request = lambda method, params, **kwargs: _CompactExecution(
        commandId=COMMAND_ID,
        result={"kind": "success", "text": "没有可安全压缩的历史。"},
    )
    runtime = runtime_with(service, cid, config, emit, client)
    asyncio.run(runtime._execute_compact(run, {"text": "/compact preserve P-900"}))

    stored = service.run_view(service.require("runs", run["id"]))
    assert stored["compaction"]["status"] == "not_needed"
    assert stored["compaction"]["boundary_observed"] is False
    assert "context.compacted" not in [kind for kind, _ in emitted]
    assert stored["usage"] is None


def test_harness_compaction_command_error_is_not_reported_complete(compaction_service):
    service, cid, user = compaction_service
    run, config = prepare_compaction(service, cid, user)
    client = Client(lifecycle(boundary=False, result="error"))

    async def emit(kind, data):
        service.db.event(run["id"], kind, data)

    client.request = lambda method, params, **kwargs: _CompactExecution(
        commandId=COMMAND_ID,
        result={"kind": "error", "text": "当前任务或另一项压缩仍在执行，请完成后再试。"},
    )
    runtime = runtime_with(service, cid, config, emit, client)
    with pytest.raises(AppError, match="未完成") as error:
        asyncio.run(runtime._execute_compact(run, {"text": "/compact preserve P-900"}))
    assert error.value.code == "HARNESS_COMPACTION_FAILED"
    assert service.run_view(service.require("runs", run["id"]))["compaction"]["status"] == "started"


def test_harness_compaction_timeout_closes_owned_client_and_settles_request(compaction_service):
    service, cid, user = compaction_service
    run, config = prepare_compaction(service, cid, user)
    service.settings.harness_compaction_timeout_seconds = 0
    client = Client(wait=True)

    async def emit(kind, data):
        service.db.event(run["id"], kind, data)

    runtime = runtime_with(service, cid, config, emit, client)
    with pytest.raises(AppError, match="超时") as error:
        asyncio.run(runtime._execute_compact(run, {"text": "/compact preserve P-900"}))
    assert error.value.code == "HARNESS_COMPACTION_TIMEOUT"
    assert client.closed and client.subscription.closed


def test_harness_compaction_cancellation_closes_and_reaps_request(compaction_service):
    service, cid, user = compaction_service
    run, config = prepare_compaction(service, cid, user)
    client = Client(wait=True)

    async def emit(kind, data):
        service.db.event(run["id"], kind, data)

    runtime = runtime_with(service, cid, config, emit, client)

    async def scenario():
        task = asyncio.create_task(runtime._execute_compact(run, {"text": "/compact preserve P-900"}))
        assert await asyncio.to_thread(client.started.wait, 1)
        await runtime.interrupt()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.closed and client.subscription.closed


def test_compaction_boundary_survives_authorization_change(compaction_service):
    service, cid, user = compaction_service
    run, config = prepare_compaction(service, cid, user)
    client = Client(lifecycle())

    async def emit(kind, data):
        service.db.event(run["id"], kind, data)
        if kind == "context.compacted":
            service.db.execute(
                "UPDATE assistants SET revision=revision+1 WHERE id=(SELECT assistant_id FROM conversations WHERE id=?)",
                (cid,),
            )

    runtime = runtime_with(service, cid, config, emit, client)
    with pytest.raises(AppError) as error:
        asyncio.run(runtime._execute_compact(run, {"text": "/compact preserve P-900"}))
    assert error.value.code == "CONFIG_CHANGED"
    assert service.db.one(
        "SELECT id FROM events WHERE run_id=? AND type='context.compacted'", (run["id"],)
    )
    RunManager(service).finish(run["id"], "failed", "助手配置已变化")
    stored = service.run_view(service.require("runs", run["id"]))
    assert stored["status"] == "failed"
    assert stored["compaction"]["status"] == "completed"
    assert stored["compaction"]["followup_status"] == "failed"
