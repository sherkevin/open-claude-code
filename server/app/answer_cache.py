"""Bounded, permission-aware reuse of finished model answers.

Only answers that are safe to reuse are stored.  Each entry is bound to the
user, assistant, conversation, authorized knowledge version, model version and
normalized prompt that produced it, so a different principal or a changed
source set can never read another answer.  Side-effect tool results and approval
outcomes are explicitly refused.  A cache hit re-runs the caller's current
authorization check before returning data; revoked grants or updated versions
delete the entry instead of serving it.

This module performs no model, network or credential access.
"""

from __future__ import annotations

import hashlib
import json
import threading
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .db import dump, now, uid

DEFAULT_TTL_SECONDS = 3600
MAX_ENTRIES = 256
MAX_PROMPT_CHARS = 4000
MAX_ANSWER_CHARS = 48000
MAX_CITATIONS = 50


class CacheRejected(ValueError):
    """A caller asked to store an answer that must never be reused."""


@dataclass(frozen=True)
class CachedAnswer:
    answer: str
    citations: tuple[dict[str, Any], ...]
    knowledge_version: str
    model_version: str
    created_at: str
    expires_at: str
    cache_key: str


def normalize_prompt(text: str) -> str:
    if not isinstance(text, str):
        raise CacheRejected("prompt must be text")
    value = unicodedata.normalize("NFKC", text)
    value = " ".join(value.split()).casefold()
    if not value or len(value) > MAX_PROMPT_CHARS:
        raise CacheRejected("prompt is empty or too long")
    return value


def cache_key(
    *,
    user_id: str,
    assistant_id: str,
    prompt: str,
    conversation_id: str | None = None,
    knowledge_version: str = "",
    model_version: str = "",
) -> tuple[str, str]:
    """Return ``(key, prompt_digest)`` for the immutable reuse boundary."""
    digest = hashlib.sha256(normalize_prompt(prompt).encode("utf-8")).hexdigest()
    boundary = {
        "user_id": user_id,
        "assistant_id": assistant_id,
        "conversation_id": conversation_id or "",
        "knowledge_version": knowledge_version or "",
        "model_version": model_version or "",
        "prompt_digest": digest,
    }
    return hashlib.sha256(dump(boundary).encode("utf-8")).hexdigest(), digest


class AnswerCacheService:
    """Persisted answer cache with explicit revocation and bounded storage."""

    def __init__(self, service, *, ttl_seconds: int | None = None, max_entries: int | None = None):
        self.service = service
        self.ttl_seconds = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
        self.max_entries = MAX_ENTRIES if max_entries is None else max(1, int(max_entries))
        self._stats = {"hits": 0, "misses": 0, "stores": 0, "rejected": 0, "evictions": 0, "invalidations": 0}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Reads and writes
    # ------------------------------------------------------------------
    def lookup(
        self,
        *,
        user_id: str,
        assistant_id: str,
        prompt: str,
        conversation_id: str | None = None,
        knowledge_version: str = "",
        model_version: str = "",
        validate: Callable[[dict], bool] | None = None,
    ) -> CachedAnswer | None:
        key, _digest = cache_key(
            user_id=user_id,
            assistant_id=assistant_id,
            prompt=prompt,
            conversation_id=conversation_id,
            knowledge_version=knowledge_version,
            model_version=model_version,
        )
        row = self.service.db.one("SELECT * FROM answer_cache WHERE cache_key=?", (key,))
        if not row:
            self._bump("misses")
            return None
        if row["expires_at"] <= now():
            self._delete_key(key)
            self._bump("misses")
            return None
        # A hit is not an authorization proof. Re-run the current grant check
        # before any answer leaves the cache; a revoked user, assistant or
        # knowledge base invalidates the stored copy immediately.
        if validate is not None:
            try:
                still_allowed = validate(row) is True
            except Exception:
                still_allowed = False
            if not still_allowed:
                self._delete_key(key)
                self._bump("misses")
                return None
        stamp = now()
        self.service.db.execute("UPDATE answer_cache SET last_hit_at=? WHERE id=?", (stamp, row["id"]))
        self._bump("hits")
        return CachedAnswer(
            answer=row["answer"],
            citations=tuple(json.loads(row["citations"] or "[]")),
            knowledge_version=row["knowledge_version"],
            model_version=row["model_version"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
            cache_key=key,
        )

    def put(
        self,
        *,
        user_id: str,
        assistant_id: str,
        prompt: str,
        answer: str,
        citations: Iterable[dict[str, Any]] = (),
        conversation_id: str | None = None,
        knowledge_version: str = "",
        knowledge_bases: Iterable[str] = (),
        model_version: str = "",
        ttl_seconds: int | None = None,
        side_effects: bool = False,
        approval_involved: bool = False,
        sensitive: bool = False,
    ) -> CachedAnswer | None:
        # Reuse is limited to pure model answers. Anything that changed state,
        # depended on an approval decision, or is marked sensitive must be
        # recomputed rather than replayed for a later request.
        if side_effects or approval_involved or sensitive:
            self._bump("rejected")
            return None
        if not isinstance(answer, str) or not answer.strip():
            self._bump("rejected")
            return None
        if len(answer) > MAX_ANSWER_CHARS:
            self._bump("rejected")
            return None
        try:
            key, digest = cache_key(
                user_id=user_id,
                assistant_id=assistant_id,
                prompt=prompt,
                conversation_id=conversation_id,
                knowledge_version=knowledge_version,
                model_version=model_version,
            )
        except CacheRejected:
            self._bump("rejected")
            return None
        citations = tuple(citations or ())
        if len(citations) > MAX_CITATIONS:
            citations = citations[:MAX_CITATIONS]
        ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        created = now()
        expires = self._expiry(created, ttl)
        bases = list(dict.fromkeys(str(item) for item in (knowledge_bases or ()) if str(item)))
        self.service.db.execute(
            "INSERT INTO answer_cache(id,cache_key,user_id,assistant_id,conversation_id,prompt_digest,knowledge_version,knowledge_bases,model_version,answer,citations,created_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET answer=excluded.answer,citations=excluded.citations,knowledge_version=excluded.knowledge_version,knowledge_bases=excluded.knowledge_bases,model_version=excluded.model_version,created_at=excluded.created_at,expires_at=excluded.expires_at,last_hit_at=NULL",
            (uid(), key, user_id, assistant_id, conversation_id, digest, knowledge_version or "", dump(bases), model_version or "", answer, dump(list(citations)), created, expires),
        )
        self._bump("stores")
        self._trim()
        return CachedAnswer(answer, citations, knowledge_version or "", model_version or "", created, expires, key)

    @staticmethod
    def _expiry(created: str, ttl: int) -> str:
        from datetime import datetime, timedelta, timezone

        moment = datetime.fromisoformat(created)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (moment + timedelta(seconds=max(0, ttl))).isoformat()

    def _trim(self) -> None:
        self.service.db.execute("DELETE FROM answer_cache WHERE expires_at<=?", (now(),))
        count = self.service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"]
        excess = int(count) - self.max_entries
        if excess <= 0:
            return
        victims = self.service.db.all("SELECT id FROM answer_cache ORDER BY created_at,rowid LIMIT ?", (excess,))
        for victim in victims:
            self.service.db.execute("DELETE FROM answer_cache WHERE id=?", (victim["id"],))
            self._bump("evictions")

    # ------------------------------------------------------------------
    # Invalidation
    # ------------------------------------------------------------------
    def _delete_key(self, key: str) -> None:
        self.service.db.execute("DELETE FROM answer_cache WHERE cache_key=?", (key,))

    def invalidate_user(self, user_id: str) -> int:
        return self._invalidate("user_id=?", (user_id,))

    def invalidate_assistant(self, assistant_id: str) -> int:
        return self._invalidate("assistant_id=?", (assistant_id,))

    def invalidate_conversation(self, conversation_id: str) -> int:
        return self._invalidate("conversation_id=?", (conversation_id,))

    def invalidate_model(self, model_version: str) -> int:
        return self._invalidate("model_version=?", (model_version,))

    def invalidate_knowledge(self, knowledge_base_id: str) -> int:
        # knowledge_bases is a JSON array; a substring match would be unsafe,
        # so filter in Python after reading the bounded candidate set.
        removed = 0
        for row in self.service.db.all("SELECT id,knowledge_bases FROM answer_cache"):
            try:
                bases = json.loads(row["knowledge_bases"] or "[]")
            except ValueError:
                bases = []
            if knowledge_base_id in bases:
                self.service.db.execute("DELETE FROM answer_cache WHERE id=?", (row["id"],))
                removed += 1
        if removed:
            self._bump("invalidations", removed)
        return removed

    def invalidate_all(self) -> int:
        count = self.service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"]
        self.service.db.execute("DELETE FROM answer_cache")
        if count:
            self._bump("invalidations", int(count))
        return int(count)

    def purge_expired(self) -> int:
        count = self.service.db.one("SELECT COUNT(*) AS n FROM answer_cache WHERE expires_at<=?", (now(),))["n"]
        self.service.db.execute("DELETE FROM answer_cache WHERE expires_at<=?", (now(),))
        return int(count)

    def _invalidate(self, where: str, values: tuple) -> int:
        count = self.service.db.one(f"SELECT COUNT(*) AS n FROM answer_cache WHERE {where}", values)["n"]
        self.service.db.execute(f"DELETE FROM answer_cache WHERE {where}", values)
        if count:
            self._bump("invalidations", int(count))
        return int(count)

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    def _bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._stats[name] = self._stats.get(name, 0) + amount

    def stats(self) -> dict[str, int]:
        with self._lock:
            values = dict(self._stats)
        values["entries"] = int(self.service.db.one("SELECT COUNT(*) AS n FROM answer_cache")["n"])
        values["max_entries"] = self.max_entries
        values["ttl_seconds"] = self.ttl_seconds
        return values
