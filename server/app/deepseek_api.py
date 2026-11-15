"""Bounded, single-attempt adapters for the optional official DeepSeek tool APIs.

The chat/runtime provider remains governed by the project's TokenHub policy.
This module is a separate, opt-in capability for public Search/Fetch and
document OCR.  It never discovers credentials, never sends them in URLs, and
returns generic application errors so the upstream service name is not exposed
to end users.

Every public method performs **one** HTTP request.  Retry, fallback, quota and
deadline budgeting belong to the shared capability executor; a second retry
layer here would silently multiply provider dispatches (and, for OCR, billing
for an upload the provider already accepted).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx

from .security import AppError

OFFICIAL_HOST = "api.deepseek.com"
PUBLIC_PROVIDER_NAME = "managed_search"
MAX_SEARCH_QUERY = 128
MAX_FETCH_URL = 8192
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_OCR_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_OCR_PAGE_TEXT = 2_000_000
MAX_OCR_TOTAL_TEXT = 8_000_000
MAX_PAGES = 1000
OCR_PURPOSES = frozenset({"text_recognition", "rich_layout_recognition"})
# Retained for callers that classify errors; the adapter itself never retries.
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


def remaining_seconds(deadline: float | None, fallback: float) -> float:
    """Absolute ``time.monotonic()`` deadline support shared by adapters."""
    if deadline is None:
        return fallback
    left = float(deadline) - time.monotonic()
    if left <= 0:
        raise AppError(504, "EXTERNAL_TOOL_TIMEOUT", "外部资料服务响应超时")
    return left


def _http_error(status: int) -> AppError:
    """Map upstream HTTP status to a stable, non-leaking internal error.

    Authentication and balance failures are deliberately *not* 5xx: the
    executor must never retry or fall back after a credential/payment error.
    """
    if status in {401, 403}:
        return AppError(403, "EXTERNAL_TOOL_UNAUTHORIZED", "外部资料能力未获授权")
    if status == 402:
        return AppError(402, "EXTERNAL_TOOL_BALANCE", "外部资料能力当前不可用")
    if status == 429:
        return AppError(429, "EXTERNAL_TOOL_RATE_LIMITED", "外部资料服务请求过于频繁")
    if status == 408:
        return AppError(504, "EXTERNAL_TOOL_TIMEOUT", "外部资料服务响应超时")
    if status >= 500:
        return AppError(502, "EXTERNAL_TOOL_REQUEST_FAILED", "外部资料服务暂时不可用")
    return AppError(400, "EXTERNAL_TOOL_REQUEST_REJECTED", "外部资料服务拒绝了请求")


def _error_envelope(body: dict[str, Any]) -> AppError | None:
    """HTTP 200 can still carry a provider error object (documented for OCR)."""
    error = body.get("error")
    if error is None:
        return None
    code = ""
    if isinstance(error, dict):
        code = " ".join(str(error.get(key) or "") for key in ("code", "type", "message")).lower()
    elif isinstance(error, str):
        code = error.lower()
    if any(token in code for token in ("timeout", "timed_out")):
        return AppError(504, "EXTERNAL_TOOL_TIMEOUT", "外部资料服务响应超时")
    if any(token in code for token in ("busy", "rate", "overload", "too_many")):
        return AppError(429, "EXTERNAL_TOOL_RATE_LIMITED", "外部资料服务当前繁忙")
    if any(token in code for token in ("auth", "permission", "forbidden", "unauthorized")):
        return AppError(403, "EXTERNAL_TOOL_UNAUTHORIZED", "外部资料能力未获授权")
    if any(token in code for token in ("balance", "quota", "insufficient")):
        return AppError(402, "EXTERNAL_TOOL_BALANCE", "外部资料能力当前不可用")
    return AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回格式无效")


async def read_json_response(client: httpx.AsyncClient, method: str, url: str, *,
                             max_bytes: int, deadline: float | None, **kwargs) -> dict[str, Any]:
    """Stream one response with a decompressed byte cap and absolute deadline.

    ``Content-Length`` is never trusted, and a provider keepalive newline does
    not extend the local budget beyond ``deadline``.
    """
    seconds = remaining_seconds(deadline, 30.0)
    try:
        with anyio.fail_after(seconds):
            async with client.stream(method, url, **kwargs) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > max_bytes:
                        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回内容过大")
                status = response.status_code
    except TimeoutError:
        raise AppError(504, "EXTERNAL_TOOL_TIMEOUT", "外部资料服务响应超时") from None
    if status >= 400:
        raise _http_error(status)
    try:
        body = json.loads(bytes(raw))
    except (TypeError, ValueError):
        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回格式无效") from None
    if not isinstance(body, dict):
        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回格式无效")
    envelope = _error_envelope(body)
    if envelope is not None:
        raise envelope
    return body


def parse_ocr_body(body: dict[str, Any], *, purpose: str) -> dict[str, Any]:
    """Validate page integrity instead of turning malformed results into success.

    Rules taken from the public contract and the security audit:

    * ``purpose`` must match the request when echoed.
    * ``pages[].index`` is zero-based, unique, and inside ``page_count``.
    * missing/duplicate pages or a ``page_count`` mismatch are ``partial``.
    * ``finish_reason == "length"`` marks that page incomplete.
    * all-empty text is explicitly ``empty``; it is never a full success.
    """
    if not isinstance(body.get("pages"), list):
        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部识别服务返回格式无效")
    echoed = body.get("purpose")
    if echoed is not None and echoed != purpose:
        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部识别服务返回用途不一致")
    raw_count = body.get("page_count")
    inferred = False
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        # A missing count is tolerated only when real pages were returned, and
        # the result is then explicitly partial so callers cannot claim a full
        # document.
        indices = [item.get("index") for item in body["pages"] if isinstance(item, dict) and type(item.get("index")) is int]
        if not indices:
            raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部识别服务未返回页面数量")
        page_count, inferred = max(indices) + 1, True
    else:
        page_count = raw_count
    if page_count < 0 or page_count > MAX_PAGES:
        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部识别服务返回页数无效")
    if page_count == 0 and body["pages"]:
        # "0 pages" cannot contain a complete page 1.
        raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部识别服务页数与页面内容矛盾")

    prepared: dict[int, dict[str, Any]] = {}
    malformed = False
    total_text = 0
    for item in body["pages"][:MAX_PAGES]:
        if not isinstance(item, dict):
            malformed = True
            continue
        index = item.get("index")
        if type(index) is not int or index < 0 or index >= page_count or index in prepared:
            malformed = True
            continue
        raw_text = item.get("text")
        if isinstance(raw_text, str):
            raw_text = raw_text.replace("\x00", "")
        else:
            raw_text = ""
        text = raw_text[:MAX_OCR_PAGE_TEXT]
        truncated = len(raw_text) > MAX_OCR_PAGE_TEXT
        if total_text + len(text) > MAX_OCR_TOTAL_TEXT:
            text = text[: max(0, MAX_OCR_TOTAL_TEXT - total_text)]
            truncated = True
        total_text += len(text)
        finish = _bounded_text(item.get("finish_reason"), 20)
        complete = finish != "length" and not truncated
        prepared[index] = {
            "page": index + 1,
            "text": text,
            "extraction": "managed-ocr",
            "finish_reason": finish,
            "complete": complete,
            "truncated": truncated,
        }
    missing = [index + 1 for index in range(page_count) if index not in prepared]
    partial = (malformed or inferred or bool(missing)
               or any(not item["complete"] for item in prepared.values()))
    pages = [prepared[index] for index in sorted(prepared)]
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    safe_usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")
                  if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool) and usage[key] >= 0}
    return {
        "id": _bounded_text(body.get("id"), 120),
        "purpose": purpose,
        "page_count": page_count,
        "pages": pages,
        "partial": partial,
        "missing_pages": missing,
        "empty": not any(item["text"].strip() for item in pages),
        "usage": safe_usage,
    }


class DeepSeekAPI:
    """Small async client. Each method is exactly one provider request."""

    def __init__(self, *, base_url: str, api_key: str, timeout_seconds: float = 30, transport=None):
        self.base_url = base_url
        self.api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.timeout_seconds = max(1.0, min(180.0, float(timeout_seconds)))
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise AppError(503, "EXTERNAL_TOOL_UNAVAILABLE", "外部资料能力尚未配置")
        return {"authorization": "Bearer " + self.api_key, "content-type": "application/json"}

    def _client(self, headers: dict[str, str], deadline: float | None) -> httpx.AsyncClient:
        seconds = remaining_seconds(deadline, self.timeout_seconds)
        timeout = httpx.Timeout(seconds, connect=min(10.0, seconds))
        return httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=False,
            trust_env=False, transport=self.transport,
        )

    async def _post(self, path: str, *, json_body: dict[str, Any] | None = None, files=None, data=None,
                    deadline: float | None = None, max_bytes: int = MAX_RESPONSE_BYTES) -> dict[str, Any]:
        headers = self._headers()
        if files is not None:
            headers.pop("content-type", None)
        url = _endpoint(self.base_url, path)
        async with self._client(headers, deadline) as client:
            return await read_json_response(
                client, "POST", url, max_bytes=max_bytes, deadline=deadline,
                json=json_body, files=files, data=data,
            )

    async def search(self, query: str, *, count: int = 10, original_question: str | None = None,
                     need_content: bool = False, deadline: float | None = None) -> list[dict[str, Any]]:
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
        body = await self._post("/v1/tools/search", json_body=payload, deadline=deadline)
        # A legal empty result is an explicit ``results: []``.  A body without
        # the key is a contract break, not "no results".
        rows = body.get("results")
        if not isinstance(rows, list):
            raise AppError(502, "EXTERNAL_TOOL_RESPONSE_INVALID", "外部资料服务返回格式无效")
        results: list[dict[str, Any]] = []
        for raw in rows[:count]:
            if not isinstance(raw, dict):
                continue
            try:
                url = _public_url(raw.get("url") or "")
            except AppError:
                url = None
            if not url:
                continue
            results.append({
                "title": _bounded_text(raw.get("title") or url, 500),
                "url": url,
                "excerpt": _bounded_text(raw.get("snippet") or raw.get("summary") or raw.get("content"), 3000),
                "summary": _bounded_text(raw.get("summary"), 3000),
                "site_icon": _bounded_text(raw.get("site_icon"), 2048) or None,
                "published_at": _bounded_text(raw.get("date"), 100) or None,
                "score": raw.get("rerank_score") if isinstance(raw.get("rerank_score"), (int, float)) else None,
                "authority": raw.get("authority") if isinstance(raw.get("authority"), int) else None,
                "content": _bounded_text(raw.get("content"), 16000),
                "site_name": _bounded_text(raw.get("site_name"), 200) or None,
            })
        return results

    async def fetch(self, url: str, *, deadline: float | None = None) -> dict[str, str]:
        # Reject overlong input before validating it; truncating would send a
        # different URL than the user/model approved.
        if not isinstance(url, str) or len(url) > MAX_FETCH_URL:
            raise AppError(422, "WEB_URL_DENIED", "只能读取公开 HTTP 或 HTTPS 网页")
        safe_url = _public_url(url)
        body = await self._post("/v1/tools/fetch", json_body={"url": safe_url}, deadline=deadline)
        content = _bounded_text(body.get("content"), 16000)
        if not content.strip():
            raise AppError(422, "WEB_EMPTY", "该网页未提供可读取的正文")
        return {"title": _bounded_text(body.get("title") or safe_url, 500), "url": safe_url, "content": content}

    async def ocr_bytes(self, data: bytes, *, filename: str, mime_type: str, purpose: str = "rich_layout_recognition",
                        timeout_seconds: float | None = None, deadline: float | None = None,
                        transport=None) -> dict[str, Any]:
        """One multipart OCR request.  There is intentionally no retry here.

        The provider may keep processing (and billing) an upload after the
        client disconnects, so an ambiguous timeout must stay ambiguous rather
        than being silently re-sent.
        """
        if purpose not in OCR_PURPOSES:
            raise AppError(422, "OCR_INPUT_INVALID", "外部识别用途无效")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise AppError(422, "INVALID_DOCUMENT", "文件内容为空")
        if len(data) > MAX_FILE_BYTES:
            raise AppError(413, "DOCUMENT_TOO_LARGE", "文件超过外部识别上限")
        safe_filename = Path(str(filename or "document")).name[:255] or "document"
        safe_mime = str(mime_type or "application/octet-stream")[:120]
        if transport is not None:
            self.transport = transport
        if timeout_seconds is not None:
            self.timeout_seconds = max(1.0, min(180.0, float(timeout_seconds)))
        files = {"file": (safe_filename, bytes(data), safe_mime)}
        body = await self._post("/v1/tools/ocr", files=files, data={"purpose": purpose},
                                deadline=deadline, max_bytes=MAX_OCR_RESPONSE_BYTES)
        return parse_ocr_body(body, purpose=purpose)

    async def ocr(self, path: str | Path, *, filename: str, purpose: str = "rich_layout_recognition",
                  deadline: float | None = None) -> dict[str, Any]:
        """Legacy internal wrapper.  Never expose ``path`` as a model tool."""
        source = Path(path)
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise AppError(422, "INVALID_DOCUMENT", "文件无法读取") from exc
        if size <= 0:
            raise AppError(422, "INVALID_DOCUMENT", "文件内容为空")
        if size > MAX_FILE_BYTES:
            raise AppError(413, "DOCUMENT_TOO_LARGE", "文件超过外部识别上限")
        try:
            payload = source.read_bytes()
        except OSError as exc:
            raise AppError(422, "INVALID_DOCUMENT", "文件无法读取") from exc
        return await self.ocr_bytes(payload, filename=filename, mime_type="application/octet-stream",
                                    purpose=purpose, deadline=deadline)
