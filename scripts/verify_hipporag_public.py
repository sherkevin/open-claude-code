#!/usr/bin/env python3
"""Explicit, bounded live-model diagnostic on the frozen public NIST pilot.

This is not a production switch, client benchmark, or independent gold set.
Evaluation-only fields are never passed to retrieval or model prompts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "fixtures/hipporag-public-pilot-v1"
DATA_ROOT = Path("/media/data3/dengkw")
PYTHON = ROOT / ".runtime/hipporag-env/bin/python"
WEIGHTS = DATA_ROOT / "shared/models/multilingual-e5-small"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def normalization(value):
    return " ".join(re.findall(r"[\w]+", value.casefold()))


def evidence_metrics(questions, rankings, *, k=6):
    """Designated evidence coverage, not answer correctness or exhaustive recall."""
    result = []
    for question in questions:
        required = {e.get("passage_id", e.get("image_id")) for e in question["required_evidence"]}
        found = set(rankings[question["id"]][:k])
        result.append({"id": question["id"], "kind": question["kind"], "required": sorted(required),
                       "retrieved": rankings[question["id"]][:k], "hits": sorted(required & found),
                       "coverage": len(required & found) / len(required), "complete": required <= found})
    return {"k": k, "questions": result, "complete_count": sum(r["complete"] for r in result),
            "question_count": len(result), "mean_designated_coverage": sum(r["coverage"] for r in result) / len(result)}


def entity_metrics(passages, openie, expected):
    extracted = {row["passage"]: row for row in openie}
    by_id = {row["id"]: row for row in passages}
    result = []
    for entity in expected["entities"]:
        aliases = {normalization(value) for value in [entity["canonical_name"], *entity["aliases"]]}
        hits = []
        for pid in sorted({m["passage_id"] for m in entity["mentions"]}):
            candidates = extracted.get(by_id[pid]["text"], {}).get("extracted_entities", [])
            matches = [name for name in candidates if normalization(name) in aliases]
            if matches:
                hits.append({"passage_id": pid, "names": matches})
        result.append({"id": entity["id"], "scope": entity["source_scope"], "matched": bool(hits), "hits": hits})
    return {"metric": "scoped_exact_normalized_alias_entity_recall_not_precision", "entities": result,
            "matched": sum(r["matched"] for r in result), "expected": len(result),
            "recall": sum(r["matched"] for r in result) / len(result)}


def fuse(rankings, image_rankings):
    """Fixed RRF (k=60); ranks only, no gold-dependent weights or inserted hits."""
    combined = {}
    for question_id, original in rankings.items():
        score = {key: 1 / (60 + rank) for rank, key in enumerate(original, 1)}
        for rank, key in enumerate(image_rankings.get(question_id, []), 1):
            score[key] = score.get(key, 0) + 1 / (60 + rank)
        combined[question_id] = sorted(score, key=lambda key: (-score[key], key))
    return combined


def model_trace_summary(path):
    records = lines(path) if path.exists() else []
    results = [record for record in records if record.get("event") == "llm_result"]
    attempts = [record for record in records if record.get("event") == "llm_attempt"]
    denied = [record for record in records if record.get("event") == "llm_denied"]
    attempt_ids = [r.get("call_id") for r in attempts]
    result_ids = [r.get("call_id") for r in results]
    budget = read_json(path.parent / "model-budget.json") if (path.parent / "model-budget.json").exists() else {}
    ids_valid = all(type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) for value in [*attempt_ids, *result_ids])
    ledger_valid = (set(budget) == {"schema", "limit", "used"} and budget.get("schema") == "hipporag-public-budget/v1"
                    and type(budget.get("limit")) is int and budget["limit"] == 64
                    and type(budget.get("used")) is int and budget["used"] == len(attempts))
    pairs_valid = False
    if ids_valid and len(set(attempt_ids)) == len(attempt_ids) and sorted(attempt_ids) == sorted(result_ids):
        paired = {row["call_id"]: row for row in results}
        pairs_valid = (all(type(row.get("sequence")) is int for row in [*attempts, *results])
                       and sorted(row.get("sequence", -1) for row in attempts) == list(range(1, len(attempts) + 1))
                       and all(all(row.get(key) == paired[row["call_id"]].get(key)
                                   for key in ("sequence", "model", "prompt_sha256"))
                               and row.get("model") == "tencent-cloud/deepseek-v4.1-flash-1017"
                               and type(row.get("prompt_sha256")) is str
                               and bool(re.fullmatch(r"[0-9a-f]{64}", row["prompt_sha256"])) for row in attempts))
    totals = {}
    for record in results:
        for key, value in (record.get("usage") or {}).items():
            if type(value) is int:
                totals[key] = totals.get(key, 0) + value
    return {"attempts": len(attempts), "completed": len(results),
            "reserved": budget.get("used"), "denied": len(denied),
            "failed": sum(record.get("status") != "ok" for record in results),
            "usage_missing": sum(record.get("usage") is None for record in results),
            "usage_complete_count": sum(record.get("usage_status") == "reported" for record in results),
            "reported_usage": totals or None,
            "complete": bool(attempts) and not denied and ledger_valid and ids_valid and pairs_valid
            and all(r.get("status") == "ok" and r.get("finish_reason") == "stop" for r in results)}


def rows_from_sources(passages, visual):
    rows = [{"id": row["id"], "kb_id": "public-nist-pilot", "document_id": row["source_id"],
             "version": 1, "sha256": row["source_sha256"], "text": row["text"],
             "page": row["physical_page"], "start": row["char_start"], "end": row["char_end"],
             "name": row["source_id"]} for row in passages]
    # Captions are explicitly model-derived candidates tied to an actual image,
    # never original quotations or annotations relabeled as model extraction.
    image_sources = {row["id"]: row for row in read_json(PILOT / "images.json")}
    for item in visual["records"]:
        if item["status"] != "measured_candidate" or not item.get("caption"):
            continue
        original = image_sources[item["image_id"]]
        text = "Model-derived image evidence candidate, not a source quotation.\n" + json.dumps(
            {key: item[key] for key in ("caption", "entities", "relations")}, ensure_ascii=False)
        rows.append({"id": original["id"], "kb_id": "public-nist-pilot", "document_id": original["id"],
                     "version": 1, "sha256": original["sha256"], "text": text,
                     "page": original["physical_page"], "name": "Public NIST image candidate"})
    return rows


def worker(output):
    from verify_hipporag_official import source_identity, shared_dependency_site
    source = ROOT / ".runtime/third_party/HippoRAG"
    pin = source_identity(source)
    from app.hipporag import PINNED_COMMIT
    from app.hipporag_process import HippoRAGProcessAdapter
    from hipporag_public_models import LocalE5Embedding, LoopbackLLM
    from hipporag_public_visual import collect_visual_evidence, image_rankings

    manifest = read_json(PILOT / "source-manifest.json")
    if manifest["status"] != "frozen_diagnostic_pilot_not_holdout":
        raise ValueError("pilot_not_frozen")
    for name, expected in manifest["files"].items():
        if sha((PILOT / name).read_bytes()) != expected:
            raise ValueError("pilot_digest_changed")
    report = {"status": "RUNNING", "scope": "public_industrial_diagnostic_not_acceptance",
              "source": pin, "pilot_manifest_sha256": sha((PILOT / "source-manifest.json").read_bytes()),
              "independent_human_gold": False, "production_changes": False, "default_retrieval_changed": False,
              "stages": [], "limitations": [
                  "Two public NIST documents, eight assistant-authored diagnostic queries; not independent held-out gold.",
                  "Not all cross-passage questions are strict graph multi-hop reasoning.",
                  "Entity annotations are incomplete: recall is measured, precision is not inferred.",
                  "Image captions and relations are model candidates, not verified physical facts.",
                  "Fixed RRF adds a two-image ranking; this tiny pool cannot establish general multimodal gains.",
                  "No answer generation, no no-answer cases, no hallucination-rate or customer-quality claim.",
                  "Usage includes all reported indexing/retrieval calls; missing usage is not zero cost."]}
    trace = output / "model-trace.jsonl"
    backend = None
    try:
        passages = lines(PILOT / "passages.jsonl")
        images = read_json(PILOT / "images.json")
        embedding = LocalE5Embedding(str(WEIGHTS))
        probe = LoopbackLLM(trace_path=trace, budget_path=output / "model-budget.json")
        probe.infer([{"role": "user", "content": 'Reply only with the JSON object {"ready": true}.'}],
                    max_new_tokens=2048, response_format={"type": "json_object"})
        report["stages"].append("local_weights_and_authorized_text_route_preflight")
        # Strip evaluation-only fields before handing questions to any model.
        questions = [{"id": q["id"], "question": q["question"]} for q in lines(PILOT / "evaluation/questions.jsonl")]
        visual = collect_visual_evidence(images, output)
        report["visual"] = visual
        report["stages"].append("visual_model_completed")
        rows = rows_from_sources(passages, visual)
        write_json(output / "indexed-sources.json", rows)
        identity = {"upstream_commit": PINNED_COMMIT, **{
            name: sha(value) for name, value in {
                "parser_digest": (PILOT / "source-manifest.json").read_bytes(),
                "extraction_digest": (ROOT / "scripts/hipporag_public_models.py").read_bytes(),
                "embedding_digest": (WEIGHTS / "revision.json").read_bytes(),
                "chunking_digest": json.dumps(rows, sort_keys=True).encode(),
                "acl_digest": b"explicit-public-NIST-pilot-only-v1",
            }.items()}}
        paths = [ROOT / "server", ROOT / "scripts", source / "src",
                 PYTHON.absolute().parent.parent / "lib/python3.10/site-packages", shared_dependency_site(PYTHON),
                 Path("/usr/local/lib/python3.10/dist-packages"), Path("/usr/lib/python3/dist-packages")]
        backend = HippoRAGProcessAdapter(work_root=output, python_executable=PYTHON,
            factory_module="hipporag_public_models", factory_name="create_adapter", module_paths=paths,
            factory_kwargs={"weights_dir": str(WEIGHTS), "trace_path": str(trace),
                            "budget_path": str(output / "model-budget.json")}, allow_network=True,
            build_timeout_seconds=2400, search_timeout_seconds=90, control_timeout_seconds=30)
        start = time.monotonic()
        report["build"] = backend.build(rows, identity=identity, index_dir=output / "index")
        report["build_seconds"] = round(time.monotonic() - start, 3)
        if not model_trace_summary(trace)["complete"]:
            raise ValueError("model_extraction_incomplete")
        report["stages"].append("real_official_index_completed")
        rankings, query_details = {}, []
        for question in questions:
            start = time.monotonic()
            scores, stats = backend.search(question["question"], rows=rows, limit=len(rows))
            rankings[question["id"]] = sorted(scores, key=lambda key: (-scores[key], key))
            query_details.append({"id": question["id"], "elapsed_seconds": round(time.monotonic() - start, 3),
                                  "stats": stats, "ranked_ids": rankings[question["id"]], "scores": scores})
            if not model_trace_summary(trace)["complete"]:
                raise ValueError("model_retrieval_incomplete")
        write_json(output / "official-rankings.json", query_details)
        receipt = backend.seal()
        write_json(output / "trusted-host-receipt.json", receipt)
        report["stages"].append("real_official_retrieval_completed_and_sealed")
        dense = embedding.batch_encode([row["text"] for row in rows])
        queries = embedding.batch_encode([q["question"] for q in questions], instruction="query")
        dense_rankings = {q["id"]: [rows[int(i)]["id"] for i in (-(dense @ vector)).argsort()]
                          for q, vector in zip(questions, queries)}
        image_result = image_rankings(images, questions)
        report["clip"] = image_result
        clip_rankings = {r["question_id"]: [i["image_id"] for i in r["results"]]
                         for r in image_result.get("rankings", [])}
        # Only now load expected answers/entities for offline scoring.
        evaluation = lines(PILOT / "evaluation/questions.jsonl")
        expected = read_json(PILOT / "evaluation/expectations.json")
        files = list((output / "index").rglob("openie_results_ner_*.json"))
        if len(files) != 1:
            raise ValueError("openie_evidence_missing_or_ambiguous")
        openie = read_json(files[0])["docs"]
        write_json(output / "extracted-candidates.json", openie)
        report["entities"] = entity_metrics(passages, openie, expected)
        report["retrieval"] = {
            "official_with_model_image_candidates": evidence_metrics(evaluation, rankings),
            "local_e5_dense_same_sources": evidence_metrics(evaluation, dense_rankings),
        }
        if image_result.get("status") == "measured":
            report["retrieval"]["official_plus_clip_rrf"] = evidence_metrics(evaluation, fuse(rankings, clip_rankings))
        report["query_details"] = query_details
        report["status"] = "COMPLETED_DIAGNOSTIC"
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "INCOMPLETE"
        report["error_type"] = type(error).__name__
        report["error_code"] = getattr(error, "code", None)
        # Do not print provider exception strings or payloads.
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception as error:
                report["status"] = "INCOMPLETE"
                report["cleanup_error_type"] = type(error).__name__
        report["model_calls"] = model_trace_summary(trace)
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-authorized-model-calls", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.absolute()
    if not args.allow_authorized_model_calls:
        parser.error("Live authorized model calls require --allow-authorized-model-calls")
    if not output.resolve().is_relative_to(DATA_ROOT) or any(p.is_symlink() for p in (output, *output.parents)):
        parser.error("Output must be a non-symlink directory under /media/data3/dengkw")
    if args.worker:
        manifest = read_json(ROOT / ".runtime/hipporag-env/installation.json")
        sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "server"),
                       str(PYTHON.absolute().parent.parent / "lib/python3.10/site-packages"), manifest["shared_site"],
                       "/usr/local/lib/python3.10/dist-packages", "/usr/lib/python3/dist-packages"]
        def terminate(_signum, _frame):
            raise InterruptedError("Public diagnostic interrupted")

        signal.signal(signal.SIGTERM, terminate)
        try:
            result = worker(output)
        except (Exception, KeyboardInterrupt) as error:
            result = {"status": "INCOMPLETE", "stage": "preflight", "error_type": type(error).__name__,
                      "error_code": getattr(error, "code", None), "quality_evaluated": False}
            write_json(output / "report.json", result)
        print(json.dumps({"status": result["status"], "report": str(output / "report.json")}))
        return 0 if result["status"] == "COMPLETED_DIAGNOSTIC" else 1
    if output.exists():
        parser.error("Output must be fresh; no automatic retry or reuse of paid model calls")
    output.mkdir(parents=True, mode=0o700)
    for name in ("home", "tmp", "cache"):
        (output / name).mkdir(mode=0o700)
    try:
        subprocess.run([sys.executable, "-B", str(PILOT / "verify_sources.py")], check=True)
    except Exception as error:
        write_json(output / "report.json", {"status": "INCOMPLETE", "stage": "source_preflight",
                   "error_type": type(error).__name__, "external_model_calls": 0})
        return 1
    env = {"PATH": "/usr/bin:/bin", "HOME": str(output / "home"), "TMPDIR": str(output / "tmp"),
           "XDG_CACHE_HOME": str(output / "cache"), "HF_HOME": str(output / "cache/hf"),
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
           "PYTHONDONTWRITEBYTECODE": "1", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "MKL_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": "", "LITELLM_LOCAL_MODEL_COST_MAP": "True"}
    with (output / "worker.log").open("w") as log:
        def terminate_parent(_signum, _frame):
            raise KeyboardInterrupt()

        signal.signal(signal.SIGTERM, terminate_parent)
        process = subprocess.Popen([str(PYTHON), "-I", "-S", "-B", str(Path(__file__).resolve()),
            "--worker", "--allow-authorized-model-calls", "--output-dir", str(output)],
            cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            exit_code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            # The worker's TERM handler unwinds the process adapter, which owns
            # and reaps its separate child session. Do not kill the owner first.
            exit_code = process.wait()
    exists = (output / "report.json").exists()
    print(json.dumps({"exit_code": exit_code, "report": str(output / "report.json") if exists else None}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
