"""Owner-scoped global scheduling catalog: same durable rows and actions."""
import time
from datetime import datetime,timedelta,timezone
from app.conversation_schedules import enqueue_due_schedules
from test_api import client,configured,conversation,login,PASSWORD


def stamp(seconds=7200):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat()


def create(client,cid,text,recurrence="once",seconds=7200):
    route=f"/api/v1/conversations/{cid}/schedules"
    response=client.post(route,json={"text":text,"recurrence":recurrence,"scheduled_at":stamp(seconds)})
    assert response.status_code==201,response.text
    return response.json()["id"]


def catalog(client,scope="active",**params):
    result=client.get("/api/v1/schedules",params={"scope":scope,**params})
    assert result.status_code==200,result.text
    assert result.headers["Cache-Control"]=="no-store"
    return result.json()


def test_global_catalog_all_own_conversations_not_other_users(client):
    service=client.app.state.service
    a,b=conversation(client)["id"],conversation(client)["id"]
    service.db.execute("UPDATE conversations SET title=? WHERE id=?",("own conversation A",a))
    aid=create(client,a,"own future A")
    bid=create(client,b,"own recurring B","daily")
    assert client.post("/api/v1/users",json={"username":"catalog-member","display_name":"member","password":PASSWORD,"role":"member"}).status_code==201
    login(client,"catalog-member")
    c=conversation(client)["id"]
    cid=create(client,c,"other private task")
    assert {r["id"] for r in catalog(client)["items"]}=={cid}
    assert client.post(f"/api/v1/conversations/{b}/schedules/{bid}/pause").status_code==404
    login(client)
    rows=catalog(client)["items"]
    assert {r["id"] for r in rows}=={aid,bid}
    assert next(r for r in rows if r["id"]==aid)["conversation_title"]=="own conversation A"
    assert next(r for r in rows if r["id"]==bid)["kind"]=="series"
    assert "other private task" not in str(rows)
    assert catalog(client,"history")["items"]==[]


def test_global_status_changes_use_existing_conversation_actions(client):
    service=client.app.state.service
    cid=conversation(client)["id"]
    sid=create(client,cid,"periodic text","weekly")
    oid=create(client,cid,"single text")
    base=f"/api/v1/conversations/{cid}/schedules"
    assert client.post(f"{base}/{sid}/pause").status_code==200
    assert next(r for r in catalog(client)["items"] if r["id"]==sid)["status"]=="paused"
    assert client.post(f"{base}/{sid}/resume",json={}).status_code==200
    assert next(r for r in catalog(client)["items"] if r["id"]==sid)["status"]=="scheduled"
    assert client.delete(f"{base}/{oid}").status_code==204
    assert {r["id"] for r in catalog(client)["items"]}=={sid}
    history=catalog(client,"history")["items"]
    assert history[0]["id"]==oid and history[0]["status"]=="cancelled"
    assert service.db.all("SELECT id FROM runs WHERE conversation_id=?",(cid,))==[]


def test_catalog_run_link_points_to_real_execution(client):
    service=client.app.state.service
    cid=conversation(client)["id"]
    sid=create(client,cid,"run link fixture","daily")
    service.db.execute("UPDATE conversation_schedule_series SET next_at=? WHERE id=?",(stamp(-1),sid))
    deadline=time.monotonic()+12
    row=None
    while time.monotonic()<deadline:
        entries=catalog(client,"history")["items"]
        row=next((r for r in entries if r["series_id"]==sid and r["status"]=="succeeded"),None)
        if row:break
        time.sleep(.1)
    assert row and row["kind"]=="occurrence"
    run=service.require("runs",row["run_id"])
    assert row["message_id"]==run["message_id"]
    assert run["conversation_id"]==cid
    assert any(r["id"]==sid and r["kind"]=="series" for r in catalog(client)["items"])


def test_keyset_pagination_is_stable_and_bounded(client):
    cid=conversation(client)["id"]
    ids=[create(client,cid,f"fixture {i}") for i in range(4)]
    first=catalog(client,limit=2)
    assert len(first["items"])==2 and first["next_cursor"]
    second=catalog(client,limit=2,before=first["next_cursor"])
    assert len(second["items"])==2 and second["next_cursor"] is None
    assert set(r["id"] for r in first["items"]+second["items"])==set(ids)
    assert not {r["id"] for r in first["items"]}&{r["id"] for r in second["items"]}
    assert client.get("/api/v1/schedules?limit=51").status_code==422
    assert client.get("/api/v1/schedules?scope=all").status_code==422
    assert client.get("/api/v1/schedules",params={"before":"invalid"}).status_code==422


def test_archived_conversation_visible_and_unauthenticated_denied(client):
    service=client.app.state.service
    cid=conversation(client)["id"]
    sid=create(client,cid,"archived task","daily")
    service.db.execute("UPDATE conversations SET archived=1 WHERE id=?",(cid,))
    row=next(r for r in catalog(client)["items"] if r["id"]==sid)
    assert row["conversation_archived"]
    assert client.post("/api/v1/auth/logout").status_code in (200,204)
    assert client.get("/api/v1/schedules").status_code==401
