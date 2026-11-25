from datetime import datetime,timedelta,timezone
import pytest
from app.conversation_schedules import enqueue_due_schedules
from app.db import now
from test_api import client,configured,conversation,login,PASSWORD
from test_official_schedule_rules import upstream

def stamp(delta=3600):
    return (datetime.now(timezone.utc)+timedelta(seconds=delta)).isoformat()

def create(client, **kwargs):
    cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/schedules"
    result=client.post(route,json={"text":"isolated lifecycle","recurrence":"daily","scheduled_at":stamp(),**kwargs})
    assert result.status_code==201,result.text
    return cid,route,result.json()["id"]

def state(service, identity):
    return service.db.one("SELECT * FROM conversation_schedule_series WHERE id=?",(identity,))

def occurrences(service,identity):
    return service.db.all("SELECT * FROM conversation_schedules WHERE series_id=?",(identity,))

def test_pause_resume_future_only(client):
    cid,route,identity=create(client)
    service=client.app.state.service
    endpoint=f"{route}/{identity}"
    assert client.post(endpoint+"/pause").json()["status"]=="paused"
    assert client.post(endpoint+"/pause").json()["status"]=="paused"
    service.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",(stamp(-86400*3),identity))
    enqueue_due_schedules(service)
    assert occurrences(service,identity)==[]
    resumed=client.post(endpoint+"/resume",json={})
    assert resumed.status_code==200,resumed.text
    assert datetime.fromisoformat(resumed.json()["scheduled_at"])>datetime.now(timezone.utc)
    assert client.post(endpoint+"/resume",json={}).json()==resumed.json()
    enqueue_due_schedules(service)
    assert occurrences(service,identity)==[]
    assert client.delete(endpoint).status_code==204
    assert client.post(endpoint+"/resume",json={}).status_code==409

def test_failure_acknowledgement_does_not_replay_and_new_failure_pauses(client):
    cid,route,identity=create(client)
    service=client.app.state.service
    endpoint=f"{route}/{identity}"
    failed="schedule-old-failed"
    with service.db.transaction():
        service.db.execute("INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id) VALUES(?,?,?,?,'failed',?,?)",
                           (failed,cid,"old fixture",stamp(-100),stamp(-100),identity))
    enqueue_due_schedules(service)
    assert state(service,identity)["status"]=="paused"
    denied=client.post(endpoint+"/resume",json={})
    assert denied.status_code==409,denied.text
    resumed=client.post(endpoint+"/resume",json={"acknowledge_failure":True})
    assert resumed.status_code==200,resumed.text
    enqueue_due_schedules(service)
    assert state(service,identity)["status"]=="scheduled"
    assert state(service,identity)["acknowledged_occurrence_id"]==failed
    assert [x["id"] for x in occurrences(service,identity)]==[failed]
    with service.db.transaction():
        service.db.execute("INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id) VALUES(?,?,?,?,'failed',?,?)",
                           ("schedule-new-failed",cid,"new fixture",stamp(-1),now(),identity))
    enqueue_due_schedules(service)
    assert state(service,identity)["status"]=="paused"

def test_pause_does_not_cancel_committed_occurrence_and_resume_refuses_active(client):
    cid,route,identity=create(client)
    service=client.app.state.service
    with service.db.transaction():
        service.db.execute("INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id) VALUES(?,?,?,?,'queued',?,?)",
                           ("schedule-inflight-fixture",cid,"fixture",stamp(-1),now(),identity))
    assert client.post(f"{route}/{identity}/pause").status_code==200
    assert occurrences(service,identity)[0]["status"]=="queued"
    assert client.post(f"{route}/{identity}/resume",json={"acknowledge_failure":True}).status_code==409
    assert state(service,identity)["status"]=="paused"

def test_lifecycle_ownership_and_nonseries_rejected(client):
    cid,route,identity=create(client)
    other=conversation(client)["id"]
    for action in ("pause","resume"):
        assert client.post(f"/api/v1/conversations/{other}/schedules/{identity}/{action}",json={}).status_code==404
    assert client.post("/api/v1/users",json={"username":"schedule-member","display_name":"member","password":PASSWORD,"role":"member"}).status_code==201
    login(client,"schedule-member")
    for action in ("pause","resume"):
        assert client.post(f"{route}/{identity}/{action}",json={}).status_code==404
    assert state(client.app.state.service,identity)["status"]=="scheduled"

@pytest.mark.parametrize("kind",["cron","rrule"])
def test_real_advanced_future_only_and_exhaustion(client,upstream,kind):
    service=upstream
    cid=conversation(client)["id"]; route=f"/api/v1/conversations/{cid}/schedules"
    future=(datetime.now(timezone.utc)+timedelta(hours=1)).replace(microsecond=0)
    expression="0 9 * * *" if kind=="cron" else "DTSTART:"+future.strftime("%Y%m%dT%H%M%SZ")+"\nRRULE:FREQ=DAILY;COUNT=1"
    result=client.post(route,json={"text":"advanced fixture","recurrence":kind,"rule_expression":expression})
    assert result.status_code==201,result.text
    identity=result.json()["id"]
    assert client.post(f"{route}/{identity}/pause").status_code==200
    # Use a coherent past rule and slot, simulating time passing while paused.
    past=(datetime.now(timezone.utc)-timedelta(days=2)).replace(microsecond=0)
    if kind=="cron":
        past=past.replace(hour=9,minute=0,second=0)
    else:
        expression="DTSTART:"+past.strftime("%Y%m%dT%H%M%SZ")+"\nRRULE:FREQ=DAILY;COUNT=1"
    service.db.execute("UPDATE conversation_schedule_series SET next_at=?,rule_expression=? WHERE id=?",(past.isoformat(),expression,identity))
    resumed=client.post(f"{route}/{identity}/resume",json={})
    assert resumed.status_code==200,resumed.text
    assert resumed.json()["status"]==("scheduled" if kind=="cron" else "exhausted")
    if kind=="cron":
        assert datetime.fromisoformat(resumed.json()["scheduled_at"])>datetime.now(timezone.utc)
    enqueue_due_schedules(service)
    assert occurrences(service,identity)==[]


def test_resume_rule_capacity_does_not_consume_scheduler_slot(client):
    from app.official_schedule_rules import _CREATE_SLOT,resolve_resume_rule
    from app.security import AppError
    assert _CREATE_SLOT.acquire(blocking=False)
    try:
        with pytest.raises(AppError) as exc:
            resolve_resume_rule(client.app.state.service.settings,"cron","0 9 * * *","UTC",stamp(-1),datetime.now(timezone.utc))
        assert exc.value.status==429 and exc.value.code=="SCHEDULE_RULE_BUSY"
    finally:
        _CREATE_SLOT.release()
