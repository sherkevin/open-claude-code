"""Independent text-only workers driven by the pinned Harness SDK."""
import asyncio
import json

from .db import dump, now, offload, offload_write
from .harness_runtime import HarnessRuntime, _EmptyResult
from .harness_usage import HarnessUsageLedger
from .security import AppError


class HarnessChild(HarnessRuntime):
    def __init__(self, service, child, selection):
        config = {
            "runtime_type": "harness", "recovery_only": True,
            "provider_env": service.validated_provider(selection),
            "public_model_id": selection["model_id"], "public_model_label": selection["model_label"],
            "upstream_model": selection["upstream_model"], "upstream_effort": selection["upstream_effort"],
            "instructions": (
                "只完成分配的文字分析，给主助手可直接使用的结论、关键数字和依据。"
                "通常用三到五条短要点；按任务需要保留计算、条款或例外，标明无法验证的前提。"
                "省略题目复述、开场和面向用户的完整报告。"
            ),
            "worker_max_output_tokens": min(800, service.settings.harness_max_output_tokens), "worker_max_steps": 3,
        }
        super().__init__(service, child["conversation_id"], config, None, None)
        self.child = child
        self.session_id = f"child-{child['child_id']}"

    def connection_layout(self):
        directory = self.service.settings.data_dir / "workspaces" / self.conversation_id / "subagents" / self.child["child_id"]
        return directory / "workspace", directory / "runtime"

    def recheck(self):
        service = self.service
        run = service.require("runs", self.child["parent_run_id"])
        conversation = service.require("conversations", self.conversation_id)
        owner = service.require("users", self.child["user_id"])
        if (not owner["enabled"] or conversation["archived"] or run["user_id"] != owner["id"]
                or conversation["user_id"] != owner["id"] or conversation["active_run_id"] != run["id"] or run["status"] != "running"):
            raise AppError(409, "SUBAGENT_PARENT_INACTIVE", "父任务已结束或授权已失效")
        service.usable_assistant(conversation["assistant_id"], owner)

    async def complete(self, text, max_result_chars):
        await offload(self.recheck)
        usage_run = {"id": self.child["child_id"]}
        began = False
        usage, status = None, "failed"
        try:
            await self.connect()
            await offload(self.recheck)
            client = self.client
            ledger = HarnessUsageLedger(self.session_id, usage_run["id"])
            await self._usage_snapshot(client, usage_run, ledger, "begin")
            began = True
            await offload_write(self.service.db.execute, "INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)",
                (usage_run["id"], self.child["user_id"], "subagent", self.config["public_model_id"], self.config["public_model_label"], "started", None, now(), None))
            prompt_receipt = await asyncio.to_thread(client.session_prompt, self.session_id, [{"type": "text", "text": text}])
            reply, accepted, finish = "", False, None
            while True:
                notification = await asyncio.to_thread(client.next_notification)
                payload = notification.payload
                if payload.get("sessionId") != self.session_id:
                    continue
                if notification.method == "workspace.limit":
                    raise AppError(409, "SUBAGENT_STEP_LIMIT", "子任务已达执行上限")
                if notification.method == "workspace.stream" and payload.get("event") == "end" and payload.get("finishObserved") is not True:
                    raise AppError(502, "SUBAGENT_STREAM_INCOMPLETE", "子任务回复提前结束")
                if notification.method == "session.event":
                    event = payload.get("event", {})
                    kind, data = event.get("type"), event.get("data", {})
                    if kind == "agent/inbox/spliced":
                        accepted = accepted or any(item.get("id") == prompt_receipt for item in data.get("inserted", []) if isinstance(item, dict))
                    if not accepted:
                        continue
                    if kind == "assistant/message":
                        reply += "".join(block.get("text", "") for block in (data.get("message") or data).get("content", []) if block.get("type") == "text")
                    if kind == "turn/end":
                        finish = (data.get("reason") or {}).get("kind")
                if notification.method == "session.status" and accepted and payload.get("status") == "idle":
                    if finish != "completed" or not reply.strip():
                        raise AppError(502, "SUBAGENT_MODEL_FAILED", "子任务未取得模型结果")
                    await asyncio.to_thread(client.request, "session/wait", {"sessionId": self.session_id}, response_model=_EmptyResult)
                    await self._usage_snapshot(client, usage_run, ledger)
                    usage = ledger.receipt()
                    await offload(self.recheck)
                    status = "succeeded"
                    return {"output": reply[:max_result_chars], "usage": {key: usage.get(key) for key in ("input_tokens", "output_tokens")}}
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            try:
                await self.close()
            finally:
                if began:
                    await offload_write(self.service.db.execute,
                        "UPDATE model_usage_calls SET status=?,usage=?,completed_at=? WHERE id=?",
                        (status, dump(usage) if usage else None, now(), usage_run["id"]))


async def execute_harness_child(service, child, task, output_limit):
    parent = await offload(service.require, "runs", child["parent_run_id"])
    snapshot = json.loads(parent.get("execution_meta") or "{}")["selection"]
    selection = service.catalog.validate_snapshot(snapshot)
    worker = HarnessChild(service, child, selection)
    return await worker.complete(task["task"], output_limit)
