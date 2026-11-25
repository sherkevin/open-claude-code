"""Actual host bridge, approval actor and pinned calendar; no supplier requests."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

import pytest

from app.harness_schedule import CANONICAL, NAME
from app.security import AppError
from test_harness_tool_execution import system, payload


@pytest.fixture
def configured_schedule(system):
    runtime, actor, owner, assistant, kb = system
    node, source = os.getenv("HARNESS_TEST_NODE"), os.getenv("HARNESS_TEST_SOURCE")
    if not node or not source:
        pytest.skip("Pinned upstream toolchain required")
    runtime.service.settings.harness_node_path=node
    runtime.service.settings.harness_source_dir=source
    runtime.service.settings.approval_seconds=10
    runtime.config=runtime.service.runtime_config(runtime.conversation_id)
    return system


def arguments():
    first=(datetime.now(timezone.utc)+timedelta(days=1)).replace(microsecond=0)
    return {"title":"每日报告","prompt":"仅用测试资料生成报告",
            "rrule":"DTSTART:"+first.strftime("%Y%m%dT%H%M%SZ")+"\nRRULE:FREQ=DAILY;COUNT=3",
            "target":"this_session"}


def rows(runtime):
    return runtime.service.db.all("SELECT * FROM conversation_schedule_series")


async def decide(runtime, actor, owner, decision, *, source="user"):
    for _ in range(300):
        candidates=actor.service.db.all("SELECT * FROM approvals WHERE run_id=? AND status='pending'",(actor.current_run_id,))
        if candidates:
            row=candidates[0]
            break
        await asyncio.sleep(.05)
    else:
        raise AssertionError("Expected a real approval after pinned calendar calculation")
    assert rows(runtime)==[]
    view=json.loads(row["arguments"])
    assert view["prompt"]==arguments()["prompt"]
    assert view["target"]=="this_session" and view["timezone"]=="UTC"
    assert datetime.fromisoformat(view["first_occurrence_utc"])>datetime.now(timezone.utc)
    runtime.service.db.execute("UPDATE approvals SET decided_by=?,decision_source=? WHERE id=?",
                               (owner["id"],source,row["id"]))
    actor.manager.approvals[row["id"]].set_result(decision)
    return row


@pytest.mark.parametrize("decision",["allow","deny"])
def test_natural_language_schedule_requires_owner_approval(configured_schedule,decision):
    runtime,actor,owner,*_=configured_schedule
    assert NAME in {item["name"] for item in runtime.bridge.definitions()}

    async def scenario():
        req=payload(runtime,NAME,arguments())
        task=asyncio.create_task(runtime.bridge.invoke(req))
        row=await decide(runtime,actor,owner,decision)
        result=await asyncio.wait_for(task,5)
        assert bool(result.get("is_error"))==(decision=="deny"),result
        if decision=="allow":
            receipt=json.loads(result["content"][0]["text"])
            assert len(rows(runtime))==1
            assert rows(runtime)[0]["conversation_id"]==runtime.conversation_id
            assert rows(runtime)[0]["id"]==receipt["id"]
            assert rows(runtime)[0]["next_at"]==json.loads(row["arguments"])["first_occurrence_utc"]
            assert rows(runtime)[0]["execution_mode"]=="agent"
            assert runtime.service.db.one("SELECT * FROM conversation_schedule_create_requests")["approval_id"]==row["id"]
            again=await runtime.bridge.invoke(payload(runtime,NAME,arguments(),call_id="second-call"))
            assert again["is_error"] and len(rows(runtime))==1
        else:
            assert rows(runtime)==[]
    asyncio.run(scenario())


@pytest.mark.parametrize("change",["target","zone","scheduled_run","model_approval"])
def test_reject_invalid_context_or_non_owner_receipt(configured_schedule,change):
    runtime,actor,owner,*_=configured_schedule
    req=arguments()
    if change=="target":
        req["target"]="new"
    elif change=="zone":
        req["rrule"]=req["rrule"].replace("Z\n","\n")
    elif change=="scheduled_run":
        meta=json.loads(runtime.current_run["execution_meta"])
        meta["schedule_id"]="fixture"
        runtime.service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?",(json.dumps(meta),actor.current_run_id))

    async def scenario():
        task=asyncio.create_task(runtime.bridge.invoke(payload(runtime,NAME,req)))
        if change=="model_approval":
            await decide(runtime,actor,owner,"allow",source="model")
        result=await asyncio.wait_for(task,15)
        assert result["is_error"],result
        assert rows(runtime)==[]
        if change!="model_approval":
            assert runtime.service.db.all("SELECT * FROM approvals")==[]
    asyncio.run(scenario())


def test_dual_policy_fails_closed_before_private_preview(configured_schedule):
    runtime,actor,owner,assistant,_=configured_schedule
    runtime.service.save_high_risk_approval_policy(assistant["id"], {
        "enabled":True,"allowed_tools":[],"admin_allowlist":[]},owner)
    result=asyncio.run(runtime.bridge.invoke(payload(runtime,NAME,arguments())))
    assert result["is_error"] and rows(runtime)==[]
    assert not runtime.service.db.all("SELECT * FROM approvals")
