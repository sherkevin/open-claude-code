#!/usr/bin/env python3
"""Measure local CLIP text-to-image ranking on the public visual smoke set.

This script is deliberately offline. It loads the existing local checkpoint
through the product's VisionRuntime and reports the measured ranking; it never
downloads weights or sends images to an external provider.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "industrial-public-v2"
QUALITY = FIXTURE / "retrieval-quality.json"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, help="optional JSON report path")
    args = parser.parse_args()

    quality = json.loads(QUALITY.read_text(encoding="utf-8"))
    catalog = json.loads((FIXTURE / "source-catalog.json").read_text(encoding="utf-8"))
    assets = {item["asset_id"]: item for item in catalog["local_assets"]}
    sys.path.insert(0, str(ROOT / "server"))
    from app.vision import VisionRuntime

    runtime = VisionRuntime(ROOT / ".runtime" / "data" / "knowledge")
    status = runtime.status()
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture": quality["schema_version"],
        "offline_only": True,
        "independent_human_gold": quality["independent_human_gold"],
        "runtime": status,
        "assets": {asset_id: {"sha256": digest(FIXTURE / item["path"]), "path": item["path"]}
                   for asset_id, item in assets.items()},
        "cases": [],
    }
    if not status.get("model_path_exists"):
        report["status"] = "needs_model"
        _write(report, args.output)
        return 2

    image_vectors = {}
    for asset_id, item in assets.items():
        result = runtime.embed_image(FIXTURE / item["path"])
        if not result:
            report["status"] = "embedding_failed"
            report["runtime"] = runtime.status()
            _write(report, args.output)
            return 2
        image_vectors[asset_id] = result

    report["status"] = "measured"
    report["model"] = image_vectors[next(iter(image_vectors))]["model"]
    report["dimensions"] = image_vectors[next(iter(image_vectors))]["dimensions"]
    for case in quality["cases"]:
        text = runtime.embed_text(case["query"])
        if not text:
            report["status"] = "text_embedding_failed"
            report["runtime"] = runtime.status()
            _write(report, args.output)
            return 2
        scores = []
        for asset_id, result in image_vectors.items():
            score = sum(a * b for a, b in zip(text["vector"], result["vector"]))
            scores.append({"asset_id": asset_id, "cosine": round(score, 8)})
        scores.sort(key=lambda row: row["cosine"], reverse=True)
        ranking = [row["asset_id"] for row in scores]
        positive = case["positive_asset_id"]
        report["cases"].append({
            "case_id": case["case_id"],
            "query": case["query"],
            "positive_asset_id": positive,
            "hard_negative_asset_ids": case["hard_negative_asset_ids"],
            "negative_asset_ids": case["negative_asset_ids"],
            "ranking": ranking,
            "scores": scores,
            "positive_rank": ranking.index(positive) + 1,
            "hit_at_1": ranking[0] == positive,
        })
    report["summary"] = {
        "cases": len(report["cases"]),
        "hit_at_1": sum(case["hit_at_1"] for case in report["cases"]),
        "mean_reciprocal_rank": round(sum(1 / case["positive_rank"] for case in report["cases"]) / len(report["cases"]), 6),
    }
    _write(report, args.output)
    return 0


def _write(report: dict, output: Path | None) -> None:
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    sys.exit(main())
