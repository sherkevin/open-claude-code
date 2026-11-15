"""Single deployment-level capability policy (normal vs controlled trial).

``normal`` keeps every existing behaviour.  ``trial`` is an explicit, fail-closed
allowlist: unknown modes, unknown capability names, unlisted routes, tools and
retrieval modes are refused with ``403 FEATURE_DISABLED`` before any business
handler, run, approval, model call or database write can happen.

The policy is intentionally a pure value object so the same instance rules can
be enforced by the HTTP route guard, :meth:`Service.authorize_tool`, tool
registration and the retrieval layer.  Hiding UI controls is never the boundary.
"""
from __future__ import annotations

from types import MappingProxyType

from .security import AppError

DEPLOYMENT_MODES = frozenset({"normal", "trial"})

FEATURE_DISABLED = "FEATURE_DISABLED"


def valid_deployment_mode(mode: object) -> str:
    """Return an explicitly configured deployment mode without normalization."""
    if type(mode) is not str or mode not in DEPLOYMENT_MODES:
        raise ValueError("AGENT_DEPLOYMENT_MODE must be normal or trial")
    return mode


# Every capability a trial deployment may explicitly enable.  Unknown names are
# a startup failure; there is no prefix or wildcard matching.
CAPABILITIES = frozenset({
    "health",
    "auth",
    "config",
    "models.read",
    # Tab completion is low-risk but still an explicit capability.  Exposing
    # its route does not authorize a provider call; the model route remains
    # fail-closed when the approved 1017 target is unavailable.
    "models.complete",
    "assistants.read",
    "conversations.read",
    "conversations.write",
    "messages.read",
    "messages.send",
    "runs.read",
    "runs.cancel",
    "knowledge.read",
    "knowledge.search",
    "sources.read",
    "agent.knowledge_tools",
})

DEFAULT_TRIAL_CAPABILITIES = tuple(sorted(CAPABILITIES))

# The only agent tools a trial deployment may execute.  Registration and
# execution both consult this set; history snapshots and forged tool names
# cannot widen it.
TRIAL_TOOLS = frozenset({
    "mcp__workspace__knowledge_search",
    "mcp__workspace__knowledge_read",
})

KNOWN_RETRIEVAL_MODES = ("hybrid", "bm25", "vector", "hipporag")
TRIAL_RETRIEVAL_MODES = ("bm25", "hybrid")

# Exact (HTTP method, matched route template) -> capability.  The template is
# the one FastAPI reports after router prefixes are applied; the guard never
# matches on a raw path prefix.
_TRIAL_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/health/live"): "health",
    ("GET", "/api/v1/health/ready"): "health",
    ("POST", "/api/v1/auth/login"): "auth",
    ("POST", "/api/v1/auth/register"): "auth",
    ("POST", "/api/v1/auth/logout"): "auth",
    ("GET", "/api/v1/auth/me"): "auth",
    ("GET", "/api/v1/config"): "config",
    ("GET", "/api/v1/models"): "models.read",
    ("POST", "/api/v1/models/complete"): "models.complete",
    ("GET", "/api/v1/assistants"): "assistants.read",
    ("GET", "/api/v1/assistants/{item_id}"): "assistants.read",
    ("GET", "/api/v1/conversations"): "conversations.read",
    ("GET", "/api/v1/conversations/overview"): "conversations.read",
    ("POST", "/api/v1/conversations"): "conversations.write",
    ("GET", "/api/v1/conversations/{item_id}"): "conversations.read",
    ("GET", "/api/v1/conversations/{item_id}/trajectory"): "messages.read",
    ("PATCH", "/api/v1/conversations/{item_id}"): "conversations.write",
    ("GET", "/api/v1/conversations/{item_id}/messages"): "messages.read",
    ("POST", "/api/v1/conversations/{item_id}/messages"): "messages.send",
    ("GET", "/api/v1/conversations/{item_id}/queue"): "messages.read",
    ("POST", "/api/v1/conversations/{item_id}/queue"): "messages.send",
    ("PATCH", "/api/v1/conversations/{item_id}/queue/{queue_id}"): "messages.send",
    ("POST", "/api/v1/conversations/{item_id}/queue/{queue_id}/cancel"): "messages.send",
    ("POST", "/api/v1/conversations/{item_id}/queue/{queue_id}/dispatch"): "messages.send",
    ("GET", "/api/v1/runs/{item_id}"): "runs.read",
    ("GET", "/api/v1/runs/{item_id}/trace"): "runs.read",
    ("GET", "/api/v1/runs/{item_id}/events"): "runs.read",
    ("POST", "/api/v1/runs/{item_id}/cancel"): "runs.cancel",
    # Public example knowledge bases: browse, retrieve, evidence card, original
    # document and image viewing only.
    ("GET", "/api/v1/knowledge-bases"): "knowledge.read",
    ("GET", "/api/v1/knowledge-bases/{kb_id}"): "knowledge.read",
    ("GET", "/api/v1/knowledge-bases/{kb_id}/documents"): "knowledge.read",
    ("POST", "/api/v1/knowledge-bases/{kb_id}/search"): "knowledge.search",
    ("GET", "/api/v1/knowledge-documents/{document_id}/versions"): "knowledge.read",
    ("GET", "/api/v1/knowledge-documents/{document_id}/content"): "knowledge.read",
    ("GET", "/api/v1/knowledge-documents/{document_id}/image"): "knowledge.read",
    ("GET", "/api/v1/knowledge-documents/{document_id}/regions"): "knowledge.read",
    ("GET", "/api/v1/knowledge-documents/{document_id}/thumbnail"): "knowledge.read",
    ("GET", "/api/v1/knowledge-documents/{document_id}/regions/{region_id}/thumbnail"): "knowledge.read",
    ("GET", "/api/v1/knowledge-documents/{document_id}/chunks"): "knowledge.read",
    ("GET", "/api/v1/runs/{run_id}/sources"): "sources.read",
    ("GET", "/api/v1/sources/{source_id}"): "sources.read",
}
TRIAL_ROUTES = MappingProxyType(_TRIAL_ROUTES)

# Composite projections must preserve every existing data boundary. Metadata
# access alone cannot reveal message bodies or run/tool receipts through a new
# route. Enforce these exact requirements in the middleware before handlers.
_TRIAL_ROUTE_REQUIREMENTS = MappingProxyType({
    ("GET", "/api/v1/conversations/overview"): frozenset({"runs.read"}),
    ("GET", "/api/v1/conversations/{item_id}/trajectory"): frozenset({"conversations.read", "runs.read"}),
})


def _disabled(message: str = "此功能未在当前工作空间开放") -> AppError:
    return AppError(403, FEATURE_DISABLED, message)


class DeploymentPolicy:
    """Immutable deployment capability policy.

    ``capabilities=None`` means the documented trial default set; an explicit
    (possibly empty) iterable selects a subset.  In ``normal`` mode every known
    capability is active and unknown routes keep their previous behaviour.
    """

    __slots__ = ("_mode", "_capabilities")

    def __init__(self, mode: str = "normal", capabilities=None):
        checked_mode = valid_deployment_mode(mode)
        if checked_mode == "normal":
            selected = CAPABILITIES
        else:
            selected = DEFAULT_TRIAL_CAPABILITIES if capabilities is None else tuple(capabilities)
            if type(selected) is not tuple or any(not isinstance(item, str) for item in selected):
                raise ValueError("trial capabilities must be a tuple of capability names")
            unknown = sorted(set(selected) - CAPABILITIES)
            if unknown:
                raise ValueError("unknown trial capability: " + ", ".join(unknown))
            selected = frozenset(selected)
        object.__setattr__(self, "_mode", checked_mode)
        object.__setattr__(self, "_capabilities", frozenset(selected))

    def __setattr__(self, name, value):
        raise AttributeError("DeploymentPolicy is read-only")

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    @property
    def is_trial(self) -> bool:
        return self._mode == "trial"

    def allows(self, capability: str) -> bool:
        if not self.is_trial:
            return True
        return capability in self._capabilities

    def require(self, capability: str, message: str = "此功能未在当前工作空间开放") -> None:
        if not self.allows(capability):
            raise _disabled(message)

    def route_allowed(self, method: str, template: str | None) -> bool:
        if not self.is_trial:
            return True
        route = (method.upper(), template)
        capability = TRIAL_ROUTES.get(route)
        return bool(capability and capability in self._capabilities
                    and _TRIAL_ROUTE_REQUIREMENTS.get(route, frozenset()) <= self._capabilities)

    def require_route(self, method: str, template: str | None) -> None:
        if not self.route_allowed(method, template):
            raise _disabled()

    def require_tool_name(self, tool_name: object) -> str:
        """Validate the shared tool-name boundary before any registry lookup."""
        if type(tool_name) is not str or not tool_name.strip():
            raise _disabled("工具名无效")
        return tool_name

    def tool_allowed(self, tool_name: object) -> bool:
        if type(tool_name) is not str or not tool_name.strip():
            return False
        if not self.is_trial:
            return True
        return tool_name in TRIAL_TOOLS and "agent.knowledge_tools" in self._capabilities

    def require_tool(self, tool_name: object) -> None:
        tool_name = self.require_tool_name(tool_name)
        if not self.tool_allowed(tool_name):
            raise _disabled("此工具未在当前工作空间开放")

    def retrieval_modes(self) -> tuple[str, ...]:
        return TRIAL_RETRIEVAL_MODES if self.is_trial else KNOWN_RETRIEVAL_MODES

    def retrieval_allowed(self, mode: str) -> bool:
        if not self.is_trial:
            return mode in KNOWN_RETRIEVAL_MODES
        return mode in TRIAL_RETRIEVAL_MODES

    def require_retrieval_mode(self, mode: str) -> None:
        if not self.retrieval_allowed(mode):
            raise _disabled("此检索方式未在当前工作空间开放")

    def public_view(self) -> dict:
        """Safe, deterministic capability advertisement (no credentials)."""
        return {
            "deployment_mode": self.mode,
            "capabilities": sorted(self._capabilities),
            "allowed_retrieval_modes": sorted(self.retrieval_modes()),
        }
