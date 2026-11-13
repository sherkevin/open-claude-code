#!/usr/bin/env python3
"""Run pinned official HippoRAG with synthetic models and Python IO audit guards.

This exercises real algorithms, not model quality, customer data, or a deployment.
Optional dependencies live in .runtime/hipporag-env, never the application runtime.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
PINNED_COMMIT = "d5c8329422e0a0b834a15874545cb6a74b4f9b26"
PINNED_LICENSE_SHA256 = "93b43d692b033b76129504448695bfe76ef22d18b11e51352bfab4b5622e5aaa"
BUNDLED_LICENSE = ROOT / "server/LICENSE.HippoRAG"
SCOPE = "official-algorithms-synthetic-models"


def require(condition, description):
    if not condition:
        raise RuntimeError(description)


def license_identity(source):
    """Check both upstream and distributed notices before importing its code."""
    for path in (source / "LICENSE", BUNDLED_LICENSE):
        require(not path.is_symlink() and path.is_file(), "HippoRAG LICENSE is missing or is a symlink")
        with path.open("rb") as notice:
            contents = notice.read(65_537)
        require(len(contents) <= 65_536, "HippoRAG LICENSE exceeds the verification limit")
        require(hashlib.sha256(contents).hexdigest() == PINNED_LICENSE_SHA256,
                "HippoRAG LICENSE differs from the reviewed original")
    return {"license": "MIT", "license_sha256": PINNED_LICENSE_SHA256}


def source_identity(source, expected_commit=PINNED_COMMIT):
    license_record = license_identity(source)
    command = ["git", "-C", str(source)]
    commit = subprocess.check_output(command + ["rev-parse", "HEAD"], text=True,
                                     stderr=subprocess.DEVNULL).strip()
    require(commit == expected_commit, "Upstream commit does not match the reviewed pin")
    tracked = subprocess.check_output(command + ["ls-tree", "-r", "--name-only", "-z", commit, "--", "src/hipporag"],
                                      stderr=subprocess.DEVNULL).decode().split("\0")
    files = sorted(path for path in tracked if path.endswith(".py"))
    actual = sorted(path.relative_to(source).as_posix()
                    for path in (source / "src/hipporag").rglob("*.py"))
    require(files and actual == files, "Upstream Python file set differs from the reviewed source")
    digest = hashlib.sha256()
    for relative in files:
        path = source / relative
        require(not path.is_symlink(), "Upstream Python sources cannot be symlinks")
        contents = path.read_bytes()
        committed = subprocess.check_output(command + ["show", f"{commit}:{relative}"],
                                             stderr=subprocess.DEVNULL)
        require(contents == committed, "Upstream Python source has unreviewed modifications")
        digest.update(relative.encode("ascii") + b"\0" + contents + b"\0")
    return {"commit": commit, "source_sha256": digest.hexdigest(), "python_files": len(files),
            **license_record}


def synthetic_rows(documents):
    rows = []
    # The duplicate is a different local document and must retain its own citation.
    for index, text in enumerate([*documents, documents[0]]):
        rows.append({
            "id": f"fixture-chunk-{index}", "kb_id": "synthetic-kb",
            "document_id": f"fixture-document-{index}", "version": 1,
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text,
            "page": 1, "paragraph": 1, "start": 0, "end": len(text),
        })
    return rows


def adapter_identity():
    identity = {"upstream_commit": PINNED_COMMIT}
    for component in ("parser", "extraction", "embedding", "chunking", "acl"):
        identity[f"{component}_digest"] = hashlib.sha256(f"synthetic-{component}-v1".encode()).hexdigest()
    return identity


def shared_dependency_site(python, explicit=None):
    """Resolve the install-time dependency path, never the caller's HOME."""
    if explicit is not None:
        path = Path(explicit).resolve()
    else:
        # Do not resolve the interpreter symlink: its parent identifies the venv.
        manifest = Path(python).absolute().parent.parent / "installation.json"
        require(manifest.is_file() and manifest.stat().st_size <= 1_000_000,
                "Isolated environment installation manifest is missing or oversized")
        configured = json.loads(manifest.read_text(encoding="utf-8")).get("shared_site")
        require(isinstance(configured, str) and Path(configured).is_absolute(),
                "Installation manifest needs an absolute shared_site; or pass --shared-site explicitly")
        path = Path(configured).resolve()
    require(path.is_dir(), "Configured shared dependency directory is unavailable")
    return path


def run_worker(source, directory, shared_site):
    identity = source_identity(source)
    # Only code packages are shared, after the isolated environment's own packages.
    # HOME and provider environment variables are not inherited by this process.
    isolated_site = Path(sys.prefix) / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    require(str(isolated_site) in sys.path, "Worker is not using the isolated environment")
    sys.path.insert(sys.path.index(str(isolated_site)) + 1, str(shared_site))
    sys.path[:0] = [str(source / "src"), str(ROOT / "server"), str(ROOT / "server/tests")]
    blocked = Counter()

    def reject_external_io(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen"}:
            blocked[event] += 1
            raise PermissionError("External IO is forbidden in the synthetic verification worker")

    sys.addaudithook(reject_external_io)
    checks = [{"name": "reviewed_upstream_source", "status": "PASS"}]
    with redirect_stdout(sys.stderr):
        from hipporag import Chunk, HippoRAG
        from hipporag.information_extraction.openie_openai import OpenIE
        from hipporag.rerank import DSPyFilter
        from hipporag.utils.embed_utils import retrieve_knn
        from hipporag_official_fixture import DOCUMENT_FACTS, QUERY, SyntheticEmbedding, SyntheticLLM, make_engine

        require(Path(sys.modules[HippoRAG.__module__].__file__).resolve().is_relative_to(source),
                "Official implementation was not imported from the verified source")
        observed = Counter()
        code_names = {
            HippoRAG.index.__code__: "official_index",
            HippoRAG.retrieve.__code__: "official_retrieve",
            HippoRAG.graph_search_with_fact_entities.__code__: "official_graph_search",
            HippoRAG.run_ppr.__code__: "official_ppr",
            OpenIE.ner.__code__: "official_ner_parser",
            OpenIE.triple_extraction.__code__: "official_triple_parser",
            DSPyFilter.rerank.__code__: "official_fact_filter",
            retrieve_knn.__code__: "official_synonym_knn",
        }

        def observe(frame, event, arg):
            if event == "call" and frame.f_code in code_names:
                observed[code_names[frame.f_code]] += 1

        llm, embedding = SyntheticLLM(), SyntheticEmbedding()
        chunks = [Chunk(content=text, source_id=f"synthetic-{index}", metadata={"fixture": True})
                  for index, text in enumerate(DOCUMENT_FACTS)]
        sys.setprofile(observe)
        threading.setprofile(observe)
        try:
            with make_engine(save_dir=directory / "official", llm=llm, embedding=embedding) as engine:
                engine.index(chunks)
                result = engine.retrieve([QUERY], num_to_retrieve=len(chunks))[0]
                require(set(result.docs) == set(DOCUMENT_FACTS), "Official documents did not round-trip")
                require(len(result.doc_metadata) == len(chunks), "Official source metadata is missing")
                require(all(item.get("fixture") is True and item.get("source_id", "").startswith("synthetic-")
                            for item in result.doc_metadata), "Official metadata was not retained")
                require(bool(result.graph_seeds) and engine.ppr_time > 0, "Retrieval did not traverse the graph")
                require(bool(engine._synonym_edge_scores), "Synthetic synonym edges were not constructed")
                ranking = list(result.docs)
                graph = {"nodes": engine.graph.vcount(), "edges": engine.graph.ecount(),
                         "seed_count": len(result.graph_seeds), "synonym_edges": len(engine._synonym_edge_scores)}
            checks.append({"name": "official_index_openie_filter_synonyms_ppr", "status": "PASS"})
            checks.append({"name": "official_source_metadata_round_trip", "status": "PASS"})

            # Only this process-created, synthetic index is reopened. The product
            # adapter deliberately rejects arbitrary persisted graph pickle files.
            reopened_llm, reopened_embedding = SyntheticLLM(), SyntheticEmbedding()
            with make_engine(save_dir=directory / "official", llm=reopened_llm,
                             embedding=reopened_embedding) as reopened:
                again = reopened.retrieve([QUERY], num_to_retrieve=len(chunks))[0]
                require(again.docs == ranking and bool(again.graph_seeds), "Reopened index changed retrieval")
                require(reopened_llm.calls["ner"] == reopened_llm.calls["triples"] == 0,
                        "Reopened index unexpectedly extracted documents again")
                require(reopened_llm.calls["fact_filter"] > 0, "Reopened retrieval skipped fact filtering")
            checks.append({"name": "owned_synthetic_index_reopen", "status": "PASS"})

            from app.hipporag import HippoRAGAdapter
            adapter_llm, adapter_embedding = SyntheticLLM(), SyntheticEmbedding()

            def engine_factory(*, save_dir):
                return make_engine(save_dir=save_dir, llm=adapter_llm, embedding=adapter_embedding)

            adapter = HippoRAGAdapter(engine_factory=engine_factory, chunk_factory=Chunk)
            rows = synthetic_rows(list(DOCUMENT_FACTS))
            try:
                adapter.build(rows, identity=adapter_identity(), index_dir=directory / "adapter")
                scores, stats = adapter.search(QUERY, rows=rows, limit=len(rows))
                require(set(scores) == {row["id"] for row in rows}, "Adapter lost a local source")
                require(scores[rows[0]["id"]] == scores[rows[-1]["id"]], "Duplicate source score changed")
                require(adapter_llm.calls["ner"] == len(DOCUMENT_FACTS),
                        "Duplicate text was not deduplicated before official extraction")
            finally:
                adapter.close()
            checks.append({"name": "product_adapter_preserves_duplicate_sources", "status": "PASS"})
        finally:
            sys.setprofile(None)
            threading.setprofile(None)
        require(all(observed[name] > 0 for name in code_names.values()), "An official algorithm was bypassed")
        require(llm.unexpected_calls == reopened_llm.unexpected_calls == adapter_llm.unexpected_calls == 0,
                "Synthetic model received an unexpected request")
        require(not blocked, "An external IO attempt was blocked")
        checks.append({"name": "no_audited_python_io_or_unexpected_model_requests", "status": "PASS"})
        packages = {}
        for package in ("torch", "transformers", "openai", "litellm", "igraph", "numpy", "pandas",
                        "pyarrow", "pydantic", "boto3", "tiktoken", "tenacity", "einops", "nest_asyncio"):
            packages[package] = importlib.metadata.version(package)
        model_stages = {
            name: {"llm_calls": dict(model.calls), "embedding_calls": vectors.calls,
                   "embedded_texts": vectors.text_count}
            for name, model, vectors in (
                ("index_and_retrieve", llm, embedding),
                ("reopened_retrieve", reopened_llm, reopened_embedding),
                ("adapter_index_and_retrieve", adapter_llm, adapter_embedding),
            )
        }
        llm_totals = llm.calls + reopened_llm.calls + adapter_llm.calls
    return {
        "status": "PASS", "scope": SCOPE, "source": identity, "checks": checks,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "graph": graph, "official_calls_observed": dict(observed), "synthetic_llm_calls": dict(llm_totals),
        "adapter": stats,
        "synthetic_embedding_calls": sum(stage["embedding_calls"] for stage in model_stages.values()),
        "synthetic_model_stages": model_stages, "external_model_calls": 0,
        "external_io_attempts": dict(blocked), "dependencies": packages,
        "io_guard": "python-audit-hook-not-os-sandbox",
        "shared_dependency_site": str(shared_site),
        "limitations": [
            "Synthetic model outputs do not establish extraction quality or semantic retrieval quality.",
            "Reopen covers only an index created inside this isolated worker, not product index reuse.",
            "No customer data, real model endpoint, production deployment, or browser acceptance is exercised.",
            "Python audit guards are not an OS sandbox and do not cover native extensions or startup .pth code.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / ".runtime/third_party/HippoRAG")
    parser.add_argument("--python", type=Path, default=ROOT / ".runtime/hipporag-env/bin/python")
    parser.add_argument("--temp-root", type=Path, default=ROOT / ".runtime/hipporag-tmp")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--shared-site", type=Path,
                        help="Explicit trusted dependency directory; otherwise use the venv installation manifest")
    args = parser.parse_args()
    try:
        if args.worker:
            report = run_worker(args.source.resolve(), args.temp_root.resolve(), args.shared_site.resolve())
        else:
            source_identity(args.source.resolve())
            require(args.python.is_file(), "Optional isolated Python environment is not installed")
            shared_site = shared_dependency_site(args.python, args.shared_site)
            args.temp_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="official-hipporag-", dir=args.temp_root) as temporary:
                directory = Path(temporary).resolve()
                for child in ("home", "tmp", "cache"):
                    (directory / child).mkdir()
                env = {
                    "PATH": "/usr/bin:/bin", "HOME": str(directory / "home"),
                    "TMPDIR": str(directory / "tmp"), "XDG_CACHE_HOME": str(directory / "cache"),
                    "PYTHONPYCACHEPREFIX": str(directory / "cache/pycache"),
                    "HF_HOME": str(directory / "cache/huggingface"), "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                    "LITELLM_LOCAL_MODEL_COST_MAP": "True", "AWS_EC2_METADATA_DISABLED": "true",
                    "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
                    "MKL_NUM_THREADS": "1", "TOKENIZERS_PARALLELISM": "false",
                }
                result = subprocess.run(
                    [str(args.python.absolute()), "-s", "-B", str(Path(__file__).resolve()), "--worker",
                     "--source", str(args.source.resolve()), "--temp-root", str(directory),
                     "--shared-site", str(shared_site)],
                    env=env, cwd=directory, capture_output=True, text=True, timeout=180,
                )
                if result.stderr:
                    print(result.stderr, file=sys.stderr, end="")
                report = json.loads(result.stdout)
                require(result.returncode == 0 and report.get("status") == "PASS",
                        "Official isolated verification failed: " + str(report.get("error", "unknown")))
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "scope": SCOPE, "error": type(exc).__name__,
                          "detail": str(exc), "external_model_calls": 0}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
