from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_to_bytes, urlencode

from fastapi import Depends, FastAPI, File, Request, Response, UploadFile, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.routing import Match, WebSocketRoute

from .config import Settings
from .db import dump, now, offload, offload_write, uid
from .deployment_policy import DeploymentPolicy
from .models import *
from .run_steering import SteerTurn, dispatch_steer, list_steers
from .middleware import BodyLimitMiddleware, TrustedProxySchemeMiddleware, is_streaming_upload
from .prompt_safety import PromptPolicyUpdate
from .runtime import RunManager
from .security import AppError, password_hash, password_matches, safe_name, token_hash
from .service import Service, TERMINAL, user_view, connection_view, assistant_view, conversation_view, message_view, file_view, approval_view

log = logging.getLogger(__name__)
COOKIE = "zhixing_session"


class DeploymentPolicyMiddleware:
    """Run the exact trial route policy before CORS and body preprocessing."""

    def __init__(self, app, policy: DeploymentPolicy, routes):
        self.app = app
        self.policy = policy
        self.routes = routes

    def _route_template_from(self, routes, scope, method):
        candidate_scope = {**scope, "method": method}
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                template = self._route_template_from(included.routes, scope, method)
                if template is not None:
                    return template
                continue
            match, child_scope = route.matches(candidate_scope)
            if match == Match.FULL:
                return getattr(child_scope.get("route"), "path", None)
        return None

    def _route_template(self, scope, method):
        return self._route_template_from(self.routes, scope, method)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.policy.is_trial:
            return await self.app(scope, receive, send)

        method = scope.get("method", "")
        effective_method = method
        if method == "OPTIONS":
            requested_method = Headers(scope=scope).get("access-control-request-method")
            if requested_method is not None:
                effective_method = requested_method
        template = self._route_template(scope, effective_method)
        try:
            self.policy.require_route(effective_method, template)
        except AppError:
            response = JSONResponse(
                status_code=403,
                content={"error": {"code": "FEATURE_DISABLED", "message": "当前工作空间未开放此功能"}},
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, runtime_factory=None) -> FastAPI:
    settings = settings or Settings()
    policy = DeploymentPolicy(settings.deployment_mode, settings.trial_capabilities)
    prefix = "/api/v1"
    streaming_upload_path = None if policy.is_trial else prefix + "/files/binary"

    def deployment_guard(request: Request):
        """Exact template + method allowlist; runs before every handler.

        The matched route template (with router prefixes) comes from the
        Starlette scope, so trailing slashes, encoded paths, case changes and
        method variants can never match a broader prefix.  Rejections happen
        before auth and before any business handler, so a trial deployment
        produces no run/approval/database/provider side effect.
        """
        route = request.scope.get("route")
        policy.require_route(request.method, getattr(route, "path", None))

    @asynccontextmanager
    async def lifespan(app):
        service = app.state.service = Service(settings)
        service.catalog.require_credentials = runtime_factory is None
        manager = app.state.manager = RunManager(service, runtime_factory)
        service.manager = manager
        manager.recover()
        app.state.login_attempts = {}
        app.state.feishu_h5_attempts = {}
        app.state.dummy_password_hash = password_hash(secrets.token_urlsafe(32))
        manager.start_queue_worker()
        yield
        await manager.close()
        service.db.close()

    app = FastAPI(
        title="知行工作台 Server",
        version="0.4.2",
        lifespan=lifespan,
        dependencies=[Depends(deployment_guard)],
        docs_url=None if policy.is_trial else "/docs",
        redoc_url=None if policy.is_trial else "/redoc",
        openapi_url=None if policy.is_trial else "/openapi.json",
    )
    app.state.deployment_policy = policy
    # In trial mode a slash redirect is another route into code that was not
    # explicitly allowlisted.  Disable Starlette's automatic 307 instead of
    # letting a trailing-slash variant reach the router's normal handling.
    if policy.is_trial:
        app.router.redirect_slashes = False
    cors_headers = ["Content-Type", "Last-Event-ID"]
    if streaming_upload_path is not None:
        cors_headers.append("X-Upload-Filename")
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.origins), allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=cors_headers)
    app.add_middleware(BodyLimitMiddleware, maximum=settings.upload_max_bytes + 1024 * 1024, streaming_upload_path=streaming_upload_path)
    # Forwarded transport is honored only from explicitly configured proxy
    # socket addresses; application code trusts the resulting ASGI scheme.
    if settings.trusted_proxy_peers:
        app.add_middleware(TrustedProxySchemeMiddleware, peers=settings.trusted_proxy_peers)

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}})

    if policy.is_trial:
        @app.exception_handler(StarletteHTTPException)
        async def trial_http_error(request, exc):
            # Unknown routes and method variants do not have a matched route for
            # the global dependency, so fail closed here with the same generic
            # response.  Do not disclose whether an internal path exists.
            if exc.status_code in {404, 405}:
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "FEATURE_DISABLED", "message": "当前工作空间未开放此功能"}},
                )
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": {"code": "HTTP_ERROR", "message": "请求无法完成"}},
                headers=exc.headers,
            )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(status_code=422, content={"error": {"code": "VALIDATION_ERROR", "message": "请求字段无效，请检查必填项、格式与长度"}})

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        log.error("Unhandled API failure (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "服务暂时不可用，请稍后重试"}})

    @app.middleware("http")
    async def safety_headers(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            local_origin = f"{request.url.scheme}://{request.url.netloc}"
            if (origin and origin.rstrip("/") not in {*settings.origins, local_origin}) or request.headers.get("sec-fetch-site") == "cross-site" and not origin:
                return JSONResponse(status_code=403, content={"error": {"code": "ORIGIN_DENIED", "message": "请求来源未授权"}})
            if not is_streaming_upload(request.scope, streaming_upload_path):
                try:
                    too_large = int(request.headers.get("content-length", "0")) > settings.upload_max_bytes + 1024 * 1024
                except ValueError:
                    too_large = True
                if too_large:
                    return JSONResponse(status_code=413, content={"error": {"code": "UPLOAD_TOO_LARGE", "message": "上传内容超过大小限制"}})
        response = await call_next(request)
        # Route-specific hardening (for example the operator page) may be
        # stricter than these defaults; never overwrite it here.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    # Added last so trial route rejection wraps the CORS preflight responder,
    # Origin/Content-Length checks and request-body parsing.
    app.add_middleware(DeploymentPolicyMiddleware, policy=policy, routes=app.router.routes)

    def svc(request: Request) -> Service:
        return request.app.state.service

    def authenticated(request: Request, service: Service = Depends(svc)):
        token = request.cookies.get(settings.session_cookie_name)
        if not token:
            raise AppError(401, "UNAUTHENTICATED", "请先登录")
        row = service.db.one("SELECT users.* FROM sessions JOIN users ON sessions.user_id=users.id WHERE sessions.token_hash=? AND sessions.expires_at>? AND users.enabled=1", (token_hash(token), now()))
        if not row:
            raise AppError(401, "UNAUTHENTICATED", "登录已过期，请重新登录")
        return row

    def admin(user=Depends(authenticated), service: Service = Depends(svc)):
        service.require_admin(user)
        return user

    from .registration import create_registration_router, registration_available, MIN_PASSWORD_LENGTH
    app.include_router(create_registration_router(svc))
    from .mcp_import import create_import_router
    app.include_router(create_import_router(svc, authenticated))
    from .kaiwu_platform_export import create_kaiwu_platform_export_router
    app.include_router(create_kaiwu_platform_export_router(svc, admin))
    from .skills import create_skill_router
    app.include_router(create_skill_router(svc, authenticated))
    from .plugins import create_plugin_router
    app.include_router(create_plugin_router(svc, admin))
    from .capability_settings import create_capability_settings_router
    app.include_router(create_capability_settings_router(svc, admin))
    from .knowledge import create_knowledge_router
    from .evidence import create_evidence_router
    app.include_router(create_knowledge_router(svc, authenticated))
    app.include_router(create_evidence_router(svc, authenticated))
    from .business_data import create_business_data_router
    app.include_router(create_business_data_router(svc, authenticated))
    from .memory import create_memory_router
    app.include_router(create_memory_router(svc, authenticated))
    from .message_queue import create_message_queue_router
    app.include_router(create_message_queue_router(svc, authenticated))
    from .context import create_context_router
    app.include_router(create_context_router(svc, authenticated))
    from .feishu import create_feishu_router
    app.include_router(create_feishu_router(svc))
    from .feishu_h5 import create_feishu_h5_router
    app.include_router(create_feishu_h5_router(svc))
    from .benchmark import create_benchmark_router
    app.include_router(create_benchmark_router(svc, admin))
    from .artifacts import create_artifact_router
    app.include_router(create_artifact_router(svc, authenticated))
    from .conversation_export import create_conversation_export_router
    app.include_router(create_conversation_export_router(svc, authenticated))
    if not policy.is_trial:
        from .conversation_fork import create_conversation_fork_router
        from .conversation_interactions import create_interaction_router
        from .conversation_schedules import create_schedule_router
        app.include_router(create_conversation_fork_router(svc, authenticated))
        app.include_router(create_interaction_router(svc, authenticated))
        app.include_router(create_schedule_router(svc, authenticated))
    from .workflows import create_workflow_router
    app.include_router(create_workflow_router(svc, authenticated, enabled=settings.workflows_enabled))
    from .kaiwu_control import create_kaiwu_control_router
    app.include_router(create_kaiwu_control_router(svc))
    from .kaiwu_native_relay import NATIVE_RELAY_PATH, NativeRelayRegistry, connect_native_relay
    app.state.kaiwu_native_relay = NativeRelayRegistry()
    app.router.routes.append(WebSocketRoute(NATIVE_RELAY_PATH, connect_native_relay))
    from .kaiwu_edge import create_kaiwu_edge_router
    app.include_router(create_kaiwu_edge_router(svc, settings))

    @app.get(prefix + "/config")
    def config():
        view = policy.public_view()
        feishu_ready = settings.feishu_h5_ready and not policy.is_trial
        upload_config: dict[str, object] = {"upload_max_bytes": settings.upload_max_bytes}
        if streaming_upload_path is not None:
            from .harness_attachments import MAX_IMAGE_BYTES, attachment_limits

            upload_config.update(
                chat_attachment_upload_max_bytes=settings.chat_attachment_upload_max_bytes,
                chat_attachment_limits=attachment_limits(settings),
                legacy_chat_image_max_bytes=MAX_IMAGE_BYTES,
                chat_attachment_discard=True,
            )
        return {
            "brand_name": settings.brand_name,
            "registration_enabled": registration_available(settings),
            "registration_min_password_length": MIN_PASSWORD_LENGTH,
            **upload_config,
            "auth_types": ["none", "bearer"],
            **({"harness_subagents": bool(settings.harness_source_dir and settings.harness_node_path and settings.max_active_sessions >= 2)}
               if not policy.is_trial else {}),
            "feishu_h5": {
                "enabled": feishu_ready,
                "app_id": settings.feishu_h5_app_id if feishu_ready else "",
                "path": "/feishu",
            },
            "deployment_mode": view["deployment_mode"],
            "capabilities": view["capabilities"],
            "allowed_retrieval_modes": view["allowed_retrieval_modes"],
            **app.state.service.catalog.defaults,
        }

    @app.get(prefix + "/users/registration-invite")
    def registration_invite(response: Response, user=Depends(admin)):
        response.headers["Cache-Control"] = "no-store"
        available = registration_available(settings)
        return {"enabled": available, "invite_code": settings.registration_invite_code if available else None}

    @app.get(prefix + "/admin/prompt-safety")
    def read_prompt_safety(user_id: str | None = Query(default=None, min_length=1, max_length=100), user=Depends(admin), service: Service = Depends(svc)):
        return service.prompt_safety.admin_view(user_id)

    @app.put(prefix + "/admin/prompt-safety")
    def save_prompt_safety(data: PromptPolicyUpdate, user_id: str | None = Query(default=None, min_length=1, max_length=100), user=Depends(admin), service: Service = Depends(svc)):
        return service.prompt_safety.save_policy(data, user["id"], user_id)

    @app.get(prefix + "/admin/activity")
    def admin_activity(user_id: str | None = Query(default=None, max_length=100),
                       before: str | None = Query(default=None, max_length=220),
                       limit: int = Query(default=50, ge=1, le=100), user=Depends(admin), service: Service = Depends(svc)):
        from .admin_activity import activity_page
        return activity_page(service, user_id=user_id, before=before, limit=limit)

    @app.get(prefix + "/models")
    def models(user=Depends(authenticated), service: Service = Depends(svc)):
        return service.catalog.public()

    @app.post(prefix + "/models/recommend")
    async def recommend_model(data: ModelRecommendationRequest, user=Depends(authenticated), service: Service = Depends(svc)):
        """Return an advisory model recommendation without changing a session."""
        await offload(service.prompt_safety.require_allowed, data.task_summary, user["id"])
        from .model_recommendation import provider_router, public_router_result
        return public_router_result(await provider_router(service, data.task_summary, data.candidate_model_ids, user_id=user["id"]))

    @app.get(prefix + "/models/router-status")
    def model_router_status(user=Depends(authenticated), service: Service = Depends(svc)):
        """Return fail-closed router readiness without making a provider call."""
        from .model_recommendation import public_router_preflight
        return public_router_preflight(service)

    @app.get(prefix + "/models/provider-preflight")
    def model_provider_preflight(user=Depends(authenticated), service: Service = Depends(svc)):
        """Report independent chat, Tab and knowledge-role readiness.

        This is metadata-only. It never probes TokenHub or exposes private
        provider settings; role-specific inference remains disabled until an
        explicit adapter is configured and tested.
        """
        from .model_capabilities import provider_preflight
        return provider_preflight(service)

    @app.post(prefix + "/models/complete")
    async def complete_model_text(data: ModelCompletionRequest, user=Depends(authenticated), service: Service = Depends(svc)):
        """Return a real, bounded model completion for an explicit Tab press.

        The endpoint is deliberately opt-in and never falls back to a hard-
        coded phrase. If the authorized router route is unavailable, the
        response carries an evidence gap and a null suggestion.
        """
        await offload(service.prompt_safety.require_allowed, data.text, user["id"])
        from .model_recommendation import provider_completion, public_router_result
        return public_router_result(await provider_completion(service, data.text, user_id=user["id"]))

    @app.post(prefix + "/models/followups")
    async def model_followups(data: ModelFollowupsRequest, user=Depends(authenticated), service: Service = Depends(svc)):
        from .model_followups import conversation_followups
        return await conversation_followups(service, data.conversation_id, data.run_id, user)

    @app.get(prefix + "/health/live")
    def live():
        return {"status": "ok"}

    @app.get(prefix + "/health/ready")
    def ready(service: Service = Depends(svc)):
        service.db.one("SELECT 1")
        try:
            provider = settings.provider_env()
            configured = bool(provider.get("ANTHROPIC_API_KEY") or provider.get("ANTHROPIC_AUTH_TOKEN"))
            from .provider_policy import provider_route
            route = provider_route({**provider, "ANTHROPIC_BASE_URL": provider.get("ANTHROPIC_BASE_URL") or "https://tokenhub.tencentmaas.com"})
        except Exception:
            configured = False
            route = None
        # Readiness is a production admission signal, not a test-gateway
        # liveness signal.  Synthetic and loopback routes may be enabled by
        # deterministic tests, but they must never report a configured model
        # to a deployment or customer probe.
        # Keep readiness useful to a browser probe without exposing the
        # upstream provider or private routing policy.  Provider diagnostics
        # belong in protected operator telemetry, not the public health API.
        return {
            "status": "ready",
            "model_configured": bool(configured and route and route.production_ready),
            "model_route_ready": bool(route and route.production_ready),
        }

    @app.post(prefix + "/auth/login")
    def login(data: Login, request: Request, response: Response, service: Service = Depends(svc)):
        ip = request.client.host if request.client else ""
        key, ip_key = ("account", ip, data.username.lower()), ("ip", ip)
        attempts = request.app.state.login_attempts
        stamp = time.monotonic()
        for bucket, times in list(attempts.items()):
            recent = [value for value in times if value > stamp - 60]
            if recent:
                attempts[bucket] = recent
            else:
                attempts.pop(bucket, None)
        if len(attempts.get(key, [])) >= 8 or len(attempts.get(ip_key, [])) >= 40 or (len(attempts) >= 10000 and key not in attempts):
            raise AppError(429, "LOGIN_RATE_LIMIT", "尝试次数过多，请稍后再试")
        attempts[key] = attempts.get(key, []) + [stamp]
        attempts[ip_key] = attempts.get(ip_key, []) + [stamp]
        user = service.db.one("SELECT * FROM users WHERE username=?", (data.username,))
        valid = password_matches(data.password, user["password_hash"] if user else request.app.state.dummy_password_hash)
        if not user or not user["enabled"] or not valid:
            raise AppError(401, "INVALID_CREDENTIALS", "用户名或密码不正确")
        attempts.pop(key, None)
        token = secrets.token_urlsafe(48)
        service.db.execute("DELETE FROM sessions WHERE expires_at<?", (now(),))
        service.db.execute("INSERT INTO sessions VALUES(?,?,?)", (token_hash(token), user["id"], (datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)).isoformat()))
        response.set_cookie(settings.session_cookie_name, token, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=settings.session_hours * 3600, path=settings.cookie_path)
        return user_view(user)

    @app.get(prefix + "/auth/me")
    def me(user=Depends(authenticated)):
        return user_view(user)

    @app.post(prefix + "/auth/logout")
    def logout(request: Request, response: Response, service: Service = Depends(svc)):
        token = request.cookies.get(settings.session_cookie_name)
        if token:
            service.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(token),))
        response.delete_cookie(settings.session_cookie_name, path=settings.cookie_path, secure=settings.cookie_secure, httponly=True, samesite="lax")
        return {"ok": True}

    @app.get(prefix + "/users")
    def users(user=Depends(admin), service: Service = Depends(svc)):
        return [user_view(row) for row in service.db.all("SELECT * FROM users ORDER BY created_at")]

    @app.post(prefix + "/users", status_code=201)
    def user_create(data: UserCreate, user=Depends(admin), service: Service = Depends(svc)):
        return service.create_user(data.model_dump())

    @app.patch(prefix + "/users/{item_id}")
    def user_patch(item_id: str, data: UserPatch, user=Depends(admin), service: Service = Depends(svc)):
        return service.patch_user(item_id, data.model_dump(exclude_unset=True), user)

    @app.get(prefix + "/connections")
    def connections(user=Depends(authenticated), service: Service = Depends(svc)):
        return [connection_view(row, user) for row in service.db.all("SELECT * FROM connections WHERE deleted=0 ORDER BY name") if service.resource_access(row, user)]

    @app.post(prefix + "/connections", status_code=201)
    def connection_create(data: ConnectionCreate, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.save_connection(data.model_dump(), user=user)

    @app.get(prefix + "/connections/{item_id}")
    def connection_get(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        return connection_view(service.require_resource("connections", item_id, user), user)

    @app.patch(prefix + "/connections/{item_id}")
    def connection_patch(item_id: str, data: ConnectionPatch, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.save_connection(data.model_dump(exclude_unset=True), item_id, user=user)

    @app.delete(prefix + "/connections/{item_id}", status_code=204)
    async def connection_delete(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        row = await offload(service.require_resource, "connections", item_id, user, True)
        if row["auth_type"] == "oauth":
            await service.mcp_oauth.revoke(item_id, user, delete=True)
        else:
            await offload_write(service.db.execute, "UPDATE connections SET deleted=1,enabled=0,revision=revision+1 WHERE id=?", (item_id,))
        return Response(status_code=204)

    @app.post(prefix + "/connections/{item_id}/test")
    async def connection_test(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        return await service.test_connection(item_id, user=user)

    @app.get(prefix + "/connections/oauth/profiles")
    def connection_oauth_profiles(user=Depends(authenticated), service: Service = Depends(svc)):
        return [{"url": profile.resource} for profile in service.mcp_oauth.profiles.values()]

    @app.post(prefix + "/connections/{item_id}/oauth/start")
    async def connection_oauth_start(item_id: str, request: Request, user=Depends(authenticated), service: Service = Depends(svc)):
        url = await service.mcp_oauth.start(item_id, user, token_hash(request.cookies[settings.session_cookie_name]))
        return {"authorization_url": url}

    @app.post(prefix + "/connections/{item_id}/oauth/revoke")
    async def connection_oauth_revoke(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        remotely_revoked = await service.mcp_oauth.revoke(item_id, user)
        return {"locally_revoked": True, "issuer_revoked": remotely_revoked}

    @app.get(prefix + "/connections/oauth/callback/{profile_key}")
    async def connection_oauth_callback(profile_key: str, request: Request, user=Depends(authenticated), service: Service = Depends(svc)):
        query = request.query_params
        state, code, issuer = (query.get(key) for key in ("state", "code", "iss"))
        if (not state or not re.fullmatch(r"[A-Za-z0-9_-]{43}", state)
                or code is not None and (not re.fullmatch(r"[\x21-\x7e]{1,4096}", code) or "error" in query)
                or issuer is not None and len(issuer) > 2048
                or any(len(query.getlist(key)) > 1 for key in ("state", "code", "iss", "error"))):
            raise AppError(422, "MCP_OAUTH_CALLBACK_INVALID", "OAuth 授权回调格式无效")
        outcome = {"mcp_oauth": "failed"}
        try:
            item_id = await service.mcp_oauth.complete(profile_key, user, token_hash(request.cookies[settings.session_cookie_name]), state, code, issuer)
            current = await offload(service.require_resource, "connections", item_id, user)
            await service.probe_connection(current, timeout=10)
            outcome = {"mcp_oauth": "linked", "mcp_oauth_connection": item_id}
        except AppError:
            # The browser lands on a clean URL. Provider callback values and
            # exception content never enter application redirects or UI copy.
            pass
        return RedirectResponse("/?" + urlencode(outcome), status_code=303, headers={"Referrer-Policy": "no-referrer"})

    @app.get(prefix + "/assistants")
    def assistants(manage: bool = False, user=Depends(authenticated), service: Service = Depends(svc)):
        return [assistant_view(row, user) for row in service.db.all("SELECT * FROM assistants WHERE deleted=0 ORDER BY name") if service.assistant_access(row, user) or manage and service.resource_editable(row, user)]

    @app.post(prefix + "/assistants", status_code=201)
    def assistant_create(data: AssistantCreate, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.save_assistant(data.model_dump(), user=user)

    @app.get(prefix + "/assistants/{item_id}")
    def assistant_get(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        row = service.require_resource("assistants", item_id, user)
        if not service.resource_editable(row, user) and not service.assistant_access(row, user):
            raise AppError(404, "NOT_FOUND", "助手不存在")
        return assistant_view(row, user)

    @app.patch(prefix + "/assistants/{item_id}")
    def assistant_patch(item_id: str, data: AssistantPatch, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.save_assistant(data.model_dump(exclude_unset=True), item_id, user=user)

    @app.get(prefix + "/assistants/{item_id}/model-approval")
    def model_approval_get(item_id: str, user=Depends(admin), service: Service = Depends(svc)):
        service.require("assistants", item_id)
        return service.model_approval_policy(item_id)

    @app.put(prefix + "/assistants/{item_id}/model-approval")
    def model_approval_put(item_id: str, data: ModelApprovalPolicy, user=Depends(admin), service: Service = Depends(svc)):
        return service.save_model_approval_policy(item_id, data.model_dump(), user)

    @app.get(prefix + "/assistants/{item_id}/high-risk-approval")
    def high_risk_approval_get(item_id: str, user=Depends(admin), service: Service = Depends(svc)):
        service.require("assistants", item_id)
        return service.high_risk_approval_policy(item_id)

    @app.put(prefix + "/assistants/{item_id}/high-risk-approval")
    def high_risk_approval_put(item_id: str, data: HighRiskApprovalPolicy, user=Depends(admin), service: Service = Depends(svc)):
        return service.save_high_risk_approval_policy(item_id, data.model_dump(), user)

    @app.delete(prefix + "/assistants/{item_id}", status_code=204)
    def assistant_delete(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        row = service.require_resource("assistants", item_id, user, edit=True)
        if row.get("scope", "team") == "team" and user["role"] != "admin":
            raise AppError(403, "ADMIN_REQUIRED", "团队助手的删除由管理员处理")
        service.db.execute("UPDATE assistants SET deleted=1,enabled=0,revision=revision+1 WHERE id=?", (item_id,))
        return Response(status_code=204)

    @app.get(prefix + "/conversations")
    def conversations(q: str | None = Query(default=None, max_length=200), assistant_id: str | None = None, archived: bool | None = None, category: str | None = Query(default=None, max_length=100), user=Depends(authenticated), service: Service = Depends(svc)):
        return service.list_conversations(user, q=q, assistant_id=assistant_id, archived=archived, category=category)

    @app.post(prefix + "/conversations", status_code=201)
    def conversation_create(data: ConversationCreate, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.create_conversation(data.model_dump(exclude_unset=True), user)

    @app.get(prefix + "/conversations/overview")
    def conversation_overview(user=Depends(authenticated), service: Service = Depends(svc)):
        from .session_observability import overview
        return overview(service, user)

    @app.get(prefix + "/conversations/{item_id}")
    def conversation_get(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        conversation = conversation_view(service.require("conversations", item_id, user), user)
        fork = service.db.one("SELECT inherited_through_id FROM conversation_forks WHERE conversation_id=?", (item_id,))
        return {**conversation, "fork_inherited_through_id": fork["inherited_through_id"]} if fork else conversation

    @app.get(prefix + "/conversations/{item_id}/trajectory")
    def conversation_trajectory(item_id: str, cursor: str | None = Query(default=None, max_length=1024),
                                limit: int = Query(default=50, ge=1, le=100), user=Depends(authenticated), service: Service = Depends(svc)):
        from .session_observability import trajectory
        return trajectory(service, user, item_id, cursor=cursor, limit=limit)

    @app.patch(prefix + "/conversations/{item_id}")
    def conversation_patch(item_id: str, data: ConversationPatch, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.patch_conversation(item_id, data.model_dump(exclude_unset=True), user)

    @app.get(prefix + "/conversations/{item_id}/messages")
    def messages(item_id: str, limit: int | None = Query(None, ge=1, le=100),
                 before: str | None = Query(None, min_length=1, max_length=128),
                 after: str | None = Query(None, min_length=1, max_length=128),
                 around: str | None = Query(None, min_length=1, max_length=128),
                 user=Depends(authenticated), service: Service = Depends(svc)):
        from .conversation_history import list_messages, message_history

        with service.db.lock:
            service.require("conversations", item_id, user)
            if limit is not None or before is not None or after is not None or around is not None:
                return message_history(service.db, item_id, user["id"], limit or 40, before=before, after=after, around=around)
            return list_messages(service.db, item_id, user["id"])

    @app.post(prefix + "/conversations/{item_id}/messages", status_code=202)
    async def message_send(item_id: str, data: SendMessage, user=Depends(authenticated), service: Service = Depends(svc)):
        result, _ = await service.prepare_and_submit_async(item_id, data.model_dump(), user, provider_routing=True)
        return result

    @app.get(prefix + "/runs/{item_id}")
    def run_get(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.run_view(service.require("runs", item_id, user))

    @app.get(prefix + "/runs/{item_id}/trace")
    def run_trace(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        row = service.require("runs", item_id, user)
        end = datetime.fromisoformat(row["completed_at"] or now())
        started = datetime.fromisoformat(row["created_at"])
        result = {"schema_version": 1, "generated_at": now(), "run": {**service.run_view(row), "duration_ms": max(0, int((end - started).total_seconds() * 1000))}, "messages": [message_view(item) for item in service.db.all("SELECT * FROM messages WHERE run_id=? ORDER BY created_at,rowid", (item_id,))], "events": service.public_events(item_id, 0), "usage": json.loads(row["usage"]) if row["usage"] else None}
        from .run_trace import redact_run_trace
        return JSONResponse(content=redact_run_trace(result), headers={"Content-Disposition": f'attachment; filename="run-{item_id}.json"'})

    if not policy.is_trial:
        @app.get(prefix + "/runs/{item_id}/steering")
        def run_steering(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
            return list_steers(service, item_id, user)

        @app.post(prefix + "/runs/{item_id}/steering")
        async def steer_run(item_id: str, body: SteerTurn, user=Depends(authenticated), service: Service = Depends(svc)):
            return await dispatch_steer(service, item_id, body, user)

    @app.get(prefix + "/runs/{item_id}/events")
    async def run_events(item_id: str, request: Request, after: int = 0, user=Depends(authenticated), service: Service = Depends(svc)):
        await service.require_async("runs", item_id, user)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")), 0)
        except ValueError:
            raise AppError(422, "INVALID_CURSOR", "事件游标无效")

        async def stream():
            nonlocal cursor
            quiet = 0
            final_pass = False
            while True:
                # Existing streams must stop after a user is disabled or session revoked.
                try:
                    await offload(authenticated, request, service)
                except AppError:
                    return
                for event in await service.public_events_async(item_id, cursor):
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {dump(event)}\n\n"
                if final_pass:
                    return
                if (await service.require_async("runs", item_id, user))["status"] in TERMINAL:
                    # The event snapshot above and this terminal read are two
                    # separate reads, so a run can finish between them. Replay
                    # once more from the current cursor before closing: the
                    # events committed together with the terminal status (for
                    # example ``run.completed``) belong to this connection and
                    # must not be left for a reconnect to discover.
                    final_pass = True
                    continue
                if await request.is_disconnected():
                    return
                quiet += 1
                if quiet % 40 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    if not policy.is_trial:
        @app.get(prefix + "/conversations/{item_id}/subagent-plans")
        def subagent_plans(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
            service.require("conversations", item_id, user)
            return service.db.all("""SELECT id,created_at,status FROM runs r WHERE r.conversation_id=?
                AND EXISTS(SELECT 1 FROM subagent_children c WHERE c.parent_run_id=r.id)
                ORDER BY created_at DESC,r.rowid DESC LIMIT 100""", (item_id,))

    @app.post(prefix + "/runs/{item_id}/subagents", status_code=202)
    async def subagent_plan(item_id: str, data: SubagentPlanRequest, user=Depends(authenticated), service: Service = Depends(svc)):
        await service.require_async("runs", item_id, user)
        plan = await offload(service.subagents.create_plan, item_id, user["id"], data.tasks, data.mode)
        service.subagents.start_plan(item_id, user["id"])
        return plan

    @app.get(prefix + "/runs/{item_id}/subagents")
    def subagent_list(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        service.require("runs", item_id, user)
        return service.subagents.plan_view(item_id, user["id"], include_display_titles=True)

    @app.post(prefix + "/runs/{item_id}/subagents/cancel")
    async def subagent_cancel(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        await service.require_async("runs", item_id, user)
        return await service.subagents.cancel_plan(item_id, user["id"])

    @app.post(prefix + "/runs/{item_id}/cancel")
    async def run_cancel(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        await service.require_async("runs", item_id, user)
        await service.manager.cancel(item_id)
        return await offload(lambda: service.run_view(service.require("runs", item_id, user)))

    @app.get(prefix + "/approvals/pending")
    def pending_approvals(user=Depends(admin), service: Service = Depends(svc)):
        """Return only pending dual approvals assigned to this administrator.

        Arguments are projected through the existing redactor; the endpoint
        never exposes a global queue or credentials to non-allowlisted users.
        """
        rows = service.db.all("SELECT * FROM approvals WHERE status='pending' AND approval_mode='dual' ORDER BY created_at")
        visible = []
        for row in rows:
            # Do not surface approvals whose immutable policy snapshot is no
            # longer current. Voting such a request is rejected server-side;
            # hiding it here avoids presenting stale work in the admin queue.
            if row.get("expires_at") <= now():
                continue
            if not service.high_risk_approval._row_policy_current(row):
                continue
            if user["id"] in json.loads(row.get("approver_allowlist") or "[]"):
                visible.append(approval_view(row))
        return visible

    @app.get(prefix + "/approvals/{item_id}/memory-content")
    def memory_approval_content(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        from .harness_memory import validate_memory_approval

        row = service.require("approvals", item_id, user)
        return validate_memory_approval(service, row, user)

    @app.post(prefix + "/approvals/{item_id}/decision")
    async def decide(item_id: str, data: ApprovalDecision, user=Depends(authenticated), service: Service = Depends(svc)):
        row = await service.require_async("approvals", item_id)
        from .harness_memory import MEMORY_WRITE, validate_memory_approval

        if row["tool_name"] == MEMORY_WRITE:
            row = await service.require_async("approvals", item_id, user)
            if row.get("approval_mode") == "dual":
                raise AppError(403, "MEMORY_PRIVATE_REVIEW_UNAVAILABLE", "当前策略不支持私有记忆的跨用户正文审批")
            if data.decision == "allow":
                content = await offload(validate_memory_approval, service, row, user)
                if not data.memory_content_digest or not secrets.compare_digest(data.memory_content_digest, content["content_digest"]):
                    raise AppError(409, "MEMORY_REVIEW_REQUIRED", "请先打开完整记忆正文并确认本次写入内容")
        if row.get("approval_mode") == "dual":
            result = await offload_write(service.high_risk_approval.record_vote, row, user, data.decision)
            # Only the final vote wakes the waiting runtime.  The immutable
            # vote and progress event are persisted even while the approval
            # remains pending for a second, distinct administrator.
            await offload_write(service.db.event, row["run_id"], "approval.dual_vote", {
                "approval_id": row["id"], "decision": data.decision,
                "votes": result.get("votes", 0), "required": result.get("required", row.get("required_approvals", 2)),
                "voter_id": user["id"], "terminal": result.get("terminal", False),
            })
            if not result.get("terminal"):
                return {"id": item_id, **result, "decision_source": "dual_admin"}
            future = service.manager.approvals.get(item_id)
            if future and not future.done():
                future.set_result(data.decision)
            decided = await offload(service.db.one, "SELECT decided_at FROM approvals WHERE id=?", (item_id,))
            return {"id": item_id, **result, "decided_at": decided["decided_at"], "decision_source": "dual_admin"}
        row = await service.require_async("approvals", item_id, user)
        future = service.manager.approvals.get(item_id)
        if row["status"] != "pending" or row["expires_at"] <= now() or not future or future.done():
            raise AppError(409, "APPROVAL_EXPIRED", "此审批已处理或过期")
        # Persist the authenticated decision maker before waking the runtime.
        # The arguments remain the original immutable server-side snapshot;
        # clients cannot replace them through this endpoint.
        decided_at = now()
        status = "allowed" if data.decision == "allow" else "denied"
        updated = await offload_write(
            service.db.execute,
            "UPDATE approvals SET status=?,decided_at=?,decided_by=?,decision_source='user' WHERE id=? AND status='pending'",
            (status, decided_at, user["id"], item_id),
        )
        if updated.rowcount != 1:
            raise AppError(409, "APPROVAL_EXPIRED", "此审批已处理或过期")
        # The conditional UPDATE is the audit truth; a waiter that timed out
        # while it ran on a worker thread settles from that row.  Wake the
        # future only on the loop thread and only if it is still pending, so a
        # stale future can never raise InvalidStateError (HTTP 500) or make the
        # event stream disagree with the database.
        future = service.manager.approvals.get(item_id)
        if future is not None and not future.done():
            future.set_result(data.decision)
        return {"id": item_id, "status": status, "decided_at": decided_at, "decision_source": "user"}

    def register_upload(service, file_id, name, mime, size, path, user, draft, upload_id, content_digest):
        created_at = now()
        try:
            with service.db.transaction():
                if upload_id:
                    existing = service.db.one("""SELECT f.*,uploads.content_digest FROM chat_draft_upload_requests uploads
                        JOIN files f ON f.id=uploads.file_id WHERE uploads.user_id=? AND uploads.request_id=?""", (user["id"], upload_id))
                    if existing:
                        if (existing["size"], existing["name"], existing["content_digest"]) != (size, name, content_digest):
                            raise AppError(409, "UPLOAD_REQUEST_CHANGED", "上传编号对应的附件已变化，请重新添加")
                        path.unlink(missing_ok=True)
                        return file_view(existing)
                service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,NULL)",
                                   (file_id, user["id"], name, mime, size, str(path), created_at))
                if draft and not policy.is_trial:
                    from .draft_uploads import mark_draft_upload
                    mark_draft_upload(service.db, file_id)
                    if upload_id:
                        service.db.execute("INSERT INTO chat_draft_upload_requests VALUES(?,?,?,?)",
                                           (user["id"], upload_id, file_id, content_digest))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return {"id": file_id, "name": name, "mime_type": mime, "size": size, "created_at": created_at}

    upload_request = Query(None, pattern=r"^[A-Za-z0-9_-]{8,100}$")

    @app.post(prefix + "/files", status_code=201)
    async def upload(file: UploadFile = File(...), draft: bool = False, upload_id: str | None = upload_request,
                     user=Depends(authenticated), service: Service = Depends(svc)):
        upload_id = upload_id if draft and not policy.is_trial else None
        digest = hashlib.sha256() if upload_id else None
        file_id, name = uid(), safe_name(file.filename or "attachment")
        path = settings.data_dir / "files" / file_id
        size = 0
        try:
            try:
                with path.open("xb") as output:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > settings.upload_max_bytes:
                            raise AppError(413, "UPLOAD_TOO_LARGE", "附件超过大小限制")
                        if digest:
                            digest.update(chunk)
                        output.write(chunk)
            finally:
                await file.close()
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return await offload_write(register_upload, service, file_id, name, mime, size, path, user, draft, upload_id, digest.hexdigest() if digest else None)

    if streaming_upload_path is not None:
        @app.post(streaming_upload_path, status_code=201)
        async def upload_binary(request: Request, draft: bool = False, upload_id: str | None = upload_request,
                                user=Depends(authenticated), service: Service = Depends(svc)):
            upload_id = upload_id if draft else None
            digest = hashlib.sha256() if upload_id else None
            maximum = settings.chat_attachment_upload_max_bytes
            try:
                declared_size = int(request.headers.get("content-length", "0"))
            except ValueError:
                declared_size = -1
            if not 0 <= declared_size <= maximum:
                raise AppError(413, "UPLOAD_TOO_LARGE", "附件超过大小限制")

            media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
            if media_type != "application/octet-stream":
                raise AppError(415, "UNSUPPORTED_UPLOAD_TYPE", "请使用 application/octet-stream 上传附件")
            filename = request.headers.get("x-upload-filename", "")
            if not filename or not filename.isascii() or re.search(r"%(?![0-9a-fA-F]{2})", filename):
                raise AppError(400, "INVALID_UPLOAD_FILENAME", "缺少有效的附件名称")
            try:
                name = safe_name(unquote_to_bytes(filename).decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise AppError(400, "INVALID_UPLOAD_FILENAME", "缺少有效的附件名称") from exc

            file_id = uid()
            path = settings.data_dir / "files" / file_id
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            size = 0
            output = path.open("xb")
            try:
                with output:
                    try:
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > maximum:
                                raise AppError(413, "UPLOAD_TOO_LARGE", "附件超过大小限制")
                            if chunk:
                                if digest:
                                    digest.update(chunk)
                                await offload_write(output.write, chunk)
                    except ClientDisconnect as exc:
                        raise AppError(400, "UPLOAD_DISCONNECTED", "附件上传中断") from exc
                    if size == 0:
                        raise AppError(400, "UPLOAD_EMPTY", "附件为空")
                    await offload_write(output.flush)
            except BaseException:
                path.unlink(missing_ok=True)
                raise

            return await offload_write(register_upload, service, file_id, name, mime, size, path, user, draft, upload_id, digest.hexdigest() if digest else None)

    @app.get(prefix + "/files/{item_id}")
    def file_get(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        return file_view(service.require("files", item_id, user))

    if not policy.is_trial:
        @app.get(prefix + "/files/{item_id}/preview")
        async def preview_file(item_id: str, page: int = Query(1, ge=1, le=10000), user=Depends(authenticated), service: Service = Depends(svc)):
            from .document_preview import file_preview

            return await offload(file_preview, service, item_id, user, page)

    if streaming_upload_path is not None:
        @app.delete(prefix + "/files/{item_id}", status_code=204)
        async def discard_draft_file(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
            from .draft_uploads import release_draft_upload

            await offload_write(release_draft_upload, service, item_id, user)

    @app.get(prefix + "/files/{item_id}/content")
    def file_content(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        row = service.require("files", item_id, user)
        preview = row["mime_type"] in {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}
        return FileResponse(row["path"], media_type=row["mime_type"] if preview else "application/octet-stream", filename=row["name"], content_disposition_type="inline" if preview else "attachment", headers={"Content-Security-Policy": "sandbox; default-src 'none'"})

    @app.get(prefix + "/usage")
    def usage(scope: str = "self", model_id: str | None = None, date_from: str | None = None, date_to: str | None = None, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.usage(user, scope=scope, model_id=model_id, date_from=date_from, date_to=date_to)

    return app


app = create_app()
