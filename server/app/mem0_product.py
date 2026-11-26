"""Opt-in product API for the derived Mem0 semantic index.

This is the first product entry point for :class:`app.mem0_host.Mem0Host`.
It is disabled unless an operator sets ``AGENT_MEM0_PRODUCT_ENABLED=true``
*and* explicitly injects a scoped factory through
:meth:`Mem0ProductService.configure`. Merely enabling the route does not import
the Mem0 SDK, choose an embedding provider or authorize data egress.

Design boundaries (unchanged from the adapter/host work):

* ``NativeMemory`` and its published snapshot remain the only authority. Every
  lookup and sync revalidates the owner, published record, active run binding,
  revision, digest and native epoch through ``NativeMemorySourceReader``.
* Search is read-only: at most one provider query, zero index writes and no
  scope provisioning. A missing or retired scope falls back to authoritative
  memory instead of creating SDK storage.
* Sync and logical retirement are explicit owner maintenance actions. They are
  never triggered by a model or by a read. Retirement is a durable logical
  receipt; it neither deletes bytes nor claims physical deletion.
* A query/index/provider failure is reported as ``fallback`` so callers can use
  authoritative memory. It is never treated as an empty memory.
* All SDK locking, lease uncertainty, revocation and close semantics stay in
  ``Mem0Host``/``ScopeStore``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .mem0_host import Mem0Host
from .mem0_index import (
    INDEX_AGENT_ID,
    Mem0IndexConfig,
    Mem0IndexDisabled,
    Mem0IndexError,
    Mem0IndexQueryInvalid,
    Mem0IndexScopeInvalid,
    Mem0IndexSourceChanged,
    Mem0IndexSourceInvalid,
    VerifiedHit,
)
from .mem0_retirement import retire_committed_clear
from .security import AppError

DISABLED_MESSAGE = "Mem0 语义记忆索引未启用或未配置显式后端"


def _config_from_settings(settings) -> Mem0IndexConfig:
    return Mem0IndexConfig(
        enabled=bool(getattr(settings, "mem0_product_enabled", False)),
        agent_id=getattr(settings, "mem0_agent_id", INDEX_AGENT_ID),
        top_k=getattr(settings, "mem0_top_k", 5),
        threshold=getattr(settings, "mem0_threshold", 0.0),
    )


def _app_error(exc: Mem0IndexError) -> AppError:
    """Map a stable adapter error to a stable HTTP error without leaking detail."""
    if isinstance(exc, Mem0IndexDisabled):
        return AppError(403, "MEM0_DISABLED", DISABLED_MESSAGE)
    if isinstance(exc, Mem0IndexQueryInvalid):
        return AppError(422, "MEM0_QUERY_INVALID", "检索词为空、过长或包含无效字符")
    if isinstance(exc, Mem0IndexSourceChanged):
        return AppError(409, "MEM0_SOURCE_CHANGED", "记忆或任务版本已变化，请刷新后重试")
    if isinstance(exc, (Mem0IndexScopeInvalid, Mem0IndexSourceInvalid)):
        return AppError(403, "MEM0_SCOPE_DENIED", "当前记忆不允许使用派生语义索引")
    # Includes Mem0IndexUnavailable and Mem0IndexSourceUnavailable.
    return AppError(503, "MEM0_UNAVAILABLE", "Mem0 语义记忆索引暂时不可用，请使用原生记忆")


def _hit_view(hit: VerifiedHit) -> dict[str, Any]:
    """Whole verified text plus the authoritative source coordinates."""
    return {
        "source_id": hit.source_id,
        "source_path": hit.path,
        "content_sha256": hit.content_sha256,
        "score": hit.score,
        "revision": hit.revision,
        "native_epoch": hit.native_epoch,
        "text": hit.text,
    }


class Mem0ProductService:
    """Owner-scoped product facade over ``Mem0Host``; disabled until injected.

    ``configure`` is the only place a backend may enter. Production code never
    calls it, so the default process has no provider, no SDK handle and no
    external data path. Tests inject a synthetic backend/factory.
    """

    def __init__(self, service, *, backend=None, scoped_factory=None,
                 config: Mem0IndexConfig | None = None,
                 max_context_bytes: int | None = None):
        self.service = service
        if config is not None and config.enabled != service.settings.mem0_product_enabled:
            raise ValueError("product configuration cannot override deployment switch")
        self.config = config or _config_from_settings(service.settings)
        if max_context_bytes is None:
            max_context_bytes = getattr(service.settings, "mem0_max_context_bytes", 16384)
        self.max_context_bytes = max_context_bytes
        self.host: Mem0Host | None = None
        if backend is not None or scoped_factory is not None:
            self.configure(backend=backend, scoped_factory=scoped_factory,
                           config=config, max_context_bytes=max_context_bytes)

    def configure(self, *, backend=None, scoped_factory=None,
                  config: Mem0IndexConfig | None = None,
                  max_context_bytes: int | None = None) -> "Mem0ProductService":
        """Explicitly inject the only backend this process will ever use.

        A host is built only when the deployment switch is on and an isolated
        scoped factory is supplied. Reconfiguration is not supported.
        """
        if backend is not None:
            raise ValueError("product requires an isolated scoped factory")
        if self.host is not None:
            raise ValueError("product factory is configured once at startup")
        if config is not None and config.enabled != self.service.settings.mem0_product_enabled:
            raise ValueError("product configuration cannot override deployment switch")
        if max_context_bytes is not None:
            self.max_context_bytes = max_context_bytes
        if config is not None:
            self.config = config
        if self.config.enabled and self.service.settings.mem0_product_enabled and scoped_factory is not None:
            self.host = Mem0Host(
                self.service, self.config,
                backend=backend, scoped_factory=scoped_factory,
                max_context_bytes=self.max_context_bytes,
            )
        else:
            self.host = None
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.host is not None)

    def status(self) -> dict[str, Any]:
        """Safe, credential-free capability view. Never touches a backend."""
        mode = None
        if self.host is not None:
            mode = "scoped" if self.host.scope_store is not None else "single"
        return {
            "enabled": bool(self.config.enabled),
            "configured": self.host is not None,
            "mode": mode,
            "agent_id": self.config.agent_id,
            "top_k": self.config.top_k,
            "threshold": self.config.threshold,
            "max_context_bytes": self.max_context_bytes,
            "read_only_lookup": True,
            "physical_deletion": False,
        }

    def _require_host(self) -> Mem0Host:
        if not self.config.enabled:
            raise AppError(403, "MEM0_DISABLED", DISABLED_MESSAGE)
        if self.host is None:
            raise AppError(403, "MEM0_DISABLED", DISABLED_MESSAGE)
        return self.host

    def _authorize(self, user, conversation_id: str) -> dict:
        # Ownership + existence through the authoritative store. The source
        # reader revalidates the same facts again under its own transaction.
        return self.service.require("conversations", conversation_id, user)

    def _validate_query(self, query: Any) -> str:
        if type(query) is not str or not query.strip() or "\x00" in query:
            raise AppError(422, "MEM0_QUERY_INVALID", "检索词为空或包含无效字符")
        if len(query) > self.config.max_query_chars:
            raise AppError(422, "MEM0_QUERY_INVALID", "检索词过长")
        return query

    def lookup(self, user, conversation_id: str, query: str,
               *, run_id: str | None = None) -> dict[str, Any]:
        """Read-only semantic lookup with authoritative fallback.

        ``run_id`` is optional and, when present, must be an active run of this
        conversation bound to the current published memory; the reader enforces
        that. A ``fallback`` result means the caller must use authoritative
        memory, not that the index is empty.
        """
        host = self._require_host()
        self._authorize(user, conversation_id)
        text = self._validate_query(query)
        if run_id is not None:
            self.service.require("runs", run_id, user)
        result = host.lookup(user["id"], conversation_id, text, run_id=run_id)
        hits = [_hit_view(hit) for hit in result.hits]
        return {
            "status": result.status,
            "reason": result.reason,
            "run_id": run_id,
            "hits": hits,
            "context_bytes": sum(len(hit.text.encode("utf-8")) for hit in result.hits),
            "fallback": result.status != "verified",
            "authoritative_source": "native_memory",
        }

    def sync(self, user, conversation_id: str) -> dict[str, Any]:
        """Explicit owner maintenance: mirror the committed snapshot into Mem0."""
        host = self._require_host()
        self._authorize(user, conversation_id)
        try:
            result = host.sync(user["id"], conversation_id)
        except Mem0IndexError as exc:
            raise _app_error(exc) from None
        return {**result.as_dict(), "authoritative_source": "native_memory"}

    def retire(self, user, conversation_id: str, operation_key: str) -> dict[str, Any]:
        """Explicit logical retirement after a committed native clear.

        Requires the independent scoped-store identity. It writes only a
        durable metadata receipt; derived bodies are not read or deleted and
        ``cleanup_status`` stays ``not_performed``.
        """
        host = self._require_host()
        self._authorize(user, conversation_id)
        if host.scope_store is None:
            raise AppError(409, "MEM0_SCOPE_REQUIRED", "逻辑退役仅适用于独立作用域存储")
        try:
            receipt = retire_committed_clear(
                self.service, host.scope_store, user["id"], conversation_id, operation_key,
            )
        except Mem0IndexError as exc:
            raise _app_error(exc) from None
        return {
            "state": receipt.state,
            "cleanup_status": receipt.cleanup_status,
            "physical_deletion": False,
            "retirement_proof": receipt.proof,
        }


class SemanticQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=4000)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)


class RetireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=256)


def create_mem0_router(service_dependency, authenticated_dependency, *, enabled: bool = False):
    """Mount stable route shapes; disabled deployments fail closed with 403."""
    router = APIRouter(prefix="/api/v1", tags=["Native memory semantic index"])
    route_specs = (
        ("POST", "/conversations/{conversation_id}/memory/semantic/query"),
        ("GET", "/conversations/{conversation_id}/memory/semantic/status"),
        ("POST", "/conversations/{conversation_id}/memory/semantic/sync"),
        ("POST", "/conversations/{conversation_id}/memory/semantic/retire"),
    )

    if not enabled:
        def disabled(request: Request, conversation_id: str):
            raise AppError(403, "MEM0_DISABLED", DISABLED_MESSAGE)

        for method, path in route_specs:
            router.add_api_route(
                path, disabled, methods=[method],
                dependencies=[Depends(authenticated_dependency)], response_model=None,
            )
        return router

    @router.get("/conversations/{conversation_id}/memory/semantic/status")
    def semantic_status(conversation_id: str,
                        service=Depends(service_dependency),
                        user=Depends(authenticated_dependency)):
        service.require("conversations", conversation_id, user)
        return service.mem0_product.status()

    @router.post("/conversations/{conversation_id}/memory/semantic/query")
    def semantic_lookup(conversation_id: str, body: SemanticQuery,
                        service=Depends(service_dependency),
                        user=Depends(authenticated_dependency)):
        return service.mem0_product.lookup(user, conversation_id, body.q, run_id=body.run_id)

    @router.post("/conversations/{conversation_id}/memory/semantic/sync")
    def semantic_sync(conversation_id: str,
                      service=Depends(service_dependency),
                      user=Depends(authenticated_dependency)):
        return service.mem0_product.sync(user, conversation_id)

    @router.post("/conversations/{conversation_id}/memory/semantic/retire")
    def semantic_retire(conversation_id: str, body: RetireRequest,
                        service=Depends(service_dependency),
                        user=Depends(authenticated_dependency)):
        return service.mem0_product.retire(user, conversation_id, body.operation_id)

    return router


__all__ = [
    "DISABLED_MESSAGE",
    "Mem0ProductService",
    "create_mem0_router",
]
