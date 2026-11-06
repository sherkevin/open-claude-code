"""Opt-in public identity switching acceptance through the product HTTP API.

The default prints a no-network plan. --real-model creates isolated private
fixtures and makes three business-model turns plus four metadata turns. Answers
are saved privately for semantic review; no user conversation is modified.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

from identity_cases import contains_required_fact, scan_output
from identity_smoke import save_private
from smoke_api import API, CheckFailure, TERMINAL, now, require


PLAN = [
    {"id": "dano_identity", "model": "dano-v2.1", "kind": "product_metadata"},
    {"id": "dano_business_seed", "model": "dano-v2.1", "kind": "model"},
    {"id": "deepseek_identity_after_switch", "model": "deepseek-v4.1-flash", "kind": "product_metadata"},
    {"id": "deepseek_business_retains_history", "model": "deepseek-v4.1-flash", "kind": "model"},
    {"id": "dano_identity_after_switch_back", "model": "dano-v2.1", "kind": "product_metadata"},
    {"id": "fresh_glm_identity", "model": "glm-5.3", "kind": "product_metadata"},
    {"id": "fresh_glm_business", "model": "glm-5.3", "kind": "model"},
]


def validate_turn(case, run, trace, answer, label, facts=(), forbidden=()):
    """Check execution evidence separately from a bounded answer-content scan."""
    failures = []
    if run.get("status") != "succeeded":
        failures.append("run_not_succeeded")
    if run.get("model_id") != case["model"] or run.get("model") != label:
        failures.append("run_public_selection_mismatch")
    if run.get("execution_kind") != case["kind"]:
        failures.append("execution_kind_mismatch")
    stages = {item.get("data", {}).get("stage") for item in trace.get("events", []) if item.get("type") == "run.stage"}
    timings = run.get("timings") or {}
    if case["kind"] == "product_metadata":
        expected = ("我是 Dano-v2.1，Better AI 的工业场景智能助手。" if case["model"] == "dano-v2.1"
                    else f"我是 Better AI 的工业场景智能助手，当前使用 {label}。")
        if answer != expected:
            failures.append("metadata_answer_mismatch")
        if run.get("identity_source") != "run_selection" or run.get("model_call_count") != 0:
            failures.append("metadata_origin_missing")
        if stages.intersection({"initializing", "runtime_ready", "model_started"}) or any(key in timings for key in ("runtime_ready_ms", "first_model_event_ms")):
            failures.append("metadata_started_model_runtime")
        usage = run.get("usage") or {}
        if usage.get("source") != "product_metadata" or usage.get("model_call_count") != 0:
            failures.append("metadata_usage_not_distinguished")
    else:
        if "model_started" not in stages or "first_model_event_ms" not in timings:
            failures.append("real_model_event_missing")
        if label.casefold() not in answer.casefold():
            failures.append("answer_current_public_model_missing")
    for fact in facts:
        if not contains_required_fact(answer, fact):
            failures.append("required_business_fact_missing")
    failures.extend(scan_output(answer, list(forbidden)))
    return sorted(set(failures))


class SelectionAcceptance:
    def __init__(self, args):
        self.args = args
        self.api = API(args.base_url, args.origin)
        self.active_run = None
        self.report = {
            "suite": "identity-selection-0.4.2", "started_at": now(), "status": "running",
            "cases": [], "resources": {"conversations": [], "runs": []},
            "limitations": [
                "Only newly created private fixtures are used; existing user conversations are not changed.",
                "Product metadata answers are not provider-generated answers and do not prove model inference quality.",
                "Business turns require real model events and retained facts; human semantic review remains required.",
                "Private upstream routing should be cross-checked separately by the deployment operator using run IDs.",
            ],
        }
        self.options = {}
        self.forbidden = []

    def save(self):
        save_private(self.args.report, self.report)

    def setup(self, password):
        self.api.login(self.args.username, password)
        self.options = {option["id"]: option for option in self.api.request("GET", "/models")}
        for model in {case["model"] for case in PLAN}:
            require(self.options.get(model, {}).get("available"), "A required public model is unavailable")
        if self.args.forbidden_text_file:
            self.forbidden = json.loads(self.args.forbidden_text_file.read_text(encoding="utf-8"))
            require(isinstance(self.forbidden, list) and all(isinstance(value, str) and value for value in self.forbidden), "Forbidden values must be a private JSON string array")
        assistant = self.api.request("POST", "/assistants", expected=(201,), json={
            "name": "模型选择验收 " + uuid.uuid4().hex[:8], "scope": "private",
            "instructions": "使用用户在本会话提供的数据完成计算，保持设备代号与统计口径。无需查询外部资料或写入记忆。",
            "connections": [], "knowledge_base_ids": [], "skill_ids": [], "web_search_enabled": False,
        })
        self.report["resources"]["assistant_id"] = assistant["id"]
        self.save()

    def new_conversation(self, model):
        level = self.args.dano_reasoning_level if model == "dano-v2.1" else self.options[model]["default_reasoning_level"]
        response = self.api.request("POST", "/conversations", expected=(201,), json={
            "assistant_id": self.report["resources"]["assistant_id"], "title": "模型身份验收 " + model,
            "model_id": model, "reasoning_level": level,
        })
        self.report["resources"]["conversations"].append(response["id"])
        self.save()
        return response["id"]

    def switch(self, conversation, model):
        body = {"model_id": model}
        if model == "dano-v2.1":
            body["reasoning_level"] = self.args.dano_reasoning_level
        response = self.api.request("PATCH", f"/conversations/{conversation}", json=body)
        require(response.get("model_id") == model, "Model selection did not persist")

    def turn(self, index, conversation, prompt, facts=()):
        case = PLAN[index]
        response = self.api.request("POST", f"/conversations/{conversation}/messages", expected=(202,), json={
            "text": prompt, "file_ids": [], "skill_ids": [], "client_request_id": uuid.uuid4().hex,
        })
        run_id = response["run_id"]
        self.active_run = run_id
        self.report["resources"]["runs"].append(run_id)
        record = {**case, "conversation_id": conversation, "run_id": run_id, "prompt": prompt, "status": "running"}
        self.report["cases"].append(record)
        self.save()
        start = time.monotonic()
        while time.monotonic() - start < self.args.timeout:
            run = self.api.request("GET", f"/runs/{run_id}")
            if run["status"] == "awaiting_approval":
                for approval in run.get("approvals", []):
                    if approval["status"] == "pending":
                        self.api.request("POST", f"/approvals/{approval['id']}/decision", json={"decision": "deny"})
                raise CheckFailure("Unexpected approval; fixture needs only user-supplied arithmetic")
            if run["status"] in TERMINAL:
                self.active_run = None
                trace = self.api.request("GET", f"/runs/{run_id}/trace")
                answer = "\n".join(message.get("text", "") for message in trace.get("messages", [])
                                   if message.get("id") == run.get("assistant_message_id"))
                failures = validate_turn(case, run, trace, answer, self.options[case["model"]]["label"], facts, self.forbidden)
                record.update(status="failed" if failures else "scanned_clean", findings=failures, answer=answer,
                              run=run, events=trace.get("events", []), seconds=round(time.monotonic() - start, 3),
                              semantic_review="pending" if case["kind"] == "model" else "exact_metadata_verified")
                self.save()
                print(case["id"] + ": " + record["status"], flush=True)
                return
            time.sleep(0.4)
        raise CheckFailure("Acceptance turn timed out; cancellation will be requested")

    def execute(self):
        marker = "PUMP-" + uuid.uuid4().hex[:10].upper()
        self.report["business_marker"] = marker
        conversation = self.new_conversation("dano-v2.1")
        self.turn(0, conversation, "你是谁")
        self.turn(1, conversation, f"本会话的验收设备代号为 {marker}。该设备本批次总数 200 件，不良 10 件。请计算不良率，复述设备代号，并说明本轮当前选中的公开模型名称。不要读写文件、使用记忆或查询外部资料。", (marker, "5%"))
        history = self.api.request("GET", f"/conversations/{conversation}/messages")
        old_text = {message["id"]: message["text"] for message in history}
        self.switch(conversation, "deepseek-v4.1-flash")
        self.turn(2, conversation, "你是谁")
        self.turn(3, conversation, "沿用我先前给出的设备代号、总数和不良数，完整复述设备代号并计算不良率，再说明本轮当前选中的公开模型名称。不要读写文件、使用记忆或查询外部资料。", (marker, "5%"))
        self.switch(conversation, "dano-v2.1")
        self.turn(4, conversation, "你是谁")
        after = {message["id"]: message["text"] for message in self.api.request("GET", f"/conversations/{conversation}/messages")}
        self.report["historical_messages_unchanged"] = all(after.get(key) == value for key, value in old_text.items())
        fresh = self.new_conversation("glm-5.3")
        self.turn(5, fresh, "你是谁")
        self.turn(6, fresh, "某批次总数 200 件，不良 10 件。请计算不良率，并说明本轮当前选中的公开模型名称。不要读写文件、使用记忆或查询外部资料。", ("5%",))
        failures = any(case["status"] != "scanned_clean" for case in self.report["cases"])
        failures = failures or not self.report["historical_messages_unchanged"]
        self.report.update(status="automated_failures" if failures else "awaiting_semantic_review", completed_at=now())
        self.save()
        return 1 if failures else 0

    def cleanup(self):
        failures = []
        if self.active_run:
            try:
                self.api.request("POST", f"/runs/{self.active_run}/cancel", expected=(200, 202, 409))
            except Exception:
                failures.append("cancel_active_run")
        for conversation in self.report["resources"]["conversations"]:
            try:
                self.api.request("PATCH", f"/conversations/{conversation}", json={"archived": True})
            except Exception:
                failures.append("archive_fixture")
        assistant = self.report["resources"].get("assistant_id")
        if assistant:
            try:
                self.api.request("PATCH", f"/assistants/{assistant}", json={"enabled": False})
            except Exception:
                failures.append("disable_fixture")
        self.report["cleanup"] = {"status": "incomplete" if failures else "completed", "failures": failures}
        self.save()
        self.api.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--origin")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--forbidden-text-file", type=Path)
    parser.add_argument("--dano-reasoning-level", choices=("light", "high", "max"), default="light")
    parser.add_argument("--report", type=Path, default=Path(".runtime/identity-selection-report.json"))
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args(argv)
    if not args.real_model:
        print(json.dumps({"plan_only": True, "requests_made": 0, "cases": PLAN, "maximum_user_turns": 7, "business_model_turns": 3, "product_metadata_turns": 4}, ensure_ascii=False, indent=2))
        return 0
    require(args.timeout > 0, "Timeout must be positive")
    require(not args.report.exists(), "Choose a fresh private report path")
    password = args.password_file.read_text(encoding="utf-8").strip() if args.password_file else os.environ.get("SMOKE_ADMIN_PASSWORD", "")
    require(password, "Supply a private password file or SMOKE_ADMIN_PASSWORD")
    logging.basicConfig(level=logging.ERROR)
    runner = SelectionAcceptance(args)
    try:
        runner.setup(password)
        return runner.execute()
    except Exception as exc:
        detail = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        runner.report.update(status="execution_failed", failure=detail)
        runner.save()
        print("Identity switching acceptance stopped: " + detail, file=sys.stderr)
        return 1
    finally:
        runner.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
