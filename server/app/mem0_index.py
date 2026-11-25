"""Optional Mem0 index derived from already-approved, committed memory.

NativeMemory and its SQLite snapshot pointer remain authoritative. This module
only mirrors committed Markdown text into Mem0 with ``infer=False``. It never
creates facts, approvals, revisions or epochs, and it never accepts a user,
conversation, native epoch, revision, digest or provider filter from a model.
The model-facing input is a query string; all scope is derived from a
``TrustedScope`` that the host builds from a verified memory binding. The host
must only call this adapter for an active snapshot it has approved and
committed. The native source reader additionally requires a published record,
an empty initial migration, and current host authorization. It never approves
legacy imports or initializes memory on behalf of an index request.

Importing this module does not import or install Mem0. The SDK is loaded only
by :func:`load_mem0_backend`, which requires an explicit self-hosted config,
pins mem0ai 2.2.1, and refuses to continue unless telemetry is disabled.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

SCHEMA = 1
INDEX_AGENT_ID = "kaiwu-memory-index-v1"
DEFAULT_TOP_K = 5
MAX_TOP_K = 50
DEFAULT_THRESHOLD = 0.0
MAX_DOCUMENTS = 100
# Mem0 2.2.1 get_all is bounded, not a paginated API. Allow one
# complete generation and one stale generation, plus a truncation sentinel.
MAX_ENUMERATION_ROWS = 2 * MAX_DOCUMENTS + 1
MAX_DOCUMENT_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 4000
MAX_METADATA_BYTES = 4096
MEM0_SDK_VERSION = "2.2.1"

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_KIND = "approved_memory_text"


class Mem0IndexError(RuntimeError):
    """Base class; all adapter failures carry a stable code."""

    code = "MEM0_INDEX_ERROR"


class Mem0IndexDisabled(Mem0IndexError):
    code = "MEM0_INDEX_DISABLED"


class Mem0IndexUnavailable(Mem0IndexError):
    code = "MEM0_INDEX_UNAVAILABLE"


class Mem0IndexScopeInvalid(Mem0IndexError):
    code = "MEM0_INDEX_SCOPE_INVALID"


class Mem0IndexSourceInvalid(Mem0IndexError):
    code = "MEM0_INDEX_SOURCE_INVALID"


class Mem0IndexSourceChanged(Mem0IndexError):
    code = "MEM0_INDEX_SOURCE_CHANGED"


class Mem0IndexSourceUnavailable(Mem0IndexError):
    code = "MEM0_INDEX_SOURCE_UNAVAILABLE"


class Mem0IndexQueryInvalid(Mem0IndexError):
    code = "MEM0_INDEX_QUERY_INVALID"


class _Reject(Exception):
    """A candidate is not eligible for model context; drop only that row."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_id(value: Any) -> bool:
    return type(value) is str and _ID.fullmatch(value) is not None


def _valid_digest(value: Any) -> bool:
    return type(value) is str and _DIGEST.fullmatch(value) is not None


def _document_digest(documents: tuple["SourceDocument", ...]) -> str:
    return hashlib.sha256(canonical([
        {"source_id": document.source_id, "path": document.path,
         "sha256": document.content_sha256, "size": document.size}
        for document in documents
    ])).hexdigest()


@dataclass(frozen=True)
class TrustedScope:
    """Host-derived identity and version; never constructed from model input."""

    user_id: str
    conversation_id: str
    native_epoch: int
    revision: int
    digest: str
    snapshot_id: str | None = None
    manifest_digest: str | None = None

    def __post_init__(self) -> None:
        if not _valid_id(self.user_id):
            raise Mem0IndexScopeInvalid("user_id 必须由宿主生成")
        if not _valid_id(self.conversation_id):
            raise Mem0IndexScopeInvalid("conversation_id 必须由宿主生成")
        for name, value in (("native_epoch", self.native_epoch), ("revision", self.revision)):
            if type(value) is not int or value < 0:
                raise Mem0IndexScopeInvalid(f"{name} 必须是非负整数")
        if not _valid_digest(self.digest):
            raise Mem0IndexScopeInvalid("digest 必须是宿主快照的 sha256")
        if self.snapshot_id is not None and not _valid_id(self.snapshot_id):
            raise Mem0IndexScopeInvalid("snapshot_id 无效")
        if self.manifest_digest is not None and not _valid_digest(self.manifest_digest):
            raise Mem0IndexScopeInvalid("manifest_digest 无效")

    @classmethod
    def from_binding(cls, binding: Mapping[str, Any]) -> "TrustedScope":
        """Build a scope from a binding returned by the host snapshot store.

        Callers must pass a binding that came from the authoritative store, not
        from a tool argument or model output.
        """
        if not isinstance(binding, Mapping):
            raise Mem0IndexScopeInvalid("记忆绑定必须由宿主快照生成")
        missing = [key for key in ("user_id", "conversation_id", "native_epoch", "revision", "digest")
                   if key not in binding]
        if missing:
            raise Mem0IndexScopeInvalid("记忆绑定不完整：" + ",".join(missing))
        return cls(
            user_id=binding["user_id"],
            conversation_id=binding["conversation_id"],
            native_epoch=binding["native_epoch"],
            revision=binding["revision"],
            digest=binding["digest"],
            snapshot_id=binding.get("snapshot_id"),
            manifest_digest=binding.get("manifest_digest"),
        )

    def as_binding(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "revision": self.revision,
            "digest": self.digest,
            "snapshot_id": self.snapshot_id,
            "manifest_digest": self.manifest_digest,
            "native_epoch": self.native_epoch,
        }

    @property
    def generation(self) -> str:
        """Index generation bound to the exact approved snapshot and epoch."""
        return hashlib.sha256(canonical({
            "schema": SCHEMA,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "native_epoch": self.native_epoch,
            "revision": self.revision,
            "digest": self.digest,
            "snapshot_id": self.snapshot_id,
            "manifest_digest": self.manifest_digest,
        })).hexdigest()


def _validate_source_path(raw: Any) -> None:
    if type(raw) is not str or not raw or len(raw) > 240:
        raise Mem0IndexSourceInvalid("记忆相对路径无效")
    if "\\" in raw or ":" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise Mem0IndexSourceInvalid("记忆相对路径无效")
    try:
        raw.encode("utf-8")
    except UnicodeError:
        raise Mem0IndexSourceInvalid("记忆相对路径不是 UTF-8") from None
    parts = raw.split("/")
    if len(parts) > 5 or any(not part or part.startswith(".") for part in parts):
        raise Mem0IndexSourceInvalid("记忆相对路径无效")
    if not parts[-1].lower().endswith(".md"):
        raise Mem0IndexSourceInvalid("派生索引只接受 Markdown 记忆")


@dataclass(frozen=True)
class SourceDocument:
    """One approved Markdown file body from the authoritative snapshot."""

    source_id: str
    path: str
    text: str
    content_sha256: str

    def __post_init__(self) -> None:
        _validate_source_path(self.path)
        if self.source_id != self.path:
            raise Mem0IndexSourceInvalid("source_id 必须与快照内相对路径一致")
        if type(self.text) is not str:
            raise Mem0IndexSourceInvalid("记忆正文必须是文本")
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeError:
            raise Mem0IndexSourceInvalid("记忆正文不是有效 UTF-8") from None
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise Mem0IndexSourceInvalid("单个记忆文件超过派生索引上限")
        if not _valid_digest(self.content_sha256) or hashlib.sha256(encoded).hexdigest() != self.content_sha256:
            raise Mem0IndexSourceInvalid("记忆正文摘要不匹配")

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))


@dataclass(frozen=True)
class SourceSnapshot:
    scope: TrustedScope
    documents: tuple[SourceDocument, ...]
    documents_digest: str

    @classmethod
    def build(cls, scope: TrustedScope, documents: Any) -> "SourceSnapshot":
        if type(scope) is not TrustedScope:
            raise Mem0IndexScopeInvalid("来源快照缺少宿主作用域")
        if documents is None:
            raise Mem0IndexSourceInvalid("来源快照缺少文档列表")
        try:
            ordered = tuple(sorted(tuple(documents), key=lambda document: document.source_id))
        except (TypeError, AttributeError) as exc:
            raise Mem0IndexSourceInvalid("来源文档格式无效") from exc
        return cls(scope=scope, documents=ordered, documents_digest=_document_digest(ordered))

    def __post_init__(self) -> None:
        if type(self.scope) is not TrustedScope:
            raise Mem0IndexScopeInvalid("来源快照缺少宿主作用域")
        if not isinstance(self.documents, tuple):
            raise Mem0IndexSourceInvalid("来源文档必须是不可变序列")
        for document in self.documents:
            if type(document) is not SourceDocument:
                raise Mem0IndexSourceInvalid("来源文档格式无效")
        try:
            ordered = tuple(sorted(self.documents, key=lambda document: document.source_id))
        except (TypeError, AttributeError) as exc:
            raise Mem0IndexSourceInvalid("来源文档格式无效") from exc
        if len(ordered) > MAX_DOCUMENTS:
            raise Mem0IndexSourceInvalid("来源文档数量超过派生索引上限")
        if sum(document.size for document in ordered) > MAX_TOTAL_BYTES:
            raise Mem0IndexSourceInvalid("来源文档总容量超过派生索引上限")
        identifiers = [document.source_id for document in ordered]
        if len(set(identifiers)) != len(identifiers):
            raise Mem0IndexSourceInvalid("来源文档 source_id 重复")
        object.__setattr__(self, "documents", ordered)
        if not _valid_digest(self.documents_digest) or self.documents_digest != _document_digest(ordered):
            raise Mem0IndexSourceInvalid("来源文档摘要不匹配")


class SourceReader(Protocol):
    def load(self, scope: TrustedScope) -> SourceSnapshot:
        """Read the current authoritative snapshot for a host-derived scope."""


class Mem0Backend(Protocol):
    """The exact subset of mem0.Memory used by this adapter."""

    def add(self, text: str, *, user_id: str, agent_id: str, run_id: str,
            metadata: Mapping[str, Any], infer: bool) -> Mapping[str, Any]: ...

    def search(self, query: str, *, filters: Mapping[str, Any], top_k: int,
               threshold: float) -> Mapping[str, Any]: ...

    def get_all(self, *, filters: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def delete(self, memory_id: str) -> Any: ...

    def delete_all(self, *, user_id: str, agent_id: str, run_id: str) -> Any: ...


@dataclass(frozen=True)
class Mem0IndexConfig:
    """Host configuration. Disabled by default; no provider defaults exist."""

    enabled: bool = False
    agent_id: str = INDEX_AGENT_ID
    top_k: int = DEFAULT_TOP_K
    threshold: float = DEFAULT_THRESHOLD
    max_documents: int = MAX_DOCUMENTS
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_query_chars: int = MAX_QUERY_CHARS

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        if not _valid_id(self.agent_id):
            raise ValueError("agent_id 无效")
        if type(self.top_k) is not int or not 1 <= self.top_k <= MAX_TOP_K:
            raise ValueError(f"top_k 必须在 1..{MAX_TOP_K}")
        if type(self.threshold) is bool or not isinstance(self.threshold, (int, float)) or not 0.0 <= float(self.threshold) <= 1.0:
            raise ValueError("threshold 必须在 0..1")
        object.__setattr__(self, "threshold", float(self.threshold))
        if type(self.max_documents) is not int or not 1 <= self.max_documents <= MAX_DOCUMENTS:
            raise ValueError(f"max_documents 必须在 1..{MAX_DOCUMENTS}")
        if type(self.max_total_bytes) is not int or not 1 <= self.max_total_bytes <= MAX_TOTAL_BYTES:
            raise ValueError(f"max_total_bytes 必须在 1..{MAX_TOTAL_BYTES}")
        if type(self.max_query_chars) is not int or not 1 <= self.max_query_chars <= MAX_QUERY_CHARS:
            raise ValueError(f"max_query_chars 必须在 1..{MAX_QUERY_CHARS}")


@dataclass(frozen=True)
class SyncResult:
    status: str
    generation: str
    documents: int
    skipped_empty: int
    added: int
    unchanged: int
    deleted: int
    rejected_remote: int
    cleanup_pending: int
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generation": self.generation,
            "documents": self.documents,
            "skipped_empty": self.skipped_empty,
            "added": self.added,
            "unchanged": self.unchanged,
            "deleted": self.deleted,
            "rejected_remote": self.rejected_remote,
            "cleanup_pending": self.cleanup_pending,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class VerifiedHit:
    source_id: str
    path: str
    text: str
    content_sha256: str
    score: float
    revision: int
    native_epoch: int


@dataclass(frozen=True)
class SearchResult:
    generation: str
    hits: tuple[VerifiedHit, ...]
    fetched: int
    rejected: int


@dataclass(frozen=True)
class PurgeResult:
    generation: str


class Mem0DerivedIndex:
    """Disabled-by-default derived index over approved memory text.

    Search and sync take a :class:`TrustedScope`; there is no public parameter
    for user, conversation, epoch, revision, digest or raw provider filters.
    Every search hit is rechecked against a fresh authoritative snapshot and
    the exact generation that wrote the index row. A stale, forged, duplicate
    or unreadable candidate is dropped; a backend failure raises so callers do
    not mistake it for an empty memory.
    """

    def __init__(self, config: Mem0IndexConfig, backend: Mem0Backend | None = None,
                 reader: SourceReader | None = None):
        if type(config) is not Mem0IndexConfig:
            raise TypeError("config 必须是 Mem0IndexConfig")
        if config.enabled and (backend is None or reader is None):
            raise ValueError("启用 Mem0 派生索引时必须注入 backend 和 reader")
        self.config = config
        self.backend = backend
        self.reader = reader
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise Mem0IndexDisabled("Mem0 派生索引未启用")

    @staticmethod
    def _checked_scope(scope: Any) -> TrustedScope:
        if type(scope) is not TrustedScope:
            raise Mem0IndexScopeInvalid("索引作用域必须由宿主从已提交记忆快照生成")
        return scope

    def _load(self, scope: TrustedScope) -> SourceSnapshot:
        if self.reader is None:
            raise Mem0IndexUnavailable("未注入权威记忆读取器")
        try:
            snapshot = self.reader.load(scope)
        except Mem0IndexError:
            raise
        except Exception as exc:
            raise Mem0IndexSourceUnavailable("权威记忆快照读取失败") from exc
        if type(snapshot) is not SourceSnapshot:
            raise Mem0IndexSourceUnavailable("权威记忆读取器返回了无效快照")
        if snapshot.scope != scope:
            raise Mem0IndexSourceChanged("权威记忆作用域已变化")
        if len(snapshot.documents) > self.config.max_documents:
            raise Mem0IndexSourceInvalid("来源文档数量超过索引配置上限")
        if sum(document.size for document in snapshot.documents) > self.config.max_total_bytes:
            raise Mem0IndexSourceInvalid("来源文档总容量超过索引配置上限")
        return snapshot

    def _filters(self, scope: TrustedScope) -> dict[str, str]:
        return {"user_id": scope.user_id, "agent_id": self.config.agent_id, "run_id": scope.conversation_id}

    def _metadata(self, scope: TrustedScope, document: SourceDocument) -> dict[str, Any]:
        metadata = {
            "kaiwu_schema": SCHEMA,
            "kaiwu_generation": scope.generation,
            "kaiwu_user_id": scope.user_id,
            "kaiwu_conversation_id": scope.conversation_id,
            "kaiwu_agent_id": self.config.agent_id,
            "kaiwu_run_id": scope.conversation_id,
            "kaiwu_native_epoch": scope.native_epoch,
            "kaiwu_revision": scope.revision,
            "kaiwu_digest": scope.digest,
            "kaiwu_source_id": document.source_id,
            "kaiwu_source_path": document.path,
            "kaiwu_content_sha256": document.content_sha256,
            "kaiwu_content_size": document.size,
            "kaiwu_kind": _KIND,
        }
        if len(canonical(metadata)) > MAX_METADATA_BYTES:
            raise Mem0IndexScopeInvalid("派生索引元数据超过上限")
        return metadata

    @staticmethod
    def _row_id(row: Any) -> str | None:
        if isinstance(row, Mapping):
            memory_id = row.get("id")
            if type(memory_id) is str and memory_id:
                return memory_id
        return None

    @staticmethod
    def _rows(value: Any, operation: str) -> list[Any]:
        if not isinstance(value, Mapping):
            raise Mem0IndexUnavailable(f"Mem0 {operation} 返回格式无效")
        rows = value.get("results")
        if not isinstance(rows, list):
            raise Mem0IndexUnavailable(f"Mem0 {operation} 返回格式无效")
        return rows

    def _row_owned(self, row: Any, scope: TrustedScope) -> bool:
        if not isinstance(row, Mapping):
            return False
        metadata = row.get("metadata")
        if not isinstance(metadata, Mapping):
            return False
        return (
            type(metadata.get("kaiwu_schema")) is int and metadata.get("kaiwu_schema") == SCHEMA
            and metadata.get("kaiwu_user_id") == scope.user_id
            and metadata.get("kaiwu_conversation_id") == scope.conversation_id
            and metadata.get("kaiwu_agent_id") == self.config.agent_id
            and metadata.get("kaiwu_run_id") == scope.conversation_id
        )

    def _meta_matches(self, metadata: Any, scope: TrustedScope, document: SourceDocument) -> bool:
        if not isinstance(metadata, Mapping):
            return False
        return (
            type(metadata.get("kaiwu_schema")) is int and metadata.get("kaiwu_schema") == SCHEMA
            and metadata.get("kaiwu_generation") == scope.generation
            and metadata.get("kaiwu_user_id") == scope.user_id
            and metadata.get("kaiwu_conversation_id") == scope.conversation_id
            and metadata.get("kaiwu_agent_id") == self.config.agent_id
            and metadata.get("kaiwu_run_id") == scope.conversation_id
            and type(metadata.get("kaiwu_native_epoch")) is int
            and metadata.get("kaiwu_native_epoch") == scope.native_epoch
            and type(metadata.get("kaiwu_revision")) is int
            and metadata.get("kaiwu_revision") == scope.revision
            and metadata.get("kaiwu_digest") == scope.digest
            and metadata.get("kaiwu_source_id") == document.source_id
            and metadata.get("kaiwu_source_path") == document.path
            and metadata.get("kaiwu_content_sha256") == document.content_sha256
            and type(metadata.get("kaiwu_content_size")) is int
            and metadata.get("kaiwu_content_size") == document.size
            and metadata.get("kaiwu_kind") == _KIND
        )

    def _remote_valid(self, row: Any, scope: TrustedScope, document: SourceDocument) -> bool:
        if not isinstance(row, Mapping) or row.get("memory") != document.text:
            return False
        return self._meta_matches(row.get("metadata"), scope, document)

    def _verify_hit(self, row: Any, scope: TrustedScope, documents: Mapping[str, SourceDocument]) -> VerifiedHit:
        if not isinstance(row, Mapping):
            raise _Reject
        text = row.get("memory")
        if type(text) is not str:
            raise _Reject
        metadata = row.get("metadata")
        if not isinstance(metadata, Mapping):
            raise _Reject
        source_id = metadata.get("kaiwu_source_id")
        if type(source_id) is not str or not source_id:
            raise _Reject
        document = documents.get(source_id)
        if document is None or not self._meta_matches(metadata, scope, document) or text != document.text:
            raise _Reject
        score = row.get("score", 0.0)
        if type(score) is bool or not isinstance(score, (int, float)):
            raise _Reject
        return VerifiedHit(
            source_id=document.source_id,
            path=document.path,
            text=document.text,
            content_sha256=document.content_sha256,
            score=float(score),
            revision=scope.revision,
            native_epoch=scope.native_epoch,
        )

    def _fetch_k(self) -> int:
        return min(MAX_TOP_K, max(self.config.top_k * 3, self.config.top_k + 5))

    def _validate_query(self, query: Any) -> None:
        if type(query) is not str:
            raise Mem0IndexQueryInvalid("检索词必须是文本")
        if not query.strip() or len(query) > self.config.max_query_chars or "\x00" in query:
            raise Mem0IndexQueryInvalid("检索词为空、过长或包含无效字符")

    def _best_effort_delete(self, memory_ids: list[str]) -> None:
        for memory_id in sorted(set(memory_ids)):
            try:
                self.backend.delete(memory_id)  # type: ignore[union-attr]
            except Exception:
                # The row remains generation-tagged and cannot pass search
                # verification; a later sync retries cleanup.
                pass

    def sync(self, scope: TrustedScope) -> SyncResult:
        """Mirror one authoritative snapshot into Mem0; idempotent per source digest."""
        self._require_enabled()
        scope = self._checked_scope(scope)
        with self._lock:
            before = self._load(scope)
            documents = {document.source_id: document for document in before.documents if document.text}
            skipped_empty = len(before.documents) - len(documents)
            try:
                raw = self.backend.get_all(filters=self._filters(scope))  # type: ignore[union-attr]
            except Mem0IndexError:
                raise
            except Exception as exc:
                raise Mem0IndexUnavailable("Mem0 派生索引枚举失败") from exc
            rows = self._rows(raw, "get_all")
            if len(rows) >= MAX_ENUMERATION_ROWS:
                raise Mem0IndexUnavailable("Mem0 枚举达到安全上限，需先核对并清理派生索引")

            stale_ids: list[str] = []
            kept: dict[str, Any] = {}
            rejected = 0
            cleanup_pending = 0

            def mark_stale(row: Any) -> None:
                nonlocal cleanup_pending, rejected
                if self._row_owned(row, scope):
                    memory_id = self._row_id(row)
                    if memory_id is None:
                        cleanup_pending += 1
                        rejected += 1
                    else:
                        stale_ids.append(memory_id)
                else:
                    rejected += 1

            grouped: dict[str, list[Any]] = {}
            for row in rows:
                metadata = row.get("metadata") if isinstance(row, Mapping) else None
                source_id = metadata.get("kaiwu_source_id") if isinstance(metadata, Mapping) else None
                if type(source_id) is str and source_id:
                    grouped.setdefault(source_id, []).append(row)
                else:
                    rejected += 1
                    if self._row_id(row) is not None:
                        # Unknown rows are not deleted: they cannot be proven
                        # to belong to this schema, but they cannot pass search.
                        cleanup_pending += 1

            for source_id, candidates in grouped.items():
                document = documents.get(source_id)
                if document is None:
                    for row in candidates:
                        mark_stale(row)
                    continue
                valid = [row for row in candidates if self._remote_valid(row, scope, document)]
                if valid:
                    valid.sort(key=lambda row: (self._row_id(row) or "", str(row.get("memory", ""))))
                    kept[source_id] = valid[0]
                    kept_identity = id(valid[0])
                    for row in candidates:
                        if id(row) != kept_identity:
                            mark_stale(row)
                else:
                    for row in candidates:
                        mark_stale(row)

            added_ids: list[str] = []
            # Never roll back an ID that was present before this operation.
            # The backend is dedicated to this host; unknown outcomes need
            # explicit reconciliation. Stale/revoked rows cannot pass search.
            existing_ids = {self._row_id(row) for row in rows}
            acknowledged_ids: set[str] = set()
            added = 0
            errors: list[str] = []

            def require_current() -> None:
                current = self._load(scope)
                if current.documents_digest != before.documents_digest:
                    raise Mem0IndexSourceChanged("权威记忆在索引期间发生变化，未完成本轮派生索引")

            try:
                for source_id, document in sorted(documents.items()):
                    if source_id in kept:
                        continue
                    # Provider calls are not atomic with host authorization.
                    # Revalidate before each submission, not just the batch.
                    require_current()
                    try:
                        response = self.backend.add(  # type: ignore[union-attr]
                            document.text,
                            user_id=scope.user_id,
                            agent_id=self.config.agent_id,
                            run_id=scope.conversation_id,
                            metadata=self._metadata(scope, document),
                            infer=False,
                        )
                    except Mem0IndexError:
                        raise
                    except Exception as exc:
                        errors.append(f"add:{type(exc).__name__}")
                        require_current()
                        continue
                    added_rows = self._rows(response, "add")
                    accepted = [row for row in added_rows if isinstance(row, Mapping) and self._row_id(row)]
                    if not accepted:
                        errors.append("add:no_result")
                    else:
                        added += 1
                        for row in accepted:
                            memory_id = self._row_id(row)
                            if memory_id:
                                acknowledged_ids.add(memory_id)
                                if memory_id not in existing_ids:
                                    added_ids.append(memory_id)
                    require_current()

                deleted = 0
                kept_ids = {self._row_id(row) for row in kept.values()}
                for memory_id in sorted(set(stale_ids) - acknowledged_ids - kept_ids):
                    require_current()
                    try:
                        self.backend.delete(memory_id)  # type: ignore[union-attr]
                    except Mem0IndexError:
                        raise
                    except Exception as exc:
                        errors.append(f"delete:{type(exc).__name__}")
                        cleanup_pending += 1
                    else:
                        deleted += 1

                require_current()
            except Mem0IndexError:
                # Best effort rollback of acknowledged new rows even if the
                # current source is no longer readable. Never replay the add.
                self._best_effort_delete(added_ids)
                raise

            status = "partial" if errors or cleanup_pending or rejected else ("indexed" if added or deleted else "unchanged")
            return SyncResult(
                status=status,
                generation=scope.generation,
                documents=len(documents),
                skipped_empty=skipped_empty,
                added=added,
                unchanged=len(kept),
                deleted=deleted,
                rejected_remote=rejected,
                cleanup_pending=cleanup_pending,
                errors=tuple(errors),
            )

    def search(self, query: str, *, scope: TrustedScope) -> SearchResult:
        """Return only hits revalidated against the current authoritative snapshot.

        The model may supply ``query`` only. A backend failure raises instead of
        returning an empty result, so the host falls back to authoritative
        memory rather than treating an outage as "no memory".
        """
        self._require_enabled()
        scope = self._checked_scope(scope)
        self._validate_query(query)
        with self._lock:
            before = self._load(scope)
            try:
                raw = self.backend.search(  # type: ignore[union-attr]
                    query,
                    filters=self._filters(scope),
                    top_k=self._fetch_k(),
                    threshold=self.config.threshold,
                )
            except Mem0IndexError:
                raise
            except Exception as exc:
                raise Mem0IndexUnavailable("Mem0 派生索引检索失败") from exc
            rows = self._rows(raw, "search")
            after = self._load(scope)
            if after.documents_digest != before.documents_digest:
                raise Mem0IndexSourceChanged("权威记忆在检索期间发生变化，候选不可用")
            documents = {document.source_id: document for document in after.documents if document.text}
            verified: dict[str, VerifiedHit] = {}
            rejected = 0
            for row in rows:
                try:
                    hit = self._verify_hit(row, scope, documents)
                except _Reject:
                    rejected += 1
                    continue
                current = verified.get(hit.source_id)
                if current is None or hit.score > current.score:
                    verified[hit.source_id] = hit
            ordered = tuple(sorted(verified.values(), key=lambda hit: (-hit.score, hit.source_id))[:self.config.top_k])
            return SearchResult(generation=scope.generation, hits=ordered, fetched=len(rows), rejected=rejected)

    def purge(self, scope: TrustedScope) -> PurgeResult:
        """Remove all derived rows after the authoritative memory is empty."""
        self._require_enabled()
        scope = self._checked_scope(scope)
        with self._lock:
            snapshot = self._load(scope)
            if snapshot.documents:
                raise Mem0IndexScopeInvalid("仅能在权威记忆已清空时清理派生索引")
            try:
                self.backend.delete_all(  # type: ignore[union-attr]
                    user_id=scope.user_id,
                    agent_id=self.config.agent_id,
                    run_id=scope.conversation_id,
                )
            except Mem0IndexError:
                raise
            except Exception as exc:
                raise Mem0IndexUnavailable("Mem0 派生索引清理失败") from exc
            return PurgeResult(generation=scope.generation)


class Mem0SdkBackend:
    """Thin wrapper over ``mem0.Memory`` using the probe's existing contract."""

    def __init__(self, memory: Any):
        if memory is None:
            raise ValueError("memory 不能为空")
        self.memory = memory

    def add(self, text: str, *, user_id: str, agent_id: str, run_id: str,
            metadata: Mapping[str, Any], infer: bool) -> Mapping[str, Any]:
        if infer is not False:
            raise ValueError("派生索引只允许 infer=False，禁止自动写入正式事实")
        return self.memory.add(
            text,
            user_id=user_id,
            agent_id=agent_id,
            run_id=run_id,
            metadata=dict(metadata),
            infer=False,
        )

    def search(self, query: str, *, filters: Mapping[str, Any], top_k: int,
               threshold: float) -> Mapping[str, Any]:
        return self.memory.search(query, filters=dict(filters), top_k=top_k, threshold=threshold)

    def get_all(self, *, filters: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.memory.get_all(filters=dict(filters),
                                     top_k=MAX_ENUMERATION_ROWS, show_expired=True)
        if not isinstance(result, Mapping) or not isinstance(result.get("results"), list):
            raise Mem0IndexUnavailable("Mem0 枚举回执格式无效")
        # A full page is ambiguous; do not reconcile a partial generation
        # or add more duplicates. An explicit repair is required.
        if len(result["results"]) >= MAX_ENUMERATION_ROWS:
            raise Mem0IndexUnavailable("Mem0 枚举达到安全上限，需先核对并清理派生索引")
        return result

    def delete(self, memory_id: str) -> Any:
        return self.memory.delete(memory_id)

    def delete_all(self, *, user_id: str, agent_id: str, run_id: str) -> Any:
        return self.memory.delete_all(user_id=user_id, agent_id=agent_id, run_id=run_id)

    def close(self) -> Any:
        close = getattr(self.memory, "close", None)
        return close() if callable(close) else None


def load_mem0_backend(config: Mapping[str, Any]) -> Mem0SdkBackend:
    """Load mem0ai 2.2.1 with telemetry disabled and no provider defaults.

    The caller must pass an explicit self-hosted config containing
    ``vector_store``, ``embedder`` and ``llm``. This function performs no
    network requests itself; constructing the SDK may initialize the configured
    local store and provider client.
    """
    if not isinstance(config, Mapping) or not {"vector_store", "embedder", "llm"} <= set(config):
        raise Mem0IndexUnavailable("必须显式配置 vector_store、embedder 和 llm，禁止默认供应方")
    os.environ["MEM0_TELEMETRY"] = "false"
    try:
        from importlib.metadata import version as package_version
        installed = package_version("mem0ai")
    except Exception as exc:
        raise Mem0IndexUnavailable("Mem0 SDK 2.2.1 未安装") from exc
    if installed != MEM0_SDK_VERSION:
        raise Mem0IndexUnavailable(f"Mem0 SDK 版本必须为 {MEM0_SDK_VERSION}")
    try:
        from mem0 import Memory
        from mem0.memory import telemetry
    except Exception as exc:
        raise Mem0IndexUnavailable("Mem0 SDK 导入失败") from exc
    if getattr(telemetry, "MEM0_TELEMETRY", None) is not False:
        raise Mem0IndexUnavailable("Mem0 遥测状态未能确认关闭")
    try:
        memory = Memory.from_config(dict(config))
    except Exception as exc:
        raise Mem0IndexUnavailable("Mem0 初始化失败") from exc
    return Mem0SdkBackend(memory)


class NativeMemorySourceReader:
    """Read committed Harness snapshots through the host's verified store.

    ``scope`` must be built from the binding returned by
    ``NativeMemory.snapshot_for_run``. The reader rechecks the SQLite pointer
    and verified tree identity before and after reading. If a run_id is given,
    the run must still be the conversation's active run.
    """

    def __init__(self, native_memory: Any, *, run_id: str | None = None):
        self.memory = native_memory
        self.run_id = run_id

    def _admitted_scope(self, user_id: str, conversation_id: str) -> TrustedScope:
        """Read only existing, published native snapshots; never migrate or approve.

        Conservatively refuse legacy imports (including nonempty first
        migrations with origin=empty) until a separate explicit review policy
        exists. A later edit of one file does not approve inherited siblings.
        """
        service = self.memory.service
        with service.db.lock:
            try:
                user = service.require("users", user_id)
                conversation = service.require("conversations", conversation_id, user)
                if not user["enabled"] or conversation["archived"] or conversation.get("runtime_type") != "harness":
                    raise Mem0IndexScopeInvalid("当前会话不允许使用派生记忆索引")
                service.usable_assistant(conversation["assistant_id"], user)
                row = service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (conversation_id,))
                if not row or row["storage_version"] != 1 or not row["active_snapshot_id"]:
                    raise Mem0IndexSourceUnavailable("尚无已发布的原生记忆快照")
                if row["mutation_id"] or (self.run_id is None and self.memory.busy(user_id, conversation_id)):
                    raise Mem0IndexSourceChanged("记忆或会话正在变化")
                if row["origin"] != "empty":
                    raise Mem0IndexScopeInvalid("旧版迁移记忆尚未获得派生索引授权")
                initial = service.db.all("""SELECT o.user_id AS owner,s.user_id AS snapshot_owner,
                    s.conversation_id AS snapshot_conversation,s.state,s.digest
                    FROM memory_operations o JOIN memory_snapshot_records s ON s.id=o.snapshot_id
                    WHERE o.conversation_id=? AND o.kind='migration' AND o.status='committed'""", (conversation_id,))
                if (len(initial) != 1 or initial[0]["owner"] != user_id
                        or initial[0]["snapshot_owner"] != user_id
                        or initial[0]["snapshot_conversation"] != conversation_id
                        or initial[0]["state"] != "published"
                        or initial[0]["digest"] != hashlib.sha256(b"").hexdigest()):
                    raise Mem0IndexScopeInvalid("无法证明初始记忆为空；不自动索引旧内容")
                record = service.db.one("SELECT * FROM memory_snapshot_records WHERE id=?", (row["active_snapshot_id"],))
                if (not record or record["state"] != "published" or record["user_id"] != user_id
                        or record["conversation_id"] != conversation_id or record["digest"] != row["digest"]
                        or record["manifest_digest"] != row["manifest_digest"]):
                    raise Mem0IndexSourceUnavailable("当前记忆快照未通过发布记录核验")
                binding = {"user_id": user_id, "conversation_id": conversation_id,
                           "native_epoch": conversation["native_epoch"], "revision": row["revision"],
                           "digest": row["digest"], "snapshot_id": row["active_snapshot_id"],
                           "manifest_digest": row["manifest_digest"]}
                if self.run_id is not None:
                    run = service.require("runs", self.run_id, user)
                    meta = json.loads(run.get("execution_meta") or "{}")
                    if (conversation["active_run_id"] != self.run_id or run["conversation_id"] != conversation_id
                            or run["status"] not in {"running", "awaiting_approval"}
                            or not isinstance(meta, dict) or meta.get("memory_binding") != binding):
                        raise Mem0IndexSourceChanged("当前任务未绑定此已发布记忆版本")
                return TrustedScope.from_binding(binding)
            except Mem0IndexError:
                raise
            except Exception:
                raise Mem0IndexScopeInvalid("无法核验当前记忆索引授权") from None

    def current_scope(self, user_id: str, conversation_id: str) -> TrustedScope:
        """Host-only scope derivation. No creation, migration or backend calls."""
        scope = self._admitted_scope(user_id, conversation_id)
        self.load(scope)
        return scope

    def load(self, scope: TrustedScope) -> SourceSnapshot:
        if type(scope) is not TrustedScope:
            raise Mem0IndexScopeInvalid("索引作用域必须由宿主生成")
        binding = scope.as_binding()
        try:
            if self._admitted_scope(scope.user_id, scope.conversation_id) != scope:
                raise Mem0IndexSourceChanged("权威记忆快照已变化，派生索引不得继续使用")
            self.memory.snapshots.assert_binding(binding, self.run_id)
            with self.memory.snapshots.fs.verify(binding) as tree:
                self.memory.snapshots.assert_binding(binding, self.run_id)
                documents = tuple(
                    SourceDocument(
                        source_id=path,
                        path=path,
                        text=data.decode("utf-8"),
                        content_sha256=hashlib.sha256(data).hexdigest(),
                    )
                    for path, data in sorted(tree.files.items())
                )
                tree.assert_unchanged()
            self.memory.snapshots.assert_binding(binding, self.run_id)
            if self._admitted_scope(scope.user_id, scope.conversation_id) != scope:
                raise Mem0IndexSourceChanged("权威记忆快照已变化，派生索引不得继续使用")
        except Mem0IndexError:
            raise
        except Exception as exc:
            if getattr(exc, "code", "") in {"MEMORY_REVISION_CONFLICT", "MEMORY_RUN_CHANGED", "MEMORY_BUSY"}:
                raise Mem0IndexSourceChanged("权威记忆快照已变化，派生索引不得继续使用") from exc
            raise Mem0IndexSourceUnavailable("权威记忆快照不可用") from exc
        return SourceSnapshot.build(scope, documents)


__all__ = [
    "INDEX_AGENT_ID",
    "Mem0Backend",
    "Mem0DerivedIndex",
    "Mem0IndexConfig",
    "Mem0IndexDisabled",
    "Mem0IndexError",
    "Mem0IndexQueryInvalid",
    "Mem0IndexScopeInvalid",
    "Mem0IndexSourceChanged",
    "Mem0IndexSourceInvalid",
    "Mem0IndexSourceUnavailable",
    "Mem0IndexUnavailable",
    "Mem0SdkBackend",
    "NativeMemorySourceReader",
    "PurgeResult",
    "SCHEMA",
    "SearchResult",
    "SourceDocument",
    "SourceReader",
    "SourceSnapshot",
    "SyncResult",
    "TrustedScope",
    "VerifiedHit",
    "load_mem0_backend",
    "text_digest",
]
