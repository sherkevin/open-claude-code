"""Owner-confirmed adaptation of pinned Harness schedule_create.

The upstream tool is Host-wide and can create new Sessions. Neither is
appropriate for the enterprise host. Reuse its RRULE contract and actual
calendar implementation, and leave all dispatch to the existing durable FIFO.
"""
from datetime import datetime, timezone
import json
import re

from .conversation_schedules import (
    ScheduleCreate, ScheduleEdit, ScheduleResume, prepare_schedule, persist_schedule,
    _edit_target, _available, _previous, _require_editable, _editable_row, edit_token,
    prepare_schedule_edit, persist_schedule_edit, prepare_schedule_resume,
    persist_schedule_resume, persist_schedule_pause, persist_schedule_cancel,
)
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


READ_NAMES = frozenset({"schedule_list", "schedule_view"})
READ_DEFINITIONS = [
    {"name":"schedule_list","description":
     "查询当前会话的实际定时计划，不包含其他会话。active=true 只显示仍有效的计划，false 显示暂停或已结束计划，省略显示全部。limit 1-20，默认10；offset 默认0。列表只含指令预览；需要完整指令时使用 schedule_view。列表是读取时的状态，非执行成功证明。",
     "input_schema":{"type":"object","properties":{
         "active":{"type":"boolean"},"limit":{"type":"integer"},"offset":{"type":"integer"}},
         "additionalProperties":False}},
    {"name":"schedule_view","description":
     "按真实计划 ID 查看当前会话的计划内容、下次时间、状态及最近执行状态。不要猜测 ID；先使用 schedule_list。此工具不修改计划。",
     "input_schema":{"type":"object","properties":{"id":{"type":"string"}},
         "required":["id"],"additionalProperties":False}},
]
EDIT_NAME = "schedule_edit"
EDIT_CANONICAL = "mcp__workspace__schedule_edit"
EDIT_DEFINITION = {
    "name":EDIT_NAME,
    "description":(
        "仅在用户明确要求管理当前会话的计划时调用。先用 schedule_view 取得真实 id 与 expectedUpdatedAt（宿主版本摘要）。"
        "action=delete 表示取消未来计划而非删除历史；action=update 可传 prompt、rrule（仅原 RRULE 计划）或单独 active。"
        "active=false 暂停周期，true 恢复未来周期，不补跑旧任务。不要混合生命周期和内容修改；"
        "不修改目标、标题、执行模式或频率类别。失败未确认、执行中、已到期或过长指令，请使用网页任务中心。"
        "完整变更预览必须本人批准，每个前台轮次最多成功创建或修改一个计划。"
    ),
    "input_schema":{"type":"object","properties":{
        "id":{"type":"string"},"action":{"type":"string"},
        "expectedUpdatedAt":{"type":"string"},"prompt":{"type":"string"},
        "rrule":{"type":"string"},"active":{"type":"boolean"}},
        "required":["id","action","expectedUpdatedAt"],"additionalProperties":False},
}
NAMES = READ_NAMES | {NAME, EDIT_NAME}

def read_arguments(name, args):
    if type(args) is not dict:
        raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","计划查询参数无效")
    if name=="schedule_view":
        if (set(args)!={"id"} or type(args["id"]) is not str
                or not re.fullmatch(r"(?:series|schedule)-[A-Za-z0-9_-]{1,100}",args["id"])):
            raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","请输入有效的计划编号")
        return args
    if (set(args)-{"active","limit","offset"}
            or ("active" in args and type(args["active"]) is not bool)
            or type(args.get("limit",10)) is not int or not 1<=args.get("limit",10)<=20
            or type(args.get("offset",0)) is not int or not 0<=args.get("offset",0)<=10000):
        raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","计划查询参数或分页范围无效")
    return args

# Use the authoritative plan, queue and run rows, never a model-supplied
# conversation/user filter. Only top-level plans, not independent occurrences,
# appear in the directory. This mirrors the upstream list/view contract
# within the caller's session, rather than its Host-wide storage.
_PLAN_SQL = """WITH plans AS (
    SELECT p.id,p.text,p.recurrence,p.execution_mode,p.status,p.next_at AS scheduled_at,
           p.timezone,p.rule_expression,p.created_at,
           NULL AS last_run_id,NULL AS last_run_status
      FROM conversation_schedule_series p JOIN conversations c ON c.id=p.conversation_id
     WHERE c.id=? AND c.user_id=?
    UNION ALL
    SELECT s.id,s.text,'once',s.execution_mode,COALESCE(r.status,q.status,s.status),
           s.scheduled_at,NULL,NULL,s.created_at,r.id,r.status
      FROM conversation_schedules s JOIN conversations c ON c.id=s.conversation_id
      LEFT JOIN message_queue q ON q.conversation_id=c.id AND q.id=s.id
      LEFT JOIN runs r ON r.id=q.run_id AND r.conversation_id=c.id AND r.user_id=c.user_id
     WHERE c.id=? AND c.user_id=? AND s.series_id IS NULL
) SELECT * FROM plans"""
_ACTIVE = "('scheduled','pending','queued','accepted','running','awaiting_approval','cancelling')"

def _project_plan(db, row, cid, *, full):
    # Bound provider context; never send diagnostic errors, credentials, or
    # approval records as part of a schedule query.
    if _editable_row(row):
        row["expectedUpdatedAt"]=edit_token(row)
    text=row.pop("text")
    cap=4000 if full else 240
    row["prompt"]=text[:cap]
    row["prompt_truncated"]=len(text)>cap
    row["active"]=row["status"] in {"scheduled","pending","queued","accepted","running","awaiting_approval","cancelling"}
    if row["recurrence"]!="once":
        latest=db.one("""SELECT s.id AS occurrence_id,COALESCE(r.status,q.status,s.status) AS status,r.id AS run_id
            FROM conversation_schedules s
            LEFT JOIN message_queue q ON q.id=s.id AND q.conversation_id=s.conversation_id
            LEFT JOIN runs r ON r.id=q.run_id AND r.conversation_id=s.conversation_id
            WHERE s.conversation_id=? AND s.series_id=? ORDER BY s.created_at DESC,s.id DESC LIMIT 1""",(cid,row["id"]))
        row["latest_occurrence"]=latest
    # For inactive recurring rules next_at is the last stored time, not a
    # promise of another delivery.
    row["next_occurrence_at"]=row["scheduled_at"] if row["status"]=="scheduled" else None
    return row

def read(runtime,run_id,name,args,recheck):
    service=runtime.service
    args=read_arguments(name,args)
    with service.db.lock:
        recheck()
        run=service.require("runs",run_id)
        user=service.require("users",run["user_id"])
        conversation=service.require("conversations",runtime.conversation_id,user)
        if (runtime.config.get("subagent_child_id") or runtime.config.get("recovery_only")
                or not user["enabled"] or run["conversation_id"]!=conversation["id"]
                or conversation["active_run_id"]!=run_id or conversation["archived"]
                or json.loads(run.get("execution_meta") or "{}").get("schedule_id")):
            raise AppError(403,"SCHEDULE_CALLER_DENIED","计划查询仅支持当前人工会话")
        cid=conversation["id"]
        params=(cid,user["id"],cid,user["id"])
        if name=="schedule_view":
            row=service.db.one(_PLAN_SQL+" WHERE id=?",(*params,args["id"]))
            if row is None:
                raise AppError(404,"SCHEDULE_NOT_FOUND","当前会话中没有该计划")
            return {"scope":"this_session","item":_project_plan(service.db,row,cid,full=True)}
        clauses=""
        if "active" in args:
            clauses=" WHERE status "+("IN " if args["active"] else "NOT IN ")+_ACTIVE
        limit,offset=args.get("limit",10),args.get("offset",0)
        rows=service.db.all(_PLAN_SQL+clauses+" ORDER BY CASE WHEN status IN "+_ACTIVE+
            " THEN 0 ELSE 1 END,CASE WHEN status IN "+_ACTIVE+
            " THEN scheduled_at END ASC,created_at DESC,id DESC LIMIT ? OFFSET ?",
            (*params,limit+1,offset))
        items=[_project_plan(service.db,row,cid,full=False) for row in rows[:limit]]
        return {"scope":"this_session","items":items,"next_offset":offset+limit if len(rows)>limit else None}


def check_context(runtime, run_id, canonical=CANONICAL):
    service = runtime.service
    if runtime.config.get("subagent_child_id") or runtime.config.get("recovery_only"):
        raise AppError(403, "SCHEDULE_CALLER_DENIED", "子智能体或恢复轮次不能创建定时计划")
    runtime.check_tool(canonical, {})
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
    if (service.db.one("SELECT run_id FROM conversation_schedule_create_requests WHERE run_id=?", (run_id,))
            or service.db.one("SELECT run_id FROM conversation_schedule_edit_requests WHERE run_id=?", (run_id,))):
        raise AppError(409, "SCHEDULE_ALREADY_CHANGED", "本轮已成功创建或修改计划，请在任务中心核对")
    return user


def explicit_rrule(value):
    rule = value.strip()
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
    return rule,zone


def prepare(runtime, run_id, arguments):
    if (type(arguments) is not dict or set(arguments) != {"title", "prompt", "rrule", "target"}
            or any(type(v) is not str for v in arguments.values())
            or arguments["target"] != "this_session"
            or any(not 1 <= len(arguments[k].strip()) <= n for k,n in
                   (("title",120),("prompt",4000),("rrule",2048)))
            or any(len(arguments[k]) > n for k,n in (("title",120),("prompt",4000),("rrule",2048)))):
        raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","定时计划参数无效，只能创建当前会话的计划")
    user = check_context(runtime, run_id)
    rule,zone = explicit_rrule(arguments["rrule"])
    body = ScheduleCreate(text=arguments["prompt"].strip(), recurrence="rrule",
                          execution_mode="agent", rule_expression=rule, timezone=zone)
    prepared = prepare_schedule(runtime.service,user,runtime.conversation_id,body)
    stamp, expression, effective_zone, local_zone = prepared
    preview = {"title":arguments["title"].strip(), "prompt":body.text,
               "rrule":expression, "target":"this_session", "timezone":effective_zone,
               "first_occurrence_utc":stamp.isoformat(),
               "first_occurrence_local":stamp.astimezone(local_zone).isoformat()}
    return body, prepared, preview


def owner_approval(service,user,run_id,canonical,preview,approval):
    row = service.db.one("SELECT * FROM approvals WHERE id=? AND run_id=? AND tool_name=?",
                         (getattr(approval,"approval_id",None),run_id,canonical))
    if (not row or row["status"]!="allowed" or row["user_id"]!=user["id"]
            or row.get("decided_by")!=user["id"] or row.get("decision_source")!="user"
            or row["arguments"]!=dump(preview) or row.get("strategy")=="model"
            or row.get("approval_mode")=="dual"
            or datetime.fromisoformat(row["expires_at"])<=datetime.now(timezone.utc)):
        raise AppError(403,"SCHEDULE_HUMAN_APPROVAL_REQUIRED","没有与此完整计划一致的本人批准记录")
    return row


def commit(runtime,run_id,body,prepared,preview,approval,recheck):
    service = runtime.service
    with service.db.transaction():
        recheck()
        user = check_context(runtime,run_id)
        row = owner_approval(service,user,run_id,CANONICAL,preview,approval)
        result = persist_schedule(service,user,runtime.conversation_id,body,prepared)
        service.db.execute("INSERT INTO conversation_schedule_create_requests VALUES(?,?,?,?)",
                           (run_id,row["id"],result["id"],now()))
        return {**result,"title":preview["title"],"timezone":preview["timezone"],
                "first_occurrence_local":preview["first_occurrence_local"]}

def prepare_edit(runtime,run_id,args):
    if type(args) is not dict or not {"id","action","expectedUpdatedAt"} <= set(args) or set(args)-{
        "id","action","expectedUpdatedAt","prompt","rrule","active"}:
        raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","计划修改参数无效")
    read_arguments("schedule_view",{"id":args["id"]})
    if (type(args["expectedUpdatedAt"]) is not str or not re.fullmatch(r"[a-f0-9]{64}",args["expectedUpdatedAt"])
            or args["action"] not in ("update","delete")):
        raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","请使用 schedule_view 返回的计划版本")
    fields=set(args)-{"id","action","expectedUpdatedAt"}
    if ((args["action"]=="delete" and fields) or
            (args["action"]=="update" and (not fields or ("active" in fields and fields!={"active"}))) or
            ("active" in args and type(args["active"]) is not bool) or
            any(type(args[k]) is not str or not 1<=len(args[k].strip())<=n or len(args[k])>n
                for k,n in (("prompt",4000),("rrule",2048)) if k in args)):
        raise AppError(422,"SCHEDULE_ARGUMENTS_INVALID","取消或生命周期操作须与内容修改分开确认")
    service=runtime.service;db=service.db;cid=runtime.conversation_id
    user=check_context(runtime,run_id,EDIT_CANONICAL)
    identity=args["id"]
    snapshot,is_series=_edit_target(db,cid,identity)
    previous=_previous(db,identity) if is_series else None
    if args["expectedUpdatedAt"]!=edit_token(snapshot):
        raise AppError(409,"SCHEDULE_CHANGED","计划版本已变化，请重新查看")
    _available(service,cid,snapshot["execution_mode"])
    _require_editable(db,snapshot,is_series,previous,datetime.now(timezone.utc))
    recurrence=snapshot.get("recurrence","once")
    if recurrence not in ("once","daily","weekly","cron","rrule") or len(snapshot["text"])>4000:
        raise AppError(409,"SCHEDULE_WEB_REQUIRED","当前计划需要在网页任务中心管理")
    old_time=snapshot.get("next_at",snapshot.get("scheduled_at"))
    old={"prompt":snapshot["text"],"status":snapshot["status"],"scheduled_at":old_time,
         "recurrence":recurrence,"rule_expression":snapshot.get("rule_expression"),
         "timezone":snapshot.get("timezone"),"execution_mode":snapshot["execution_mode"]}
    prepared=None
    if args["action"]=="delete":
        operation="cancel"
        new={**old,"status":"cancelled"}
        effect="取消尚未执行的计划，保留历史；不会撤回已完成的业务操作。"
    elif "active" in args:
        if not is_series or snapshot["status"]!=("paused" if args["active"] else "scheduled"):
            raise AppError(409,"SCHEDULE_INACTIVE","当前周期状态不适合该操作")
        if args["active"]:
            operation="resume"
            # Failure acknowledgment is a separate owner action in the UI.
            prepared=prepare_schedule_resume(service,user,cid,identity,ScheduleResume())
            if prepared.get("already_scheduled") or prepared["next_at"] is None:
                raise AppError(409,"SCHEDULE_INACTIVE","没有可恢复的未来周期，请在网页管理")
            new={**old,"status":"scheduled","scheduled_at":prepared["next_at"]}
            effect="只恢复未来周期，不重试或补跑历史任务。"
        else:
            operation="pause"
            new={**old,"status":"paused"}
            effect="暂停未来周期，保留计划规则及历史。"
    else:
        operation="edit"
        timing={}
        if "rrule" in args:
            if recurrence!="rrule":
                raise AppError(409,"SCHEDULE_WEB_REQUIRED","仅原 RRULE 计划支持对话内改规则；其他计划请在网页管理")
            rule,zone=explicit_rrule(args["rrule"])
            timing={"rule_expression":rule,"timezone":zone}
        body=ScheduleEdit(text=args.get("prompt",snapshot["text"]),expected_token=args["expectedUpdatedAt"],**timing)
        prepared=prepare_schedule_edit(service,user,cid,identity,body)
        new={**old,"prompt":prepared["text"],"scheduled_at":prepared["next_at"],
             "rule_expression":prepared["expression"],"timezone":prepared["zone_name"]}
        effect="仅修改未来计划，保持原执行模式、频率类别、目标与历史。修改有限 RRULE 会以新规则重新计算未来次数。"
    if prepared is not None and prepared["snapshot"]!=snapshot:
        raise AppError(409,"SCHEDULE_CHANGED","计划已变化，请重新查看")
    preview={"id":identity,"operation":operation,"expectedUpdatedAt":args["expectedUpdatedAt"],
             "before":old,"after":new,"effect":effect,"target":"this_session"}
    return {"snapshot":snapshot,"is_series":is_series,"previous":previous,
            "operation":operation,"prepared":prepared},None,preview


def commit_edit(runtime,run_id,proposal,unused,preview,approval,recheck):
    service=runtime.service;db=service.db;cid=runtime.conversation_id
    identity=preview["id"];operation=proposal["operation"]
    with db.transaction():
        recheck()
        user=check_context(runtime,run_id,EDIT_CANONICAL)
        receipt=owner_approval(service,user,run_id,EDIT_CANONICAL,preview,approval)
        current,is_series=_edit_target(db,cid,identity)
        previous=_previous(db,identity) if is_series else None
        if (current!=proposal["snapshot"] or is_series!=proposal["is_series"] or previous!=proposal["previous"]):
            raise AppError(409,"SCHEDULE_CHANGED","批准期间计划或执行状态发生变化，请重新查看")
        _available(service,cid,current["execution_mode"])
        _require_editable(db,current,is_series,previous,datetime.now(timezone.utc))
        if operation=="edit":
            result=persist_schedule_edit(service,user,cid,identity,proposal["prepared"])
        elif operation=="resume":
            result=persist_schedule_resume(service,user,cid,identity,proposal["prepared"])
        elif operation=="pause":
            result=persist_schedule_pause(service,user,cid,identity)
        else:
            persist_schedule_cancel(service,user,cid,identity)
            result={"id":identity,"status":"cancelled"}
        db.execute("INSERT INTO conversation_schedule_edit_requests VALUES(?,?,?,?,?,?)",
                   (run_id,receipt["id"],cid,identity,operation,now()))
        return {**result,"operation":operation,"scope":"this_session"}
