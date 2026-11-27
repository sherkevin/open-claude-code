"""Trusted, run-bound tool execution for the isolated Harness process.

The child proposes operations; only this host executes business handlers. Native
Harness tools and model-supplied identity fields never grant authority.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time

from .db import offload_write, uid
from .business_results import presentation_receipt
from .external_capabilities import CAPABILITY_TOOL_SHORT_NAMES, record_approval_receipt
from .harness_bridge import _arguments as knowledge_arguments
from .harness_memory import HarnessMemoryTools, MEMORY_NAMES
from . import harness_schedule
from .mcp_tools import build_mcp_tools, enforce_model_result_budget
from .runtime import result_fact_metadata
from .run_recovery import record_dispatch, require_tool_execution
from .security import AppError, public_tool_arguments
from .session_permissions import automatic_decision
from .workspace_tools import APPROVAL_ID, TOOL_CALL_ID, build_workspace_tools

OCR_PURPOSES = frozenset({"text_recognition", "rich_layout_recognition"})
IMAGE_MODES = frozenset({"text_search", "reverse_image"})
RESULT_DELIVERY_TIMEOUT_SECONDS = 1.0


def bounded_respond(client, request_id, result):
    """Bound pinned SDK's blocking stdin write without taking its writer lock.

    The reviewed SDK has no write timeout. Killing only its owned process
    closes the pipe and releases blocked writers, including a writer holding
    the SDK lock. The watchdog never calls client.close or touches the DB.
    """
    process = getattr(client, "_proc", None)
    if not isinstance(process, subprocess.Popen):
        raise AppError(503, "HARNESS_RESULT_DELIVERY_UNAVAILABLE", "执行进程已关闭，工具结果未交付")
    state, state_lock = {"done": False, "expired": False}, threading.Lock()
    def expire():
        with state_lock:
            if state["done"]:
                return
            state["expired"] = True
            try:
                process.kill()
            except ProcessLookupError:
                pass
    timer = threading.Timer(RESULT_DELIVERY_TIMEOUT_SECONDS, expire)
    timer.daemon = True
    timer.start()
    try:
        client.respond(request_id, result)
    except Exception:
        if not state["expired"]:
            raise
    finally:
        with state_lock:
            state["done"] = True
        timer.cancel()
    if state["expired"]:
        raise AppError(504, "HARNESS_RESULT_DELIVERY_TIMEOUT", "工具结果交付超时，执行进程已终止；本次未自动重试")


class HarnessToolBridge:
    def __init__(self, runtime, parent_bridge=None):
        self.runtime = runtime
        recovery_only = runtime.config.get("recovery_only")
        self.tools = {} if recovery_only else {tool.name: tool for tool in build_workspace_tools(runtime)}
        if parent_bridge is None:
            self.inherited_names = None
            self.business_tools = {} if recovery_only else build_mcp_tools(runtime)
        else:
            self.inherited_names = parent_bridge.runtime.mounted_tools.keys() - {"delegate_tasks"}
            self.business_tools = {name: tool for name, tool in parent_bridge.business_tools.items()
                                   if name in self.inherited_names}
        self.memory = HarnessMemoryTools(self)
        self.schedule_enabled = not recovery_only and parent_bridge is None and not runtime.config.get("subagent_child_id") and runtime.config.get("runtime_type")=="harness"
        self.calls = set()
        self.response_guards = {}
        self.child_call_events = {}

    def definitions(self):
        if self.runtime.config.get("recovery_only"):
            return []
        types = {str: "string", int: "integer", list: "array"}
        policy = self.runtime.service.policy
        workspace = [{"name": tool.name, "description": tool.description,
                 "input_schema": {"type": "object", "properties": {
                     key: {"type": types[value], **({"items": {}} if value is list else {})}
                     for key, value in tool.input_schema.items()},
                     "additionalProperties": False}}
                for tool in self.tools.values()
                if policy.tool_allowed(f"mcp__workspace__{tool.name}")]
        for definition in workspace:
            if definition["name"] == "delegate_tasks":
                schema = definition["input_schema"]
                schema["required"] = ["tasks"]
                tasks = schema["properties"]["tasks"]
                tasks["items"] = {"type": "string"}
        business = [tool.definition() for tool in self.business_tools.values()
                    if policy.tool_allowed(tool.canonical_name)]
        memory = [item for item in self.memory.definitions()
                  if policy.tool_allowed(f"mcp__workspace__{item['name']}")]
        definitions = workspace + business + memory
        if self.schedule_enabled:
            definitions.extend(item for item in [harness_schedule.DEFINITION,harness_schedule.EDIT_DEFINITION,*harness_schedule.READ_DEFINITIONS]
                               if policy.tool_allowed(f"mcp__workspace__{item['name']}"))
        if self.inherited_names is not None:
            definitions = [item for item in definitions if item["name"] in self.inherited_names]
        return definitions

    def active(self, run_id):
        runtime, service = self.runtime, self.runtime.service
        conversation = service.require("conversations", runtime.conversation_id)
        run = service.require("runs", run_id)
        if (runtime.interrupted or (runtime.current_run or {}).get("id") != run_id
                or conversation["active_run_id"] != run_id or conversation["archived"]
                or run["status"] != "running"
                or run["conversation_id"] != runtime.conversation_id
                or run["user_id"] != conversation["user_id"]):
            raise AppError(409, "HARNESS_RUN_INACTIVE", "此任务不再允许执行工具")
        require_tool_execution(run)
        if self.inherited_names is not None:
            runtime.recheck()

    def arguments(self, name, arguments):
        if name in self.business_tools:
            return self.business_tools[name].arguments(arguments)
        tool = self.tools[name]
        if (type(arguments) is not dict or set(arguments) - tool.input_schema.keys()
                or len(json.dumps(arguments, ensure_ascii=False)) > 512000
                or any(type(value) is not tool.input_schema[key] for key, value in arguments.items())):
            raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "工具参数无效或超出允许范围")
        if name in {"knowledge_search", "knowledge_read"}:
            return knowledge_arguments(name, arguments)
        required = {
            "ask_user": {"question"},
            "review_plan": {"plan"},
            "delegate_tasks": {"tasks"},
            "generate_xlsx": {"filename", "sheets"},
            "generate_image": {"prompt"},
            "web_search": {"query"},
            "web_read": {"url"},
            "file_read": {"path"},
            "file_write": {"path", "content", "expected_revision"},
            "file_search": {"query"},
            "ocr": {"file_id"},
            # image_search mode selects which of query/file_id is required;
            # validated below instead of a static required set.
            "image_search": set(),
        }[name]
        if not required <= arguments.keys():
            raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "工具缺少必要参数")
        if name == "ocr":
            file_id = arguments.get("file_id")
            if not isinstance(file_id, str) or not file_id or len(file_id) > 128 or not file_id.isascii():
                raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "附件 ID 无效")
            purpose = arguments.get("purpose", "text_recognition")
            if purpose not in OCR_PURPOSES:
                raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "识别用途无效")
        if name == "image_search":
            mode = arguments.get("mode", "text_search")
            if mode not in IMAGE_MODES:
                raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "图片搜索模式无效")
            query = arguments.get("query", "")
            file_id = arguments.get("file_id", "")
            if not isinstance(query, str) or not isinstance(file_id, str):
                raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "图片搜索参数无效")
            if mode == "text_search":
                if not query.strip() or len(query) > 400 or file_id:
                    raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "文字搜图需要 1–400 字符检索词且不能附带图片")
            else:
                if query or not file_id or len(file_id) > 128 or not file_id.isascii():
                    raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "以图搜图需要当前会话附件且不能附带检索词")
            if "limit" in arguments and (type(arguments["limit"]) is not int or not 1 <= arguments["limit"] <= 20):
                raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "返回数量无效")
        if name.startswith("web_"):
            if len(arguments.get("query", "")) > 2000 or len(arguments.get("url", "")) > 8192:
                raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "检索参数过长")
            if "limit" in arguments and not 1 <= arguments["limit"] <= 20:
                raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "检索数量无效")
        if name.startswith("file_"):
            if name in {"file_read", "file_write"}:
                path = arguments.get("path")
                if not isinstance(path, str) or not path or len(path) > 512 or "\x00" in path:
                    raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "文件路径无效")
            if name == "file_write":
                if not isinstance(arguments.get("content"), str) or len(arguments["content"]) > 524288:
                    raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "文件内容无效或超过限制")
                if type(arguments.get("expected_revision")) is not int or arguments["expected_revision"] < 0:
                    raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "文件版本号无效")
            if name == "file_search":
                query = arguments.get("query")
                if not isinstance(query, str) or not query.strip() or len(query) > 200:
                    raise AppError(422, "HARNESS_ARGUMENTS_INVALID", "文件检索词无效")
        return arguments

    def policy_snapshot(self):
        service = self.runtime.service
        conversation = service.require("conversations", self.runtime.conversation_id)
        assistant = conversation["assistant_id"]
        # Include absence/disabled states: introducing a policy during a read
        # must invalidate that result just like changing an existing grant.
        return json.dumps([service.model_approval.policy(assistant), service.high_risk_approval.policy(assistant)], sort_keys=True)

    def unchanged_policy(self, expected):
        if self.policy_snapshot() != expected:
            raise AppError(403, "HARNESS_APPROVAL_CHANGED", "审批策略已变化，请重新发送任务")

    def discard(self, run_id, call_id, tool_name, error):
        self.runtime.service.db.event(run_id, "tool.discarded", {
            "tool_call_id": call_id, "tool_name": tool_name, "success": False,
            "error_code": error.code,
            **({"subagent_child_id": self.runtime.subagent_child_id}
               if self.inherited_names is not None else {"error_message": error.message})})
        return {"is_error": True, "content": [{"type": "text", "text": error.message}]}

    def respond(self, client, request_id, payload, result, run_id):
        """Last host handoff, on a worker, with no await after authorization.

        Cancellation/config changes cannot interleave with the final bounded
        RPC write. Keep this separate from invoke's event-persistence awaits.
        """
        with self.runtime.service.db.lock:
            call_id = payload.get("callId")
            guard = self.response_guards.pop((run_id, call_id), None)
            event_id, tool_name = self.child_call_events.pop(call_id, (call_id, payload.get("toolName")))
            try:
                if guard:
                    guard()
                self.active(run_id)
            except AppError as exc:
                result = self.discard(run_id, event_id, tool_name, exc)
                # Even an error result can prompt the native model's next step.
                # Cancellation owns transport shutdown after its children join.
                if self.runtime.service.db.one(
                        "SELECT 1 FROM runs WHERE id=? AND status IN ('cancelling','cancelled')",
                        (run_id,)):
                    return
            try:
                bounded_respond(client, request_id, result)
            except AppError as exc:
                self.discard(run_id, event_id, tool_name, exc)
                raise

    async def invoke(self, payload):
        runtime, service = self.runtime, self.runtime.service
        run_id = (runtime.current_run or {}).get("id")
        if (type(payload) is not dict or set(payload) != {"schema", "sessionId", "callId", "toolName", "arguments"}
                or payload.get("schema") != "workspace-tool/v1"
                or payload.get("sessionId") != runtime.session_id
                or not run_id):
            raise AppError(403, "HARNESS_REQUEST_DENIED", "工具请求与当前会话不匹配")
        name, call_id = service.policy.require_tool_name(payload["toolName"]), payload["callId"]
        candidate = self.business_tools.get(name)
        policy_name = candidate.canonical_name if candidate else f"mcp__workspace__{name}"
        service.policy.require_tool(policy_name)
        if ((self.inherited_names is not None and name not in self.inherited_names)
                or (name not in self.tools and name not in self.business_tools and name not in MEMORY_NAMES
                    and not (self.schedule_enabled and name in harness_schedule.NAMES))
                or not isinstance(call_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", call_id)
                or (name not in MEMORY_NAMES and (run_id, call_id) in self.calls)):
            raise AppError(403, "HARNESS_TOOL_DENIED", "工具未注册或调用已处理")
        child_id = runtime.subagent_child_id if self.inherited_names is not None else None
        host_call_id = f"{child_id}:{call_id}" if child_id else call_id
        business = self.business_tools.get(name)
        tool_name = business.canonical_name if business else f"mcp__workspace__{name}"
        event_id = uid()
        if child_id:
            self.child_call_events[call_id] = (event_id, tool_name)
        if name in MEMORY_NAMES:
            try:
                result = await self.memory.invoke(name, call_id, payload["arguments"], event_id=event_id)
                if child_id:
                    self.response_guards[(run_id, call_id)] = lambda: runtime.check_tool(tool_name, payload["arguments"])
                return result
            except AppError as exc:
                return {"is_error": True, "content": [{"type": "text", "text": exc.message}]}
        self.calls.add((run_id, call_id))
        schedule_proposal = None
        if name in {harness_schedule.NAME,harness_schedule.EDIT_NAME}:
            try:
                self.active(run_id)
                prepare = harness_schedule.prepare if name==harness_schedule.NAME else harness_schedule.prepare_edit
                schedule_proposal = await asyncio.to_thread(prepare, runtime, run_id, payload["arguments"])
                arguments = schedule_proposal[2]
            except AppError as exc:
                return {"is_error":True,"content":[{"type":"text","text":exc.message}]}
        elif name in harness_schedule.READ_NAMES:
            try:
                arguments=harness_schedule.read_arguments(name,payload["arguments"])
            except AppError as exc:
                return {"is_error":True,"content":[{"type":"text","text":exc.message}]}
        else:
            arguments = self.arguments(name, payload["arguments"])
        started = time.monotonic()
        success = False
        sources = []
        error = None
        model_input_bytes = None
        result = {"is_error": True, "content": [{"type": "text", "text": "工具调用未完成"}]}
        recheck = None
        try:
            self.active(run_id)
            automatic = runtime.check_tool(tool_name, arguments)
            # Business MCP grants authorize use, not absence of confirmation.
            # The selected session mode and current read-only certificate are
            # the only paths to unattended external business execution.
            if business:
                automatic = False
            policies = self.policy_snapshot()
            if schedule_proposal is not None:
                harness_schedule.check_context(runtime,run_id,tool_name)
            run = service.require("runs", run_id)
            review_required = service.model_approval.requires_review(runtime.conversation_id, tool_name)
            dual = service.high_risk_approval.requires_dual(run, tool_name, arguments)
            session_decision = None if automatic or review_required or dual else automatic_decision(service, run, tool_name)
            # This is an attempt before approval, not completed business
            # routing or scope enforcement. Report evidence after completion.
            await runtime.emit("run.stage", {"stage": "tool", "tool_name": tool_name})
            await runtime.emit("tool.started", {"tool_call_id": event_id, "tool_name": tool_name,
                               "arguments": public_tool_arguments(tool_name, arguments), "requires_approval": not (automatic or session_decision) or review_required or dual,
                               "dispatch_tracked": True})
            review = await service.model_approval.review(run, tool_name, arguments)
            def schedule_approval_guard():
                self.active(run_id)
                self.unchanged_policy(policies)
                harness_schedule.check_context(runtime,run_id,tool_name)
                return service.require("runs",run_id)

            allowed = (await runtime.approve(tool_name, arguments, review=review, review_checked=True,
                         **({"check_active":schedule_approval_guard} if schedule_proposal is not None else {}))
                       if review is not None or dual or not (automatic or session_decision) else True)
            # An approval authorizes this exact operation only. It never makes
            # a revoked account, changed policy, or cancelled run executable.
            self.active(run_id)
            runtime.check_tool(tool_name, arguments)
            self.unchanged_policy(policies)
            if not allowed:
                raise AppError(403, "HARNESS_APPROVAL_DENIED", "用户拒绝、审批超时或业务授权已失效")
            # A capability call authorized by approval needs an exact one-time
            # receipt keyed by this host call id and these public arguments.
            # Automatic authorization has no approval row and needs none.
            receipt_id = None
            if name in CAPABILITY_TOOL_SHORT_NAMES and getattr(allowed, "approval_id", None):
                receipt_id = record_approval_receipt(
                    service, run_id=run_id, tool_call_id=host_call_id, tool_name=tool_name,
                    arguments=arguments, receipt=allowed)
            def recheck():
                runtime.check_tool(tool_name, arguments)
                self.unchanged_policy(policies)
                if business:
                    business.current()
                self.active(run_id)
                if session_decision and automatic_decision(service, run, tool_name) != session_decision:
                    raise AppError(403, "SESSION_APPROVAL_CHANGED", "会话权限或只读核验已变化，请重新执行")

            if session_decision:
                recheck.definition_digest = session_decision.get("definition_digest")
                recheck()
                await runtime.emit("approval.session_decided", {**session_decision, "tool_name": tool_name,
                                   "tool_call_id": event_id, "decision": "allow"})

            self.response_guards[(run_id, call_id)] = recheck
            if child_id:
                await offload_write(lambda: record_dispatch(service, run_id, event_id, tool_name,
                                                            subagent_child_id=child_id, check_active=recheck))
            else:
                await offload_write(record_dispatch, service, run_id, event_id, tool_name)
            recheck()

            if schedule_proposal is not None:
                body, prepared, preview = schedule_proposal
                commit = harness_schedule.commit if name==harness_schedule.NAME else harness_schedule.commit_edit
                created = await offload_write(commit, runtime, run_id, body, prepared, preview, allowed, recheck)
                result = {"content":[{"type":"text","text":json.dumps(created,ensure_ascii=False)}]}
            elif name in harness_schedule.READ_NAMES:
                receipt=await asyncio.to_thread(harness_schedule.read,runtime,run_id,name,arguments,recheck)
                result={"content":[{"type":"text","text":json.dumps(receipt,ensure_ascii=False)}]}
            elif business:
                result = await business.invoke(arguments, recheck)
            else:
                # The real host callId is unique and auditable; never synthesize
                # run+tool identity for capability execution.  The approval
                # receipt names the exact row that authorized this call.
                token = TOOL_CALL_ID.set(host_call_id)
                approval_token = APPROVAL_ID.set(receipt_id)
                try:
                    result = await self.tools[name].handler(arguments)
                finally:
                    APPROVAL_ID.reset(approval_token)
                    TOOL_CALL_ID.reset(token)
            recheck()
            if not result.get("is_error") and not result.get("isError"):
                if business:
                    # Also guard injected/custom adapters, before source registration.
                    enforce_model_result_budget(result)
                    registered = service.evidence.register_mcp(run_id, business.connection_id, result, tool_name=business.server_tool)
                    if registered:
                        result["content"].append({"type": "text", "text": json.dumps([
                            {"title": source["title"], "kind": source["kind"], "citation": f"[[source:{source['id']}]]"}
                            for source in registered], ensure_ascii=False)})
                    # Other tools can finish while the HTTP call is pending.
                    # Attribute only this result's sources, including reused IDs.
                    sources = [source["id"] for source in registered][:30]
                    display_status = presentation_receipt(result)
                    if display_status:
                        result["content"].append({"type": "text", "text": display_status})
                    # Citation context is real model input too; do not exempt it.
                    model_input_bytes = enforce_model_result_budget(result)
                else:
                    # Bind this exact result, not concurrent inserts or only newly
                    # created rows: repeated reads may reuse a registered source.
                    candidates = service.evidence.source_ids_from_tool_result(result)
                    user = service.require("users", run["user_id"])
                    sources = [source["id"] for source in service.evidence.trusted_sources(run_id, candidates, user)]
                success = True
        except asyncio.CancelledError:
            raise
        except AppError as exc:
            error = {"error_code": exc.code, "error_message": exc.message}
            result = {"is_error": True, "content": [{"type": "text", "text": exc.message}]}
        finally:
            # Failure to persist completion also fails the RPC; the child must
            # not see a successful unaudited result.
            await runtime.emit("run.stage", {"stage": "evidence", "tool_name": tool_name,
                               "status": "reported" if success and sources else "unknown",
                               "source_count": len(sources)})
            if success:
                try:
                    with service.db.lock:
                        recheck()
                except AppError as exc:
                    success, sources = False, []
                    error = {"error_code": exc.code, "error_message": exc.message}
                    result = {"is_error": True, "content": [{"type": "text", "text": exc.message}]}
            await runtime.emit("tool.completed", {"tool_call_id": event_id, "tool_name": tool_name,
                               "_expected_run_id": run_id,
                               **({"_expected_tool_call_id": call_id} if success else {}),
                               "success": success, "duration_ms": round((time.monotonic() - started) * 1000),
                               **(error or {}),
                               **({"model_input_bytes": model_input_bytes} if success and model_input_bytes is not None else {}),
                               **result_fact_metadata(success, locals().get("result"), sources)})
        with service.db.lock:
            try:
                if success:
                    recheck()
                self.active(run_id)
            except AppError as exc:
                return self.discard(run_id, event_id, tool_name, exc)
            return result
