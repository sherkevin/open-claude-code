"""Separate Harness sessions with the owning turn’s host tool authority."""
import asyncio
from bisect import bisect_right

from .db import dump, now, offload, offload_write
from .harness_runtime import HarnessRuntime, _EmptyResult, prompt_with_selected_skills
from .harness_usage import HarnessUsageLedger
from .security import AppError
from .subagents import HARNESS_CHILD_MAX_STEPS, ChildOutputLimit


class HarnessChild(HarnessRuntime):
    def __init__(self, service, child, parent_actor, parent_runtime):
        self.child = child
        self.parent_actor = parent_actor
        self.parent_runtime = parent_runtime
        config = {
            **parent_runtime.config,
            "native_session_id": None,
            # The parent owns the reserved Goal round. Each child runs a normal
            # prompted turn in its own session under the parent tool grants.
            "goal_execution": None,
            "parent_goal_run_id": (parent_runtime.config.get("goal_execution") or {}).get("run_id"),
            "subagent_child_id": child["child_id"],
            "worker_max_output_tokens": min(800, service.settings.harness_max_output_tokens),
            "worker_max_steps": min(HARNESS_CHILD_MAX_STEPS, service.settings.max_turns),
        }
        super().__init__(service, child["conversation_id"], config, self._approve, self._emit,
                         parent_bridge=parent_runtime.bridge)
        self.session_id = f"child-{child['child_id']}"

    def model_system_prompt(self):
        return super().model_system_prompt() + (
            "\n\n你是本轮的独立执行子任务。完成分派的目标，按本轮授权使用需要的工具；"
            "需审批的操作等待宿主确认。只报告取得的结果和必要证据。"
        )

    def connection_layout(self):
        directory = self.service.settings.data_dir / "workspaces" / self.conversation_id / "subagents" / self.child["child_id"]
        return directory / "workspace", directory / "runtime"

    def recheck(self):
        service = self.service
        child = self.child
        run = service.require("runs", child["parent_run_id"])
        conversation = service.require("conversations", self.conversation_id)
        owner = service.require("users", child["user_id"])
        active_child = service.db.one("SELECT status FROM subagent_children WHERE child_id=? AND parent_run_id=? "
                                      "AND conversation_id=? AND user_id=?",
                                      (child["child_id"], run["id"], self.conversation_id, owner["id"]))
        if (self.interrupted or not active_child or active_child["status"] != "running"
                or not owner["enabled"] or conversation["archived"] or run["user_id"] != owner["id"]
                or run["conversation_id"] != self.conversation_id or conversation["user_id"] != owner["id"]
                or conversation["active_run_id"] != run["id"] or run["status"] != "running"
                or self.parent_actor.current_run_id != run["id"] or self.parent_actor.cancelled
                or self.parent_actor.runtime is not self.parent_runtime or self.parent_runtime.interrupted):
            raise AppError(409, "SUBAGENT_PARENT_INACTIVE", "子任务或父任务已结束，或授权已失效")
        service.usable_assistant(conversation["assistant_id"], owner)
        return run

    def check_tool(self, tool, arguments):
        self.recheck()
        return super().check_tool(tool, arguments)

    def recheck_configuration(self):
        run = self.recheck()
        if self.service.runtime_config(self.conversation_id)["fingerprint"] != self.config["fingerprint"]:
            raise AppError(403, "CONFIG_CHANGED", "助手配置已更改，请重新发送任务")
        return run

    async def _approve(self, tool, arguments, **options):
        return await self.parent_actor.approve(tool, arguments, **options,
            subagent_child_id=self.subagent_child_id, check_active=self.recheck_configuration)

    async def _emit(self, kind, data):
        await offload_write(self._write_event, kind, data)

    def _write_event(self, kind, data):
        # Child trace records expose call identity and status. Task input, tool
        # arguments, result bodies and server diagnostics stay out of these rows.
        safe_keys = {
            "tool.started": ("tool_call_id", "tool_name", "requires_approval", "dispatch_tracked"),
            "tool.completed": ("tool_call_id", "tool_name", "success", "duration_ms", "error_code",
                               "source_ids", "business_result", "model_input_bytes"),
            "approval.session_decided": ("tool_call_id", "tool_name", "mode", "scope", "decision"),
            "memory.pending": ("operation_id", "revision", "native_epoch", "mutation_status"),
            "memory.aborted": ("operation_id", "revision", "native_epoch", "mutation_status"),
        }
        if kind not in safe_keys:
            return
        with self.service.db.lock:
            run_id = self.child["parent_run_id"]
            if kind == "tool.completed" and data.get("success"):
                try:
                    self.recheck()
                    call_id = data.get("_expected_tool_call_id")
                    if call_id is not None:
                        guard = self.bridge.response_guards.get((run_id, call_id))
                        if guard is None:
                            raise AppError(409, "HARNESS_RESULT_STALE", "工具结果所属会话已失效")
                        guard()
                except AppError as error:
                    self.bridge.discard(run_id, data["tool_call_id"], data["tool_name"], error)
                    return
            self.service.db.event(run_id, kind, {"subagent_child_id": self.subagent_child_id,
                **{key: data[key] for key in safe_keys[kind] if key in data}})

    def _discard_unfinished_memory(self):
        service = self.service
        run_id = self.child["parent_run_id"]
        pending = service.memory.pending_for_run(run_id)
        if pending and pending.get("approval_id"):
            approval = service.require("approvals", pending["approval_id"])
            if approval.get("subagent_child_id") == self.subagent_child_id:
                service.memory.discard_for_run(run_id, reason="subagent_incomplete")

    async def complete(self, text, max_result_chars):
        self.current_run = await offload(self.recheck_configuration)
        text = prompt_with_selected_skills(text, self.config["skill_snapshots"])
        usage_run = {"id": self.child["child_id"]}
        began = False
        subscription = None
        usage, status = None, "failed"
        try:
            await self.connect()
            await offload(self.recheck_configuration)
            client = self.client
            ledger = HarnessUsageLedger(self.session_id, usage_run["id"])
            subscription = client.subscribe_notifications(lambda event:
                event.method == "workspace.usage" and event.payload.get("sessionId") == self.session_id
                and event.payload.get("runId") == usage_run["id"])
            await self._usage_snapshot(client, usage_run, ledger, "begin")
            began = True
            await offload_write(self.service.db.execute, "INSERT INTO model_usage_calls VALUES(?,?,?,?,?,?,?,?,?)",
                (usage_run["id"], self.child["user_id"], "subagent", self.config["public_model_id"], self.config["public_model_label"], "started", None, now(), None))
            prompt_receipt = await asyncio.to_thread(client.session_prompt, self.session_id, [{"type": "text", "text": text}])
            reply, accepted, finish = "", False, None
            active_streams, committed_message = set(), False
            while True:
                notification = await asyncio.to_thread(client.next_notification)
                payload = notification.payload
                if payload.get("sessionId") != self.session_id:
                    continue
                if notification.method == "workspace.tool_cancelled":
                    if self.tool_task and payload.get("callId") == self.tool_call_id:
                        self.tool_task.cancel()
                if notification.method == "workspace.limit":
                    raise AppError(409, "SUBAGENT_STEP_LIMIT", "子任务已达执行上限")
                if notification.method == "workspace.stream":
                    step = payload.get("stepId")
                    if payload.get("event") == "start":
                        active_streams.add(step)
                    elif payload.get("event") == "end":
                        outcome = payload.get("outcome") or {}
                        if step not in active_streams or payload.get("finishObserved") is not True or outcome.get("kind") != "committed":
                            raise AppError(502, "SUBAGENT_STREAM_INCOMPLETE", "子任务回复提前结束")
                        active_streams.remove(step)
                        committed_message = outcome.get("eventType") == "assistant/message"
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
                    if finish not in {"completed", "max-tokens"} or (finish == "completed" and not reply.strip()):
                        raise AppError(502, "SUBAGENT_MODEL_FAILED", "子任务未取得模型结果")
                    if active_streams or not committed_message:
                        raise AppError(502, "SUBAGENT_STREAM_INCOMPLETE", "子任务回复提前结束")
                    await asyncio.to_thread(client.request, "session/wait", {"sessionId": self.session_id}, response_model=_EmptyResult)
                    await self._usage_snapshot(client, usage_run, ledger)
                    usage = ledger.receipt()
                    await offload(self.recheck_configuration)
                    tokens = {key: usage.get(key) for key in ("input_tokens", "output_tokens")}
                    output = reply[:max_result_chars]
                    # Escaped text still has to fit in the stored JSON receipt.
                    limit = bisect_right(range(len(output) + 1), max_result_chars + 2,
                                         key=lambda length: len(dump(output[:length]))) - 1
                    output = output[:limit]
                    if finish == "max-tokens":
                        status = "interrupted"
                        return ChildOutputLimit(output=output, usage=tokens)
                    status = "succeeded"
                    return {"output": output, "usage": tokens}
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            try:
                if began and usage is None:
                    # Capture while the worker still owns its SDK process. A broken
                    # transport can still have delivered partial HTTP receipts.
                    if status != "cancelled":
                        try:
                            await self._usage_snapshot(client, usage_run, ledger)
                        except Exception:
                            ledger.complete = False
                    self._drain_usage(subscription, ledger)
                    if ledger.attempts or ledger.complete:
                        usage = ledger.receipt()
            finally:
                if subscription is not None:
                    subscription.close()
                try:
                    if status != "succeeded":
                        self.interrupted = True
                    await self.close()
                    if status == "succeeded":
                        self.require_process_reaped()
                except BaseException:
                    status = "failed"
                    raise
                finally:
                    self.current_run = None
                    if status != "succeeded":
                        await offload_write(self._discard_unfinished_memory)
                    if began:
                        await offload_write(self.service.db.execute,
                            "UPDATE model_usage_calls SET status=?,usage=?,completed_at=? WHERE id=?",
                            (status, dump(usage) if usage else None, now(), usage_run["id"]))


async def execute_harness_child(service, child, task, output_limit):
    parent = await offload(service.require, "runs", child["parent_run_id"])
    actor, runtime = service.subagents.harness_parent(parent)
    worker = await offload(HarnessChild, service, child, actor, runtime)
    return await worker.complete(task["task"], output_limit)
