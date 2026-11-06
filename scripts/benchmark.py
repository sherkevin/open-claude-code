"""Run the same task set against a configured assistant and export reviewable evidence.

This invokes the configured real model and can incur provider charges. It never
approves pending write operations automatically. Business correctness is separate
from transport completion and must be reviewed against customer ground truth.
"""
from __future__ import annotations
import argparse
import csv
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--origin", default="http://127.0.0.1:18081")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--assistant-id", required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    tasks = json.loads(args.tasks.read_text(encoding="utf-8"))
    if not isinstance(tasks, list) or not tasks:
        parser.error("Tasks must be a nonempty JSON array")
    rows, traces = [], []
    with httpx.Client(base_url=args.base_url.rstrip("/") + "/api/v1", headers={"Origin": args.origin}, timeout=30) as client:
        login = client.post("/auth/login", json={"username": args.username, "password": args.password_file.read_text().strip()})
        login.raise_for_status()
        configuration = client.get("/config").json()
        assistant_response = client.get(f"/assistants/{args.assistant_id}")
        assistant_response.raise_for_status()
        assistant = assistant_response.json()
        for task in tasks:
            response = client.post("/conversations", json={"assistant_id": args.assistant_id, "title": f"测评：{task['id']}"})
            response.raise_for_status()
            conversation_id = response.json()["id"]
            for turn_index, prompt in enumerate(task["turns"], 1):
                started = time.monotonic()
                response = client.post(f"/conversations/{conversation_id}/messages", json={"client_request_id": str(uuid.uuid4()), "text": prompt, "file_ids": []})
                response.raise_for_status()
                run_id = response.json()["run_id"]
                deadline = started + args.timeout
                denied = set()
                while True:
                    response = client.get(f"/runs/{run_id}")
                    response.raise_for_status()
                    run = response.json()
                    for approval in run.get("approvals", []):
                        if approval["id"] not in denied and approval.get("status") == "pending":
                            decision = client.post(f"/approvals/{approval['id']}/decision", json={"decision": "deny"})
                            decision.raise_for_status()
                            denied.add(approval["id"])
                    if run["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                        break
                    if time.monotonic() >= deadline:
                        client.post(f"/runs/{run_id}/cancel").raise_for_status()
                        run["status"] = "benchmark_timeout"
                        break
                    time.sleep(0.4)
                duration = round(time.monotonic() - started, 3)
                messages = client.get(f"/conversations/{conversation_id}/messages")
                messages.raise_for_status()
                answer = "\n".join(m["text"] for m in messages.json() if m.get("run_id") == run_id and m["role"] == "assistant")
                usage = run.get("usage") or {}
                expectations = task.get("expected_contains", []) if turn_index == len(task["turns"]) else []
                row = {
                    "label": args.label, "task_id": task["id"], "turn": turn_index,
                    "conversation_id": conversation_id, "run_id": run_id,
                    "status": run["status"], "duration_seconds": duration,
                    "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                    "estimated_cost_usd": usage.get("estimated_cost_usd"),
                    "expectation_checks_passed": all(term in answer for term in expectations) if expectations else None,
                    "business_correctness_review": None,
                    "denied_approvals": len(denied), "answer": answer,
                }
                rows.append(row)
                trace = client.get(f"/runs/{run_id}/trace")
                traces.append(trace.json() if trace.is_success else {"run": run, "trace_unavailable": True})
                print(f"{task['id']} turn {turn_index}: {run['status']} ({duration}s)")
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(), "label": args.label,
        "configuration": configuration, "assistant": assistant, "tasks": tasks,
        "rows": rows, "traces": traces,
        "evidence_boundary": "Task completion is not business correctness. No savings/superiority claim without matched baseline, customer ground truth and verified provider billing. This report contains task content; restrict access.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if rows:
        with args.output.with_suffix(".csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    print(f"Saved report and CSV: {args.output}")

if __name__ == "__main__":
    main()
