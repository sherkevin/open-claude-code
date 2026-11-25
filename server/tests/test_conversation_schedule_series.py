import time
from datetime import datetime,timedelta,timezone
import pytest
from app.conversation_schedules import next_slot,series_slots,enqueue_due_schedules
from app.db import now
from test_api import client, configured, conversation


def future():
    return (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()


def past():
    return (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()


@pytest.mark.parametrize("recurrence",["daily","weekly"])
def test_recurring_series_persists_next_slot_and_executes_once_per_occurrence(client, recurrence):
    service=client.app.state.service;cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/schedules"
    r=client.post(route,json={"text":"periodic check","scheduled_at":future(),"recurrence":recurrence,"timezone":"Asia/Shanghai"})
    assert r.status_code==201,r.text
    series=r.json()["id"]
    service.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",(past(),series))
    def await_runs(n):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            rows=service.db.all("SELECT r.status,r.client_request_id FROM runs r JOIN conversation_schedules s ON r.client_request_id=s.id WHERE s.series_id=?",(series,))
            if len(rows)==n and all(x["status"]=="succeeded" for x in rows):
                return rows
            time.sleep(.05)
        raise AssertionError(rows)
    first=await_runs(1)
    assert service.db.one("SELECT next_at FROM conversation_schedule_series WHERE id=?",(series,))["next_at"]>now()
    enqueue_due_schedules(service)
    assert len(service.db.all("SELECT id FROM conversation_schedules WHERE series_id=?",(series,)))==1
    service.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",(past(),series))
    second=await_runs(2)
    assert len({r["client_request_id"] for r in second})==2
    items=client.get(route).json()
    assert any(x["id"]==series and x["recurrence"]==recurrence and x["status"]=="scheduled" for x in items)
    assert client.delete(f"{route}/{series}").status_code==204
    enqueue_due_schedules(service)
    assert len(service.db.all("SELECT id FROM conversation_schedules WHERE series_id=?",(series,)))==2


def test_series_skips_overlaps_and_pauses_after_failed_occurrence(client):
    service=client.app.state.service;cid=conversation(client)["id"];route=f"/api/v1/conversations/{cid}/schedules"
    series=client.post(route,json={"text":"periodic check","scheduled_at":future(),"recurrence":"daily"}).json()["id"]
    with service.db.transaction():
        service.db.execute("INSERT INTO conversation_schedules(id,conversation_id,text,scheduled_at,status,created_at,series_id) VALUES(?,?,?,?,'queued',?,?)",
                           ("schedule-overlap-fixture",cid,"fixture",past(),now(),series))
        service.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",(past(),series))
    enqueue_due_schedules(service)
    state=service.db.one("SELECT * FROM conversation_schedule_series WHERE id=?",(series,))
    assert state["skipped_count"]==1 and state["next_at"]>now()
    assert len(service.db.all("SELECT id FROM conversation_schedules WHERE series_id=?",(series,)))==1
    service.db.execute("UPDATE conversation_schedules SET status='failed' WHERE id='schedule-overlap-fixture'")
    enqueue_due_schedules(service)
    assert service.db.one("SELECT status FROM conversation_schedule_series WHERE id=?",(series,))["status"]=="paused"
    assert client.delete(f"{route}/{series}").status_code==204


def test_wall_clock_daily_weekly_across_dst():
    s={"timezone":"America/New_York","recurrence":"daily","local_time":"09:00:00",
       "next_at":"2026-03-07T14:00:00+00:00"}
    assert next_slot(s,datetime.fromisoformat("2026-03-07T14:00:01+00:00"))[0]=="2026-03-08T13:00:00+00:00"
    s["recurrence"]="weekly"
    assert next_slot(s,datetime.fromisoformat("2026-03-07T14:00:01+00:00"))[0]=="2026-03-14T13:00:00+00:00"
    s["recurrence"]="daily";s["local_time"]="02:30:00";s["next_at"]="2026-03-07T07:30:00+00:00"
    assert next_slot(s,datetime.fromisoformat("2026-03-07T07:30:01+00:00"))[0]=="2026-03-09T06:30:00+00:00"


def test_invalid_timezone_and_legacy_text_default(client):
    cid=conversation(client)["id"];route=f"/api/v1/conversations/{cid}/schedules"
    assert client.post(route,json={"text":"invalid","scheduled_at":future(),"timezone":"invalid/no-zone","recurrence":"daily"}).status_code==422
    response=client.post(route,json={"text":"old client","scheduled_at":future()})
    assert response.status_code==201 and response.json()["execution_mode"]=="text"

def test_overdue_series_delivers_only_latest_due_slot():
    s={"timezone":"Asia/Shanghai","recurrence":"daily","local_time":"09:00:00",
       "next_at":"2026-11-20T01:00:00+00:00"}
    assert series_slots(s,datetime.fromisoformat("2026-11-25T02:00:00+00:00"))==(
        "2026-11-26T01:00:00+00:00","2026-11-25T01:00:00+00:00",5)
