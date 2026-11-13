#!/usr/bin/env python3
"""Verify public pilot provenance without importing a model or calling a network."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def public_path(relative, corpus):
    path = (ROOT / relative).resolve()
    require(path.is_relative_to((ROOT / "fixtures" / corpus).resolve()), "Source escaped its public corpus")
    return path


def main():
    manifest = read_json(HERE / "source-manifest.json")
    require(manifest["independent_human_gold"] is False, "Pilot cannot claim independent gold")
    require(manifest["model_calls"] == 0, "This verifier does not certify model results")
    version = subprocess.run(["pdftotext", "-v"], capture_output=True, check=True, text=True).stderr
    require(version.splitlines()[0] == "pdftotext version " + manifest["extractor"]["version"],
            "Extraction version changed; review source offsets before updating")
    for name, expected in manifest["files"].items():
        path = (HERE / name).resolve()
        require(path.is_relative_to(HERE), "Fixture file escaped its directory")
        require(digest(path.read_bytes()) == expected, "Fixture file digest changed: " + name)

    sources = {source["source_id"]: source for source in manifest["sources"]}
    for source in sources.values():
        path = public_path(source["path"], "industrial-public-v1")
        require(digest(path.read_bytes()) == source["sha256"], "Original PDF digest changed")

    pages = {}
    passages = {row["id"]: row for row in read_lines(HERE / "passages.jsonl")}
    require(len(passages) == manifest["passage_count"], "Passage count mismatch")
    for row in passages.values():
        source = sources[row["source_id"]]
        require(row["source_sha256"] == source["sha256"], "Passage source digest mismatch")
        key = row["source_id"], row["physical_page"]
        if key not in pages:
            page = str(row["physical_page"])
            raw = subprocess.check_output([
                "pdftotext", "-enc", "UTF-8", "-f", page, "-l", page,
                str(public_path(source["path"], "industrial-public-v1")), "-",
            ])
            pages[key] = raw.decode("utf-8").replace("\f", "")
        content = pages[key]
        require(digest(content.encode()) == row["page_text_sha256"], "Canonical page digest changed")
        start, end = row["char_start"], row["char_end"]
        require(0 <= start < end <= len(content), "Passage span is invalid")
        require(content[start:end] == row["text"], "Passage is not the original source span")
        require(digest(row["text"].encode()) == row["text_sha256"], "Passage digest mismatch")

    images = {row["id"]: row for row in read_json(HERE / "images.json")}
    require(len(images) == manifest["image_count"], "Image count mismatch")
    for row in images.values():
        raw = public_path(row["path"], "industrial-public-v2").read_bytes()
        require(digest(raw) == row["sha256"], "Image digest changed")
        require(raw[:8] == b"\x89PNG\r\n\x1a\n", "Expected the reviewed PNG page image")
        require(struct.unpack(">II", raw[16:24]) == (row["width"], row["height"]), "Image size changed")
        require(row["source_sha256"] == sources[row["source_id"]]["sha256"], "Image PDF digest mismatch")

    labels = read_json(HERE / "evaluation/expectations.json")
    entities = {entity["id"]: entity for entity in labels["entities"]}
    for entity in entities.values():
        for mention in entity["mentions"]:
            passage = passages[mention["passage_id"]]
            require(passage["source_id"] == entity["source_scope"], "Entity crossed its source scope")
            require(passage["text"][mention["char_start"]:mention["char_end"]] == mention["text"],
                    "Entity mention does not match its source")
    for relation in labels["relations"]:
        require(relation["subject"] in entities and relation["object"] in entities, "Unknown relation entity")
        evidence = relation["evidence"]
        text = passages[evidence["passage_id"]]["text"]
        require(text[evidence["char_start"]:evidence["char_end"]] == evidence["quote"],
                "Relation quote does not match its source")

    regions = {}
    for annotation in labels["visual_annotations"]:
        image = images[annotation["image_id"]]
        for entity in annotation["entities"]:
            x, y, width, height = entity["bbox"]
            require(x >= 0 and y >= 0 and width > 0 and height > 0
                    and x + width <= image["width"] and y + height <= image["height"], "Invalid image box")
        regions[annotation["image_id"]] = {item["id"] for item in annotation["entities"]}
    questions = read_lines(HERE / "evaluation/questions.jsonl")
    require(len(questions) == manifest["question_count"], "Question count mismatch")
    require(len({question["id"] for question in questions}) == len(questions), "Duplicate question ID")
    for question in questions:
        require(question["independent_human_gold"] is False, "Question cannot claim independent gold")
        require(len(question["required_evidence"]) >= 2, "Question needs more than one evidence object")
        require(set(question["expected_entity_ids"]) <= entities.keys(), "Unknown expected entity")
        for evidence in question["required_evidence"]:
            if "passage_id" in evidence:
                require(evidence["passage_id"] in passages, "Unknown passage evidence")
            else:
                require(evidence["image_id"] in images, "Unknown image evidence")
                require(set(evidence["visual_entity_ids"]) <= regions[evidence["image_id"]], "Unknown visual entity")
    print(json.dumps({"status": "PASS", "scope": "source-integrity-only", "passages": len(passages),
                      "questions": len(questions), "images": len(images), "source_pages": len(pages),
                      "independent_human_gold": False, "model_calls": 0, "quality_evaluated": False}, indent=2))


if __name__ == "__main__":
    main()
