"""Protected public MCP Edge for the Kaiwu enterprise bridge.

The Edge authenticates the one-time enterprise integration credential K,
binds it to an approved source, creates an Edge-owned MCP session, and streams
the request through the loopback visitor into the dedicated FRP tunnel. It
never sees or adds the customer's internal bearer or HMAC material.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings
from .db import Database
from .security import AppError, token_hash


MAX_EDGE_REQUEST_BYTES = 4 << 20
MAX_EDGE_RESPONSE_BYTES = 8 << 20
MAX_EDGE_SESSIONS = 2048
SOURCE_ID_LENGTH = len("src_") + 32


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers={"Cache-Control": "no-store"},
    )


def _integration_token(request: Request) -> str:
    supplied = request.headers.get("authorization", "")
    scheme, _, token = supplied.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        return ""
    return token.strip()


def _expired(raw: str) -> bool:
    try:
        value = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value <= datetime.now(timezone.utc)


@dataclass
class EdgeSession:
    integration_id: str
    connector_id: str
    source_id: str
    upstream_id: str
    expires_at: float


class EdgeSessions:
    def __init__(self, ttl_seconds: int):
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, EdgeSession] = {}

    def _purge(self, now_value: float | None = None) -> None:
        current = now_value or time.monotonic()
        expired = [key for key, value in self._items.items() if value.expires_at <= current]
        for key in expired:
            self._items.pop(key, None)

    def resolve(self, session_id: str, integration_id: str, connector_id: str, source_id: str) -> str:
        if not session_id:
            return ""
        with self._lock:
            self._purge()
            item = self._items.get(session_id)
            if (
                not item
                or item.integration_id != integration_id
                or item.connector_id != connector_id
                or item.source_id != source_id
            ):
                raise AppError(404, "MCP_SESSION_INVALID", "MCP 会话不存在或已过期")
            item.expires_at = time.monotonic() + self.ttl_seconds
            return item.upstream_id

    def bind(
        self,
        existing_id: str,
        integration_id: str,
        connector_id: str,
        source_id: str,
        upstream_id: str,
    ) -> str:
        if not upstream_id:
            return existing_id
        with self._lock:
            self._purge()
            session_id = existing_id if existing_id in self._items else ""
            if not session_id:
                session_id = "edge_" + secrets.token_urlsafe(32)
            if len(self._items) >= MAX_EDGE_SESSIONS and session_id not in self._items:
                oldest = min(self._items, key=lambda key: self._items[key].expires_at)
                self._items.pop(oldest, None)
            self._items[session_id] = EdgeSession(
                integration_id=integration_id,
                connector_id=connector_id,
                source_id=source_id,
                upstream_id=upstream_id,
                expires_at=time.monotonic() + self.ttl_seconds,
            )
            return session_id


def _valid_source_id(value: str) -> bool:
    if len(value) != SOURCE_ID_LENGTH or not value.startswith("src_"):
        return False
    try:
        int(value[4:], 16)
    except ValueError:
        return False
    return True


def _protocol_header(value: str) -> bool:
    return bool(value) and len(value) <= 32 and all(char.isdigit() or char == "-" for char in value)


def _authenticated_integration(
    db: Database,
    request: Request,
    source_id: str,
) -> tuple[dict, dict, dict, str]:
    raw_token = _integration_token(request)
    if not raw_token:
        raise AppError(401, "INTEGRATION_UNAUTHENTICATED", "企业集成凭据无效")
    digest = token_hash(raw_token)
    row = db.one(
        "SELECT i.*, c.revoked AS connector_revoked, c.draining AS connector_draining "
        "FROM kaiwu_integrations i "
        "JOIN kaiwu_connectors c ON c.id=i.connector_id "
        "WHERE i.token_hash=?",
        (digest,),
    )
    if (
        not row
        or not hmac.compare_digest(str(row["token_hash"]), digest)
        or row["revoked_at"]
        or row["connector_revoked"]
        or _expired(row["expires_at"])
    ):
        raise AppError(401, "INTEGRATION_UNAUTHENTICATED", "企业集成凭据无效")
    source = db.one(
        "SELECT s.*, r.visitor_host, r.visitor_port "
        "FROM kaiwu_integration_sources m "
        "JOIN kaiwu_sources s ON s.connector_id=m.connector_id AND s.source_id=m.source_id "
        "LEFT JOIN kaiwu_edge_routes r "
        "ON r.connector_id=s.connector_id AND r.source_id=s.source_id "
        "WHERE m.integration_id=? AND m.connector_id=? AND m.source_id=?",
        (row["id"], row["connector_id"], source_id),
    )
    if not source:
        raise AppError(403, "SOURCE_ACCESS_DENIED", "当前企业集成无权访问该来源")
    if row["connector_draining"]:
        raise AppError(503, "SOURCE_UNAVAILABLE", "来源暂不可用")
    if not source["visitor_host"] or not source["visitor_port"]:
        raise AppError(503, "SOURCE_UNAVAILABLE", "来源的中继路由尚未就绪")
    return row, source, {}, raw_token


def _integration_still_active(db: Database, integration_id: str) -> bool:
    row = db.one(
        "SELECT i.expires_at,i.revoked_at,c.revoked "
        "FROM kaiwu_integrations i JOIN kaiwu_connectors c ON c.id=i.connector_id "
        "WHERE i.id=?",
        (integration_id,),
    )
    return bool(row and not row["revoked_at"] and not row["revoked"] and not _expired(row["expires_at"]))


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise AppError(502, "EDGE_RESPONSE_TOO_LARGE", "来源响应超过大小限制")
        chunks.append(chunk)
    return b"".join(chunks)


def create_kaiwu_edge_router(service_getter: Callable, settings: Settings) -> APIRouter:
    prefix = settings.kaiwu_edge_mount_path.rstrip("/")
    router = APIRouter(prefix=prefix, tags=["kaiwu-edge"])
    sessions = EdgeSessions(settings.kaiwu_edge_session_ttl_seconds)

    def service(request: Request):
        return service_getter(request)

    @router.api_route("/sources/{source_id}/mcp", methods=["POST", "GET", "DELETE", "PUT", "PATCH"])
    async def edge(
        source_id: str,
        request: Request,
        service_obj=Depends(service),
    ):
        if request.method != "POST":
            return JSONResponse(
                status_code=405,
                content={"error": {"code": "METHOD_NOT_ALLOWED", "message": "当前 MCP 来源只接受 POST"}},
                headers={"Allow": "POST", "Cache-Control": "no-store"},
            )
        if not _valid_source_id(source_id):
            return _error(404, "SOURCE_NOT_FOUND", "来源路径不存在")
        try:
            integration, source, _, _ = _authenticated_integration(
                service_obj.db, request, source_id
            )
            if source["visitor_host"] != settings.kaiwu_edge_visitor_host:
                return _error(503, "SOURCE_UNAVAILABLE", "来源的中继路由未通过本机策略校验")
            incoming_session = request.headers.get("mcp-session-id", "").strip()
            upstream_session = sessions.resolve(
                incoming_session,
                integration["id"],
                integration["connector_id"],
                source_id,
            )
            body = await request.body()
            if len(body) > MAX_EDGE_REQUEST_BYTES:
                return _error(413, "EDGE_REQUEST_TOO_LARGE", "MCP 请求超过大小限制")
            if not body:
                return _error(400, "MCP_INVALID", "MCP 请求不能为空")
            content_type = request.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                return _error(415, "CONTENT_TYPE_INVALID", "只支持 application/json")

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "User-Agent": "kaiwu-mcp-edge/1",
            }
            protocol = request.headers.get("mcp-protocol-version", "").strip()
            if _protocol_header(protocol):
                headers["MCP-Protocol-Version"] = protocol
            if upstream_session:
                headers["Mcp-Session-Id"] = upstream_session

            target = f"http://{source['visitor_host']}:{int(source['visitor_port'])}/mcp"
            timeout = httpx.Timeout(
                settings.kaiwu_edge_request_timeout_seconds,
                connect=min(10.0, settings.kaiwu_edge_request_timeout_seconds),
            )
            client = httpx.AsyncClient(
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
            )
            try:
                outgoing = client.build_request("POST", target, content=body, headers=headers)
                upstream = await client.send(outgoing, stream=True)
            except httpx.TimeoutException:
                await client.aclose()
                return _error(504, "UPSTREAM_TIMEOUT", "来源响应超时")
            except httpx.RequestError:
                await client.aclose()
                return _error(503, "SOURCE_UNAVAILABLE", "来源暂不可用")

            if 300 <= upstream.status_code < 400:
                await upstream.aclose()
                await client.aclose()
                return _error(502, "UPSTREAM_REDIRECT_DENIED", "来源返回了不允许的重定向")

            upstream_id = upstream.headers.get("mcp-session-id", "").strip()
            edge_session = sessions.bind(
                incoming_session,
                integration["id"],
                integration["connector_id"],
                source_id,
                upstream_id,
            )
            upstream_type = upstream.headers.get("content-type", "application/json")
            response_headers = {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            }
            if edge_session:
                response_headers["Mcp-Session-Id"] = edge_session

            if "text/event-stream" in upstream_type.lower():
                async def stream():
                    checked_at = 0.0
                    try:
                        async for chunk in upstream.aiter_raw():
                            now_value = time.monotonic()
                            if now_value - checked_at >= 1.0:
                                checked_at = now_value
                                if not _integration_still_active(service_obj.db, integration["id"]):
                                    return
                            if chunk:
                                yield chunk
                    finally:
                        await upstream.aclose()
                        await client.aclose()

                response_headers["Content-Type"] = upstream_type
                return StreamingResponse(
                    stream(),
                    status_code=upstream.status_code,
                    headers=response_headers,
                )

            try:
                payload = await _read_bounded(upstream, MAX_EDGE_RESPONSE_BYTES)
            except AppError as exc:
                return _error(exc.status, exc.code, exc.message)
            finally:
                await upstream.aclose()
                await client.aclose()
            response_headers["Content-Type"] = upstream_type
            return Response(
                content=payload,
                status_code=upstream.status_code,
                headers=response_headers,
            )
        except AppError as exc:
            return _error(exc.status, exc.code, exc.message)

    return router
