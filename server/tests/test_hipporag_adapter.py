"""Source and cache contracts using an explicitly injected engine double.

These tests do not claim official retrieval quality or invoke a model.
"""
import copy
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.hipporag import HippoRAGAdapter, HippoRAGError, PINNED_COMMIT


IDENTITY = {"upstream_commit": PINNED_COMMIT, **{
    key: char * 64 for key, char in zip(
        ("parser_digest", "extraction_digest", "embedding_digest", "chunking_digest", "acl_digest"), "abcde")}}


@dataclass
class Chunk:
    content: str
    source_id: str
    metadata: dict


def row(identifier="a", text="Pump P-1 feeds valve V-2.", **changes):
    return {"id": identifier, "document_id": f"doc-{identifier}", "kb_id": "kb-1", "version": 1,
            "sha256": "f" * 64, "text": text, "name": "manual.md", "page": 1,
            "paragraph": 1, "start": 0, "end": len(text), "section": "Equipment", **changes}


class Engine:
    def __init__(self, *, save_dir):
        self.directory = Path(save_dir)
        self.chunks = []
        self.calls = []
        self.closed = False
        self.change_result = lambda result: result

    def index(self, chunks):
        assert self.directory.is_dir()
        assert not (self.directory.parent / "manifest.json").exists()
        self.chunks = chunks
        (self.directory / "own-cache.bin").write_bytes(b"synthetic-not-a-pickle")

    def retrieve(self, queries, num_to_retrieve):
        self.calls.append((queries, num_to_retrieve))
        chunks = sorted(self.chunks, key=lambda chunk: chunk.content)[:num_to_retrieve]
        result = SimpleNamespace(question=queries[0], docs=[chunk.content for chunk in chunks],
            doc_scores=[0.5 - index for index, _ in enumerate(chunks)],
            doc_metadata=[{**chunk.metadata, "source_id": chunk.source_id} for chunk in chunks],
            graph_seeds=[("not-verified-entity", "not-verified-relation", "not-verified-target")])
        return self.change_result([result])

    def close(self):
        self.closed = True


@pytest.fixture
def prepared(tmp_path):
    engines = []

    def factory(**kwargs):
        engine = Engine(**kwargs)
        engines.append(engine)
        return engine

    adapter = HippoRAGAdapter(engine_factory=factory, chunk_factory=Chunk)
    rows = [row(), row("b", "Valve V-2 is monitored by sensor S-3.")]
    directory = tmp_path / "index"
    yield SimpleNamespace(adapter=adapter, rows=rows, directory=directory, engines=engines, factory=factory)
    adapter.close()


def build(env):
    return env.adapter.build(env.rows, identity=IDENTITY, index_dir=env.directory)


def failure(code, action):
    with pytest.raises(HippoRAGError) as caught:
        action()
    assert caught.value.code == code
    return caught.value


def test_default_does_not_load_or_call_an_engine(tmp_path):
    adapter = HippoRAGAdapter()
    failure("unavailable", lambda: adapter.build([row()], identity=IDENTITY, index_dir=tmp_path / "none"))
    failure("index_not_ready", lambda: adapter.search("pump", rows=[row()]))
    assert not list(tmp_path.iterdir())


def test_build_and_search_preserve_native_ranking_negative_scores_and_local_ids(prepared):
    env = prepared
    stats = build(env)
    scores, trace = env.adapter.search("pump", rows=env.rows, limit=2)
    assert list(scores) == ["a", "b"] and scores == {"a": 0.5, "b": -0.5}
    assert trace == {**stats, "returned_count": 2, "graph_seed_count": 1}
    assert trace["algorithm"] == "hipporag2-official"
    assert "not-verified" not in json.dumps(trace)
    assert env.engines[0].calls == [(["pump"], 2)]
    manifest = json.loads((env.directory / "manifest.json").read_text())
    assert manifest["state"] == "ready" and manifest["identity"] == IDENTITY
    assert env.rows[0]["text"] not in (env.directory / "manifest.json").read_text()


def test_same_text_keeps_all_sources_without_official_metadata_overwrite(prepared):
    env = prepared
    env.rows = [row("c"), row("a"), row("b", "Unrelated text.")]
    stats = build(env)
    assert stats["source_count"] == 3 and stats["unique_text_count"] == 2
    assert len(env.engines[0].chunks) == 2
    scores, _ = env.adapter.search("pump", rows=env.rows, limit=3)
    assert list(scores) == ["a", "c", "b"]
    assert scores["a"] == scores["c"] == 0.5
    limited, _ = env.adapter.search("pump", rows=env.rows, limit=1)
    assert limited == {"a": 0.5}


def test_row_order_and_non_source_ranking_fields_do_not_change_snapshot(prepared):
    env = prepared
    build(env)
    shuffled = [{**item, "terms": "new tokens", "vector": "[]", "lexical_score": 99} for item in reversed(env.rows)]
    assert env.adapter.search("pump", rows=shuffled)[0] == {"a": 0.5, "b": -0.5}


@pytest.mark.parametrize("field,value", [
    ("id", "changed"), ("kb_id", "different-kb"), ("document_id", "different-doc"),
    ("version", 2), ("sha256", "0" * 64), ("text", "Different text."),
    ("page", 2), ("paragraph", 2), ("start", 1), ("end", 88),
    ("name", "other.md"), ("section", "Other"), ("imported_at", "different"), ("knowledge_base", "Other"),
])
def test_any_source_identity_change_is_rejected_before_retrieval(prepared, field, value):
    env = prepared
    build(env)
    changed = copy.deepcopy(env.rows)
    changed[0][field] = value
    failure("identity_mismatch", lambda: env.adapter.search("pump", rows=changed))
    assert not env.engines[0].calls


@pytest.mark.parametrize("change", ["add", "remove", "model"])
def test_source_set_and_model_changes_do_not_reuse_index(prepared, change):
    env = prepared
    build(env)
    rows, identity = env.rows[:], dict(IDENTITY)
    if change == "add":
        rows.append(row("c", "New document."))
    elif change == "remove":
        rows.pop()
    else:
        identity["embedding_digest"] = "0" * 64
    failure("identity_mismatch", lambda: env.adapter.search("pump", rows=rows, identity=identity))
    assert not env.engines[0].calls


@pytest.mark.parametrize("kind", ["empty", "cache", "symlink", "ancestor_symlink"])
def test_unknown_existing_directories_never_reach_factory(prepared, kind):
    env = prepared
    if kind == "symlink":
        env.directory.symlink_to(env.directory.parent, target_is_directory=True)
    elif kind == "ancestor_symlink":
        linked = env.directory.parent / "link"
        linked.symlink_to(env.directory.parent, target_is_directory=True)
        env.directory = linked / "new-index"
    else:
        env.directory.mkdir()
        if kind == "cache":
            (env.directory / "untrusted.pkl").write_bytes(b"never deserialize")
    failure("index_untrusted", lambda: build(env))
    assert not env.engines


def test_another_adapter_cannot_reopen_even_an_own_valid_manifest(prepared):
    env = prepared
    build(env)
    another = HippoRAGAdapter(engine_factory=env.factory, chunk_factory=Chunk)
    failure("index_untrusted", lambda: another.build(env.rows, identity=IDENTITY, index_dir=env.directory))
    assert len(env.engines) == 1
    failure("index_untrusted", lambda: build(env))


@pytest.mark.parametrize("change", ["edit", "delete", "symlink", "replace_directory", "ancestor_symlink"])
def test_manifest_and_directory_mutations_are_rejected(prepared, change):
    env = prepared
    build(env)
    manifest = env.directory / "manifest.json"
    if change == "edit":
        manifest.write_text('{"state":"ready"}')
    elif change == "delete":
        manifest.unlink()
    elif change == "symlink":
        original = env.directory / "original.json"
        manifest.rename(original)
        manifest.symlink_to(original)
    elif change == "replace_directory":
        old = env.directory.with_name("old")
        env.directory.rename(old)
        env.directory.mkdir()
        (env.directory / "manifest.json").write_bytes((old / "manifest.json").read_bytes())
    else:
        original = env.directory.parent
        moved = original.with_name(original.name + "-moved")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
    failure("index_untrusted", lambda: env.adapter.search("pump", rows=env.rows))
    assert not env.engines[0].calls


@pytest.mark.parametrize("fault", [
    "unknown_group", "extra_metadata", "wrong_source", "wrong_snapshot", "changed_text", "missing_metadata",
    "missing_score", "duplicate_doc", "extra_query", "wrong_question", "raw_tuple", "too_many_docs",
    "nan", "inf", "bool", "string", "huge", "malformed_seeds",
])
def test_unbound_or_malformed_official_results_are_rejected_as_a_whole(prepared, fault):
    env = prepared
    build(env)

    def mutate(results):
        result = results[0]
        if fault == "unknown_group":
            result.doc_metadata[0]["adapter_group"] = "unknown"
        elif fault == "extra_metadata":
            result.doc_metadata[0]["url"] = "https://untrusted.invalid"
        elif fault == "wrong_source":
            result.doc_metadata[0]["source_id"] = "foreign"
        elif fault == "wrong_snapshot":
            result.doc_metadata[0]["adapter_snapshot"] = "0" * 64
        elif fault == "changed_text":
            result.docs[0] += " injected"
        elif fault == "missing_metadata":
            result.doc_metadata.pop()
        elif fault == "missing_score":
            result.doc_scores.pop()
        elif fault in {"duplicate_doc", "too_many_docs"}:
            for key in ("docs", "doc_scores", "doc_metadata"):
                items = getattr(result, key)
                items.append(items[0]) if fault == "too_many_docs" else items.__setitem__(1, items[0])
        elif fault == "extra_query":
            return results * 2
        elif fault == "wrong_question":
            result.question = "different query"
        elif fault == "raw_tuple":
            return results, {"metrics": "unexpected evaluation output"}
        elif fault == "malformed_seeds":
            result.graph_seeds = "raw text is not a seed list"
        else:
            result.doc_scores[0] = {"nan": float("nan"), "inf": float("inf"), "bool": True,
                                    "string": "0.5", "huge": 1e100}[fault]
        return results

    env.engines[0].change_result = mutate
    failure("result_invalid", lambda: env.adapter.search("pump", rows=env.rows))


@pytest.mark.parametrize("field,value", [
    ("text", ""), ("text", "\ud800"), ("text", "a" * 65537), ("version", True),
    ("version", 0), ("page", False), ("page", -1), ("sha256", "bad"),
    ("name", "\ud800"), ("id", ""), ("end", -1),
])
def test_bad_rows_fail_before_directory_or_factory(prepared, field, value):
    env = prepared
    env.rows[0][field] = value
    failure("invalid_input", lambda: build(env))
    assert not env.directory.exists() and not env.engines


@pytest.mark.parametrize("query,limit", [("", 1), ("x" * 2001, 1), ("\ud800", 1), ("pump", True), ("pump", 0), ("pump", 101)])
def test_query_bounds_are_checked_before_model(prepared, query, limit):
    env = prepared
    build(env)
    failure("invalid_input", lambda: env.adapter.search(query, rows=env.rows, limit=limit))
    assert not env.engines[0].calls


def test_duplicate_chunk_ids_and_unbounded_snapshot_fail_closed(prepared):
    env = prepared
    env.rows.append(copy.deepcopy(env.rows[0]))
    failure("invalid_input", lambda: build(env))
    env.rows = [row(str(index)) for index in range(5001)]
    failure("invalid_input", lambda: build(env))
    assert not env.engines


@pytest.mark.parametrize("change", ["missing", "extra", "wrong_commit", "invalid_digest"])
def test_identity_is_explicit_bounded_and_credential_free(prepared, change):
    env = prepared
    identity = dict(IDENTITY)
    if change == "missing":
        identity.pop("acl_digest")
    elif change == "extra":
        identity["api_key"] = "synthetic-must-not-persist"
    elif change == "wrong_commit":
        identity["upstream_commit"] = "0" * 40
    else:
        identity["embedding_digest"] = "some model name"
    failure("invalid_input", lambda: env.adapter.build(env.rows, identity=identity, index_dir=env.directory))
    assert not env.directory.exists() and not env.engines


def test_build_failure_closes_engine_without_publishing_ready_manifest(prepared):
    env = prepared

    def broken_factory(**kwargs):
        engine = env.factory(**kwargs)

        def broken_index(_chunks):
            raise RuntimeError("synthetic-secret-error-never-return")

        engine.index = broken_index
        return engine

    env.adapter.engine_factory = broken_factory
    error = failure("index_failed", lambda: build(env))
    assert "synthetic-secret" not in str(error)
    assert env.engines[0].closed and not (env.directory / "manifest.json").exists()
    failure("index_not_ready", lambda: env.adapter.search("pump", rows=env.rows))
    failure("index_untrusted", lambda: build(env))


def test_retrieval_error_is_sanitized_and_close_is_idempotent(prepared):
    env = prepared
    build(env)

    def broken_retrieval(*args, **kwargs):
        raise RuntimeError("synthetic-private-remote-response")

    env.engines[0].retrieve = broken_retrieval
    error = failure("retrieval_failed", lambda: env.adapter.search("pump", rows=env.rows))
    assert "synthetic-private" not in str(error)
    env.adapter.close()
    env.adapter.close()
    assert env.engines[0].closed
    failure("index_not_ready", lambda: env.adapter.search("pump", rows=env.rows))


def test_snapshot_and_manifest_are_rechecked_after_retrieval(prepared):
    env = prepared
    build(env)

    def changed(results):
        env.rows[0]["version"] += 1
        return results

    env.engines[0].change_result = changed
    failure("identity_mismatch", lambda: env.adapter.search("pump", rows=env.rows))


def test_concurrent_retrieval_and_close_are_serialized(prepared):
    env = prepared
    build(env)
    entered, release, close_finished = threading.Event(), threading.Event(), threading.Event()
    original = env.engines[0].retrieve
    outcomes = []

    def waiting(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        assert not env.engines[0].closed
        return original(*args, **kwargs)

    def close():
        env.adapter.close()
        close_finished.set()

    env.engines[0].retrieve = waiting
    search = threading.Thread(target=lambda: outcomes.append(env.adapter.search("pump", rows=env.rows)))
    search.start()
    assert entered.wait(3)
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not close_finished.wait(0.05)
    finally:
        release.set()
        search.join(3)
        closer.join(3)
    assert not search.is_alive() and not closer.is_alive()
    assert close_finished.is_set() and outcomes[0][0] == {"a": 0.5, "b": -0.5}


def test_ready_manifest_is_published_once_after_index_completes(prepared, monkeypatch):
    import app.hipporag as module

    env = prepared
    original, publications = module.os.replace, []

    def inspect(source, target):
        assert not target.exists()
        assert env.engines[0].chunks
        manifest = json.loads(source.read_bytes())
        assert manifest["state"] == "ready"
        publications.append((source, target))
        return original(source, target)

    monkeypatch.setattr(module.os, "replace", inspect)
    build(env)
    assert len(publications) == 1
    assert not publications[0][0].exists() and publications[0][1].is_file()
    assert (publications[0][1].stat().st_mode & 0o777) == 0o600
    assert (env.directory.stat().st_mode & 0o777) == 0o700


def test_failed_publication_cannot_leave_a_queryable_adapter(prepared, monkeypatch):
    import app.hipporag as module

    env = prepared

    def denied(*_args):
        raise OSError("synthetic-private-filesystem-error")

    monkeypatch.setattr(module.os, "replace", denied)
    failure("index_failed", lambda: build(env))
    assert env.engines[0].closed and not (env.directory / "manifest.json").exists()
    failure("index_not_ready", lambda: env.adapter.search("pump", rows=env.rows))


def test_source_mutation_during_build_does_not_publish_a_stale_index(prepared):
    env = prepared

    def factory(**kwargs):
        engine = env.factory(**kwargs)
        original = engine.index

        def changed(chunks):
            original(chunks)
            env.rows[0]["page"] += 1

        engine.index = changed
        return engine

    env.adapter.engine_factory = factory
    failure("index_failed", lambda: build(env))
    assert env.engines[0].closed and not (env.directory / "manifest.json").exists()


def test_manifest_is_rechecked_after_model_returns(prepared):
    env = prepared
    build(env)

    def changed(results):
        (env.directory / "manifest.json").write_text("changed while awaiting retrieval")
        return results

    env.engines[0].change_result = changed
    failure("index_untrusted", lambda: env.adapter.search("pump", rows=env.rows))


@pytest.mark.parametrize("change", ["unknown_source_field", "contradictory_document", "aggregate_limit"])
def test_sources_cannot_silently_expand_or_disagree(prepared, monkeypatch, change):
    env = prepared
    if change == "unknown_source_field":
        env.rows[0]["source_url"] = "https://foreign.invalid/evidence"
    elif change == "contradictory_document":
        env.rows[1]["document_id"] = env.rows[0]["document_id"]
        env.rows[1]["sha256"] = "0" * 64
    else:
        monkeypatch.setattr("app.hipporag.MAX_SNAPSHOT_BYTES", 30)
    failure("invalid_input", lambda: build(env))
    assert not env.engines and not env.directory.exists()


def test_returned_statistics_cannot_mutate_bound_identity(prepared):
    env = prepared
    stats = build(env)
    stats["identity_digest"] = "not the bound index"
    _, actual = env.adapter.search("pump", rows=env.rows)
    assert actual["identity_digest"] != stats["identity_digest"]


class PersistedEngine(Engine):
    """Flush on close and reopen JSON only; never deserialize a test pickle."""
    def __init__(self, *, save_dir):
        super().__init__(save_dir=save_dir)
        state = self.directory / "chunks.json"
        if state.exists():
            self.chunks = [Chunk(**item) for item in json.loads(state.read_text())]

    def close(self):
        (self.directory / "chunks.json").write_text(json.dumps([chunk.__dict__ for chunk in self.chunks]))
        super().close()


@pytest.fixture
def recoverable(tmp_path):
    engines = []

    def factory(**kwargs):
        engine = PersistedEngine(**kwargs)
        engines.append(engine)
        return engine

    adapter = HippoRAGAdapter(engine_factory=factory, chunk_factory=Chunk)
    rows = [row(), row("duplicate"), row("b", "Valve V-2 is monitored by sensor S-3.")]
    directory = tmp_path / "source"
    adapter.build(rows, identity=IDENTITY, index_dir=directory)
    yield SimpleNamespace(adapter=adapter, engines=engines, factory=factory, rows=rows,
                          directory=directory, destination=tmp_path / "recovered")
    adapter.close()


def restore(env, receipt, *, adapter=None, **overrides):
    adapter = adapter or HippoRAGAdapter(engine_factory=env.factory, chunk_factory=Chunk)
    options = {"identity": IDENTITY, "source_dir": env.directory, "receipt": receipt,
               "index_dir": env.destination, **overrides}
    adapter.restore(env.rows, **options)
    return adapter


def tree_contents(directory):
    return {str(path.relative_to(directory)): (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in directory.rglob("*") if path.is_file()}


def test_seal_flushes_closes_and_returns_a_small_externally_held_receipt(recoverable):
    env = recoverable
    assert not (env.directory / "upstream/chunks.json").exists()
    receipt = env.adapter.seal()
    assert env.engines[0].closed
    assert (env.directory / "upstream/chunks.json").is_file()
    assert receipt["source_dir"] == str(env.directory)
    assert len(json.dumps(receipt).encode()) < 8192
    seal = (env.directory / "seal.json").read_bytes()
    assert hashlib.sha256(seal).hexdigest() == receipt["tree_digest"]
    inventory = json.loads(seal)
    paths = {entry["path"] for entry in inventory["entries"]}
    assert paths == {".", "upstream", "manifest.json", "upstream/chunks.json", "upstream/own-cache.bin"}
    assert receipt["file_count"] == 4
    assert receipt["total_bytes"] == sum(len(data) for data, _ in tree_contents(env.directory).values())
    assert all(mode == 0o600 for _, mode in tree_contents(env.directory).values())
    assert all((path.stat().st_mode & 0o777) == 0o700 for path in env.directory.rglob("*") if path.is_dir())
    failure("index_not_ready", lambda: env.adapter.search("pump", rows=env.rows))
    failure("index_not_ready", env.adapter.seal)


def test_receipt_restore_copies_then_queries_without_reindex_or_source_changes(recoverable):
    env = recoverable
    before_scores, _ = env.adapter.search("pump", rows=env.rows)
    receipt = json.loads(json.dumps(env.adapter.seal()))
    before_source = tree_contents(env.directory)
    recovered = restore(env, receipt)
    try:
        assert len(env.engines) == 2
        assert env.engines[-1].directory == env.destination / "upstream"
        assert recovered.search("pump", rows=env.rows)[0] == before_scores
        assert tree_contents(env.directory) == before_source
        source_file = env.directory / "upstream/chunks.json"
        restored_file = env.destination / "upstream/chunks.json"
        assert source_file.stat().st_ino != restored_file.stat().st_ino
        assert source_file.stat().st_nlink == restored_file.stat().st_nlink == 1
    finally:
        recovered.close()
    assert tree_contents(env.directory) == before_source


def test_restored_copy_can_be_resealed_with_a_new_path_bound_receipt(recoverable):
    env = recoverable
    first = env.adapter.seal()
    source = tree_contents(env.directory)
    restored = restore(env, first)
    second = restored.seal()
    assert second["source_dir"] == str(env.destination)
    assert second["identity_digest"] == first["identity_digest"]
    assert tree_contents(env.directory) == source
    third = HippoRAGAdapter(engine_factory=env.factory, chunk_factory=Chunk)
    try:
        third.restore(env.rows, identity=IDENTITY, source_dir=env.destination, receipt=second,
                      index_dir=env.destination.with_name("third"))
        assert set(third.search("pump", rows=env.rows)[0]) == {"a", "duplicate", "b"}
    finally:
        third.close()


@pytest.mark.parametrize("mutation", [
    "file_content", "file_deleted", "extra_file", "extra_directory", "file_renamed", "manifest",
    "seal_content", "seal_deleted", "permission_file", "permission_directory", "permission_root",
    "permission_seal", "symlink_file", "symlink_directory", "symlink_seal", "hardlink", "fifo",
])
def test_any_untrusted_recovery_tree_is_rejected_before_factory(recoverable, mutation):
    env = recoverable
    receipt = env.adapter.seal()
    cache = env.directory / "upstream/own-cache.bin"
    if mutation == "file_content":
        cache.write_bytes(b"malicious-pickle-must-never-load")
    elif mutation == "file_deleted":
        cache.unlink()
    elif mutation == "extra_file":
        (env.directory / "surprise.pkl").write_bytes(b"unknown")
    elif mutation == "extra_directory":
        (env.directory / "surprise").mkdir(mode=0o700)
    elif mutation == "file_renamed":
        cache.rename(cache.with_name("different.bin"))
    elif mutation == "manifest":
        (env.directory / "manifest.json").write_text('{"state":"ready"}')
    elif mutation == "seal_content":
        (env.directory / "seal.json").write_text('{"trusted":true}')
    elif mutation == "seal_deleted":
        (env.directory / "seal.json").unlink()
    elif mutation.startswith("permission_"):
        target = {"permission_file": cache, "permission_directory": cache.parent,
                  "permission_root": env.directory, "permission_seal": env.directory / "seal.json"}[mutation]
        target.chmod(0o644 if target.is_file() else 0o755)
    elif mutation == "symlink_file":
        cache.unlink()
        cache.symlink_to(env.directory / "manifest.json")
    elif mutation == "symlink_directory":
        other = cache.parent.with_name("old-upstream")
        cache.parent.rename(other)
        cache.parent.symlink_to(other, target_is_directory=True)
    elif mutation == "symlink_seal":
        original = env.directory / "seal.json"
        moved = original.with_name("original-seal.json")
        original.rename(moved)
        original.symlink_to(moved)
    elif mutation == "hardlink":
        os.link(cache, env.directory.parent / "external-hardlink")
    else:
        os.mkfifo(env.directory / "pipe")
    failure("index_untrusted", lambda: restore(env, receipt))
    assert len(env.engines) == 1


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong_schema", "owner", "digest", "file_count", "total_bytes", "bool"])
def test_receipt_is_strict_and_cannot_be_replaced_by_directory_claims(recoverable, mutation):
    env = recoverable
    receipt = env.adapter.seal()
    if mutation == "missing":
        receipt.pop("tree_digest")
    elif mutation == "extra":
        receipt["trusted"] = True
    elif mutation == "wrong_schema":
        receipt["schema"] = "arbitrary"
    elif mutation == "owner":
        receipt["owner_uid"] += 1
    elif mutation == "digest":
        receipt["tree_digest"] = "0" * 64
    elif mutation == "file_count":
        receipt["file_count"] += 1
    elif mutation == "total_bytes":
        receipt["total_bytes"] += 1
    else:
        receipt["file_count"] = True
    failure("index_untrusted", lambda: restore(env, receipt))
    assert len(env.engines) == 1


@pytest.mark.parametrize("mutation", ["rows", "identity", "snapshot_digest", "identity_digest"])
def test_recovery_cannot_change_the_authorized_snapshot_or_model(recoverable, mutation):
    env = recoverable
    receipt = env.adapter.seal()
    identity = dict(IDENTITY)
    if mutation == "rows":
        env.rows[0]["version"] = 2
    elif mutation == "identity":
        identity["embedding_digest"] = "0" * 64
    else:
        receipt[mutation] = "0" * 64
    failure("identity_mismatch", lambda: restore(env, receipt, identity=identity))
    assert len(env.engines) == 1


@pytest.mark.parametrize("mutation", ["moved_source", "ancestor_link", "existing_destination", "same_destination", "nested_destination"])
def test_recovery_paths_are_explicit_and_cannot_mutate_the_source(recoverable, mutation):
    env = recoverable
    receipt = env.adapter.seal()
    source, destination = env.directory, env.destination
    if mutation == "moved_source":
        source = env.directory.with_name("moved-source")
        env.directory.rename(source)
    elif mutation == "ancestor_link":
        link = env.directory.parent / "alias"
        link.symlink_to(env.directory.parent, target_is_directory=True)
        source = link / env.directory.name
        receipt["source_dir"] = str(source)
    elif mutation == "existing_destination":
        destination.mkdir()
    elif mutation == "same_destination":
        destination = source
    else:
        destination = source / "child"
    failure("index_untrusted", lambda: restore(env, receipt, source_dir=source, index_dir=destination))
    assert len(env.engines) == 1


def test_a_sealed_instance_cannot_restore_itself(recoverable):
    env = recoverable
    receipt = env.adapter.seal()
    failure("index_untrusted", lambda: restore(env, receipt, adapter=env.adapter))
    assert len(env.engines) == 1


def test_engine_close_failure_does_not_issue_a_receipt(recoverable):
    env = recoverable

    def fail_close():
        raise RuntimeError("synthetic-private-flush-error")

    env.engines[0].close = fail_close
    error = failure("index_failed", env.adapter.seal)
    assert "synthetic-private" not in str(error)
    assert not (env.directory / "seal.json").exists()
    failure("index_not_ready", lambda: env.adapter.search("pump", rows=env.rows))


@pytest.mark.parametrize("mutation", ["old_seal", "hardlink", "symlink", "fifo", "manifest"])
def test_unsafe_live_indexes_cannot_be_sealed(recoverable, mutation):
    env = recoverable
    if mutation == "old_seal":
        (env.directory / "seal.json").write_text('{"trusted":true}')
    elif mutation == "hardlink":
        os.link(env.directory / "upstream/own-cache.bin", env.directory.parent / "external-link")
    elif mutation == "symlink":
        (env.directory / "untrusted").symlink_to(env.directory / "manifest.json")
    elif mutation == "fifo":
        os.mkfifo(env.directory / "pipe")
    else:
        (env.directory / "manifest.json").write_text("changed manifest")
    failure("index_untrusted", env.adapter.seal)
    assert env.engines[0].closed
    failure("index_not_ready", lambda: env.adapter.search("pump", rows=env.rows))


@pytest.mark.parametrize("bound", ["MAX_INDEX_FILE_BYTES", "MAX_INDEX_BYTES", "MAX_INDEX_FILES", "MAX_INDEX_ENTRIES", "MAX_SEAL_BYTES", "MAX_INDEX_DEPTH"])
def test_seal_resource_bounds_are_enforced(recoverable, monkeypatch, bound):
    env = recoverable
    monkeypatch.setattr(f"app.hipporag.{bound}", 1 if bound != "MAX_INDEX_DEPTH" else 0)
    failure("index_untrusted", env.adapter.seal)
    assert env.engines[0].closed
    failure("index_not_ready", lambda: env.adapter.search("pump", rows=env.rows))


def test_copy_is_verified_again_before_engine_construction(recoverable, monkeypatch):
    import app.hipporag as module

    env = recoverable
    receipt = env.adapter.seal()
    original = module._tree

    def tamper(directory, **kwargs):
        result = original(directory, **kwargs)
        destination = kwargs.get("destination")
        if destination is not None:
            (destination / "upstream/own-cache.bin").write_bytes(b"malicious-copy-never-open")
        return result

    monkeypatch.setattr(module, "_tree", tamper)
    failure("index_untrusted", lambda: restore(env, receipt))
    assert len(env.engines) == 1


def test_file_change_between_validation_and_copy_is_rejected(recoverable, monkeypatch):
    import app.hipporag as module

    env = recoverable
    receipt = env.adapter.seal()
    original = module._tree

    def tamper(directory, **kwargs):
        if kwargs.get("destination") is not None:
            (directory / "upstream/own-cache.bin").write_bytes(b"changed-after-initial-hash")
        return original(directory, **kwargs)

    monkeypatch.setattr(module, "_tree", tamper)
    failure("index_untrusted", lambda: restore(env, receipt))
    assert len(env.engines) == 1


def test_old_inventory_cannot_authorize_replaced_content(recoverable):
    env = recoverable
    receipt = env.adapter.seal()
    old_seal = (env.directory / "seal.json").read_bytes()
    (env.directory / "upstream/own-cache.bin").write_bytes(b"new-untrusted-cache")
    (env.directory / "seal.json").write_bytes(old_seal)
    failure("index_untrusted", lambda: restore(env, receipt))
    assert len(env.engines) == 1


def test_restoration_never_uses_unverified_paths_from_a_forged_inventory(recoverable):
    env = recoverable
    receipt = env.adapter.seal()
    path = env.directory / "seal.json"
    document = json.loads(path.read_text())
    document["entries"][0]["path"] = "../../outside.pkl"
    forged = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(forged)
    receipt["tree_digest"] = hashlib.sha256(forged).hexdigest()
    failure("index_untrusted", lambda: restore(env, receipt))
    assert len(env.engines) == 1


def test_source_rows_are_rechecked_after_copy_before_constructor(recoverable, monkeypatch):
    import app.hipporag as module

    env = recoverable
    receipt = env.adapter.seal()
    original = module._tree

    def revoke(directory, **kwargs):
        result = original(directory, **kwargs)
        if kwargs.get("destination") is not None:
            env.rows[0]["version"] += 1
        return result

    monkeypatch.setattr(module, "_tree", revoke)
    failure("identity_mismatch", lambda: restore(env, receipt))
    assert len(env.engines) == 1


def test_wrong_file_owner_fails_before_any_recovery_factory(recoverable, monkeypatch):
    import app.hipporag as module

    env = recoverable
    receipt = env.adapter.seal()
    fake_uid = os.geteuid() + 1
    receipt["owner_uid"] = fake_uid
    monkeypatch.setattr(module.os, "geteuid", lambda: fake_uid)
    failure("index_untrusted", lambda: restore(env, receipt))
    assert len(env.engines) == 1


def test_failed_recovery_constructor_does_not_modify_the_sealed_source(recoverable):
    env = recoverable
    receipt = env.adapter.seal()
    original = tree_contents(env.directory)
    attempted = []

    def broken(**kwargs):
        attempted.append(kwargs["save_dir"])
        raise RuntimeError("synthetic-private-constructor-error")

    adapter = HippoRAGAdapter(engine_factory=broken, chunk_factory=Chunk)
    error = failure("index_failed", lambda: restore(env, receipt, adapter=adapter))
    assert "synthetic-private" not in str(error)
    assert attempted == [str(env.destination / "upstream")]
    assert tree_contents(env.directory) == original
    failure("index_not_ready", lambda: adapter.search("pump", rows=env.rows))


def test_unavailable_restore_never_creates_or_loads_a_directory(recoverable):
    env = recoverable
    receipt = env.adapter.seal()
    adapter = HippoRAGAdapter()
    failure("unavailable", lambda: restore(env, receipt, adapter=adapter))
    assert not env.destination.exists()


@pytest.mark.parametrize("code", ["timeout", "cancelled", "worker_failed", "worker_protocol"])
def test_worker_failure_codes_are_fixed_safe_messages(code):
    error = HippoRAGError(code)
    assert error.code == code
    assert "HippoRAG" in str(error) and "\n" not in str(error)
