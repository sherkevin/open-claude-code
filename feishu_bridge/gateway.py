from __future__ import annotations

import time
import uuid

import httpx

from .config import BridgeConfig
from .parser import IncomingMessage


class GatewayError(RuntimeError):
    pass


class AgentGateway:
    def __init__(self, config: BridgeConfig, client: httpx.Client | None = None):
        self.config = config
        self.http = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=3.0), follow_redirects=False)

    def close(self) -> None:
        self.http.close()

    def _request(self, method: str, path: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["X-Agent-Feishu-Bridge-Token"] = self.config.bridge_token
        response = self.http.request(method, self.config.api_base + path, headers=headers, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", "gateway request failed")
            except Exception:
                detail = "gateway request failed"
            raise GatewayError(f"{response.status_code}: {detail}")
        return response.json()

    def submit(self, message: IncomingMessage) -> dict:
        return self._request("POST", "/internal/feishu/messages", json={
            "event_id": message.event_id,
            "message_id": message.message_id,
            "chat_id": message.chat_id,
            "chat_type": message.chat_type,
            "open_id": message.open_id,
            "text": message.text,
        })

    def run_events(self, run_id: str, after: int = 0) -> dict:
        return self._request("GET", f"/internal/feishu/runs/{run_id}/events", params={"after": max(0, after)})

    def status(self) -> list[dict]:
        return self._request("GET", "/internal/feishu/status")

    def cancel(self, run_id: str) -> dict:
        return self._request("POST", f"/internal/feishu/runs/{run_id}/cancel", json={})

    def decide(self, approval_id: str, decision: str) -> dict:
        return self._request("POST", f"/internal/feishu/approvals/{approval_id}/decision", json={"decision": decision})


def request_id() -> str:
    return f"feishu-{int(time.time())}-{uuid.uuid4().hex}"
