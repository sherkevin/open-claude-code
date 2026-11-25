"""User-triggered runs share the timer's authoritative FIFO and permissions."""
from datetime import datetime,timedelta,timezone
import time
import pytest
from test_api import client,configured,conversation,login,PASSWORD,poll
from test_official_schedule_rules import upstream
from app.conversation_schedules import enqueue_due_schedules
from app.db import now


def stamp(seconds=7200):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def create(client, recurrence="once", **kwargs):
    cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/schedules"
    response=client.post(route,json={"text":"manual fixture","recurrence":recurrence,
        **({"scheduled_at":stamp()} if recurrence in {"once","daily","weekly"} else {}),**kwargs})
    assert response.status_code==201,response.text
    identity=response.json()["id"]
    row=next(x for x in client.get(route).json() if x["id"]==identity)
    return cid,route,identity,row


def request(client,route,identity,row,key="manual-fixture-request"):
    return client.post(f"{route}/{identity}/run-now",json={"request_id":key,"expected_token":row["edit_token"]})


def await_run(service,cid,oid):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        row=service.db.one("SELECT * FROM runs WHERE conversation_id=? AND client_request_id=?",(cid,oid))
        if row and row["status"] in {"succeeded","failed","cancelled","interrupted"}:
            assert row["status"]=="succeeded",row["status"]
            return row
        time.sleep(.05)
    raise AssertionError("manual run did not finish")


def test_one_shot_reuses_original_identity_and_receipt(client):
    service=client.app.state.service
    cid,route,identity,row=create(client)
    response=request(client,route,identity,row)
    assert response.status_code==202,response.text
    receipt=response.json()
    assert receipt["plan_id"]==receipt["occurrence_id"]==identity
    assert receipt["status"]=="pending"
    run=await_run(service,cid,identity)
    replay=request(client,route,identity,row)
    assert replay.status_code==202 and replay.json()["run_id"]==run["id"]
    assert replay.json()["status"]=="succeeded"
    assert request(client,route,identity,row,"new-request").status_code==409
    service.db.execute("UPDATE conversation_schedules SET scheduled_at=? WHERE id=?",(stamp(-1),identity))
    enqueue_due_schedules(service)
    assert service.db.one("SELECT count(*) n FROM runs WHERE conversation_id=? AND client_request_id=?",(cid,identity))["n"]==1
    history=client.get("/api/v1/schedules?scope=history").json()["items"]
    assert any(x["id"]==identity and x["manual_at"]==receipt["created_at"] and x["run_id"]==run["id"] for x in history)


@pytest.mark.parametrize("recurrence",["daily","weekly"])
def test_extra_occurrence_keeps_series_time_and_prevents_repeat(client,recurrence):
    service=client.app.state.service
    cid,route,identity,row=create(client,recurrence)
    old=service.db.one("SELECT * FROM conversation_schedule_series WHERE id=?",(identity,))
    response=request(client,route,identity,row)
    assert response.status_code==202,response.text
    oid=response.json()["occurrence_id"]
    assert oid!=identity
    run=await_run(service,cid,oid)
    assert request(client,route,identity,row).json()["run_id"]==run["id"]
    assert request(client,route,identity,row,"another-click").status_code==409
    assert service.db.one("SELECT * FROM conversation_schedule_series WHERE id=?",(identity,))==old
    assert service.db.one("SELECT count(*) n FROM conversation_schedules WHERE series_id=?",(identity,))["n"]==1
    assert service.db.one("SELECT text FROM messages WHERE id=?",(run["message_id"],))["text"]=="manual fixture"


def test_manual_request_key_bound_to_exact_plan_and_version(client):
    cid,route,identity,row=create(client)
    service=client.app.state.service
    assert request(client,route,identity,row).status_code==202
    assert client.post(f"{route}/{identity}/run-now",json={"request_id":"manual-fixture-request","expected_token":"0"*64}).status_code==409
    second=client.post(route,json={"text":"other plan","scheduled_at":stamp()}).json()["id"]
    row2=next(x for x in client.get(route).json() if x["id"]==second)
    assert request(client,route,second,row2).status_code==409
    assert service.db.one("SELECT count(*) n FROM conversation_schedule_run_requests WHERE conversation_id=?",(cid,))["n"]==1


def test_pause_failure_and_near_due_guards(client):
    cid,route,identity,row=create(client,"daily")
    service=client.app.state.service
    assert client.post(f"{route}/{identity}/pause").status_code==200
    paused=next(x for x in client.get(route).json() if x["id"]==identity)
    assert request(client,route,identity,paused).status_code==409
    assert client.post(f"{route}/{identity}/resume",json={}).status_code==200
    service.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",(stamp(35),identity))
    near=next(x for x in client.get(route).json() if x["id"]==identity)
    assert request(client,route,identity,near).status_code==409
    service.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",(stamp(),identity))
    service.db.execute("""INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id)
        VALUES(?,?,?,?,'failed',?,?)""",("schedule-manual-old-failed",cid,"old fixture",stamp(-100),stamp(-100),identity))
    current=next(x for x in client.get(route).json() if x["id"]==identity)
    assert request(client,route,identity,current).status_code==409
    assert service.db.one("SELECT count(*) n FROM conversation_schedule_run_requests WHERE conversation_id=?",(cid,))["n"]==0


def test_foreign_owner_and_archived_conversation_denied(client):
    cid,route,identity,row=create(client)
    service=client.app.state.service
    other=conversation(client)["id"]
    assert request(client,f"/api/v1/conversations/{other}/schedules",identity,row).status_code==404
    service.db.execute("UPDATE conversations SET archived=1 WHERE id=?",(cid,))
    assert request(client,route,identity,row).status_code==409
    assert client.post("/api/v1/users",json={"username":"run-now-member","display_name":"Member","password":PASSWORD,"role":"member"}).status_code==201
    login(client,"run-now-member")
    assert request(client,route,identity,row).status_code==404
    assert service.db.one("SELECT count(*) n FROM conversation_schedule_run_requests WHERE conversation_id=?",(cid,))["n"]==0


def test_full_queue_leaves_no_manual_receipt_or_occurrence(client):
    cid,route,identity,row=create(client,"daily")
    service=client.app.state.service
    with service.db.transaction():
        # The first draft has an error, so the durable FIFO cannot start.
        for i in range(20):
            service.db.execute("""INSERT INTO message_queue(id,conversation_id,payload,created_at,updated_at,error)
                VALUES(?,?,?,?,?,?)""",(f"draft-{i}",cid,"{}",now(),now(),"fixture blocked"))
    assert request(client,route,identity,row).status_code==409
    assert service.db.one("SELECT count(*) n FROM conversation_schedules WHERE series_id=?",(identity,))["n"]==0
    assert service.db.one("SELECT count(*) n FROM conversation_schedule_run_requests WHERE conversation_id=?",(cid,))["n"]==0


def test_official_rule_plan_retains_exact_rule_and_next_slot(client,upstream):
    cid,route,identity,row=create(client,"cron",rule_expression="0 9 * * 1-5",timezone="Asia/Shanghai")
    old=upstream.db.one("SELECT * FROM conversation_schedule_series WHERE id=?",(identity,))
    result=request(client,route,identity,row)
    assert result.status_code==202,result.text
    await_run(upstream,cid,result.json()["occurrence_id"])
    assert upstream.db.one("SELECT * FROM conversation_schedule_series WHERE id=?",(identity,))==old
