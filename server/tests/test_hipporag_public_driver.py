"""Offline driver wiring: real public inputs, fake models/processes, no quality claim."""
import copy
import importlib.util
import json
from pathlib import Path
import signal
import subprocess
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[2]
PIN = "d5c8329422e0a0b834a15874545cb6a74b4f9b26"


@pytest.fixture
def driver():
    spec = importlib.util.spec_from_file_location("public_driver_contract", ROOT / "scripts/verify_hipporag_public.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(monkeypatch, driver, output, *, worker=False):
    args = [str(ROOT / "scripts/verify_hipporag_public.py"), "--allow-authorized-model-calls", "--output-dir", str(output)]
    if worker:
        args.append("--worker")
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(driver, "DATA_ROOT", output.parent)


@pytest.mark.parametrize("interruption", ["keyboard", "sigterm"])
def test_parent_interrupt_terminates_owner_then_waits_without_blind_kill(driver, tmp_path, monkeypatch, interruption):
    output = tmp_path / "run"
    arguments(monkeypatch, driver, output)
    events, handlers, launches = [], {}, []
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_SECRET_NOT_FOR_CHILD")
    monkeypatch.setattr(driver.signal, "signal", lambda kind, handler: handlers.__setitem__(kind, handler))
    monkeypatch.setattr(driver.subprocess, "run", lambda *args, **kwargs: events.append("source_preflight"))
    monkeypatch.setattr(driver.os, "killpg", lambda *args: pytest.fail("Parent must not blindly kill the worker group"))

    class Process:
        waits = 0

        def wait(self):
            self.waits += 1
            events.append("wait")
            if self.waits == 1:
                if interruption == "sigterm":
                    handlers[signal.SIGTERM](signal.SIGTERM, None)
                raise KeyboardInterrupt()
            return 143

        def terminate(self):
            events.append("TERM")

        def kill(self):
            pytest.fail("Worker must be allowed to unwind and reap its owned child")

    def launch(*args, **kwargs):
        events.append("launch")
        launches.append((args, kwargs))
        return Process()

    monkeypatch.setattr(driver.subprocess, "Popen", launch)
    assert driver.main() == 143
    assert events == ["source_preflight", "launch", "wait", "TERM", "wait"]
    command = launches[0][0][0]
    assert command[1:4] == ["-I", "-S", "-B"] and "--worker" in command
    assert launches[0][1]["start_new_session"] is True
    assert "OPENAI_API_KEY" not in launches[0][1]["env"]
    assert launches[0][1]["env"]["HOME"] == str(output / "home")


def test_source_preflight_failure_reports_zero_models_and_never_launches(driver, tmp_path, monkeypatch):
    output = tmp_path / "run"
    arguments(monkeypatch, driver, output)

    def failed_source(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "source-check", output="NOT_PUBLIC_EXCEPTION_PAYLOAD")

    monkeypatch.setattr(driver.subprocess, "run", failed_source)
    monkeypatch.setattr(driver.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("No worker before source verification"))
    assert driver.main() == 1
    report = driver.read_json(output / "report.json")
    assert report["status"] == "INCOMPLETE" and report["stage"] == "source_preflight"
    assert report["external_model_calls"] == 0
    assert "NOT_PUBLIC" not in json.dumps(report)


def test_worker_preflight_failure_is_reported_without_calling_model(driver, tmp_path, monkeypatch):
    output = tmp_path / "run"
    output.mkdir()
    arguments(monkeypatch, driver, output, worker=True)
    original_json = driver.read_json
    installation = ROOT / ".runtime/hipporag-env/installation.json"
    monkeypatch.setattr(driver, "read_json", lambda path: {"shared_site": str(tmp_path)}
                        if path == installation else original_json(path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(driver.signal, "signal", lambda *args: None)
    verifier = types.ModuleType("verify_hipporag_official")

    def failed_identity(*args):
        raise RuntimeError("NOT_PUBLIC_PREFLIGHT_PAYLOAD")

    verifier.source_identity = failed_identity
    verifier.shared_dependency_site = lambda *args: pytest.fail("No dependencies or models after failed source identity")
    monkeypatch.setitem(sys.modules, "verify_hipporag_official", verifier)
    assert driver.main() == 1
    report = original_json(output / "report.json")
    assert report["status"] == "INCOMPLETE" and report["stage"] == "preflight"
    assert report["quality_evaluated"] is False
    assert "NOT_PUBLIC" not in json.dumps(report)


@pytest.fixture
def worker_dependencies(driver, tmp_path, monkeypatch):
    """Every inference boundary is replaced; no optional model package is imported."""
    monkeypatch.syspath_prepend(str(ROOT / "server"))
    state = types.SimpleNamespace(events=[], model_inputs=[], embeddings=[], searches=[], visuals=[],
                                  ranking_inputs=[], rows=None, backend=None, close_error=False,
                                  clip_status="measured", preflight_error=False, missing_result=False)
    weights = tmp_path / "fake-weight-metadata-only"
    weights.mkdir()
    (weights / "revision.json").write_text('{"fixture":"offline-no-weights"}')
    monkeypatch.setattr(driver, "WEIGHTS", weights)

    def install(name, **fields):
        module = types.ModuleType(name)
        module.__dict__.update(fields)
        monkeypatch.setitem(sys.modules, name, module)

    install("verify_hipporag_official", source_identity=lambda source: {"commit": PIN, "fixture": "fake-source-preflight"},
            shared_dependency_site=lambda python: tmp_path / "fake-packages")
    install("app.hipporag", PINNED_COMMIT=PIN)

    class Scores(list):
        def __neg__(self):
            return Scores(-value for value in self)

        def argsort(self):
            return sorted(range(len(self)), key=self.__getitem__)

    class Dense(list):
        def __matmul__(self, vector):
            return Scores(sum(a * b for a, b in zip(row, vector)) for row in self)

    class Embedding:
        def __init__(self, path):
            assert path == str(weights)
            state.events.append("embedding_constructed")

        def batch_encode(self, texts, instruction=None):
            state.events.append("embedding_query" if instruction == "query" else "embedding_documents")
            state.embeddings.append((copy.deepcopy(texts), instruction))
            if instruction == "query":
                return [[1.0, 0.0] for text in texts]
            return Dense([[1 / (index + 1), 0.0] for index, text in enumerate(texts)])

    class LLM:
        def __init__(self, *, trace_path, budget_path):
            self.trace_path, self.budget_path = trace_path, budget_path

        def infer(self, messages, **kwargs):
            state.events.append("preflight_llm")
            state.model_inputs.append((copy.deepcopy(messages), kwargs))
            if state.preflight_error:
                raise RuntimeError("NOT_PUBLIC_MODEL_PAYLOAD")
            common = {"call_id": "a" * 32, "sequence": 1, "prompt_sha256": "b" * 64,
                      "model": "tencent-cloud/deepseek-v4.1-flash-1017"}
            records = [{**common, "event": "llm_attempt"}]
            if not state.missing_result:
                records.append({**common, "event": "llm_result", "status": "ok", "finish_reason": "stop",
                                "usage": None, "usage_status": "missing"})
            self.trace_path.write_text("".join(json.dumps(record) + "\n" for record in records))
            self.budget_path.write_text(json.dumps({"schema": "hipporag-public-budget/v1", "limit": 64, "used": 1}))
            return '{"ready":true}', {"fixture": "offline"}

    class Backend:
        def __init__(self, **kwargs):
            state.backend = self
            self.settings = kwargs

        def build(self, rows, *, identity, index_dir):
            state.events.append("build")
            state.rows = copy.deepcopy(rows)
            index_dir.mkdir()
            # Deliberately empty extraction: expected annotations do not become output.
            (index_dir / "openie_results_ner_fixture.json").write_text(json.dumps({
                "docs": [{"passage": row["text"], "extracted_entities": [], "extracted_triples": []} for row in rows]}))
            return {"fixture": "offline-fake-backend", "source_count": len(rows)}

        def search(self, query, *, rows, limit):
            state.events.append("search")
            state.searches.append((query, copy.deepcopy(rows), limit))
            return {row["id"]: float(len(rows) - index) for index, row in enumerate(rows)}, {"fixture": "offline"}

        def seal(self):
            state.events.append("seal")
            return {"schema": "synthetic-offline-receipt-not-product-evidence"}

        def close(self):
            state.events.append("close")
            if state.close_error:
                raise RuntimeError("NOT_PUBLIC_CLEANUP_PAYLOAD")

    def collect_visual(images, output):
        state.events.append("visual")
        state.visuals.append(copy.deepcopy(images))
        return {"records": [{"image_id": image["id"], "caption": "Synthetic test-only caption " + image["id"],
            "entities": [], "relations": [], "status": status, "usage": None}
            for image, status in zip(images, ("measured_candidate", "partial_candidate"))]}

    def image_rankings(images, questions):
        state.events.append("clip")
        state.ranking_inputs.append(copy.deepcopy(questions))
        # Even contradictory failure output must not activate the fusion branch.
        return {"status": state.clip_status, "rankings": [{"question_id": question["id"],
            "results": [{"image_id": image["id"], "score": 1 / (rank + 1)} for rank, image in enumerate(images)]}
            for question in questions]}

    install("app.hipporag_process", HippoRAGProcessAdapter=Backend)
    install("hipporag_public_models", LocalE5Embedding=Embedding, LoopbackLLM=LLM)
    install("hipporag_public_visual", collect_visual_evidence=collect_visual, image_rankings=image_rankings)
    return state


def run_worker(driver, tmp_path):
    output = tmp_path / "diagnostic"
    output.mkdir()
    return output, driver.worker(output)


def test_worker_projects_queries_keeps_gold_out_of_index_and_uses_measured_visual_contracts(
        driver, tmp_path, worker_dependencies):
    state = worker_dependencies
    output, report = run_worker(driver, tmp_path)
    original = driver.lines(driver.PILOT / "evaluation/questions.jsonl")
    projected = [{"id": q["id"], "question": q["question"]} for q in original]
    passages = driver.lines(driver.PILOT / "passages.jsonl")
    images = driver.read_json(driver.PILOT / "images.json")
    assert report["status"] == "COMPLETED_DIAGNOSTIC"
    assert report["scope"] == "public_industrial_diagnostic_not_acceptance"
    assert report["independent_human_gold"] is False and report["production_changes"] is False
    assert state.ranking_inputs == [projected]
    assert [item[0] for item in state.searches] == [q["question"] for q in original]
    assert state.embeddings[1] == ([q["question"] for q in original], "query")
    assert len(state.rows) == len(passages) + 1
    assert state.rows[-1]["id"] == images[0]["id"] and state.rows[-1]["sha256"] == images[0]["sha256"]
    assert "Model-derived image evidence candidate, not a source quotation." in state.rows[-1]["text"]
    assert images[1]["id"] not in {row["id"] for row in state.rows}
    assert [row["text"] for row in state.rows[:-1]] == [row["text"] for row in passages]
    wire = json.dumps([state.model_inputs, state.rows, state.embeddings, state.searches, state.ranking_inputs], ensure_ascii=False)
    assert all(key not in wire for key in ("reference_answer", "expected_entity_ids", "required_evidence", "why_two_sources"))
    assert all(question["reference_answer"] not in wire for question in original)
    assert state.events[-1] == "close"
    assert report["entities"]["matched"] == 0  # Fake extraction cannot inherit gold aliases.
    assert report["model_calls"]["reported_usage"] is None
    assert driver.read_json(output / "report.json") == report

    original_rank = [row["id"] for row in state.rows]
    rrf = {key: 1 / (60 + rank) for rank, key in enumerate(original_rank, 1)}
    for rank, image in enumerate(images, 1):
        rrf[image["id"]] = rrf.get(image["id"], 0) + 1 / (60 + rank)
    expected_order = sorted(rrf, key=lambda key: (-rrf[key], key))[:6]
    fusion = report["retrieval"]["official_plus_clip_rrf"]
    assert all(row["retrieved"] == expected_order for row in fusion["questions"])
    assert all(row["retrieved"] == original_rank[:6]
               for row in report["retrieval"]["official_with_model_image_candidates"]["questions"])


def test_unavailable_clip_does_not_claim_fusion_despite_present_results(driver, tmp_path, worker_dependencies):
    worker_dependencies.clip_status = "embedding_unavailable"
    _, report = run_worker(driver, tmp_path)
    assert report["status"] == "COMPLETED_DIAGNOSTIC"
    assert report["clip"]["status"] == "embedding_unavailable"
    assert report["clip"]["rankings"]
    assert "official_plus_clip_rrf" not in report["retrieval"]


def test_close_failure_makes_finished_diagnostic_incomplete_without_payload_leak(driver, tmp_path, worker_dependencies):
    worker_dependencies.close_error = True
    output, report = run_worker(driver, tmp_path)
    assert "retrieval" in report
    assert report["status"] == "INCOMPLETE" and report["cleanup_error_type"] == "RuntimeError"
    assert "NOT_PUBLIC" not in json.dumps(report)
    assert driver.read_json(output / "report.json")["status"] == "INCOMPLETE"
    assert worker_dependencies.events.count("close") == 1


def test_failed_model_preflight_never_calls_visual_or_builds_index(driver, tmp_path, worker_dependencies):
    worker_dependencies.preflight_error = True
    _, report = run_worker(driver, tmp_path)
    assert report["status"] == "INCOMPLETE" and report["error_type"] == "RuntimeError"
    assert worker_dependencies.visuals == [] and worker_dependencies.backend is None
    assert "NOT_PUBLIC" not in json.dumps(report)
    assert "retrieval" not in report and "entities" not in report


def test_missing_model_result_stops_before_retrieval_and_still_closes(driver, tmp_path, worker_dependencies):
    worker_dependencies.missing_result = True
    _, report = run_worker(driver, tmp_path)
    assert report["status"] == "INCOMPLETE" and report["model_calls"]["complete"] is False
    assert worker_dependencies.searches == [] and worker_dependencies.ranking_inputs == []
    assert worker_dependencies.events[-1] == "close"
    assert "retrieval" not in report and "entities" not in report
