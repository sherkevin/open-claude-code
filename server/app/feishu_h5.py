"""Feishu in-client web application login.

The browser obtains a short-lived code from the Feishu H5 JavaScript API.  The
server exchanges that code with Feishu, checks the returned ``open_id`` against
the deployment allowlist, and then creates the same first-party session used by
the normal username/password login.  Feishu access tokens are intentionally
not persisted: they are only used for the one identity lookup.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .db import now
from .security import AppError, token_hash
from .service import user_view


COOKIE = "zhixing_session"
H5_ATTEMPTS_PER_MINUTE = 20


class FeishuH5Login(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Feishu pre-authorization codes are short-lived and single-use.  Keep the
    # upper bound generous enough for both requestAccess and requestAuthCode.
    code: str = Field(min_length=1, max_length=2048)


class FeishuH5RemoteError(RuntimeError):
    pass


def _remote_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "飞书返回了无效响应"
    value = payload.get("msg") or payload.get("message") or payload.get("error_description") or payload.get("error")
    return str(value)[:240] if value else "飞书身份校验失败"


def _payload_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise FeishuH5RemoteError("invalid response")
    # The legacy open.feishu.cn endpoints return top-level fields for the app
    # token and a `data` object for user tokens.  Accept both documented shapes
    # so a tenant can migrate the Feishu endpoint without changing this app.
    code = payload.get("code")
    if isinstance(code, int) and code != 0:
        raise FeishuH5RemoteError(_remote_message(payload))
    if isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


async def exchange_feishu_identity(settings, code: str) -> dict[str, str]:
    """Exchange a one-time H5 code for the caller's Feishu open ID."""
    if not settings.feishu_h5_ready:
        raise AppError(503, "FEISHU_H5_NOT_CONFIGURED", "飞书网页应用尚未完成服务端配置")

    base = settings.feishu_api_base.rstrip("/")
    timeout = httpx.Timeout(8.0, connect=3.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            app_response = await client.post(
                f"{base}/open-apis/auth/v3/app_access_token/internal",
                json={"app_id": settings.feishu_h5_app_id, "app_secret": settings.feishu_h5_app_secret},
            )
            app_payload = app_response.json()
            if app_response.status_code >= 400:
                raise FeishuH5RemoteError(_remote_message(app_payload))
            app_data = _payload_data(app_payload)
            app_access_token = app_data.get("app_access_token") or app_data.get("access_token")
            if not isinstance(app_access_token, str) or not app_access_token:
                raise FeishuH5RemoteError("missing app access token")

            user_response = await client.post(
                f"{base}/open-apis/authen/v1/access_token",
                headers={"Authorization": f"Bearer {app_access_token}"},
                json={"grant_type": "authorization_code", "code": code},
            )
            user_payload = user_response.json()
            if user_response.status_code >= 400:
                raise FeishuH5RemoteError(_remote_message(user_payload))
            user_data = _payload_data(user_payload)
            user_access_token = user_data.get("access_token") or user_data.get("user_access_token")
            if not isinstance(user_access_token, str) or not user_access_token:
                raise FeishuH5RemoteError("missing user access token")

            info_response = await client.get(
                f"{base}/open-apis/authen/v1/user_info",
                headers={"Authorization": f"Bearer {user_access_token}"},
            )
            info_payload = info_response.json()
            if info_response.status_code >= 400:
                raise FeishuH5RemoteError(_remote_message(info_payload))
            info = _payload_data(info_payload)
    except AppError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, FeishuH5RemoteError) as exc:
        # Do not expose Feishu's response body, tokens, or application details
        # to the browser.  The cause remains available in server logs through
        # the request trace, without logging the secret-bearing payload.
        raise AppError(502, "FEISHU_AUTH_FAILED", "飞书身份校验失败，请稍后重试") from exc

    open_id = info.get("open_id") or info.get("openId")
    if not isinstance(open_id, str) or not open_id:
        raise AppError(502, "FEISHU_AUTH_FAILED", "飞书未返回有效用户身份")
    display_name = info.get("name") or info.get("en_name") or ""
    return {"open_id": open_id, "display_name": str(display_name)[:100]}


def _user_map(settings) -> dict[str, str]:
    if not settings.feishu_h5_user_map:
        return {}
    try:
        value = json.loads(settings.feishu_h5_user_map)
    except (TypeError, ValueError) as exc:
        raise AppError(503, "FEISHU_H5_INVALID_USER_MAP", "飞书网页应用用户映射配置无效") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()):
        raise AppError(503, "FEISHU_H5_INVALID_USER_MAP", "飞书网页应用用户映射配置无效")
    return value


def configured_h5_user(service, open_id: str):
    settings = service.settings
    if not settings.feishu_h5_allowed_open_ids:
        raise AppError(503, "FEISHU_H5_ALLOWLIST_NOT_CONFIGURED", "服务端尚未配置飞书网页应用 open_id 白名单")
    if open_id not in settings.feishu_h5_allowed_open_ids:
        raise AppError(403, "FEISHU_H5_USER_DENIED", "此飞书账号未被允许使用工作台")

    configured = _user_map(settings).get(open_id)
    if configured:
        user = service.db.one("SELECT * FROM users WHERE (id=? OR username=?) AND enabled=1", (configured, configured))
    elif settings.feishu_user_id:
        # Single-user deployments can reuse the bridge identity while they are
        # migrating to an explicit open_id -> local-user mapping.
        user = service.db.one("SELECT * FROM users WHERE id=? AND enabled=1", (settings.feishu_user_id,))
    elif settings.feishu_username:
        user = service.db.one("SELECT * FROM users WHERE username=? AND enabled=1", (settings.feishu_username,))
    else:
        user = None
    if not user:
        raise AppError(503, "FEISHU_H5_USER_NOT_CONFIGURED", "飞书账号尚未绑定工作台成员")
    return user


def _check_rate_limit(request: Request) -> None:
    """Bound calls that can spend a Feishu identity-exchange request."""
    attempts = getattr(request.app.state, "feishu_h5_attempts", None)
    if attempts is None:
        return
    ip = request.client.host if request.client else "unknown"
    stamp = time.monotonic()
    recent = [value for value in attempts.get(ip, []) if value > stamp - 60]
    if len(recent) >= H5_ATTEMPTS_PER_MINUTE:
        attempts[ip] = recent
        raise AppError(429, "FEISHU_H5_RATE_LIMIT", "飞书免登请求过于频繁，请稍后重试")
    attempts[ip] = recent + [stamp]


def create_feishu_h5_router(service_dependency):
    router = APIRouter(prefix="/api/v1/feishu/h5", tags=["Feishu H5"])

    @router.get("/config")
    def config(service=Depends(service_dependency)):
        settings = service.settings
        return {
            "enabled": settings.feishu_h5_ready,
            "app_id": settings.feishu_h5_app_id if settings.feishu_h5_ready else "",
            "path": "/feishu",
        }

    @router.post("/login")
    async def login(data: FeishuH5Login, request: Request, response: Response, service=Depends(service_dependency)):
        _check_rate_limit(request)
        identity = await exchange_feishu_identity(service.settings, data.code)
        user = configured_h5_user(service, identity["open_id"])

        token = secrets.token_urlsafe(48)
        service.db.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=service.settings.session_hours)).isoformat()
        service.db.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), user["id"], expires_at))
        response.set_cookie(
            service.settings.session_cookie_name,
            token,
            httponly=True,
            secure=service.settings.cookie_secure,
            samesite="lax",
            max_age=service.settings.session_hours * 3600,
            path=service.settings.cookie_path,
        )
        return user_view(user)

    return router
