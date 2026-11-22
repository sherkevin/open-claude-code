"""A bounded, authenticated outbound transport for registered MCP sources."""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import re
import secrets
from collections import deque

import anyio
import httpx
from starlette.websockets import WebSocket, WebSocketDisconnect

from .security import AppError


NATIVE_RELAY_PATH = "/api/kaiwu/v1/connectors/{connector_id}/native-relay"
MAX_NATIVE_IN_FLIGHT = 20
MAX_NATIVE_CHUNK = 16 << 10
MAX_NATIVE_MESSAGE = 24 << 10
MAX_NATIVE_BUFFER_CHUNKS = 32
MAX_NATIVE_RESPONSE_BYTES = 8 << 20
MAX_NATIVE_SEND_SECONDS = 5.0
ID_RE = re.compile(r"[0-9a-f]{32}")
RESPONSE_HEADERS = {"content-type", "mcp-session-id"}
REQUEST_HEADERS = {"content-type", "accept", "mcp-protocol-version", "mcp-session-id", "x-kaiwu-trace-id"}


def is_native_connector(db, connector_id: str) -> bool:
    return db.one(
        "SELECT 1 FROM kaiwu_native_relay_devices WHERE connector_id=?",
        (connector_id,),
    ) is not None


class RelayProtocolError(Exception):
    """A peer sent an invalid native-relay message."""


def _response_headers(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > len(RESPONSE_HEADERS):
        raise RelayProtocolError
    headers = {}
    for key, text in value.items():
        name = key.lower()
        if (
            name not in RESPONSE_HEADERS
            or name in headers
            or not isinstance(text, str)
            or len(text) > 4096
            or any(ord(char) < 32 or ord(char) > 126 for char in text)
        ):
            raise RelayProtocolError
        if name == "content-type" and text.split(";", 1)[0].strip().lower() not in {
            "application/json", "application/problem+json", "text/event-stream",
        }:
            raise RelayProtocolError
        headers[name] = text
    return headers


class RelayExchange:
    def __init__(self, timeout: float):
        self.deadline = asyncio.get_running_loop().time() + timeout
        self.headers: tuple[int, dict[str, str]] | None = None
        self.chunks: deque[bytes] = deque()
        self.changed = asyncio.Event()
        self.complete = False
        self.failure: httpx.RequestError | None = None
        self.size = 0

    def fail(self, error: httpx.RequestError) -> None:
        self.failure = error
        self.chunks.clear()
        self.changed.set()

    def handle(self, frame: dict) -> None:
        kind = frame["type"]
        if self.complete or self.failure:
            return
        if kind == "response":
            status = frame.get("status")
            if self.headers is not None or type(status) is not int or not 200 <= status <= 599:
                raise RelayProtocolError
            self.headers = (status, _response_headers(frame.get("headers", {})))
        elif kind == "chunk":
            if self.headers is None or not isinstance(frame.get("body"), str):
                raise RelayProtocolError
            try:
                chunk = base64.b64decode(frame["body"], validate=True)
            except (ValueError, binascii.Error) as exc:
                raise RelayProtocolError from exc
            if not chunk or len(chunk) > MAX_NATIVE_CHUNK:
                raise RelayProtocolError
            self.size += len(chunk)
            if self.size > MAX_NATIVE_RESPONSE_BYTES or len(self.chunks) >= MAX_NATIVE_BUFFER_CHUNKS:
                self.fail(httpx.RemoteProtocolError("native relay response limit exceeded"))
                return
            self.chunks.append(chunk)
        elif kind == "end":
            if self.headers is None:
                raise RelayProtocolError
            self.complete = True
        elif kind == "error":
            self.complete = True
            self.fail(httpx.RemoteProtocolError("native relay request failed"))
            return
        self.changed.set()

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self.changed.wait(), self._remaining())
        except asyncio.TimeoutError:
            raise httpx.ReadTimeout("native relay response timed out") from None
        self.changed.clear()

    def _remaining(self) -> float:
        remaining = self.deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise httpx.ReadTimeout("native relay response timed out")
        return remaining

    async def wait_headers(self) -> tuple[int, dict[str, str]]:
        while self.headers is None and self.failure is None:
            await self._wait()
        if self.failure:
            raise self.failure
        self._remaining()
        return self.headers

    async def read(self):
        while True:
            if self.failure:
                raise self.failure
            if self.complete and not self.chunks:
                return
            if self.chunks:
                self._remaining()
                yield self.chunks.popleft()
            else:
                await self._wait()


class RelayResponseBody(httpx.AsyncByteStream):
    def __init__(self, peer: NativeRelayPeer, exchange_id: str, exchange: RelayExchange):
        self.peer = peer
        self.exchange_id = exchange_id
        self.exchange = exchange

    async def __aiter__(self):
        async for chunk in self.exchange.read():
            yield chunk

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            await self.peer.release(self.exchange_id)


class NativeRelayPeer:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket
        self.exchanges: dict[str, RelayExchange] = {}
        self.send_lock = asyncio.Lock()
        self.ready = False
        self.closed = False

    async def _send(self, frame: dict) -> None:
        async with self.send_lock:
            if self.closed:
                raise httpx.ConnectError("native relay is disconnected")
            await self.websocket.send_json(frame)

    async def send(self, frame: dict) -> None:
        try:
            await asyncio.wait_for(self._send(frame), MAX_NATIVE_SEND_SECONDS)
        except asyncio.TimeoutError:
            self.disconnect()
            await self.close_socket(1011)
            raise httpx.WriteTimeout("native relay write timed out") from None
        except (OSError, RuntimeError, WebSocketDisconnect):
            self.disconnect()
            raise httpx.ConnectError("native relay is disconnected") from None

    async def forward(
        self, source_id: str, headers: dict[str, str], body: bytes, timeout: float,
    ) -> httpx.Response:
        if not self.ready or self.closed:
            raise httpx.ConnectError("native relay is disconnected")
        if len(self.exchanges) >= MAX_NATIVE_IN_FLIGHT:
            raise AppError(429, "SOURCE_BUSY", "连接器同时处理的调用过多，请稍后重试")
        exchange_id = secrets.token_hex(16)
        exchange = self.exchanges[exchange_id] = RelayExchange(timeout)
        try:
            await self.send({
                "type": "request",
                "id": exchange_id,
                "source_id": source_id,
                "headers": {name: value for name, value in headers.items() if name.lower() in REQUEST_HEADERS},
                "body": base64.b64encode(body).decode("ascii"),
            })
            status, response_headers = await exchange.wait_headers()
        except (asyncio.CancelledError, Exception):
            with anyio.CancelScope(shield=True):
                await self.release(exchange_id)
            raise
        return httpx.Response(
            status,
            headers=response_headers,
            stream=RelayResponseBody(self, exchange_id, exchange),
        )

    async def release(self, exchange_id: str) -> None:
        exchange = self.exchanges.pop(exchange_id, None)
        if exchange is None or exchange.complete or self.closed:
            return
        try:
            await self.send({"type": "cancel", "id": exchange_id})
        except httpx.RequestError:
            pass

    async def receive(self) -> None:
        while not self.closed:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("text")
            if raw is None or len(raw) > MAX_NATIVE_MESSAGE:
                raise RelayProtocolError
            try:
                frame = json.loads(raw)
            except (ValueError, RecursionError) as exc:
                raise RelayProtocolError from exc
            if (
                not isinstance(frame, dict)
                or not isinstance(frame.get("type"), str)
                or frame["type"] not in {"response", "chunk", "end", "error"}
                or not isinstance(frame.get("id"), str)
                or not ID_RE.fullmatch(frame["id"])
            ):
                raise RelayProtocolError
            exchange = self.exchanges.get(frame["id"])
            if exchange is not None:
                exchange.handle(frame)
                # Drain ready streams even while the websocket's receive
                # buffer is full. A genuinely slow reader remains bounded.
                if frame["type"] == "chunk":
                    await asyncio.sleep(0)

    def disconnect(self) -> None:
        self.closed = True
        for exchange in self.exchanges.values():
            if not exchange.complete:
                exchange.fail(httpx.ConnectError("native relay disconnected before the response completed"))

    async def _close_socket(self, code: int) -> None:
        async with self.send_lock:
            await self.websocket.close(code)

    async def close_socket(self, code: int) -> None:
        try:
            await asyncio.wait_for(self._close_socket(code), MAX_NATIVE_SEND_SECONDS)
        except (asyncio.TimeoutError, OSError, RuntimeError, WebSocketDisconnect):
            pass


class NativeRelayRegistry:
    """Per-process socket owners, shared by the control plane and public Edge."""

    def __init__(self):
        self.peers: dict[str, NativeRelayPeer] = {}

    def is_online(self, connector_id: str) -> bool:
        peer = self.peers.get(connector_id)
        return bool(peer and peer.ready and not peer.closed)

    async def forward(
        self, connector_id: str, source_id: str, headers: dict[str, str], body: bytes, timeout: float,
    ) -> httpx.Response:
        peer = self.peers.get(connector_id)
        if peer is None:
            raise httpx.ConnectError("native relay is disconnected")
        return await peer.forward(source_id, headers, body, timeout)

    async def disconnect(self, connector_id: str, code: int = 1008) -> None:
        peer = self.peers.pop(connector_id, None)
        if peer is not None:
            peer.disconnect()
            await peer.close_socket(code)

    async def register(self, connector_id: str, peer: NativeRelayPeer) -> None:
        previous = self.peers.get(connector_id)
        self.peers[connector_id] = peer
        if previous is not None:
            previous.disconnect()
            await previous.close_socket(1012)

    def remove(self, connector_id: str, peer: NativeRelayPeer) -> None:
        peer.disconnect()
        if self.peers.get(connector_id) is peer:
            self.peers.pop(connector_id)


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


async def connect_native_relay(websocket: WebSocket) -> None:
    from .kaiwu_control import verify_device_request

    app = websocket.app
    service = app.state.service
    connector_id: str = websocket.path_params["connector_id"]
    if (
        not service.settings.kaiwu_native_relay_enabled
        or app.state.deployment_policy.is_trial
        or "origin" in websocket.headers
        or websocket.url.query
        or (
            websocket.url.scheme != "wss"
            and not (_loopback(websocket.url.hostname) and websocket.client and _loopback(websocket.client.host))
        )
    ):
        await websocket.close(1008)
        return
    try:
        connector = verify_device_request(
            service, websocket.headers, "GET", websocket.url.path, b"",
        )["connector"]
        if connector["id"] != connector_id or connector["draining"]:
            raise AppError(403, "DEVICE_SCOPE_DENIED", "设备无权访问该连接器")
    except AppError:
        await websocket.close(1008)
        return
    await websocket.accept()
    try:
        with service.db.transaction():
            current = service.db.one("SELECT revoked,draining FROM kaiwu_connectors WHERE id=?", (connector_id,))
            if current["revoked"] or current["draining"]:
                raise AppError(403, "DEVICE_REVOKED", "设备无法接入中继")
            service.db.execute(
                "INSERT OR IGNORE INTO kaiwu_native_relay_devices(connector_id) VALUES(?)",
                (connector_id,),
            )
    except AppError:
        await websocket.close(1008)
        return
    registry = app.state.kaiwu_native_relay
    peer = NativeRelayPeer(websocket)
    try:
        await registry.register(connector_id, peer)
        await peer.send({"type": "ready"})
        peer.ready = True
        await peer.receive()
    except RelayProtocolError:
        await peer.close_socket(1002)
    except (httpx.RequestError, WebSocketDisconnect):
        pass
    finally:
        registry.remove(connector_id, peer)
