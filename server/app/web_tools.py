"""Public web retrieval with bounded reads, pinned public DNS and source records."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpcore
import httpx
import anyio
from httpcore._backends.auto import AutoBackend

from .db import now
from .deepseek_api import DeepSeekAPI, PUBLIC_PROVIDER_NAME
from .security import AppError

PROXY_FAKE_IPS = ipaddress.ip_network("198.18.0.0/15")
# Fixed public-IP HTTPS endpoints avoid resolving the resolver through fake DNS.
# TLS certificates are verified against each literal endpoint address.
DOH_ENDPOINTS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve")
SEARCH_PROVIDERS = {"bing_rss", "searxng", "brave"}
TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "dclid", "mc_cid", "mc_eid"})
MAX_SEARCH_RESULTS = 8
MAX_SEARCH_RESPONSE_BYTES = 500_000


def global_unicast(value):
    address = ipaddress.ip_address(value)
    return address.is_global and not (address.is_multicast or address.is_reserved or address.is_unspecified)


async def public_dns_answers(hostname, timeout=6):
    """Public DNS fallback for an all-198.18/15 proxy fake-IP response only.

    The public resolver receives the requested hostname, never page content,
    knowledge documents or credentials. Private answers remain a hard failure.
    """
    for endpoint in DOH_ENDPOINTS:
        try:
            with anyio.fail_after(timeout):
                transport = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
                async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=timeout,
                                            follow_redirects=False, headers={"Accept": "application/dns-json"}) as client:
                    async with client.stream("GET", endpoint, params={"name": hostname, "type": "A", "edns_client_subnet": "0.0.0.0/0"}) as response:
                        if response.status_code != 200:
                            continue
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > 65536:
                                raise AppError(502, "WEB_DNS_UNAVAILABLE", "公开域名解析结果过大")
            document = json.loads(body)
            if not isinstance(document, dict) or document.get("Status") != 0 or not isinstance(document.get("Answer", []), list):
                continue
            answers = [item.get("data") for item in document.get("Answer", []) if isinstance(item, dict) and item.get("type") in {1, 28}]
            if not answers:
                continue
            if len(answers) > 100 or any(not isinstance(value, str) or not global_unicast(value) for value in answers):
                raise AppError(422, "WEB_URL_DENIED", "网页地址指向非公开网络")
            return list(dict.fromkeys(answers))
        except AppError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
            continue
    raise AppError(502, "WEB_DNS_UNAVAILABLE", "公开域名解析暂时不可用，请稍后重试")


def public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (len(value) > 4096 or parsed.scheme not in {"http", "https"}
                or not parsed.hostname or parsed.username or parsed.password
                or port not in {None, 80, 443} or "\\" in value
                or any(ord(char) < 32 for char in value)):
            raise ValueError()
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not global_unicast(address):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise AppError(422, "WEB_URL_DENIED", "只能读取公开 HTTP 或 HTTPS 网页") from None


class PublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Resolve once, validate all addresses, connect to the validated IP.

    httpcore retains the original hostname for TLS and Host, preventing the
    validation/request DNS race without weakening certificate verification.
    """
    def __init__(self):
        self.backend = AutoBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        hostname = host.decode() if isinstance(host, bytes) else host
        try:
            try:
                literal = ipaddress.ip_address(hostname)
            except ValueError:
                literal = None
            if literal is not None and not global_unicast(literal):
                raise AppError(422, "WEB_URL_DENIED", "网页地址指向非公开网络")
            resolved = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(hostname, port, type=socket.SOCK_STREAM),
                timeout=timeout or 15,
            )
            addresses = list(dict.fromkeys(item[4][0] for item in resolved))
            # Some controlled proxies synthesize 198.18/15 for every public
            # hostname. Do not connect to those private fake IPs. Resolve the
            # hostname independently, validate all public answers and pin one.
            if literal is None and addresses and all(ipaddress.ip_address(address) in PROXY_FAKE_IPS for address in addresses):
                addresses = await public_dns_answers(hostname, timeout=min(timeout or 6, 6))
            if not addresses or any(not global_unicast(address) for address in addresses):
                raise AppError(422, "WEB_URL_DENIED", "网页地址指向非公开网络")
            # No second hostname resolution: the transport connects to this IP.
            return await self.backend.connect_tcp(addresses[0], port, timeout, local_address, socket_options)
        except AppError:
            raise
        except (OSError, ValueError, asyncio.TimeoutError) as exc:
            raise httpcore.ConnectError("Public hostname resolution failed") from exc

    async def connect_unix_socket(self, *args, **kwargs):
        raise AppError(422, "WEB_URL_DENIED", "不支持本地套接字")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title_parts, self.suppressed = [], [], []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.suppressed.append(tag)
        if tag == "title":
            self.in_title = True
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.suppressed and tag == self.suppressed[-1]:
            self.suppressed.pop()
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)
            if self.in_title:
                self.title_parts.append(data)

    @property
    def text(self):
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[^\S\n]+", " ", "".join(self.parts))).strip()


async def fetch_public(url: str, *, max_bytes: int = 2_000_000, deadline: float | None = None,
                       recheck=None) -> tuple[str, str, bytes]:
    try:
        budget = 40 if deadline is None else max(0.1, deadline - time.monotonic())
        with anyio.fail_after(min(40, budget)):
            return await _fetch_public(url, max_bytes=max_bytes, recheck=recheck)
    except TimeoutError:
        raise AppError(504, "WEB_TIMEOUT", "网页读取超时，请尝试其他来源") from None


async def fetch_public_headers(url: str, *, headers: dict[str, str], max_bytes: int = MAX_SEARCH_RESPONSE_BYTES,
                               deadline: float | None = None, credentialed: bool = False) -> tuple[str, str, bytes]:
    """Fetch a public URL with bounded headers and the same SSRF checks.

    ``credentialed`` requests are restricted to HTTPS and refuse every
    redirect, so an upstream 30x can never forward a provider authorization
    header to another public host or downgrade the scheme to plain HTTP.
    """
    try:
        budget = 40 if deadline is None else max(0.1, deadline - time.monotonic())
        with anyio.fail_after(min(40, budget)):
            url = public_url(url)
            if credentialed and urlsplit(url).scheme != "https":
                raise AppError(422, "WEB_URL_DENIED", "带凭据的搜索请求必须使用 HTTPS")
            transport = httpx.AsyncHTTPTransport(retries=0)
            transport._pool._network_backend = PublicNetworkBackend()
            safe_headers = {str(key): str(value)[:4096] for key, value in headers.items()}
            async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=15,
                                        follow_redirects=False, headers=safe_headers) as client:
                for _ in range(5):
                    async with client.stream("GET", url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if credentialed:
                                raise AppError(502, "WEB_SEARCH_PROVIDER_FAILED", "搜索服务返回了不允许的重定向")
                            url = public_url(urljoin(url, response.headers.get("location", "")))
                            continue
                        if response.status_code >= 400:
                            raise AppError(502, "WEB_SEARCH_PROVIDER_FAILED", f"搜索服务返回 HTTP {response.status_code}")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > max_bytes:
                                raise AppError(422, "WEB_SEARCH_RESPONSE_TOO_LARGE", "搜索服务返回内容过大")
                        return url, response.headers.get("content-type", ""), bytes(body)
            raise AppError(422, "WEB_REDIRECT_LIMIT", "搜索服务重定向次数过多")
    except TimeoutError:
        raise AppError(504, "WEB_SEARCH_TIMEOUT", "搜索服务响应超时，请稍后重试") from None


async def _fetch_public(url: str, *, max_bytes: int, recheck=None) -> tuple[str, str, bytes]:
    url = public_url(url)
    transport = httpx.AsyncHTTPTransport(retries=0)
    # Pinned httpx 0.28.x/httpcore 1.x integration. The pool owns TLS correctly.
    transport._pool._network_backend = PublicNetworkBackend()
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=15,
                                headers={"User-Agent": "BetterAI-Research/0.4 (+public-page-reader)", "Accept": "text/html,application/rss+xml,text/plain"}) as client:
        for _ in range(5):
            # The current domain/authorization policy is evaluated for every
            # hop *before* the request leaves the process, so a redirect cannot
            # turn an allowed first request into a denied outbound connection.
            if recheck is not None:
                recheck(url)
            async with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = public_url(urljoin(url, response.headers.get("location", "")))
                    continue
                if response.status_code >= 400:
                    raise AppError(502, "WEB_FETCH_FAILED", f"网页返回 HTTP {response.status_code}，可尝试其他来源")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise AppError(422, "WEB_PAGE_TOO_LARGE", "网页内容过大，请改用具体的文本页面")
                return url, response.headers.get("content-type", ""), bytes(body)
    raise AppError(422, "WEB_REDIRECT_LIMIT", "网页重定向次数过多")


def canonical_result_url(value: str) -> str | None:
    """Normalize a result URL for safe deterministic de-duplication."""
    try:
        value = public_url(value)
        parts = urlsplit(value)
        pairs = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                 if not key.lower().startswith("utm_") and key.lower() not in TRACKING_QUERY_KEYS]
        path = parts.path or "/"
        if path != "/":
            path = path.rstrip("/") or "/"
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(pairs), ""))
    except (AppError, ValueError, TypeError):
        return None


def _result_item(raw: dict, *, fallback_title: str = "") -> dict | None:
    """Convert provider-specific result fields into a small trusted shape."""
    if not isinstance(raw, dict):
        return None
    url = canonical_result_url(raw.get("url") or raw.get("link") or "")
    if not url:
        return None
    title = str(raw.get("title") or raw.get("name") or fallback_title or url)[:500]
    excerpt = str(raw.get("excerpt") or raw.get("snippet") or raw.get("content") or "")[:3000]
    published = raw.get("published_at") or raw.get("publishedDate") or raw.get("date")
    return {"title": title, "url": url, "excerpt": excerpt,
            "published_at": str(published)[:100] if published else None}


class WebTools:
    def __init__(self, service):
        self.service = service

    def _provider_config(self) -> tuple[str, str, str, str, bool]:
        settings = getattr(self.service, "settings", None)
        provider = getattr(settings, "web_search_provider", None) or "bing_rss"
        searx_url = getattr(settings, "web_search_searxng_url", "") or ""
        brave_url = getattr(settings, "web_search_brave_url", "") or ""
        brave_key = getattr(settings, "web_search_brave_api_key", "") or ""
        fallback = bool(getattr(settings, "web_search_fallback_bing", True))
        if not settings:
            # Lightweight test doubles do not carry Settings; environment
            # lookup is intentionally limited to this provider boundary.
            import os
            provider = os.getenv("AGENT_WEB_SEARCH_PROVIDER", provider).strip().lower()
            searx_url = os.getenv("AGENT_WEB_SEARCH_SEARXNG_URL", searx_url).strip()
            brave_url = os.getenv("AGENT_WEB_SEARCH_BRAVE_URL", brave_url).strip()
            brave_key = os.getenv("AGENT_WEB_SEARCH_BRAVE_API_KEY", brave_key).strip()
            fallback = os.getenv("AGENT_WEB_SEARCH_FALLBACK_BING", "true").lower() == "true"
        return provider, searx_url, brave_url, brave_key, fallback

    def _deepseek_client(self) -> DeepSeekAPI | None:
        settings = getattr(self.service, "settings", None)
        if settings is None:
            import os
            enabled = os.getenv("AGENT_DEEPSEEK_API_ENABLED", "false").lower() == "true"
            base_url = os.getenv("AGENT_DEEPSEEK_API_BASE_URL", "https://api.deepseek.com").strip()
            api_key = os.getenv("AGENT_DEEPSEEK_API_KEY", "").strip()
            timeout = float(os.getenv("AGENT_DEEPSEEK_API_TIMEOUT_SECONDS", "30"))
        else:
            enabled = bool(getattr(settings, "deepseek_api_enabled", False))
            base_url = str(getattr(settings, "deepseek_api_base_url", "https://api.deepseek.com") or "").strip()
            api_key = str(getattr(settings, "deepseek_api_key", "") or "").strip()
            timeout = float(getattr(settings, "deepseek_api_timeout_seconds", 30) or 30)
        if not enabled or not api_key:
            return None
        return DeepSeekAPI(base_url=base_url, api_key=api_key, timeout_seconds=timeout)

    async def _deepseek(self, query: str, limit: int, deadline: float | None = None) -> list[dict]:
        client = self._deepseek_client()
        if client is None:
            raise AppError(503, "WEB_SEARCH_PROVIDER_UNAVAILABLE", "当前网页搜索服务未配置")
        if deadline is None:
            return await client.search(query, count=limit, need_content=False)
        async with anyio.fail_after(max(0.1, deadline - time.monotonic())):
            return await client.search(query, count=limit, need_content=False)

    async def _bing(self, query: str, deadline: float | None = None) -> list[dict]:
        url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})
        try:
            _, _, body = await fetch_public(url, max_bytes=MAX_SEARCH_RESPONSE_BYTES, deadline=deadline)
        except AppError:
            raise
        except (httpx.HTTPError, httpcore.NetworkError, OSError, TimeoutError) as exc:
            raise AppError(502, "WEB_SEARCH_UNAVAILABLE", "搜索服务暂时不可用，请稍后重试") from exc
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise AppError(502, "WEB_SEARCH_UNAVAILABLE", "搜索服务暂时未返回可用结果，请稍后重试")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise AppError(502, "WEB_SEARCH_UNAVAILABLE", "搜索服务暂时未返回可用结果，请稍后重试") from None
        results = []
        for item in root.findall("./channel/item")[:MAX_SEARCH_RESULTS]:
            parser = PageText()
            parser.feed(item.findtext("description", ""))
            result = _result_item({"title": item.findtext("title", ""),
                                   "url": item.findtext("link", ""), "excerpt": parser.text})
            if result:
                results.append(result)
        return results

    async def _searxng(self, endpoint: str, query: str, limit: int, deadline: float | None = None) -> list[dict]:
        if not endpoint:
            raise AppError(503, "WEB_SEARCH_PROVIDER_UNAVAILABLE", "未配置公共 SearXNG 搜索地址")
        endpoint = public_url(endpoint.rstrip("/"))
        separator = "&" if "?" in endpoint else "?"
        url = endpoint + separator + urlencode({"q": query, "format": "json", "language": "all", "safesearch": 1})
        try:
            _, _, body = await fetch_public(url, max_bytes=MAX_SEARCH_RESPONSE_BYTES, deadline=deadline)
        except AppError:
            raise
        except (httpx.HTTPError, httpcore.NetworkError, OSError, TimeoutError) as exc:
            raise AppError(502, "WEB_SEARCH_PROVIDER_FAILED", "SearXNG 搜索暂时不可用，请稍后重试") from exc
        try:
            document = json.loads(body)
        except (TypeError, ValueError):
            raise AppError(502, "WEB_SEARCH_PROVIDER_FAILED", "SearXNG 未返回可用结果") from None
        rows = document.get("results", []) if isinstance(document, dict) else []
        return [item for raw in rows[:MAX_SEARCH_RESULTS] if (item := _result_item(raw))][:limit]

    async def _brave(self, endpoint: str, key: str, query: str, limit: int, deadline: float | None = None) -> list[dict]:
        if not endpoint or not key:
            raise AppError(503, "WEB_SEARCH_PROVIDER_UNAVAILABLE", "未配置 Brave 搜索地址或授权密钥")
        endpoint = public_url(endpoint)
        separator = "&" if "?" in endpoint else "?"
        url = endpoint + separator + urlencode({"q": query, "count": limit, "safesearch": "moderate"})
        try:
            _, _, body = await fetch_public_headers(
                url, headers={"Accept": "application/json", "X-Subscription-Token": key},
                max_bytes=MAX_SEARCH_RESPONSE_BYTES, deadline=deadline, credentialed=True)
        except AppError:
            raise
        except (httpx.HTTPError, httpcore.NetworkError, OSError, TimeoutError) as exc:
            raise AppError(502, "WEB_SEARCH_PROVIDER_FAILED", "Brave 搜索暂时不可用，请稍后重试") from exc
        try:
            document = json.loads(body)
        except (TypeError, ValueError):
            raise AppError(502, "WEB_SEARCH_PROVIDER_FAILED", "Brave 搜索未返回可用结果") from None
        rows = ((document.get("web") or {}).get("results") or []) if isinstance(document, dict) else []
        return [item for raw in rows[:MAX_SEARCH_RESULTS] if (item := _result_item(raw))][:limit]

    def _host_recheck(self, run_id, capability, arguments):
        from .capability_policy import CAPABILITY_TOOLS
        run = self.service.require("runs", run_id)
        # May return False (approval required); the executor verifies the
        # corresponding approvals row rather than treating False as approval.
        self.service.authorize_tool(run["conversation_id"], CAPABILITY_TOOLS[capability], arguments)

    def _context(self, user, assistant, run_id, capability, arguments, call_id=None):
        from .capability_execution import RunContext, new_call_id
        run = self.service.require("runs", run_id)
        return RunContext(
            run_id=run_id, conversation_id=run["conversation_id"], user_id=user["id"],
            assistant_id=assistant["id"], tool_call_id=call_id or new_call_id(),
            recheck=lambda: self._host_recheck(run_id, capability, arguments))

    async def search_for_run(self, user, assistant, run_id, query, limit=5, call_id=None):
        query = str(query).strip()
        if not query or len(query) > 500:
            raise AppError(422, "WEB_QUERY_INVALID", "搜索词长度应为 1–500 个字符")
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        _, searx_url, brave_url, brave_key, _ = self._provider_config()
        arguments = {"query": query, "limit": limit}
        context = self._context(user, assistant, run_id, "search", arguments, call_id)

        async def operation(provider, deadline, recheck):
            if provider == "bing_rss":
                return await self._bing(query, deadline)
            if provider == "searxng":
                return await self._searxng(searx_url, query, limit, deadline)
            if provider == "brave":
                return await self._brave(brave_url, brave_key, query, limit, deadline)
            if provider == "deepseek_api":
                return await self._deepseek(query, limit, deadline)
            raise AppError(503, "WEB_SEARCH_PROVIDER_UNAVAILABLE", "当前网页搜索服务未配置")

        from .capability_execution import CapabilityExecutor
        from .capability_policy import domain_allows
        executor = CapabilityExecutor(self.service)
        route = await executor.execute_for_run(context, "search", arguments, operation)
        domains = executor.effective(context, "search", arguments)["domains"]
        used_provider, requested_provider = route.provider, route.requested_provider
        results, seen = [], set()
        for item in route.value:
            canonical = item.get("url")
            if not canonical or canonical in seen:
                continue
            # Domain policy governs which result content is accepted; it is not
            # a claim that the upstream search service only queried those hosts.
            if not domain_allows(domains, canonical):
                continue
            seen.add(canonical)
            public_provider = PUBLIC_PROVIDER_NAME if used_provider == "deepseek_api" else used_provider
            locator = {"url": canonical, "fetched_at": now(), "search_query": query,
                       "content_type": "search_excerpt", "capability": "search", "provider": public_provider}
            if item.get("published_at"):
                locator["published_at"] = item["published_at"]
            source = self.service.evidence.register(
                run_id, "web", item["title"] or canonical, locator, item["excerpt"],
                resource_type="web", resource_id=assistant["id"], original_url=canonical,
                tool_name="mcp__workspace__web_search",
            )
            results.append({"title": item["title"], "url": canonical, "excerpt": item["excerpt"],
                           "source_id": source["id"], "citation": f"[[source:{source['id']}]]",
                           "provider": public_provider})
            if len(results) >= limit:
                break
        note = "这些是搜索摘要；精确引文或细节请用 web_read 读取原网页。"
        if used_provider != requested_provider:
            note += " 当前首选搜索服务暂时不可用，已使用配置的回退服务。"
        public_used = PUBLIC_PROVIDER_NAME if used_provider == "deepseek_api" else used_provider
        public_requested = PUBLIC_PROVIDER_NAME if requested_provider == "deepseek_api" else requested_provider
        return {"results": results, "provider": public_used, "requested_provider": public_requested,
                "fallback": used_provider != requested_provider, "note": note}

    async def read_for_run(self, user, assistant, run_id, url, query="", call_id=None):
        url = public_url(url)
        arguments = {"url": url, "query": str(query or "")}
        context = self._context(user, assistant, run_id, "fetch", arguments, call_id)

        async def operation(provider, deadline, recheck):
            if provider == "deepseek_api":
                client = self._deepseek_client()
                if client is None:
                    raise AppError(503, "WEB_SEARCH_PROVIDER_UNAVAILABLE", "当前网页读取服务未配置")
                async with anyio.fail_after(max(0.1, deadline - time.monotonic())):
                    page = await client.fetch(url)
                return page["url"], page["content"], page["title"]
            if provider != "public_http":
                raise AppError(503, "WEB_SEARCH_PROVIDER_UNAVAILABLE", "当前网页读取服务未配置")
            final_url, mime, body = await fetch_public(url, deadline=deadline)
            if mime and not any(kind in mime for kind in ("text/", "xml", "json")):
                raise AppError(422, "WEB_FORMAT_UNSUPPORTED", "此链接不是可读取的文本网页，请使用网页正文链接")
            encoding = httpx.Response(200, headers={"content-type": mime}, content=body).encoding or "utf-8"
            decoded = body.decode(encoding, errors="replace")
            parser = PageText()
            if "html" in mime or "<html" in decoded[:300].lower():
                parser.feed(decoded)
                text, title = parser.text, "".join(parser.title_parts).strip()
            else:
                text, title = decoded, final_url
            return final_url, text, title

        from .capability_execution import CapabilityExecutor
        route = await CapabilityExecutor(self.service).execute_for_run(
            context, "fetch", arguments, operation, target_url=url)
        final_url, text, title = route.value
        used_provider, requested_provider = route.provider, route.requested_provider
        start = 0
        query = str(query).strip()[:300]
        if query:
            found = text.casefold().find(query.casefold())
            if found >= 0:
                start = max(0, found - 1500)
        excerpt = text[start:start + 16000]
        if not excerpt.strip():
            raise AppError(422, "WEB_EMPTY", "该网页未提供可读取的正文")
        source = self.service.evidence.register(
            run_id, "web", title[:300] or final_url,
            {"url": final_url, "fetched_at": now(), "content_type": "page_text", "capability": "fetch",
             "character_start": start, "character_end": start + len(excerpt),
             "truncated": len(text) > start + len(excerpt),
             "provider": PUBLIC_PROVIDER_NAME if used_provider == "deepseek_api" else used_provider},
            excerpt, resource_type="web", resource_id=assistant["id"], original_url=final_url,
            tool_name="mcp__workspace__web_read",
        )
        return {"title": title, "url": final_url, "text": excerpt, "source_id": source["id"], "citation": f"[[source:{source['id']}]]",
                "provider": PUBLIC_PROVIDER_NAME if used_provider == "deepseek_api" else used_provider,
                "fallback": used_provider != requested_provider}
