#!/usr/bin/env python3
"""Re-run the public industrial fixture parsers and write bounded evidence.

This check is intentionally offline.  It only reads files already present in
``fixtures/industrial-public-v1`` and ``fixtures/industrial-public-v2`` and
uses the product's local ``DocumentParser``.  It does not call a model or
download anything.  A photo with no readable text is reported as a valid
``visual_only_ocr_empty`` asset rather than being presented as searchable text.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "fixtures" / "industrial-public-v1"
V2 = ROOT / "fixtures" / "industrial-public-v2"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def catalog_entries(root: Path) -> dict[str, dict]:
    payload = json.loads((root / "source-catalog.json").read_text(encoding="utf-8"))
    return {str(item["path"]): item for item in payload.get("downloaded", []) + payload.get("local_assets", [])}


def image_metadata(path: Path) -> dict:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return {"width": image.width, "height": image.height, "mode": image.mode, "format": image.format}
    except Exception as exc:  # pragma: no cover - diagnostic path for a corrupt image
        return {"error": f"{type(exc).__name__}: {exc}"[:240]}


def parse_one(parser, path: Path, fixture_root: Path, catalog: dict[str, dict]) -> dict:
    relative = path.relative_to(fixture_root).as_posix()
    actual_hash = sha256(path)
    item = catalog.get(relative, {})
    result = {
        "fixture": fixture_root.name,
        "path": relative,
        "bytes": path.stat().st_size,
        "sha256": actual_hash,
        "expected_sha256": item.get("sha256") or item.get("source_pdf_sha256"),
        "hash_match": actual_hash == (item.get("sha256") or item.get("source_pdf_sha256")),
        "source_url": item.get("url") or item.get("source_url"),
        "license": item.get("license"),
        "page_count": 0,
        "nonempty_pages": 0,
        "text_chars": 0,
        "extraction_counts": {},
        "status": "unknown",
        "text_searchable": False,
        "visual_asset_valid": False,
    }
    if path.suffix.lower() in IMAGE_SUFFIXES:
        result["image"] = image_metadata(path)
        result["visual_asset_valid"] = "error" not in result["image"]
    try:
        pages = parser.parse(path, path.name)
        extractions = Counter(str(page.get("extraction", "unknown")) for page in pages)
        result.update(
            page_count=len(pages),
            nonempty_pages=sum(bool((page.get("text") or "").strip()) for page in pages),
            text_chars=sum(len(page.get("text") or "") for page in pages),
            extraction_counts=dict(sorted(extractions.items())),
        )
        result["text_searchable"] = result["nonempty_pages"] > 0
        if path.suffix.lower() in IMAGE_SUFFIXES and not result["text_searchable"]:
            # The parser deliberately preserves textless pictures as visual
            # assets.  They are not text-searchable and must not be reported
            # as a successful OCR result.
            result["status"] = "visual_only_ocr_empty"
        else:
            result["status"] = "parsed_with_local_ocr" if "local-ocr" in extractions else "parsed"
        if path.suffix.lower() in IMAGE_SUFFIXES:
            result["visual_asset_valid"] = True
    except Exception as exc:
        result["error_code"] = getattr(exc, "code", None)
        result["error"] = str(getattr(exc, "message", exc))[:300]
        # An ordinary photo can be decoded and indexed by an image model even
        # when OCR returns no text.  Keep that distinction explicit.
        if path.suffix.lower() in IMAGE_SUFFIXES and result["visual_asset_valid"] and getattr(exc, "code", None) == "EMPTY_DOCUMENT":
            result["status"] = "visual_only_ocr_empty"
        elif getattr(exc, "code", None) == "OCR_REQUIRED":
            result["status"] = "needs_ocr"
        else:
            result["status"] = "failed"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/evidence/public-parse-recovery-2026-11-08.json")
    parser.add_argument("--strict", action="store_true", help="return non-zero for failed or needs_ocr entries")
    args = parser.parse_args()

    # Keep the script usable from a clean checkout while leaving dependency
    # installation to the caller; the evidence command uses the local runtime.
    sys.path.insert(0, str(ROOT / "server"))
    from app.knowledge import DocumentParser

    parser_runtime = DocumentParser()
    records = []
    for fixture_root, paths in (
        (V1, sorted((V1 / "documents").iterdir())),
        (V2, sorted((V2 / "images").iterdir())),
    ):
        catalog = catalog_entries(fixture_root)
        for path in paths:
            if path.is_file():
                records.append(parse_one(parser_runtime, path, fixture_root, catalog))

    summary = Counter(row["status"] for row in records)
    report = {
        "schema_version": "public-parse-recovery-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "offline_only": True,
        "customer_data": False,
        "external_model_calls": False,
        "source_policy": "Existing allowlisted fixture files only; no download in this check.",
        "summary": dict(sorted(summary.items())),
        "accepted_for_demo": all(row["hash_match"] and row["status"] in {"parsed", "parsed_with_local_ocr", "visual_only_ocr_empty"} for row in records),
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and any(row["status"] in {"failed", "needs_ocr"} or not row["hash_match"] for row in records):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
