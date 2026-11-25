import io
import json
from datetime import datetime,timedelta,timezone

from PIL import Image
from app.conversation_feedback import sweep_feedback
from app.db import now
from test_api import client, configured, conversation, login, send, poll, PASSWORD


def png():
    out=io.BytesIO()
    Image.new("RGB",(8,8),"blue").save(out,format="PNG")
    return out.getvalue()


def test_feedback_user_explicit_upload_admin_only_scope_and_redacted_logs(client):
    service=client.app.state.service
    service.create_user({"username":"member","display_name":"Member","password":PASSWORD,"role":"member"})
    login(client,"member")
    c=conversation(client)["id"]
    r=send(client,c,"SENSITIVE_BODY_EXCLUDED","feedback-message-1")
    run=r["run_id"]
    assert poll(client,run)["status"]=="succeeded"
    service.db.event(run,"tool.completed",{"tool_name":"fixture_query","success":True,"error_code":"FIXTURE_OK",
        "arguments":{"token":"SECRET_PARAMETER"},"result":"PRIVATE_RESULT","error_message":"SENSITIVE_DETAIL"})
    data={"description":"发现表格显示问题","request_id":"feedback-unique-1","run_id":run,"message_id":r["message"]["id"]}
    route=f"/api/v1/conversations/{c}/feedback"
    first=client.post(route,data=data,files=[("screenshots",("screenshot.png",png(),"image/png"))])
    assert first.status_code==201,first.text
    fid=first.json()["id"]
    assert client.post(route,data=data).json()["id"]==fid
    assert client.get("/api/v1/admin/feedback").status_code==403
    assert client.get(f"/api/v1/admin/feedback/{fid}").status_code==403
    login(client)
    rows=client.get("/api/v1/admin/feedback").json()
    assert len(rows)==1 and rows[0]["image_count"]==1
    detail=client.get(f"/api/v1/admin/feedback/{fid}")
    assert detail.status_code==200,detail.text
    payload=detail.json()
    assert payload["conversation_id"]==c and payload["run_id"]==run and payload["message_id"]==r["message"]["id"]
    assert payload["description"]==data["description"] and len(payload["images"])==1
    for excluded in ("SENSITIVE_BODY_EXCLUDED","SECRET_PARAMETER","PRIVATE_RESULT","SENSITIVE_DETAIL"):
        assert excluded not in detail.text
    assert any(e["data"].get("tool_name")=="fixture_query" for e in payload["diagnostics"]["events"])
    image=payload["images"][0]["id"]
    response=client.get(f"/api/v1/admin/feedback/{fid}/images/{image}")
    assert response.status_code==200 and response.headers["content-type"]=="image/jpeg"
    assert response.headers["cache-control"]=="no-store"
    assert client.patch(f"/api/v1/admin/feedback/{fid}",json={"status":"resolved"}).json()["status"]=="resolved"
    login(client,"member")
    assert client.get(f"/api/v1/admin/feedback/{fid}/images/{image}").status_code==403


def test_feedback_rejects_cross_conversation_and_invalid_images(client):
    service=client.app.state.service
    c1=conversation(client)["id"];c2=conversation(client)["id"]
    r=send(client,c2,"other session","feedback-other-1")
    assert poll(client,r["run_id"])["status"]=="succeeded"
    route=f"/api/v1/conversations/{c1}/feedback"
    data={"description":"issue","request_id":"feedback-cross-1"}
    assert client.post(route,data={**data,"run_id":r["run_id"]}).status_code==404
    assert client.post(route,data={**data,"message_id":r["message"]["id"]}).status_code==404
    assert client.post(route,data=data,files=[("screenshots",("bad.svg",b"<svg/>","image/svg+xml"))]).status_code==422
    assert client.post(route,data=data,files=[("screenshots",("bad.png",b"not an image","image/png"))]).status_code==422
    assert client.post(route,data=data,files=[("screenshots",("shot.png",png(),"image/png"))]*4).status_code==422
    assert service.db.all("SELECT id FROM conversation_feedback")==[]
    service.create_user({"username":"other","display_name":"Other","password":PASSWORD,"role":"member"})
    login(client,"other")
    assert client.post(route,data=data).status_code==404


def test_feedback_retention_cascades_screenshot_cleanup(client):
    service=client.app.state.service
    cid=conversation(client)["id"]
    fid=client.post(f"/api/v1/conversations/{cid}/feedback",data={"description":"retention","request_id":"feedback-retention"},
        files=[("screenshots",("shot.png",png(),"image/png"))]).json()["id"]
    service.db.execute("UPDATE conversation_feedback SET created_at=? WHERE id=?",((datetime.now(timezone.utc)-timedelta(days=91)).isoformat(),fid))
    sweep_feedback(service)
    assert not service.db.one("SELECT id FROM conversation_feedback WHERE id=?",(fid,))
    assert not service.db.one("SELECT id FROM conversation_feedback_images WHERE feedback_id=?",(fid,))


def test_own_feedback_receipt_history_reflects_admin_status_and_isolation(client):
    service=client.app.state.service
    for username in ("reporter", "other"):
        service.create_user({"username":username,"display_name":username,"password":PASSWORD,"role":"member"})
    login(client,"reporter")
    cid=conversation(client)["id"]
    other_cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/feedback"
    other_route=f"/api/v1/conversations/{other_cid}/feedback"
    assert client.get(route).json()==[]
    fid=client.post(route,data={"description":"my issue","request_id":"own-status-1"}).json()["id"]
    other_id=client.post(other_route,data={"description":"another session","request_id":"own-status-2"}).json()["id"]
    reply=client.get(route)
    assert reply.headers["cache-control"]=="no-store"
    rows=reply.json()
    assert len(rows)==1 and rows[0]["id"]==fid and rows[0]["status"]=="new"
    assert set(rows[0])=={"id","description","status","created_at","updated_at","run_id","message_id","resolution_note"}
    assert other_id not in reply.text and "another session" not in reply.text
    assert client.patch(f"/api/v1/admin/feedback/{fid}",json={"status":"resolved"}).status_code==403
    # Administrator processing does not grant access to another user's
    # reporter endpoint or start a model turn.
    login(client)
    assert client.get(route).status_code==404
    assert client.patch(f"/api/v1/admin/feedback/{fid}",json={"status":"in_progress"}).status_code==200
    login(client,"reporter")
    assert client.get(route).json()[0]["status"]=="in_progress"
    login(client,"other")
    assert client.get(route).status_code==404
    login(client)
    assert client.patch(f"/api/v1/admin/feedback/{fid}",json={"status":"resolved"}).status_code==200
    login(client,"reporter")
    assert client.get(route).json()[0]["status"]=="resolved"
    assert service.db.all("SELECT id FROM runs WHERE conversation_id=?",(cid,))==[]
    service.db.execute("UPDATE conversation_feedback SET created_at=? WHERE id=?",
        ((datetime.now(timezone.utc)-timedelta(days=91)).isoformat(),fid))
    sweep_feedback(service)
    assert client.get(route).json()==[]


def test_published_resolution_note_is_scoped_versioned_and_legacy_compatible(client):
    service=client.app.state.service
    for name in ("reporter","other"):
        service.create_user({"username":name,"display_name":name,"password":PASSWORD,"role":"member"})
    login(client,"reporter")
    cid=conversation(client)["id"]
    route=f"/api/v1/conversations/{cid}/feedback"
    fid=client.post(route,data={"description":"fixture","request_id":"resolution-note-1"}).json()["id"]
    target=f"/api/v1/admin/feedback/{fid}"
    initial=client.get(route).json()[0]
    assert initial["resolution_note"]==""
    assert client.patch(target,json={"status":"resolved","resolution_note":"forged"}).status_code==403
    login(client)
    assert client.patch(target,json={"status":"resolved","resolution_note":"x"*2001}).status_code==422
    update=client.patch(target,json={"status":"resolved","resolution_note":"  已修复显示问题  ","expected_updated_at":initial["updated_at"]})
    assert update.status_code==200,update.text
    assert update.json()["resolution_note"]=="已修复显示问题"
    assert client.patch(target,json={"status":"new","resolution_note":"stale","expected_updated_at":initial["updated_at"]}).status_code==409
    assert client.patch(target,json={"status":"in_progress"}).json()["resolution_note"]=="已修复显示问题"
    login(client,"other")
    assert client.get(route).status_code==404
    login(client,"reporter")
    visible=client.get(route).json()[0]
    assert visible["status"]=="in_progress" and visible["resolution_note"]=="已修复显示问题"
    login(client)
    assert client.patch(target,json={"status":"resolved","resolution_note":"","expected_updated_at":visible["updated_at"]}).json()["resolution_note"]==""
    login(client,"reporter")
    assert client.get(route).json()[0]["resolution_note"]==""
    assert service.db.all("SELECT id FROM runs WHERE conversation_id=?",(cid,))==[]

def test_feedback_notifications_are_owned_versioned_and_not_model_input(client):
    service=client.app.state.service
    for name in ("reporter", "other"):
        service.create_user({"username":name,"display_name":name,"password":PASSWORD,"role":"member"})
    inbox="/api/v1/feedback/notifications"
    login(client,"reporter")
    cid=conversation(client)["id"]
    fid=client.post(f"/api/v1/conversations/{cid}/feedback",data={"description":"own fixture","request_id":"notice-1"}).json()["id"]
    assert client.get(inbox).json()=={"unread_count":0,"items":[]}
    login(client,"other")
    other_cid=conversation(client)["id"]
    other_id=client.post(f"/api/v1/conversations/{other_cid}/feedback",data={"description":"foreign fixture","request_id":"notice-2"}).json()["id"]
    login(client)
    for identity in (fid,other_id):
        assert client.patch(f"/api/v1/admin/feedback/{identity}",json={"status":"in_progress","resolution_note":"reviewing"}).status_code==200
    assert client.get(inbox).json()=={"unread_count":0,"items":[]}
    assert client.patch(inbox+"/"+fid,json={"expected_version":1}).status_code==404
    login(client,"reporter")
    result=client.get(inbox)
    assert result.headers["cache-control"]=="no-store"
    item,=result.json()["items"]
    assert result.json()["unread_count"]==1 and item["id"]==fid and item["version"]==1
    assert item["conversation_id"]==cid and item["status"]=="in_progress"
    assert set(item)=={"id","conversation_id","message_id","status","description","resolution_note","updated_at","version"}
    assert other_id not in result.text and "foreign fixture" not in result.text
    assert client.patch(inbox+"/"+other_id,json={"expected_version":1}).status_code==404
    assert client.patch(inbox+"/"+fid,json={"expected_version":0}).status_code==422
    assert client.patch(inbox+"/"+fid,json={"expected_version":"1"}).status_code==422
    assert client.patch(inbox+"/"+fid,json={"expected_version":1}).json()["unread"] is False
    assert client.patch(inbox+"/"+fid,json={"expected_version":1}).status_code==200
    assert client.get(inbox).json()=={"unread_count":0,"items":[]}
    login(client)
    target=f"/api/v1/admin/feedback/{fid}"
    assert client.patch(target,json={"status":"in_progress","resolution_note":"reviewing"}).status_code==200
    login(client,"reporter")
    assert client.get(inbox).json()=={"unread_count":0,"items":[]}
    login(client)
    assert client.patch(target,json={"status":"resolved","resolution_note":"fixed"}).status_code==200
    login(client,"reporter")
    item,=client.get(inbox).json()["items"]
    assert item["version"]==2 and item["resolution_note"]=="fixed"
    assert client.patch(inbox+"/"+fid,json={"expected_version":1}).status_code==409
    assert client.get(inbox).json()["unread_count"]==1
    assert client.patch(inbox+"/"+fid,json={"expected_version":2}).status_code==200
    assert client.get(inbox).json()["unread_count"]==0
    assert service.db.all("SELECT id FROM runs")==[]
