"""Invite-only self registration for the existing single-organization workspace."""
from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from .db import now, uid
from .security import AppError, password_hash

MIN_PASSWORD_LENGTH = 12


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=3, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH, max_length=128)
    invite_code: str = Field(min_length=1, max_length=256)


def registration_available(settings):
    # An enabled flag alone must never turn a private organization public.
    return bool(getattr(settings, "registration_enabled", False)
                and len(getattr(settings, "registration_invite_code", "")) >= 16)


def register(service, data, client_address):
    settings = service.settings
    if not registration_available(settings):
        raise AppError(403, "REGISTRATION_DISABLED", "暂未开放注册，请联系工作台管理员")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    # No IP addresses, passwords or invitation codes are retained in the ledger.
    source = hashlib.sha256(str(client_address or "unknown").encode()).hexdigest()
    with service.db.transaction():
        service.db.execute("DELETE FROM registration_attempts WHERE created_at<?", (cutoff,))
        total = service.db.one("SELECT COUNT(*) AS n FROM registration_attempts")["n"]
        local = service.db.one("SELECT COUNT(*) AS n FROM registration_attempts WHERE source_hash=?", (source,))["n"]
        if (local >= max(1, getattr(settings, "registration_ip_limit", 5))
                or total >= max(1, getattr(settings, "registration_global_limit", 50))):
            raise AppError(429, "REGISTRATION_RATE_LIMIT", "注册尝试较多，请一小时后再试或联系管理员")
        service.db.execute("INSERT INTO registration_attempts VALUES(?,?,?)", (uid(), source, now()))
    if not hmac.compare_digest(data.invite_code.encode(), settings.registration_invite_code.encode()):
        raise AppError(403, "INVITATION_INVALID", "邀请码无效，请向工作台管理员确认")
    username = data.username.strip().lower()
    name = data.display_name.strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,63}", username) or not name:
        raise AppError(422, "REGISTRATION_INVALID", "账号须为3–64位字母、数字、点、下划线或连字符，显示名称不能为空")
    password = data.password
    if (len(password) < MIN_PASSWORD_LENGTH or len(password) > 128
            or not any(ch.isalpha() for ch in password) or not any(ch.isdigit() for ch in password)
            or username in password.casefold() or password.casefold() in {"password123456", "1234567890abc"}):
        raise AppError(422, "PASSWORD_POLICY", "密码须为12–128个字符，包含字母和数字，且不能包含账号")
    hashed = password_hash(password)
    try:
        with service.db.transaction():
            if service.db.one("SELECT id FROM users WHERE lower(username)=?", (username,)):
                raise AppError(409, "USERNAME_EXISTS", "该账号已被使用，请换一个账号或直接登录")
            service.db.execute("INSERT INTO users(id,username,display_name,password_hash,role,enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                               (uid(), username, name, hashed, "member", 1, now()))
    except sqlite3.IntegrityError as exc:
        raise AppError(409, "USERNAME_EXISTS", "该账号已被使用，请换一个账号或直接登录") from exc
    return {"registered": True, "username": username, "message": "注册成功，请使用新账号登录"}


def create_registration_router(svc):
    router = APIRouter()

    @router.post("/api/v1/auth/register", status_code=201)
    def register_account(data: RegistrationRequest, request: Request, service=Depends(svc)):
        # Do not trust arbitrary X-Forwarded-For supplied by a registrant.
        return register(service, data, request.client.host if request.client else "unknown")

    return router
