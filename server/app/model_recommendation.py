"""Bounded, reviewable model fitness recommendations.

Recommendations are advisory metadata. They do not patch a conversation or
start a run. An explicitly enabled deployment can use the bounded provider
path; the local scorer remains the safe default.
"""
from __future__ import annotations

import re
import hashlib
import json
from typing import Any

import httpx

from .usage_accounting import metered_post
from .security import AppError
from .provider_policy import TOKENHUB_BASE_URL, public_route_metadata, provider_route

# The router has one exact target.  If the catalog does not contain this
# authorized route, recommendation remains local and provider calls are denied.
ROUTER_MODEL_ID = "deepseek-v4.1-flash-1007"
TAB_COMPLETION_MODEL_ID = "deepseek-v4.1-flash-1017"
MAX_SUMMARY_CHARS = 2000
MAX_REASON_CHARS = 240
MAX_COMPLETION_INPUT_CHARS = 500
MAX_COMPLETION_OUTPUT_CHARS = 700
ROUTE_EVIDENCE_SCHEMA = "model-route-evidence/v1"
ROUTE_POLICY_VERSION = "bounded-router-v1"


def _safe_summary(value: str) -> str:
    # Do not forward obvious credentials or control characters to a future
    # router implementation. This also keeps logs/review artifacts bounded.
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value or "")
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", "[credential]", value)
    value = re.sub(r"(?i)\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+", "[credential]", value)
    return " ".join(value.split())[:MAX_SUMMARY_CHARS]


def _safe_completion(value: str) -> str:
    """Bound a draft before it can reach an optional provider call."""
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value or "")
    value = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}", "[credential]", value)
    value = re.sub(r"(?i)\b(?:api[_ -]?key|token|password|secret)\s*[:=]\s*\S+", "[credential]", value)
    return " ".join(value.split())[:MAX_COMPLETION_INPUT_CHARS]


def short_text_options(selection: dict) -> dict:
    """Keep short drafting tasks out of DeepSeek's default reasoning mode.

    Verified with the authorized route and its Anthropic compatibility API:
    https://api-docs.deepseek.com/guides/anthropic_api/
    Main chat reasoning and other model families are not changed here.
    """
    return {"thinking": {"type": "disabled"}} if "deepseek" in str(selection.get("upstream_model", "")).casefold() else {}


def _router_target(service):
    """Resolve the configured public router target without inventing routes."""
    target_id = getattr(service.settings, "model_router_model_id", "") or ROUTER_MODEL_ID
    try:
        return target_id, service.catalog.select(target_id), None
    except (AppError, ValueError, TypeError) as error:
        return target_id, None, getattr(error, "message", "授权模型线路不可用")


def _tab_target(service):
    """Resolve the separately authorized low-risk Tab route.

    The fitness router and Tab completion intentionally have independent
    public targets.  A local catalog may expose the approved 1017 deployment
    under a branded public model (for example ``dano-v2.1``); in that case an
    exact upstream suffix match is accepted without exposing the private name
    to clients.  No unrelated model is selected as a fallback.
    """
    target_id = getattr(service.settings, "tab_completion_model_id", "") or TAB_COMPLETION_MODEL_ID
    try:
        return target_id, service.catalog.select(target_id), None
    except (AppError, ValueError, TypeError) as error:
        # Keep the exact public target fail-closed first.  Only an explicit
        # catalog entry whose pinned upstream route exactly names this target may
        # act as an approved public alias.
        models = getattr(service.catalog, "models", {})
        if target_id in models:
            # An explicit catalog entry (including a disabled one) is authoritative.
            return target_id, None, getattr(error, "message", "授权模型线路不可用")
        for public_id, item in models.items():
            if not isinstance(public_id, str) or not isinstance(item, dict):
                continue
            upstream = item.get("upstream_model")
            if not isinstance(upstream, str) or not re.search(
                rf"(?:^|/){re.escape(target_id)}(?:\[[^\[\]\r\n]+\])?$", upstream, re.I
            ):
                continue
            try:
                return target_id, service.catalog.select(public_id), None
            except (AppError, ValueError, TypeError):
                continue
        return target_id, None, getattr(error, "message", "授权模型线路不可用")


def _target_metadata(target_id: str, selection, error: str | None = None) -> dict[str, Any]:
    return {
        "target_model_id": target_id,
        "target_available": selection is not None,
        "target_status": "available" if selection is not None else "unavailable",
        **({"target_error": error[:MAX_REASON_CHARS]} if error else {}),
    }


def _provider_metadata(service, selection) -> dict[str, Any]:
    """Expose provider authorization evidence without exposing credentials."""
    if selection is None:
        return {"provider_configured": False, "provider_status": "target_unavailable", "provider_attempted": False}
    try:
        provider = service.effective_provider(selection)
    except (ValueError, KeyError, TypeError):
        return {"provider_configured": False, "provider_status": "provider_invalid", "provider_attempted": False}
    configured = bool(provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN"))
    route = provider_route(provider)
    # Keep synthetic/loopback adapters available to deterministic tests, but a
    # real external endpoint is never presented as an authorized model route.
    if configured and not route.allowed:
        return {
            "provider_configured": False,
            "provider_status": "non_tokenhub_route",
            "provider_attempted": False,
            **public_route_metadata(provider),
        }
    return {
        "provider_configured": configured,
        "provider_status": "credentials_present" if configured else "credentials_missing",
        "provider_attempted": False,
        **public_route_metadata(provider),
    }


def router_preflight(service) -> dict[str, Any]:
    """Report router readiness without making a provider call.

    This endpoint is intentionally metadata-only: it exposes the exact public
    target and whether the local catalog/provider authorization can satisfy the
    opt-in route, but never exposes private upstream names, URLs, credentials,
    or a synthetic success result.  Keeping this calculation in one helper
    also makes the recommendation and Tab paths auditable with the same state.
    """
    target_id, selection, target_error = _router_target(service)
    tab_target_id, tab_selection, tab_target_error = _tab_target(service)
    provider = _provider_metadata(service, selection)
    tab_provider = _provider_metadata(service, tab_selection)
    settings = service.settings
    enabled = bool(settings.model_router_enabled)
    tab_enabled = bool(getattr(settings, "tab_completion_enabled", True))
    budget_usd = float(settings.model_router_budget_usd)
    budget_enabled = budget_usd > 0
    target_available = selection is not None
    provider_configured = bool(provider.get("provider_configured"))
    test_mode = bool(getattr(settings, "provider_test_mode", False))
    provider_production_ready = bool(provider.get("tokenhub_route"))
    tab_provider_production_ready = bool(tab_provider.get("tokenhub_route"))
    ready = enabled and budget_enabled and target_available and provider_configured and (provider_production_ready or test_mode)
    tab_target_available = tab_selection is not None
    tab_provider_configured = bool(tab_provider.get("provider_configured"))
    tab_ready = tab_enabled and budget_enabled and tab_target_available and tab_provider_configured and (tab_provider_production_ready or test_mode)
    if not tab_target_available:
        tab_completion_mode = "unavailable"
    elif not tab_enabled:
        tab_completion_mode = "disabled"
    elif not budget_enabled:
        tab_completion_mode = "budget_disabled"
    elif not tab_provider_configured or not (tab_provider_production_ready or test_mode):
        tab_completion_mode = "provider_unconfigured"
    else:
        tab_completion_mode = "ready"
    result: dict[str, Any] = {
        "schema_version": "model-router-preflight/v1",
        "policy_version": ROUTE_POLICY_VERSION,
        "target_model_id": target_id,
        "target_status": "available" if target_available else "unavailable",
        "target_available": target_available,
        "enabled": enabled,
        "budget_enabled": budget_enabled,
        "budget_usd": budget_usd,
        "provider_configured": provider_configured,
        "provider_status": provider.get("provider_status"),
        "provider_route": provider.get("provider_route"),
        "tokenhub_route": bool(provider.get("tokenhub_route")),
        "provider_test_mode": test_mode,
        "provider_attempted": False,
        "ready": ready,
        # Keep the legacy top-level target fields for the fitness router. Tab
        # has an independent target and readiness state below.
        "tab_completion_mode": tab_completion_mode,
        "tab_completion_enabled": tab_enabled,
        "tab_completion_ready": tab_ready,
        "tab_target_model_id": tab_target_id,
        "tab_target_status": "available" if tab_target_available else "unavailable",
        "tab_target_available": tab_target_available,
        "tab_provider_configured": tab_provider_configured,
        "tab_provider_status": tab_provider.get("provider_status"),
        "tab_provider_route": tab_provider.get("provider_route"),
        "tab_tokenhub_route": bool(tab_provider.get("tokenhub_route")),
        "tab_provider_test_mode": test_mode,
        "recommendation_mode": "provider" if ready else "bounded_local",
        "evidence_boundary": "仅报告本地目录、路由开关、预算和授权状态；未发起模型请求。",
    }
    if target_error:
        result["target_error"] = target_error[:MAX_REASON_CHARS]
    if tab_target_error:
        result["tab_target_error"] = tab_target_error[:MAX_REASON_CHARS]
    return result


_PUBLIC_PROVIDER_STATUS = {
    # Keep the status vocabulary useful to the browser while avoiding the
    # internal route classification (which may identify a specific upstream
    # gateway or deployment policy).
    "non_tokenhub_route": "provider_unavailable",
    "provider_invalid": "provider_unavailable",
}


def public_router_preflight(service) -> dict[str, Any]:
    """Return brand-neutral router readiness for authenticated end users.

    ``router_preflight`` remains the internal, auditable representation and
    deliberately retains route classification fields.  This serializer is
    the boundary used by the browser API: it keeps public model aliases and
    readiness state, but never sends provider route names or gateway flags.
    """
    internal = router_preflight(service)
    route_fields = {
        "provider_route",
        "tokenhub_route",
        "tab_provider_route",
        "tab_tokenhub_route",
    }
    public = {key: value for key, value in internal.items() if key not in route_fields}
    for key in ("provider_status", "tab_provider_status"):
        value = public.get(key)
        public[key] = _PUBLIC_PROVIDER_STATUS.get(value, value)
    # The mode is a product decision, not an upstream implementation detail.
    if public.get("recommendation_mode") == "provider":
        public["recommendation_mode"] = "authorized"
    return public


def public_router_result(result: dict[str, Any]) -> dict[str, Any]:
    """Project a router/completion result for the authenticated browser.

    Provider route classification is retained in server-side run metadata for
    audit, but it is not a product-facing response field.  Keep this boundary
    separate from the internal helpers so tests and operators can still inspect
    the fail-closed decision without exposing gateway names to end users.
    """
    if not isinstance(result, dict):
        return result
    public = dict(result)
    router = public.get("router")
    if isinstance(router, dict):
        router_public = dict(router)
        for key in ("provider_route", "provider_route_reason", "tokenhub_route",
                    "tab_provider_route", "tab_provider_route_reason", "tab_tokenhub_route"):
            router_public.pop(key, None)
        for key in ("provider_status", "tab_provider_status"):
            value = router_public.get(key)
            router_public[key] = _PUBLIC_PROVIDER_STATUS.get(value, value)
        if router_public.get("mode") == "provider":
            router_public["mode"] = "authorized"
        if router_public.get("recommendation_mode") == "provider":
            router_public["recommendation_mode"] = "authorized"
        public["router"] = router_public
    return public


def _mark_provider_attempt(router: dict[str, Any], attempted: bool = True) -> None:
    """Record bounded provider-call state in route evidence."""
    router["provider_attempted"] = attempted
    evidence = router.get("evidence")
    if isinstance(evidence, dict):
        evidence["provider_attempted"] = attempted


def _route_evidence(
    summary: str,
    candidate_ids: list[str],
    *,
    mode: str,
    target_id: str = ROUTER_MODEL_ID,
    target_status: str = "unresolved",
    selected_model_id: str | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    """Return bounded, non-content route evidence for audit and replay.

    The route decision must be explainable without persisting the user's task
    text or provider secrets. A digest lets an operator correlate a decision
    with a request held by the caller, while the public candidate IDs and
    policy version make the decision reproducible from the catalog snapshot.
    """
    canonical = json.dumps(
        {
            "summary": summary,
            "candidate_ids": list(candidate_ids),
            "target_id": target_id,
            "policy_version": ROUTE_POLICY_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    result: dict[str, Any] = {
        "schema_version": ROUTE_EVIDENCE_SCHEMA,
        "policy_version": ROUTE_POLICY_VERSION,
        "decision_mode": mode,
        "input_digest": hashlib.sha256(canonical).hexdigest(),
        "candidate_model_ids": list(candidate_ids),
        "target_model_id": target_id,
        "target_status": target_status,
    }
    if selected_model_id:
        result["selected_model_id"] = selected_model_id
    if fallback_reason:
        result["fallback_reason"] = fallback_reason[:MAX_REASON_CHARS]
    return result


def enrich_route_metadata(router: dict[str, Any], target_id: str, selection, error: str | None = None) -> dict[str, Any]:
    """Attach resolved target status to both legacy and evidence fields."""
    router.update(_target_metadata(target_id, selection, error))
    evidence = router.get("evidence")
    if isinstance(evidence, dict):
        evidence.update({
            "target_model_id": target_id,
            "target_status": "available" if selection is not None else "unavailable",
        })
        if error:
            evidence["fallback_reason"] = error[:MAX_REASON_CHARS]
    return router


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
            "evidence": _route_evidence(
                summary,
                [item["id"] for item in candidates],
                mode="bounded_local",
                selected_model_id=selected["id"],
            ),
        },
    }


async def provider_router(service, task_summary: str, candidate_model_ids: list[str] | None = None, *, user_id=None) -> dict[str, Any]:
    """Use one bounded DeepSeek V4.1 Flash call when explicitly enabled.

    Provider failures, invalid JSON and out-of-catalog IDs fall back to the
    local scorer. The router receives only the sanitized summary and public
    catalog capabilities; it never receives a conversation, credentials or
    private upstream deployment IDs.
    """
    fallback = bounded_router(service.catalog, task_summary, candidate_model_ids)
    settings = service.settings
    target_id, target_selection, target_error = _router_target(service)
    enrich_route_metadata(fallback["router"], target_id, target_selection, target_error)
    fallback["router"].update(_provider_metadata(service, target_selection))
    if not settings.model_router_enabled or settings.model_router_budget_usd <= 0 or target_selection is None:
        return fallback
    try:
        provider = service.effective_provider(target_selection)
    except (ValueError, KeyError, TypeError):
        return fallback
    route = provider_route(provider)
    if not (route.production_ready or (getattr(settings, "provider_test_mode", False) and route.allowed)):
        fallback["router"]["fallback_reason"] = "non_tokenhub_route"
        evidence = fallback["router"].get("evidence")
        if isinstance(evidence, dict):
            evidence["fallback_reason"] = "non_tokenhub_route"
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
    base = provider.get("ANTHROPIC_BASE_URL", TOKENHUB_BASE_URL).rstrip("/")
    endpoint = base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if provider.get("ANTHROPIC_API_KEY"):
        headers["x-api-key"] = provider["ANTHROPIC_API_KEY"]
    else:
        headers["authorization"] = f"Bearer {token}"
    payload = {"model": target_selection["upstream_model"], "max_tokens": max(32, min(256, settings.model_router_max_output_tokens)), "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
    _mark_provider_attempt(fallback["router"])
    try:
        timeout = httpx.Timeout(max(1, min(15, settings.model_router_timeout_seconds)), connect=3)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=False) as http:
            response = await metered_post(http, endpoint, service=service, kind="routing", selection=target_selection, payload=payload, user_id=user_id)
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
        provider_router_metadata = {**fallback["router"], "model_id": target_id, "mode": "provider", "estimated_cost_usd": None, "budget_usd": settings.model_router_budget_usd}
        provider_router_metadata["evidence"] = _route_evidence(
            summary,
            [item["id"] for item in allowed],
            mode="provider",
            target_id=target_id,
            target_status="available",
            selected_model_id=chosen["id"],
        )
        _mark_provider_attempt(provider_router_metadata)
        return {**fallback, "model_id": chosen["id"], "model_label": chosen["label"], "reasoning_level": chosen["default_reasoning_level"], "reason": reason, "router": provider_router_metadata}
    # A compatible gateway can return a valid HTTP response with a malformed
    # JSON shape (for example a top-level list or ``null``).  Treat that as a
    # bounded provider rejection rather than letting ``body.get`` raise an
    # uncaught AttributeError and turning a recommendation into a 500.
    except (httpx.HTTPError, ValueError, TypeError, StopIteration, KeyError, AttributeError):
        fallback["router"]["fallback_reason"] = "provider_response_rejected"
        evidence = fallback["router"].get("evidence")
        if isinstance(evidence, dict):
            evidence["fallback_reason"] = "provider_response_rejected"
        return fallback


async def provider_completion(service, draft: str, *, user_id=None) -> dict[str, Any]:
    """Generate a real Tab completion through the explicitly authorized route.

    A missing Tab route, disabled Tab switch, or invalid provider result returns a
    null suggestion. The client must not silently replace that with a hard-coded
    phrase, which keeps completion evidence honest and cost bounded. This route
    is independent from the Fitness recommendation target.
    """
    if len(draft) > MAX_COMPLETION_INPUT_CHARS:
        return {"suggestion": None, "router": {"mode": "input_too_long", "provider_attempted": False}}
    text = _safe_completion(draft)
    if not text:
        raise AppError(422, "EMPTY_COMPLETION_TEXT", "请输入需要补全的内容")
    settings = service.settings
    target_id, target_selection, target_error = _tab_target(service)
    tab_enabled = bool(getattr(settings, "tab_completion_enabled", True))
    if target_selection is None:
        mode = "unavailable"
    elif not tab_enabled:
        mode = "disabled"
    elif settings.model_router_budget_usd <= 0:
        mode = "budget_disabled"
    else:
        mode = "ready"
    metadata = {
        "model_id": target_id,
        "mode": mode,
        "input_chars": len(text),
        "max_output_chars": MAX_COMPLETION_OUTPUT_CHARS,
        **_target_metadata(target_id, target_selection, target_error),
        **_provider_metadata(service, target_selection),
        "evidence": _route_evidence(
            text,
            [target_id],
            mode=mode,
            target_id=target_id,
            target_status="available" if target_selection is not None else "unavailable",
            selected_model_id=target_id if target_selection is not None else None,
            fallback_reason=target_error,
        ),
    }
    result = {"suggestion": None, "router": metadata}
    if not tab_enabled or settings.model_router_budget_usd <= 0 or target_selection is None:
        return result
    try:
        provider = service.effective_provider(target_selection)
    except (ValueError, KeyError, TypeError):
        result["router"].update(mode="provider_invalid", rejection_reason="provider_invalid")
        evidence = result["router"].get("evidence")
        if isinstance(evidence, dict):
            evidence.update(decision_mode="provider_invalid", fallback_reason="provider_invalid")
        return result
    route = provider_route(provider)
    if not (route.production_ready or (getattr(settings, "provider_test_mode", False) and route.allowed)):
        result["router"].update(mode="provider_invalid", rejection_reason="non_tokenhub_route")
        evidence = result["router"].get("evidence")
        if isinstance(evidence, dict):
            evidence.update(decision_mode="provider_invalid", fallback_reason="non_tokenhub_route")
        return result
    token = provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        result["router"]["mode"] = "provider_unconfigured"
        evidence = result["router"].get("evidence")
        if isinstance(evidence, dict):
            evidence["decision_mode"] = "provider_unconfigured"
            evidence["fallback_reason"] = "credentials_missing"
        return result
    prompt = (
        "补全用户正在输入的工业任务。只返回 JSON，不要 markdown："
        '{"completion":"完整句子"}。completion 必须原样保留 draft 前缀，'
        "保持用户的业务意图、数据范围和已提出的指标。只能续完未完成句子，或追加展示格式、"
        "来源核对等表达要求；不得新增业务指标（例如未提及的退款额）、日期、筛选条件、"
        "统计粒度或操作步骤，不得假设工具能力。若任务已完整则只补一句简短的输出要求。"
        "最多 500 个字符，不得添加凭据。\n"
        f"draft={text}"
    )
    base = provider.get("ANTHROPIC_BASE_URL", TOKENHUB_BASE_URL).rstrip("/")
    endpoint = base if base.endswith("/messages") else base + ("/messages" if base.endswith("/v1") else "/v1/messages")
    headers = {"anthropic-version": "2023-06-01", "content-type": "application/json"}
    if provider.get("ANTHROPIC_API_KEY"):
        headers["x-api-key"] = provider["ANTHROPIC_API_KEY"]
    else:
        headers["authorization"] = f"Bearer {token}"
    payload = {"model": target_selection["upstream_model"], "max_tokens": max(32, min(256, settings.model_router_max_output_tokens)), "temperature": 0, "messages": [{"role": "user", "content": prompt}], **short_text_options(target_selection)}
    result["router"]["provider_attempted"] = True
    evidence = result["router"].get("evidence")
    if isinstance(evidence, dict):
        evidence["provider_attempted"] = True
    try:
        timeout = httpx.Timeout(max(1, min(15, settings.model_router_timeout_seconds)), connect=3)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=False, trust_env=False) as http:
            response = await metered_post(http, endpoint, service=service, kind="completion", selection=target_selection, payload=payload, user_id=user_id)
            response.raise_for_status()
            body = response.json()
        raw = next((item.get("text", "") for item in body.get("content", []) if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text")), "")
        raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw, flags=re.I)
        value = __import__("json").loads(raw)
        completion = value.get("completion") if isinstance(value, dict) else None
        if not isinstance(completion, str):
            result["router"].update(mode="provider_response_rejected", rejection_reason="completion_schema_invalid")
            evidence = result["router"].get("evidence")
            if isinstance(evidence, dict):
                evidence.update(decision_mode="provider_response_rejected", fallback_reason="completion_schema_invalid")
            return result
        completion = re.sub(r"[\x00-\x1f\x7f]", " ", completion).strip()[:MAX_COMPLETION_OUTPUT_CHARS]
        if not completion.startswith(text):
            result["router"].update(mode="provider_response_rejected", rejection_reason="completion_prefix_violation")
            evidence = result["router"].get("evidence")
            if isinstance(evidence, dict):
                evidence.update(decision_mode="provider_response_rejected", fallback_reason="completion_prefix_violation")
            return result
        # The bounded provider prompt normalizes whitespace and redacts secrets.
        # Only append its new suffix; never replace the user's original prefix.
        suffix = completion[len(text):]
        if not suffix.strip() or _safe_summary(suffix) != " ".join(suffix.split()) or len(draft + suffix) > MAX_COMPLETION_OUTPUT_CHARS:
            result["router"].update(mode="provider_response_rejected", rejection_reason="completion_suffix_invalid")
            if isinstance(evidence, dict):
                evidence.update(decision_mode="provider_response_rejected", fallback_reason="completion_suffix_invalid")
            return result
        provider_metadata = {**metadata, "mode": "provider"}
        provider_metadata["evidence"] = _route_evidence(
            text,
            [target_id],
            mode="provider",
            target_id=target_id,
            target_status="available",
            selected_model_id=target_id,
        )
        provider_metadata["evidence"]["provider_attempted"] = True
        return {"suggestion": draft + suffix, "router": provider_metadata}
    # Keep malformed gateway envelopes fail-closed.  ``body`` is not trusted
    # merely because the HTTP status was successful; a non-object response
    # must yield a null suggestion and auditable rejection metadata.
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
        result["router"].update(mode="provider_response_rejected", rejection_reason="provider_response_invalid")
        evidence = result["router"].get("evidence")
        if isinstance(evidence, dict):
            evidence.update(decision_mode="provider_response_rejected", fallback_reason="provider_response_invalid")
        return result
