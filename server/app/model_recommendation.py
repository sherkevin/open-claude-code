"""Bounded, reviewable model fitness recommendations.

Recommendations are advisory metadata. They do not patch a conversation or
start a run. An explicitly enabled deployment can use the bounded provider
path; the local scorer remains the safe default.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from .security import AppError

ROUTER_MODEL_ID = "deepseek-v4.1-flash"
MAX_SUMMARY_CHARS = 2000
MAX_REASON_CHARS = 240


def _safe_summary(value: str) -> str:
    # Do not forward obvious credentials or control characters to a future
    # router implementation. This also keeps logs/review artifacts bounded.
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value or "")
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", "[credential]", value)
    value = re.sub(r"(?i)\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+", "[credential]", value)
    return " ".join(value.split())[:MAX_SUMMARY_CHARS]


def _score(item: dict[str, Any], summary: str) -> tuple[int, str]:
    text = summary.casefold()
    capabilities = set(item.get("capabilities", []))
    weights = {
        "multimodal": (40, ("image", "图片", "视觉", "图像", "音频", "视频")),
        "long_context": (30, ("long context", "长上下文", "大量文档", "长文档", "整本")),
        "coding": (20, ("code", "代码", "编程", "debug", "脚本")),
        "reasoning": (15, ("reason", "推理", "分析", "规划", "复杂")),
        "chinese": (10, ("中文", "中国", "汉语")),
        "industrial": (10, ("设备", "工业", "生产", "质量", "工厂")),
    }
    score, matched = 0, []
    for capability, (weight, keywords) in weights.items():
        if capability in capabilities and any(keyword in text for keyword in keywords):
            score += weight
            matched.append(capability)
    return score, ", ".join(matched)


def bounded_router(catalog, task_summary: str, candidate_model_ids: list[str] | None = None) -> dict[str, Any]:
    """Return a deterministic recommendation under a fixed input/output bound.

    The router identity is pinned to DeepSeek V4.1 Flash in the response. The
    local bounded scorer is intentionally the safe default until an operator
    explicitly enables a provider-backed router; no credentials or history are
    sent by this function.
    """
    summary = _safe_summary(task_summary)
    if not summary:
        raise AppError(422, "EMPTY_TASK_SUMMARY", "请描述需要完成的任务")
    requested = candidate_model_ids or list(catalog.models)
    candidates = []
    for model_id in requested:
        if not isinstance(model_id, str) or model_id not in catalog.models:
            continue
        item = catalog.models[model_id]
        if item.get("enabled", True) and item.get("upstream_model"):
            candidates.append(item)
    if not candidates:
        raise AppError(503, "NO_AVAILABLE_MODELS", "当前没有可推荐的模型")
    ranked = sorted(candidates, key=lambda item: (-_score(item, summary)[0], list(catalog.models).index(item["id"])))
    selected = ranked[0]
    score, matched = _score(selected, summary)
    reason = (f"根据任务摘要匹配公开能力：{matched}。" if matched else "根据当前可用模型与任务摘要，选择默认候选。")
    return {
        "model_id": selected["id"],
        "model_label": selected["label"],
        "reasoning_level": selected["default_reasoning_level"],
        "reason": reason[:MAX_REASON_CHARS],
        "applied": False,
        "router": {
            "model_id": ROUTER_MODEL_ID,
            "mode": "bounded_local",
            "input_chars": len(summary),
            "candidate_count": len(candidates),
            "max_output_chars": MAX_REASON_CHARS,
            "estimated_cost_usd": 0,
        },
    }


async def provider_router(service, task_summary: str, candidate_model_ids: list[str] | None = None) -> dict[str, Any]:
    """Use one bounded DeepSeek V4.1 Flash call when explicitly enabled.

    Provider failures, invalid JSON and out-of-catalog IDs fall back to the
    local scorer. The router receives only the sanitized summary and public
    catalog capabilities; it never receives a conversation, credentials or
    private upstream deployment IDs.
    """
    fallback = bounded_router(service.catalog, task_summary, candidate_model_ids)
    settings = service.settings
    if not settings.model_router_enabled or settings.model_router_budget_usd <= 0:
        return fallback
    try:
        router_selection = service.catalog.select(ROUTER_MODEL_ID)
    except AppError:
        return fallback
    try:
        provider = service.effective_provider(router_selection)
    except (ValueError, KeyError, TypeError):
        return fallback
    token = provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        return fallback
    summary = _safe_summary(task_summary)
    allowed = [item for item in service.catalog.public() if item["available"] and (not candidate_model_ids or item["id"] in candidate_model_ids)]
    if not allowed:
        return fallback
    public_candidates = [{"id": item["id"], "label": item["label"], "capabilities": item.get("capabilities", []), "reasoning_levels": [level["id"] for level in item["reasoning_levels"]]} for item in allowed]
    prompt = ("选择最适合任务的模型，只能从 candidates 中选择。返回 JSON："
              '{"model_id":"...","reason":"不超过 180 字"}\n'
              f"task_summary={summary}\ncandidates={public_candidates}")
    base = provider.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
    endpoint = base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if provider.get("ANTHROPIC_API_KEY"):
        headers["x-api-key"] = provider["ANTHROPIC_API_KEY"]
    else:
        headers["authorization"] = f"Bearer {token}"
    payload = {"model": router_selection["upstream_model"], "max_tokens": max(32, min(256, settings.model_router_max_output_tokens)), "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    try:
        timeout = httpx.Timeout(max(1, min(15, settings.model_router_timeout_seconds)), connect=3)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=False) as http:
            response = await http.post(endpoint, json=payload)
            response.raise_for_status()
            body = response.json()
        text = next((item.get("text", "") for item in body.get("content", [])
                     if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text")), "")
        # Some compatible gateways wrap strict JSON in a markdown fence. The
        # router accepts only the bounded object, never free-form model text.
        text = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text, flags=re.I)
        value = __import__("json").loads(text)
        chosen = next(item for item in allowed if item["id"] == value.get("model_id"))
        reason = " ".join(str(value.get("reason", "")).split())[:MAX_REASON_CHARS] or fallback["reason"]
        return {**fallback, "model_id": chosen["id"], "model_label": chosen["label"], "reasoning_level": chosen["default_reasoning_level"], "reason": reason, "router": {**fallback["router"], "mode": "provider", "estimated_cost_usd": None, "budget_usd": settings.model_router_budget_usd}}
    except (httpx.HTTPError, ValueError, TypeError, StopIteration, KeyError):
        return fallback
