"""Scope schedule_list/view to the current human-owned Harness session."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
import pytest
from app.conversation_schedules import ScheduleCreate,prepare_schedule,persist_schedule
from app.db import dump,now
from app.harness_tools import HarnessToolBridge
from app.security import AppError
from test_harness_tool_execution import system,payload


def seed(runtime,user,cid,text,recurrence="once"):
    service=runtime.service
    body=ScheduleCreate(text=text,scheduled_at=datetime.now(timezone.utc)+timedelta(days=1),
                        recurrence=recurrence,timezone="UTC")
    prepared=prepare_schedule(service,user,cid,body)
    with service.db.transaction():
        return persist_schedule(service,user,cid,body,prepared)["id"]


def result(runtime,name,args,call_id):
    response=asyncio.run(runtime.bridge.invoke(payload(runtime,name,args,call_id=call_id)))
    return response, json.loads(response["content"][0]["text"]) if not response.get("is_error") else None


def test_list_view_current_session_only_and_no_mutation(system):
    runtime,actor,owner,assistant,_=system
    service=runtime.service
    series=seed(runtime,owner,runtime.conversation_id,"fixture daily","daily")
    once=seed(runtime,owner,runtime.conversation_id,"fixture single")
    other=service.create_conversation({"assistant_id":assistant["id"],"title":"Other fixture"},owner)["id"]
    foreign=seed(runtime,owner,other,"private other conversation")
    assert {"schedule_list","schedule_view"} <= {item["name"] for item in runtime.bridge.definitions()}
    before=service.db.all("SELECT * FROM conversation_schedules ORDER BY id")
    series_before=service.db.all("SELECT * FROM conversation_schedule_series ORDER BY id")
    response,page=result(runtime,"schedule_list",{"limit":1},"list-page-one")
    assert not response.get("is_error") and page["scope"]=="this_session"
    assert len(page["items"])==1 and page["next_offset"]==1
    _,second=result(runtime,"schedule_list",{"offset":1,"limit":1},"list-page-two")
    assert {page["items"][0]["id"],second["items"][0]["id"]}=={series,once}
    assert second["next_offset"] is None
    _,detail=result(runtime,"schedule_view",{"id":series},"view-valid")
    assert detail["item"]["prompt"]=="fixture daily"
    assert detail["item"]["next_occurrence_at"]
    assert detail["item"]["latest_occurrence"] is None
    for i,id in enumerate((foreign,"series-missing")):
        denied,_=result(runtime,"schedule_view",{"id":id},f"view-denied-{i}")
        assert denied.get("is_error")
        assert denied["content"][0]["text"]=="当前会话中没有该计划"
    assert service.db.all("SELECT * FROM conversation_schedules ORDER BY id")==before
    assert service.db.all("SELECT * FROM conversation_schedule_series ORDER BY id")==series_before
    assert not service.db.all("SELECT * FROM approvals")
    assert not service.db.all("SELECT * FROM message_queue")
    assert not service.db.all("SELECT * FROM conversation_schedule_create_requests")


def test_active_filter_and_bounded_prompt(system):
    runtime,actor,owner,*_=system
    service=runtime.service
    active=seed(runtime,owner,runtime.conversation_id,"A"*6000)
    paused=seed(runtime,owner,runtime.conversation_id,"paused fixture","daily")
    service.db.execute("UPDATE conversation_schedule_series SET status='paused' WHERE id=?",(paused,))
    _,page=result(runtime,"schedule_list",{"active":True},"active-list")
    assert [r["id"] for r in page["items"]]==[active]
    assert len(page["items"][0]["prompt"])==240 and page["items"][0]["prompt_truncated"]
    _,page=result(runtime,"schedule_list",{"active":False},"inactive-list")
    assert [r["id"] for r in page["items"]]==[paused]
    assert page["items"][0]["next_occurrence_at"] is None
    _,detail=result(runtime,"schedule_view",{"id":active},"full-view")
    assert len(detail["item"]["prompt"])==4000 and detail["item"]["prompt_truncated"]


@pytest.mark.parametrize("name,args",[
    ("schedule_list",{"limit":21}),("schedule_list",{"offset":-1}),
    ("schedule_list",{"active":"false"}),("schedule_list",{"limit":True}),
    ("schedule_list",{"conversation_id":"other"}),("schedule_view",{"id":"../../other"}),
])
def test_invalid_query_rejected(system,name,args):
    runtime,*_=system
    response,_=result(runtime,name,args,"bad-request")
    assert response.get("is_error")


def test_scheduled_and_recovery_contexts_denied(system):
    runtime,actor,owner,*_=system
    run=runtime.service.require("runs",actor.current_run_id)
    metadata=json.loads(run.get("execution_meta") or "{}")
    metadata["schedule_id"]="schedule-fixture"
    runtime.service.db.execute("UPDATE runs SET execution_meta=? WHERE id=?",(dump(metadata),run["id"]))
    response,_=result(runtime,"schedule_list",{},"scheduled-list")
    assert response.get("is_error")
    runtime.config["recovery_only"]=True
    assert not HarnessToolBridge(runtime).definitions()
    assert not runtime.service.db.all("SELECT * FROM approvals")
