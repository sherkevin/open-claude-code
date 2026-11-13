"""No provider calls: ensure diagnostic metrics cannot fabricate missing proof."""
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("public_metrics", ROOT / "scripts/verify_hipporag_public.py")
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)


def test_partial_evidence_is_not_complete_or_answer_accuracy():
    q = [{"id": "q", "kind": "composition", "required_evidence": [{"passage_id": "a"}, {"image_id": "image"}]}]
    result = metrics.evidence_metrics(q, {"q": ["a", "other"]})
    assert result["complete_count"] == 0
    assert result["mean_designated_coverage"] == .5
    assert "answer_accuracy" not in result


def test_entity_alias_cannot_cross_scope_or_match_substring():
    passages = [{"id": "a", "text": "original a"}, {"id": "b", "text": "original b"}]
    expected = {"entities": [{"id": "plc", "source_scope": "a", "canonical_name": "PLC", "aliases": [],
                              "mentions": [{"passage_id": "a"}]}]}
    extraction = [{"passage": "original a", "extracted_entities": ["PLC control"]},
                  {"passage": "original b", "extracted_entities": ["PLC"]}]
    assert metrics.entity_metrics(passages, extraction, expected)["matched"] == 0
    extraction[0]["extracted_entities"] = ["plc"]
    assert metrics.entity_metrics(passages, extraction, expected)["matched"] == 1


def test_fusion_uses_only_input_ranks_not_expected_evidence():
    assert set(metrics.fuse({"q": ["a", "b"]}, {"q": ["picture"]})["q"]) == {"a", "b", "picture"}


def test_visual_contract_only_measured_candidates_enter_index():
    images = metrics.read_json(metrics.PILOT / "images.json")
    records = [{"image_id": image["id"], "caption": "Model-only test caption", "entities": [], "relations": [],
                "status": status} for image, status in zip(images, ["measured_candidate", "partial_candidate"])]
    rows = metrics.rows_from_sources([], {"records": records})
    assert len(rows) == 1
    assert rows[0]["id"] == images[0]["id"]
    assert rows[0]["sha256"] == images[0]["sha256"]
    assert "Model-derived" in rows[0]["text"]
    assert "Model-only test caption" in rows[0]["text"]


@pytest.mark.parametrize("failure", [None, "missing_result", "duplicate_result", "wrong_id", "denied", "truncated", "reserved_not_sent", "missing_id", "wrong_sequence", "bool_sequence", "wrong_prompt", "wrong_model", "bool_budget"])
def test_ledger_completeness_requires_every_reserved_call(tmp_path, failure):
    common = {"call_id": "a" * 32, "sequence": 1, "prompt_sha256": "b" * 64,
              "model": "tencent-cloud/deepseek-v4.1-flash-1017"}
    attempt = {**common, "event": "llm_attempt"}
    result = {**common, "event": "llm_result", "status": "ok", "finish_reason": "stop",
              "usage": None, "usage_status": "missing"}
    records = [attempt, result]
    if failure == "missing_result": records.pop()
    if failure == "duplicate_result": records.append(result)
    if failure == "wrong_id": result["call_id"] = "2"
    if failure == "denied": records.append({"event": "llm_denied"})
    if failure == "truncated": result["finish_reason"] = "length"
    if failure == "missing_id": attempt.pop("call_id"); result.pop("call_id")
    if failure == "wrong_sequence": result["sequence"] = 2
    if failure == "bool_sequence": result["sequence"] = True
    if failure == "wrong_prompt": result["prompt_sha256"] = "c" * 64
    if failure == "wrong_model": result["model"] = "other"
    (tmp_path / "model-budget.json").write_text(json.dumps({"schema": "hipporag-public-budget/v1", "limit": 64,
        "used": True if failure == "bool_budget" else 2 if failure == "reserved_not_sent" else 1}))
    path = tmp_path / "model-trace.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    summary = metrics.model_trace_summary(path)
    assert summary["complete"] is (failure is None)
    assert summary["reported_usage"] is None
    assert summary["usage_complete_count"] == 0
