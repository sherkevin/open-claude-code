import hashlib
import json
from pathlib import Path
import struct


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "industrial-public-v2"


def test_public_v2_manifest_is_source_bound_and_hashes_match():
    catalog = json.loads((FIXTURE / "source-catalog.json").read_text(encoding="utf-8"))
    assert catalog["customer_data"] is False
    assert catalog["purpose"].startswith("local_development")
    assert catalog["local_assets"]
    for item in catalog["local_assets"]:
        path = ROOT / "fixtures" / "industrial-public-v2" / item["path"]
        assert path.is_file(), item["asset_id"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == item["sha256"], item["asset_id"]
        assert item["source_url"].startswith(("https://", "http://"))
        assert item["license"]
        assert item["redistribution"]


def test_visual_entities_are_bounded_and_do_not_claim_semantic_relations():
    catalog = json.loads((FIXTURE / "source-catalog.json").read_text(encoding="utf-8"))
    acceptance = json.loads((FIXTURE / "entity-acceptance.json").read_text(encoding="utf-8"))
    assets = {item["asset_id"]: item for item in catalog["local_assets"]}
    annotations = (FIXTURE / "annotations" / "visual-entities.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(annotations) >= 3
    labels_by_asset = {}
    for line in annotations:
        row = json.loads(line)
        assert row["asset_id"] in assets
        assert row["image_sha256"] == assets[row["asset_id"]]["sha256"]
        assert row["relations"] == []
        assert row["relation_status"] == "evidence_only"
        labels_by_asset.setdefault(row["asset_id"], set()).update(item["label"] for item in row["entities"])
        for entity in row["entities"]:
            x, y, width, height = entity["bbox"]
            assert min(x, y) >= 0 and width > 0 and height > 0
            assert entity["status"] == "assistant_reviewed_demo"
            assert entity["text"]
        assert row["annotator"] == "assistant_reviewed_public_demo"
        assert "not an independent human gold label" in row["review_note"]
    for item in acceptance["acceptance"]:
        assert set(item["required_labels"]).issubset(labels_by_asset[item["asset_id"]])


def test_visual_retrieval_quality_set_is_small_source_bound_and_explicitly_non_gold():
    catalog = json.loads((FIXTURE / "source-catalog.json").read_text(encoding="utf-8"))
    quality = json.loads((FIXTURE / "retrieval-quality.json").read_text(encoding="utf-8"))
    asset_ids = {item["asset_id"] for item in catalog["local_assets"]}
    assert quality["independent_human_gold"] is False
    assert quality["ground_truth_status"] == "assistant_reviewed_demo"
    assert 2 <= len(quality["cases"]) <= 10
    for case in quality["cases"]:
        candidates = {case["positive_asset_id"], *case["hard_negative_asset_ids"], *case["negative_asset_ids"]}
        assert candidates <= asset_ids
        assert case["positive_asset_id"] not in set(case["hard_negative_asset_ids"]) | set(case["negative_asset_ids"])
        assert set(case["hard_negative_asset_ids"]).isdisjoint(case["negative_asset_ids"])
        assert case["query"].strip()


def test_scada_boxes_match_reviewed_full_page_regions_not_body_text():
    rows = [json.loads(line) for line in (FIXTURE / "annotations/visual-entities.jsonl").read_text().splitlines()]
    annotation = next(row for row in rows if row["asset_id"] == "nist-sp80082-scada-fig2")
    entities = {item["id"]: item for item in annotation["entities"]}
    # Fixed visual landmarks for this hash-pinned public page, not model accuracy.
    anchors = {
        "cc-1": (510, 565), "hmi-1": (379, 527), "ew-1": (538, 527), "hist-1": (710, 527),
        "plc-1": (298, 1004), "rtu-1": (660, 1004), "fs-1": (329, 1045),
        "fs-2": (510, 1045), "fs-3": (690, 1045),
    }
    assert set(entities) == set(anchors)
    raw = (FIXTURE / "images/nist-sp80082-scada-fig2.png").read_bytes()
    assert struct.unpack(">II", raw[16:24]) == (1020, 1320)
    for key, (anchor_x, anchor_y) in anchors.items():
        x, y, width, height = entities[key]["bbox"]
        assert x <= anchor_x < x + width and y <= anchor_y < y + height, key
        assert x >= 0 and y >= 0 and x + width <= 1020 and y + height <= 1320

    def contains(parent, child):
        px, py, pw, ph = entities[parent]["bbox"]
        cx, cy, cw, ch = entities[child]["bbox"]
        return px <= cx and py <= cy and cx + cw <= px + pw and cy + ch <= py + ph

    assert all(contains("cc-1", child) for child in ("hmi-1", "ew-1", "hist-1"))
    assert contains("fs-1", "plc-1") and contains("fs-3", "rtu-1")
    assert all(entities[key]["bbox"][1] >= 890 for key in ("fs-1", "fs-2", "fs-3"))
    assert annotation["annotation_version"] == "1.2.0"
    assert annotation["image_size"] == [1020, 1320]
    assert annotation["bbox_format"] == "xywh_pixels_full_page"
    assert annotation["independent_human_gold"] is False
