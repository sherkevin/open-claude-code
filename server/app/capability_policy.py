"""Pure, synchronous capability policy shared by the control plane and evidence.

This module never performs network I/O, never consumes quotas and never writes
audit rows.  It is the single place that computes an effective capability from
the intersection of deployment, user, assistant, run snapshot and global route
grants, so the tool authorization path and historical source reads cannot drift
apart.

``Service.authorize_tool`` remains the only tool authorization entry point; it
delegates capability tools here.  There is no second approval system.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
from typing import Any

from .security import AppError

CAPABILITIES: tuple[str, ...] = ("search", "fetch", "ocr", "image_search")

CAPABILITY_TOOLS: dict[str, str] = {
    "search": "mcp__workspace__web_search",
    "fetch": "mcp__workspace__web_read",
    "ocr": "mcp__workspace__ocr",
    "image_search": "mcp__workspace__image_search",
}
TOOL_CAPABILITIES: dict[str, str] = {tool: kind for kind, tool in CAPABILITY_TOOLS.items()}

# Relative strictness: deny < approval < allow.
EGRESS_MODES = ("deny", "approval", "allow")
_EGRESS_RANK = {mode: index for index, mode in enumerate(EGRESS_MODES)}

PUBLIC_DOMAIN_POLICY = {"mode": "public", "hosts": []}


def capability_for_tool(tool_name: str) -> str | None:
    return TOOL_CAPABILITIES.get(tool_name)


def capability_for_locator(locator: dict, tool_name: str | None = None) -> str | None:
    """Map a trusted evidence row to its capability without widening grants.

    New rows carry ``locator.capability``.  Legacy web rows are mapped from the
    trusted ``content_type`` written by the existing adapters; a row with no
    recognizable capability is rejected rather than treated as all four.
    """
    declared = locator.get("capability") if isinstance(locator, dict) else None
    if declared in CAPABILITIES:
        return declared
    if tool_name and tool_name in TOOL_CAPABILITIES:
        return TOOL_CAPABILITIES[tool_name]
    content_type = locator.get("content_type") if isinstance(locator, dict) else None
    if content_type == "search_excerpt":
        return "search"
    if content_type == "page_text":
        return "fetch"
    if content_type == "image_search_result":
        return "image_search"
    extraction = locator.get("extraction") if isinstance(locator, dict) else None
    if extraction:
        return "ocr"
    return None


def normalize_domain(value: object) -> str | None:
    """Return a canonical exact hostname, or ``None`` for an unsafe value.

    Only exact hostnames are supported: no scheme, path, port, credentials,
    wildcard, IP range or control characters.  Trailing dots are removed and
    IDNA is applied before lower-casing, so ``example.com`` and ``example.com.``
    compare equal without suffix tricks.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 253 or raw != value.strip():
        return None
    if any(ord(char) < 33 or char in "/\\?#@*[]:" for char in raw):
        return None
    if raw.endswith("."):
        raw = raw[:-1]
    if not raw:
        return None
    try:
        ascii_host = raw.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    if not ascii_host or len(ascii_host) > 253 or "." * 2 in ascii_host:
        return None
    labels = ascii_host.split(".")
    if any(not label or len(label) > 63 or label[0] == "-" or label[-1] == "-" for label in labels):
        return None
    if ascii_host == "localhost" or ascii_host.endswith((".localhost", ".local", ".internal", ".arpa")):
        return None
    try:
        address = ipaddress.ip_address(ascii_host)
    except ValueError:
        address = None
    if address is not None:
        # Single public literals are representable, but private ranges are not.
        from .web_tools import global_unicast
        if not global_unicast(ascii_host):
            return None
    return ascii_host


def normalize_domain_policy(value: object) -> dict:
    """Validate a domain policy object and return a canonical copy.

    ``allowlist`` with an empty ``hosts`` list denies everything.  ``public``
    still relies on the network layer's public-address checks.
    """
    if not isinstance(value, dict) or set(value) - {"mode", "hosts"}:
        raise AppError(422, "CAPABILITY_DOMAIN_INVALID", "域名策略格式无效")
    mode = value.get("mode")
    if mode not in {"public", "allowlist"}:
        raise AppError(422, "CAPABILITY_DOMAIN_INVALID", "域名策略模式无效")
    raw_hosts = value.get("hosts") or []
    if not isinstance(raw_hosts, list) or len(raw_hosts) > 64:
        raise AppError(422, "CAPABILITY_DOMAIN_INVALID", "域名列表无效")
    hosts = []
    for item in raw_hosts:
        host = normalize_domain(item)
        if host is None:
            raise AppError(422, "CAPABILITY_DOMAIN_INVALID", "域名必须是精确主机名")
        if host not in hosts:
            hosts.append(host)
    return {"mode": mode, "hosts": hosts}


def host_from_target(target: object) -> str | None:
    """Extract a canonical host from a URL or bare hostname."""
    if not isinstance(target, str) or not target.strip():
        return None
    from urllib.parse import urlsplit
    candidate = target.strip()
    if "://" not in candidate:
        return normalize_domain(candidate)
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return normalize_domain(parsed.hostname)


def domain_allows(policy: dict, target: object) -> bool:
    """Whether a result/target host satisfies a canonical domain policy."""
    host = host_from_target(target)
    if host is None:
        return False
    mode = policy.get("mode", "public")
    if mode == "allowlist":
        return host in set(policy.get("hosts") or [])
    if mode == "public":
        return normalize_domain(host) == host
    return False


def intersect_domains(*policies: dict | None) -> dict:
    """Intersect domain policies: the result is never wider than any input."""
    present = [policy for policy in policies if policy]
    if not present:
        return dict(PUBLIC_DOMAIN_POLICY)
    if any(policy.get("mode") == "allowlist" for policy in present):
        allowlists = [set(policy.get("hosts") or []) for policy in present if policy.get("mode") == "allowlist"]
        hosts: list[str] = []
        first = allowlists[0]
        for host in sorted(first):
            if all(host in other for other in allowlists[1:]):
                hosts.append(host)
        return {"mode": "allowlist", "hosts": hosts}
    return dict(PUBLIC_DOMAIN_POLICY)


def stricter_egress(*modes: object) -> str:
    ranked = [_EGRESS_RANK[mode] for mode in modes if mode in _EGRESS_RANK]
    return EGRESS_MODES[min(ranked)] if ranked else "deny"


def _min_limit(values: list[Any], default: int) -> int:
    numbers = [value for value in values if isinstance(value, int) and not isinstance(value, bool) and value > 0]
    return min(numbers) if numbers else default


def _grant(grants: dict, capability: str) -> dict:
    value = grants.get(capability) if isinstance(grants, dict) else None
    return value if isinstance(value, dict) else {}


def assistant_grants(assistant: dict | None) -> dict:
    """Return the assistant's v2 grants, or a legacy search/fetch mapping.

    An empty/missing ``capabilities`` object means the assistant predates v2.
    The old boolean then maps only to search/fetch; OCR and image search stay
    off.  Once any v2 object is saved it wins, so an old client cannot widen
    the fine-grained grants by writing the boolean again.
    """
    if not assistant:
        return {}
    raw = assistant.get("capabilities")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    if isinstance(raw, dict) and raw:
        return raw
    enabled = bool(assistant.get("web_search_enabled"))
    return {"search": {"enabled": enabled}, "fetch": {"enabled": enabled}}


def effective_capability(
    capability: str,
    *,
    deployment_allowed: bool,
    user_enabled: bool,
    assistant_grant: dict | None,
    snapshot_grant: dict | None,
    global_route: dict,
) -> dict:
    """Compute the effective capability from every narrowed grant.

    ``enabled`` is the logical AND.  Domains and egress take the stricter
    value; numeric limits and attempt budgets take the smaller value.
    """
    route = global_route or {}
    grants = [assistant_grants(assistant_grant)]
    if snapshot_grant is not None:
        grants.append(assistant_grants(snapshot_grant))
    scoped = [_grant(grants[0], capability)]
    if len(grants) > 1:
        scoped.append(_grant(grants[1], capability))
    enabled = bool(deployment_allowed and user_enabled and route.get("enabled"))
    for item in scoped:
        # A v2 capability object that omits this capability does not grant it.
        enabled = enabled and bool(item.get("enabled", False))
    domains = intersect_domains(route.get("domains"), *[item.get("domains") for item in scoped])
    global_egress = route.get("egress") or {}
    egress = {
        key: stricter_egress(global_egress.get(key), *[(item.get("egress") or {}).get(key) for item in scoped])
        for key in ("query", "url", "upload")
    }
    global_limits = route.get("limits") or {}
    limit_keys = ("calls_per_run", "calls_per_day", "max_upload_bytes")
    limits = {
        key: _min_limit([global_limits.get(key), *[(item.get("limits") or {}).get(key) for item in scoped]],
                        int(global_limits.get(key) or 0))
        for key in limit_keys
    }
    attempts = _min_limit([route.get("max_attempts"), *[item.get("max_attempts") for item in scoped]],
                          int(route.get("max_attempts") or 1))
    if capability == "ocr":
        # The official OCR bills and continues remotely after a local cancel.
        # Zero automatic retry and no fallback avoid duplicate upload/charges.
        attempts = 1
    return {
        "capability": capability,
        "enabled": enabled,
        "domains": domains,
        "egress": egress,
        "limits": limits,
        "max_attempts": max(1, attempts),
        "preferred": route.get("preferred"),
        "fallbacks": list(route.get("fallbacks") or []),
        "timeout_seconds": int(route.get("timeout_seconds") or 60),
        "settings_revision": int(route.get("revision") or 0),
    }


def required_egress(capability: str, arguments: dict) -> str:
    """Which egress consent this call needs (query, url, upload or none)."""
    if capability == "search":
        return "query"
    if capability == "fetch":
        return "url"
    if capability == "ocr":
        return "upload"
    if capability == "image_search":
        return "upload" if arguments.get("mode") == "reverse_image" else "query"
    return "upload"


class CapabilityPolicy:
    """Thin adapter used by ``Service.authorize_tool`` and evidence reads."""

    def __init__(self, service):
        self.service = service

    def _global_route(self, capability: str) -> dict:
        from .capability_settings import CapabilitySettings
        return CapabilitySettings(self.service).effective_route(capability)

    def current(self, *, conversation: dict, user: dict, assistant: dict, snapshot: dict | None) -> dict[str, dict]:
        deployment_allowed = not getattr(self.service.policy, "is_trial", False)
        result = {}
        for capability in CAPABILITIES:
            result[capability] = effective_capability(
                capability,
                deployment_allowed=deployment_allowed,
                user_enabled=bool(user.get("enabled")),
                assistant_grant=assistant,
                snapshot_grant=snapshot,
                global_route=self._global_route(capability),
            )
        return result

    def authorize(self, conversation_id: str, tool_name: str, arguments: dict, *, conversation: dict,
                  user: dict, assistant: dict, snapshot: dict | None) -> bool:
        capability = capability_for_tool(tool_name)
        if capability is None:
            raise AppError(403, "TOOL_DENIED", "此工具未获授权")
        effective = self.current(conversation=conversation, user=user, assistant=assistant,
                                 snapshot=snapshot)[capability]
        if not effective["enabled"]:
            raise AppError(403, "CAPABILITY_DISABLED", "当前能力未对本次任务或助手开放")
        target = arguments.get("url") if capability == "fetch" else None
        if target is not None and not domain_allows(effective["domains"], target):
            raise AppError(403, "CAPABILITY_DOMAIN_DENIED", "目标地址不在当前能力允许的域名范围内")
        mode = effective["egress"][required_egress(capability, arguments)]
        if mode == "deny":
            raise AppError(403, "CAPABILITY_EGRESS_DENIED", "当前能力的数据外发已被管理员禁止")
        return mode != "approval"


def authorize_source_access(service, source_row, user) -> None:
    """Synchronous authorization for a historical evidence source.

    The original run does not need to be active.  The current user, assistant,
    capability route, domain policy and resource ACL still apply.  Quotas are
    not consumed and nothing is written.
    """
    user = service.require("users", user["id"])
    if not user.get("enabled"):
        raise AppError(404, "SOURCE_UNAVAILABLE", "该账号已无权访问来源")
    run = service.require("runs", source_row["run_id"])
    conversation = service.require("conversations", run["conversation_id"], user)
    assistant = service.usable_assistant(conversation["assistant_id"], user)
    locator = source_row.get("locator") or {}
    if isinstance(locator, str):
        try:
            locator = json.loads(locator)
        except ValueError:
            locator = {}
    capability = capability_for_locator(locator, source_row.get("tool_name"))
    if capability is None:
        # Non-capability sources (knowledge/document/connection) keep their
        # dedicated ACL checks in EvidenceService.
        if source_row.get("kind") == "web":
            raise AppError(404, "SOURCE_UNAVAILABLE", "来源缺少可核对的能力标记")
        return
    snapshot = None
    active_id = conversation.get("active_run_id")
    if active_id:
        try:
            active = service.require("runs", active_id)
            snapshot = json.loads(active.get("capability_snapshot") or "null")
        except AppError:
            snapshot = None
    effective = CapabilityPolicy(service).current(
        conversation=conversation, user=user, assistant=assistant, snapshot=snapshot)[capability]
    if not effective["enabled"]:
        raise AppError(404, "SOURCE_UNAVAILABLE", "该能力已停用，来源不可再访问")
    original = source_row.get("original_url")
    if original and not domain_allows(effective["domains"], original):
        raise AppError(404, "SOURCE_UNAVAILABLE", "该来源域名已不在允许范围内")


def classify_error(exc: BaseException) -> str:
    """Classify an exception for retry/fallback decisions.

    Only clearly transient provider failures are retryable.  Permission,
    input, safety, domain, approval, cancellation and credential errors must
    never turn into a fallback request.
    """
    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
        return "cancel"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "transient"
    if isinstance(exc, AppError):
        if exc.status in {401, 403}:
            return "permission"
        if exc.status in {408, 429} or exc.status >= 500:
            return "transient"
        return "input"
    return "unknown"


def retryable(exc: BaseException) -> bool:
    return classify_error(exc) == "transient"
