"""Persistent scheduled conversation runs using the existing durable FIFO."""
import hashlib
import json
from datetime import datetime, timedelta, timezone, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Query, Response
from pydantic import AwareDatetime, Field

from .db import dump, now, uid
from .models import InputModel, SendMessage
from .security import AppError


class ScheduleCreate(InputModel):
    text: str = Field(min_length=1, max_length=50000)
    scheduled_at: AwareDatetime | None = None
    execution_mode: Literal["text", "agent"] = "text"
    recurrence: Literal["once", "daily", "weekly", "cron", "rrule"] = "once"
    rule_expression: str | None = Field(default=None, max_length=2048)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)


def _calendar_slot(start, clock, zone, step, periods):
    local = datetime.combine(start + timedelta(days=periods*step), clock)
    candidate = local.replace(tzinfo=zone, fold=0).astimezone(timezone.utc)
    # Nonexistent local time is skipped rather than shifting the task.
    # For a repeated local time fold=0 selects the earlier instant.
    return candidate if candidate.astimezone(zone).replace(tzinfo=None)==local else None


def series_slots(series, after):
    """Return the next slot, latest due slot and skipped-period count."""
    zone = ZoneInfo(series["timezone"])
    step = 1 if series["recurrence"] == "daily" else 7
    original = datetime.fromisoformat(series["next_at"])
    start = original.astimezone(zone).date()
    clock = time.fromisoformat(series["local_time"])
    distance = (after.astimezone(zone).date() - start).days
    periods = max(1, distance // step)
    candidate = _calendar_slot(start,clock,zone,step,periods)
    while candidate is None or candidate <= after:
        periods += 1
        candidate = _calendar_slot(start,clock,zone,step,periods)
    latest=original
    for index in range(periods-1,0,-1):
        earlier=_calendar_slot(start,clock,zone,step,index)
        if earlier is not None and earlier<=after:
            latest=earlier
            break
    return candidate.isoformat(), latest.isoformat(), max(0, periods-1)


def next_slot(series,after):
    following,_,missed=series_slots(series,after)
    return following,missed


def _available(service, cid, mode):
    conversation = service.require("conversations", cid)
    user = service.require("users", conversation["user_id"])
    if not user["enabled"] or conversation["archived"]:
        raise AppError(409,"SCHEDULE_UNAVAILABLE","会话已归档或账号已停用")
    service.usable_assistant(conversation["assistant_id"],user)
    if mode=="agent" and conversation.get("runtime_type")!="harness":
        raise AppError(409,"SCHEDULE_AGENT_UNAVAILABLE","智能体定时任务仅支持 Harness 会话")
    return conversation,user


class ScheduleEdit(InputModel):
    text: str = Field(min_length=1,max_length=50000)
    expected_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    scheduled_at: AwareDatetime | None = None
    rule_expression: str | None = Field(default=None,max_length=2048)
    timezone: str | None = Field(default=None,min_length=1,max_length=100)


def edit_token(row):
    # Compare the visible plan version, including lifecycle and time. No
    # persisted second source of truth or mutable client-supplied revision.
    fields={"id":row["id"],"text":row["text"],"status":row["status"],
            "scheduled_at":row.get("next_at",row.get("scheduled_at")),
            "recurrence":row.get("recurrence","once"),
            "execution_mode":row["execution_mode"],
            "timezone":row.get("timezone"),"rule_expression":row.get("rule_expression")}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,separators=(",",":")).encode()).hexdigest()


def _editable_row(row):
    return (row.get("recurrence","once")!="once" and row["status"] in {"scheduled","paused"}
            or row.get("recurrence","once")=="once" and not row.get("series_id") and row["status"]=="scheduled")


def with_edit_tokens(rows):
    return [{**row, "edit_token":edit_token(row)} if _editable_row(row) else row for row in rows]


def _edit_target(db,cid,identity):
    row=db.one("SELECT * FROM conversation_schedule_series WHERE id=? AND conversation_id=?",(identity,cid))
    if row:
        return row,True
    row=db.one("SELECT * FROM conversation_schedules WHERE id=? AND conversation_id=?",(identity,cid))
    if not row:
        raise AppError(404,"NOT_FOUND","计划任务不存在")
    return row,False


def _require_editable(db,row,is_series,previous,at):
    if not _editable_row(row):
        raise AppError(409,"SCHEDULE_NOT_EDITABLE","仅可修改尚未启动的单次任务或有效周期计划")
    if is_series:
        if previous and not _terminal(previous):
            raise AppError(409,"SCHEDULE_OCCURRENCE_ACTIVE","上一次任务尚未结束，请先检查会话")
        if row["status"]=="scheduled" and _failed(previous) and previous["id"]!=row["acknowledged_occurrence_id"]:
            raise AppError(409,"SCHEDULE_FAILURE_ACK_REQUIRED","请先处理上一次失败并明确恢复")
    elif db.one("SELECT id FROM message_queue WHERE conversation_id=? AND id=?",(row["conversation_id"],row["id"])):
        raise AppError(409,"SCHEDULE_STARTED","任务已经进入队列，不能再修改计划")
    if row["status"]=="scheduled" and datetime.fromisoformat(row.get("next_at",row.get("scheduled_at")))<=at:
        raise AppError(409,"SCHEDULE_DUE","计划已到期，请刷新任务状态")


class ScheduleResume(InputModel):
    acknowledge_failure: bool = False


def _previous(db, identity):
    return db.one("""SELECT s.id,s.status AS schedule_status, q.status AS queue_status,
        q.error AS queue_error,r.status AS run_status FROM conversation_schedules s
        LEFT JOIN message_queue q ON q.id=s.id AND q.conversation_id=s.conversation_id
        LEFT JOIN runs r ON r.id=q.run_id WHERE s.series_id=?
        ORDER BY s.created_at DESC,s.id DESC LIMIT 1""",(identity,))


def _failed(previous):
    return bool(previous and (previous["queue_error"] or any(
        previous[key] in {"failed","cancelled","interrupted"}
        for key in ("schedule_status","queue_status","run_status"))))


def _terminal(previous):
    if previous["run_status"] is not None:
        return previous["run_status"] in {"succeeded","failed","cancelled","interrupted"}
    return previous["queue_status"] == "cancelled" or (
        previous["queue_status"] is None and previous["schedule_status"] in {"failed","cancelled"})


def enqueue_due_series(service):
    db=service.db
    # Pause a failed series promptly, not only when its next slot arrives.
    # Never replay a possibly partially executed operation automatically.
    with db.transaction():
        db.execute("""UPDATE conversation_schedule_series AS series SET status='paused',error='上一次执行未成功，已暂停后续周期；请检查会话任务'
            WHERE series.status='scheduled' AND EXISTS (
              SELECT 1 FROM conversation_schedules s
              LEFT JOIN message_queue q ON q.id=s.id AND q.conversation_id=s.conversation_id
              LEFT JOIN runs r ON r.id=q.run_id
              WHERE s.id=(SELECT latest.id FROM conversation_schedules latest WHERE latest.series_id=series.id
                  ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
              AND s.id<>COALESCE(series.acknowledged_occurrence_id,'')
              AND (s.status IN ('failed','cancelled') OR q.status='cancelled' OR q.error IS NOT NULL
                OR r.status IN ('failed','cancelled','interrupted'))
            )""")
    # Keep legacy schedules ahead of the optional Node rule engine; cap the
    # expensive advanced evaluation to one task per scheduler scan.
    advanced_evaluated=False
    for due in db.all("SELECT id FROM conversation_schedule_series WHERE status='scheduled' AND next_at<=? ORDER BY CASE WHEN recurrence IN ('cron','rrule') THEN 1 ELSE 0 END,next_at,id LIMIT 20",(now(),)):
        # Rule calculation may spawn the pinned pure Node module. Do it outside
        # the SQLite write transaction, then compare the authoritative snapshot.
        snapshot=db.one("SELECT * FROM conversation_schedule_series WHERE id=? AND status='scheduled' AND next_at<=?",(due["id"],now()))
        if not snapshot:
            continue
        decision=datetime.now(timezone.utc)
        advanced=snapshot["recurrence"] in ("cron","rrule")
        calculation_error=None
        if advanced:
            if advanced_evaluated:
                continue
            advanced_evaluated=True
            from .official_schedule_rules import resolve_rule
            try:
                next_at,latest_due=resolve_rule(service.settings,snapshot["recurrence"],snapshot["rule_expression"],snapshot["timezone"],snapshot["next_at"],decision)
                missed=0
                coalesced=int(datetime.fromisoformat(latest_due)>datetime.fromisoformat(snapshot["next_at"]))
            except AppError as exc:
                calculation_error=exc.message
        else:
            next_at,latest_due,missed=series_slots(snapshot,decision)
            coalesced=0
        with db.transaction():
            series=db.one("SELECT * FROM conversation_schedule_series WHERE id=? AND status='scheduled' AND next_at<=?",(due["id"],now()))
            if not series or series != snapshot:
                continue
            if calculation_error:
                db.execute("UPDATE conversation_schedule_series SET status='paused',error=? WHERE id=?",(calculation_error,series["id"]))
                continue
            try:
                _available(service,series["conversation_id"],series["execution_mode"])
            except AppError as exc:
                db.execute("UPDATE conversation_schedule_series SET status='paused',error=? WHERE id=?",(exc.message,series["id"]))
                continue
            previous=_previous(db,series["id"])
            if previous and previous["id"]==series["acknowledged_occurrence_id"]:
                previous=None
            if _failed(previous):
                db.execute("UPDATE conversation_schedule_series SET status='paused',error=? WHERE id=?",
                           ("上一次执行未成功，已暂停后续周期；请检查会话任务",series["id"]))
                continue
            pending=bool(previous and previous["run_status"]!="succeeded")
            if not pending:
                identity="schedule-"+uid()
                db.execute("""INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,execution_mode,series_id)
                    VALUES(?,?,?,?,'scheduled',?,?,?)""",(identity,series["conversation_id"],series["text"],latest_due,now(),series["execution_mode"],series["id"]))
            # A finite upstream rule can exhaust. The final occurrence has
            # its own independent execution status; exhausted is not success.
            db.execute("UPDATE conversation_schedule_series SET next_at=?,status=?,skipped_count=skipped_count+?,coalesced_batches=coalesced_batches+? WHERE id=?",
                       (next_at or latest_due,"scheduled" if next_at else "exhausted",missed+int(pending),coalesced,series["id"]))


def enqueue_due_schedules(service):
    db=service.db
    enqueue_due_series(service)
    for due in db.all("SELECT id FROM conversation_schedules WHERE status='scheduled' AND scheduled_at<=? ORDER BY scheduled_at,id LIMIT 20",(now(),)):
        with db.transaction():
            row=db.one("SELECT * FROM conversation_schedules WHERE id=? AND status='scheduled'",(due["id"],))
            if row is None:
                continue
            conversation_id=row["conversation_id"]
            try:
                _available(service,conversation_id,row.get("execution_mode","text"))
            except AppError as error:
                db.execute("UPDATE conversation_schedules SET status='failed',error=? WHERE id=?",(error.message,row["id"]))
                continue
            if db.one("SELECT COUNT(*) AS n FROM message_queue WHERE conversation_id=? AND status='pending'",(conversation_id,))["n"]>=20:
                db.execute("UPDATE conversation_schedules SET status='failed',error=? WHERE id=?",("到期时会话待发队列已满",row["id"]))
                continue
            timestamp=now()
            payload=SendMessage(client_request_id=row["id"],text=row["text"],auto_route=False).model_dump()
            db.execute("INSERT INTO message_queue(id,conversation_id,payload,created_at,updated_at) VALUES(?,?,?,?,?)",
                       (row["id"],conversation_id,dump(payload),timestamp,timestamp))
            db.execute("UPDATE conversation_schedules SET status='queued' WHERE id=?",(row["id"],))


# Global management uses the same authoritative rows and existing scoped
# mutation routes. This projection does not enqueue or wake agents.
def schedule_catalog(service, user, scope, limit, before):
    if not service.require("users",user["id"])["enabled"]:
        raise AppError(403,"USER_DISABLED","账号已停用")
    args=[user["id"],user["id"]]
    terminal="('succeeded','failed','cancelled','interrupted','exhausted')"
    clauses=["status NOT IN "+terminal if scope=="active" else "status IN "+terminal]
    if before:
        try:
            stamp,identity=before.split("|",1)
            if not identity or len(identity)>128 or datetime.fromisoformat(stamp).tzinfo is None:
                raise ValueError()
        except (ValueError,TypeError):
            raise AppError(422,"SCHEDULE_CURSOR_INVALID","任务列表游标无效，请刷新") from None
        clauses.append("(created_at < ? OR (created_at = ? AND id < ?))")
        args.extend((stamp,stamp,identity))
    args.append(limit+1)
    # Only return task content and execution status that the owner can see in
    # their own conversation. No admin bypass, other owners or raw logs.
    rows=service.db.all("""WITH catalog AS (
        SELECT s.id,s.conversation_id,c.title AS conversation_title,c.archived AS conversation_archived,
            s.text,s.next_at AS scheduled_at,s.status,s.error,s.execution_mode,s.recurrence,
            s.timezone,s.rule_expression,s.skipped_count,s.coalesced_batches,NULL AS series_id,
            NULL AS run_id,NULL AS message_id,s.created_at,'series' AS kind
        FROM conversation_schedule_series s JOIN conversations c ON c.id=s.conversation_id
        WHERE c.user_id=?
        UNION ALL
        SELECT s.id,s.conversation_id,c.title,c.archived,s.text,s.scheduled_at,
            COALESCE(r.status,q.status,s.status),COALESCE(r.error,q.error,s.error),
            s.execution_mode,'once',NULL,NULL,0,0,s.series_id,r.id,r.message_id,s.created_at,
            CASE WHEN s.series_id IS NULL THEN 'once' ELSE 'occurrence' END
        FROM conversation_schedules s JOIN conversations c ON c.id=s.conversation_id
        LEFT JOIN message_queue q ON q.conversation_id=s.conversation_id AND q.id=s.id
        LEFT JOIN runs r ON r.id=q.run_id AND r.conversation_id=c.id AND r.user_id=c.user_id
        WHERE c.user_id=?
    ) SELECT * FROM catalog WHERE """+" AND ".join(clauses)+
        " ORDER BY created_at DESC,id DESC LIMIT ?",tuple(args))
    more=len(rows)>limit
    items=with_edit_tokens(rows[:limit])
    return {"items":items,"next_cursor":items[-1]["created_at"]+"|"+items[-1]["id"] if more else None}


def create_schedule_router(svc,authenticated):
    router=APIRouter(prefix="/api/v1/conversations/{conversation_id}/schedules")

    @router.get("")
    def list_schedules(conversation_id:str,service=Depends(svc),user=Depends(authenticated)):
        service.require("conversations",conversation_id,user)
        occurrences=service.db.all("""SELECT s.id,s.text,s.scheduled_at,COALESCE(r.status,q.status,s.status) AS status,
            COALESCE(r.error,q.error,s.error) AS error,r.id AS run_id,s.execution_mode,s.series_id,'once' AS recurrence
            FROM conversation_schedules s LEFT JOIN message_queue q ON q.conversation_id=s.conversation_id AND q.id=s.id
            LEFT JOIN runs r ON r.id=q.run_id WHERE s.conversation_id=? ORDER BY s.created_at DESC,s.id DESC LIMIT 100""",(conversation_id,))
        series=service.db.all("""SELECT id,text,next_at AS scheduled_at,status,error,execution_mode,recurrence,timezone,skipped_count,rule_expression,coalesced_batches
            FROM conversation_schedule_series WHERE conversation_id=? ORDER BY created_at DESC,id DESC LIMIT 100""",(conversation_id,))
        return with_edit_tokens(series+occurrences)

    @router.post("",status_code=201)
    def create_schedule(conversation_id:str,body:ScheduleCreate,service=Depends(svc),user=Depends(authenticated)):
        service.require("conversations",conversation_id,user)
        current=datetime.now(timezone.utc)
        if not body.text.strip():
            raise AppError(422,"SCHEDULE_INVALID","请输入任务内容")
        advanced=body.recurrence in ("cron","rrule")
        expression=None
        effective_zone=body.timezone
        if advanced:
            if body.scheduled_at is not None:
                raise AppError(422,"SCHEDULE_INVALID","高级规则由官方模块计算首次执行时间")
            _available(service,conversation_id,body.execution_mode)
            from .official_schedule_rules import create_rule
            next_at,expression,effective_zone=create_rule(service.settings,body.recurrence,body.rule_expression,effective_zone,current)
            stamp=datetime.fromisoformat(next_at)
        else:
            if body.rule_expression is not None or body.scheduled_at is None:
                raise AppError(422,"SCHEDULE_INVALID","普通计划需要明确执行时间")
            stamp=body.scheduled_at.astimezone(timezone.utc)
            if not current<stamp<=current+timedelta(days=365):
                raise AppError(422,"SCHEDULE_INVALID","请选择未来一年内的执行时间")
        try:
            zone=ZoneInfo(effective_zone)
        except (ZoneInfoNotFoundError,ValueError):
            raise AppError(422,"SCHEDULE_TIMEZONE_INVALID","请选择有效的时区") from None
        # This setting is present in full builds. Keep the existing review
        # when enabled, and re-review at queue dispatch through MessageQueue.
        prompt_safety=getattr(service,"prompt_safety",None)
        if prompt_safety is not None and prompt_safety.enabled:
            service.require("conversations",conversation_id,user)
            prompt_safety.require_allowed(body.text,user["id"])
        with service.db.transaction():
            service.require("conversations",conversation_id,user)
            _available(service,conversation_id,body.execution_mode)
            count=service.db.one("""SELECT COUNT(*) AS n FROM conversation_schedules s JOIN conversations c ON c.id=s.conversation_id
                LEFT JOIN message_queue q ON q.conversation_id=s.conversation_id AND q.id=s.id
                WHERE c.user_id=? AND s.series_id IS NULL AND (s.status='scheduled' OR q.status='pending')""",(user["id"],))["n"]
            count+=service.db.one("""SELECT COUNT(*) AS n FROM conversation_schedule_series s JOIN conversations c ON c.id=s.conversation_id
                WHERE c.user_id=? AND s.status IN ('scheduled','paused')""",(user["id"],))["n"]
            if count>=20:
                raise AppError(409,"SCHEDULE_LIMIT","最多保留 20 项未执行或周期计划")
            if body.recurrence=="once":
                identity="schedule-"+uid()
                service.db.execute("""INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,execution_mode)
                    VALUES(?,?,?,?,'scheduled',?,?)""",(identity,conversation_id,body.text.strip(),stamp.isoformat(),now(),body.execution_mode))
            else:
                identity="series-"+uid()
                service.db.execute("""INSERT INTO conversation_schedule_series(id,conversation_id,text,next_at,recurrence,timezone,local_time,execution_mode,created_at,rule_expression)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",(identity,conversation_id,body.text.strip(),stamp.isoformat(),body.recurrence,effective_zone,
                                                 stamp.astimezone(zone).time().isoformat(),body.execution_mode,now(),expression))
        return {"id":identity,"status":"scheduled","execution_mode":body.execution_mode,"recurrence":body.recurrence,"scheduled_at":stamp.isoformat()}

    @router.patch("/{identity}")
    def edit_schedule(conversation_id:str,identity:str,body:ScheduleEdit,service=Depends(svc),user=Depends(authenticated)):
        db=service.db
        service.require("conversations",conversation_id,user)
        snapshot,is_series=_edit_target(db,conversation_id,identity)
        if body.expected_token!=edit_token(snapshot):
            raise AppError(409,"SCHEDULE_CHANGED","计划已发生变化，请刷新后重试")
        previous=_previous(db,identity) if is_series else None
        reference=datetime.now(timezone.utc)
        _available(service,conversation_id,snapshot["execution_mode"])
        _require_editable(db,snapshot,is_series,previous,reference)
        text=body.text.strip()
        if not text:
            raise AppError(422,"SCHEDULE_INVALID","请输入任务内容")
        recurrence=snapshot.get("recurrence","once")
        advanced=recurrence in {"cron","rrule"}
        changed_timing=body.scheduled_at is not None or body.rule_expression is not None or body.timezone is not None
        next_at=snapshot.get("next_at",snapshot.get("scheduled_at"))
        expression=snapshot.get("rule_expression")
        zone_name=snapshot.get("timezone")
        local_time=snapshot.get("local_time")
        if changed_timing:
            if advanced:
                if body.scheduled_at is not None or body.rule_expression is None:
                    raise AppError(422,"SCHEDULE_INVALID","高级规则需要完整的新规则")
                from .official_schedule_rules import create_rule
                next_at,expression,zone_name=create_rule(service.settings,recurrence,body.rule_expression,body.timezone or zone_name,reference)
            else:
                if body.rule_expression is not None or body.scheduled_at is None:
                    raise AppError(422,"SCHEDULE_INVALID","普通计划需要明确的新执行时间")
                stamp=body.scheduled_at.astimezone(timezone.utc)
                if not reference<stamp<=reference+timedelta(days=365):
                    raise AppError(422,"SCHEDULE_INVALID","请选择未来一年内的执行时间")
                if not is_series and body.timezone is not None:
                    raise AppError(422,"SCHEDULE_INVALID","单次任务使用明确的绝对执行时间")
                next_at=stamp.isoformat()
                if is_series:
                    zone_name=body.timezone or zone_name
            if is_series:
                try:
                    local_time=datetime.fromisoformat(next_at).astimezone(ZoneInfo(zone_name)).time().isoformat()
                except (ZoneInfoNotFoundError,ValueError,TypeError):
                    raise AppError(422,"SCHEDULE_TIMEZONE_INVALID","请选择有效的时区") from None
        prompt_safety=getattr(service,"prompt_safety",None)
        if prompt_safety is not None and prompt_safety.enabled:
            prompt_safety.require_allowed(text,user["id"])
        with db.transaction():
            service.require("conversations",conversation_id,user)
            _available(service,conversation_id,snapshot["execution_mode"])
            current,current_series=_edit_target(db,conversation_id,identity)
            if current!=snapshot or current_series!=is_series or (is_series and _previous(db,identity)!=previous):
                raise AppError(409,"SCHEDULE_CHANGED","计划或运行状态已变化，请刷新后重试")
            current_time=datetime.now(timezone.utc)
            _require_editable(db,current,is_series,previous,current_time)
            if (changed_timing or current["status"]=="scheduled") and datetime.fromisoformat(next_at)<=current_time:
                raise AppError(409,"SCHEDULE_CHANGED","编辑期间计划已到期，请刷新后重试")
            if is_series:
                db.execute("""UPDATE conversation_schedule_series SET text=?,next_at=?,timezone=?,local_time=?,rule_expression=? WHERE id=?""",
                           (text,next_at,zone_name,local_time,expression,identity))
            else:
                db.execute("UPDATE conversation_schedules SET text=?,scheduled_at=? WHERE id=?",(text,next_at,identity))
            saved,_=_edit_target(db,conversation_id,identity)
            return {"id":identity,"text":text,"scheduled_at":next_at,"status":saved["status"],"recurrence":recurrence,
                    "timezone":zone_name,"rule_expression":expression,"execution_mode":saved["execution_mode"],"edit_token":edit_token(saved)}

    @router.post("/{identity}/pause")
    def pause_series(conversation_id:str,identity:str,service=Depends(svc),user=Depends(authenticated)):
        with service.db.transaction():
            service.require("conversations",conversation_id,user)
            row=service.db.one("SELECT * FROM conversation_schedule_series WHERE id=? AND conversation_id=?",(identity,conversation_id))
            if not row:
                raise AppError(404,"NOT_FOUND","周期计划不存在")
            if row["status"] not in {"scheduled","paused"}:
                raise AppError(409,"SCHEDULE_INACTIVE","该周期计划已结束")
            if row["status"]=="scheduled":
                service.db.execute("UPDATE conversation_schedule_series SET status='paused',error=NULL WHERE id=?",(identity,))
            # Already committed occurrences are deliberately left alone.
            return {"id":identity,"status":"paused","scheduled_at":row["next_at"]}

    @router.post("/{identity}/resume")
    def resume_series(conversation_id:str,identity:str,body:ScheduleResume,service=Depends(svc),user=Depends(authenticated)):
        db=service.db
        service.require("conversations",conversation_id,user)
        snapshot=db.one("SELECT * FROM conversation_schedule_series WHERE id=? AND conversation_id=?",(identity,conversation_id))
        if not snapshot:
            raise AppError(404,"NOT_FOUND","周期计划不存在")
        if snapshot["status"]=="scheduled":
            return {"id":identity,"status":"scheduled","scheduled_at":snapshot["next_at"]}
        if snapshot["status"]!="paused":
            raise AppError(409,"SCHEDULE_INACTIVE","该周期计划已结束")
        _available(service,conversation_id,snapshot["execution_mode"])
        previous=_previous(db,identity)
        if previous and not _terminal(previous):
            raise AppError(409,"SCHEDULE_OCCURRENCE_ACTIVE","上一次任务尚未结束，请先检查会话")
        failed=_failed(previous) and previous["id"]!=snapshot["acknowledged_occurrence_id"]
        if failed and not body.acknowledge_failure:
            raise AppError(409,"SCHEDULE_FAILURE_ACK_REQUIRED","请确认上次失败可能产生的影响；恢复仅执行未来周期，不会重试上次任务")
        reference=datetime.now(timezone.utc)
        next_at=snapshot["next_at"]
        if datetime.fromisoformat(next_at)<=reference:
            if snapshot["recurrence"] in {"cron","rrule"}:
                from .official_schedule_rules import resolve_resume_rule
                next_at,_=resolve_resume_rule(service.settings,snapshot["recurrence"],snapshot["rule_expression"],snapshot["timezone"],next_at,reference)
            else:
                next_at,_,_=series_slots(snapshot,reference)
        with db.transaction():
            service.require("conversations",conversation_id,user)
            _available(service,conversation_id,snapshot["execution_mode"])
            current=db.one("SELECT * FROM conversation_schedule_series WHERE id=? AND conversation_id=?",(identity,conversation_id))
            if current!=snapshot or _previous(db,identity)!=previous:
                raise AppError(409,"SCHEDULE_CHANGED","计划或运行状态已变化，请刷新后重试")
            if next_at and datetime.fromisoformat(next_at)<=datetime.now(timezone.utc):
                raise AppError(409,"SCHEDULE_CHANGED","计算期间下次时间已到期，请刷新后重试")
            status="scheduled" if next_at else "exhausted"
            ack=previous["id"] if failed else snapshot["acknowledged_occurrence_id"]
            db.execute("UPDATE conversation_schedule_series SET status=?,next_at=?,error=NULL,acknowledged_occurrence_id=? WHERE id=?",
                       (status,next_at or snapshot["next_at"],ack,identity))
        return {"id":identity,"status":status,"scheduled_at":next_at or snapshot["next_at"]}

    @router.delete("/{identity}",status_code=204)
    def cancel_schedule(conversation_id:str,identity:str,service=Depends(svc),user=Depends(authenticated)):
        with service.db.transaction():
            service.require("conversations",conversation_id,user)
            series=service.db.one("SELECT status FROM conversation_schedule_series WHERE id=? AND conversation_id=?",(identity,conversation_id))
            if series:
                service.db.execute("UPDATE conversation_schedule_series SET status='cancelled' WHERE id=?",(identity,))
                # Cancel outstanding occurrences that have not been dispatched.
                service.db.execute("""UPDATE message_queue SET status='cancelled',revision=revision+1,updated_at=? WHERE status='pending' AND conversation_id=? AND id IN
                    (SELECT id FROM conversation_schedules WHERE series_id=?)""",(now(),conversation_id,identity))
                service.db.execute("UPDATE conversation_schedules SET status='cancelled' WHERE series_id=? AND status='scheduled'",(identity,))
                return
            row=service.db.one("SELECT status FROM conversation_schedules WHERE id=? AND conversation_id=?",(identity,conversation_id))
            if row is None:
                raise AppError(404,"NOT_FOUND","计划任务不存在")
            if row["status"]=="cancelled":
                return
            if row["status"]=="queued":
                changed=service.db.execute("UPDATE message_queue SET status='cancelled',revision=revision+1,updated_at=? WHERE id=? AND conversation_id=? AND status='pending'",(now(),identity,conversation_id))
                if not changed.rowcount:
                    raise AppError(409,"SCHEDULE_STARTED","任务已启动，请在会话里操作")
            elif row["status"]!="scheduled":
                raise AppError(409,"SCHEDULE_INACTIVE","该计划任务已结束")
            service.db.execute("UPDATE conversation_schedules SET status='cancelled' WHERE id=?",(identity,))
    root=APIRouter()
    root.include_router(router)

    @root.get("/api/v1/schedules")
    def global_schedules(response:Response,scope:Literal["active","history"]="active",
                         limit:int=Query(30,ge=1,le=50),before:str|None=Query(None,max_length=250),
                         service=Depends(svc),user=Depends(authenticated)):
        response.headers["Cache-Control"]="no-store"
        return schedule_catalog(service,user,scope,limit,before)

    return root
