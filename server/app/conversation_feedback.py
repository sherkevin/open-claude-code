"""Explicit, scoped customer feedback; never automatically export chat bodies."""
from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import Field

from .db import now, offload, offload_write, uid
from .models import InputModel
from .security import AppError

MAX_IMAGE = 4 * 1024 * 1024
MAX_IMAGES = 3
MAX_TOTAL = 8 * 1024 * 1024
MAX_DESCRIPTION = 4000
RETENTION_DAYS = 90
MIMES = {"image/png", "image/jpeg", "image/webp"}
EVENT_FIELDS = ("tool_name", "success", "error_code", "duration_ms", "requires_approval",
                "tool_call_id", "scope", "decision", "status", "stage", "trace_id",
                "subagent_child_id", "child_id", "approval_id")
SAFE_EVENTS = {"tool.started", "tool.completed", "tool.dispatched", "tool.discarded",
               "approval.session_decided", "approval.requested", "approval.decided",
               "run.stage", "run.completed", "run.failed", "run.cancelled",
               "run.interrupted", "run.started", "subagent.completed"}
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class FeedbackStatus(InputModel):
    status: Literal["new", "in_progress", "resolved"]


def normalize_image(raw, mime):
    # The existing attachment validator enforces decoded dimensions and pixels.
    from .harness_attachments import _image
    from PIL import Image
    if mime not in MIMES or not raw or len(raw) > MAX_IMAGE:
        raise AppError(422, "FEEDBACK_IMAGE_INVALID", "截图仅支持 PNG、JPG、WebP，每张不超过 4 MB")
    try:
        _image(raw, mime)
        with Image.open(io.BytesIO(raw)) as image:
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            rgb.save(output, format="JPEG", quality=90)
            data = output.getvalue()
        if len(data) > MAX_IMAGE:
            raise ValueError()
        return data
    except Exception:
        raise AppError(422, "FEEDBACK_IMAGE_INVALID", "截图格式无效或超过大小限制") from None


def save_feedback(service, user, cid, description, request_id, run_id, message_id, images):
    description = description.strip()
    if not description or len(description) > MAX_DESCRIPTION or not ID_RE.fullmatch(request_id):
        raise AppError(422, "FEEDBACK_INVALID", "请填写有效的问题描述")
    with service.db.transaction():
        service.require("conversations", cid, user)
        if not service.require("users", user["id"])["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        prior = service.db.one("SELECT id,conversation_id FROM conversation_feedback WHERE user_id=? AND request_id=?",
                               (user["id"], request_id))
        if prior:
            if prior["conversation_id"] != cid:
                raise AppError(409, "FEEDBACK_CONFLICT", "反馈编号与会话不一致")
            return {"id": prior["id"]}
        if message_id:
            msg = service.db.one("SELECT id,run_id FROM messages WHERE id=? AND conversation_id=?", (message_id, cid))
            if not msg or run_id and msg["run_id"] != run_id:
                raise AppError(404, "FEEDBACK_TARGET_NOT_FOUND", "反馈关联的消息不存在")
            run_id = msg["run_id"]
        if run_id:
            run = service.db.one("SELECT id FROM runs WHERE id=? AND conversation_id=? AND user_id=?", (run_id, cid, user["id"]))
            if not run:
                raise AppError(404, "FEEDBACK_TARGET_NOT_FOUND", "反馈关联的执行轮次不存在")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        if service.db.one("SELECT count(*) n FROM conversation_feedback WHERE user_id=? AND created_at>=?",
                          (user["id"], cutoff))["n"] >= 20:
            raise AppError(429, "FEEDBACK_LIMIT", "每天最多提交 20 条问题反馈")
        fid, stamp = uid(), now()
        service.db.execute("INSERT INTO conversation_feedback(id,user_id,conversation_id,run_id,message_id,description,request_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (fid,user["id"],cid,run_id,message_id,description,request_id,stamp,stamp))
        for data in images:
            service.db.execute("INSERT INTO conversation_feedback_images VALUES(?,?,?,?,?)",
                               (uid(),fid,"image/jpeg",data,len(data)))
        return {"id": fid}


def sweep_feedback(service):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    service.db.execute("DELETE FROM conversation_feedback WHERE created_at<?", (cutoff,))


def _diagnostics(service, row):
    if not row["run_id"]:
        return None
    run = service.db.one("SELECT id,status,error_code,created_at,completed_at,execution_meta FROM runs WHERE id=? AND conversation_id=?",
                         (row["run_id"], row["conversation_id"]))
    if not run:
        return None
    meta = json.loads(run.pop("execution_meta") or "{}")
    run["runtime_type"] = meta.get("runtime_type")
    run["model"] = meta.get("model")
    events = []
    kinds = sorted(SAFE_EVENTS)
    placeholders = ",".join("?" for _ in kinds)
    rows = service.db.all(f"SELECT id,type,created_at,data FROM events WHERE run_id=? AND type IN ({placeholders}) ORDER BY id DESC LIMIT 151", (run["id"], *kinds))
    truncated = len(rows) > 150
    for event in reversed(rows[:150]):
        data = json.loads(event.pop("data") or "{}")
        event["data"] = {k:v for k,v in data.items() if k in EVENT_FIELDS and
                         (type(v) in (bool,int,float) or isinstance(v,str) and ID_RE.fullmatch(v))}
        events.append(event)
    return {"run": run, "events": events, "events_truncated": truncated}


def create_feedback_router(svc, authenticated, admin):
    router = APIRouter(prefix="/api/v1")

    @router.post("/conversations/{cid}/feedback", status_code=201)
    async def submit(cid: str, description: str=Form(..., max_length=MAX_DESCRIPTION),
                     request_id: str=Form(..., max_length=160),
                     run_id: str|None=Form(None, max_length=160),
                     message_id: str|None=Form(None, max_length=160),
                     screenshots: list[UploadFile]=File(default=[]),
                     user=Depends(authenticated), service=Depends(svc)):
        await offload(service.require, "conversations", cid, user)
        if len(screenshots) > MAX_IMAGES:
            raise AppError(422, "FEEDBACK_IMAGE_LIMIT", "最多上传 3 张截图")
        normalized, total = [], 0
        for file in screenshots:
            raw = await file.read(MAX_IMAGE+1)
            total += len(raw)
            if total > MAX_TOTAL:
                raise AppError(413, "FEEDBACK_IMAGE_LIMIT", "截图合计不能超过 8 MB")
            normalized.append(await offload(normalize_image, raw, file.content_type))
        if sum(map(len,normalized)) > MAX_TOTAL:
            raise AppError(413, "FEEDBACK_IMAGE_LIMIT", "截图合计不能超过 8 MB")
        return await offload_write(save_feedback, service,user,cid,description,request_id,run_id,message_id,normalized)

    @router.get("/admin/feedback")
    def list_feedback(service=Depends(svc), user=Depends(admin)):
        return service.db.all("""SELECT f.id,f.user_id,f.conversation_id,f.run_id,f.message_id,
            f.status,f.created_at,f.updated_at,u.display_name,
            (SELECT count(*) FROM conversation_feedback_images i WHERE i.feedback_id=f.id) AS image_count
            FROM conversation_feedback f JOIN users u ON u.id=f.user_id
            ORDER BY f.created_at DESC,f.id DESC LIMIT 100""")

    @router.get("/admin/feedback/{fid}")
    def get_feedback(fid:str, service=Depends(svc), user=Depends(admin)):
        row=service.require("conversation_feedback",fid)
        row.pop("request_id",None)
        row["images"]=service.db.all("SELECT id,mime,size FROM conversation_feedback_images WHERE feedback_id=?", (fid,))
        row["diagnostics"]=_diagnostics(service,row)
        return row

    @router.get("/admin/feedback/{fid}/images/{image_id}")
    def get_image(fid:str,image_id:str,service=Depends(svc),user=Depends(admin)):
        row=service.db.one("SELECT mime,content FROM conversation_feedback_images WHERE id=? AND feedback_id=?", (image_id,fid))
        if not row:
            raise AppError(404,"NOT_FOUND","截图不存在")
        return Response(row["content"],media_type=row["mime"],headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})

    @router.patch("/admin/feedback/{fid}")
    def update_status(fid:str,body:FeedbackStatus,service=Depends(svc),user=Depends(admin)):
        service.require("conversation_feedback",fid)
        service.db.execute("UPDATE conversation_feedback SET status=?,updated_at=? WHERE id=?", (body.status,now(),fid))
        return {"id":fid,"status":body.status}

    return router
