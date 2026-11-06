"""Bounded real-provider acceptance for configured sources and Skill invocation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time
import uuid

from smoke_api import API, TERMINAL, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument("--seed-report", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--case", choices=["identity", "company", "data", "web", "glm-company", "deepseek-data"], action="append")
    args = parser.parse_args()
    if not args.real_model:
        print("Plan: identity, company original-page citations, database provenance + Skill, public web original, GLM company, DeepSeek data. Add --real-model to execute.")
        return
    seed = json.loads(args.seed_report.read_text(encoding="utf-8"))
    report = {"status": "running", "cases": [], "cleanup": {}}
    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        args.report.chmod(0o600)
    api, conversations, assistant_id, active_run = API(args.base_url), [], None, None
    try:
        api.login("admin", args.password_file.read_text().strip())
        source = api.request("GET", "/assistants/" + seed["assistant_id"])
        assistant = api.request("POST", "/assistants", expected=(201,), json={
            "name": "0.4 验收专用助手 " + uuid.uuid4().hex[:8], "scope": "private", "description": "技术验收，结束后停用",
            **{key: source[key] for key in ("instructions", "connections", "knowledge_base_ids", "skill_ids", "web_search_enabled")},
        })
        assistant_id = assistant["id"]
        cases = [
            ("identity", "dano-v2.1", "你是谁？请用一句话回答。", None),
            ("company", "dano-v2.1", "Better AI 主要面向什么行业？它与华微世纪在方案中怎样分工？请查公司资料并引用原文页码，简洁回答。", "knowledge"),
            ("data", "dano-v2.1", "查询 L01 产线 2026-11-01 至 2026-11-04 的总检验件数、总合格件数、加权合格率，并说明计算口径。引用具体数据来源。", "data"),
            ("web", "dano-v2.1", "搜索 MCP 官方协议文档，读取相关原网页并确认它是否使用 JSON-RPC 2.0。给出一句结论并引用原网页。", "web"),
            ("glm-company", "glm-5.3", "查公司知识库，用两句话介绍 Better AI 的定位与合作分工，引用来源。", "knowledge"),
            ("deepseek-data", "deepseek-v4.1-flash", "查 L01 产线 2026-11-01 到 2026-11-04 的检验总数、合格总数和加权合格率，引用数据库来源。", "data"),
        ]
        for name, model, prompt, kind in cases:
            if args.case and name not in args.case:
                continue
            conversation = api.request("POST", "/conversations", expected=(201,), json={"assistant_id": assistant_id,
                "title": "0.4 验收 · " + name, "model_id": model, "reasoning_level": "high" if model == "dano-v2.1" else "default"})
            conversations.append(conversation["id"])
            body = {"text": prompt, "file_ids": [], "client_request_id": uuid.uuid4().hex}
            if name == "data":
                body["skill_ids"] = seed["skill_ids"][:1]
            submitted = api.request("POST", f"/conversations/{conversation['id']}/messages", expected=(200, 202), json=body)
            active_run = submitted["run_id"]
            record = {"case": name, "model": model, "conversation_id": conversation["id"], "run_id": active_run, "status": "running"}
            report["cases"].append(record)
            save()
            deadline = time.monotonic() + 180
            while True:
                run = api.request("GET", "/runs/" + active_run)
                if run["status"] in TERMINAL:
                    break
                if time.monotonic() > deadline:
                    api.request("POST", f"/runs/{active_run}/cancel")
                    raise RuntimeError("Actual provider case exceeded180s; cancellation requested")
                time.sleep(0.4)
            trace = api.request("GET", f"/runs/{active_run}/trace")
            message = next(item for item in trace["messages"] if item["role"] == "assistant")
            record.update(status=run["status"], answer=message["text"], citations=message.get("citations", []), timings=run.get("timings"),
                          tool_events=[event for event in trace["events"] if event["type"] in {"tool.started", "tool.completed"}])
            save()
            require(run["status"] == "succeeded", "Actual model task did not succeed")
            require(run.get("timings", {}).get("first_text_ms") is not None, "Visible text timing missing")
            require(run["timings"]["total_ms"] >= run["timings"]["first_text_ms"], "Timing order invalid")
            require("Claude Code" not in message["text"] and "deepseek-v4.1-flash-1017" not in message["text"], "Internal runtime/route appeared")
            require("演示数据" not in message["text"], "Routine dataset disclaimer remained")
            if name == "identity":
                require("Dano-v2.1" in message["text"] and "Better AI" in message["text"], "Current public identity missing")
            if kind:
                citations = [item for item in message.get("citations", []) if item["kind"] == kind]
                require(citations, "Required real source citation missing")
                sources = [api.request("GET", "/sources/" + item["id"]) for item in citations]
                record["sources"] = sources
                if kind == "knowledge":
                    require(all(item["locator"].get("page") and item["document_version"] for item in sources), "Knowledge page/version absent")
                    document = api.client.get(sources[0]["content_url"].replace("/api/v1/", ""))
                    require(document.status_code == 200, "Original document is inaccessible")
                    require(hashlib.sha256(document.content).hexdigest() == sources[0]["locator"]["sha256"], "Original file version hash mismatch")
                if kind == "data":
                    require(any(item["locator"].get("table") == "quality_batches" and item["locator"].get("fields") and item["locator"].get("time_range") for item in sources), "Database provenance incomplete")
                    require("4700" in message["text"].replace(",", "") and "4537" in message["text"].replace(",", ""), "Database totals wrong")
                if kind == "web":
                    require(any(item["locator"].get("content_type") == "page_text" for item in sources), "Only search snippets cited; original page read not verified")
            record["automated_checks"] = "passed"
            save()
            print(json.dumps({"case": name, "status": "passed", "timings": run["timings"], "citations": len(record["citations"])}, ensure_ascii=False), flush=True)
            active_run = None
        report["status"] = "automated_passed_pending_semantic_review"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        if active_run:
            try:
                run = api.request("GET", "/runs/" + active_run)
                if run["status"] not in TERMINAL:
                    api.request("POST", f"/runs/{active_run}/cancel")
                    deadline = time.monotonic() + 20
                    while api.request("GET", "/runs/" + active_run)["status"] not in TERMINAL and time.monotonic() < deadline:
                        time.sleep(0.25)
            except Exception as exc:
                report["cleanup"]["cancel_error"] = type(exc).__name__
        for conversation_id in conversations:
            try:
                api.request("PATCH", "/conversations/" + conversation_id, json={"archived": True})
            except Exception as exc:
                report["cleanup"][conversation_id] = type(exc).__name__
        if assistant_id:
            try:
                api.request("PATCH", "/assistants/" + assistant_id, json={"enabled": False})
            except Exception as exc:
                report["cleanup"]["assistant_error"] = type(exc).__name__
        report["cleanup"]["conversation_count"] = len(conversations)
        save()
        api.close()


if __name__ == "__main__":
    main()
