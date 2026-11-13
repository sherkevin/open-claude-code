import asyncio
import json
from types import MethodType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, now, uid
from app.harness_bridge import HarnessKnowledgeBridge
from app.main import create_app
from app.runtime import SDKRuntime
from app.security import AppError
from app.service import Service


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="test"))
    with TestClient(app):
        service = app.state.service
        owner = service.create_user({"username": "bridge", "display_name": "bridge", "password": "test-password-123", "role": "member"})
        kb = service.knowledge.save({"name": "Bridge fixture"}, owner)
        doc = service.knowledge.upload(kb["id"], owner, "pump.md", b"P-900 pressure limit is 7 bar.")
        assistant = service.save_assistant({"name": "Bridge", "knowledge_base_ids": [kb["id"]]}, user=owner)
        conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Bridge"}, user=owner)
        binding = service.memory.snapshot_for_run(owner["id"], conversation["id"])
        run_id = uid()
        service.db.execute(
            "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at,capability_snapshot,execution_meta) VALUES(?,?,?,?,?,'running',?,?,?)",
            (run_id, conversation["id"], owner["id"], uid(), uid(), now(), dump(assistant),
             dump({"runtime_type": conversation["runtime_type"], "memory_binding": binding,
                   "native_epoch": binding["native_epoch"]})),
        )
        service.db.execute("UPDATE conversations SET active_run_id=? WHERE id=?", (run_id, conversation["id"]))
        service.knowledge.snapshot_for_run(run_id, assistant, owner)
        # Exercise SDKRuntime.check_tool without resolving any model credentials.
        config = {"assistant_snapshot": assistant, "fingerprint": "fixture-v1"}
        monkeypatch.setattr(service, "runtime_config", lambda _cid: {"fingerprint": "fixture-v1"})
        runtime = SimpleNamespace(service=service, conversation_id=conversation["id"], config=config,
                                  current_run={"id": run_id})
        runtime.check_tool = lambda tool, args: SDKRuntime.check_tool(runtime, tool, args)
        yield runtime, owner, kb, doc


def call(bridge, name="knowledge_search", arguments=None):
    return asyncio.run(bridge.invoke(name, {"query": "P-900"} if arguments is None else arguments))


def test_disabled_by_default_and_boolean_opt_in_required(system):
    runtime, *_ = system
    for enabled in (False, "true", 1, None):
        with pytest.raises(AppError) as exc:
            call(HarnessKnowledgeBridge(runtime, enabled=enabled))
        assert exc.value.code == "HARNESS_BRIDGE_DISABLED"


@pytest.mark.parametrize("name", ["generate_xlsx", "generate_image", "web_search", "bash", "Read", "mcp__workspace__knowledge_search", "plugin_install", "x" * 5000])
def test_non_knowledge_tools_never_execute(system, name):
    runtime, *_ = system
    with pytest.raises(AppError) as exc:
        call(HarnessKnowledgeBridge(runtime, enabled=True), name)
    assert exc.value.code == "HARNESS_TOOL_DENIED"
    assert not (runtime.service.settings.data_dir / "workspaces" / runtime.conversation_id / "artifacts").exists()


@pytest.mark.parametrize("name,args", [
    ("knowledge_search", {}), ("knowledge_search", {"query": " "}),
    ("knowledge_search", {"query": "x" * 2001}), ("knowledge_search", {"query": 12}),
    ("knowledge_search", {"query": "x", "limit": True}),
    ("knowledge_search", {"query": "x", "limit": 0}),
    ("knowledge_search", {"query": "x", "limit": 21}),
    ("knowledge_search", {"query": "x", "user_id": "peer"}),
    ("knowledge_search", []), ("knowledge_search", None),
    ("knowledge_read", {"document_id": "../../secret", "page": 0}),
    ("knowledge_read", {"document_id": "doc", "page": -1}),
    ("knowledge_read", {"document_id": "doc", "page": True}),
    ("knowledge_read", {"document_id": "doc", "page": 100001}),
    ("knowledge_read", {"document_id": "doc", "run_id": "other"}),
])
def test_strict_arguments_before_any_read(system, name, args, monkeypatch):
    runtime, *_ = system
    def unexpected(*_args, **_kwargs):
        pytest.fail("invalid arguments reached the knowledge service")
    monkeypatch.setattr(runtime.service.knowledge, "search_for_run", unexpected)
    monkeypatch.setattr(runtime.service.knowledge, "read_for_run", unexpected)
    with pytest.raises(AppError) as exc:
        asyncio.run(HarnessKnowledgeBridge(runtime, enabled=True).invoke(name, args))
    assert exc.value.code == "HARNESS_ARGUMENTS_INVALID"


def test_real_search_and_read_keep_snapshot_and_source_links(system):
    runtime, owner, kb, doc = system
    bridge = HarnessKnowledgeBridge(runtime, enabled=True)
    runtime.service.knowledge.upload(kb["id"], owner, "pump.md", b"P-900 pressure limit is 9 bar.", doc["id"])
    result = call(bridge)
    assert not result.get("is_error")
    body = json.loads(result["content"][0]["text"])
    assert body["results"]
    hit = body["results"][0]
    assert hit["version"] == 1
    assert hit["citation"].startswith("[[source:")
    assert hit["source_url"].startswith("/api/v1/sources/")
    assert hit["content_url"].endswith("version=1")
    read = call(bridge, "knowledge_read", {"document_id": doc["id"], "page": 0})
    assert not read.get("is_error")
    assert "7 bar" in read["content"][0]["text"]
    assert "9 bar" not in read["content"][0]["text"]


@pytest.mark.parametrize("change", ["run_id", "run_owner", "run_conversation", "inactive", "finished", "archived", "assistant", "disabled", "config", "kb"])
def test_revocation_and_binding_changes_fail_closed(system, change):
    runtime, owner, kb, _ = system
    bridge = HarnessKnowledgeBridge(runtime, enabled=True)
    db = runtime.service.db
    if change == "run_id":
        runtime.current_run = {"id": "foreign"}
    elif change == "run_owner":
        peer = runtime.service.create_user({"username": "peer", "display_name": "peer", "password": "test-password-123", "role": "member"})
        db.execute("UPDATE runs SET user_id=? WHERE id=?", (peer["id"], runtime.current_run["id"]))
    elif change == "run_conversation":
        other = runtime.service.create_conversation({"assistant_id": runtime.config["assistant_snapshot"]["id"], "title": "Other"}, user=owner)
        db.execute("UPDATE runs SET conversation_id=? WHERE id=?", (other["id"], runtime.current_run["id"]))
    elif change == "inactive":
        db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (runtime.conversation_id,))
    elif change == "finished":
        db.execute("UPDATE runs SET status='cancelled' WHERE id=?", (runtime.current_run["id"],))
    elif change == "archived":
        db.execute("UPDATE conversations SET archived=1 WHERE id=?", (runtime.conversation_id,))
    elif change == "assistant":
        db.execute("UPDATE assistants SET enabled=0 WHERE id=?", (runtime.config["assistant_snapshot"]["id"],))
    elif change == "disabled":
        db.execute("UPDATE users SET enabled=0 WHERE id=?", (owner["id"],))
    elif change == "config":
        runtime.config["fingerprint"] = "changed"
    else:
        db.execute("UPDATE knowledge_bases SET enabled=0 WHERE id=?", (kb["id"],))
    with pytest.raises(AppError):
        call(bridge)


def test_requires_approval_cannot_reuse_sdk_autoallow_semantics(system, monkeypatch):
    runtime, *_ = system
    bridge = HarnessKnowledgeBridge(runtime, enabled=True)
    runtime.check_tool = lambda *_: False
    with pytest.raises(AppError) as exc:
        call(bridge)
    assert exc.value.code == "HARNESS_APPROVAL_REQUIRED"
    runtime.check_tool = lambda *_: True
    monkeypatch.setattr(runtime.service.model_approval, "requires_review", lambda *_: True)
    with pytest.raises(AppError) as exc:
        call(bridge)
    assert exc.value.code == "HARNESS_APPROVAL_REQUIRED"


def test_real_config_fingerprint_detects_knowledge_revision(system, monkeypatch):
    runtime, _, kb, _ = system
    service = runtime.service
    # Keep real fingerprint/ACL logic; only the provider credential boundary is stubbed.
    monkeypatch.setattr(service, "runtime_config", MethodType(Service.runtime_config, service))
    monkeypatch.setattr(service, "validated_provider", lambda _selection: {})
    runtime.config = service.runtime_config(runtime.conversation_id)
    bridge = HarnessKnowledgeBridge(runtime, enabled=True)
    service.db.execute("UPDATE knowledge_bases SET revision=revision+1 WHERE id=?", (kb["id"],))
    with pytest.raises(AppError) as exc:
        call(bridge)
    assert exc.value.code == "CONFIG_CHANGED"


def test_recheck_discards_result_after_revocation(system, monkeypatch):
    runtime, owner, *_ = system
    async def revoked(*args, **kwargs):
        runtime.service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (owner["id"],))
        return {"results": [{"text": "must not reach caller"}]}
    monkeypatch.setattr(runtime.service.knowledge, "search_for_run", revoked)
    with pytest.raises(AppError):
        call(HarnessKnowledgeBridge(runtime, enabled=True))


@pytest.mark.parametrize("during_read", [False, True])
def test_dual_approval_policy_never_bypassed(system, monkeypatch, during_read):
    runtime, owner, *_ = system
    service = runtime.service
    service.db.execute("UPDATE users SET role='admin' WHERE id=?", (owner["id"],))
    admin = service.require("users", owner["id"])
    def enable():
        service.high_risk_approval.save_policy(runtime.config["assistant_snapshot"]["id"], {
            "enabled": True, "allowed_tools": [], "admin_allowlist": [],
        }, admin)
    async def read(*args, **kwargs):
        if not during_read:
            pytest.fail("dual policy was bypassed before read")
        enable()
        return {"results": [{"text": "must not reach caller"}]}
    monkeypatch.setattr(service.knowledge, "search_for_run", read)
    if not during_read:
        enable()
    with pytest.raises(AppError) as exc:
        call(HarnessKnowledgeBridge(runtime, enabled=True))
    assert exc.value.code == "HARNESS_APPROVAL_REQUIRED"


def test_audit_omits_queries_documents_and_untrusted_names(system):
    runtime, *_ = system
    bridge = HarnessKnowledgeBridge(runtime, enabled=True)
    call(bridge, arguments={"query": "PRIVATE_QUERY_MARKER"})
    with pytest.raises(AppError):
        call(bridge, name="PRIVATE_TOOL_MARKER")
    events = runtime.service.db.events(runtime.current_run["id"], 0)
    events = [item for item in events if item["type"] == "harness.bridge"]
    assert [item["data"]["outcome"] for item in events] == ["started", "succeeded", "denied"]
    assert events[0]["data"]["call_id"] == events[1]["data"]["call_id"]
    assert "PRIVATE_" not in dump(events)
    assert all(set(item["data"]) == {"schema", "call_id", "tool", "outcome"} for item in events)


def test_cancelled_read_keeps_cancellation_and_terminal_audit(system, monkeypatch):
    runtime, *_ = system
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(runtime.service.knowledge, "search_for_run", cancelled)
    with pytest.raises(asyncio.CancelledError):
        call(HarnessKnowledgeBridge(runtime, enabled=True))
    events = runtime.service.db.events(runtime.current_run["id"], 0)
    assert [event["data"]["outcome"] for event in events if event["type"] == "harness.bridge"] == ["started", "cancelled"]


@pytest.mark.parametrize("after_read", [False, True])
def test_audit_failure_never_returns_documents_or_internal_errors(system, monkeypatch, after_read):
    runtime, *_ = system
    original = runtime.service.db.event
    def broken(run_id, kind, data):
        if not after_read or data["outcome"] != "started":
            raise OSError("PRIVATE_DATABASE_DETAILS")
        return original(run_id, kind, data)
    monkeypatch.setattr(runtime.service.db, "event", broken)
    with pytest.raises(AppError) as exc:
        call(HarnessKnowledgeBridge(runtime, enabled=True))
    assert exc.value.code == "HARNESS_AUDIT_UNAVAILABLE"
    assert "PRIVATE" not in exc.value.message
