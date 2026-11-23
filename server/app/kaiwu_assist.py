"""Customer-approved MCP discovery and import for one connector and endpoint."""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field, StrictBool, ValidationInfo, field_validator

from .db import dump, now, uid
from .kaiwu_control import (
    CONNECTOR_ID_RE,
    MAX_TOOL_COUNT,
    TOOL_NAME_RE,
    IntegrationCreateRequest,
    StrictModel,
    _audit,
    _available_native_source,
    _control_settings,
    _issue_platform_integration,
    new_trace_id,
)
from .kaiwu_diagnostics import SOURCE_ID_RE
from .security import AppError

SESSION_TTL = 30 * 60
DEVICE_LEASE = 15
QUEUE_TTL = 45
RUN_TTL = 120
SESSION_ID_RE = r"^asst_[0-9a-f]{32}$"
JOB_ID_RE = r"^asjob_[0-9a-f]{32}$"
LIVE_STATES = ("pending", "active")
UNTARGETED_ASSIST_NAME = "内网接入协助"
BRIDGE_OPERATOR_PREFIX = "workbench:"
# Locally started sessions use a device owner until an administrator joins.
DEVICE_OWNER_PREFIX = "device:"
BRIDGE_OPERATOR_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
ToolName = Annotated[str, Field(pattern=TOOL_NAME_RE.pattern)]


def _future(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _target_url(value: str) -> str:
    if re.search(r"[^!-~]|[\\?#]", value):
        raise ValueError("invalid target URL")
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
    except ValueError:
        raise ValueError("invalid target URL") from None
    if (
        not host
        or "@" in url.netloc
        or "%" in host
        or port == 0
        or not url.path.startswith("/")
        or url.path == "/"
        or url.scheme not in {"http", "https"}
    ):
        raise ValueError("invalid target URL")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ValueError("invalid target hostname") from None
    if url.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("HTTPS required for network services")
    if ip and (ip.is_unspecified or ip.is_multicast or ip.is_link_local):
        raise ValueError("invalid target address")
    address = f"[{host}]" if ":" in host else host
    if port is not None:
        address += f":{port}"
    return urlunsplit((url.scheme, address, url.path, "", ""))


class TargetInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value):
        value = IntegrationCreateRequest.valid_display_name(value)
        if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
            raise ValueError("invalid display name")
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return _target_url(value)


class OfferInput(TargetInput):
    connector_id: str = Field(pattern=CONNECTOR_ID_RE.pattern)


class SessionInput(StrictModel):
    session_id: str = Field(pattern=SESSION_ID_RE)


class StartWithoutTargetInput(StrictModel):
    pass


class ApproveTargetInput(SessionInput, TargetInput):
    pass


class PollInput(SessionInput):
    accept_jobs: StrictBool = True


class DynamicAuth(StrictModel):
    app_id: str = Field(default="MCP", max_length=256)
    channel: str = Field(min_length=1, max_length=256)
    user_id: str = Field(min_length=1, max_length=256)
    hmac_key: str = Field(min_length=1, max_length=4096, repr=False)

    @field_validator("app_id", "channel", "user_id")
    @classmethod
    def valid_header(cls, value, info: ValidationInfo):
        if len(value.encode("utf-8")) > 256 or any(char in value for char in "\r\n&"):
            raise ValueError("invalid signing identity")
        cleaned = value.strip()
        if cleaned:
            return cleaned
        if info.field_name == "app_id":
            return "MCP"
        raise ValueError("invalid signing identity")

    @field_validator("hmac_key")
    @classmethod
    def valid_key(cls, value):
        if not value.strip() or len(value.encode("utf-8")) > 4096 or any(char in value for char in "\r\n"):
            raise ValueError("invalid signing key")
        return value


class JobInput(StrictModel):
    action: Literal["discover", "connect"]
    bearer_token: str | None = Field(default=None, max_length=8192, repr=False)
    dynamic_auth: DynamicAuth | None = Field(default=None, repr=False)
    tools: list[ToolName] = Field(default_factory=list, max_length=MAX_TOOL_COUNT)

    @field_validator("bearer_token")
    @classmethod
    def valid_bearer(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value):
            raise ValueError("invalid Bearer token")
        return value


class JobResult(SessionInput):
    job_id: str = Field(pattern=JOB_ID_RE)
    status: Literal["succeeded", "failed"]
    code: Literal["tools_loaded", "no_tools", "upstream_failed", "connect_failed", "connected", "canceled"]
    stage: Literal["initialize", "tools/list", "connect"] | None = None
    http_status: int | None = Field(default=None, ge=100, le=599, strict=True)
    authorization: StrictBool | None = None
    tools: list[ToolName] = Field(default_factory=list, max_length=256)
    source_id: str | None = Field(default=None, pattern=SOURCE_ID_RE.pattern)


class ImportInput(StrictModel):
    scope: Literal["private", "team"] = "private"


def _scope(service):
    settings = _control_settings(service)
    if not settings.kaiwu_native_relay_enabled or not settings.kaiwu_assist_tenants:
        raise AppError(403, "ASSIST_DISABLED", "当前平台未开放 MCP 协助")
    return settings.kaiwu_assist_tenants


def _device_scope(service, device, connector_id):
    connector = device["connector"]
    if connector["id"] != connector_id or connector["tenant_id"] not in _scope(service):
        raise AppError(403, "ASSIST_DEVICE_DENIED", "此设备无法使用 MCP 协助")
    return connector


def _connector(service, connector_id, tenant_id=None):
    row = service.db.one("SELECT * FROM kaiwu_connectors WHERE id=?", (connector_id,))
    tenants = _scope(service)
    if not row or row["tenant_id"] not in tenants or tenant_id and row["tenant_id"] != tenant_id:
        raise AppError(404, "ASSIST_DEVICE_MISSING", "未找到可协助的设备")
    return row


def _session(service, session_id, *, connector_id=None, owner=None):
    row = service.db.one(
        "SELECT s.*, c.tenant_name, c.revoked, c.draining FROM kaiwu_assist_sessions s "
        "JOIN kaiwu_connectors c ON c.id=s.connector_id AND c.tenant_id=s.tenant_id WHERE s.id=?",
        (session_id,),
    )
    tenants = _scope(service)
    if (
        not row
        or row["tenant_id"] not in tenants
        or connector_id and row["connector_id"] != connector_id
        or owner and row["created_by"] != owner
    ):
        raise AppError(404, "ASSIST_SESSION_MISSING", "未找到此协助")
    return row


def _job(service, session_id):
    return service.db.one(
        "SELECT * FROM kaiwu_assist_jobs WHERE session_id=? ORDER BY seq DESC LIMIT 1", (session_id,),
    )


def _cancel_jobs(db, session_id, timestamp):
    db.execute(
        "UPDATE kaiwu_assist_jobs SET status='failed', result=?, finished_at=?, sealed_bearer=NULL "
        "WHERE session_id=? AND status IN ('queued','running')",
        (dump({"code": "canceled"}), timestamp, session_id),
    )


def _expire(service):
    db = service.db
    settings = service.settings
    allowed = (
        settings.kaiwu_assist_tenants
        if settings.kaiwu_control_enabled and settings.kaiwu_native_relay_enabled
        else ()
    )
    timestamp = now()
    db.execute(
        "UPDATE kaiwu_assist_sessions SET status='expired' "
        "WHERE status IN ('pending','active') AND (expires_at<=? OR connector_id IN "
        "(SELECT id FROM kaiwu_connectors WHERE revoked=1 OR draining=1) "
        "OR (instr(created_by, ?) = 1 AND ? = 0) "
        "OR (instr(created_by, ?) != 1 AND created_by != ? || connector_id AND NOT EXISTS "
        "(SELECT 1 FROM users WHERE id=created_by AND role='admin' AND enabled=1)) "
        "OR tenant_id NOT IN (SELECT value FROM json_each(?)))",
        (
            timestamp, BRIDGE_OPERATOR_PREFIX, bool(settings.kaiwu_assist_bridge_secret()),
            BRIDGE_OPERATOR_PREFIX, DEVICE_OWNER_PREFIX, dump(allowed),
        ),
    )
    db.execute(
        "UPDATE kaiwu_assist_jobs SET status='failed', result=?, finished_at=?, sealed_bearer=NULL "
        "WHERE status IN ('queued','running') AND (expires_at<=? OR session_id IN "
        "(SELECT id FROM kaiwu_assist_sessions WHERE status IN ('expired','stopped')))",
        (dump({"code": "canceled"}), timestamp, timestamp),
    )


def expire_assist_on_startup(service):
    # Clear queued credentials for tenants removed while the server was down.
    with service.db.transaction():
        _expire(service)


def _active(row):
    return row["status"] == "active" and not row["revoked"] and not row["draining"] and row["expires_at"] > now()


def _require_recent_consent(row):
    if not _active(row):
        raise AppError(409, "ASSIST_NOT_ACTIVE", "请等待客户在连接器上开始协助")
    if not row["last_poll_at"] or row["last_poll_at"] <= _future(-DEVICE_LEASE):
        raise AppError(409, "ASSIST_DEVICE_NOT_RESPONDING", "设备协助未在线，请等待连接器恢复")


def _require_target(row):
    if not row["url"]:
        raise AppError(409, "ASSIST_TARGET_REQUIRED", "请先在连接器上确认要协助的 MCP 服务")


def _offer(row):
    return {key: row[key] for key in ("id", "name", "url", "status", "expires_at")}


def _job_view(job):
    if not job:
        return None
    view = {key: job[key] for key in ("id", "action", "status", "finished_at") if job[key] is not None}
    if job["result"]:
        view.update(json.loads(job["result"]))
    return view


def _session_view(service, row):
    view = {
        **_offer(row),
        "connector_id": row["connector_id"],
        "tenant_name": row["tenant_name"],
        "latest_job": _job_view(_job(service, row["id"])),
        "discovered_tools": json.loads(row["discovered_tools"]),
    }
    if row["imported_connection_id"]:
        view["imported_connection_id"] = row["imported_connection_id"]
    return view


def _event(service, row, action, *, owner=None, job_id=None, source_id=None):
    detail = {"session_id": row["id"]}
    if job_id:
        detail["job_id"] = job_id
    _audit(
        service.db, tenant_id=row["tenant_id"], connector_id=row["connector_id"],
        source_id=source_id, actor_type="platform-user" if owner else "connector",
        actor_id=owner or row["connector_id"], action=f"assist.{action}", outcome="allow",
        trace_id=new_trace_id(), detail=detail,
    )


def _stop(service, row, *, owner=None):
    if row["status"] not in LIVE_STATES:
        return
    timestamp = now()
    service.db.execute(
        "UPDATE kaiwu_assist_sessions SET status='stopped', last_poll_at=NULL WHERE id=?", (row["id"],),
    )
    _cancel_jobs(service.db, row["id"], timestamp)
    _event(service, row, "stopped", owner=owner)


def _accept_result(service, row, job, data):
    acceptable = (
        {"tools_loaded"} if job["action"] == "discover" else {"connected"}
    ) if data.status == "succeeded" else (
        {"no_tools", "upstream_failed", "canceled"} if job["action"] == "discover" else {"connect_failed", "canceled"}
    )
    if (
        data.code not in acceptable
        or (data.code == "tools_loaded") != bool(data.tools)
        or len(set(data.tools)) != len(data.tools)
        or (data.code == "connected") != bool(data.source_id)
    ):
        raise AppError(422, "ASSIST_RESULT_INVALID", "设备返回的协助结果无效")
    if data.code != "connected":
        return None
    source = service.db.one(
        "SELECT * FROM kaiwu_sources WHERE connector_id=? AND tenant_id=? AND source_id=?",
        (row["connector_id"], row["tenant_id"], data.source_id),
    )
    if (
        not source
        or source["display_name"] != row["name"]
        or source["transport"] != "streamable_http"
        or set(json.loads(source["allowed_tools"])) != set(json.loads(job["selected_tools"]))
    ):
        raise AppError(409, "ASSIST_SOURCE_CHANGED", "设备登记的 MCP 配置与本次协助不一致")
    return source["revision"]


def add_device_assist(router, service_dependency, device):
    path = "/v1/connectors/{connector_id}/assist"

    @router.get(path + "/offer")
    def read_offer(
        connector_id: str, device_first: bool = False, target_after_start: bool = False,
        identity=Depends(device), service=Depends(service_dependency),
    ):
        if (
            identity["connector"]["id"] == connector_id
            and (
                not service.settings.kaiwu_native_relay_enabled
                or identity["connector"]["tenant_id"] not in service.settings.kaiwu_assist_tenants
            )
        ):
            return {"offer": None}
        _device_scope(service, identity, connector_id)
        with service.db.transaction():
            _expire(service)
            row = service.db.one(
                "SELECT * FROM kaiwu_assist_sessions WHERE connector_id=? AND status IN ('pending','active')",
                (connector_id,),
            )
            result = {"offer": _offer(row) if row and (row["url"] or target_after_start) else None}
            if device_first:
                result["enabled"] = True
            if target_after_start:
                result["target_after_start"] = True
            return result

    @router.post(path + "/start")
    def start(
        connector_id: str, data: TargetInput | SessionInput | StartWithoutTargetInput,
        identity=Depends(device), service=Depends(service_dependency),
    ):
        connector = _device_scope(service, identity, connector_id)
        with service.db.transaction():
            _expire(service)
            if not isinstance(data, SessionInput):
                name, url = (data.name, data.url) if isinstance(data, TargetInput) else (UNTARGETED_ASSIST_NAME, "")
                connector = _connector(service, connector_id, connector["tenant_id"])
                if connector["revoked"] or connector["draining"]:
                    raise AppError(409, "ASSIST_DEVICE_UNAVAILABLE", "此设备当前无法开始协助")
                existing = service.db.one(
                    "SELECT * FROM kaiwu_assist_sessions WHERE connector_id=? AND status IN ('pending','active')",
                    (connector_id,),
                )
                if existing and existing["status"] == "active":
                    if (existing["name"], existing["url"]) != (name, url):
                        raise AppError(409, "ASSIST_ALREADY_OPEN", "请先结束本机正在进行的协助")
                    return _offer(existing)
                if existing:
                    _stop(service, existing)
                session_id = "asst_" + uid()
                # Only the first authenticated poll makes this grant usable by an operator.
                service.db.execute(
                    "INSERT INTO kaiwu_assist_sessions"
                    "(id,connector_id,tenant_id,name,url,created_by,status,expires_at,created_at) "
                    "VALUES(?,?,?,?,?,?,'active',?,?)",
                    (
                        session_id, connector_id, connector["tenant_id"], name, url,
                        DEVICE_OWNER_PREFIX + connector_id, _future(SESSION_TTL), now(),
                    ),
                )
                row = _session(service, session_id, connector_id=connector_id)
                _event(service, row, "started")
                return _offer(row)
            row = _session(service, data.session_id, connector_id=connector_id)
            if _active(row):
                return {}
            if row["status"] != "pending":
                raise AppError(409, "ASSIST_EXPIRED", "协助申请已结束，请重新发起")
            timestamp = now()
            service.db.execute(
                "UPDATE kaiwu_assist_sessions SET status='active', expires_at=?, last_poll_at=? WHERE id=?",
                (_future(SESSION_TTL), timestamp, row["id"]),
            )
            _event(service, row, "approved")
            return {}

    @router.post(path + "/target")
    def approve_target(
        connector_id: str, data: ApproveTargetInput, identity=Depends(device), service=Depends(service_dependency),
    ):
        _device_scope(service, identity, connector_id)
        with service.db.transaction():
            _expire(service)
            row = _session(service, data.session_id, connector_id=connector_id)
            if not _active(row):
                raise AppError(409, "ASSIST_NOT_ACTIVE", "请先在连接器上开始协助")
            if row["url"]:
                if (row["name"], row["url"]) != (data.name, data.url):
                    raise AppError(409, "ASSIST_TARGET_FIXED", "已经确认了本次协助的 MCP 服务，请先停止协助再更换")
                return _offer(row)
            service.db.execute(
                "UPDATE kaiwu_assist_sessions SET name=?,url=? WHERE id=?", (data.name, data.url, row["id"]),
            )
            _event(service, row, "target_approved")
            return _offer(_session(service, row["id"], connector_id=connector_id))

    @router.post(path + "/stop")
    def stop(connector_id: str, data: SessionInput, identity=Depends(device), service=Depends(service_dependency)):
        _device_scope(service, identity, connector_id)
        with service.db.transaction():
            _expire(service)
            _stop(service, _session(service, data.session_id, connector_id=connector_id))
            return {}

    @router.post(path + "/poll")
    def poll(connector_id: str, data: PollInput, identity=Depends(device), service=Depends(service_dependency)):
        _device_scope(service, identity, connector_id)
        with service.db.transaction():
            _expire(service)
            row = _session(service, data.session_id, connector_id=connector_id)
            if not _active(row):
                return {"active": False, "job": None}
            service.db.execute("UPDATE kaiwu_assist_sessions SET last_poll_at=? WHERE id=?", (now(), row["id"]))
            if not data.accept_jobs or not row["url"]:
                return {"active": True, "job": None}
            job = _job(service, row["id"])
            if not job or job["status"] != "queued":
                return {"active": True, "job": None}
            response = {"id": job["id"], "action": job["action"]}
            if job["action"] == "connect":
                response["tools"] = json.loads(job["selected_tools"])
            if job["sealed_bearer"]:
                plaintext = service.secrets.decrypt(job["sealed_bearer"])
                # Older jobs queued before this release stored the compact
                # Bearer token by itself. It cannot start with a JSON brace.
                credentials = json.loads(plaintext) if plaintext.startswith("{") else {"bearer_token": plaintext}
                for field in ("bearer_token", "dynamic_auth"):
                    if field in credentials:
                        response[field] = credentials[field]
            service.db.execute(
                "UPDATE kaiwu_assist_jobs SET status='running', sealed_bearer=NULL, expires_at=? WHERE id=?",
                (_future(RUN_TTL), job["id"]),
            )
            return {"active": True, "job": response}

    @router.post(path + "/result")
    def result(connector_id: str, data: JobResult, identity=Depends(device), service=Depends(service_dependency)):
        _device_scope(service, identity, connector_id)
        with service.db.transaction():
            _expire(service)
            row = _session(service, data.session_id, connector_id=connector_id)
            job = service.db.one(
                "SELECT * FROM kaiwu_assist_jobs WHERE id=? AND session_id=?", (data.job_id, row["id"]),
            )
            if not job or not _active(row):
                raise AppError(409, "ASSIST_JOB_INACTIVE", "协助任务已结束")
            fields = data.model_dump(exclude={"session_id", "job_id", "status"}, exclude_none=True)
            if not data.tools:
                fields.pop("tools", None)
            encoded = dump(fields)
            if job["status"] == data.status and job["result"] == encoded:
                return {}
            if job["status"] != "running":
                raise AppError(409, "ASSIST_JOB_INACTIVE", "协助任务已结束")
            revision = _accept_result(service, row, job, data)
            service.db.execute(
                "UPDATE kaiwu_assist_jobs SET status=?, result=?, source_revision=?, finished_at=?, sealed_bearer=NULL "
                "WHERE id=?",
                (data.status, encoded, revision, now(), job["id"]),
            )
            if data.code == "tools_loaded":
                service.db.execute("UPDATE kaiwu_assist_sessions SET discovered_tools=? WHERE id=?", (dump(data.tools), row["id"]))
            _event(service, row, "result_received", job_id=job["id"], source_id=data.source_id)
            return {}


def create_kaiwu_assist_router(service_dependency, admin):
    return _assist_router(service_dependency, admin, prefix="/api/v1/connections/kaiwu-assist")


def create_kaiwu_assist_bridge_router(service_dependency):
    def bridge_operator(request: Request, service=Depends(service_dependency)):
        secret = service.settings.kaiwu_assist_bridge_secret()
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        operator = request.headers.get("x-kaiwu-operator", "")
        if (
            request.client is None
            or request.client.host not in {"127.0.0.1", "::1"}
            or not secret
            or scheme.lower() != "bearer"
            or not hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))
            or not BRIDGE_OPERATOR_RE.fullmatch(operator)
        ):
            raise AppError(403, "ASSIST_BRIDGE_DENIED", "协助服务凭据无效")
        return {"id": BRIDGE_OPERATOR_PREFIX + operator}

    return _assist_router(service_dependency, bridge_operator, prefix="/internal/kaiwu-assist/v1", bridge=True)


def _assist_router(service_dependency, admin, *, prefix: str, bridge: bool = False):
    router = APIRouter(prefix=prefix, tags=["MCP assistance"], include_in_schema=not bridge)

    @router.get("/devices")
    def devices(request: Request, service=Depends(service_dependency), user=Depends(admin)):
        tenants = _scope(service)
        placeholders = ",".join("?" for _ in tenants)
        with service.db.transaction():
            _expire(service)
            rows = service.db.all(
                f"SELECT c.id,c.tenant_id,c.tenant_name,h.received_at AS last_seen_at, "
                f"CASE WHEN s.created_by=? THEN s.id END AS session_id, "
                f"CASE WHEN s.status='active' AND s.last_poll_at>? THEN s.id END AS active_session_id "
                f"FROM kaiwu_connectors c LEFT JOIN kaiwu_heartbeats h ON h.connector_id=c.id "
                f"LEFT JOIN kaiwu_assist_sessions s ON s.connector_id=c.id "
                f"AND (s.created_by=? OR s.created_by=? || c.id) "
                f"AND s.status IN ('pending','active') WHERE c.revoked=0 AND c.draining=0 "
                f"AND c.tenant_id IN ({placeholders}) ORDER BY c.created_at DESC",
                (user["id"], _future(-DEVICE_LEASE), user["id"], DEVICE_OWNER_PREFIX, *tenants),
            )
        relay = getattr(request.app.state, "kaiwu_native_relay", None)
        for row in rows:
            row["online"] = bool(relay and relay.is_online(row["id"]))
        return {"devices": rows}

    @router.post("/sessions", status_code=201)
    def offer(data: OfferInput, service=Depends(service_dependency), user=Depends(admin)):
        with service.db.transaction():
            _expire(service)
            connector = _connector(service, data.connector_id)
            if connector["revoked"] or connector["draining"]:
                raise AppError(409, "ASSIST_DEVICE_UNAVAILABLE", "此设备当前无法接受协助")
            existing = service.db.one(
                "SELECT id FROM kaiwu_assist_sessions WHERE connector_id=? AND status IN ('pending','active')",
                (connector["id"],),
            )
            if existing:
                raise AppError(409, "ASSIST_ALREADY_OPEN", "此设备已有一项待处理的协助")
            session_id = "asst_" + uid()
            service.db.execute(
                "INSERT INTO kaiwu_assist_sessions(id,connector_id,tenant_id,name,url,created_by,status,expires_at,created_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?)",
                (session_id, connector["id"], connector["tenant_id"], data.name, data.url, user["id"], _future(SESSION_TTL), now()),
            )
            row = _session(service, session_id, owner=user["id"])
            _event(service, row, "requested", owner=user["id"])
            return _session_view(service, row)

    @router.get("/sessions/{session_id}")
    def status(session_id: str, service=Depends(service_dependency), user=Depends(admin)):
        with service.db.transaction():
            _expire(service)
            return _session_view(service, _session(service, session_id, owner=user["id"]))

    @router.post("/sessions/{session_id}/claim")
    def claim(session_id: str, service=Depends(service_dependency), user=Depends(admin)):
        with service.db.transaction():
            _expire(service)
            row = _session(service, session_id)
            unclaimed = row["created_by"] == DEVICE_OWNER_PREFIX + row["connector_id"]
            if row["created_by"] != user["id"] and not unclaimed:
                raise AppError(404, "ASSIST_SESSION_MISSING", "未找到此协助")
            _require_recent_consent(row)
            if unclaimed:
                service.db.execute(
                    "UPDATE kaiwu_assist_sessions SET created_by=? WHERE id=?", (user["id"], row["id"]),
                )
                _event(service, row, "claimed", owner=user["id"])
            return _session_view(service, row)

    @router.post("/sessions/{session_id}/stop")
    def end(session_id: str, service=Depends(service_dependency), user=Depends(admin)):
        with service.db.transaction():
            _expire(service)
            _stop(service, _session(service, session_id, owner=user["id"]), owner=user["id"])
            return _session_view(service, _session(service, session_id, owner=user["id"]))

    @router.post("/sessions/{session_id}/jobs")
    def queue(session_id: str, data: JobInput, service=Depends(service_dependency), user=Depends(admin)):
        with service.db.transaction():
            _expire(service)
            row = _session(service, session_id, owner=user["id"])
            _require_recent_consent(row)
            _require_target(row)
            latest = _job(service, row["id"])
            if latest and (latest["status"] in {"queued", "running"} or latest["source_revision"] is not None):
                raise AppError(409, "ASSIST_JOB_BUSY", "当前协助已有任务，请查看该任务的结果")
            discovered = json.loads(row["discovered_tools"])
            credentials = data.model_dump(include={"bearer_token", "dynamic_auth"}, exclude_none=True)
            if data.action == "discover":
                if data.tools:
                    raise AppError(422, "ASSIST_JOB_INVALID", "读取工具时无需选择工具")
                service.db.execute("UPDATE kaiwu_assist_sessions SET discovered_tools='[]' WHERE id=?", (row["id"],))
            elif credentials or not data.tools or len(set(data.tools)) != len(data.tools) or not set(data.tools) <= set(discovered):
                raise AppError(422, "ASSIST_JOB_INVALID", "请选择本次读取到的工具")
            job_id = "asjob_" + uid()
            service.db.execute(
                "INSERT INTO kaiwu_assist_jobs(id,session_id,action,status,selected_tools,sealed_bearer,expires_at) "
                "VALUES(?,?,?,'queued',?,?,?)",
                (
                    job_id, row["id"], data.action, dump(data.tools),
                    service.secrets.encrypt(dump(credentials)) if credentials else None, _future(QUEUE_TTL),
                ),
            )
            _event(service, row, f"{data.action}_queued", owner=user["id"], job_id=job_id)
            return _session_view(service, _session(service, session_id, owner=user["id"]))

    @router.post("/sessions/{session_id}/import")
    def import_source(
        session_id: str, data: ImportInput, request: Request, response: Response,
        service=Depends(service_dependency), user=Depends(admin),
    ):
        with service.db.transaction():
            _expire(service)
            row = _session(service, session_id, owner=user["id"])
            if not bridge and row["imported_connection_id"]:
                from .service import connection_view

                existing = service.require_resource("connections", row["imported_connection_id"], user, edit=True)
                return {"connection": connection_view(existing, user)}
            _require_recent_consent(row)
            _require_target(row)
            job = _job(service, row["id"])
            if not job or job["status"] != "succeeded" or job["action"] != "connect":
                raise AppError(409, "ASSIST_NOT_CONNECTED", "请先保存已选择的 MCP 工具")
            source_id = json.loads(job["result"])["source_id"]
            connector = _connector(service, row["connector_id"], row["tenant_id"])
            source = _available_native_source(request, service, connector, source_id)
            if source["revision"] != job["source_revision"]:
                raise AppError(409, "ASSIST_SOURCE_CHANGED", "MCP 服务配置发生变化，请重新发起协助")
            integration = IntegrationCreateRequest(
                tenant_id=row["tenant_id"], connector_id=row["connector_id"],
                display_name=row["name"], source_ids=[source_id],
            )
            if bridge:
                issued = _issue_platform_integration(service, integration, [source], user["id"], assist_session_id=row["id"])
                response.headers["Cache-Control"] = "no-store"
                return {"mcp_config": issued["mcp_config"]}
            from .mcp_import import ImportRequest, plan_mcp_import

            issued = _issue_platform_integration(service, integration, [source], user["id"])
            plan = plan_mcp_import(ImportRequest(config=issued["mcp_config"], scope=data.scope), service, user)
            connection = service.save_connection({**plan.connections[0].create_fields(), "scope": data.scope}, user=user)
            service.db.execute(
                "UPDATE kaiwu_assist_sessions SET imported_connection_id=? WHERE id=?", (connection["id"], row["id"]),
            )
            _event(service, row, "imported", owner=user["id"], job_id=job["id"], source_id=source_id)
            return {"connection": connection}

    return router
