"""Natural-language mutations must share API logic and exact owner approval."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import pytest
from app.conversation_schedules import ScheduleCreate,prepare_schedule,persist_schedule,edit_token
from app.db import now
from test_harness_tool_execution import system,payload,pending_approval
from test_harness_schedule_create import configured_schedule
from test_harness_schedule_read import seed


async def execute(runtime,actor,owner,args,decision="allow",source="user",change=None):
    task=asyncio.create_task(runtime.bridge.invoke(payload(runtime,"schedule_edit",args,call_id="edit-"+args["action"])))
    row=await pending_approval(actor)
    preview=json.loads(row["arguments"])
    assert preview["id"]==args["id"] and preview["expectedUpdatedAt"]==args["expectedUpdatedAt"]
    if change:change()
    runtime.service.db.execute("UPDATE approvals SET decided_by=?,decision_source=? WHERE id=?",
                               (owner["id"],source,row["id"]))
    actor.manager.approvals[row["id"]].set_result(decision)
    return await asyncio.wait_for(task,5),preview,row


def plan(runtime,identity):
    table="conversation_schedule_series" if identity.startswith("series-") else "conversation_schedules"
    return runtime.service.db.one("SELECT * FROM "+table+" WHERE id=?",(identity,))


@pytest.mark.parametrize("operation",["edit","pause","resume","cancel","cancel_once"])
def test_mutation_confirmed_and_persisted_once(system,operation):
    runtime,actor,owner,*_=system
    service=runtime.service
    identity=seed(runtime,owner,runtime.conversation_id,"original fixture",
                  "once" if operation=="cancel_once" else "daily")
    if operation=="resume":
        service.db.execute("UPDATE conversation_schedule_series SET status='paused' WHERE id=?",(identity,))
    before=plan(runtime,identity)
    args={"id":identity,"action":"delete" if operation.startswith("cancel") else "update","expectedUpdatedAt":edit_token(before)}
    if operation=="edit":args["prompt"]="changed future fixture"
    if operation=="pause":args["active"]=False
    if operation=="resume":args["active"]=True
    result,preview,approval=asyncio.run(execute(runtime,actor,owner,args))
    assert not result.get("is_error"),result
    after=plan(runtime,identity)
    assert preview["before"]["prompt"]=="original fixture"
    assert preview["after"]["status"]==after["status"]
    if operation=="edit":
        assert after["text"]=="changed future fixture"
        assert after["next_at"]==before["next_at"]
    elif operation=="resume":assert after["status"]=="scheduled"
    elif operation=="pause":assert after["status"]=="paused"
    else:assert after["status"]=="cancelled"
    receipt=service.db.one("SELECT * FROM conversation_schedule_edit_requests")
    assert receipt["approval_id"]==approval["id"] and receipt["plan_id"]==identity
    duplicate=asyncio.run(runtime.bridge.invoke(payload(runtime,"schedule_edit",args,call_id="another-edit")))
    assert duplicate.get("is_error")
    assert service.db.one("SELECT count(*) n FROM conversation_schedule_edit_requests")["n"]==1
    assert service.db.all("SELECT * FROM message_queue")==[]


@pytest.mark.parametrize("failure",["deny","model","changed"])
def test_unapproved_or_stale_mutation_not_committed(system,failure):
    runtime,actor,owner,*_=system
    service=runtime.service
    identity=seed(runtime,owner,runtime.conversation_id,"original fixture","daily")
    before=plan(runtime,identity)
    args={"id":identity,"action":"update","expectedUpdatedAt":edit_token(before),"prompt":"unauthorized change"}
    def change():
        if failure=="changed":
            service.db.execute("UPDATE conversation_schedule_series SET text='concurrent user change' WHERE id=?",(identity,))
    result,_,_=asyncio.run(execute(runtime,actor,owner,args,decision="deny" if failure=="deny" else "allow",
                                  source="model" if failure=="model" else "user",change=change))
    assert result.get("is_error"),result
    assert plan(runtime,identity)["text"]==("concurrent user change" if failure=="changed" else before["text"])
    assert not service.db.all("SELECT * FROM conversation_schedule_edit_requests")


def test_read_version_cross_session_invalid_target_and_active_refused(system):
    runtime,actor,owner,assistant,_=system
    service=runtime.service
    identity=seed(runtime,owner,runtime.conversation_id,"local fixture","daily")
    other=service.create_conversation({"assistant_id":assistant["id"],"title":"other"},owner)["id"]
    foreign=seed(runtime,owner,other,"other private plan","daily")
    viewed=asyncio.run(runtime.bridge.invoke(payload(runtime,"schedule_view",{"id":identity},call_id="read-for-edit")))
    token=json.loads(viewed["content"][0]["text"])["item"]["expectedUpdatedAt"]
    assert token==edit_token(plan(runtime,identity))
    base={"id":identity,"action":"update","expectedUpdatedAt":token,"active":False}
    for i,args in enumerate((
        {**base,"id":foreign,"expectedUpdatedAt":edit_token(plan(runtime,foreign))},
        {**base,"target":"new"},
        {**base,"expectedUpdatedAt":"0"*64},
        {**base,"prompt":"mixed request"},
    )):
        response=asyncio.run(runtime.bridge.invoke(payload(runtime,"schedule_edit",args,call_id=f"invalid-{i}")))
        assert response.get("is_error")
    service.db.execute("""INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id)
        VALUES(?,?,?,?,'queued',?,?)""",("schedule-active-fixture",runtime.conversation_id,"active",now(),now(),identity))
    blocked=asyncio.run(runtime.bridge.invoke(payload(runtime,"schedule_edit",base,call_id="active-block")))
    assert blocked.get("is_error")
    assert not service.db.all("SELECT * FROM approvals")


def test_rrule_retiming_uses_official_calendar_and_preview(configured_schedule):
    runtime,actor,owner,*_=configured_schedule
    service=runtime.service
    first=(datetime.now(timezone.utc)+timedelta(days=2)).replace(microsecond=0)
    original="DTSTART:"+first.strftime("%Y%m%dT%H%M%SZ")+"\nRRULE:FREQ=DAILY;COUNT=3"
    body=ScheduleCreate(text="original fixture",recurrence="rrule",rule_expression=original,timezone="UTC")
    prepared=prepare_schedule(service,owner,runtime.conversation_id,body)
    with service.db.transaction():
        identity=persist_schedule(service,owner,runtime.conversation_id,body,prepared)["id"]
    before=plan(runtime,identity)
    replacement="DTSTART:"+(first+timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")+"\nRRULE:FREQ=DAILY;COUNT=2"
    args={"id":identity,"action":"update","expectedUpdatedAt":edit_token(before),"rrule":replacement}
    result,preview,_=asyncio.run(execute(runtime,actor,owner,args))
    assert not result.get("is_error"),result
    after=plan(runtime,identity)
    assert after["next_at"]==preview["after"]["scheduled_at"]
    assert after["rule_expression"]==preview["after"]["rule_expression"]
    assert datetime.fromisoformat(after["next_at"])==first+timedelta(hours=1)
    assert "COUNT=2" in after["rule_expression"]
