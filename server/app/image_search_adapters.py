"""Explicit, single-request image *search* adapters.

Image search is deliberately split by real provider contract:

* ``brave_images``: text-to-image search via Brave's dedicated Images API.
* ``deepseek_webtools``: reverse-image search through the official
  ``deepseek-flash`` Web Tools bridge.  DeepSeek's standalone Search API does
  **not** support images, so this is a distinct provider and mode; it is never
  presented as a standalone image endpoint.

The reverse-image request below mirrors the school-cloud probe that actually
passed on 2026-11-14: ``/anthropic/v1/messages``, ``model=deepseek-flash``,
``thinking`` disabled, ``tools=[{type:deepseek_web_search,name:deepseek_web_search}]``
and a base64 image source.  The parser trusts only structured
``server_tool_use`` / ``web_search_tool_result`` blocks correlated by ID; model
prose is never a source.  Both adapters perform exactly one request.
"""
from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from .deepseek_api import (
    OFFICIAL_HOST,
    _endpoint,
    read_json_response,
    remaining_seconds,
)
from .security import AppError

BRAVE_IMAGES_BASE = "https://api.search.brave.com"
BRAVE_IMAGES_PATH = "/res/v1/images/search"
BRAVE_IMAGES_HOST = "api.search.brave.com"
MAX_QUERY = 400
MAX_RESULTS = 20
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
FIXED_WEBTOOLS_MODEL = "deepseek-flash"
FIXED_WEBTOOLS_TOOL = "deepseek_web_search"
MAX_WEBTOOLS_TOKENS = 700
WEBTOOLS_REVERSE_INSTRUCTION = (
    "Use image search to find this image or visually matching original public pages. "
    "Prefer image search rather than text search. Do not guess URLs."
)
IMAGE_RESULT_KINDS = frozenset({"image_search", "deep_image_search"})
IMAGE_RESULT_TYPES = frozenset({"web_search_result", "image_search_result"})


def _safe_public_url(value: Any) -> str | None:
    """Accept only provider-returned public HTTP(S) links without credentials."""
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        from .evidence import public_url as evidence_public_url
        from .web_tools import public_url as public_http_url
        public_http_url(value)
        return evidence_public_url(value)
    except (AppError, ValueError, TypeError):
        return None


def _int_dimension(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 20_000 else None


def _brave_endpoint() -> str:
    """Fixed official endpoint; no user-supplied provider URL is accepted."""
    return BRAVE_IMAGES_BASE + BRAVE_IMAGES_PATH


def parse_brave_images(body: dict[str, Any], *, limit: int = 5, query: str = "") -> dict[str, Any]:
    """Pure parser for ``GET /res/v1/images/search``.

    ``results[].url`` is the original page; ``properties.url`` is the original
    image; ``thumbnail.src`` is a thumbnail.  Results missing either required
    link or failing URL safety are dropped, never repaired by guessing.
    """
    if body.get("type") != "images":
        raise AppError(502, "IMAGE_SEARCH_RESPONSE_INVALID", "图片搜索服务返回格式无效")
    rows = body.get("results")
    if not isinstance(rows, list):
        raise AppError(502, "IMAGE_SEARCH_RESPONSE_INVALID", "图片搜索服务返回格式无效")
    results, seen = [], set()
    for raw in rows[:MAX_RESULTS]:
        if not isinstance(raw, dict) or raw.get("type") != "image_result":
            continue
        page_url = _safe_public_url(raw.get("url"))
        properties = raw.get("properties") if isinstance(raw.get("properties"), dict) else {}
        image_url = _safe_public_url(properties.get("url"))
        if not page_url or not image_url:
            continue
        key = (page_url, image_url)
        if key in seen:
            continue
        seen.add(key)
        thumbnail = raw.get("thumbnail") if isinstance(raw.get("thumbnail"), dict) else {}
        results.append({
            "title": str(raw.get("title") or "")[:500],
            "page_url": page_url,
            "image_url": image_url,
            # Thumbnails are display hints only and are never auto-loaded.
            "thumbnail_url": _safe_public_url(thumbnail.get("src")),
            "width": _int_dimension(properties.get("width")) or _int_dimension(thumbnail.get("width")),
            "height": _int_dimension(properties.get("height")) or _int_dimension(thumbnail.get("height")),
            "provider": "brave_images",
        })
        if len(results) >= limit:
            break
    return {"status": "results" if results else "no_results", "mode": "text_search",
            "query": query, "results": results, "provider": "brave_images"}


class BraveImagesAPI:
    """One-request client for the fixed official Brave Images endpoint."""

    def __init__(self, *, api_key: str, timeout_seconds: float = 20, transport=None):
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.timeout_seconds = max(1.0, min(60.0, float(timeout_seconds)))
        self.transport = transport

    async def search(self, query: str, *, limit: int = 5, deadline: float | None = None) -> dict[str, Any]:
        query = str(query or "").strip()
        if not query or len(query) > MAX_QUERY:
            raise AppError(422, "IMAGE_SEARCH_QUERY_INVALID", "图片检索词长度应为 1–400 个字符")
        try:
            limit = max(1, min(MAX_RESULTS, int(limit)))
        except (TypeError, ValueError):
            raise AppError(422, "IMAGE_SEARCH_QUERY_INVALID", "返回数量无效") from None
        if not self.api_key:
            raise AppError(503, "IMAGE_SEARCH_PROVIDER_UNAVAILABLE", "图片搜索服务尚未配置")
        seconds = remaining_seconds(deadline, self.timeout_seconds)
        url = _brave_endpoint() + "?" + urlencode({"q": query, "count": limit, "safesearch": "strict"})
        timeout = httpx.Timeout(seconds, connect=min(10.0, seconds))
        transport = self.transport
        if transport is None:
            transport = httpx.AsyncHTTPTransport(retries=0)
            # Pin validated public addresses and keep TLS verification.
            from .web_tools import PublicNetworkBackend
            transport._pool._network_backend = PublicNetworkBackend()
        headers = {"Accept": "application/json", "X-Subscription-Token": self.api_key}
        async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=timeout,
                                     follow_redirects=False, headers=headers) as client:
            body = await read_json_response(client, "GET", url, max_bytes=MAX_RESPONSE_BYTES, deadline=deadline)
        parsed = parse_brave_images(body, limit=limit, query=query)
        parsed["requested_provider"] = "brave_images"
        return parsed


def build_webtools_reverse_request(*, mime_type: str, data: bytes) -> dict[str, Any]:
    """Request body matching the validated Messages-shaped Web Tools route.

    Only the selected image plus a fixed instruction are sent.  The main
    conversation, other attachments, knowledge context and host system prompt
    are never forwarded.
    """
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "model": FIXED_WEBTOOLS_MODEL,
        "max_tokens": MAX_WEBTOOLS_TOKENS,
        "thinking": {"type": "disabled"},
        "stream": False,
        "tools": [{"type": FIXED_WEBTOOLS_TOOL, "name": FIXED_WEBTOOLS_TOOL}],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": WEBTOOLS_REVERSE_INSTRUCTION},
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime_type, "data": encoded}},
            ],
        }],
    }


def _walk_blocks(value: Any, *, limit: int = 512):
    """Bounded traversal of a provider response tree."""
    pending, seen = [(value, 0)], 0
    while pending and seen < limit:
        item, depth = pending.pop(0)
        seen += 1
        if depth > 8:
            continue
        if isinstance(item, dict):
            yield item
            for key in ("content", "message", "output", "blocks", "result", "results", "choices"):
                if key in item:
                    pending.append((item[key], depth + 1))
        elif isinstance(item, list):
            pending.extend((entry, depth + 1) for entry in item[:64])


def parse_webtools_image_result(body: dict[str, Any]) -> dict[str, Any]:
    """Parse verified image-search calls and correlated result blocks.

    The real 2026-11-14 response uses ``server_tool_use(kind=image_search,
    name=deepseek_web_search)`` followed by ``web_search_tool_result(kind=
    image_search)`` whose entries are ``web_search_result`` with ``url`` and
    ``image_url``.  Entries have no ``kind``; requiring one dropped every real
    result.  A plain web search therefore stays ``not_executed``.
    """
    image_calls: dict[str, dict[str, Any]] = {}
    blocks: list[dict[str, Any]] = []
    for block in _walk_blocks(body):
        block_type = block.get("type")
        if block_type == "server_tool_use" and isinstance(block.get("id"), str):
            if (block.get("name") == FIXED_WEBTOOLS_TOOL
                    and block.get("kind") in IMAGE_RESULT_KINDS):
                image_calls[block["id"]] = block
        elif block_type in {"web_search_tool_result", "image_search_tool_result"}:
            blocks.append(block)
    executed = bool(image_calls)
    results, seen = [], set()
    for block in blocks:
        call = image_calls.get(block.get("tool_use_id"))
        if not call:
            continue
        # Call and result kinds must agree; a mixed block is not evidence.
        if block.get("kind") not in IMAGE_RESULT_KINDS or block.get("kind") != call.get("kind"):
            continue
        entries = block.get("content")
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_type, entry_kind = entry.get("type"), entry.get("kind")
            if entry_type not in IMAGE_RESULT_TYPES and entry_kind not in IMAGE_RESULT_KINDS:
                continue
            page_url = _safe_public_url(entry.get("url"))
            image_url = _safe_public_url(entry.get("image_url"))
            if not page_url or not image_url:
                continue
            key = (page_url, image_url)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "title": str(entry.get("title") or "")[:500],
                "page_url": page_url,
                "image_url": image_url,
                "thumbnail_url": _safe_public_url(entry.get("thumbnail_url")),
                "width": _int_dimension(entry.get("width")),
                "height": _int_dimension(entry.get("height")),
                "kind": str(call.get("kind")),
                "provider": "deepseek_webtools",
            })
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    server_usage = usage.get("server_tool_use") if isinstance(usage.get("server_tool_use"), dict) else {}
    count = server_usage.get("tool_image_search")
    safe_usage = {key: usage[key] for key in
                  ("input_tokens", "output_tokens", "cache_read_input_tokens", "service_tier")
                  if usage.get(key) is not None}
    safe_usage["tool_image_search"] = (count if isinstance(count, int) and not isinstance(count, bool)
                                       and count >= 0 else None)
    return {
        "status": "results" if results else ("no_results" if executed else "not_executed"),
        "mode": "reverse_image",
        "results": results,
        "provider": "deepseek_webtools",
        "requested_provider": "deepseek_webtools",
        "billed_image_search_count": safe_usage["tool_image_search"],
        "usage": safe_usage,
    }


class DeepSeekWebToolsAPI:
    """One-request reverse-image bridge over the validated Messages route."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 60, transport=None):
        self.base_url = base_url
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.timeout_seconds = max(1.0, min(180.0, float(timeout_seconds)))
        self.transport = transport

    async def reverse_image(self, *, data: bytes, mime_type: str, deadline: float | None = None) -> dict[str, Any]:
        if not data:
            raise AppError(422, "INVALID_DOCUMENT", "文件内容为空")
        if not self.api_key:
            raise AppError(503, "IMAGE_SEARCH_PROVIDER_UNAVAILABLE", "图片搜索服务尚未配置")
        if not str(mime_type or "").startswith("image/"):
            raise AppError(422, "IMAGE_SEARCH_INPUT_INVALID", "以图搜图需要图片文件")
        endpoint = _endpoint(self.base_url, "/anthropic/v1/messages")
        seconds = remaining_seconds(deadline, self.timeout_seconds)
        timeout = httpx.Timeout(seconds, connect=min(10.0, seconds))
        # The validated route uses Anthropic-style headers, not Bearer.
        headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        payload = build_webtools_reverse_request(mime_type=mime_type, data=data)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False,
                                     trust_env=False, transport=self.transport) as client:
            body = await read_json_response(client, "POST", endpoint, max_bytes=MAX_RESPONSE_BYTES,
                                            deadline=deadline, json=payload)
        return parse_webtools_image_result(body)


def official_host(value: str) -> bool:
    """Small helper retained for diagnostics without exposing config values."""
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".") == OFFICIAL_HOST
    except ValueError:
        return False
