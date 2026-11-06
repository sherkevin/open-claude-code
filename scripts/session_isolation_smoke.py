"""Opt-in live proof of concurrent sessions and native memory isolation.

Creates a private assistant and two disposable sessions through product APIs.
Uses only the existing read-only slow_lookup MCP fixture; never grants write tools.
Credentials and evidence remain in explicitly supplied private files.
"""
from __future__ import annotations

import argparse
import json
import secrets
import time
import uuid
from pathlib import Path

from smoke_api import API, TERMINAL, require


def run(args):
    api = API(args.base_url)
    api.login(args.username, Path(args.password_file).read_text().strip())
    report = {"status": "running", "checks": [], "runs": [], "cleanup": []}
    assistant, conversations = None, []
    active = set()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def start(conversation, text):
        result = api.request("POST", f"/conversations/{conversation['id']}/messages", expected=(200, 202), json={"client_request_id": uuid.uuid4().hex, "text": text, "file_ids": []})
        active.add(result["run_id"])
        report["runs"].append({"id": result["run_id"], "conversation_id": conversation["id"]})
        save()
        return result["run_id"]

    def wait(run_id, predicate):
        deadline = time.monotonic() + args.timeout
        while True:
            value = api.request("GET", f"/runs/{run_id}")
            if predicate(value):
                return value
            require(value["status"] not in TERMINAL, "Run terminated before the expected state")
            require(time.monotonic() < deadline, "Timed out waiting for the expected state")
            time.sleep(0.5)

    def completed(run_id):
        value = wait(run_id, lambda item: item["status"] in TERMINAL)
        require(value["status"] == "succeeded", "Real model run did not succeed")
        active.discard(run_id)
        entry = next(item for item in report["runs"] if item["id"] == run_id)
        entry.update(status=value["status"], model_id=value.get("model_id"), timings=value.get("timings"))
        messages = api.request("GET", f"/conversations/{entry['conversation_id']}/messages")
        answer = next(item["text"] for item in messages if item.get("run_id") == run_id and item["role"] == "assistant")
        entry["answer"] = answer
        save()
        return answer

    try:
        connections = api.request("GET", "/connections")
        connection = next((item for item in connections if item["id"] == args.connection_id), None)
        require(connection is not None and connection["enabled"], "Authorized read-only fixture connection is required")
        require(any(tool["name"] == "slow_lookup" for tool in connection["tools"]), "Fixture slow_lookup tool is required")
        assistant = api.request("POST", "/assistants", expected=(200, 201), json={
            "name": "会话隔离验收 " + secrets.token_hex(3), "scope": "private",
            "description": "临时并发验收，完成后停用。", "instructions": "按用户要求读取当前会话记忆，使用已授权的查询工具。",
            "connections": [{"connection_id": connection["id"], "allowed_tools": ["slow_lookup"], "auto_approved_tools": []}],
            "knowledge_base_ids": [], "skill_ids": [], "web_search_enabled": False,
            "default_model_id": "dano-v2.1", "default_reasoning_level": "light",
        })
        report["assistant_id"] = assistant["id"]
        markers = ["SESSION-A-" + secrets.token_hex(10), "SESSION-B-" + secrets.token_hex(10)]
        for index in range(2):
            conv = api.request("POST", "/conversations", expected=(200, 201), json={"assistant_id": assistant["id"], "title": f"独立会话验收 {index+1}", "model_id": "dano-v2.1", "reasoning_level": "light"})
            conversations.append(conv)
            endpoint = f"/conversations/{conv['id']}/memory"
            memory = api.request("GET", endpoint)
            require(memory["scope"] == "conversation" and not memory["files"], "New session must start with empty independent memory")
            api.request("PUT", endpoint + "/file", json={"path": "MEMORY.md", "content": f"# 会话验收\n本会话唯一校验标记：{markers[index]}\n", "revision": memory["revision"]})
        report["checks"].append("Two new sessions have empty, distinct writable memory scopes")
        a, b = conversations
        run_a = start(a, "请调用 slow_lookup，delay_seconds=2；这是一次读取查询，等待应用审批后再返回结果。")
        pending = wait(run_a, lambda item: item["status"] == "awaiting_approval")
        require(pending["approvals"], "Session A must have a pending approval")
        run_b = start(b, "请读取当前会话的原生记忆，只回答 MEMORY.md 中的唯一校验标记，不要查询业务工具。")
        response = api.client.post(f"conversations/{a['id']}/messages", json={"client_request_id": uuid.uuid4().hex, "text": "重复执行应被阻止", "file_ids": []})
        require(response.status_code == 409 and response.json()["error"]["code"] == "CONVERSATION_BUSY", "Same-session duplicate run must remain blocked")
        report["checks"].append("Session B starts while A awaits approval; duplicate run in A rejected")
        answer_b = completed(run_b)
        require(markers[1] in answer_b and markers[0] not in answer_b, "Session B must read only its own memory")
        require(api.request("GET", f"/runs/{run_a}")["status"] == "awaiting_approval", "B completion must not resolve A approval")
        api.request("POST", f"/runs/{run_a}/cancel", expected=(200, 202))
        cancelled = wait(run_a, lambda item: item["status"] in TERMINAL)
        require(cancelled["status"] == "cancelled", "A cancellation should complete")
        active.discard(run_a)
        report["runs"][0]["status"] = "cancelled"
        require(api.request("GET", f"/runs/{run_b}")["status"] == "succeeded", "Cancelling A must not change B")
        report["checks"].append("B completes during A pending approval; cancelling A leaves B succeeded")
        run_a2 = start(a, "请读取当前会话的原生记忆，只回答 MEMORY.md 中的唯一校验标记，不要查询业务工具。")
        answer_a = completed(run_a2)
        require(markers[0] in answer_a and markers[1] not in answer_a, "Session A must read only its own memory after cancellation")
        report["checks"].append("Native reads return distinct per-session memory markers, including after cancellation")
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        for run_id in list(active):
            try:
                api.request("POST", f"/runs/{run_id}/cancel", expected=(200, 202))
                value = wait(run_id, lambda item: item["status"] in TERMINAL)
                report["cleanup"].append({"run_id": run_id, "status": value["status"]})
            except Exception as exc:
                report["cleanup"].append({"run_id": run_id, "error_type": type(exc).__name__})
        for conv in conversations:
            try:
                api.request("PATCH", f"/conversations/{conv['id']}", json={"archived": True})
                report["cleanup"].append({"conversation_id": conv["id"], "archived": True})
            except Exception as exc:
                report["cleanup"].append({"conversation_id": conv["id"], "error_type": type(exc).__name__})
        if assistant:
            try:
                api.request("PATCH", f"/assistants/{assistant['id']}", json={"enabled": False})
                report["cleanup"].append({"assistant_disabled": True})
            except Exception as exc:
                report["cleanup"].append({"assistant_disable_error": type(exc).__name__})
        save()
        api.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=int, default=150)
    parser.add_argument("--real-model", action="store_true")
    args = parser.parse_args()
    require(args.real_model, "Pass --real-model to authorize actual model calls")
    run(args)
    print("Session isolation acceptance completed; see private report.")
