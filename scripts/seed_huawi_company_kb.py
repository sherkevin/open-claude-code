#!/usr/bin/env python3
"""Seed the curated Huawi company knowledge base in an isolated data dir.

This is intentionally a project-branch seed job, not a production migration.
It imports sanitized local copies, then replaces rule-only graph candidates
with source-bound, manually reviewed entities and relations from the fixture.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.config import Settings  # noqa: E402
from app.db import now  # noqa: E402
from app.service import Service  # noqa: E402


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def stable_id(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:32]


def locate_source(service: Service, kb_id: str, entity: dict, review_document_id: str):
    aliases = [entity["label"], *entity.get("aliases", []), entity.get("anchor", "")]
    rows = service.db.all(
        "SELECT c.*,d.name FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id "
        "WHERE d.kb_id=? AND d.deleted=0 ORDER BY CASE WHEN d.id=? THEN 0 ELSE 1 END,c.document_id,c.page,c.start",
        (kb_id, review_document_id),
    )
    for alias in aliases:
        if not alias:
            continue
        needle = alias.casefold()
        for row in rows:
            index = row["text"].casefold().find(needle)
            if index >= 0:
                start = row["start"] + index
                return row, start, start + len(alias), True
    # Every curated entity must still have a valid local evidence span. The
    # review document is the explicit fallback, never an invented source.
    row = next((item for item in rows if item["document_id"] == review_document_id), rows[0] if rows else None)
    if row is None:
        raise RuntimeError(f"no indexed chunk available for entity {entity['label']}")
    return row, row["start"], min(row["end"], row["start"] + len(entity["label"])), False


def install_curated_graph(service: Service, kb_id: str, fixture: Path) -> dict:
    entities = load_json(fixture / "graph" / "curated_entities.json")["entities"]
    edges = load_json(fixture / "graph" / "curated_edges.json")["edges"]
    review = service.db.one(
        "SELECT id FROM knowledge_documents WHERE kb_id=? AND name=? AND deleted=0",
        (kb_id, "graph-curated-review.md"),
    )
    if not review:
        raise RuntimeError("curated graph review document was not indexed")
    service.db.execute("DELETE FROM knowledge_graph_edges WHERE kb_id=?", (kb_id,))
    service.db.execute("DELETE FROM knowledge_graph_nodes WHERE kb_id=?", (kb_id,))
    node_ids = {}
    locations = {}
    resolved_entities = 0
    stamp = now()
    for entity in entities:
        row, start, end, resolved = locate_source(service, kb_id, entity, review["id"])
        resolved_entities += int(resolved)
        node_id = stable_id("huawi-curated", kb_id, entity["id"])
        node_ids[entity["id"]] = node_id
        locations[entity["id"]] = (row, start, end)
        service.db.execute(
            "INSERT INTO knowledge_graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (node_id, kb_id, row["document_id"], row["version"], entity["label"], entity["label"].casefold(), entity["kind"], row["page"], row["paragraph"], start, end, stamp),
        )
    for edge in edges:
        source = locations[edge["source"]][0]
        _, start, end = locations[edge["source"]]
        edge_id = stable_id("huawi-curated-edge", kb_id, edge["source"], edge["target"], edge["relation"])
        service.db.execute(
            "INSERT INTO knowledge_graph_edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (edge_id, kb_id, source["document_id"], source["version"], node_ids[edge["source"]], node_ids[edge["target"]], edge["relation"], source["page"], source["paragraph"], start, end, stamp),
        )
    quality = {
        "status": "curated_seed",
        "quality_scope": "curated_seed_not_evaluated",
        "reviewed_entity_count": len(entities),
        "reviewed_edge_count": len(edges),
        "source_resolution_rate": resolved_entities / len(entities) if entities else 0.0,
        "unresolved_entity_count": len(entities) - resolved_entities,
        "candidate_noise_rate": None,
        "core_entity_recall": None,
        "acceptance_targets": {"core_entity_recall": 0.90, "candidate_noise_rate": 0.10},
        "source_documents": sorted({locations[item["id"]][0]["name"] for item in entities}),
        "note": "人工整理的种子图谱；规则候选未作为已核验事实。未找到原始片段的种子只指向复核文档，正式验收前不能把未测指标当作质量结论。",
    }
    target = service.knowledge.root / kb_id / "graph-quality.json"
    target.write_text(json.dumps(quality, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target.chmod(0o400)
    return quality


def seed_fixture(service: Service, user: dict, fixture: Path, include_media: bool = True, replace: bool = False) -> dict:
    manifest = load_json(fixture / "manifest.json")
    existing = service.db.one("SELECT id FROM knowledge_bases WHERE name=? AND deleted=0", (manifest["knowledge_base_name"],))
    if existing and not replace:
        raise RuntimeError("华微公司知识库已存在；如需重建请显式使用 --replace")
    if existing:
        service.knowledge.delete(existing["id"], user)
    kb = service.knowledge.save(
        {
            "name": manifest["knowledge_base_name"],
            "description": "华微世纪项目群授权资料：会议逐字稿、合作方案、产品演示和人工复核图谱。团队可见；新助手不自动绑定。",
            "scope": "team",
            "enabled": True,
            "default_for_new_assistants": False,
        },
        user,
    )
    uploaded = []
    for item in manifest["documents"]:
        relative = item.get("path") or item.get("text_path")
        if not relative:
            continue
        path = fixture / relative
        if not path.is_file():
            raise RuntimeError(f"fixture document missing: {relative}")
        uploaded.append(service.knowledge.upload(kb["id"], user, path.name, path.read_bytes()))
    if include_media:
        for item in manifest.get("media", []):
            path = fixture / item["path"]
            if path.is_file():
                uploaded.append(service.knowledge.upload(kb["id"], user, path.name, path.read_bytes()))
    quality = install_curated_graph(service, kb["id"], fixture)
    return {"knowledge_base": service.knowledge.view(service.knowledge.require(kb["id"], user), user), "uploaded": len(uploaded), "ready": sum(item["status"] == "ready" for item in uploaded), "graph_quality": quality}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=Path("fixtures/huawi-company-kb-v1"))
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument(
        "--admin-username",
        required=True,
        help="Existing enabled admin username; the seed job never creates or resets credentials.",
    )
    parser.add_argument("--include-media", action="store_true")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    service = Service(Settings(data_dir=args.data_dir))
    try:
        row = service.db.one(
            "SELECT * FROM users WHERE username=? AND role='admin' AND enabled=1",
            (args.admin_username,),
        )
        if not row:
            raise SystemExit(
                f"enabled admin account not found: {args.admin_username}; "
                "the seed job does not create or reset credentials"
            )
        print(
            json.dumps(
                seed_fixture(service, dict(row), args.fixture, args.include_media, args.replace),
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        service.db.close()


if __name__ == "__main__":
    main()
