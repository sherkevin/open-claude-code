"""Run the request-free acceptance gate before delivery-affecting changes.

The gate is intentionally local and deterministic.  It records which governed
files changed, replays the synthetic benchmark, and fails if that benchmark is
not green.  It does not claim customer, model, latency, or production evidence.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOVERNED_PREFIXES = (
    "server/app/identity.py",
    "server/app/knowledge.py",
    "server/app/hipporag.py",
    "server/app/hipporag_process.py",
    "server/app/artifacts.py",
    "server/app/benchmark.py",
    "server/app/capabilities.py",
    "server/app/feishu.py",
    "server/app/mcp_import.py",
    "server/app/mcp_tools.py",
    "server/app/model_recommendation.py",
    "server/app/openai_compat.py",
    "server/app/runtime.py",
    "server/app/harness_",
    "server/app/workspace_tools.py",
    "server/harness/",
    "server/app/security.py",
    "server/app/service.py",
    "server/app/model_approval.py",
    "server/app/high_risk_approval.py",
    "server/app/subagents.py",
    "server/app/workflows.py",
    "server/app/models.py",
    "server/app/db.py",
    "client/src/BenchmarkPage.tsx",
    "client/src/ChatPage.tsx",
    "client/src/KnowledgePage.tsx",
)


def changed_files(root: Path = ROOT) -> list[str]:
    """Include staged, unstaged, and untracked paths without reading contents."""
    commands = (
        ("git", "diff", "--name-only", "HEAD"),
        ("git", "ls-files", "--others", "--exclude-standard"),
    )
    paths: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=True)
        paths.update(item.strip() for item in result.stdout.splitlines() if item.strip())
    return sorted(paths)


def run_gate(root: Path = ROOT) -> dict:
    from synthetic_benchmark import build_report

    files = changed_files(root)
    governed = [path for path in files if path.startswith(GOVERNED_PREFIXES)]
    benchmark = build_report("local-gate")
    aggregate = benchmark.get("aggregate") if isinstance(benchmark.get("aggregate"), dict) else {}
    task_count = aggregate.get("task_count")
    passed_tasks = aggregate.get("passed")
    # Require the benchmark's aggregate to agree with its status. A malformed
    # or truncated report must never satisfy the release gate accidentally.
    aggregate_green = type(task_count) is int and type(passed_tasks) is int and task_count > 0 and passed_tasks == task_count
    status = "passed" if benchmark.get("status") == "passed" and benchmark.get("synthetic") is True and aggregate_green else "failed"
    return {
        "schema_version": "evaluation-gate/v1",
        "status": status,
        "synthetic": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "changed_files": files,
        "governed_changes": governed,
        "evaluation_required": bool(governed),
        "benchmark": {
            "status": benchmark.get("status"),
            "fixture_id": benchmark.get("fixture", {}).get("id"),
            "comparison_key": benchmark.get("inputs", {}).get("comparison_key"),
            "passed": passed_tasks,
            "task_count": task_count,
            "aggregate_green": aggregate_green,
        },
        "evidence_boundary": "仅重放本地合成夹具；未调用模型、客户系统、生产数据、凭据或网络服务。",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="write the JSON gate report")
    args = parser.parse_args(argv)
    try:
        report = run_gate()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(args.output), "evaluation_required": report["evaluation_required"]}, ensure_ascii=False))
        return 0 if report["status"] == "passed" else 1
    except (OSError, subprocess.CalledProcessError, ImportError) as exc:
        print(f"evaluation gate error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
