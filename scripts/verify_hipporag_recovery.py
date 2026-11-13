#!/usr/bin/env python3
"""Exercise actual upstream recovery and process interruption; no paid models."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

from verify_hipporag_official import ROOT, adapter_identity, shared_dependency_site, source_identity, synthetic_rows


def require(value, message):
    if not value:
        raise RuntimeError(message)


def events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def tree_bytes(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def gone(pid):
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def verify(directory, python, source):
    pin = source_identity(source)
    shared = shared_dependency_site(python)
    sys.path.insert(0, str(ROOT / "server"))
    from app.hipporag import HippoRAGError
    from app.hipporag_process import HippoRAGProcessAdapter

    documents = ["Pump P101 supplies valve V20.", "Valve V20 requires seal S7.",
                 "Seal S7 has a service interval of 90 days.", "Compressor C3 requires oil O9.",
                 "Pump P101 is also called MainPump."]
    rows, identity = synthetic_rows(documents), adapter_identity()
    query = "Which seal is required by the valve supplied by P101?"
    trace, hang = directory / "calls.jsonl", directory / "hang-model"
    module_paths = [ROOT / "server", ROOT / "server/tests", source / "src",
                    python.absolute().parent.parent / "lib/python3.10/site-packages", shared,
                    Path("/usr/local/lib/python3.10/dist-packages"), Path("/usr/lib/python3/dist-packages")]
    workers, pids, checks = [], [], []

    def backend():
        adapter = HippoRAGProcessAdapter(
            work_root=directory, python_executable=python,
            factory_module="hipporag_recovery_fixture", factory_name="create_adapter",
            factory_kwargs={"trace_path": str(trace), "hang_path": str(hang)},
            module_paths=module_paths, build_timeout_seconds=120, search_timeout_seconds=30,
            control_timeout_seconds=30,
        )
        workers.append(adapter)
        return adapter

    try:
        original = backend()
        original.build(rows, identity=identity, index_dir=directory / "index-original")
        pids.append(original.worker_pid)
        first, stats = original.search(query, rows=rows, limit=len(rows))
        require(len(first) == 6, "Build must preserve six local sources")
        receipt = original.seal()
        require(original.worker_pid is None and gone(pids[-1]), "Seal must reap the original worker")
        original.close()
        before = tree_bytes(directory / "index-original")
        checks.append("real_official_build_search_seal_and_worker_exit")

        restored = backend()
        restored.restore(rows, identity=identity, source_dir=directory / "index-original",
                         receipt=receipt, index_dir=directory / "index-restored")
        pids.append(restored.worker_pid)
        second, _ = restored.search(query, rows=rows, limit=len(rows))
        require(first == second, "Restoring must preserve scores and duplicate citations")
        require(tree_bytes(directory / "index-original") == before, "Recovery modified the sealed source")
        require(sum(e["event"] == "index" for e in events(trace)) == 1, "Restore must not re-index")
        checks.append("verified_copy_restore_without_reindex_or_source_mutation")

        hang.touch(mode=0o600)
        start = time.monotonic()
        try:
            restored.search(query, rows=rows, timeout_seconds=2)
            raise RuntimeError("Hung model unexpectedly returned")
        except HippoRAGError as exc:
            require(exc.code == "timeout", "Hung retrieval did not report timeout")
        timeout_seconds = round(time.monotonic() - start, 3)
        require(restored.worker_pid is None and gone(pids[-1]), "Timeout left its process alive")
        require(any(e["event"] == "blocked_model" for e in events(trace)), "Model hang was not reached")
        require(timeout_seconds < 5, "Termination exceeded its bounded cleanup allowance")
        hang.unlink()
        checks.append("real_model_boundary_timeout_reaps_worker")

        # Interrupted mutable copies are never reopened. Reuse the original receipt.
        restored.restore(rows, identity=identity, source_dir=directory / "index-original",
                         receipt=receipt, index_dir=directory / "index-after-timeout")
        pids.append(restored.worker_pid)
        third, _ = restored.search(query, rows=rows, limit=len(rows))
        require(third == first, "Explicit recovery after timeout changed results")
        checks.append("explicit_recovery_after_timeout")

        hang.touch(mode=0o600)
        cancellation = threading.Event()
        cancel_watcher_done = threading.Event()

        def cancel_at_model_boundary():
            deadline = time.monotonic() + 8
            while not cancel_watcher_done.wait(.02) and time.monotonic() < deadline:
                if any(e["event"] == "blocked_model" and e["pid"] == pids[-1] for e in events(trace)):
                    cancellation.set()
                    return

        timer = threading.Thread(target=cancel_at_model_boundary)
        timer.start()
        try:
            restored.search(query, rows=rows, cancel_event=cancellation, timeout_seconds=10)
            raise RuntimeError("Cancelled retrieval unexpectedly returned")
        except HippoRAGError as exc:
            require(exc.code == "cancelled", "Retrieval did not report cancellation")
        finally:
            cancel_watcher_done.set()
            timer.join()
        require(cancellation.is_set(), "Cancellation did not reach the current worker model boundary")
        require(restored.worker_pid is None and gone(pids[-1]), "Cancellation left its process alive")
        hang.unlink()
        checks.append("real_model_boundary_cancel_reaps_worker")

        prior_factories = sum(e["event"] == "factory" for e in events(trace))
        (directory / "index-original/manifest.json").write_text("{}")
        rejected = backend()
        try:
            rejected.restore(rows, identity=identity, source_dir=directory / "index-original",
                             receipt=receipt, index_dir=directory / "index-rejected")
            raise RuntimeError("Tampered index was accepted")
        except HippoRAGError as exc:
            require(exc.code == "index_untrusted", "Tampered index was not rejected as untrusted")
        require(sum(e["event"] == "factory" for e in events(trace)) == prior_factories,
                "Untrusted data reached an official engine constructor")
        require(sum(e["event"] == "index" for e in events(trace)) == 1, "Recovery re-indexed documents")
        checks.append("tamper_rejected_before_official_constructor")
    finally:
        for worker in workers:
            worker.close()

    require(all(gone(pid) for pid in pids), "A verification worker remains alive")
    return {"status": "PASS", "scope": "real-official-process-recovery-synthetic-models",
            "source": pin, "checks": checks, "source_count": stats["source_count"],
            "unique_text_count": stats["unique_text_count"], "returned_count": len(first),
            "real_index_calls": 1, "worker_processes": len(set(pids)), "all_workers_reaped": True,
            "timeout_elapsed_seconds": timeout_seconds, "external_model_calls": 0,
            "limitations": ["Synthetic model outputs are not industrial quality evidence.",
                            "Trusted host receipt; not an operating-system sandbox or signed attestation."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=ROOT / ".runtime/hipporag-tmp")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    python = ROOT / ".runtime/hipporag-env/bin/python"
    source = ROOT / ".runtime/third_party/HippoRAG"
    try:
        args.work_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="official-recovery-", dir=args.work_root) as directory:
            report = verify(Path(directory), python, source)
    except Exception as exc:
        report = {"status": "FAIL", "error_type": type(exc).__name__,
                  "error_code": getattr(exc, "code", None), "external_model_calls": 0}
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
