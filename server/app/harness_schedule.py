"""Owner-confirmed adaptation of pinned Harness schedule_create.

The upstream tool is Host-wide and can create new Sessions. Neither is
appropriate for the enterprise host. Reuse its RRULE contract and actual
calendar implementation, and leave all dispatch to the existing durable FIFO.
"""
from datetime import datetime, timezone
import json
import re

from .conversation_schedules import ScheduleCreate, prepare_schedule, persist_schedule
from .db import dump, now
from .security import AppError

NAME = "schedule_create"
CANONICAL = "mcp__workspace__schedule_create"

DEFINITION = {
    "name": NAME,
    "description": (
        "仅在用户明确要求创建定时任务时调用。沿用官方 Schedule 的 title、prompt、rrule、target 参数。"
        "仅允许 this_session。RRULE 必须包含 DTSTART（UTC Z 或明确 IANA TZID）与 RRULE 两行；"
        "一次性任务使用 FREQ=DAILY;COUNT=1。时区或时间不明确时先询问。"
        "由宿主展示完整指令、规则、首次时间，等待本人批准；批准不授予未来业务工具权限。"
        "每个人工会话轮次最多创建一个计划；定时轮次和子智能体不能创建。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "rrule": {"type": "string"},
            "target": {"type": "string"},
        },
        "required": ["title", "prompt", "rrule", "target"],
        "additionalProperties": False,
    },
}


def check_context(runtime, run_id):
    service = runtime.service
    if runtime.config.get("subagent_child_id") or runtime.config.get("recovery_only"):
        raise AppError(403, "SCHEDULE_CALLER_DENIED", "子智能体或恢复轮次不能创建定时计划")
    runtime.check_tool(CANONICAL, {})
    run = service.require("runs", run_id)
    user = service.require("users", run["user_id"])
    conversation = service.require("conversations", runtime.conversation_id, user)
    if (runtime.interrupted or (runtime.current_run or {}).get("id") != run_id
            or conversation["active_run_id"] != run_id or run["conversation_id"] != conversation["id"]
            or run["status"] != "running" or not user["enabled"]):
        raise AppError(409, "SCHEDULE_RUN_REQUIRED", "当前任务已结束或失去授权")
    # This private prompt would otherwise be exposed to other approvers. Fail
    # closed until an owner-first dual approval workflow has been implemented.
    high = service.high_risk_approval.policy(conversation["assistant_id"])
    if high and high["enabled"]:
        raise AppError(403, "SCHEDULE_DUAL_UNAVAILABLE", "当前助手启用双人审批，请在定时任务中心手动创建")
    if service.db.one("SELECT run_id FROM conversation_schedule_create_requests WHERE run_id=?", (run_id,)):
        raise AppError(409, "SCHEDULE_ALREADY_CREATED", "本轮已创建计划，请在任务中心核对，不重复创建")
    return user


def prepare(runtime, run_id, arguments):
    if (type(arguments) is not dict or set(arguments) != {"title", "prompt", "rrule", "target"}
            or any(type(v) is not str for v in arguments.values())
            or arguments["target"] != "this_session"
            or any(not 1 <= len(arguments[k].strip()) <= n for k,n in
                   (("title",120),("prompt",4000),("rrule",2048)))
            or any(len(arguments[k]) > n for k,n in (("title",120),("prompt",4000),("rrule",2048)))):
        raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","定时计划参数无效，只能创建当前会话的计划")
    user = check_context(runtime, run_id)
    rule = arguments["rrule"].strip()
    lines = rule.splitlines()
    if len(lines)!=2 or not lines[1].startswith("RRULE:"):
        raise AppError(422,"SCHEDULE_RULE_INVALID","请明确 DTSTART 与 RRULE 两行")
    match = re.fullmatch(r"DTSTART;TZID=([A-Za-z0-9_+./-]{1,100}):[0-9]{8}T[0-9]{6}",lines[0])
    if match:
        zone = match.group(1)
    elif re.fullmatch(r"DTSTART:[0-9]{8}T[0-9]{6}Z",lines[0]):
        zone = "UTC"
    else:
        raise AppError(422,"SCHEDULE_RULE_INVALID","DTSTART 必须明确 UTC 或 IANA 时区")
    body = ScheduleCreate(text=arguments["prompt"].strip(), recurrence="rrule",
                          execution_mode="agent", rule_expression=rule, timezone=zone)
    prepared = prepare_schedule(runtime.service,user,runtime.conversation_id,body)
    stamp, expression, effective_zone, local_zone = prepared
    preview = {"title":arguments["title"].strip(), "prompt":body.text,
               "rrule":expression, "target":"this_session", "timezone":effective_zone,
               "first_occurrence_utc":stamp.isoformat(),
               "first_occurrence_local":stamp.astimezone(local_zone).isoformat()}
    return body, prepared, preview


def commit(runtime,run_id,body,prepared,preview,approval,recheck):
    service = runtime.service
    with service.db.transaction():
        recheck()
        user = check_context(runtime,run_id)
        row = service.db.one("SELECT * FROM approvals WHERE id=? AND run_id=? AND tool_name=?",
                             (getattr(approval,"approval_id",None),run_id,CANONICAL))
        if (not row or row["status"]!="allowed" or row["user_id"]!=user["id"]
                or row.get("decided_by")!=user["id"] or row.get("decision_source")!="user"
                or row["arguments"]!=dump(preview) or row.get("strategy")=="model"
                or row.get("approval_mode")=="dual"
                or datetime.fromisoformat(row["expires_at"])<=datetime.now(timezone.utc)):
            raise AppError(403,"SCHEDULE_HUMAN_APPROVAL_REQUIRED","没有与此完整计划一致的本人批准记录")
        result = persist_schedule(service,user,runtime.conversation_id,body,prepared)
        service.db.execute("INSERT INTO conversation_schedule_create_requests VALUES(?,?,?,?)",
                           (run_id,row["id"],result["id"],now()))
        return {**result,"title":preview["title"],"timezone":preview["timezone"],
                "first_occurrence_local":preview["first_occurrence_local"]}
