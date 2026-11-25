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
