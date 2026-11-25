"""Exercise actual pinned upstream timing code through the host and API."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.official_schedule_rules import create_rule, resolve_rule
from app.security import AppError
from app.conversation_schedules import enqueue_due_schedules
from test_api import client, configured, conversation


@pytest.fixture
def upstream(client):
    service = client.app.state.service
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("Pinned upstream toolchain required")
    service.settings.harness_node_path = node
    service.settings.harness_source_dir = source
    return service


def test_direct_rule_engine_receipts(upstream):
    stamp = datetime.fromisoformat("2026-11-20T00:00:00+00:00")
    next_at, expression, zone = create_rule(upstream.settings, "cron", "0 9 * * 1-5", "Asia/Shanghai", stamp)
    assert next_at == "2026-11-20T01:00:00+00:00"
    assert resolve_rule(upstream.settings, "cron", expression, zone, next_at,
                        datetime.fromisoformat("2026-11-25T02:00:00+00:00")) == (
        "2026-11-26T01:00:00+00:00", "2026-11-25T01:00:00+00:00")
    with pytest.raises(AppError) as e:
        create_rule(upstream.settings, "cron", "0 0 L * *", "UTC", stamp)
    assert e.value.code == "SCHEDULE_RULE_INVALID"


def test_create_and_exhaust_real_rrule_via_durable_queue(client, upstream):
    cid = conversation(client)["id"]
    route = f"/api/v1/conversations/{cid}/schedules"
    future = (datetime.now(timezone.utc)+timedelta(seconds=12)).replace(microsecond=0)
    rule = "DTSTART:"+future.strftime("%Y%m%dT%H%M%SZ")+"\nRRULE:FREQ=DAILY;COUNT=1"
    result = client.post(route, json={"text":"periodic fixture","recurrence":"rrule","rule_expression":rule,"timezone":"UTC"})
    assert result.status_code == 201, result.text
    identity = result.json()["id"]
    assert datetime.fromisoformat(result.json()["scheduled_at"]) == future
    deadline = time.monotonic()+35
    while time.monotonic()<deadline:
        row = upstream.db.one("SELECT status FROM conversation_schedule_series WHERE id=?", (identity,))
        runs = upstream.db.all("SELECT r.status,r.client_request_id FROM runs r JOIN conversation_schedules s ON s.id=r.client_request_id WHERE s.series_id=?", (identity,))
        if row["status"] == "exhausted" and len(runs)==1 and runs[0]["status"]=="succeeded":
            break
        time.sleep(.1)
    assert row["status"]=="exhausted" and len(runs)==1 and runs[0]["status"]=="succeeded",(row,runs)
    enqueue_due_schedules(upstream)
    assert len(upstream.db.all("SELECT id FROM conversation_schedules WHERE series_id=?", (identity,)))==1
    assert any(item["id"]==identity and item["status"]=="exhausted" for item in client.get(route).json())


def test_invalid_or_missing_engine_is_fail_closed(client):
    service = client.app.state.service
    service.settings.harness_source_dir = ""
    service.settings.harness_node_path = ""
    cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/schedules"
    assert client.post(route,json={"text":"invalid","recurrence":"cron","rule_expression":"0 9 * * *"}).status_code==503
    assert client.post(route,json={"text":"invalid","recurrence":"daily"}).status_code==422
    assert client.get(route).json()==[]


def test_rule_engine_failure_pauses_without_dispatch(client,upstream):
    cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/schedules"
    result=client.post(route,json={"text":"periodic fixture","recurrence":"cron","rule_expression":"0 9 * * *"})
    assert result.status_code==201,result.text
    identity=result.json()["id"]
    upstream.settings.harness_source_dir=""
    with upstream.db.transaction():
        upstream.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),identity))
    enqueue_due_schedules(upstream)
    assert upstream.db.one("SELECT status FROM conversation_schedule_series WHERE id=?",(identity,))["status"]=="paused"
    assert upstream.db.all("SELECT id FROM conversation_schedules WHERE series_id=?",(identity,))==[]

def test_validation_capacity_does_not_consume_scheduler_slot(client):
    from app.official_schedule_rules import _CREATE_SLOT
    assert _CREATE_SLOT.acquire(blocking=False)
    try:
        with pytest.raises(AppError) as e:
            create_rule(client.app.state.service.settings,"cron","0 9 * * *","UTC",datetime.now(timezone.utc))
        assert e.value.code=="SCHEDULE_RULE_BUSY" and e.value.status==429
    finally:
        _CREATE_SLOT.release()
