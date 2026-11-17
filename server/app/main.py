from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, File, Request, Response, UploadFile, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

from .config import Settings
from .db import dump, now, offload, offload_write, uid
from .deployment_policy import DeploymentPolicy
from .models import *
from .middleware import BodyLimitMiddleware, TrustedProxySchemeMiddleware
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
        manager = app.state.manager = RunManager(service, runtime_factory)
        service.manager = manager
        manager.recover()
        app.state.login_attempts = {}
        app.state.feishu_h5_attempts = {}
        app.state.dummy_password_hash = password_hash(secrets.token_urlsafe(32))
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
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.origins), allow_credentials=True, allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type", "Last-Event-ID"])
    app.add_middleware(BodyLimitMiddleware, maximum=settings.upload_max_bytes + 1024 * 1024)
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
            try:
                too_large = int(request.headers.get("content-length", "0")) > settings.upload_max_bytes + 1024 * 1024
            except ValueError:
                too_large = True
            if too_large:
                return JSONResponse(status_code=413, content={"error": {"code": "UPLOAD_TOO_LARGE", "message": "上传内容超过大小限制"}})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
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
    from .workflows import create_workflow_router
    app.include_router(create_workflow_router(svc, authenticated, enabled=settings.workflows_enabled))
    from .kaiwu_control import create_kaiwu_control_router
    app.include_router(create_kaiwu_control_router(svc))

    prefix = "/api/v1"

    @app.get(prefix + "/config")
    def config():
        view = policy.public_view()
        feishu_ready = settings.feishu_h5_ready and not policy.is_trial
        return {
            "brand_name": settings.brand_name,
            "registration_enabled": registration_available(settings),
            "registration_min_password_length": MIN_PASSWORD_LENGTH,
            "upload_max_bytes": settings.upload_max_bytes,
            "auth_types": ["none", "bearer"],
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

    @app.get(prefix + "/models")
    def models(user=Depends(authenticated), service: Service = Depends(svc)):
        return service.catalog.public()

    @app.post(prefix + "/models/recommend")
    async def recommend_model(data: ModelRecommendationRequest, user=Depends(authenticated), service: Service = Depends(svc)):
        """Return an advisory model recommendation without changing a session."""
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
    def connection_delete(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        service.require_resource("connections", item_id, user, edit=True)
        service.db.execute("UPDATE connections SET deleted=1,enabled=0,revision=revision+1 WHERE id=?", (item_id,))
        return Response(status_code=204)

    @app.post(prefix + "/connections/{item_id}/test")
    async def connection_test(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        return await service.test_connection(item_id, user=user)

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
        service.require_resource("assistants", item_id, user, edit=True)
        service.db.execute("UPDATE assistants SET deleted=1,enabled=0,revision=revision+1 WHERE id=?", (item_id,))
        return Response(status_code=204)

    @app.get(prefix + "/conversations")
    def conversations(q: str | None = Query(default=None, max_length=200), assistant_id: str | None = None, archived: bool | None = None, category: str | None = Query(default=None, max_length=100), user=Depends(authenticated), service: Service = Depends(svc)):
        return service.list_conversations(user, q=q, assistant_id=assistant_id, archived=archived, category=category)

    @app.post(prefix + "/conversations", status_code=201)
    def conversation_create(data: ConversationCreate, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.create_conversation(data.model_dump(), user)

    @app.get(prefix + "/conversations/{item_id}")
    def conversation_get(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        return conversation_view(service.require("conversations", item_id, user))

    @app.patch(prefix + "/conversations/{item_id}")
    def conversation_patch(item_id: str, data: ConversationPatch, user=Depends(authenticated), service: Service = Depends(svc)):
        return service.patch_conversation(item_id, data.model_dump(exclude_unset=True), user)

    @app.get(prefix + "/conversations/{item_id}/messages")
    def messages(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        service.require("conversations", item_id, user)
        return [message_view(row) for row in service.db.all("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at,rowid", (item_id,))]

    @app.post(prefix + "/conversations/{item_id}/messages", status_code=202)
    async def message_send(item_id: str, data: SendMessage, user=Depends(authenticated), service: Service = Depends(svc)):
        payload = data.model_dump()
        # Trial mode refuses advanced routing and attachments before any model,
        # run or database side effect (the service layer re-checks as well).
        service.require_trial_fields(
            auto_route=bool(payload.get("auto_route")),
            skill_ids=payload.get("skill_ids"),
            file_ids=payload.get("file_ids"),
        )
        conversation = await offload(service.require, "conversations", item_id, user)
        route_enabled = False if service.policy.is_trial else (payload.get("auto_route") if payload.get("auto_route") is not None else bool(conversation.get("auto_model_routing")))
        duplicate = await offload(service.db.one, "SELECT id FROM runs WHERE conversation_id=? AND client_request_id=?", (item_id, payload["client_request_id"]))
        if not duplicate and route_enabled and not payload.get("model_id") and payload.get("text", "").strip() and not conversation.get("active_run_id"):
            # Provider-backed fitness routing is asynchronous at the API edge;
            # prepare_run consumes the bounded result and records its evidence.
            from .model_recommendation import provider_router
            candidates = [item["id"] for item in service.catalog.public() if item["available"]]
            payload["_route_recommendation"] = await provider_router(service, payload.get("text", ""), candidates, user_id=user["id"])
        result, created = await service.prepare_and_submit_async(item_id, payload, user)
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
        from .security import redact
        return JSONResponse(content=redact(result, truncate=False), headers={"Content-Disposition": f'attachment; filename="run-{item_id}.json"'})

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

    @app.post(prefix + "/runs/{item_id}/subagents", status_code=202)
    async def subagent_plan(item_id: str, data: SubagentPlanRequest, user=Depends(authenticated), service: Service = Depends(svc)):
        """Start a protected, local synthetic read-only child plan.

        This endpoint is intentionally not a model or MCP delegation surface;
        it exists to exercise the bounded lifecycle contract without customer
        data, credentials, network calls, or paid model calls.
        """
        await service.require_async("runs", item_id, user)
        plan = await offload(service.subagents.create_plan, item_id, user["id"], data.tasks, data.mode)
        service.subagents.start_plan(item_id, user["id"])
        return plan

    @app.get(prefix + "/runs/{item_id}/subagents")
    def subagent_list(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        service.require("runs", item_id, user)
        return service.subagents.plan_view(item_id, user["id"])

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

    @app.post(prefix + "/files", status_code=201)
    async def upload(file: UploadFile = File(...), user=Depends(authenticated), service: Service = Depends(svc)):
        file_id, name = uid(), safe_name(file.filename or "attachment")
        path = settings.data_dir / "files" / file_id
        size = 0
        try:
            with path.open("xb") as output:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.upload_max_bytes:
                        raise AppError(413, "UPLOAD_TOO_LARGE", "附件超过大小限制")
                    output.write(chunk)
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            await offload_write(service.db.execute, "INSERT INTO files VALUES(?,?,?,?,?,?,?,NULL)", (file_id, user["id"], name, mime, size, str(path), now()))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        return file_view(await service.require_async("files", file_id, user))

    @app.get(prefix + "/files/{item_id}")
    def file_get(item_id: str, user=Depends(authenticated), service: Service = Depends(svc)):
        return file_view(service.require("files", item_id, user))

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
