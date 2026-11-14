import asyncio
import json
import sys
import time
import types
from types import SimpleNamespace

import httpx
import pytest

from app.deepseek_api import DeepSeekAPI, MAX_SEARCH_QUERY
from app.security import AppError
from app.web_tools import WebTools
from app.knowledge import KnowledgeService


def transport_for(responses, requests):
    def handler(request):
        requests.append(request)
        value = responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value
    return httpx.MockTransport(handler)


def test_search_matches_official_payload_and_normalizes_public_results():
    requests = []
    response = httpx.Response(200, json={"results": [{
        "title": "A result", "url": "https://example.com/a/?utm_source=x",
        "snippet": "Short evidence", "summary": "Long evidence", "authority": 3,
        "rerank_score": 2.1, "date": "2026-11-14T00:00:00Z",
    }]})
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key", transport=transport_for([response], requests))
    result = asyncio.run(client.search("工业安全", count=3, original_question="工业安全规范", need_content=True))
    assert result[0]["url"] == "https://example.com/a"
    assert result[0]["excerpt"] == "Short evidence"
    payload = json.loads(requests[0].content)
    assert payload == {"query": "工业安全", "count": 3, "need_content": True, "ori_question": "工业安全规范"}
    assert requests[0].url == "https://api.deepseek.com/v1/tools/search"
    assert requests[0].headers["authorization"] == "Bearer test-key"


def test_search_rejects_overlong_query_without_truncating_or_calling():
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key", transport=transport_for([], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("x" * (MAX_SEARCH_QUERY + 1)))
    assert error.value.code == "WEB_QUERY_INVALID"
    assert not requests
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("query", original_question="x" * (MAX_SEARCH_QUERY + 1)))
    assert error.value.code == "WEB_QUERY_INVALID"


def test_one_attempt_per_call_and_stable_non_retryable_errors():
    # The shared executor owns retry/fallback; the adapter must never send a
    # second request on its own (OCR may already be billed after receipt).
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                         transport=transport_for([httpx.Response(429)], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("query"))
    assert error.value.code == "EXTERNAL_TOOL_RATE_LIMITED" and error.value.status == 429
    assert len(requests) == 1
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                         transport=transport_for([httpx.Response(402)], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("query"))
    assert error.value.code == "EXTERNAL_TOOL_BALANCE" and error.value.status == 402
    assert len(requests) == 1
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                         transport=transport_for([httpx.Response(401)], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("query"))
    assert error.value.code == "EXTERNAL_TOOL_UNAUTHORIZED" and len(requests) == 1


def test_search_requires_results_key_but_allows_explicit_empty_array():
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                         transport=transport_for([httpx.Response(200, json={"results": []})], requests))
    assert asyncio.run(client.search("query")) == []
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                         transport=transport_for([httpx.Response(200, json={"items": []})], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("query"))
    assert error.value.code == "EXTERNAL_TOOL_RESPONSE_INVALID"


def test_ocr_uses_multipart_file_and_returns_bounded_pages(tmp_path):
    source = tmp_path / "scan.png"
    source.write_bytes(b"fake image")
    requests = []
    response = httpx.Response(200, json={"id": "ocr-test", "page_count": 1,
                                         "pages": [{"index": 0, "text": "标题", "finish_reason": "stop"}],
                                         "usage": {"total_tokens": 4}})
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key", transport=transport_for([response], requests))
    result = asyncio.run(client.ocr(source, filename="scan.png"))
    assert result["pages"] == [{"page": 1, "text": "标题", "extraction": "managed-ocr",
                                "finish_reason": "stop", "complete": True, "truncated": False}]
    assert result["partial"] is False and result["empty"] is False and result["page_count"] == 1
    body = requests[0].content
    assert b'name="purpose"' in body and b'rich_layout_recognition' in body
    assert b'name="file"' in body and b'fake image' in body


def test_ocr_http_200_error_envelope_is_not_a_success(tmp_path):
    source = tmp_path / "scan.png"
    source.write_bytes(b"fake image")
    requests = []
    body = {"id": "ocr-test", "page_count": 1, "pages": [],
            "error": {"code": "upstream_timeout", "message": "provider detail must not leak"}}
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                         transport=transport_for([httpx.Response(200, json=body)], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.ocr(source, filename="scan.png"))
    assert error.value.code == "EXTERNAL_TOOL_TIMEOUT" and len(requests) == 1
    assert "provider detail" not in error.value.message


def test_ocr_page_integrity_is_explicit_partial_not_success(tmp_path):
    source = tmp_path / "scan.png"
    source.write_bytes(b"fake image")
    cases = [
        # missing page 2 of 3
        {"page_count": 3, "pages": [{"index": 0, "text": "a"}, {"index": 2, "text": "c"}]},
        # duplicate index
        {"page_count": 2, "pages": [{"index": 0, "text": "a"}, {"index": 0, "text": "b"}]},
        # truncated page
        {"page_count": 1, "pages": [{"index": 0, "text": "partial", "finish_reason": "length"}]},
    ]
    for case in cases:
        requests = []
        client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                             transport=transport_for([httpx.Response(200, json=case)], requests))
        result = asyncio.run(client.ocr(source, filename="scan.png"))
        assert result["partial"] is True and result["empty"] is False, case


def test_ocr_all_empty_text_is_explicit_empty():
    requests = []
    body = {"page_count": 1, "pages": [{"index": 0, "text": ""}]}
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key",
                         transport=transport_for([httpx.Response(200, json=body)], requests))
    result = asyncio.run(client.ocr_bytes(b"fake image", filename="scan.png", mime_type="image/png"))
    assert result["empty"] is True and result["partial"] is False
    assert len(requests) == 1


def test_fetch_validates_target_before_sending_and_returns_markdown():
    requests = []
    response = httpx.Response(200, json={"title": "Page", "content": "# Body"})
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key", transport=transport_for([response], requests))
    result = asyncio.run(client.fetch("https://example.com/page"))
    assert result == {"title": "Page", "url": "https://example.com/page", "content": "# Body"}
    assert json.loads(requests[0].content) == {"url": "https://example.com/page"}
    with pytest.raises(AppError) as error:
        asyncio.run(client.fetch("http://127.0.0.1/metadata"))
    assert error.value.code == "WEB_URL_DENIED"
    assert len(requests) == 1


def test_missing_key_is_fail_closed_without_network():
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="", transport=transport_for([], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("query"))
    assert error.value.code == "EXTERNAL_TOOL_UNAVAILABLE"
    assert not requests


def test_non_official_endpoint_is_rejected():
    client = DeepSeekAPI(base_url="https://attacker.example.invalid", api_key="test-key", transport=transport_for([], []))
    with pytest.raises(AppError):
        asyncio.run(client.search("query"))


def _fake_executor(monkeypatch, *, used, requested):
    """Drive WebTools' result mapping without a live capability policy.

    The route selection itself belongs to the capability owner; this test keeps
    the public provider-name mapping honest even after WebTools moved behind
    the shared executor.
    """
    module = types.ModuleType("app.capability_execution")

    def RunContext(**kwargs):
        return SimpleNamespace(**kwargs)

    class Executor:
        def __init__(self, service):
            self.service = service

        async def execute_for_run(self, context, capability, arguments, operation, *,
                                  target_url=None, external_upload=False):
            value = await operation(used, time.monotonic() + 10, context.recheck)
            return SimpleNamespace(value=value, provider=used, requested_provider=requested,
                                   fallback=used != requested, attempts=1, usage_id="usage-1",
                                   settings_revision=1)

        def effective(self, context, capability, arguments):
            return {"domains": {"mode": "public", "hosts": []}}

    module.RunContext = RunContext
    module.new_call_id = lambda: "cap-test-call"
    module.CapabilityExecutor = Executor
    monkeypatch.setitem(sys.modules, "app.capability_execution", module)


class _WebService:
    def __init__(self, settings, evidence):
        self.settings, self.evidence = settings, evidence

    def require(self, table, item_id, user=None):
        return {"id": item_id, "conversation_id": "conversation"}

    def authorize_tool(self, conversation_id, tool, arguments):
        return True

    def usable_assistant(self, assistant_id, user):
        return {"id": assistant_id, "web_search_enabled": True}


def test_web_tools_hides_upstream_name_and_falls_back_when_disabled(monkeypatch):
    class Evidence:
        def register(self, *args, **kwargs):
            return {"id": "source-1"}
    settings = SimpleNamespace(
        web_search_provider="deepseek_api", web_search_searxng_url="", web_search_brave_url="",
        web_search_brave_api_key="", web_search_fallback_bing=True,
        deepseek_api_enabled=False, deepseek_api_key="", deepseek_api_base_url="https://api.deepseek.com",
        deepseek_api_timeout_seconds=30,
    )
    _fake_executor(monkeypatch, used="bing_rss", requested="deepseek_api")
    monkeypatch.setattr(WebTools, "_bing", lambda self, query, deadline=None: asyncio.sleep(0, result=[{"title": "Fallback", "url": "https://example.com", "excerpt": "ok"}]))
    result = asyncio.run(WebTools(_WebService(settings, Evidence())).search_for_run({"id": "u"}, {"id": "a"}, "run", "query"))
    assert result["provider"] == "bing_rss" and result["requested_provider"] == "managed_search"
    assert "deepseek" not in json.dumps(result).lower()


def test_web_tools_hides_upstream_name_in_evidence_for_enabled_route(monkeypatch):
    class Evidence:
        def __init__(self):
            self.last = None
        def register(self, *args, **kwargs):
            self.last = (args, kwargs)
            return {"id": "source-1"}
    settings = SimpleNamespace(
        web_search_provider="deepseek_api", web_search_searxng_url="", web_search_brave_url="",
        web_search_brave_api_key="", web_search_fallback_bing=False,
        deepseek_api_enabled=True, deepseek_api_key="test-key", deepseek_api_base_url="https://api.deepseek.com",
        deepseek_api_timeout_seconds=30,
    )
    _fake_executor(monkeypatch, used="deepseek_api", requested="deepseek_api")
    monkeypatch.setattr(WebTools, "_deepseek", lambda self, query, limit, deadline=None: asyncio.sleep(0, result=[{"title": "Result", "url": "https://example.com", "excerpt": "ok"}]))
    service = _WebService(settings, Evidence())
    result = asyncio.run(WebTools(service).search_for_run({"id": "u"}, {"id": "a"}, "run", "query"))
    assert result["provider"] == "managed_search" and result["requested_provider"] == "managed_search"
    assert service.evidence.last[0][3]["provider"] == "managed_search"
    assert "deepseek" not in json.dumps(result).lower() and "deepseek" not in json.dumps(service.evidence.last).lower()


def test_knowledge_ocr_fallback_is_explicit_and_parser_compatible(tmp_path, monkeypatch, request):
    from app.capability_settings import CAPABILITY_KINDS, CapabilitySettings, CapabilityUpdate
    from app.config import Settings
    from app.service import Service
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"sample")
    settings = Settings(data_dir=tmp_path / "data", provider_settings="",
        deepseek_api_enabled=True, deepseek_api_ocr_enabled=True,
        deepseek_api_key="test-key", deepseek_api_base_url="https://api.deepseek.com",
        deepseek_api_timeout_seconds=30,
    )
    service = Service(settings)
    request.addfinalizer(service.db.close)
    admin = service.create_user({"username": "ocr-admin", "display_name": "OCR admin", "role": "admin",
                                 "password": "Synthetic-pass-for-test-123"})
    api = CapabilitySettings(service)
    data = {key: api.effective()[key] for key in ("revision", *CAPABILITY_KINDS)}
    data["ocr"]["enabled"] = True
    data["ocr"]["egress"]["upload"] = "allow"
    api.save(admin, CapabilityUpdate(**data))
    knowledge = KnowledgeService.__new__(KnowledgeService)
    knowledge.service = service
    seen = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        async def ocr(self, path, **kwargs):
            assert path == source and kwargs["filename"] == "scan.pdf"
            return {"pages": [{"page": 1, "text": "扫描内容", "extraction": "managed-ocr"}]}

    monkeypatch.setattr("app.knowledge.DeepSeekAPI", FakeClient)
    assert knowledge._deepseek_ocr_pages(source, "scan.pdf") == [{"page": 1, "text": "扫描内容", "extraction": "managed-ocr"}]
    assert seen["api_key"] == "test-key"

    settings.deepseek_api_enabled = False
    with pytest.raises(AppError) as error:
        knowledge._deepseek_ocr_pages(source, "scan.pdf")
    assert error.value.code == "OCR_REQUIRED"
