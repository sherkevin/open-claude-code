"""Acceptance tests for source-bound multimodal graph evidence.

The tests intentionally treat model output as a candidate.  Only OCR spans
and persisted image coordinates are source-bound; model relations remain
explicitly evidence-only.
"""
import asyncio
import io
import json

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import now, uid
from app.main import create_app


def _image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (640, 480), "#dbead4").save(stream, format="PNG")
    return stream.getvalue()


@pytest.fixture
def system(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
    app = create_app(Settings(data_dir=tmp_path, provider_settings="", model="test"))
    with TestClient(app):
        service = app.state.service
        user = service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "admin"})
        yield app, service, user


def _assistant_run(service, user, kb_id):
    assistant = service.save_assistant({"name": "visual", "knowledge_base_ids": [kb_id]}, user=user)
    conversation = service.create_conversation({"assistant_id": assistant["id"], "title": "visual"}, user)
    run_id = uid()
    service.db.execute(
        "INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,status,created_at) VALUES(?,?,?,?,?,'running',?)",
        (run_id, conversation["id"], user["id"], uid(), uid(), now()),
    )
    return assistant, run_id


def test_ocr_entity_is_bound_to_region_and_source_box(system, monkeypatch):
    _, service, user = system
    kb = service.knowledge.save({"name": "visual evidence"}, user)
    text = "压力=7 bar P-101"
    monkeypatch.setattr(service.knowledge.parser, "ocr_image", lambda _path: text)
    monkeypatch.setattr(
        service.knowledge.vision,
        "ocr_image",
        lambda _path: {
            "text": text,
            "boxes": [{"polygon": [[12, 20], [320, 20], [320, 70], [12, 70]], "text": text, "confidence": 0.98}],
            "model": "test-ocr",
            "status": "ready",
        },
    )
    monkeypatch.setattr(service.knowledge.vision, "embed_image", lambda _path: None)
    document = service.knowledge.upload(kb["id"], user, "nameplate.png", _image_bytes())

    assert document["status"] == "ready"
    rows = service.db.all(
        "SELECT label,region_id,source_boxes,source_offset_start,source_offset_end,evidence_kind FROM knowledge_visual_entities WHERE document_id=?",
        (document["id"],),
    )
    assert rows and all(row["region_id"] for row in rows)
    assert {row["evidence_kind"] for row in rows} == {"source_bound"}
    assert all(json.loads(row["source_boxes"])[0]["polygon"] for row in rows)
    assert all(row["source_offset_start"] < row["source_offset_end"] for row in rows)

    graph = service.knowledge.graph(kb["id"], user)
    entities = graph["multimodal"]["visual_entities"]
    assert entities and all(item["source_boxes"] for item in entities)
    assert all(item["evidence_kind"] == "source_bound" for item in entities)
    assert all(item["thumbnail_url"].endswith(f"{document['id']}/thumbnail?version=1") for item in entities)
    assert all(item["region_thumbnail_url"] for item in entities)


def test_provider_entities_are_candidates_and_relations_evidence_only(tmp_path, monkeypatch):
    catalog = tmp_path / "models.json"
    catalog.write_text(
        json.dumps(
            {
                "default_model_id": "vision",
                "models": [{"id": "vision", "label": "Vision", "upstream_model": "provider/vision", "capabilities": ["multimodal"], "reasoning_levels": {"default": None}}],
            }
        ),
        encoding="utf-8",
    )
    provider = tmp_path / "provider.json"
    provider.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "test-only-token", "ANTHROPIC_BASE_URL": "http://provider.test/v1"}}), encoding="utf-8")

    async def fake_visual(**_kwargs):
        return {
            "json": {
                "entities": [
                    {"label": "Control Server", "kind": "equipment", "confidence": 0.91},
                    {"label": "HMI", "kind": "equipment", "confidence": 0.88},
                ],
                "relations": [{"source": "Control Server", "target": "HMI", "relation": "connects", "confidence": 0.82}],
            },
            "latency_ms": 123.4,
            "response_sha256": "a" * 64,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }

    monkeypatch.setattr("app.knowledge.call_visual_json", fake_visual)
    app = create_app(
        Settings(
            data_dir=tmp_path / "data",
            model_catalog=str(catalog),
            provider_settings=str(provider),
            model="vision",
            knowledge_vision_model_id="vision",
            knowledge_vision_enabled=True,
        )
    )
    with TestClient(app):
        service = app.state.service
        user = service.create_user({"username": "owner", "display_name": "Owner", "password": "test-password-123", "role": "admin"})
        kb = service.knowledge.save({"name": "provider evidence"}, user)
        monkeypatch.setattr(service.knowledge.parser, "ocr_image", lambda _path: "")
        monkeypatch.setattr(service.knowledge.vision, "ocr_image", lambda _path: None)
        monkeypatch.setattr(service.knowledge.vision, "embed_image", lambda _path: None)
        document = service.knowledge.upload(kb["id"], user, "diagram.png", _image_bytes())
        entities = service.db.all("SELECT kind,evidence_kind,source_boxes FROM knowledge_visual_entities WHERE document_id=?", (document["id"],))
        assert len(entities) == 2
        assert all(item["kind"].startswith("model_entity:") and item["evidence_kind"] == "model_candidate" for item in entities)
        assert all(json.loads(item["source_boxes"]) == [] for item in entities)
        relation = service.db.one("SELECT relation,evidence,evidence_kind FROM knowledge_visual_relations WHERE document_id=?", (document["id"],))
        assert relation == {"relation": "connects", "evidence": "provider_visual_candidate", "evidence_kind": "evidence_only"}
        metadata = json.loads(service.db.one("SELECT metadata_json FROM knowledge_image_assets WHERE document_id=?", (document["id"],))["metadata_json"])
        assert metadata["provider_visual"]["response_sha256"] == "a" * 64
        assert metadata["provider_visual"]["usage"]["total_tokens"] == 30


def test_search_result_and_run_citation_expose_document_links(system):
    _, service, user = system
    kb = service.knowledge.save({"name": "links"}, user)
    document = service.knowledge.upload(kb["id"], user, "manual.md", b"Pressure limit is 7 bar.")
    result = service.knowledge.search([kb["id"]], user, "pressure limit", mode="bm25")
    assert result["results"] and result["results"][0]["content_url"].endswith(f"{document['id']}/content?version=1")
    assistant, run_id = _assistant_run(service, user, kb["id"])
    registered = asyncio.run(service.knowledge.search_for_run(user, assistant, run_id, "pressure limit"))["results"][0]
    assert registered["source_url"].startswith("/api/v1/sources/")
    assert registered["content_url"].endswith(f"{document['id']}/content?version=1")
