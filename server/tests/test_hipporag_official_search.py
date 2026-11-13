"""Offline service boundary tests; the official engine is a deterministic fake."""

import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import dump, now, uid
from app.main import create_app
from app.security import AppError
from app.service import assistant_view


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="test"))
    with TestClient(app) as client:
        service = app.state.service
        users = {
            name: service.create_user({
                "username": name, "display_name": name,
                "password": "test-password-123", "role": "member",
            })
            for name in ("owner", "peer")
        }
        kb = service.knowledge.save({"name": "Synthetic industrial manual"}, users["owner"])
        doc = service.knowledge.upload(
            kb["id"], users["owner"], "pump.md",
            b"P-101 pump connects to V-2 valve.\n\nP-101 pressure limit is 7 bar.",
        )
        yield client, service, users, kb, doc


def _rows(service, kb_ids, run_id=None):
    marks = ",".join("?" for _ in kb_ids)
    version = (
        "EXISTS(SELECT 1 FROM knowledge_run_documents rd WHERE rd.run_id=? "
        "AND rd.document_id=c.document_id AND rd.version=c.version)"
        if run_id else "d.current_version=c.version"
    )
    values = ([run_id] if run_id else []) + list(kb_ids)
    return service.db.all(
        f"SELECT c.*,d.name,d.kb_id,v.sha256,v.created_at AS imported_at,"
        f"k.name AS knowledge_base FROM knowledge_chunks c "
        f"JOIN knowledge_documents d ON d.id=c.document_id AND {version} "
        "JOIN knowledge_versions v ON v.document_id=c.document_id AND v.version=c.version "
        "JOIN knowledge_bases k ON k.id=d.kb_id "
        f"WHERE d.kb_id IN ({marks}) AND d.deleted=0 AND v.status='ready' ORDER BY c.start",
        values,
    )


def _run(service, owner, kb):
    assistant = service.save_assistant(
        {"name": "Synthetic retrieval assistant", "knowledge_base_ids": [kb["id"]]},
        user=owner,
    )
    snapshot = assistant_view(service.require("assistants", assistant["id"]), owner)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "Offline test"}, owner)
    run_id = uid()
    service.db.execute(
        "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at,capability_snapshot) "
        "VALUES(?,?,?,?,?,'running',?,?)",
        (run_id, conversation["id"], owner["id"], uid(), uid(), now(), dump(snapshot)),
    )
    service.db.execute("UPDATE conversations SET active_run_id=? WHERE id=?", (run_id, conversation["id"]))
    service.knowledge.snapshot_for_run(run_id, snapshot, owner)
    return assistant, conversation, run_id


class FakeOfficialEngine:
    def __init__(self):
        self.chunks = []
        self.calls = 0
        self.during_retrieve = lambda: None
        self.invalid_metadata = False
        self.failure = None

    def index(self, chunks):
        self.chunks = chunks

    def retrieve(self, queries, num_to_retrieve):
        self.calls += 1
        self.during_retrieve()
        if self.failure:
            raise self.failure
        # One upstream hit only: the service must not invent official neighbors.
        chunk = self.chunks[0]
        metadata = {} if self.invalid_metadata else {**chunk.metadata, "source_id": chunk.source_id}
        return [SimpleNamespace(
            question=queries[0], docs=[chunk.content], doc_scores=[0.75],
            doc_metadata=[metadata], graph_seeds=[],
        )]


def _attach(service, rows, directory):
    from app.hipporag import HippoRAGAdapter, PINNED_COMMIT

    engine = FakeOfficialEngine()
    adapter = HippoRAGAdapter(
        engine_factory=lambda **_kwargs: engine,
        chunk_factory=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    adapter.build(rows, identity={
        "upstream_commit": PINNED_COMMIT,
        "parser_digest": "a" * 64, "extraction_digest": "b" * 64,
        "embedding_digest": "c" * 64, "chunking_digest": "d" * 64,
        "acl_digest": "e" * 64,
    }, index_dir=directory)
    service.knowledge.hipporag_backend = adapter
    return engine


def test_default_hipporag_stays_lite_and_does_not_create_official_index(system):
    _, service, users, kb, _ = system
    assert service.knowledge.hipporag_backend is None
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag")
    assert result["retrieval"]["algorithm"] == "hipporag2-lite"
    assert result["retrieval"]["backend"] == "lite"
    assert not list(service.settings.data_dir.rglob("manifest.json"))


def test_official_hit_uses_local_evidence_links_without_lite_padding(system, tmp_path):
    client, service, users, kb, doc = system
    _, _, run_id = _run(service, users["owner"], kb)
    engine = _attach(service, _rows(service, [kb["id"]], run_id), tmp_path / "official-index")
    result = service.knowledge.search(
        [kb["id"]], users["owner"], "P-101", mode="hipporag",
        run_id=run_id, limit=6, context_chars=64,
    )
    assert engine.calls == 1
    assert result["retrieval"]["algorithm"] == "hipporag2-official"
    assert result["retrieval"]["backend"] == "official"
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["retrieval_algorithm"] == "hipporag2-official"
    assert hit["document_id"] == doc["id"] and hit["version"] == 1
    assert hit["content_url"] == doc["content_url"]
    assert hit["text"] in {row["text"] for row in _rows(service, [kb["id"]])}
    assert hit.get("evidence_type") != "adjacent_source_context"
    assert result["context"]["characters"] <= 64
    registered = service.knowledge._register_chunk(run_id, hit)
    evidence = service.evidence.get(registered["source_id"], users["owner"])
    assert evidence["document_id"] == doc["id"]
    assert evidence["locator"]["sha256"] == doc["sha256"]
    assert registered["citation"].startswith("[[source:")
    assert client.post("/api/v1/auth/login", json={"username": "owner", "password": "test-password-123"}).status_code == 200
    assert client.get(registered["content_url"]).status_code == 200


@pytest.mark.parametrize("mode", ["hybrid", "bm25", "vector"])
def test_other_retrieval_modes_never_call_official_backend(system, tmp_path, mode):
    _, service, users, kb, _ = system
    engine = _attach(service, _rows(service, [kb["id"]]), tmp_path / "official-index")
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode=mode)
    assert engine.calls == 0
    assert result["retrieval"]["mode"] == mode


def test_private_knowledge_is_rejected_before_official_retrieval(system, tmp_path):
    _, service, users, kb, _ = system
    engine = _attach(service, _rows(service, [kb["id"]]), tmp_path / "official-index")
    with pytest.raises(AppError):
        service.knowledge.search([kb["id"]], users["peer"], "P-101", mode="hipporag")
    assert engine.calls == 0


def test_superset_official_index_is_rejected_before_graph_query(system, tmp_path):
    _, service, users, kb, _ = system
    foreign = service.knowledge.save({"name": "Other user private knowledge"}, users["peer"])
    service.knowledge.upload(foreign["id"], users["peer"], "private.md", b"SECRET_OTHER_USER P-101 pressure is 999 bar.")
    engine = _attach(service, _rows(service, [kb["id"], foreign["id"]]), tmp_path / "official-index")
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag")
    assert engine.calls == 0
    assert result["retrieval"]["algorithm"] == "hipporag2-lite"
    assert result["retrieval"]["backend"] == "lite"
    assert result["retrieval"]["fallback"] == "identity_mismatch"
    assert all(hit["kb_id"] == kb["id"] for hit in result["results"])
    assert "SECRET_OTHER_USER" not in dump(result)


@pytest.mark.parametrize("invalid", ["exception", "metadata"])
def test_official_failure_is_visible_and_does_not_leak_exception_details(system, tmp_path, invalid):
    _, service, users, kb, _ = system
    engine = _attach(service, _rows(service, [kb["id"]]), tmp_path / "official-index")
    if invalid == "exception":
        engine.failure = RuntimeError("SECRET_ENDPOINT /private/model/path")
    else:
        engine.invalid_metadata = True
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag")
    assert engine.calls == 1
    assert result["retrieval"]["algorithm"] == "hipporag2-lite"
    assert result["retrieval"]["backend"] == "lite"
    assert result["retrieval"]["fallback"] == ("retrieval_failed" if invalid == "exception" else "result_invalid")
    assert result["results"]
    assert "SECRET_ENDPOINT" not in dump(result)
    assert "/private/model/path" not in dump(result)


def test_unknown_backend_chunk_cannot_enter_results_or_context(system):
    _, service, users, kb, _ = system

    class UnknownChunkBackend:
        def search(self, query, *, rows, limit):
            return {"unregistered-secret-chunk": 1.0}, {}

    service.knowledge.hipporag_backend = UnknownChunkBackend()
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag")
    assert result["retrieval"]["backend"] == "lite"
    assert result["retrieval"]["fallback"] == "result_invalid"
    assert "unregistered-secret-chunk" not in dump(result)


def test_stale_index_falls_back_to_current_version_without_running_official_query(system, tmp_path):
    _, service, users, kb, doc = system
    engine = _attach(service, _rows(service, [kb["id"]]), tmp_path / "official-index")
    service.knowledge.upload(kb["id"], users["owner"], "pump.md", b"P-101 pressure is now 9 bar.", doc["id"])
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag")
    assert engine.calls == 0
    assert result["retrieval"]["fallback"] == "identity_mismatch"
    assert result["results"] and all(hit["version"] == 2 for hit in result["results"])
    assert "7 bar" not in result["context"]["text"]


@pytest.mark.parametrize("change", [
    "document_deleted", "kb_disabled", "kb_deleted", "user_disabled", "owner_changed",
    "version_failed", "new_version", "chunk_changed",
])
def test_changes_during_official_retrieval_reject_stale_results(system, tmp_path, change):
    _, service, users, kb, doc = system
    engine = _attach(service, _rows(service, [kb["id"]]), tmp_path / "official-index")

    def mutate():
        if change == "document_deleted":
            service.db.execute("UPDATE knowledge_documents SET deleted=1 WHERE id=?", (doc["id"],))
        elif change == "kb_disabled":
            service.db.execute("UPDATE knowledge_bases SET enabled=0 WHERE id=?", (kb["id"],))
        elif change == "kb_deleted":
            service.db.execute("UPDATE knowledge_bases SET deleted=1 WHERE id=?", (kb["id"],))
        elif change == "user_disabled":
            service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (users["owner"]["id"],))
        elif change == "owner_changed":
            service.db.execute("UPDATE knowledge_bases SET owner_id=? WHERE id=?", (users["peer"]["id"], kb["id"]))
        elif change == "version_failed":
            service.db.execute("UPDATE knowledge_versions SET status='failed' WHERE document_id=?", (doc["id"],))
        elif change == "new_version":
            service.knowledge.upload(kb["id"], users["owner"], "pump.md", b"P-101 pressure is now 9 bar.", doc["id"])
        else:
            service.db.execute("UPDATE knowledge_chunks SET text='CHANGED_SOURCE' WHERE document_id=?", (doc["id"],))

    engine.during_retrieve = mutate
    with pytest.raises(AppError) as rejected:
        service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag")
    assert rejected.value.status in {403, 404, 409}
    assert engine.calls == 1


def test_run_snapshot_keeps_old_version_when_new_version_arrives_during_query(system, tmp_path):
    _, service, users, kb, doc = system
    _, _, run_id = _run(service, users["owner"], kb)
    engine = _attach(service, _rows(service, [kb["id"]], run_id), tmp_path / "official-index")
    engine.during_retrieve = lambda: service.knowledge.upload(
        kb["id"], users["owner"], "pump.md", b"P-101 pressure is now 9 bar.", doc["id"],
    )
    result = service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag", run_id=run_id)
    assert result["retrieval"]["backend"] == "official"
    assert result["results"] and all(hit["version"] == 1 for hit in result["results"])
    assert "9 bar" not in result["context"]["text"]


@pytest.mark.parametrize("change", ["assistant_disabled", "kb_unbound", "archived", "inactive_run"])
def test_run_authorization_is_rechecked_after_official_query(system, tmp_path, change):
    _, service, users, kb, _ = system
    assistant, conversation, run_id = _run(service, users["owner"], kb)
    engine = _attach(service, _rows(service, [kb["id"]], run_id), tmp_path / "official-index")

    def revoke():
        if change == "assistant_disabled":
            service.save_assistant({"enabled": False}, assistant["id"], user=users["owner"])
        elif change == "kb_unbound":
            service.save_assistant({"knowledge_base_ids": []}, assistant["id"], user=users["owner"])
        elif change == "archived":
            service.db.execute("UPDATE conversations SET archived=1 WHERE id=?", (conversation["id"],))
        else:
            service.db.execute("UPDATE conversations SET active_run_id=NULL WHERE id=?", (conversation["id"],))

    engine.during_retrieve = revoke
    with pytest.raises(AppError):
        service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag", run_id=run_id)
    assert engine.calls == 1


def test_another_users_run_is_rejected_before_official_query(system, tmp_path):
    _, service, users, kb, _ = system
    other_kb = service.knowledge.save({"name": "Peer knowledge"}, users["peer"])
    _, _, foreign_run = _run(service, users["peer"], other_kb)
    engine = _attach(service, _rows(service, [kb["id"]]), tmp_path / "official-index")
    with pytest.raises(AppError):
        service.knowledge.search([kb["id"]], users["owner"], "P-101", mode="hipporag", run_id=foreign_run)
    assert engine.calls == 0


def test_agent_search_uses_explicit_backend_and_registers_versioned_citation(system, tmp_path):
    _, service, users, kb, doc = system
    assistant, _, run_id = _run(service, users["owner"], kb)
    engine = _attach(service, _rows(service, [kb["id"]], run_id), tmp_path / "official-index")
    result = asyncio.run(service.knowledge.search_for_run(users["owner"], assistant, run_id, "P-101"))
    assert engine.calls == 1 and result["retrieval"]["backend"] == "official"
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["source_url"].startswith("/api/v1/sources/")
    assert hit["content_url"] == doc["content_url"]
    assert hit["citation"] == f"[[source:{hit['source_id']}]]"
    assert service.evidence.get(hit["source_id"], users["owner"])["document_version"] == 1


@pytest.mark.parametrize("score", [0.0, -0.5])
def test_official_scores_are_not_assumed_positive_and_upstream_trace_is_not_forwarded(system, score):
    _, service, users, kb, _ = system
    source = _rows(service, [kb["id"]])[0]

    class ScoredBackend:
        def search(self, query, *, rows, limit):
            return {source["id"]: score}, {
                "source_count": len(rows), "private_path": "/SECRET_MODEL_DIRECTORY",
                "graph_seed_count": "SECRET_PROVIDER_TRACE",
            }

    service.knowledge.hipporag_backend = ScoredBackend()
    result = service.knowledge.search([kb["id"]], users["owner"], "unmatched", mode="hipporag")
    assert result["retrieval"]["backend"] == "official"
    assert len(result["results"]) == 1
    assert result["results"][0]["chunk_id"] == source["id"]
    assert result["results"][0]["score"] == score
    assert "SECRET_MODEL_DIRECTORY" not in dump(result)
    assert "SECRET_PROVIDER_TRACE" not in dump(result)


@pytest.mark.parametrize("change", ["deleted", "current_version", "sha256", "status"])
def test_pure_image_fallback_rechecks_each_source_before_return(system, monkeypatch, change):
    from PIL import Image

    _, service, users, _, _ = system
    knowledge = service.knowledge
    kb = knowledge.save({"name": "Synthetic image-only evidence"}, users["owner"])
    monkeypatch.setattr(knowledge.parser, "ocr_image", lambda _path: "")
    monkeypatch.setattr(knowledge.vision, "ocr_image", lambda _path: None)
    monkeypatch.setattr(knowledge.vision, "embed_image", lambda _path: None)
    image = io.BytesIO()
    Image.new("RGB", (64, 64), "#456b55").save(image, format="PNG")
    doc = knowledge.upload(kb["id"], users["owner"], "synthetic.png", image.getvalue())
    assert _rows(service, [kb["id"]]) == []

    class UnexpectedOfficialCall:
        def search(self, *_args, **_kwargs):
            pytest.fail("Image-only fallback must not query a text index")

    knowledge.hipporag_backend = UnexpectedOfficialCall()

    def visual_result(*_args, **_kwargs):
        if change == "deleted":
            service.db.execute("UPDATE knowledge_documents SET deleted=1 WHERE id=?", (doc["id"],))
        elif change == "current_version":
            service.db.execute("UPDATE knowledge_documents SET current_version=2 WHERE id=?", (doc["id"],))
        elif change == "sha256":
            service.db.execute("UPDATE knowledge_versions SET sha256=? WHERE document_id=?", ("f" * 64, doc["id"]))
        else:
            service.db.execute("UPDATE knowledge_versions SET status='failed' WHERE document_id=?", (doc["id"],))
        return [{"document_id": doc["id"], "version": 1, "sha256": doc["sha256"], "kb_id": kb["id"]}]

    monkeypatch.setattr(knowledge, "_visual_search", visual_result)
    with pytest.raises(AppError):
        knowledge.search([kb["id"]], users["owner"], "green machine", mode="hipporag")


@pytest.mark.parametrize("change", ["user_disabled", "document_deleted", "kb_unbound", "chunk_changed"])
def test_agent_handoff_rechecks_before_registering_any_source(system, tmp_path, monkeypatch, change):
    _, service, users, kb, doc = system
    knowledge = service.knowledge
    assistant, _, run_id = _run(service, users["owner"], kb)
    _attach(service, _rows(service, [kb["id"]], run_id), tmp_path / "official-index")
    original_search = knowledge.search

    def search_then_revoke(*args, **kwargs):
        result = original_search(*args, **kwargs)
        if change == "user_disabled":
            service.db.execute("UPDATE users SET enabled=0 WHERE id=?", (users["owner"]["id"],))
        elif change == "document_deleted":
            service.db.execute("UPDATE knowledge_documents SET deleted=1 WHERE id=?", (doc["id"],))
        elif change == "kb_unbound":
            service.save_assistant({"knowledge_base_ids": []}, assistant["id"], user=users["owner"])
        else:
            service.db.execute("UPDATE knowledge_chunks SET text='CHANGED_AFTER_QUERY' WHERE document_id=?", (doc["id"],))
        return result

    monkeypatch.setattr(knowledge, "search", search_then_revoke)
    with pytest.raises(AppError):
        asyncio.run(knowledge.search_for_run(users["owner"], assistant, run_id, "P-101"))
    assert service.db.one("SELECT COUNT(*) n FROM evidence_sources WHERE run_id=?", (run_id,))["n"] == 0
