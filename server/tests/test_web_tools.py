import asyncio
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from app.security import AppError
from app.web_tools import PageText, PublicNetworkBackend, WebTools, public_dns_answers, public_url


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/a", "http://127.0.0.1", "http://169.254.169.254/latest", "http://[::1]", "http://user:pass@example.com", "https://example.com:8443", "https://host.internal/", "http://192.168.1.1", "http://example.com\\@127.0.0.1"])
def test_public_url_rejects_private_and_ambiguous_targets(url):
    with pytest.raises(AppError):
        public_url(url)


def test_public_dns_is_validated_and_pinned(monkeypatch):
    async def scenario():
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock(return_value="stream")
        loop = asyncio.get_running_loop()
        resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
        monkeypatch.setattr(loop, "getaddrinfo", resolver)
        assert await backend.connect_tcp("example.com", 443, 3) == "stream"
        assert backend.backend.connect_tcp.call_args.args[0] == "93.184.216.34"
        assert resolver.await_count == 1
        resolver.return_value.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)))
        with pytest.raises(AppError):
            await backend.connect_tcp("mixed.example.com", 443, 3)
        assert backend.backend.connect_tcp.await_count == 1
    asyncio.run(scenario())


def test_page_text_excludes_executable_content():
    parser = PageText()
    parser.feed("<title>Source</title><script>ignored credential example</script><p>Yield &amp; throughput</p><style>.hide{}</style><p>95%</p>")
    assert "ignored" not in parser.text and "hide" not in parser.text
    assert "Yield & throughput" in parser.text and "95%" in parser.text
    assert "".join(parser.title_parts) == "Source"


def test_search_uses_real_response_sources_and_current_assistant_flag(monkeypatch):
    class Evidence:
        def register(self, *args, **kwargs):
            self.last = (args, kwargs)
            return {"id": "registered-source"}
    class Service:
        evidence = Evidence()
        enabled = True
        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "web_search_enabled": self.enabled}
    service = Service()
    fetch = AsyncMock(return_value=("https://www.bing.com/search", "application/rss+xml", b'<rss><channel><item><title>Real title</title><link>https://example.com/report</link><description>Measured result</description></item></channel></rss>'))
    monkeypatch.setattr("app.web_tools.fetch_public", fetch)
    async def scenario():
        result = await WebTools(service).search_for_run({"id": "u"}, {"id": "a"}, "run", "public query")
        assert result["results"][0]["citation"] == "[[source:registered-source]]"
        assert service.evidence.last[0][4] == "Measured result"
        assert service.evidence.last[0][3]["fetched_at"]
        service.enabled = False
        with pytest.raises(AppError):
            await WebTools(service).read_for_run({"id": "u"}, {"id": "a"}, "run", "https://example.com")
        assert fetch.await_count == 1
    asyncio.run(scenario())


def test_search_deduplicates_canonical_urls_and_marks_provider(monkeypatch):
    class Evidence:
        def __init__(self):
            self.items = []
        def register(self, *args, **kwargs):
            self.items.append((args, kwargs))
            return {"id": f"source-{len(self.items)}"}
    class Service:
        evidence = Evidence()
        settings = None
        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "web_search_enabled": True}
    monkeypatch.setenv("AGENT_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("AGENT_WEB_SEARCH_SEARXNG_URL", "https://search.example/search")
    fetch = AsyncMock(return_value=("https://search.example/search", "application/json", json_bytes({
        "results": [
            {"title": "First", "url": "https://example.com/report/?utm_source=x", "content": "a"},
            {"title": "Duplicate", "url": "https://EXAMPLE.com/report", "content": "b"},
            {"title": "Second", "url": "https://example.org/other", "content": "c"},
        ]
    })))
    monkeypatch.setattr("app.web_tools.fetch_public", fetch)
    async def scenario():
        result = await WebTools(Service()).search_for_run({"id": "u"}, {"id": "a"}, "run", "public query", limit=5)
        assert result["provider"] == "searxng" and result["fallback"] is False
        assert [item["url"] for item in result["results"]] == ["https://example.com/report", "https://example.org/other"]
        assert all(item["provider"] == "searxng" for item in result["results"])
        assert len(Service.evidence.items) == 2
    asyncio.run(scenario())


def test_optional_provider_without_endpoint_falls_back_to_bing(monkeypatch):
    class Evidence:
        def register(self, *args, **kwargs):
            return {"id": "source"}
    class Service:
        evidence = Evidence()
        settings = None
        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "web_search_enabled": True}
    monkeypatch.setenv("AGENT_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.delenv("AGENT_WEB_SEARCH_SEARXNG_URL", raising=False)
    monkeypatch.setattr(WebTools, "_bing", AsyncMock(return_value=[{"title": "Fallback", "url": "https://example.com", "excerpt": "ok"}]))
    async def scenario():
        result = await WebTools(Service()).search_for_run({"id": "u"}, {"id": "a"}, "run", "public query")
        assert result["provider"] == "bing_rss" and result["requested_provider"] == "searxng"
        assert result["fallback"] is True
    asyncio.run(scenario())


def test_optional_provider_transport_failure_falls_back_without_leaking_error(monkeypatch):
    class Evidence:
        def register(self, *args, **kwargs):
            return {"id": "source"}
    class Service:
        evidence = Evidence()
        settings = None
        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "web_search_enabled": True}
    monkeypatch.setenv("AGENT_WEB_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("AGENT_WEB_SEARCH_SEARXNG_URL", "https://search.example/search")
    monkeypatch.setattr("app.web_tools.fetch_public", AsyncMock(side_effect=httpx.ConnectError("offline")))
    monkeypatch.setattr(WebTools, "_bing", AsyncMock(return_value=[{"title": "Fallback", "url": "https://example.com", "excerpt": "ok"}]))
    async def scenario():
        result = await WebTools(Service()).search_for_run({"id": "u"}, {"id": "a"}, "run", "public query")
        assert result["provider"] == "bing_rss" and result["fallback"] is True
    asyncio.run(scenario())


def test_brave_provider_uses_header_without_putting_key_in_url(monkeypatch):
    class Evidence:
        def register(self, *args, **kwargs):
            return {"id": "source"}
    class Service:
        evidence = Evidence()
        settings = None
        def usable_assistant(self, assistant_id, user):
            return {"id": assistant_id, "web_search_enabled": True}
    monkeypatch.setenv("AGENT_WEB_SEARCH_PROVIDER", "brave")
    monkeypatch.setenv("AGENT_WEB_SEARCH_BRAVE_URL", "https://api.search.brave.com/res/v1/web/search")
    monkeypatch.setenv("AGENT_WEB_SEARCH_BRAVE_API_KEY", "secret-key")
    fetch = AsyncMock(return_value=("https://api.search.brave.com/res/v1/web/search", "application/json", json_bytes({
        "web": {"results": [{"title": "Brave result", "url": "https://example.com", "description": "ok"}]}
    })))
    monkeypatch.setattr("app.web_tools.fetch_public_headers", fetch)
    async def scenario():
        result = await WebTools(Service()).search_for_run({"id": "u"}, {"id": "a"}, "run", "public query")
        assert result["provider"] == "brave"
        called_url = fetch.await_args.args[0]
        assert "secret-key" not in called_url
        assert fetch.await_args.kwargs["headers"]["X-Subscription-Token"] == "secret-key"
    asyncio.run(scenario())


def json_bytes(value):
    """Keep provider fixture payloads compact while preserving Unicode support."""
    import json
    return json.dumps(value).encode("utf-8")


def test_fake_ip_dns_uses_independent_public_dns_and_pins_answer(monkeypatch):
    async def scenario():
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock(return_value="stream")
        resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.49", 443))])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        public = AsyncMock(return_value=["23.199.232.212", "23.199.232.199"])
        monkeypatch.setattr("app.web_tools.public_dns_answers", public)
        assert await backend.connect_tcp("www.bing.com", 443, 3) == "stream"
        public.assert_awaited_once_with("www.bing.com", timeout=3)
        assert backend.backend.connect_tcp.call_args.args[0] == "23.199.232.212"
        assert resolver.await_count == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('addresses', [
    ['10.0.0.1'], ['127.0.0.1'], ['169.254.169.254'], ['198.18.0.49', '10.0.0.1'],
    ['198.18.0.49', '93.184.216.34'], ['::1'], ['224.0.0.1'],
])
def test_other_private_or_mixed_answers_never_use_public_dns_fallback(monkeypatch, addresses):
    async def scenario():
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock()
        resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses])
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
        public = AsyncMock(return_value=['93.184.216.34'])
        monkeypatch.setattr('app.web_tools.public_dns_answers', public)
        with pytest.raises(AppError):
            await backend.connect_tcp('restricted.example.com', 443, 3)
        public.assert_not_awaited()
        backend.backend.connect_tcp.assert_not_awaited()
    asyncio.run(scenario())


def test_fake_ip_literal_is_never_resolved_as_public_hostname(monkeypatch):
    async def scenario():
        backend = PublicNetworkBackend()
        backend.backend.connect_tcp = AsyncMock()
        public = AsyncMock()
        monkeypatch.setattr('app.web_tools.public_dns_answers', public)
        with pytest.raises(AppError):
            public_url('https://198.18.0.49/')
        with pytest.raises(AppError):
            await backend.connect_tcp('198.18.0.49', 443, 3)
        public.assert_not_awaited()
        backend.backend.connect_tcp.assert_not_awaited()
    asyncio.run(scenario())


def test_doh_fixed_https_bootstrap_and_public_answers(monkeypatch):
    requests, transports = [], []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'Status': 0, 'Answer': [
            {'type': 5, 'data': 'cdn.example.com.'}, {'type': 1, 'data': '93.184.216.34'},
            {'type': 1, 'data': '93.184.216.34'},
        ]})
    def transport(**kwargs):
        transports.append(kwargs)
        return httpx.MockTransport(handler)
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', transport)
    assert asyncio.run(public_dns_answers('example.com')) == ['93.184.216.34']
    assert requests[0].url.scheme == 'https' and requests[0].url.host == '1.1.1.1'
    assert requests[0].url.params['name'] == 'example.com'
    assert requests[0].url.params['edns_client_subnet'] == '0.0.0.0/0'
    assert transports == [{'retries': 0, 'trust_env': False}]


@pytest.mark.parametrize('addresses', [['10.0.0.1'], ['169.254.169.254'], ['198.18.0.49'], ['93.184.216.34', '127.0.0.1'], ['224.0.0.1']])
def test_doh_private_and_mixed_answers_remain_denied(monkeypatch, addresses):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 1, 'data': address} for address in addresses]})
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', lambda **kwargs: httpx.MockTransport(handler))
    with pytest.raises(AppError) as error:
        asyncio.run(public_dns_answers('attacker.example.com'))
    assert error.value.code == 'WEB_URL_DENIED'
    assert len(requests) == 1  # No second resolver can override a private answer.


def test_doh_response_limit_and_redirect_denial(monkeypatch):
    requests = []
    def large(request):
        return httpx.Response(200, content=b' ' * 65537)
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', lambda **kwargs: httpx.MockTransport(large))
    with pytest.raises(AppError) as error:
        asyncio.run(public_dns_answers('example.com'))
    assert error.value.code == 'WEB_DNS_UNAVAILABLE'
    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest'})
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', lambda **kwargs: httpx.MockTransport(redirect))
    with pytest.raises(AppError):
        asyncio.run(public_dns_answers('example.com'))
    assert [request.url.host for request in requests] == ['1.1.1.1', '8.8.8.8']


def test_doh_total_timeout_is_bounded_for_each_resolver(monkeypatch):
    requests = []
    async def slow(request):
        requests.append(request)
        await asyncio.sleep(1)
        return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 1, 'data': '93.184.216.34'}]})
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', lambda **kwargs: httpx.MockTransport(slow))
    with pytest.raises(AppError) as error:
        asyncio.run(public_dns_answers('example.com', timeout=.01))
    assert error.value.code == 'WEB_DNS_UNAVAILABLE'
    assert len(requests) == 2


def test_public_fetch_redirect_to_metadata_is_rejected_before_request(monkeypatch):
    from types import SimpleNamespace
    from app.web_tools import fetch_public
    requests = []
    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data'})
    def transport(**kwargs):
        value = httpx.MockTransport(redirect)
        value._pool = SimpleNamespace(_network_backend=None)
        return value
    monkeypatch.setattr('app.web_tools.httpx.AsyncHTTPTransport', transport)
    with pytest.raises(AppError) as error:
        asyncio.run(fetch_public('https://example.com'))
    assert error.value.code == 'WEB_URL_DENIED'
    assert len(requests) == 1
