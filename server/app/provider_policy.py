"""Provider routing policy for model execution.

Production model traffic is allowed only through Tencent TokenHub.  The
reserved host names used by the test suite and loopback adapters are retained
as explicit synthetic/local routes so deterministic tests never make a real
network call.  They are not treated as production-ready external providers.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


TOKENHUB_BASE_URL = "https://tokenhub.tencentmaas.com"
TOKENHUB_HOST = "tokenhub.tencentmaas.com"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_RESERVED_SUFFIXES = (".test", ".invalid", ".example")


@dataclass(frozen=True)
class ProviderRoute:
    """A non-secret classification of a provider base URL."""

    kind: str
    host: str | None
    reason: str

    @property
    def allowed(self) -> bool:
        return self.kind in {"tokenhub", "local_proxy", "synthetic"}

    @property
    def production_ready(self) -> bool:
        return self.kind == "tokenhub"


def classify_provider_url(value: str | None) -> ProviderRoute:
    """Classify a base URL without resolving DNS or contacting the network.

    ``synthetic`` is intentionally limited to RFC-reserved test domains.  A
    real external hostname other than TokenHub is rejected even when it looks
    like a compatible Anthropic endpoint.
    """

    raw = str(value or "").strip()
    if not raw:
        return ProviderRoute("missing", None, "base_url_missing")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ProviderRoute("rejected", None, "base_url_invalid")
    host = (parsed.hostname or "").lower().rstrip(".") or None
    try:
        port = parsed.port
    except ValueError:
        return ProviderRoute("rejected", host, "base_url_invalid")
    if (parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        return ProviderRoute("rejected", host, "base_url_invalid")
    if host == TOKENHUB_HOST and parsed.scheme == "https" and port in {None, 443}:
        return ProviderRoute("tokenhub", host, "tokenhub_authorized_route")
    if host in _LOCAL_HOSTS:
        return ProviderRoute("local_proxy", host, "loopback_adapter_requires_tokenhub_upstream")
    if any(host.endswith(suffix) for suffix in _RESERVED_SUFFIXES):
        return ProviderRoute("synthetic", host, "reserved_test_route")
    return ProviderRoute("rejected", host, "non_tokenhub_external_route")


def provider_route(provider: dict[str, str] | None) -> ProviderRoute:
    """Classify provider settings while keeping credentials out of evidence."""

    return classify_provider_url((provider or {}).get("ANTHROPIC_BASE_URL"))


def public_route_metadata(provider: dict[str, str] | None) -> dict[str, str | bool | None]:
    """Return safe route evidence suitable for APIs and audit records."""

    route = provider_route(provider)
    return {
        "provider_route": route.kind,
        "provider_route_reason": route.reason,
        "tokenhub_route": route.production_ready,
    }
