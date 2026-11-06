"""Run a deterministic, offline synthetic industrial benchmark.

This runner is a stage-0 fixture for PROT-003 and BENCH-001/002.  It never
opens the product database, calls a model, contacts a network service, or
reads a production credential.  Missing model metrics are recorded as
``not_measured`` instead of being estimated.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import re
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "fixtures" / "synthetic-industrial-v1"
MANIFEST_PATH = FIXTURE_DIR / "manifest.json"
SCHEMA_VERSION = "synthetic-benchmark/v1"
PLATFORM_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class FixtureError(ValueError):
    """Raised when a fixture is missing, modified, or not synthetic."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError(f"cannot read JSON fixture {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FixtureError(f"JSON fixture must be an object: {path}")
    return value


def load_fixture(root: Path = FIXTURE_DIR) -> tuple[dict[str, Any], str]:
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("fixture_id") != "synthetic-industrial-v1":
        raise FixtureError("unexpected fixture_id")
    if manifest.get("synthetic") is not True:
        raise FixtureError("fixture manifest must set synthetic=true")
    if manifest.get("purpose") != "development_and_acceptance_only":
        raise FixtureError("fixture purpose must be development_and_acceptance_only")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise FixtureError("fixture manifest has no files")
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise FixtureError("fixture file entry is malformed")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise FixtureError(f"fixture path escapes root: {relative}")
        path = root / relative
        if not path.is_file():
            raise FixtureError(f"fixture file is missing: {relative}")
        expected = item.get("sha256")
        actual = sha256_bytes(path.read_bytes())
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise FixtureError(f"fixture hash is not finalized: {relative}")
        if actual != expected:
            raise FixtureError(f"fixture hash mismatch: {relative}")

    answers = read_json(root / "answers.json")
    if answers.get("fixture_id") != manifest["fixture_id"] or answers.get("synthetic") is not True:
        raise FixtureError("answer key is not bound to the synthetic fixture")
    tools = read_json(root / "mcp" / "tools.json")
    if tools.get("fixture_id") != manifest["fixture_id"] or tools.get("synthetic") is not True:
        raise FixtureError("MCP tool contract is not bound to the synthetic fixture")
    manifest_hash = sha256_bytes(manifest_path.read_bytes())
    return manifest, manifest_hash


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Source:
    kind: str
    path: str
    sha256: str
    locator: dict[str, Any]
    excerpt: str

    def as_dict(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": f"synthetic:{self.kind}:{self.sha256[:16]}",
            "synthetic": True,
            "fixture_id": manifest["fixture_id"],
            "fixture_version": manifest["fixture_version"],
            "kind": self.kind,
            "path": self.path,
            "sha256": self.sha256,
            "locator": self.locator,
            "excerpt": self.excerpt,
        }


class SyntheticIndustrialMCP:
    """Read-only deterministic tool contract backed by the fixture files."""

    def __init__(self, root: Path, manifest: dict[str, Any]):
        self.root = root
        self.manifest = manifest

    def _source(self, kind: str, relative: str, locator: dict[str, Any], excerpt: str) -> dict[str, Any]:
        path = self.root / relative
        return Source(kind, relative, sha256_bytes(path.read_bytes()), locator, excerpt).as_dict(self.manifest)

    def search_knowledge(self, query: str) -> dict[str, Any]:
        if not query.strip() or len(query) > 300:
            raise FixtureError("query must contain 1 to 300 characters")
        relative = "documents/quality-policy.md"
        text = (self.root / relative).read_text(encoding="utf-8")
        if not any(term in text for term in ("质量", "合格率", query.strip())):
            return {"synthetic": True, "fixture_id": self.manifest["fixture_id"], "documents": [], "sources": []}
        source = self._source(
            "knowledge",
            relative,
            {"section": "合成产线质量统计口径", "paragraph": 1},
            text[:800],
        )
        return {
            "synthetic": True,
            "fixture_id": self.manifest["fixture_id"],
            "query": query,
            "documents": [{"id": "SYN-KB-QUALITY-001", "text": text}],
            "sources": [source],
        }

    def query_quality_metrics(self) -> dict[str, Any]:
        relative = "tables/quality-batches.csv"
        path = self.root / relative
        with path.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        inspected = sum(int(row["inspected_count"]) for row in rows)
        qualified = sum(int(row["qualified_count"]) for row in rows)
        source = self._source(
            "data",
            relative,
            {
                "database": "synthetic-quality.csv",
                "table": "quality_batches",
                "fields": list(rows[0]),
                "filters": {"line": "L01", "production_date": {"from": "2026-11-01", "to": "2026-11-04", "inclusive": True}},
                "aggregation": "SUM(qualified_count) / SUM(inspected_count)",
                "timezone": "Asia/Shanghai",
            },
            canonical(rows),
        )
        return {
            "synthetic": True,
            "fixture_id": self.manifest["fixture_id"],
            "rows": rows,
            "inspected_count": inspected,
            "qualified_count": qualified,
            "yield_rate": qualified / inspected if inspected else None,
            "sources": [source],
        }

    def inspect_image(self) -> dict[str, Any]:
        relative = "images/quality-inspection.svg"
        path = self.root / relative
        raw = path.read_text(encoding="utf-8")
        source = self._source(
            "image",
            relative,
            {"media_type": "image/svg+xml", "region": "full_document", "visual_model": False},
            "合成产线质检示意图；视觉质量未测量。",
        )
        return {
            "synthetic": True,
            "fixture_id": self.manifest["fixture_id"],
            "media_type": "image/svg+xml",
            "byte_count": len(raw.encode("utf-8")),
            "title_present": "合成产线质检示意图" in raw,
            "visual_quality_status": "not_measured",
            "sources": [source],
        }


def checks_for(task_id: str, result: dict[str, Any], answer: str, answers: dict[str, Any]) -> list[dict[str, Any]]:
    expected = next(item["expected"] for item in answers["tasks"] if item["id"] == task_id)
    checks: list[dict[str, Any]] = []
    if task_id == "quality-yield":
        checks.extend([
            {"name": "inspected_count", "passed": result["inspected_count"] == expected["inspected_count"], "actual": result["inspected_count"], "expected": expected["inspected_count"]},
            {"name": "qualified_count", "passed": result["qualified_count"] == expected["qualified_count"], "actual": result["qualified_count"], "expected": expected["qualified_count"]},
            {"name": "yield_rate_rounded", "passed": f"{result['yield_rate']:.2%}" == expected["yield_rate_rounded"], "actual": f"{result['yield_rate']:.2%}", "expected": expected["yield_rate_rounded"]},
        ])
    elif task_id == "knowledge-procedure":
        checks.extend({"name": f"answer_contains:{term}", "passed": term in answer, "actual": term in answer, "expected": True} for term in expected["terms"])
    elif task_id == "session-memory":
        checks.extend([
            {"name": "memory_marker", "passed": expected["marker"] in answer, "actual": expected["marker"] in answer, "expected": True},
            {"name": "memory_scope", "passed": result.get("memory_scope") == expected["scope"], "actual": result.get("memory_scope"), "expected": expected["scope"]},
        ])
    elif task_id == "image-source":
        checks.extend([
            {"name": "media_type", "passed": result.get("media_type") == expected["media_type"], "actual": result.get("media_type"), "expected": expected["media_type"]},
            {"name": "visual_quality_status", "passed": result.get("visual_quality_status") == expected["visual_quality_status"], "actual": result.get("visual_quality_status"), "expected": expected["visual_quality_status"]},
        ])
    return checks


def source_checks(result: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    sources = result.get("sources", [])
    valid = bool(sources) and all(
        item.get("synthetic") is True and item.get("fixture_id") == manifest["fixture_id"] and item.get("source_id", "").startswith("synthetic:")
        for item in sources
    )
    return {"status": "measured", "registered": len(sources), "synthetic_marked": valid, "passed": bool(sources) and valid}


def run_task(task_id: str, mcp: SyntheticIndustrialMCP, answers: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    if task_id == "quality-yield":
        result = mcp.query_quality_metrics()
        answer = f"合成质量统计：检验 {result['inspected_count']} 件，合格 {result['qualified_count']} 件，加权合格率 {result['yield_rate']:.2%}。"
    elif task_id == "knowledge-procedure":
        result = mcp.search_knowledge("质量统计口径")
        answer = result["documents"][0]["text"] if result["documents"] else "未找到合成知识。"
    elif task_id == "session-memory":
        marker = next(item["expected"]["marker"] for item in answers["tasks"] if item["id"] == task_id)
        result = {
            "synthetic": True,
            "fixture_id": manifest["fixture_id"],
            "memory_scope": "session",
            "memory_recalled": True,
            "sources": [mcp._source("memory", "answers.json", {"scope": "session", "marker_policy": "fixed_fixture_marker"}, "合成会话记忆标记，仅用于验收。")],
        }
        answer = f"已在当前合成会话记住标记 {marker}。"
    elif task_id == "image-source":
        result = mcp.inspect_image()
        answer = "已登记合成图片来源；视觉质量指标待测。"
    else:
        raise FixtureError(f"unknown benchmark task: {task_id}")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    checks = checks_for(task_id, result, answer, answers)
    source = source_checks(result, manifest)
    return {
        "task_id": task_id,
        "status": "passed" if all(item["passed"] for item in checks) and source["passed"] else "failed",
        "answer": answer,
        "answer_sha256": sha256_bytes(answer.encode("utf-8")),
        "checks": checks,
        "source_coverage": source,
        "metrics": {
            "latency": {"status": "measured", "local_elapsed_ms": elapsed_ms, "time_to_first_byte_ms": None},
            "tokens": {"status": "not_measured", "input_tokens": None, "output_tokens": None, "cost_usd": None},
            "context": {"status": "not_measured", "reason": "no_model_or_sdk_session_in_offline_fixture"},
            "memory": {"status": "measured" if task_id == "session-memory" else "not_measured", "scope": result.get("memory_scope"), "recalled": result.get("memory_recalled")},
            "multimodal": {"status": "not_measured", "fixture_present": task_id == "image-source", "reason": "no_visual_model_in_stage0"},
        },
        "raw_evidence": {"result": result, "tool": next(item["tool"] for item in answers["tasks"] if item["id"] == task_id)},
    }


def build_report(platform_name: str) -> dict[str, Any]:
    if not PLATFORM_RE.fullmatch(platform_name):
        raise FixtureError("platform must contain only lowercase letters, digits, dot, underscore, or hyphen")
    manifest, manifest_hash = load_fixture()
    answers = read_json(FIXTURE_DIR / "answers.json")
    tools_path = FIXTURE_DIR / "mcp" / "tools.json"
    answers_path = FIXTURE_DIR / "answers.json"
    mcp = SyntheticIndustrialMCP(FIXTURE_DIR, manifest)
    task_records = [run_task(item["id"], mcp, answers, manifest) for item in answers["tasks"]]
    task_status = "passed" if all(item["status"] == "passed" for item in task_records) else "failed"
    run_id = sha256_bytes(canonical({"fixture": manifest["fixture_id"], "version": manifest["fixture_version"], "seed": manifest["seed"], "platform": platform_name}).encode("utf-8"))[:24]
    return {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "fixture": {"id": manifest["fixture_id"], "version": manifest["fixture_version"], "seed": manifest["seed"], "manifest_sha256": manifest_hash},
        "inputs": {
            "task_ids": [item["id"] for item in answers["tasks"]],
            "answers_sha256": sha256_bytes(answers_path.read_bytes()),
            "tool_contract_sha256": sha256_bytes(tools_path.read_bytes()),
            "comparison_key": sha256_bytes(canonical({"manifest": manifest_hash, "answers": sha256_bytes(answers_path.read_bytes()), "tools": sha256_bytes(tools_path.read_bytes()), "seed": manifest["seed"]}).encode("utf-8"))[:24],
        },
        "run": {"run_id": run_id, "platform": platform_name, "started_at": now(), "python": platform.python_version(), "transport": "offline-local-fixture"},
        "status": task_status,
        "tasks": task_records,
        "aggregate": {
            "task_count": len(task_records),
            "passed": sum(item["status"] == "passed" for item in task_records),
            "correctness_status": "measured",
            "source_coverage_status": "measured",
            "token_status": "not_measured",
            "context_status": "not_measured",
            "memory_status": "measured_for_session_memory_only",
            "multimodal_status": "not_measured",
        },
        "evidence_boundary": "Synthetic fixture only. No product database, production data, model, customer MCP, network, billing, or visual model was used. Missing performance metrics remain not_measured.",
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_report(path: Path) -> dict[str, Any]:
    report = read_json(path)
    errors: list[str] = []
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    if report.get("synthetic") is not True:
        errors.append("report is not synthetic")
    try:
        manifest, manifest_hash = load_fixture()
        if report.get("fixture", {}).get("manifest_sha256") != manifest_hash:
            errors.append("manifest hash mismatch")
        if report.get("fixture", {}).get("id") != manifest["fixture_id"]:
            errors.append("fixture id mismatch")
    except FixtureError as exc:
        errors.append(str(exc))
    tasks = report.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append("report has no task records")
    else:
        for task in tasks:
            if task.get("status") != "passed":
                errors.append(f"task failed: {task.get('task_id')}")
            if task.get("source_coverage", {}).get("synthetic_marked") is not True:
                errors.append(f"source is not synthetic: {task.get('task_id')}")
            if task.get("metrics", {}).get("tokens", {}).get("status") != "not_measured":
                errors.append(f"token boundary missing: {task.get('task_id')}")
    return {"status": "passed" if not errors else "failed", "errors": errors, "report": str(path)}


def clean_output(path: Path) -> list[str]:
    if path.suffix.lower() != ".json":
        raise FixtureError("clean output must be an explicitly named .json report")
    if path.exists() and not path.is_file():
        raise FixtureError("clean refuses to remove a directory; pass the report file path")
    removed: list[str] = []
    candidates = [path, path.with_suffix(".csv"), path.with_name(path.stem + ".raw")]
    for candidate in candidates:
        if candidate == path and candidate.is_dir():
            raise FixtureError("clean refuses to remove the report path directory")
        if candidate.is_dir() and candidate.name.endswith(".raw"):
            shutil.rmtree(candidate)
            removed.append(str(candidate))
        elif candidate.is_file():
            candidate.unlink()
            removed.append(str(candidate))
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="validate fixture and print the task/metric plan without writing")
    run_parser = sub.add_parser("run", help="run the offline benchmark and write a JSON report")
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--platform", default="synthetic-reference")
    verify_parser = sub.add_parser("verify", help="verify a generated report and fixture hashes")
    verify_parser.add_argument("--report", type=Path, required=True)
    clean_parser = sub.add_parser("clean", help="remove only the explicitly named generated report")
    clean_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest, manifest_hash = load_fixture()
        answers = read_json(FIXTURE_DIR / "answers.json")
        if args.command == "plan":
            print(json.dumps({"status": "ready", "synthetic": True, "fixture_id": manifest["fixture_id"], "fixture_version": manifest["fixture_version"], "seed": manifest["seed"], "manifest_sha256": manifest_hash, "tasks": answers["tasks"], "network": False, "model": False}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            report = build_report(args.platform)
            write_report(args.output, report)
            print(json.dumps({"status": report["status"], "report": str(args.output), "task_count": report["aggregate"]["task_count"], "passed": report["aggregate"]["passed"]}, ensure_ascii=False))
            return 0 if report["status"] == "passed" else 1
        if args.command == "verify":
            result = verify_report(args.report)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "passed" else 1
        removed = clean_output(args.output)
        print(json.dumps({"status": "cleaned", "removed": removed}, ensure_ascii=False))
        return 0
    except FixtureError as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"file error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
