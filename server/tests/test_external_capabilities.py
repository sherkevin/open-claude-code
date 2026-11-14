"""Synthetic (no provider) tests for independent OCR and image search.

Covers strict OCR integrity, zero automatic retry, HTTP-200 error envelopes,
upload isolation, revocation/cancellation before evidence, provider-specific
image parsing, separate page/image evidence links, and fail-closed source
authorization.  No real provider, credential or customer data is used.
"""
import asyncio
import io
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.db import now, uid
from app.deepseek_api import DeepSeekAPI
from app.evidence import EvidenceService
from app.external_capabilities import ExternalCapabilities, ToolContext, public_provider
from app.image_search_adapters import (
    WEBTOOLS_REVERSE_INSTRUCTION,
    parse_brave_images,
    parse_webtools_image_result,
)
from app.security import AppError, tool_risk_profile
from app.service import Service
from app.workspace_tools import WorkspaceTool, build_workspace_tools


def png_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (16, 12), "blue").save(stream, format="PNG")
    return stream.getvalue()


def row_path_bytes(system, row):
    return Path(row["path"]).read_bytes()


def transport_for(responses, requests=None, on_request=None):
    requests = requests if requests is not None else []

    def handler(request):
        requests.append(request)
        if on_request is not None:
            on_request(request)
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value
    return httpx.MockTransport(handler)


class FakeExecutor:
    def __init__(self, provider="deepseek_api", rechecks=None):
        self.provider = provider
        self.calls = []
        self.rechecks = rechecks if rechecks is not None else []

    async def execute_for_run(self, context, capability, arguments, operation, *,
                              target_url=None, external_upload=False):
        self.calls.append({"context": context, "capability": capability, "arguments": arguments,
                           "operation": operation, "target_url": target_url,
                           "external_upload": external_upload})

        def recheck():
            self.rechecks.append(time.monotonic())

        value = await operation(self.provider, time.monotonic() + 30, recheck)
        return SimpleNamespace(value=value, provider=self.provider,
                               requested_provider=self.provider, fallback=False, attempts=1)


@pytest.fixture
def policy(monkeypatch):
    module = types.ModuleType("app.capability_policy")
    module.calls = []

    def authorize_source_access(service, row, user):
        module.calls.append((row["id"], user["id"]))

    module.authorize_source_access = authorize_source_access
    monkeypatch.setitem(sys.modules, "app.capability_policy", module)
    return module


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, runtime_type="harness", model="test",
                               provider_settings="", model_catalog="", knowledge_vision_enabled=False))
    user = service.create_user({"username": "capability", "display_name": "Test",
                                "password": "safe-test-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Capability test", "connections": []}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "test"}, user)
    service.settings.deepseek_api_enabled = True
    service.settings.deepseek_api_key = "test-key"
    service.settings.deepseek_api_base_url = "https://api.deepseek.com"
    service.settings.deepseek_api_timeout_seconds = 30
    service.settings.web_search_brave_api_key = "brave-test-key"

    def upload(data, name="scan.png", mime="image/png", **fields):
        file_id = uid()
        path = tmp_path / "files" / file_id
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(data)
        row = {"id": file_id, "user_id": user["id"], "name": name, "mime_type": mime,
               "size": len(data), "path": str(path), "created_at": now(), "conversation_id": None, **fields}
        service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", tuple(row.values()))
        return row

    def run_with(files):
        response, _ = service.prepare_run(conversation["id"], {
            "text": "read", "file_ids": [row["id"] for row in files], "client_request_id": uid()}, user)
        run = service.require("runs", response["run_id"])
        service.db.execute("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
        return service.require("runs", run["id"])

    yield SimpleNamespace(service=service, user=user, assistant=assistant,
                          conversation=conversation, upload=upload, run_with=run_with,
                          tmp_path=tmp_path)
    service.db.close()


def context_for(system, run, checks=None):
    checks = checks if checks is not None else []
    return ToolContext(run_id=run["id"], conversation_id=system.conversation["id"],
                       user_id=system.user["id"], assistant_id=system.assistant["id"],
                       tool_call_id="call-ocr-1", recheck=lambda: checks.append("recheck"))


# --------------------------------------------------------------------------
# Registration and argument validation
# --------------------------------------------------------------------------

def fake_runtime(capabilities):
    class Service:
        settings = SimpleNamespace(data_dir=Path("/tmp/unused-capability-test"))
        catalog = SimpleNamespace(models={})
        knowledge = SimpleNamespace()

        def require(self, table, item_id):
            if table == "conversations":
                return {"id": "conversation", "user_id": "user", "assistant_id": "assistant"}
            return {"id": "user", "role": "member"}

        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "knowledge_base_ids": [], "web_search_enabled": False}

    return SimpleNamespace(service=Service(), conversation_id="conversation",
                           config={"assistant_snapshot": {"id": "assistant", "knowledge_base_ids": [],
                                                          "web_search_enabled": False,
                                                          "capabilities": capabilities}},
                           current_run={"id": "run"}, check_tool=lambda *args: True)


def test_new_tools_register_only_with_explicit_assistant_grant():
    tools = {tool.name: tool for tool in build_workspace_tools(fake_runtime({}))}
    assert "ocr" not in tools and "image_search" not in tools
    tools = {tool.name: tool for tool in build_workspace_tools(fake_runtime({
        "ocr": {"enabled": True}, "image_search": {"enabled": True}}))}
    assert tools["ocr"].input_schema == {"file_id": str, "purpose": str}
    assert tools["image_search"].input_schema == {"mode": str, "query": str, "file_id": str, "limit": int}
    assert tools["ocr"].read_only is True and tools["image_search"].read_only is True
    # A string-encoded snapshot is accepted without trusting it as identity.
    encoded = {tool.name: tool for tool in build_workspace_tools(fake_runtime(
        json.dumps({"ocr": {"enabled": True}})))}
    assert "ocr" in encoded and "image_search" not in encoded


def test_harness_requirement_table_contains_new_tools():
    from app.harness_tools import HarnessToolBridge
    runtime = fake_runtime({"ocr": {"enabled": True}, "image_search": {"enabled": True}})
    bridge = HarnessToolBridge.__new__(HarnessToolBridge)
    bridge.tools = {tool.name: tool for tool in build_workspace_tools(runtime)}
    bridge.business_tools = {}
    assert bridge.arguments("ocr", {"file_id": "f1"}) == {"file_id": "f1"}
    assert bridge.arguments("ocr", {"file_id": "f1", "purpose": "rich_layout_recognition"})
    with pytest.raises(AppError):
        bridge.arguments("ocr", {})
    with pytest.raises(AppError):
        bridge.arguments("ocr", {"file_id": "f1", "purpose": "make_up"})
    with pytest.raises(AppError):
        bridge.arguments("image_search", {"mode": "text_to_image", "query": "x", "file_id": "f1"})
    with pytest.raises(AppError):
        bridge.arguments("image_search", {"mode": "reverse_image", "query": "x", "file_id": "f1"})
    with pytest.raises(AppError):
        bridge.arguments("image_search", {"mode": "reverse_image"})
    assert bridge.arguments("image_search", {"mode": "text_search", "query": "森林"})
    assert bridge.arguments("image_search", {"query": "森林", "limit": 3})
    assert bridge.arguments("image_search", {"mode": "reverse_image", "file_id": "f1"})


# --------------------------------------------------------------------------
# OCR execution
# --------------------------------------------------------------------------

def test_ocr_complete_result_registers_attachment_page_evidence(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    requests = []
    body = {"id": "ocr-1", "page_count": 1,
            "pages": [{"index": 0, "text": "受控样本文字", "finish_reason": "stop"}],
            "usage": {"total_tokens": 7}}
    transport = transport_for([httpx.Response(200, json=body)], requests)
    executor = FakeExecutor("deepseek_api")
    external = ExternalCapabilities(system.service, executor=executor, ocr_transport=transport)
    result = asyncio.run(external.ocr_for_run(context_for(system, run), row["id"], "text_recognition"))
    assert result["status"] == "complete" and result["provider"] == "managed_ocr"
    assert result["page_count"] == 1 and result["missing_pages"] == []
    assert len(requests) == 1
    assert executor.calls[0]["capability"] == "ocr" and executor.calls[0]["external_upload"] is True
    assert executor.calls[0]["context"].tool_call_id == "call-ocr-1"
    source = system.service.require("files", row["id"])
    assert source["id"] == row["id"]
    page = result["pages"][0]
    assert page["source_id"] and page["citation"] == f"[[source:{page['source_id']}]]"
    view = system.service.evidence.get(page["source_id"], system.user)
    assert view["locator"]["capability"] == "ocr"
    assert view["locator"]["provider"] == "managed_ocr"
    assert view["locator"]["sha256"] == result["sha256"]
    assert view["content_url"] == f"/api/v1/files/{row['id']}/content"
    assert policy.calls and policy.calls[0][0] == page["source_id"]


@pytest.mark.parametrize("body,status", [
    ({"page_count": 2, "pages": [{"index": 0, "text": "only one"}]}, "partial"),
    ({"page_count": 1, "pages": [{"index": 0, "text": "cut", "finish_reason": "length"}]}, "partial"),
    ({"page_count": 1, "pages": [{"index": 0, "text": ""}]}, "no_text"),
])
def test_ocr_partial_or_empty_is_never_full_success(system, policy, body, status):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    transport = transport_for([httpx.Response(200, json=body)])
    external = ExternalCapabilities(system.service, executor=FakeExecutor(), ocr_transport=transport)
    result = asyncio.run(external.ocr_for_run(context_for(system, run), row["id"]))
    assert result["status"] == status
    if status == "no_text":
        # The explicit no-text result is visible, but it produces no citation
        # and is never labelled complete.
        assert result["source_ids"] == []
        assert result["pages"] and all("source_id" not in page for page in result["pages"])
        assert "不是识别成功" in result["note"]


def test_ocr_http_200_error_envelope_never_registers_evidence(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    body = {"error": {"code": "upstream_timeout", "message": "secret upstream detail"}}
    external = ExternalCapabilities(system.service, executor=FakeExecutor(),
                                    ocr_transport=transport_for([httpx.Response(200, json=body)]))
    with pytest.raises(AppError) as error:
        asyncio.run(external.ocr_for_run(context_for(system, run), row["id"]))
    assert error.value.code == "EXTERNAL_TOOL_TIMEOUT"
    assert "secret upstream detail" not in error.value.message
    assert system.service.db.all("SELECT id FROM evidence_sources") == []


def test_ocr_revocation_after_response_discards_result(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    state = {"revoked": False}

    def revoke(request):
        state["revoked"] = True

    def recheck():
        if state["revoked"]:
            raise AppError(403, "CAPABILITY_REVOKED", "授权已撤销")

    body = {"page_count": 1, "pages": [{"index": 0, "text": "text", "finish_reason": "stop"}]}
    external = ExternalCapabilities(system.service, executor=FakeExecutor(),
                                    ocr_transport=transport_for([httpx.Response(200, json=body)],
                                                                on_request=revoke))
    context = ToolContext(run_id=run["id"], conversation_id=system.conversation["id"],
                          user_id=system.user["id"], assistant_id=system.assistant["id"],
                          tool_call_id="call-revoke", recheck=recheck)
    with pytest.raises(AppError) as error:
        asyncio.run(external.ocr_for_run(context, row["id"]))
    assert error.value.code == "CAPABILITY_REVOKED"
    assert system.service.db.all("SELECT id FROM evidence_sources") == []


def test_ocr_cancellation_propagates_without_evidence(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    transport = transport_for([asyncio.CancelledError()])
    external = ExternalCapabilities(system.service, executor=FakeExecutor(), ocr_transport=transport)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(external.ocr_for_run(context_for(system, run), row["id"]))
    assert system.service.db.all("SELECT id FROM evidence_sources") == []


def test_ocr_snapshot_change_before_acceptance_discards_result(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    body = {"page_count": 1, "pages": [{"index": 0, "text": "text"}]}
    external = ExternalCapabilities(system.service, executor=FakeExecutor(),
                                    ocr_transport=transport_for([httpx.Response(200, json=body)]))
    original = ExternalCapabilities.ocr_for_run

    class MutatingExecutor(FakeExecutor):
        async def execute_for_run(self, context, capability, arguments, operation, **kwargs):
            result = await super().execute_for_run(context, capability, arguments, operation, **kwargs)
            system.service.db.execute("UPDATE files SET size=size+1 WHERE id=?", (row["id"],))
            return result

    external._executor = MutatingExecutor()
    with pytest.raises(AppError) as error:
        asyncio.run(original(external, context_for(system, run), row["id"]))
    assert error.value.code == "UPLOAD_CHANGED"
    assert system.service.db.all("SELECT id FROM evidence_sources") == []


# --------------------------------------------------------------------------
# Image search parsers
# --------------------------------------------------------------------------

def test_brave_images_requires_page_and_original_image_and_drops_unsafe():
    body = {"type": "images", "results": [
        {"type": "image_result", "url": "https://example.com/page",
         "properties": {"url": "https://cdn.example.com/a.jpg", "width": 640, "height": 480},
         "thumbnail": {"src": "https://thumb.example.com/a.jpg"}, "title": "A"},
        # missing original image: dropped, not repaired
        {"type": "image_result", "url": "https://example.com/page2", "properties": {}},
        # private host rejected
        {"type": "image_result", "url": "http://127.0.0.1/page",
         "properties": {"url": "https://cdn.example.com/b.jpg"}},
        # credential query rejected
        {"type": "image_result", "url": "https://example.com/p?access_token=abc",
         "properties": {"url": "https://cdn.example.com/c.jpg"}},
        # duplicate pair
        {"type": "image_result", "url": "https://example.com/page",
         "properties": {"url": "https://cdn.example.com/a.jpg"}},
    ]}
    parsed = parse_brave_images(body, limit=10, query="q")
    assert parsed["status"] == "results" and len(parsed["results"]) == 1
    item = parsed["results"][0]
    assert item["page_url"] == "https://example.com/page"
    assert item["image_url"] == "https://cdn.example.com/a.jpg"
    assert item["width"] == 640 and item["height"] == 480


def test_brave_images_contract_break_and_no_results_are_distinct():
    with pytest.raises(AppError):
        parse_brave_images({"results": []})
    assert parse_brave_images({"type": "images", "results": []})["status"] == "no_results"


def test_webtools_parser_distinguishes_not_executed_no_results_and_results():
    plain = {"content": [{"type": "text", "text": "Here is an image: https://evil.example/x.jpg"}]}
    assert parse_webtools_image_result(plain)["status"] == "not_executed"
    # A plain text web search is a tool call, but it is not image execution.
    text_search = {"content": [
        {"type": "server_tool_use", "id": "srv-1", "name": "deepseek_web_search", "input": {"query": "x"}},
        {"type": "web_search_tool_result", "tool_use_id": "srv-1", "content": [
            {"type": "web_search_result", "url": "https://example.com/page"}]},
    ]}
    assert parse_webtools_image_result(text_search)["status"] == "not_executed"
    # Image search really ran but returned no image entries.
    empty_image_search = {"content": [
        {"type": "server_tool_use", "id": "srv-2", "name": "deepseek_web_search",
         "input": {"action": "search", "queries": []}, "kind": "image_search"},
        {"type": "web_search_tool_result", "tool_use_id": "srv-2", "kind": "image_search", "content": []},
    ]}
    assert parse_webtools_image_result(empty_image_search)["status"] == "no_results"
    # Real 2026-11-14 response shape: entries are web_search_result with both
    # url and image_url; the call/result kind is image_search.
    real = {"content": [
        {"type": "server_tool_use", "id": "srv-3", "name": "deepseek_web_search",
         "input": {"action": "search", "queries": []}, "kind": "image_search"},
        {"type": "web_search_tool_result", "tool_use_id": "srv-3", "kind": "image_search", "content": [
            {"type": "web_search_result", "url": "https://example.com/source",
             "image_url": "https://cdn.example.com/full.jpg", "title": "source"},
            # missing image_url is not repaired
            {"type": "web_search_result", "url": "https://example.com/missing"},
        ]},
        # uncorrelated result must be ignored
        {"type": "web_search_tool_result", "tool_use_id": "unknown", "kind": "image_search", "content": [
            {"type": "web_search_result", "url": "https://example.com/uncorrelated",
             "image_url": "https://cdn.example.com/uncorrelated.jpg"}]},
        {"type": "server_tool_use", "id": "srv-4", "name": "deepseek_web_search",
         "input": {}, "kind": "deep_image_search"},
        {"type": "web_search_tool_result", "tool_use_id": "srv-4", "kind": "deep_image_search", "content": [
            {"type": "web_search_result", "url": "https://example.com/deep",
             "image_url": "https://cdn.example.com/deep.jpg"}]},
        {"type": "text", "text": "Invented prose URL: https://evil.example/fake.jpg"},
    ], "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 2,
                 "server_tool_use": {"tool_image_search": 2}}}
    parsed = parse_webtools_image_result(real)
    assert parsed["status"] == "results"
    assert [(item["page_url"], item["image_url"]) for item in parsed["results"]] == [
        ("https://example.com/source", "https://cdn.example.com/full.jpg"),
        ("https://example.com/deep", "https://cdn.example.com/deep.jpg"),
    ]
    assert {item["kind"] for item in parsed["results"]} == {"image_search", "deep_image_search"}
    assert parsed["billed_image_search_count"] == 2
    assert parsed["usage"]["input_tokens"] == 10 and parsed["usage"]["cache_read_input_tokens"] == 2


def test_webtools_parser_accepts_real_probe_fixture_without_opaque_content():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "reverse_image_response.json").read_text())
    parsed = parse_webtools_image_result(fixture)
    assert parsed["status"] == "results"
    assert len(parsed["results"]) == 11
    assert all(item["kind"] == "image_search" for item in parsed["results"])
    assert all(item["page_url"].startswith(("http://", "https://")) for item in parsed["results"])
    assert all(item["image_url"].startswith(("http://", "https://")) for item in parsed["results"])
    # Model prose is never turned into a source.
    assert not any("evil.example" in item["page_url"] for item in parsed["results"])


def test_official_text_image_search_uses_correlated_images_and_no_upload(system, policy):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "reverse_image_response.json").read_text())
    run = system.run_with([])
    requests = []
    external = ExternalCapabilities(system.service, executor=FakeExecutor("deepseek_webtools"),
                                    webtools_transport=transport_for([httpx.Response(200, json=fixture)], requests))
    result = asyncio.run(external.image_search_for_run(context_for(system, run),
                                                      mode="text_search", query="ABB robot", limit=3))
    assert result["status"] == "results" and len(result["results"]) == 3
    payload = json.loads(requests[0].content)
    assert len(requests) == 1 and "/anthropic/v1/messages" in str(requests[0].url)
    content = payload["messages"][0]["content"]
    assert all(block["type"] == "text" for block in content)
    assert "ABB robot" in content[0]["text"]
    assert external.executor().calls[0]["external_upload"] is False


def test_v2_web_source_does_not_depend_on_retired_legacy_switch(system, monkeypatch):
    service = system.service
    service.save_assistant({"capabilities": {"search": {"enabled": True}}, "web_search_enabled": False},
                           system.assistant["id"], user=system.user)
    run = system.run_with([])
    tools = __import__("app.web_tools", fromlist=["WebTools"]).WebTools(service)

    async def result(*args):
        return [{"title": "Public fixture", "url": "https://example.com/page", "excerpt": "fixture"}]

    monkeypatch.setattr(tools, "_bing", result)
    response = asyncio.run(tools.search_for_run(system.user, system.assistant, run["id"], "public"))
    source = service.evidence.get(response["results"][0]["source_id"], system.user)
    assert source["original_url"] == "https://example.com/page"
    assert source["locator"]["capability"] == "search"


def test_ocr_returns_bounded_text_and_marks_output_truncation(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    response = {"pages": [{"page": index, "text": "A" * 30000} for index in range(1, 4)],
                "page_count": 3, "empty": False, "partial": False}
    external = ExternalCapabilities(system.service, executor=FakeExecutor())
    client = SimpleNamespace(ocr_bytes=lambda *args, **kwargs: asyncio.sleep(0, result=response))
    external.ocr_client = lambda: client
    result = asyncio.run(external.ocr_for_run(context_for(system, run), row["id"]))
    assert result["status"] == "partial"
    assert sum(len(page["text"]) for page in result["pages"]) == 48000
    assert all(page["truncated"] and not page["complete"] for page in result["pages"])
    assert len(result["source_ids"]) == 2
    assert all(system.service.evidence.get(source, system.user)["excerpt"]
               for source in result["source_ids"])


# --------------------------------------------------------------------------
# Image execution and evidence
# --------------------------------------------------------------------------

def test_text_search_registers_separate_page_and_image_links(system, policy):
    run = system.run_with([])
    body = {"type": "images", "results": [
        {"type": "image_result", "url": "https://example.com/page",
         "properties": {"url": "https://cdn.example.com/a.jpg"}, "title": "A"}]}
    executor = FakeExecutor("brave_images")
    external = ExternalCapabilities(system.service, executor=executor,
                                    brave_transport=transport_for([httpx.Response(200, json=body)]))
    result = asyncio.run(external.image_search_for_run(
        context_for(system, run), mode="text_search", query="工业安全", limit=3))
    assert result["status"] == "results" and result["provider"] == "brave_images"
    item = result["results"][0]
    assert item["page_url"] == "https://example.com/page"
    assert item["image_url"] == "https://cdn.example.com/a.jpg"
    view = system.service.evidence.get(item["source_id"], system.user)
    assert view["original_url"] == "https://example.com/page"
    assert view["locator"]["image_url"] == "https://cdn.example.com/a.jpg"
    assert view["locator"]["capability"] == "image_search"
    assert view["locator"]["content_type"] == "image_search_result"
    assert view["locator"]["url"] == "https://example.com/page"
    assert view["content_url"] is None


def test_reverse_image_sends_only_selected_upload_and_keeps_status(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    requests = []
    body = {"content": [
        {"type": "server_tool_use", "id": "srv-9", "name": "deepseek_web_search",
         "input": {"action": "search", "queries": []}, "kind": "image_search"},
        {"type": "web_search_tool_result", "tool_use_id": "srv-9", "kind": "image_search", "content": [
            {"type": "web_search_result", "url": "https://example.com/match",
             "image_url": "https://cdn.example.com/match.jpg"}]},
    ]}
    executor = FakeExecutor("deepseek_webtools")
    external = ExternalCapabilities(system.service, executor=executor,
                                    webtools_transport=transport_for([httpx.Response(200, json=body)], requests))
    result = asyncio.run(external.image_search_for_run(
        context_for(system, run), mode="reverse_image", file_id=row["id"]))
    assert result["status"] == "results" and result["provider"] == "managed_image_search"
    assert executor.calls[0]["external_upload"] is True
    assert executor.calls[0]["arguments"] == {"mode": "reverse_image", "file_id": row["id"], "limit": 5}
    payload = json.loads(requests[0].content)
    # Only the selected image and a fixed instruction; no conversation context.
    assert payload["model"] == "deepseek-flash"
    assert payload["thinking"] == {"type": "disabled"} and payload["stream"] is False
    assert payload["tools"] == [{"type": "deepseek_web_search", "name": "deepseek_web_search"}]
    assert payload["messages"][0]["content"][0]["text"] == WEBTOOLS_REVERSE_INSTRUCTION
    assert len(payload["messages"][0]["content"]) == 2
    image = payload["messages"][0]["content"][1]
    assert image["type"] == "image"
    assert image["source"]["type"] == "base64"
    assert image["source"]["media_type"] == "image/png"
    decoded = __import__("base64").b64decode(image["source"]["data"])
    assert decoded == row_path_bytes(system, row)
    assert "/anthropic/v1/messages" in str(requests[0].url)


def test_reverse_image_not_executed_is_explicit_and_has_no_sources(system, policy):
    row = system.upload(png_bytes())
    run = system.run_with([row])
    body = {"choices": [{"message": {"content": "I did not call the tool."}}]}
    external = ExternalCapabilities(system.service, executor=FakeExecutor("deepseek_webtools"),
                                    webtools_transport=transport_for([httpx.Response(200, json=body)]))
    result = asyncio.run(external.image_search_for_run(
        context_for(system, run), mode="reverse_image", file_id=row["id"]))
    assert result["status"] == "not_executed" and result["results"] == []
    assert system.service.db.all("SELECT id FROM evidence_sources") == []


def test_image_evidence_finalize_keeps_candidates_visible(system, policy):
    run = system.run_with([])
    body = {"type": "images", "results": [
        {"type": "image_result", "url": "https://example.com/page",
         "properties": {"url": "https://cdn.example.com/a.jpg"}, "title": "A"}]}
    external = ExternalCapabilities(system.service, executor=FakeExecutor("brave_images"),
                                    brave_transport=transport_for([httpx.Response(200, json=body)]))
    result = asyncio.run(external.image_search_for_run(
        context_for(system, run), mode="text_search", query="q"))
    source_id = result["results"][0]["source_id"]
    finalized = system.service.evidence.finalize(run["id"], "answer", used_source_ids=[source_id])
    citation = next(item for item in finalized["citations"] if item["id"] == source_id)
    assert citation["attribution"] == "image_candidate"
    assert "图片检索候选" in finalized["text"]


def test_capability_source_reauthorizes_and_fails_closed(system, policy):
    run = system.run_with([])
    body = {"type": "images", "results": [
        {"type": "image_result", "url": "https://example.com/page",
         "properties": {"url": "https://cdn.example.com/a.jpg"}}]}
    external = ExternalCapabilities(system.service, executor=FakeExecutor("brave_images"),
                                    brave_transport=transport_for([httpx.Response(200, json=body)]))
    source_id = asyncio.run(external.image_search_for_run(
        context_for(system, run), mode="text_search", query="q"))["results"][0]["source_id"]
    system.service.evidence.get(source_id, system.user)  # policy call happened
    assert any(item[0] == source_id for item in sys.modules["app.capability_policy"].calls)
    # Revocation at click/read time is honored.
    def deny(service, row, user):
        raise AppError(404, "SOURCE_UNAVAILABLE", "该来源已撤销")
    sys.modules["app.capability_policy"].authorize_source_access = deny
    with pytest.raises(AppError):
        system.service.evidence.get(source_id, system.user)


def test_capability_source_without_policy_module_fails_closed(system, policy):
    run = system.run_with([])
    body = {"type": "images", "results": [
        {"type": "image_result", "url": "https://example.com/page",
         "properties": {"url": "https://cdn.example.com/a.jpg"}}]}
    external = ExternalCapabilities(system.service, executor=FakeExecutor("brave_images"),
                                    brave_transport=transport_for([httpx.Response(200, json=body)]))
    source_id = asyncio.run(external.image_search_for_run(
        context_for(system, run), mode="text_search", query="q"))["results"][0]["source_id"]
    saved = sys.modules.pop("app.capability_policy")
    try:
        with pytest.raises(AppError) as error:
            system.service.evidence.get(source_id, system.user)
        assert error.value.code == "SOURCE_UNAVAILABLE"
    finally:
        sys.modules["app.capability_policy"] = saved


def test_evidence_rejects_private_or_credential_image_links(system, policy):
    run = system.run_with([])
    for locator in (
        {"capability": "image_search", "content_type": "image_search_result",
         "url": "https://example.com/p", "image_url": "http://127.0.0.1/x.jpg"},
        {"capability": "image_search", "content_type": "image_search_result",
         "url": "https://example.com/p", "image_url": "https://cdn.example.com/x.jpg?access_token=1"},
    ):
        with pytest.raises(AppError):
            system.service.evidence.register(run["id"], "web", "bad", locator, "x",
                                             resource_type="web", resource_id=system.assistant["id"],
                                             original_url="https://example.com/p",
                                             tool_name="mcp__workspace__image_search")


def test_new_tool_risk_profiles_require_confirmation():
    for name in ("mcp__workspace__ocr", "mcp__workspace__image_search"):
        profile = tool_risk_profile(name)
        assert profile["approval_policy"] == "user_confirmation"
        assert profile["risk_level"] == "medium"
    assert public_provider("deepseek_api") == "managed_ocr"
    assert public_provider("deepseek_webtools") == "managed_image_search"
