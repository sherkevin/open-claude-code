"""Fail-closed preflight for non-chat knowledge model roles.

The public catalog owns model identity and the provider settings own secrets.
This module only reports whether a role can be used; it never returns an
upstream identifier, endpoint, or credential and never makes a provider call.
The knowledge index uses the same explicit visual role selection when its
provider stage is enabled; ordinary uploads remain local-only by default.
"""
from __future__ import annotations

from typing import Any

from .model_recommendation import _provider_metadata


ROLE_DEFINITIONS: dict[str, dict[str, str]] = {
    "embedding": {"setting": "knowledge_embedding_model_id", "capability": "embedding"},
    "vision": {"setting": "knowledge_vision_model_id", "capability": "multimodal"},
    "entity_extraction": {"setting": "knowledge_entity_model_id", "capability": "entity_extraction"},
    "relation_extraction": {"setting": "knowledge_relation_model_id", "capability": "relation_extraction"},
}


def _role_enabled(service, role: str) -> bool:
    """Return the explicit opt-in state for a model role.

    Knowledge visual/entity/relation adapters share the visual feature gate.
    There is intentionally no implicit enablement based on a loopback URL or
    on a catalog capability: preflight must describe configuration, not infer
    that an adapter is safe to call.
    """
    if role in {"vision", "entity_extraction", "relation_extraction"}:
        return bool(getattr(service.settings, "knowledge_vision_enabled", False))
    return False


def _target(service, role: str):
    """Resolve an explicit role target or an exact capability declaration."""
    definition = ROLE_DEFINITIONS[role]
    settings = service.settings
    explicit = str(getattr(settings, definition["setting"], "") or "").strip()
    if explicit:
        try:
            return explicit, service.catalog.select(explicit), "explicit"
        except Exception:
            return explicit, None, "unavailable"
    # Capability auto-selection is deliberately narrow: only a catalog entry
    # that explicitly declares the role is eligible, never a generic chat route.
    for public_id, item in getattr(service.catalog, "models", {}).items():
        if not isinstance(item, dict) or not item.get("enabled", True) or not item.get("upstream_model"):
            continue
        capabilities = item.get("capabilities", [])
        if definition["capability"] not in capabilities:
            continue
        try:
            return public_id, service.catalog.select(public_id), "capability"
        except Exception:
            continue
    return "", None, "unconfigured"


def _role_view(service, role: str) -> dict[str, Any]:
    target_id, selection, source = _target(service, role)
    try:
        provider = _provider_metadata(service, selection)
    except (ValueError, KeyError, TypeError, OSError):
        # A malformed or unreadable provider settings file is an evidence gap,
        # not a server error and never makes a role look ready.
        provider = {"provider_configured": False, "provider_status": "provider_invalid", "provider_attempted": False}
    configured = bool(provider.get("provider_configured"))
    target_available = selection is not None
    if not target_available:
        status = "unavailable" if target_id else "unconfigured"
    elif not configured:
        status = "provider_unconfigured"
    elif not _role_enabled(service, role):
        status = "disabled"
    else:
        status = "ready"
    # Keep this response suitable for the browser and audit logs.  The model
    # catalog's private upstream route is never copied into the response.
    return {
        "role": role,
        "target_model_id": target_id or None,
        "target_available": target_available,
        "target_source": source,
        "status": status,
        "provider_configured": configured,
        "provider_status": provider.get("provider_status"),
        "provider_route": provider.get("provider_route"),
        "tokenhub_route": bool(provider.get("tokenhub_route")),
        "provider_attempted": False,
        # Preflight never grants call permission. The indexing adapter has its
        # own explicit feature gate and authorization check at call time.
        "call_enabled": False,
        "feature_enabled": _role_enabled(service, role),
        "protocol": "openai.chat.completions" if role in {"vision", "entity_extraction", "relation_extraction"} else None,
        "evidence_boundary": "仅报告本地目录与授权状态；未发起视觉、向量、实体或关系模型请求。",
    }


def _public_role_view(view: dict[str, Any]) -> dict[str, Any]:
    """Strip provider implementation details at the browser API boundary."""
    public = dict(view)
    public.pop("provider_route", None)
    public.pop("tokenhub_route", None)
    if public.get("provider_status") == "non_tokenhub_route":
        public["provider_status"] = "provider_unavailable"
    # Preserve a useful readiness bit without naming the upstream gateway.
    public["provider_ready"] = bool(view.get("tokenhub_route"))
    return public


def provider_preflight(service) -> dict[str, Any]:
    """Return safe readiness metadata for all model roles.

    ``call_enabled`` remains false until a role-specific adapter is explicitly
    enabled.  This makes an accidental chat-model fallback impossible while
    still allowing a deployment to configure and test each role independently.
    """
    from .model_recommendation import router_preflight

    try:
        router = router_preflight(service)
    except (ValueError, KeyError, TypeError, OSError):
        # Provider settings are operator input. A malformed file must not turn
        # an authenticated metadata endpoint into a 500 or a false-ready
        # result. Keep the public target names but fail closed on authorization.
        router = {
            "target_model_id": None,
            "target_available": False,
            "provider_configured": False,
            "provider_status": "provider_invalid",
            "tab_target_model_id": None,
            "tab_target_available": False,
            "tab_provider_configured": False,
            "tab_completion_mode": "unavailable",
        }
    roles = {role: _public_role_view(_role_view(service, role)) for role in ROLE_DEFINITIONS}
    return {
        "schema_version": "provider-preflight/v1",
        "router": {
            "fitness": {
                "target_model_id": router.get("target_model_id"),
                "target_available": router.get("target_available"),
                "provider_configured": router.get("provider_configured"),
                "status": "ready" if router.get("ready") else router.get("provider_status"),
            },
            "tab": {
                "target_model_id": router.get("tab_target_model_id"),
                "target_available": router.get("tab_target_available"),
                "provider_configured": router.get("tab_provider_configured"),
                "status": router.get("tab_completion_mode"),
            },
        },
        "roles": roles,
        "provider_attempted": False,
        "evidence_boundary": "仅报告本地目录、配置开关和授权状态；没有请求模型服务，也没有读取真实客户数据。",
    }
