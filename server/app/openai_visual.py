"""OpenAI Chat Completions visual adapter for bounded knowledge indexing.

The adapter accepts image bytes only from an already authorized knowledge
document. It records model, latency and a response digest, never raw response
text or credentials. JSON extraction is deliberately validated by the caller;
an image model response is a candidate, not an authoritative graph fact.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from .security import AppError
from .provider_policy import classify_provider_url


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PROMPT_CHARS = 4000
MAX_RESPONSE_CHARS = 16000
MAX_RESPONSE_BYTES = 1024 * 1024


def _safe_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return " ".join(value.split())[:MAX_RESPONSE_CHARS]


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        if len(value) > MAX_RESPONSE_CHARS:
            raise AppError(502, "VISUAL_MODEL_RESPONSE_TOO_LARGE", "视觉模型返回内容超过上限")
        return _safe_text(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        value = "\n".join(parts)
        if len(value) > MAX_RESPONSE_CHARS:
            raise AppError(502, "VISUAL_MODEL_RESPONSE_TOO_LARGE", "视觉模型返回内容超过上限")
        return _safe_text(value)
    return ""


def _endpoint(base_url: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        raise AppError(503, "VISUAL_MODEL_UNAVAILABLE", "视觉模型服务尚未配置")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AppError(422, "VISUAL_ENDPOINT_INVALID", "视觉模型服务地址无效")
    route = classify_provider_url(base)
    if not route.allowed:
        raise AppError(503, "TOKENHUB_ROUTE_REQUIRED", "视觉模型服务线路未满足安全部署策略，请联系管理员")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


async def call_visual_json(
    *,
    base_url: str,
    model: str,
    image: bytes,
    mime_type: str,
    prompt: str,
    timeout_seconds: float = 30,
    auth_token: str = "",
    request_post=None,
) -> dict[str, Any]:
    """Call a configured OpenAI-compatible visual model with one image.

    The function is not invoked by default during upload. The caller must
    explicitly enable the visual stage and provide a role-specific target.
    """
    if not isinstance(image, bytes) or not image:
        raise AppError(422, "VISUAL_INPUT_INVALID", "图片内容为空")
    if len(image) > MAX_IMAGE_BYTES:
        raise AppError(413, "VISUAL_INPUT_TOO_LARGE", "图片超过视觉模型单次处理上限")
    model = _safe_text(model)
    if not model:
        raise AppError(503, "VISUAL_MODEL_UNAVAILABLE", "视觉模型尚未配置")
    prompt = _safe_text(prompt)[:MAX_PROMPT_CHARS]
    if not prompt:
        raise AppError(422, "VISUAL_INPUT_INVALID", "视觉模型提示词为空")
    endpoint = _endpoint(base_url)
    media_type = mime_type if isinstance(mime_type, str) and mime_type.startswith("image/") else "image/jpeg"
    encoded = base64.b64encode(image).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 1200,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{encoded}"}},
        ]}],
    }
    if model.casefold() == "hy4":
        # This bounded extraction role needs a final JSON answer, not Hy4's
        # default high reasoning consuming the entire output allowance.
        # https://cloud.tencent.com/document/product/1823/131208
        payload["thinking"] = {"type": "disabled"}
    headers = {"content-type": "application/json"}
    if auth_token:
        headers["authorization"] = "Bearer " + auth_token
    started = time.monotonic()
    try:
        timeout = httpx.Timeout(max(1, min(120, float(timeout_seconds))), connect=5)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = (await request_post(client, endpoint, payload) if request_post is not None
                        else await client.post(endpoint, json=payload))
            raw = response.content
            if not isinstance(raw, (bytes, bytearray)):
                raise AppError(502, "VISUAL_MODEL_RESPONSE_INVALID", "视觉模型返回格式无效")
            if len(raw) > MAX_RESPONSE_BYTES:
                raise AppError(502, "VISUAL_MODEL_RESPONSE_TOO_LARGE", "视觉模型响应超过上限")
            response_hash = hashlib.sha256(bytes(raw)).hexdigest()
            if response.status_code >= 400:
                if response.status_code in (401, 403):
                    code, message = "VISUAL_MODEL_AUTHORIZATION", "视觉模型服务认证失败"
                elif response.status_code == 404:
                    code, message = "VISUAL_MODEL_NOT_FOUND", "视觉模型线路不存在"
                elif response.status_code == 429:
                    code, message = "VISUAL_MODEL_RATE_LIMITED", "视觉模型服务达到限流或额度限制"
                elif response.status_code == 408:
                    code, message = "VISUAL_MODEL_TIMEOUT", "视觉模型服务请求超时"
                else:
                    code, message = "VISUAL_MODEL_REQUEST_FAILED", "视觉模型服务请求失败"
                raise AppError(502, code, message)
            try:
                body = response.json()
            except ValueError as exc:
                raise AppError(502, "VISUAL_MODEL_RESPONSE_INVALID", "视觉模型返回格式无效") from exc
    except AppError:
        raise
    except httpx.TimeoutException as exc:
        raise AppError(504, "VISUAL_MODEL_TIMEOUT", "视觉模型服务响应超时") from exc
    except httpx.HTTPError as exc:
        raise AppError(503, "VISUAL_MODEL_UNAVAILABLE", "视觉模型服务暂时不可用") from exc
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    choices = body.get("choices") if isinstance(body, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict) and choices[0].get("finish_reason") == "length":
        raise AppError(502, "VISUAL_MODEL_OUTPUT_LIMIT", "图片解析输出达到上限，未生成完整结果")
    message = choices[0].get("message") if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    content = _content_text(message.get("content") if isinstance(message, dict) else None)
    if not content:
        raise AppError(502, "VISUAL_MODEL_RESPONSE_INVALID", "视觉模型未返回可解析内容")
    # Parse JSON when the model obeys the requested schema; leave plain text as
    # a bounded candidate so the caller can reject it without losing evidence.
    parsed: Any = None
    candidate = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content, flags=re.I)
    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        parsed = None
    if parsed is not None and not isinstance(parsed, dict):
        parsed = None
    usage = body.get("usage") if isinstance(body, dict) and isinstance(body.get("usage"), dict) else {}
    safe_usage = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens") if type(usage.get(key)) is int and usage[key] >= 0}
    return {
        "model": model,
        "latency_ms": elapsed_ms,
        "response_sha256": response_hash,
        "content": content,
        "json": parsed,
        "usage": safe_usage,
    }
