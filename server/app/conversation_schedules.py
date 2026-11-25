"""Persistent scheduled conversation runs using the existing durable FIFO."""
from datetime import datetime, timedelta, timezone, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
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
            previous=db.one("""SELECT s.status AS schedule_status, q.status AS queue_status,
                q.error AS queue_error,r.status AS run_status FROM conversation_schedules s
                LEFT JOIN message_queue q ON q.id=s.id AND q.conversation_id=s.conversation_id
                LEFT JOIN runs r ON r.id=q.run_id WHERE s.series_id=?
                ORDER BY s.created_at DESC,s.id DESC LIMIT 1""",(series["id"],))
            if previous and (previous["queue_error"] or any(previous[key] in {"failed","cancelled","interrupted"} for key in ("schedule_status","queue_status","run_status"))):
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
        return series+occurrences

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
    return router
