"""Synthetic-data tests for the disabled-by-default Mem0 derived index.

No Mem0 SDK, network provider, model or real customer data is used here. The
fake backend implements the same subset used by scripts/oss/mem0_probe.py so
the adapter contract can be reviewed without installing Mem0.
"""
from __future__ import annotations

import hashlib

import pytest

from app.mem0_index import (
    INDEX_AGENT_ID,
    Mem0DerivedIndex,
    Mem0IndexConfig,
    Mem0IndexDisabled,
    Mem0IndexError,
    Mem0IndexQueryInvalid,
    Mem0IndexScopeInvalid,
    Mem0IndexSourceChanged,
    Mem0IndexSourceInvalid,
    Mem0IndexUnavailable,
    Mem0SdkBackend,
    NativeMemorySourceReader,
    SCHEMA,
    SourceDocument,
    SourceSnapshot,
    TrustedScope,
    load_mem0_backend,
    text_digest,
)

USER_ID = "a" * 32
CONVERSATION_ID = "b" * 32
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def make_scope(*, revision=1, native_epoch=1, digest=None, snapshot_id=None,
               user_id=USER_ID, conversation_id=CONVERSATION_ID):
    digest = digest or hashlib.sha256(f"content:{revision}:{native_epoch}".encode()).hexdigest()
    snapshot_id = snapshot_id or hashlib.sha256(f"snapshot:{revision}:{native_epoch}".encode()).hexdigest()[:32]
    return TrustedScope.from_binding({
        "user_id": user_id,
        "conversation_id": conversation_id,
        "native_epoch": native_epoch,
        "revision": revision,
        "digest": digest,
        "snapshot_id": snapshot_id,
        "manifest_digest": hashlib.sha256(f"manifest:{revision}:{native_epoch}".encode()).hexdigest(),
    })


def make_document(path="MEMORY.md", text="approved synthetic fact"):
    return SourceDocument(source_id=path, path=path, text=text, content_sha256=text_digest(text))


class FakeMem0:
    """The probe's add/search/get_all/delete/delete_all subset, in memory."""

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.failures = set()
        self.counter = 0

    def _new_id(self):
        self.counter += 1
        return f"mem-{self.counter:04d}"

    def insert(self, text, metadata, *, user_id, agent_id, run_id, memory_id=None, score=0.9):
        memory_id = memory_id or self._new_id()
        self.rows[memory_id] = {
            "id": memory_id,
            "memory": text,
            "metadata": dict(metadata),
            "score": score,
            "_user_id": user_id,
            "_agent_id": agent_id,
            "_run_id": run_id,
        }
        return self.rows[memory_id]

    def add(self, text, *, user_id, agent_id, run_id, metadata, infer):
        self.calls.append(("add", text, user_id, agent_id, run_id, dict(metadata), infer))
        if "add" in self.failures:
            raise RuntimeError("synthetic add failure")
        row = self.insert(text, metadata, user_id=user_id, agent_id=agent_id, run_id=run_id)
        return {"results": [{"id": row["id"], "memory": text, "event": "ADD", "metadata": dict(metadata)}]}

    def search(self, query, *, filters, top_k, threshold):
        self.calls.append(("search", query, dict(filters), top_k, threshold))
        if "search" in self.failures:
            raise RuntimeError("synthetic search failure")
        rows = [dict(row) for row in self.rows.values() if self._matches(row, filters)]
        return {"results": rows[:top_k]}

    def get_all(self, *, filters):
        self.calls.append(("get_all", dict(filters)))
        if "get_all" in self.failures:
            raise RuntimeError("synthetic get_all failure")
        return {"results": [dict(row) for row in self.rows.values() if self._matches(row, filters)]}

    def delete(self, memory_id):
        self.calls.append(("delete", memory_id))
        if "delete" in self.failures:
            raise RuntimeError("synthetic delete failure")
        self.rows.pop(memory_id, None)
        return {"message": "deleted"}

    def delete_all(self, *, user_id, agent_id, run_id):
        self.calls.append(("delete_all", user_id, agent_id, run_id))
        if "delete_all" in self.failures:
            raise RuntimeError("synthetic delete_all failure")
        for memory_id, row in list(self.rows.items()):
            if (row["_user_id"], row["_agent_id"], row["_run_id"]) == (user_id, agent_id, run_id):
                del self.rows[memory_id]
        return {"message": "deleted"}

    @staticmethod
    def _matches(row, filters):
        return all(row["_" + key] == value for key, value in filters.items())


class FakeReader:
    def __init__(self, documents=None):
        self.documents = list(documents or [])
        self.loads = 0

    def load(self, scope):
        self.loads += 1
        return SourceSnapshot.build(scope, self.documents)


class SequenceReader:
    """Return a fixed sequence of document sets to model a version race."""

    def __init__(self, *document_sets):
        self.document_sets = [tuple(documents) for documents in document_sets]
        self.calls = 0

    def load(self, scope):
        documents = self.document_sets[min(self.calls, len(self.document_sets) - 1)]
        self.calls += 1
        return SourceSnapshot.build(scope, documents)


def enabled_index(backend, reader):
    return Mem0DerivedIndex(Mem0IndexConfig(enabled=True), backend, reader)


def add_calls(backend):
    return [call for call in backend.calls if call[0] == "add"]


def test_default_is_disabled_and_enabled_requires_injected_dependencies():
    config = Mem0IndexConfig()
    backend, reader = FakeMem0(), FakeReader([make_document()])
    adapter = Mem0DerivedIndex(config, backend, reader)
    scope = make_scope()
    assert adapter.enabled is False
    with pytest.raises(Mem0IndexDisabled):
        adapter.sync(scope)
    with pytest.raises(Mem0IndexDisabled):
        adapter.search("synthetic query", scope=scope)
    with pytest.raises(Mem0IndexDisabled):
        adapter.purge(scope)
    assert backend.calls == [] and reader.loads == 0
    with pytest.raises(ValueError):
        Mem0DerivedIndex(Mem0IndexConfig(enabled=True))


def test_sync_indexes_approved_text_with_infer_false_and_host_scope_metadata():
    backend = FakeMem0()
    reader = FakeReader([make_document(), make_document("notes/plan.md", "second synthetic fact")])
    adapter = enabled_index(backend, reader)
    scope = make_scope()

    result = adapter.sync(scope)
    assert result.status == "indexed"
    assert result.added == 2 and result.unchanged == 0 and result.deleted == 0
    assert result.documents == 2 and result.skipped_empty == 0
    calls = add_calls(backend)
    assert len(calls) == 2
    for call in calls:
        metadata, infer = call[5], call[6]
        assert infer is False
        assert metadata["kaiwu_schema"] == SCHEMA
        assert metadata["kaiwu_generation"] == scope.generation
        assert metadata["kaiwu_user_id"] == scope.user_id
        assert metadata["kaiwu_conversation_id"] == scope.conversation_id
        assert metadata["kaiwu_agent_id"] == INDEX_AGENT_ID
        assert metadata["kaiwu_run_id"] == scope.conversation_id
        assert metadata["kaiwu_native_epoch"] == scope.native_epoch
        assert metadata["kaiwu_revision"] == scope.revision
        assert metadata["kaiwu_digest"] == scope.digest
        assert metadata["kaiwu_kind"] == "approved_memory_text"
    assert not any(call[6] is True for call in calls)

    again = adapter.sync(scope)
    assert again.status == "unchanged" and again.added == 0 and again.unchanged == 2
    assert len(add_calls(backend)) == 2


def test_empty_files_are_not_sent_to_mem0_but_still_part_of_the_source_snapshot():
    backend = FakeMem0()
    reader = FakeReader([make_document("EMPTY.md", ""), make_document()])
    adapter = enabled_index(backend, reader)
    result = adapter.sync(make_scope())
    assert result.added == 1 and result.documents == 1 and result.skipped_empty == 1
    assert all(call[1] for call in add_calls(backend))


def test_search_uses_only_host_scope_filters_and_returns_verified_hits():
    backend = FakeMem0()
    reader = FakeReader([make_document(), make_document("notes/plan.md", "second synthetic fact")])
    adapter = enabled_index(backend, reader)
    scope = make_scope()
    adapter.sync(scope)

    result = adapter.search("synthetic fact", scope=scope)
    assert {hit.text for hit in result.hits} == {"approved synthetic fact", "second synthetic fact"}
    assert result.rejected == 0 and result.fetched == 2
    call = [call for call in backend.calls if call[0] == "search"][-1]
    assert call[2] == {
        "user_id": scope.user_id,
        "agent_id": INDEX_AGENT_ID,
        "run_id": scope.conversation_id,
    }
    assert call[3] >= adapter.config.top_k


def test_search_rejects_stale_forged_missing_and_mismatched_candidates():
    backend = FakeMem0()
    reader = FakeReader([make_document()])
    adapter = enabled_index(backend, reader)
    scope = make_scope()
    adapter.sync(scope)
    valid = next(iter(backend.rows.values()))
    metadata = dict(valid["metadata"])

    backend.insert("forged text", metadata, user_id=scope.user_id, agent_id=INDEX_AGENT_ID,
                   run_id=scope.conversation_id, memory_id="forged-text")
    old_epoch = dict(metadata, kaiwu_native_epoch=scope.native_epoch - 1)
    backend.insert(valid["memory"], old_epoch, user_id=scope.user_id, agent_id=INDEX_AGENT_ID,
                   run_id=scope.conversation_id, memory_id="old-epoch")
    missing_source = dict(metadata, kaiwu_source_id="deleted.md", kaiwu_source_path="deleted.md")
    backend.insert(valid["memory"], missing_source, user_id=scope.user_id, agent_id=INDEX_AGENT_ID,
                   run_id=scope.conversation_id, memory_id="missing-source")
    bad_digest = dict(metadata, kaiwu_content_sha256=text_digest("other"))
    backend.insert(valid["memory"], bad_digest, user_id=scope.user_id, agent_id=INDEX_AGENT_ID,
                   run_id=scope.conversation_id, memory_id="bad-digest")

    result = adapter.search("synthetic", scope=scope)
    assert [hit.text for hit in result.hits] == ["approved synthetic fact"]
    assert result.rejected == 4
    assert result.fetched == 5


def test_new_generation_replaces_old_rows_and_old_scope_cannot_surface_old_text():
    backend = FakeMem0()
    reader = FakeReader([make_document(text="old approved fact")])
    adapter = enabled_index(backend, reader)
    old_scope = make_scope(revision=1, native_epoch=1)
    assert adapter.sync(old_scope).added == 1
    old_ids = set(backend.rows)

    new_scope = make_scope(revision=2, native_epoch=2)
    reader.documents = [make_document(text="new approved fact")]
    changed = adapter.sync(new_scope)
    assert changed.added == 1 and changed.deleted == 1
    assert old_ids.isdisjoint(backend.rows)
    assert [hit.text for hit in adapter.search("approved", scope=new_scope).hits] == ["new approved fact"]
    # Even if a permissive reader returns new documents for the old scope, the
    # generation check prevents the new row from being used as old evidence.
    assert adapter.search("approved", scope=old_scope).hits == ()


def test_clear_deletes_generation_rows_and_purge_requires_empty_authority():
    backend = FakeMem0()
    reader = FakeReader([make_document(text="old approved fact")])
    adapter = enabled_index(backend, reader)
    nonempty = make_scope(revision=1, native_epoch=1)
    adapter.sync(nonempty)
    with pytest.raises(Mem0IndexScopeInvalid):
        adapter.purge(nonempty)
    assert not any(call[0] == "delete_all" for call in backend.calls)

    empty = make_scope(revision=1, native_epoch=2, digest=EMPTY_DIGEST)
    reader.documents = []
    cleared = adapter.sync(empty)
    assert cleared.deleted == 1 and cleared.documents == 0
    assert backend.rows == {}
    assert adapter.search("approved", scope=empty).hits == ()

    purged = adapter.purge(empty)
    assert purged.generation == empty.generation
    call = [call for call in backend.calls if call[0] == "delete_all"][-1]
    assert call[1:] == (empty.user_id, INDEX_AGENT_ID, empty.conversation_id)


def test_duplicate_rows_are_collapsed_and_retry_is_idempotent():
    backend = FakeMem0()
    reader = FakeReader([make_document()])
    adapter = enabled_index(backend, reader)
    scope = make_scope()
    adapter.sync(scope)
    original = next(iter(backend.rows.values()))
    backend.insert(original["memory"], original["metadata"], user_id=scope.user_id,
                   agent_id=INDEX_AGENT_ID, run_id=scope.conversation_id, memory_id="duplicate")

    result = adapter.sync(scope)
    assert result.added == 0 and result.unchanged == 1 and result.deleted == 1
    assert len(backend.rows) == 1
    assert len(adapter.search("synthetic", scope=scope).hits) == 1


def test_backend_failures_raise_instead_of_looking_like_empty_memory():
    backend = FakeMem0()
    reader = FakeReader([make_document()])
    adapter = enabled_index(backend, reader)
    scope = make_scope()

    backend.failures.add("search")
    with pytest.raises(Mem0IndexUnavailable):
        adapter.search("synthetic", scope=scope)

    backend.failures.add("get_all")
    with pytest.raises(Mem0IndexUnavailable):
        adapter.sync(scope)

    backend.failures = {"add"}
    partial = adapter.sync(scope)
    assert partial.status == "partial" and partial.added == 0
    assert any(error.startswith("add:") for error in partial.errors)


@pytest.mark.parametrize("lost_response", ["exception", "empty"])
def test_unconfirmed_add_stops_batch_and_preserves_old_generation(lost_response):
    class LostReceiptBackend(FakeMem0):
        lose = False
        def add(self, *args, **kwargs):
            result = super().add(*args, **kwargs)
            if self.lose:
                self.lose = False
                if lost_response == "exception":
                    raise RuntimeError("synthetic lost receipt after commit")
                return {"results": []}
            return result

    backend = LostReceiptBackend()
    reader = FakeReader([make_document("a.md", "old fact")])
    adapter = enabled_index(backend, reader)
    adapter.sync(make_scope(revision=1))
    old_ids = set(backend.rows)
    reader.documents = [make_document("a.md", "new fact"), make_document("b.md", "second fact")]
    scope = make_scope(revision=2)
    backend.lose = True
    partial = adapter.sync(scope)
    assert partial.status == "partial" and partial.added == 0
    assert partial.unconfirmed_writes == 1 and partial.deferred_documents == 1
    assert partial.cleanup_pending == 1 and partial.deleted == 0
    assert len(backend.rows) == 2 and old_ids <= set(backend.rows)
    assert len(add_calls(backend)) == 2  # initial generation and one uncertain write
    assert not any(call[0] == "delete" for call in backend.calls)
    assert partial.as_dict()["unconfirmed_writes"] == 1
    # A new explicit sync is authorized and enumerates first. It recognizes
    # the committed row, adds only the missing document, and then cleans stale.
    recovered = adapter.sync(scope)
    assert recovered.status == "indexed" and recovered.added == 1 and recovered.unchanged == 1
    assert recovered.deleted == 1 and recovered.unconfirmed_writes == 0
    assert recovered.deferred_documents == 0 and len(backend.rows) == 2
    assert len(add_calls(backend)) == 3


def test_delete_failure_is_reported_and_stale_rows_never_enter_context():
    backend = FakeMem0()
    reader = FakeReader([make_document(text="old approved fact")])
    adapter = enabled_index(backend, reader)
    old_scope = make_scope(revision=1, native_epoch=1)
    adapter.sync(old_scope)

    new_scope = make_scope(revision=2, native_epoch=2)
    reader.documents = [make_document(text="new approved fact")]
    backend.failures.add("delete")
    partial = adapter.sync(new_scope)
    assert partial.status == "partial" and partial.added == 1 and partial.deleted == 0
    assert partial.cleanup_pending == 1
    assert any(error.startswith("delete:") for error in partial.errors)

    hits = adapter.search("approved", scope=new_scope)
    assert [hit.text for hit in hits.hits] == ["new approved fact"]
    assert hits.rejected == 1

    backend.failures.remove("delete")
    cleaned = adapter.sync(new_scope)
    assert cleaned.status == "indexed" and cleaned.deleted == 1
    assert len(backend.rows) == 1


def test_source_change_during_sync_removes_rows_written_for_the_old_snapshot():
    backend = FakeMem0()
    reader = SequenceReader([make_document(text="old")], [make_document(text="new")])
    adapter = enabled_index(backend, reader)
    with pytest.raises(Mem0IndexSourceChanged):
        adapter.sync(make_scope())
    assert backend.rows == {}


def test_source_change_during_search_returns_no_candidates():
    backend = FakeMem0()
    reader = FakeReader([make_document(text="stable")])
    adapter = enabled_index(backend, reader)
    scope = make_scope()
    adapter.sync(scope)
    adapter = enabled_index(backend, SequenceReader(
        [make_document(text="stable")], [make_document(text="changed")],
    ))
    with pytest.raises(Mem0IndexSourceChanged):
        adapter.search("stable", scope=scope)


def test_scope_requires_host_binding_fields_and_rejects_model_identity():
    base = {
        "user_id": USER_ID,
        "conversation_id": CONVERSATION_ID,
        "native_epoch": 1,
        "revision": 1,
        "digest": hashlib.sha256(b"x").hexdigest(),
        "snapshot_id": "c" * 32,
        "manifest_digest": hashlib.sha256(b"m").hexdigest(),
    }
    for field, value in (
        ("user_id", ""),
        ("conversation_id", None),
        ("native_epoch", -1),
        ("revision", True),
        ("digest", "A" * 64),
        ("snapshot_id", "bad/id"),
        ("manifest_digest", "short"),
    ):
        binding = dict(base)
        binding[field] = value
        with pytest.raises(Mem0IndexScopeInvalid):
            TrustedScope.from_binding(binding)
    with pytest.raises(Mem0IndexScopeInvalid):
        TrustedScope.from_binding({})

    adapter = enabled_index(FakeMem0(), FakeReader())
    with pytest.raises(Mem0IndexScopeInvalid):
        adapter.search("synthetic", scope=base)


def test_query_and_source_limits_fail_closed():
    adapter = enabled_index(FakeMem0(), FakeReader())
    scope = make_scope()
    max_query = Mem0IndexConfig().max_query_chars
    for query in ("", "   ", "x" * (max_query + 1)):
        with pytest.raises(Mem0IndexQueryInvalid):
            adapter.search(query, scope=scope)
    with pytest.raises(Mem0IndexQueryInvalid):
        adapter.search("bad\x00query", scope=scope)

    for path in ("/absolute.md", "../escape.md", "a//b.md", ".hidden.md", "a/./b.md",
                 "a/../b.md", "a\\b.md", "a:b.md", "a\nb.md", "a.json",
                 "a/b/c/d/e/f.md", "a" * 240 + ".md"):
        with pytest.raises(Mem0IndexSourceInvalid):
            SourceDocument(source_id=path, path=path, text="x", content_sha256=text_digest("x"))
    with pytest.raises(Mem0IndexSourceInvalid):
        SourceDocument(source_id="MEMORY.md", path="MEMORY.md", text="x", content_sha256=text_digest("other"))
    with pytest.raises(Mem0IndexSourceInvalid):
        SourceDocument(source_id="MEMORY.md", path="MEMORY.md", text="x" * (128 * 1024 + 1),
                       content_sha256=text_digest("x" * (128 * 1024 + 1)))

    too_many = [make_document(f"{index}.md", "x") for index in range(101)]
    with pytest.raises(Mem0IndexSourceInvalid):
        SourceSnapshot.build(scope, too_many)
    too_large = [make_document(f"{index}.md", "x" * (128 * 1024)) for index in range(9)]
    with pytest.raises(Mem0IndexSourceInvalid):
        SourceSnapshot.build(scope, too_large)


def test_sdk_wrapper_matches_probe_contract_and_refuses_infer_true():
    class RecordingMemory:
        def __init__(self):
            self.calls = []
            self.closed = 0

        def add(self, text, **kwargs):
            self.calls.append(("add", text, kwargs))
            return {"results": []}

        def search(self, query, **kwargs):
            self.calls.append(("search", query, kwargs))
            return {"results": []}

        def get_all(self, **kwargs):
            self.calls.append(("get_all", kwargs))
            return {"results": []}

        def delete(self, memory_id):
            self.calls.append(("delete", memory_id))
            return {"message": "deleted"}

        def delete_all(self, **kwargs):
            self.calls.append(("delete_all", kwargs))
            return {"message": "deleted"}

        def close(self):
            self.closed += 1

    raw = RecordingMemory()
    backend = Mem0SdkBackend(raw)
    backend.add("text", user_id="u", agent_id="a", run_id="r", metadata={"k": "v"}, infer=False)
    assert raw.calls[-1] == ("add", "text", {
        "user_id": "u", "agent_id": "a", "run_id": "r", "metadata": {"k": "v"}, "infer": False,
    })
    with pytest.raises(ValueError):
        backend.add("text", user_id="u", agent_id="a", run_id="r", metadata={}, infer=True)
    assert len([call for call in raw.calls if call[0] == "add"]) == 1

    backend.search("q", filters={"user_id": "u"}, top_k=3, threshold=0.2)
    assert raw.calls[-1] == ("search", "q", {"filters": {"user_id": "u"}, "top_k": 3, "threshold": 0.2})
    backend.get_all(filters={"user_id": "u"})
    assert raw.calls[-1] == ("get_all", {"filters": {"user_id": "u"}, "top_k": 201, "show_expired": True})
    backend.delete("memory-1")
    assert raw.calls[-1] == ("delete", "memory-1")
    backend.delete_all(user_id="u", agent_id="a", run_id="r")
    assert raw.calls[-1] == ("delete_all", {"user_id": "u", "agent_id": "a", "run_id": "r"})
    backend.close()
    assert raw.closed == 1


def test_sdk_close_owned_resources_once_and_reject_all_later_calls():
    class Resource:
        def __init__(self):
            self.closed = 0
        def close(self):
            self.closed += 1

    memory, embedding, vector = Resource(), Resource(), Resource()
    backend = Mem0SdkBackend(memory, owned_resources=(embedding, vector, embedding, memory))
    backend.close()
    backend.close()
    assert [r.closed for r in (memory, embedding, vector)] == [1, 1, 1]
    operations = [
        lambda: backend.add("text", user_id="u", agent_id="a", run_id="r", metadata={}, infer=False),
        lambda: backend.search("q", filters={}, top_k=1, threshold=0),
        lambda: backend.get_all(filters={}),
        lambda: backend.delete("id"),
        lambda: backend.delete_all(user_id="u", agent_id="a", run_id="r"),
    ]
    for operation in operations:
        with pytest.raises(Mem0IndexUnavailable, match="已关闭"):
            operation()


def test_sdk_close_attempts_remaining_resources_after_failure():
    class Resource:
        def __init__(self, fail=False):
            self.closed = 0
            self.fail = fail
        def close(self):
            self.closed += 1
            if self.fail:
                raise RuntimeError("synthetic failure")

    memory, first, second = Resource(), Resource(True), Resource()
    backend = Mem0SdkBackend(memory, owned_resources=(first, second))
    with pytest.raises(Mem0IndexUnavailable, match="资源关闭失败"):
        backend.close()
    backend.close()
    assert [r.closed for r in (memory, first, second)] == [1, 1, 1]
    with pytest.raises(Mem0IndexUnavailable, match="已关闭"):
        backend.get_all(filters={})


def test_sdk_close_waits_for_actual_active_call():
    import threading

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    close_started = threading.Event()
    closed = threading.Event()
    errors = []

    class Memory:
        def search(self, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            finished.set()
            return {"results": []}
        def close(self):
            if not finished.is_set():
                raise AssertionError("close before search returned")
            closed.set()

    backend = Mem0SdkBackend(Memory())

    def lookup():
        try:
            backend.search("q", filters={}, top_k=1, threshold=0)
        except Exception as exc:
            errors.append(exc)

    def close():
        close_started.set()
        try:
            backend.close()
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=lookup)
    closer = threading.Thread(target=close)
    reader.start()
    try:
        assert entered.wait(2)
        closer.start()
        assert close_started.wait(2)
        assert not closed.wait(0.1)
    finally:
        release.set()
        reader.join(5)
        if closer.ident is not None:
            closer.join(5)
    assert not reader.is_alive() and not closer.is_alive()
    assert not errors and closed.is_set()


def test_sdk_loader_requires_explicit_providers_before_any_import():
    with pytest.raises(Mem0IndexUnavailable):
        load_mem0_backend({})
    with pytest.raises(Mem0IndexUnavailable):
        load_mem0_backend({"vector_store": {}, "embedder": {}})


@pytest.mark.parametrize("timeout", [0, True, float("nan"), float("inf"), 61, "10"])
def test_sdk_loader_rejects_invalid_io_timeout_before_import(timeout):
    with pytest.raises(Mem0IndexUnavailable, match="超时"):
        load_mem0_backend({}, io_timeout_seconds=timeout)


def test_sdk_loader_refuses_unverified_provider_routes_before_import(tmp_path):
    config = {
        "vector_store": {"provider": "qdrant", "config": {"path": str(tmp_path)}},
        "embedder": {"provider": "openai", "config": {"model": "fixture", "api_key": "synthetic",
                                                       "openai_base_url": "http://127.0.0.1:1/v1"}},
        "llm": {"provider": "openai", "config": {"model": "fixture", "api_key": "synthetic",
                                                 "openai_base_url": "http://127.0.0.1:1/v1"}},
    }
    config["vector_store"]["config"]["url"] = "http://127.0.0.1:1"
    with pytest.raises(Mem0IndexUnavailable, match="本地"):
        load_mem0_backend(config)
    del config["vector_store"]["config"]["url"]
    config["embedder"]["provider"] = "other"
    with pytest.raises(Mem0IndexUnavailable, match="兼容供应商"):
        load_mem0_backend(config)


@pytest.fixture
def native_store(tmp_path, monkeypatch):
    from app.config import Settings
    from app.service import Service

    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="synthetic", runtime_type="harness"))
    user = service.create_user({"username": "owner", "display_name": "Owner",
                                "password": "synthetic-password-123", "role": "admin"})
    assistant = service.save_assistant({"name": "Mem0 index fixture"}, user=user)
    conversation_id = service.create_conversation(
        {"assistant_id": assistant["id"], "title": "Synthetic Mem0 index"}, user)["id"]
    try:
        yield service, user, conversation_id
    finally:
        service.db.close()


def test_native_reader_reads_verified_snapshot_and_detects_version_change(native_store):
    service, user, conversation_id = native_store
    initial_binding = service.memory.snapshot_for_run(user["id"], conversation_id)
    initial_scope = TrustedScope.from_binding(initial_binding)
    reader = NativeMemorySourceReader(service.memory)
    assert reader.load(initial_scope).documents == ()

    service.memory.edit(user, conversation_id, 0, "MEMORY.md", "approved inventory threshold 12", "native-op-1")
    binding = service.memory.snapshot_for_run(user["id"], conversation_id)
    scope = TrustedScope.from_binding(binding)
    loaded = reader.load(scope)
    assert [document.text for document in loaded.documents] == ["approved inventory threshold 12"]
    assert loaded.documents[0].source_id == "MEMORY.md"
    assert loaded.documents[0].content_sha256 == text_digest("approved inventory threshold 12")
    with pytest.raises(Mem0IndexSourceChanged):
        reader.load(initial_scope)


def test_end_to_end_fake_mem0_over_real_native_snapshot(native_store):
    service, user, conversation_id = native_store
    reader = NativeMemorySourceReader(service.memory)
    backend = FakeMem0()
    adapter = enabled_index(backend, reader)

    service.memory.edit(user, conversation_id, 0, "MEMORY.md", "approved inventory threshold 12", "native-op-1")
    first_scope = TrustedScope.from_binding(service.memory.snapshot_for_run(user["id"], conversation_id))
    assert adapter.sync(first_scope).added == 1
    first_hits = adapter.search("inventory threshold", scope=first_scope)
    assert [hit.text for hit in first_hits.hits] == ["approved inventory threshold 12"]

    service.memory.edit(user, conversation_id, 1, "MEMORY.md", "approved inventory threshold 99", "native-op-2")
    second_scope = TrustedScope.from_binding(service.memory.snapshot_for_run(user["id"], conversation_id))
    with pytest.raises(Mem0IndexSourceChanged):
        adapter.search("inventory threshold", scope=first_scope)
    assert adapter.sync(second_scope).deleted == 1
    second_hits = adapter.search("inventory threshold", scope=second_scope)
    assert [hit.text for hit in second_hits.hits] == ["approved inventory threshold 99"]
    assert {row["metadata"]["kaiwu_generation"] for row in backend.rows.values()} == {second_scope.generation}

def test_enumeration_bound_fails_closed_before_any_write():
    class Truncated(FakeMem0):
        def get_all(self, *, filters):
            return {"results": [{} for _ in range(201)]}
    backend=Truncated()
    adapter=enabled_index(backend,FakeReader([make_document()]))
    with pytest.raises(Mem0IndexUnavailable,match="安全上限"):
        adapter.sync(make_scope())
    assert not any(c[0] in {"add","delete","delete_all"} for c in backend.calls)
    class TruncatedSdk:
        def get_all(self, **kwargs):
            assert kwargs["top_k"]==201 and kwargs["show_expired"] is True
            return {"results":[{}]*201}
    with pytest.raises(Mem0IndexUnavailable,match="安全上限"):
        Mem0SdkBackend(TruncatedSdk()).get_all(filters={"user_id":"u"})


def test_duplicate_list_entry_does_not_delete_kept_row():
    class DuplicateList(FakeMem0):
        def get_all(self, *, filters):
            rows=super().get_all(filters=filters)["results"]
            return {"results": rows+rows}
    backend=DuplicateList()
    adapter=enabled_index(backend,FakeReader([make_document()]))
    scope=make_scope()
    adapter.sync(scope)
    result=adapter.sync(scope)
    assert result.unchanged==1 and result.added==0 and result.deleted==0
    assert len(backend.rows)==1
    assert len(adapter.search("fact",scope=scope).hits)==1


def test_host_scope_never_initializes_memory(native_store):
    service, user, cid = native_store
    reader = NativeMemorySourceReader(service.memory)
    before = service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,))
    with pytest.raises(Mem0IndexError):
        reader.current_scope(user["id"], cid)
    assert service.db.one("SELECT * FROM conversation_memory_scopes WHERE conversation_id=?", (cid,)) == before
    assert service.db.all("SELECT id FROM memory_operations WHERE conversation_id=?", (cid,)) == []


@pytest.mark.parametrize("origin", ["empty", "legacy_pending"])
def test_host_rejects_nonempty_migration_even_after_partial_edit(native_store, origin):
    service, user, cid = native_store
    old = service.settings.data_dir / "conversation-memory" / user["id"] / cid
    old.mkdir(parents=True, mode=0o700)
    path = old / "unreviewed.md"
    path.write_text("synthetic inherited content")
    path.chmod(0o600)
    service.db.execute("UPDATE conversation_memory_scopes SET origin=? WHERE conversation_id=?", (origin,cid))
    initial = service.memory.snapshot_for_run(user["id"], cid)
    service.memory.edit(user, cid, initial["revision"], "reviewed.md", "synthetic reviewed content", "native-edit")
    binding = service.memory.snapshot_for_run(user["id"], cid)
    backend = FakeMem0()
    reader = NativeMemorySourceReader(service.memory)
    with pytest.raises(Mem0IndexScopeInvalid):
        enabled_index(backend, reader).sync(TrustedScope.from_binding(binding))
    assert backend.calls == []


@pytest.mark.parametrize("change", ["owner", "disabled", "archived", "assistant", "unpublished", "record-owner", "manifest"])
def test_host_scope_revalidates_owner_permission_and_published_record(native_store, change):
    service, user, cid = native_store
    service.memory.edit(user, cid, 0, "MEMORY.md", "synthetic approved fact", "native-op")
    reader = NativeMemorySourceReader(service.memory)
    scope = reader.current_scope(user["id"], cid)
    backend = FakeMem0()
    adapter = enabled_index(backend, reader)
    assert adapter.sync(scope).added == 1
    count = len(backend.calls)
    if change == "owner":
        other = service.create_user({"username":"other","display_name":"Other","password":"synthetic-password-123","role":"admin"})
        scope = TrustedScope.from_binding({**scope.as_binding(), "user_id":other["id"]})
    elif change == "disabled":
        service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    elif change == "archived":
        service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (cid,))
    elif change == "assistant":
        service.db.execute("UPDATE assistants SET enabled=0 WHERE id=?", (service.require("conversations", cid)["assistant_id"],))
    elif change == "unpublished":
        service.db.execute("UPDATE memory_snapshot_records SET state='prepared' WHERE id=?", (scope.snapshot_id,))
    elif change == "record-owner":
        service.db.execute("UPDATE memory_snapshot_records SET user_id=? WHERE id=?", ("a"*32,scope.snapshot_id))
    else:
        service.db.execute("UPDATE memory_snapshot_records SET manifest_digest=? WHERE id=?", ("a"*64,scope.snapshot_id))
    with pytest.raises(Mem0IndexError):
        adapter.search("synthetic", scope=scope)
    assert len(backend.calls) == count


def test_host_scope_run_binding_and_uncommitted_proposal(native_store):
    from test_harness_memory_storage import make_run
    from app.db import dump
    service, user, cid = native_store
    service.memory.edit(user, cid, 0, "MEMORY.md", "synthetic approved fact", "native-op")
    rid, binding = make_run(native_store)
    reader = NativeMemorySourceReader(service.memory, run_id=rid)
    scope = reader.current_scope(user["id"], cid)
    backend = FakeMem0()
    adapter = enabled_index(backend, reader)
    assert adapter.sync(scope).added == 1
    with pytest.raises(Mem0IndexSourceChanged):
        NativeMemorySourceReader(service.memory).current_scope(user["id"], cid)
    service.db.execute("UPDATE runs SET execution_meta='{}' WHERE id=?", (rid,))
    with pytest.raises(Mem0IndexSourceChanged):
        adapter.search("synthetic", scope=scope)
    service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump({"memory_binding":binding}),rid))
    service.memory.stage_for_run(binding,rid,"proposal","MEMORY.md","unapproved replacement",binding["revision"])
    assert [h.text for h in adapter.search("synthetic",scope=scope).hits] == ["synthetic approved fact"]
    service.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (rid,))
    with pytest.raises(Mem0IndexSourceChanged):
        adapter.search("synthetic",scope=scope)
    assert all("unapproved replacement" not in str(call) for call in backend.calls)


def test_host_scope_accepts_only_after_approved_model_commit(native_store):
    from test_harness_memory_storage import make_run, approve, finish
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic original","native-op")
    rid,binding = make_run(native_store)
    reader = NativeMemorySourceReader(service.memory,run_id=rid)
    backend = FakeMem0()
    adapter = enabled_index(backend,reader)
    scope = reader.current_scope(user["id"],cid)
    assert adapter.sync(scope).added == 1
    op = service.memory.stage_for_run(binding,rid,"proposal","MEMORY.md","synthetic approved replacement",binding["revision"])
    approve(native_store,op)
    assert [h.text for h in adapter.search("synthetic",scope=scope).hits] == ["synthetic original"]
    service.memory.snapshots.commit(rid,authorize=lambda: True,finalize=finish(service,cid,rid))
    with pytest.raises(Mem0IndexError):
        adapter.search("synthetic",scope=scope)
    reader = NativeMemorySourceReader(service.memory)
    adapter = enabled_index(backend,reader)
    current = reader.current_scope(user["id"],cid)
    assert current.revision == scope.revision+1
    assert adapter.sync(current).added == 1
    assert [h.text for h in adapter.search("synthetic",scope=current).hits] == ["synthetic approved replacement"]


def test_host_scope_denies_hits_when_authorization_changes_during_backend_search(native_store):
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic approved fact","native-op")
    reader = NativeMemorySourceReader(service.memory)
    scope = reader.current_scope(user["id"],cid)
    class RevokingBackend(FakeMem0):
        def search(self, query, *, filters, top_k, threshold):
            result = super().search(query,filters=filters,top_k=top_k,threshold=threshold)
            service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
            return result
    adapter = enabled_index(RevokingBackend(),reader)
    assert adapter.sync(scope).added == 1
    with pytest.raises(Mem0IndexScopeInvalid):
        adapter.search("synthetic",scope=scope)


def test_mem0_host_default_disabled_and_explicit_backend(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    backend = FakeMem0()
    host = Mem0Host(service, backend=backend)
    assert host.lookup(user["id"], cid, "fact").status == "fallback"
    with pytest.raises(Mem0IndexDisabled):
        host.sync(user["id"], cid)
    assert backend.calls == []
    assert not (service.settings.data_dir / ".mem0-index.lock").exists()
    with pytest.raises(ValueError):
        Mem0Host(service, Mem0IndexConfig(enabled=True))


def test_mem0_host_explicit_sync_and_lookup_no_implicit_writes(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic fact","host-op")
    backend = FakeMem0()
    host = Mem0Host(service, Mem0IndexConfig(enabled=True), backend=backend)
    assert host.lookup(user["id"],cid,"fact").status == "verified"
    assert [call[0] for call in backend.calls] == ["search"]
    assert host.sync(user["id"],cid).added == 1
    backend.calls.clear()
    result = host.lookup(user["id"],cid,"fact")
    assert result.status == "verified"
    assert [hit.text for hit in result.hits] == ["synthetic fact"]
    assert [call[0] for call in backend.calls] == ["search"]


def test_mem0_host_provider_failure_and_byte_budget_fall_back(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","合成内容","host-op")
    backend = FakeMem0()
    host = Mem0Host(service, Mem0IndexConfig(enabled=True), backend=backend,max_context_bytes=11)
    assert host.sync(user["id"],cid).added == 1
    result = host.lookup(user["id"],cid,"fact")
    assert result.status == "fallback" and result.reason == "context_byte_budget" and not result.hits
    backend.failures.add("search")
    result = host.lookup(user["id"],cid,"fact")
    assert result.status == "fallback" and result.reason == "Mem0IndexUnavailable" and not result.hits
    backend.failures.clear()
    host = Mem0Host(service, Mem0IndexConfig(enabled=True), backend=backend,max_context_bytes=12)
    assert host.lookup(user["id"],cid,"fact").hits[0].text == "合成内容"


def test_mem0_host_single_slot_across_instances_and_release(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic fact","host-op")
    backend = FakeMem0()
    one = Mem0Host(service, Mem0IndexConfig(enabled=True),backend=backend)
    two = Mem0Host(service, Mem0IndexConfig(enabled=True),backend=backend)
    with one._slot():
        assert two.lookup(user["id"],cid,"fact").status == "fallback"
        with pytest.raises(Mem0IndexUnavailable):
            two.sync(user["id"],cid)
        assert backend.calls == []
    assert two.sync(user["id"],cid).added == 1
    assert one.lookup(user["id"],cid,"fact").hits[0].text == "synthetic fact"


def test_mem0_host_rejects_unsafe_lock_path(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    backend = FakeMem0()
    target = service.settings.data_dir / "synthetic-target"
    target.write_text("unchanged")
    (service.settings.data_dir / ".mem0-index.lock").symlink_to(target)
    host = Mem0Host(service, Mem0IndexConfig(enabled=True),backend=backend)
    assert host.lookup(user["id"],cid,"fact").status == "fallback"
    assert target.read_text() == "unchanged" and backend.calls == []


def test_mem0_host_rejects_revoked_scope_without_backend_access(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic fact","host-op")
    backend = FakeMem0()
    host = Mem0Host(service, Mem0IndexConfig(enabled=True),backend=backend)
    assert host.sync(user["id"],cid).added == 1
    backend.calls.clear()
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
    assert host.lookup(user["id"],cid,"fact").status == "fallback"
    with pytest.raises(Mem0IndexError):
        host.sync(user["id"],cid)
    assert backend.calls == []


def test_mem0_host_real_process_lock_exclusion(native_store):
    import subprocess
    import sys
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    host = Mem0Host(service, Mem0IndexConfig(enabled=True), backend=FakeMem0())
    path = service.settings.data_dir / ".mem0-index.lock"
    child = (
        "import fcntl, os, sys\n"
        "fd=os.open(sys.argv[1], os.O_RDWR | os.O_NOFOLLOW)\n"
        "try:\n"
        "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n"
        "    sys.exit(23)\n"
        "finally:\n"
        "    os.close(fd)\n"
    )
    with host._slot():
        assert subprocess.run([sys.executable,"-c",child,str(path)],timeout=5).returncode == 23
    assert subprocess.run([sys.executable,"-c",child,str(path)],timeout=5).returncode == 0

def test_mem0_host_sync_revocation_stops_later_submissions(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic first","host-op1")
    service.memory.edit(user,cid,1,"notes.md","synthetic second","host-op2")
    class RevokingBackend(FakeMem0):
        def add(self, *args, **kwargs):
            result = super().add(*args, **kwargs)
            service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (user["id"],))
            return result
    backend = RevokingBackend()
    host = Mem0Host(service, Mem0IndexConfig(enabled=True), backend=backend)
    with pytest.raises(Mem0IndexScopeInvalid):
        host.sync(user["id"],cid)
    assert len(add_calls(backend)) == 1
    assert not backend.rows
    assert host.lookup(user["id"],cid,"fact").status == "fallback"


def test_mem0_host_sync_revocation_after_enumeration_sends_no_text(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic fact","host-op")
    class RevokingBackend(FakeMem0):
        def get_all(self, **kwargs):
            result = super().get_all(**kwargs)
            service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (cid,))
            return result
    backend = RevokingBackend()
    host = Mem0Host(service, Mem0IndexConfig(enabled=True), backend=backend)
    with pytest.raises(Mem0IndexScopeInvalid):
        host.sync(user["id"],cid)
    assert len(add_calls(backend)) == 0
    assert not backend.rows


def test_sync_failed_response_rolls_back_prior_acknowledged_writes():
    class MalformedBackend(FakeMem0):
        def add(self, *args, **kwargs):
            if add_calls(self):
                return {"invalid": True}
            return super().add(*args, **kwargs)
    backend = MalformedBackend()
    reader = FakeReader([make_document("a.md"), make_document("b.md")])
    adapter = enabled_index(backend,reader)
    with pytest.raises(Mem0IndexUnavailable):
        adapter.sync(make_scope())
    assert not backend.rows


def test_host_clear_requires_current_committed_receipt_and_preserves_other_scope(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic indexed fact","edit-before-clear")
    backend = FakeMem0()
    host = Mem0Host(service,Mem0IndexConfig(enabled=True),backend=backend)
    assert host.sync(user["id"],cid).added == 1
    other = backend.insert("other scope",{},user_id="other",agent_id=INDEX_AGENT_ID,run_id=cid)["id"]
    backend.calls.clear()
    with pytest.raises(Mem0IndexScopeInvalid):
        host.clear_committed(user["id"],cid,"edit-before-clear")
    assert backend.calls == []
    service.memory.edit(user,cid,1,operation_id="clear-current")
    with pytest.raises(Mem0IndexScopeInvalid):
        host.clear_committed(user["id"],cid,"missing-clear")
    assert backend.calls == []
    assert host.clear_committed(user["id"],cid,"clear-current").deleted == 1
    assert set(backend.rows) == {other}
    assert all(call[0] in {"get_all","delete"} for call in backend.calls)
    service.memory.edit(user,cid,2,operation_id="clear-again")
    backend.calls.clear()
    with pytest.raises(Mem0IndexScopeInvalid):
        host.clear_committed(user["id"],cid,"clear-current")
    assert backend.calls == []
    assert host.clear_committed(user["id"],cid,"clear-again").status == "unchanged"


def test_host_clear_never_reports_unverified_delete_as_complete(native_store):
    from app.mem0_host import Mem0Host
    service, user, cid = native_store
    service.memory.edit(user,cid,0,"MEMORY.md","synthetic indexed fact","edit-before-clear")
    class LostDelete(FakeMem0):
        def delete(self, memory_id):
            self.calls.append(("delete",memory_id))
            return {"message":"synthetic false acknowledgment"}
    backend = LostDelete()
    host = Mem0Host(service,Mem0IndexConfig(enabled=True),backend=backend)
    host.sync(user["id"],cid)
    service.memory.edit(user,cid,1,operation_id="clear-current")
    with pytest.raises(Mem0IndexUnavailable,match="未确认完成"):
        host.clear_committed(user["id"],cid,"clear-current")
    assert len(backend.rows) == 1
    assert sum(call[0] == "delete" for call in backend.calls) == 1
    backend.calls.clear()
    service.db.execute("UPDATE users SET enabled=0 WHERE id=?",(user["id"],))
    with pytest.raises(Mem0IndexScopeInvalid):
        host.clear_committed(user["id"],cid,"clear-current")
    assert backend.calls == []
