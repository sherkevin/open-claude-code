import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.capabilities import create_workspace_tools
from app.security import AppError
from app.workspace_tools import WorkspaceTool, build_workspace_tools


@pytest.fixture
def runtime(tmp_path):
    calls = []
    source = {"marker": "[source:knowledge:doc:3]", "url": "/knowledge-bases/kb/documents/doc", "page": 3}

    class Knowledge:
        async def search_for_run(self, user, assistant, run_id, query, limit):
            calls.append(("search", user, assistant, run_id, query, limit))
            return {"results": [{"text": "evidence", "source": source}]}

        async def read_for_run(self, user, assistant, run_id, document_id, page):
            calls.append(("read", user, assistant, run_id, document_id, page))
            return {"text": "evidence", "source": source}

    class Service:
        settings = SimpleNamespace(data_dir=tmp_path)
        catalog = SimpleNamespace(models={})
        knowledge = Knowledge()
        role = "admin"
        revoked = False

        def require(self, table, item_id):
            if table == "conversations":
                return {"id": "conversation", "user_id": "user", "assistant_id": "assistant"}
            return {"id": "user", "role": self.role}

        def usable_assistant(self, assistant_id, user):
            if self.revoked:
                raise AppError(403, "DENIED", "access revoked")
            return {"id": assistant_id, "knowledge_base_ids": ["kb", "new-kb"], "web_search_enabled": True}

    checks = []
    return SimpleNamespace(
        service=Service(),
        conversation_id="conversation",
        config={"assistant_snapshot": {"id": "assistant", "knowledge_base_ids": ["kb"], "web_search_enabled": True}},
        current_run={"id": "run"},
        check_tool=lambda name, arguments: checks.append((name, arguments)) or True,
        calls=calls,
        checks=checks,
        source=source,
    )


def registry(runtime):
    return {definition.name: definition for definition in build_workspace_tools(runtime)}


def test_registry_import_does_not_require_agent_sdk():
    code = """
import builtins
original_import = builtins.__import__
def no_sdk(name, *args, **kwargs):
    if name.split('.')[0] in {'claude_agent_sdk', 'mcp'}:
        raise AssertionError('agent SDK must remain optional: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = no_sdk
from app.workspace_tools import WorkspaceTool, build_workspace_tools
from app.capabilities import create_workspace_tools
"""
    result = subprocess.run([sys.executable, "-B", "-c", code], text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_registry_preserves_tools_and_schemas(runtime):
    tools = build_workspace_tools(runtime)
    assert all(isinstance(definition, WorkspaceTool) for definition in tools)
    assert [(tool.name, tool.input_schema, tool.read_only) for tool in tools] == [
        ("generate_xlsx", {"filename": str, "sheets": list}, False),
        ("generate_image", {"prompt": str, "model_id": str}, False),
        ("knowledge_search", {"query": str, "limit": int}, True),
        ("knowledge_read", {"document_id": str, "page": int}, True),
        ("web_search", {"query": str, "limit": int}, True),
        ("web_read", {"url": str, "query": str}, True),
        ("file_read", {"path": str}, True),
        ("file_write", {"path": str, "content": str, "expected_revision": int}, False),
        ("file_search", {"query": str}, True),
    ]
    assert all(tool.description and callable(tool.handler) for tool in tools)


@pytest.mark.parametrize("knowledge_base_ids", [None, "[]", []])
def test_registry_keeps_assistant_capability_switches(runtime, knowledge_base_ids):
    runtime.config["assistant_snapshot"].update(knowledge_base_ids=knowledge_base_ids, web_search_enabled=False)
    assert list(registry(runtime)) == [
        "generate_xlsx", "generate_image", "file_read", "file_write", "file_search",
    ]


def test_registry_uses_live_assistant_when_no_snapshot(runtime):
    runtime.config = {}
    tools = registry(runtime)
    asyncio.run(tools["knowledge_search"].handler({"query": "evidence"}))
    assert runtime.calls[0][2]["knowledge_base_ids"] == ["kb", "new-kb"]


def test_sdk_wrapper_preserves_registry_metadata_and_server_contract(runtime, monkeypatch):
    captured = {}

    def server(name, version, *, tools):
        captured.update(name=name, version=version)
        return tools

    monkeypatch.setattr("claude_agent_sdk.create_sdk_mcp_server", server)
    expected = build_workspace_tools(runtime)
    actual = create_workspace_tools(runtime)
    assert captured == {"name": "workspace", "version": "0.4.0"}
    for sdk_tool, definition in zip(actual, expected, strict=True):
        assert sdk_tool.name == definition.name
        assert sdk_tool.description == definition.description
        if definition.name in {"web_search", "web_read"}:
            schema = sdk_tool.input_schema
            assert schema["type"] == "object" and schema["additionalProperties"] is False
            assert set(schema["properties"]) == set(definition.input_schema)
            assert schema["required"] == ["query" if definition.name == "web_search" else "url"]
            assert schema["properties"]["query"]["type"] == "string"
            assert "__oc_capability_ticket" not in schema["properties"]
            if definition.name == "web_search":
                assert schema["properties"]["limit"] == {"type": "integer", "minimum": 1, "maximum": 10}
                assert schema["properties"]["query"]["maxLength"] == 500
            else:
                assert schema["properties"]["url"]["maxLength"] == 4096
        else:
            assert sdk_tool.input_schema == definition.input_schema
        assert sdk_tool.annotations.readOnlyHint is definition.read_only
        assert sdk_tool.annotations.destructiveHint is False


def test_knowledge_handlers_preserve_citations_snapshot_and_current_acl(runtime):
    tools = registry(runtime)
    runtime.service.role = "member"
    result = asyncio.run(tools["knowledge_search"].handler({"query": "evidence"}))
    assert json.loads(result["content"][0]["text"])["results"][0]["source"] == runtime.source
    result = asyncio.run(tools["knowledge_read"].handler({"document_id": "doc", "page": 0}))
    assert json.loads(result["content"][0]["text"])["source"] == runtime.source
    assert runtime.calls[0][1]["role"] == "member"
    assert runtime.calls[0][2]["knowledge_base_ids"] == ["kb"]
    assert runtime.calls[0][3:] == ("run", "evidence", 6)
    assert runtime.calls[1][3:] == ("run", "doc", None)
    assert runtime.checks == [
        ("mcp__workspace__knowledge_search", {"query": "evidence"}),
        ("mcp__workspace__knowledge_read", {"document_id": "doc", "page": 0}),
    ]


@pytest.mark.parametrize("failure", ["revoked", "no_run", "permission", "unexpected"])
def test_registry_errors_do_not_execute_business_reads(runtime, failure):
    tools = registry(runtime)
    if failure == "revoked":
        runtime.service.revoked = True
    elif failure == "no_run":
        runtime.current_run = None
    else:
        def denied(*args):
            if failure == "permission":
                raise AppError(403, "DENIED", "access revoked")
            raise RuntimeError("private provider detail")
        runtime.check_tool = denied
    result = asyncio.run(tools["knowledge_search"].handler({"query": "evidence"}))
    assert result["is_error"] is True
    assert "isError" not in result
    assert result["content"][0]["type"] == "text"
    assert "private provider detail" not in result["content"][0]["text"]
    assert runtime.calls == []


def test_approved_xlsx_handler_keeps_existing_false_check_contract(runtime):
    runtime.check_tool = lambda *args: False
    result = asyncio.run(registry(runtime)["generate_xlsx"].handler({
        "filename": "approved.xlsx", "sheets": [{"name": "data", "rows": [["evidence"]]}],
    }))
    assert not result.get("is_error")
    assert json.loads(result["content"][0]["text"])["status"] == "created"
    assert (runtime.service.settings.data_dir / "workspaces/conversation/artifacts/approved.xlsx").is_file()


def test_denied_write_does_not_create_artifact(runtime):
    def denied(*args):
        raise AppError(403, "DENIED", "access revoked")
    runtime.check_tool = denied
    result = asyncio.run(registry(runtime)["generate_xlsx"].handler({"filename": "denied.xlsx", "sheets": []}))
    assert result["is_error"] is True
    assert not (runtime.service.settings.data_dir / "workspaces").exists()


def test_image_handler_keeps_unconfigured_route_fail_closed(runtime):
    result = asyncio.run(registry(runtime)["generate_image"].handler({"prompt": "test", "model_id": "missing"}))
    assert result["is_error"] is True
    assert not (runtime.service.settings.data_dir / "workspaces").exists()


def test_web_handlers_preserve_defaults_and_source_links(runtime, monkeypatch):
    calls = []
    host_bindings = []
    source = {"marker": "[source:web:public]", "url": "https://example.com/evidence"}

    class Web:
        def __init__(self, service):
            assert service is runtime.service

        async def search_for_run(self, user, assistant, run_id, query, limit, *, call_id=None, approval_id=None):
            calls.append(("search", user, assistant, run_id, query, limit))
            host_bindings.append((call_id, approval_id))
            return {"results": [{"source": source}]}

        async def read_for_run(self, user, assistant, run_id, url, query, *, call_id=None, approval_id=None):
            calls.append(("read", user, assistant, run_id, url, query))
            host_bindings.append((call_id, approval_id))
            return {"source": source}

    monkeypatch.setattr("app.workspace_tools.WebTools", Web)
    tools = registry(runtime)
    async def invoke_with_host_binding():
        from app.workspace_tools import APPROVAL_ID, TOOL_CALL_ID
        call_token, approval_token = TOOL_CALL_ID.set("host-web-call"), APPROVAL_ID.set("receipt-web-call")
        try:
            result = await tools["web_search"].handler({"query": "public"})
            assert json.loads(result["content"][0]["text"])["results"][0]["source"] == source
            return await tools["web_read"].handler({"url": source["url"]})
        finally:
            TOOL_CALL_ID.reset(call_token)
            APPROVAL_ID.reset(approval_token)
    result = asyncio.run(invoke_with_host_binding())
    assert json.loads(result["content"][0]["text"])["source"] == source
    assert calls[0][3:] == ("run", "public", 5)
    assert calls[1][3:] == ("run", source["url"], "")
    assert host_bindings == [("host-web-call", "receipt-web-call")] * 2
