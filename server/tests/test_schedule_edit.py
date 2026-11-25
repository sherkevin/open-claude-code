"""Edit future plans without changing any submitted occurrence."""
from datetime import datetime,timedelta,timezone
import pytest
from test_api import client,configured,conversation,login,PASSWORD
from test_official_schedule_rules import upstream
from app.conversation_schedules import enqueue_due_schedules
from app.db import now


def stamp(seconds=7200):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def create(client,recurrence="once",**kwargs):
    cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/schedules"
    result=client.post(route,json={"text":"original task","recurrence":recurrence,
        **({"scheduled_at":stamp()} if recurrence in {"once","daily","weekly"} else {}),**kwargs})
    assert result.status_code==201,result.text
    identity=result.json()["id"]
    row=next(x for x in client.get(route).json() if x["id"]==identity)
    assert len(row["edit_token"])==64
    return cid,route,identity,row


def test_edit_once_and_reject_stale_token(client):
    cid,route,identity,row=create(client)
    endpoint=f"{route}/{identity}"
    body={"text":"new task","scheduled_at":stamp(10800),"expected_token":row["edit_token"]}
    result=client.patch(endpoint,json=body)
    assert result.status_code==200,result.text
    saved=result.json()
    assert saved["text"]=="new task" and saved["scheduled_at"]==body["scheduled_at"]
    assert saved["edit_token"]!=row["edit_token"]
    assert client.patch(endpoint,json=body).status_code==409
    assert client.get("/api/v1/schedules").json()["items"][0]["edit_token"]==saved["edit_token"]
    assert client.app.state.service.db.all("SELECT id FROM runs WHERE conversation_id=?",(cid,))==[]


def test_paused_series_edit_preserves_history_and_state(client):
    cid,route,identity,row=create(client,"daily",timezone="Asia/Shanghai")
    service=client.app.state.service
    with service.db.transaction():
        service.db.execute("""INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id)
        VALUES(?,?,?,?,'cancelled',?,?)""",("schedule-old-edit-fixture",cid,"historical instruction",stamp(-100),stamp(-100),identity))
    enqueue_due_schedules(service)
    row=next(x for x in client.get(route).json() if x["id"]==identity)
    assert row["status"]=="paused"
    result=client.patch(f"{route}/{identity}",json={"text":"future instruction","scheduled_at":stamp(10800),"expected_token":row["edit_token"]})
    assert result.status_code==200,result.text
    assert result.json()["status"]=="paused" and result.json()["timezone"]=="Asia/Shanghai"
    original=service.db.one("SELECT * FROM conversation_schedules WHERE id='schedule-old-edit-fixture'")
    assert original["text"]=="historical instruction" and original["status"]=="cancelled"
    assert client.post(f"{route}/{identity}/resume",json={}).status_code==409
    enqueue_due_schedules(service)
    assert service.db.one("SELECT count(*) n FROM conversation_schedules WHERE series_id=?",(identity,))["n"]==1


def test_active_and_cross_user_edits_denied(client):
    cid,route,identity,row=create(client,"weekly")
    service=client.app.state.service
    with service.db.transaction():
        service.db.execute("""INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id)
        VALUES(?,?,?,?,'queued',?,?)""",("schedule-active-edit-fixture",cid,"in-flight",stamp(-1),now(),identity))
    assert client.patch(f"{route}/{identity}",json={"text":"changed","expected_token":row["edit_token"]}).status_code==409
    other=conversation(client)["id"]
    assert client.patch(f"/api/v1/conversations/{other}/schedules/{identity}",json={"text":"changed","expected_token":row["edit_token"]}).status_code==404
    assert client.post("/api/v1/users",json={"username":"edit-member","display_name":"Member","password":PASSWORD,"role":"member"}).status_code==201
    login(client,"edit-member")
    assert client.patch(f"{route}/{identity}",json={"text":"changed","expected_token":row["edit_token"]}).status_code==404
    assert service.db.one("SELECT text FROM conversation_schedule_series WHERE id=?",(identity,))["text"]=="original task"


def test_due_one_shot_and_occurrence_not_editable(client):
    cid,route,identity,row=create(client)
    service=client.app.state.service
    service.db.execute("UPDATE conversation_schedules SET status='queued' WHERE id=?",(identity,))
    assert client.patch(f"{route}/{identity}",json={"text":"changed","expected_token":row["edit_token"]}).status_code==409
    assert "edit_token" not in next(x for x in client.get(route).json() if x["id"]==identity)


def test_advanced_text_only_preserves_rule_and_retiming_uses_official_engine(client,upstream):
    cid,route,identity,row=create(client,"cron",rule_expression="0 9 * * 1-5",timezone="Asia/Shanghai")
    first=client.patch(f"{route}/{identity}",json={"text":"edited text","expected_token":row["edit_token"]})
    assert first.status_code==200,first.text
    assert first.json()["scheduled_at"]==row["scheduled_at"]
    assert first.json()["rule_expression"]==row["rule_expression"]
    second=client.patch(f"{route}/{identity}",json={"text":"edited time","expected_token":first.json()["edit_token"],
        "rule_expression":"15 10 * * 1-5","timezone":"Asia/Shanghai"})
    assert second.status_code==200,second.text
    assert second.json()["rule_expression"]=="15 10 * * 1-5"
    assert datetime.fromisoformat(second.json()["scheduled_at"]).hour==2
    assert datetime.fromisoformat(second.json()["scheduled_at"]).minute==15
    invalid=client.patch(f"{route}/{identity}",json={"text":"invalid","expected_token":second.json()["edit_token"],"rule_expression":"0 0 L * *"})
    assert invalid.status_code==422,invalid.text


def test_invalid_edit_and_no_implicit_frequency_change(client):
    cid,route,identity,row=create(client)
    endpoint=f"{route}/{identity}"
    base={"text":"updated","expected_token":row["edit_token"]}
    assert client.patch(endpoint,json={**base,"scheduled_at":stamp(-60)}).status_code==422
    assert client.patch(endpoint,json={**base,"rule_expression":"0 9 * * *"}).status_code==422
    assert client.patch(endpoint,json={**base,"recurrence":"daily"}).status_code==422
    assert client.patch(endpoint,json={**base,"text":"  "}).status_code==422
    assert next(x for x in client.get(route).json() if x["id"]==identity)["text"]=="original task"
