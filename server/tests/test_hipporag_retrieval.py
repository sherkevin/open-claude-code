import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import now, uid
from app.main import create_app
from app.security import AppError


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="test"))
    with TestClient(app) as client:
        service = app.state.service
        owner = service.create_user({"username": "owner", "display_name": "owner", "password": "test-password-123", "role": "member"})
        peer = service.create_user({"username": "peer", "display_name": "peer", "password": "test-password-123", "role": "member"})
        yield client, service, owner, peer


def _run(service, user, assistant, conversation):
    run_id = uid()
    service.db.execute(
        "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at) VALUES(?,?,?,?,?,'running',?)",
        (run_id, conversation["id"], user["id"], uid(), uid(), now()),
    )
    return run_id


def test_hipporag_is_deterministic_and_promotes_two_hop_source(system):
    _, service, owner, _ = system
    kb = service.knowledge.save({"name": "HippoRAG fixture"}, owner)
    linked = service.knowledge.upload(
        kb["id"], owner, "linked.md",
        "泵站 P-101 连接阀门 V-2。\n\n泵站维护周期为每周一次。".encode(),
    )
    service.knowledge.upload(kb["id"], owner, "unrelated.md", "仓储盘点周期为每月一次。".encode())

    first = service.knowledge.search([kb["id"]], owner, "P-101 泵站", limit=4, mode="hipporag")
    second = service.knowledge.search([kb["id"]], owner, "P-101 泵站", limit=4, mode="hipporag")
    assert first["retrieval"]["algorithm"] == "hipporag2-lite"
    assert first["retrieval"]["graph_trace"]["seed_count"] >= 1
    assert first["retrieval"]["graph_trace"]["max_hops"] == 2
    assert [item["chunk_id"] for item in first["results"]] == [item["chunk_id"] for item in second["results"]]
    assert first["results"][0]["document_id"] == linked["id"]
    assert first["results"][0]["graph_score"] > 0
    assert first["results"][0]["retrieval_algorithm"] == "hipporag2-lite"
    assert first["results"][0]["graph_trace"]["visited_node_count"] >= 1


def test_hipporag_fails_closed_without_graph_seed_or_index(system):
    _, service, owner, _ = system
    kb = service.knowledge.save({"name": "Fallback"}, owner)
    service.knowledge.upload(kb["id"], owner, "plain.md", "这个文件只包含普通说明文字。".encode())
    no_seed = service.knowledge.search([kb["id"]], owner, "不存在的查询", mode="hipporag")
    assert no_seed["retrieval"]["graph_trace"]["fallback"] in {"no_query_entity_seed", "graph_nodes_without_chunk_evidence"}
    assert no_seed["retrieval"]["algorithm"] == "hipporag2-lite"

    empty = service.knowledge.search([], owner, "P-101", mode="hipporag")
    assert empty["results"] == []
    assert empty["retrieval"]["algorithm"] == "hipporag2-lite"


def test_hipporag_respects_acl_snapshot_and_registered_citation(system):
    _, service, owner, peer = system
    kb = service.knowledge.save({"name": "Private graph"}, owner)
    doc = service.knowledge.upload(kb["id"], owner, "private.md", "P-900 的压力上限为 7 bar。".encode())
    with pytest.raises(AppError):
        service.knowledge.search([kb["id"]], peer, "P-900", mode="hipporag")

    assistant = service.save_assistant({"name": "Graph assistant", "knowledge_base_ids": [kb["id"]]}, user=owner)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "HippoRAG"}, user=owner)
    run_id = _run(service, owner, assistant, conversation)
    service.knowledge.snapshot_for_run(run_id, assistant, owner)
    result = service.knowledge.search([kb["id"]], owner, "P-900", mode="hipporag", run_id=run_id)
    assert result["results"]
    assert result["retrieval"]["graph_trace"]["seed_count"] >= 1
    registered = service.knowledge._register_chunk(run_id, result["results"][0])
    assert registered["document_id"] == doc["id"]
    assert registered["source_url"].startswith("/api/v1/sources/")
    assert registered["content_url"].endswith(f"version={doc['version']}")
    assert registered["citation"].startswith("[[source:")

    # The run snapshot is immutable: a new version is not silently included.
    service.knowledge.upload(kb["id"], owner, "private.md", "P-900 的压力上限为 9 bar。".encode(), doc["id"])
    snapshot = service.knowledge.search([kb["id"]], owner, "P-900", mode="hipporag", run_id=run_id)
    assert snapshot["results"] and snapshot["results"][0]["version"] == 1
