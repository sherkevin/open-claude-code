"""Public web retrieval with bounded reads, pinned public DNS and source records."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

import httpcore
import httpx
import anyio
from httpcore._backends.auto import AutoBackend

from .db import now
from .security import AppError

PROXY_FAKE_IPS = ipaddress.ip_network("198.18.0.0/15")
# Fixed public-IP HTTPS endpoints avoid resolving the resolver through fake DNS.
# TLS certificates are verified against each literal endpoint address.
DOH_ENDPOINTS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/resolve")


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


async def fetch_public(url: str, *, max_bytes: int = 2_000_000) -> tuple[str, str, bytes]:
    try:
        with anyio.fail_after(40):
            return await _fetch_public(url, max_bytes=max_bytes)
    except TimeoutError:
        raise AppError(504, "WEB_TIMEOUT", "网页读取超时，请尝试其他来源") from None


async def _fetch_public(url: str, *, max_bytes: int) -> tuple[str, str, bytes]:
    url = public_url(url)
    transport = httpx.AsyncHTTPTransport(retries=0)
    # Pinned httpx 0.28.x/httpcore 1.x integration. The pool owns TLS correctly.
    transport._pool._network_backend = PublicNetworkBackend()
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=15,
                                headers={"User-Agent": "BetterAI-Research/0.4 (+public-page-reader)", "Accept": "text/html,application/rss+xml,text/plain"}) as client:
        for _ in range(5):
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


class WebTools:
    def __init__(self, service):
        self.service = service

    def authorize(self, user, assistant):
        current = self.service.usable_assistant(assistant["id"], user)
        if not current.get("web_search_enabled"):
            raise AppError(403, "WEB_SEARCH_DISABLED", "当前助手未启用网页搜索")

    async def search_for_run(self, user, assistant, run_id, query, limit=5):
        self.authorize(user, assistant)
        query = str(query).strip()
        if not query or len(query) > 500:
            raise AppError(422, "WEB_QUERY_INVALID", "搜索词长度应为 1–500 个字符")
        url = "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss"})
        _, _, body = await fetch_public(url, max_bytes=500_000)
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise AppError(502, "WEB_SEARCH_UNAVAILABLE", "搜索服务暂时未返回可用结果，请稍后重试")
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            raise AppError(502, "WEB_SEARCH_UNAVAILABLE", "搜索服务暂时未返回可用结果，请稍后重试") from None
        results = []
        for item in root.findall("./channel/item")[:max(1, min(int(limit), 8))]:
            link, title = item.findtext("link", ""), item.findtext("title", "")
            try:
                public_url(link)
            except AppError:
                continue
            parser = PageText()
            parser.feed(item.findtext("description", ""))
            excerpt = parser.text[:3000]
            source = self.service.evidence.register(
                run_id, "web", title or link, {"url": link, "fetched_at": now(), "search_query": query,
                    "content_type": "search_excerpt", "provider": "Bing"}, excerpt,
                resource_type="web", resource_id=assistant["id"], original_url=link,
            )
            results.append({"title": title, "url": link, "excerpt": excerpt, "source_id": source["id"], "citation": f"[[source:{source['id']}]]"})
        return {"results": results, "note": "这些是搜索摘要；精确引文或细节请用 web_read 读取原网页。"}

    async def read_for_run(self, user, assistant, run_id, url, query=""):
        self.authorize(user, assistant)
        final_url, mime, body = await fetch_public(url)
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
            {"url": final_url, "fetched_at": now(), "content_type": "page_text", "character_start": start,
             "character_end": start + len(excerpt), "truncated": len(text) > start + len(excerpt)},
            excerpt, resource_type="web", resource_id=assistant["id"], original_url=final_url,
        )
        return {"title": title, "url": final_url, "text": excerpt, "source_id": source["id"], "citation": f"[[source:{source['id']}]]"}
