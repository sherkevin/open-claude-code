#!/usr/bin/env python3
"""Build a sanitized, source-bound fixture for the Huawi company KB.

The source directory is a local school-cloud archive collected from the
authorized Huawi project group.  This script deliberately keeps raw files
separate from searchable text: private Feishu URLs and whiteboard tokens are
removed from the RAG documents, while provenance remains in the manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree


PRIVATE_URL = re.compile(r"https?://(?:[^\s/]+\.)?feishu\.cn/[^\s)]+", re.I)
WHITEBOARD = re.compile(r"<whiteboard\b[^>]*>.*?</whiteboard>", re.I | re.S)
READONLY_BLOCK = re.compile(r"<readonly-block\b[^>]*/>", re.I)
FILE_TAG = re.compile(r"<file\b[^>]*/>", re.I)
TITLE = re.compile(r"<title>(.*?)</title>", re.S | re.I)
ROOT = Path(__file__).resolve().parents[1]
CURATED_GRAPH_SOURCE = ROOT / "scripts" / "huawi_company_graph"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sanitize(text: str) -> str:
    text = WHITEBOARD.sub("[白板内容已归档，未进入检索正文]", text)
    text = READONLY_BLOCK.sub("", text)
    text = FILE_TAG.sub("[群文件已归档，见来源清单]", text)
    text = PRIVATE_URL.sub("[飞书内部链接已归档]", text)
    # Keep searchable documents free of accidental phone-number leakage.
    text = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已脱敏]", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip() + "\n"


def doc_text(path: Path) -> tuple[str, str]:
    if path.suffix.lower() == ".json":
        raw = path.read_text(encoding="utf-8")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # The two newest documents were deliberately saved as sanitized
            # Markdown with a .json provenance suffix; treat them as text.
            title = TITLE.search(raw)
            return title.group(1).strip() if title else path.stem, raw
        content = obj.get("data", {}).get("document", {}).get("content", "")
        title = TITLE.search(content)
        return title.group(1).strip() if title else path.stem, content
    return path.stem, path.read_text(encoding="utf-8", errors="replace")


def pptx_text(path: Path) -> str:
    """Extract visible slide text without trusting embedded HTML or macros."""
    namespace = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
    slides: list[tuple[int, str]] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.search(r"(\d+)", name).group(1)),
        )
        for name in names:
            root = ElementTree.fromstring(archive.read(name))
            values = [item.text.strip() for item in root.findall(".//a:t", namespace) if item.text and item.text.strip()]
            slides.append((int(re.search(r"(\d+)", name).group(1)), "\n".join(values)))
    return "\n\n".join(f"## 第 {number} 页\n{text or '[该页为图片内容，已生成页面预览]'}" for number, text in slides) + "\n"


def render_pptx_pages(path: Path, target: Path) -> list[str]:
    """Render bounded page previews when LibreOffice and pdftoppm are available."""
    target.mkdir(parents=True, exist_ok=True)
    existing = sorted(target.glob(f"{path.stem}-page-*.jpg"))
    if existing:
        return [item.name for item in existing[:32]]
    with tempfile.TemporaryDirectory(prefix="huawi-pptx-") as temp:
        temp_path = Path(temp)
        converted = subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", str(temp_path), str(path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        pdf = temp_path / f"{path.stem}.pdf"
        if converted.returncode != 0 or not pdf.exists():
            return []
        prefix = temp_path / "page"
        rendered = subprocess.run(
            ["pdftoppm", "-jpeg", "-r", "96", str(pdf), str(prefix)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if rendered.returncode != 0:
            return []
        outputs = []
        for image in sorted(temp_path.glob("page-*.jpg"))[:32]:
            destination = target / f"{path.stem}-{image.stem}.jpg"
            shutil.copy2(image, destination)
            outputs.append(destination.name)
        return outputs


def build(source: Path, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    documents = output / "documents"
    media = output / "media"
    documents.mkdir(exist_ok=True)
    media.mkdir(exist_ok=True)
    manifest = {
        "fixture_id": "huawi-company-kb-v1",
        "knowledge_base_name": "华微公司知识库",
        "scope": "team",
        "source_chat_id": "oc_0e594fd71ee1b3e2e0b46365e4f42a7f",
        "source_identity": "authorized project-group archive",
        "school_cloud_only": True,
        "documents": [],
        "excluded": [],
        "media": [],
        "provenance_policy": {
            "search_text": "sanitized_local_copy",
            "raw_archive": "school_cloud_only",
            "private_links_in_body": False,
            "citation": "local knowledge-document content URL",
            "ai_summary_disclaimer": "飞书智能纪要由 AI 生成，可能不准确；逐字稿优先。",
        },
    }

    for path in sorted((source / "docs").glob("*.json")):
        title, raw = doc_text(path)
        related = "闲聊" not in title
        record = {
            "source": str(path.relative_to(source)),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
            "title": title,
            "kind": "feishu_doc",
            "included": related,
            "summary_only": "智能纪要" in title,
        }
        if not related:
            record["reason"] = "与华微项目无关的闲聊内容，不进入公司知识库"
            manifest["excluded"].append(record)
            continue
        name = f"feishu-{path.stem}.md"
        (documents / name).write_text(sanitize(raw), encoding="utf-8")
        record["path"] = f"documents/{name}"
        record["text_sha256"] = sha256(documents / name)
        manifest["documents"].append(record)

    for path in sorted((source / "minutes").glob("*/transcript.txt")):
        related = "soundcore" not in path.parent.name.lower()
        record = {
            "source": str(path.relative_to(source)),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
            "title": path.parent.name,
            "kind": "feishu_transcript",
            "included": related,
            "summary_only": False,
        }
        if not related:
            record["reason"] = "群内非华微项目录音，不进入公司知识库"
            manifest["excluded"].append(record)
            continue
        name = f"transcript-{len(manifest['documents']) + 1:02d}.md"
        (documents / name).write_text(sanitize(path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        record["path"] = f"documents/{name}"
        record["text_sha256"] = sha256(documents / name)
        manifest["documents"].append(record)

    for path in sorted((source / "files").glob("*.pptx")):
        record = {"source": str(path.relative_to(source)), "sha256": sha256(path), "bytes": path.stat().st_size, "kind": "feishu_pptx", "included": True}
        text_name = f"slides-{path.stem}.md"
        (documents / text_name).write_text(pptx_text(path), encoding="utf-8")
        record["text_path"] = f"documents/{text_name}"
        record["text_sha256"] = sha256(documents / text_name)
        preview_dir = media / path.stem
        pages = render_pptx_pages(path, preview_dir)
        record["page_previews"] = [f"media/{path.stem}/{item}" for item in pages]
        manifest["documents"].append(record)
        manifest["media"].extend({"path": f"media/{path.stem}/{item}", "source": record["source"], "evidence_kind": "page_preview"} for item in pages)

    # The curated graph review is a checked-in project artifact. Copying it
    # here makes fixture rebuilds deterministic while keeping it separate from
    # original customer documents and rule-based extraction candidates.
    graph_dir = output / "graph"
    graph_dir.mkdir(exist_ok=True)
    for name in ("curated_entities.json", "curated_edges.json"):
        source = CURATED_GRAPH_SOURCE / name
        if not source.is_file():
            raise FileNotFoundError(f"curated graph source missing: {source}")
        shutil.copy2(source, graph_dir / name)
    review = documents / "graph-curated-review.md"
    review_source = CURATED_GRAPH_SOURCE / "graph-curated-review.md"
    if not review_source.is_file():
        raise FileNotFoundError(f"curated graph review source missing: {review_source}")
    shutil.copy2(review_source, review)
    manifest["documents"].append({
        "source": "scripts/huawi_company_graph/graph-curated-review.md",
        "sha256": sha256(review_source),
        "bytes": review_source.stat().st_size,
        "title": "华微公司知识库：人工复核实体与关系",
        "kind": "curated_graph_review",
        "included": True,
        "path": "documents/graph-curated-review.md",
        "evidence_boundary": "人工复核种子；关系需要回到原始逐字稿核对，不把规则候选当事实。",
    })
    manifest["graph"] = {
        "status": "curated",
        "entities": "graph/curated_entities.json",
        "edges": "graph/curated_edges.json",
        "rule_candidate_policy": "needs_review",
        "minimum_acceptance": {"core_entity_recall": 0.90, "candidate_noise_rate": 0.10},
    }

    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("huawi-source-materials"))
    parser.add_argument("--output", type=Path, default=Path("fixtures/huawi-company-kb-v1"))
    args = parser.parse_args()
    manifest = build(args.source, args.output)
    print(json.dumps({"fixture_id": manifest["fixture_id"], "documents": len(manifest["documents"]), "excluded": len(manifest["excluded"]), "media": len(manifest["media"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
