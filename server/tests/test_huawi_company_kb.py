import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "huawi-company-kb-v1"
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.service import Service
from scripts.seed_huawi_company_kb import seed_fixture


@pytest.fixture
def huawi_system(tmp_path):
    service = Service(Settings(data_dir=tmp_path, provider_settings="", model="test"))
    admin = service.create_user({"username": "admin", "display_name": "admin", "password": "test-password-123", "role": "admin"})
    try:
        yield service, admin
    finally:
        service.db.close()


def test_fixture_is_sanitized_and_source_manifest_is_explicit():
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["knowledge_base_name"] == "华微公司知识库"
    assert manifest["scope"] == "team"
    assert len(manifest["documents"]) >= 18
    assert manifest["excluded"]
    assert len(manifest["media"]) == 56
    assert all((FIXTURE / item["path"]).is_file() for item in manifest["media"])
    for item in manifest["documents"]:
        relative = item.get("path") or item.get("text_path")
        if not relative or not relative.endswith(".md"):
            continue
        text = (FIXTURE / relative).read_text(encoding="utf-8")
        assert "<whiteboard" not in text.lower()
        assert "https://ucnik4pxez5h.feishu.cn" not in text


def test_seed_cli_requires_an_existing_admin_and_never_creates_credentials(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "seed_huawi_company_kb.py"),
            "--fixture",
            str(FIXTURE),
            "--data-dir",
            str(tmp_path / "data"),
            "--admin-username",
            "missing-admin",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "server") + ":" + str(ROOT)},
    )
    assert result.returncode != 0
    assert "does not create or reset credentials" in result.stderr
    assert "local-seed-only" not in result.stdout + result.stderr


def test_seed_creates_team_kb_local_citations_and_curated_graph(huawi_system):
    service, admin = huawi_system
    result = seed_fixture(service, admin, FIXTURE, include_media=False)
    kb = result["knowledge_base"]
    assert kb["name"] == "华微公司知识库"
    assert kb["scope"] == "team"
    assert kb["default_for_new_assistants"] is False
    assert result["uploaded"] >= 18
    assert result["ready"] == result["uploaded"]

    graph = service.knowledge.graph(kb["id"], admin)
    labels = {node["label"] for node in graph["nodes"]}
    assert {"华微世纪", "智能体", "知识库", "MCP", "RAG", "Memory", "Workflow", "SOP", "APS", "MES"} <= labels
    assert graph["quality"]["status"] == "curated_seed"
    assert graph["quality"]["reviewed_entity_count"] >= 20
    assert graph["quality"]["quality_scope"] == "curated_seed_not_evaluated"
    assert graph["quality"]["candidate_noise_rate"] is None
    assert graph["quality"]["core_entity_recall"] is None
    assert 0 <= graph["quality"]["source_resolution_rate"] <= 1
    assert graph["edges"]
    assert all(edge["provenance"]["document_id"] for edge in graph["edges"])
    assert all(edge["provenance"]["start"] is not None for edge in graph["edges"])

    result = service.knowledge.search([kb["id"]], admin, "上下文管理 token 消耗", mode="bm25", limit=5)
    assert result["results"]
    assert any("上下文" in item["text"] or "token" in item["text"].lower() for item in result["results"])
    assert all(item["document_id"] and item["version"] >= 1 for item in result["results"])
