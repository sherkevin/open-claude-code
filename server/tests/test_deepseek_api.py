import asyncio
import json
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


def test_retryable_status_retries_once_but_balance_error_does_not():
    requests = []
    ok = httpx.Response(200, json={"results": []})
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key", transport=transport_for([httpx.Response(429), ok], requests))
    assert asyncio.run(client.search("query")) == []
    assert len(requests) == 2
    requests = []
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key", transport=transport_for([httpx.Response(402)], requests))
    with pytest.raises(AppError) as error:
        asyncio.run(client.search("query"))
    assert error.value.code == "EXTERNAL_TOOL_UNAVAILABLE" and len(requests) == 1


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


def test_ocr_uses_multipart_file_and_returns_bounded_pages(tmp_path):
    source = tmp_path / "scan.png"
    source.write_bytes(b"fake image")
    requests = []
    response = httpx.Response(200, json={"id": "ocr-test", "pages": [{"index": 0, "text": "标题", "finish_reason": "stop"}], "usage": {"total_tokens": 4}})
    client = DeepSeekAPI(base_url="https://api.deepseek.com", api_key="test-key", transport=transport_for([response], requests))
    result = asyncio.run(client.ocr(source, filename="scan.png"))
    assert result["pages"] == [{"page": 1, "text": "标题", "extraction": "managed-ocr", "finish_reason": "stop"}]
    body = requests[0].content
    assert b'name="purpose"' in body and b'rich_layout_recognition' in body
    assert b'name="file"' in body and b'fake image' in body


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


def test_web_tools_hides_upstream_name_and_falls_back_when_disabled(monkeypatch):
    class Evidence:
        def register(self, *args, **kwargs):
            return {"id": "source-1"}
    class Service:
        evidence = Evidence()
        settings = SimpleNamespace(
            web_search_provider="deepseek_api", web_search_searxng_url="", web_search_brave_url="",
            web_search_brave_api_key="", web_search_fallback_bing=True,
            deepseek_api_enabled=False, deepseek_api_key="", deepseek_api_base_url="https://api.deepseek.com",
            deepseek_api_timeout_seconds=30,
        )
        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "web_search_enabled": True}
    monkeypatch.setattr(WebTools, "_bing", lambda self, query: asyncio.sleep(0, result=[{"title": "Fallback", "url": "https://example.com", "excerpt": "ok"}]))
    result = asyncio.run(WebTools(Service()).search_for_run({"id": "u"}, {"id": "a"}, "run", "query"))
    assert result["provider"] == "bing_rss" and result["requested_provider"] == "managed_search"
    assert "deepseek" not in json.dumps(result).lower()


def test_web_tools_hides_upstream_name_in_evidence_for_enabled_route(monkeypatch):
    class Evidence:
        def __init__(self):
            self.last = None
        def register(self, *args, **kwargs):
            self.last = (args, kwargs)
            return {"id": "source-1"}
    class Service:
        evidence = Evidence()
        settings = SimpleNamespace(
            web_search_provider="deepseek_api", web_search_searxng_url="", web_search_brave_url="",
            web_search_brave_api_key="", web_search_fallback_bing=False,
            deepseek_api_enabled=True, deepseek_api_key="test-key", deepseek_api_base_url="https://api.deepseek.com",
            deepseek_api_timeout_seconds=30,
        )
        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "web_search_enabled": True}
    monkeypatch.setattr(WebTools, "_deepseek", lambda self, query, limit: asyncio.sleep(0, result=[{"title": "Result", "url": "https://example.com", "excerpt": "ok"}]))
    service = Service()
    result = asyncio.run(WebTools(service).search_for_run({"id": "u"}, {"id": "a"}, "run", "query"))
    assert result["provider"] == "managed_search" and result["requested_provider"] == "managed_search"
    assert service.evidence.last[0][3]["provider"] == "managed_search"
    assert "deepseek" not in json.dumps(result).lower() and "deepseek" not in json.dumps(service.evidence.last).lower()


def test_knowledge_ocr_fallback_is_explicit_and_parser_compatible(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"sample")
    settings = SimpleNamespace(
        deepseek_api_enabled=True, deepseek_api_ocr_enabled=True,
        deepseek_api_key="test-key", deepseek_api_base_url="https://api.deepseek.com",
        deepseek_api_timeout_seconds=30,
    )
    service = SimpleNamespace(settings=settings)
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
