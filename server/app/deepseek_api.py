"""Bounded adapter for the optional official DeepSeek tool APIs.

The chat/runtime provider remains governed by the project's TokenHub policy.
This module is a separate, opt-in capability for public Search/Fetch and
document OCR.  It never discovers credentials, never sends them in URLs, and
returns generic application errors so the upstream service name is not exposed
to end users.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .security import AppError

OFFICIAL_HOST = "api.deepseek.com"
PUBLIC_PROVIDER_NAME = "managed_search"
MAX_SEARCH_QUERY = 128
MAX_FETCH_URL = 8192
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGES = 1000
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


def _bounded_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "")[:limit]


def _endpoint(base_url: str, path: str) -> str:
    raw = str(base_url or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        parsed = None
    try:
        port = parsed.port if parsed is not None else None
    except ValueError:
        port = -1
    if (parsed is None or parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料能力线路未配置")
    host = parsed.hostname.lower().rstrip(".")
    # Production requests are restricted to the documented endpoint. Tests
    # can still use an HTTP mock transport while retaining this host check.
    if host != OFFICIAL_HOST:
        raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料能力线路未配置")
    if host == OFFICIAL_HOST and (parsed.scheme != "https" or port not in {None, 443}):
        raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料能力线路未配置")
    return raw + path


def _public_url(value: str) -> str:
    """Validate a fetch target before it is sent to the external service."""
    try:
        from .web_tools import public_url
        return public_url(value)
    except AppError:
        raise
    except (TypeError, ValueError):
        raise AppError(422, "WEB_URL_DENIED", "只能读取公开 HTTP 或 HTTPS 网页") from None


class DeepSeekAPI:
    """Small async client with bounded retries and response validation."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 30, transport=None):
        self.base_url = base_url
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.timeout_seconds = max(1.0, min(180.0, float(timeout_seconds)))
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料能力尚未配置")
        return {"authorization": "Bearer " + self.api_key, "content-type": "application/json"}

    async def _post(self, path: str, *, json_body: dict[str, Any] | None = None, files=None, data=None):
        headers = self._headers()
        if files is not None:
            headers.pop("content-type", None)
        timeout = httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds))
        try:
            async with httpx.AsyncClient(
                headers=headers, timeout=timeout, follow_redirects=False,
                trust_env=False, transport=self.transport,
            ) as client:
                for attempt in range(2):
                    try:
                        response = await client.post(_endpoint(self.base_url, path), json=json_body, files=files, data=data)
                    except httpx.TimeoutException as exc:
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                            continue
                        raise AppError(504, "EXTERNAL_TOOL_TIMEOUT", "外部资料服务响应超时") from exc
                    except httpx.HTTPError as exc:
                        if attempt == 0:
                            await asyncio.sleep(0.2)
                            continue
                        raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料服务暂时不可用") from exc
                    raw = bytes(response.content or b"")
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回内容过大")
                    if response.status_code in RETRY_STATUSES and attempt == 0:
                        await asyncio.sleep(0.2)
                        continue
                    if response.status_code >= 400:
                        if response.status_code in {401, 403}:
                            raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料能力未获授权")
                        if response.status_code == 402:
                            raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料能力当前不可用")
                        raise AppError(502, "EXTERNAL_TOOL_REQUEST_FAILED", "外部资料服务请求失败")
                    try:
                        return response.json()
                    except (TypeError, ValueError) as exc:
                        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回格式无效") from exc
        except AppError:
            raise
        except asyncio.CancelledError:
            raise

    async def search(self, query: str, *, count: int = 10, original_question: str | None = None,
                     need_content: bool = False) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            raise AppError(422, "WEB_QUERY_INVALID", "搜索词不能为空")
        if len(query) > MAX_SEARCH_QUERY:
            raise AppError(422, "WEB_QUERY_INVALID", "搜索词不能超过 128 个字符")
        query = _bounded_text(query, MAX_SEARCH_QUERY)
        try:
            count = max(1, min(50, int(count)))
        except (TypeError, ValueError):
            raise AppError(422, "WEB_QUERY_INVALID", "返回数量无效") from None
        if original_question is not None and len(str(original_question).strip()) > MAX_SEARCH_QUERY:
            raise AppError(422, "WEB_QUERY_INVALID", "原始问题不能超过 128 个字符")
        original_question = _bounded_text(original_question, MAX_SEARCH_QUERY).strip() or None
        payload: dict[str, Any] = {"query": query, "count": count, "need_content": bool(need_content)}
        if original_question:
            payload["ori_question"] = original_question
        body = await self._post("/v1/tools/search", json_body=payload)
        rows = body.get("results", []) if isinstance(body, dict) else []
        if not isinstance(rows, list):
            raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回格式无效")
        results: list[dict[str, Any]] = []
        for raw in rows[:count]:
            if not isinstance(raw, dict):
                continue
            try:
                from .web_tools import canonical_result_url
                url = canonical_result_url(raw.get("url") or "")
            except AppError:
                url = None
            if not url:
                continue
            results.append({
                "title": _bounded_text(raw.get("title") or url, 500),
                "url": url,
                "excerpt": _bounded_text(raw.get("snippet") or raw.get("summary") or raw.get("content"), 3000),
                "published_at": _bounded_text(raw.get("date"), 100) or None,
                "score": raw.get("rerank_score") if isinstance(raw.get("rerank_score"), (int, float)) else None,
                "authority": raw.get("authority") if isinstance(raw.get("authority"), int) else None,
                "content": _bounded_text(raw.get("content"), 16000),
                "site_name": _bounded_text(raw.get("site_name"), 200) or None,
            })
        return results

    async def fetch(self, url: str) -> dict[str, str]:
        safe_url = _public_url(_bounded_text(url, MAX_FETCH_URL))
        body = await self._post("/v1/tools/fetch", json_body={"url": safe_url})
        if not isinstance(body, dict):
            raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回格式无效")
        content = _bounded_text(body.get("content"), 16000)
        if not content.strip():
            raise AppError(422, "WEB_EMPTY", "该网页未提供可读取的正文")
        return {"title": _bounded_text(body.get("title") or safe_url, 500), "url": safe_url, "content": content}

    async def ocr(self, path: str | Path, *, filename: str, purpose: str = "rich_layout_recognition") -> dict[str, Any]:
        source = Path(path)
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise AppError(422, "INVALID_DOCUMENT", "文件无法读取") from exc
        if size <= 0:
            raise AppError(422, "INVALID_DOCUMENT", "文件内容为空")
        if size > MAX_FILE_BYTES:
            raise AppError(413, "DOCUMENT_TOO_LARGE", "文件超过外部识别上限")
        if purpose not in {"text_recognition", "rich_layout_recognition"}:
            raise AppError(422, "OCR_INPUT_INVALID", "外部识别用途无效")
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise AppError(422, "INVALID_DOCUMENT", "文件无法读取") from exc
        files = {"file": (Path(filename).name[:255] or "document", payload, "application/octet-stream")}
        body = await self._post("/v1/tools/ocr", files=files, data={"purpose": purpose})
        if not isinstance(body, dict) or not isinstance(body.get("pages"), list):
            raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部识别服务返回格式无效")
        pages = []
        for index, item in enumerate(body["pages"][:MAX_PAGES]):
            if not isinstance(item, dict):
                continue
            try:
                page_index = int(item.get("index", index))
            except (TypeError, ValueError):
                continue
            if page_index < 0 or page_index >= MAX_PAGES:
                continue
            text = _bounded_text(item.get("text"), 2_000_000)
            pages.append({"page": page_index + 1, "text": text, "extraction": "managed-ocr", "finish_reason": _bounded_text(item.get("finish_reason"), 20)})
        if not pages:
            raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部识别服务未返回页面")
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        safe_usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens") if isinstance(usage.get(key), int) and usage[key] >= 0}
        return {"id": _bounded_text(body.get("id"), 120), "purpose": purpose, "pages": pages, "usage": safe_usage}
