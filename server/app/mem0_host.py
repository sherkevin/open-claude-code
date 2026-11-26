"""Opt-in host boundary for the derived Mem0 index, not a product route.

The caller must supply an authorized backend and use a single host data
directory for that backend. Linux flock serializes cooperating local host
processes; this is not a distributed lock, a cost limit or an authorization
to send customer data to an embedding provider.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
from typing import Literal

from .mem0_index import (
    Mem0Backend, Mem0DerivedIndex, Mem0IndexConfig, Mem0IndexDisabled,
    Mem0IndexError, Mem0IndexUnavailable, NativeMemorySourceReader,
    SyncResult, VerifiedHit,
)


@dataclass(frozen=True)
class Lookup:
    status: Literal["verified", "fallback"]
    hits: tuple[VerifiedHit, ...] = ()
    reason: str | None = None


class Mem0Host:
    """Explicitly injected single-host adapter; never initialize memory or
    silently synchronize from the retrieval path. No SDK/provider defaults.
    """

    def __init__(self, service, config: Mem0IndexConfig = Mem0IndexConfig(),
                 *, backend: Mem0Backend | None = None,
                 max_context_bytes: int = 16384):
        if type(config) is not Mem0IndexConfig:
            raise TypeError("invalid Mem0 host config")
        if config.enabled and backend is None:
            raise ValueError("enabled Mem0 host needs an explicit backend")
        if type(max_context_bytes) is not int or not 1 <= max_context_bytes <= 131072:
            raise ValueError("invalid Mem0 context byte budget")
        self.service = service
        self.config = config
        self.backend = backend
        self.max_context_bytes = max_context_bytes

    @contextmanager
    def _slot(self):
        if not self.config.enabled:
            raise Mem0IndexDisabled("Mem0 派生索引未启用")
        # Fixed host path; no user-, model- or conversation-controlled names.
        # Never unlink this lock: a replacement inode would split the lock.
        path = Path(self.service.settings.data_dir) / ".mem0-index.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            raise Mem0IndexUnavailable("Mem0 单写锁不可用") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise Mem0IndexUnavailable("Mem0 单写锁身份无效")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Mem0IndexUnavailable("Mem0 单写服务忙或不可用") from None
            yield
        finally:
            os.close(fd)

    def _index(self, user_id: str, conversation_id: str, *, run_id: str | None):
        reader = NativeMemorySourceReader(self.service.memory, run_id=run_id)
        scope = reader.current_scope(user_id, conversation_id)
        return Mem0DerivedIndex(self.config, self.backend, reader), scope

    def sync(self, user_id: str, conversation_id: str) -> SyncResult:
        """Explicit host maintenance only. Never called automatically on lookup."""
        with self._slot():
            index, scope = self._index(user_id, conversation_id, run_id=None)
            return index.sync(scope)

    def clear_committed(self, user_id: str, conversation_id: str, operation_key: str) -> SyncResult:
        """Explicit maintenance for a currently authorized native clear.

        Reuse scoped enumeration and individually owned row reconciliation;
        never invoke broad delete_all or automatically replay an uncertain clear.
        A successful result requires a fresh complete empty enumeration.
        """
        with self._slot():
            reader = NativeMemorySourceReader(self.service.memory)
            scope = reader.current_scope(user_id, conversation_id)
            reader.require_committed_clear(scope, operation_key)
            index = Mem0DerivedIndex(self.config, self.backend, reader)
            result = index.sync(scope)
            reader.require_committed_clear(scope, operation_key)
            if result.status == "partial":
                return result
            try:
                remaining = index._rows(self.backend.get_all(filters=index._filters(scope)), "get_all")
            except Mem0IndexError:
                raise
            except Exception as exc:
                raise Mem0IndexUnavailable("Mem0 清理结果无法确认") from exc
            reader.require_committed_clear(scope, operation_key)
            if remaining:
                raise Mem0IndexUnavailable("Mem0 清理后仍有派生记录，未确认完成")
            return result

    def lookup(self, user_id: str, conversation_id: str, query: str,
               *, run_id: str | None = None) -> Lookup:
        """At most one search, zero index writes. Whole verified hit texts only;
        fall back to authoritative memory on any admission/provider failure or
        when the byte budget cannot fit the complete result. A verified empty
        result is not proof of absent authoritative memory.
        """
        try:
            with self._slot():
                index, scope = self._index(user_id, conversation_id, run_id=run_id)
                result = index.search(query, scope=scope)
                if sum(len(hit.text.encode("utf-8")) for hit in result.hits) > self.max_context_bytes:
                    return Lookup("fallback", reason="context_byte_budget")
                return Lookup("verified", result.hits)
        except Mem0IndexError as exc:
            # Fixed exception class only: no backend response, paths or content.
            return Lookup("fallback", reason=type(exc).__name__)
