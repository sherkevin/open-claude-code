from __future__ import annotations

import json
import threading
import time
from typing import Iterable

import httpx

from .config import BridgeConfig


class FeishuApiError(RuntimeError):
    pass


class FeishuTransport:
    """Small raw HTTP client for outbound bot messages.

    The WebSocket event transport is provided by ``lark-oapi``.  Outbound
    calls use the documented HTTP endpoints directly so message delivery is
    easy to test with a fake HTTP transport and does not depend on SDK model
    builder details.
    """

    def __init__(self, config: BridgeConfig, client: httpx.Client | None = None):
        self.config = config
        self.http = client or httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
        self._token = ""
        self._token_expiry = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        self.http.close()

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expiry:
                return self._token
            response = self.http.post(
                self.config.feishu_base + "/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("code", 0) != 0 or not body.get("tenant_access_token"):
                raise FeishuApiError("Feishu token request failed")
            self._token = str(body["tenant_access_token"])
            self._token_expiry = time.time() + max(60, int(body.get("expire", 7200)) - 120)
            return self._token

    def _request(self, method: str, path: str, payload: dict) -> dict:
        token = self._access_token()
        response = self.http.request(method, self.config.feishu_base + path, headers={"Authorization": f"Bearer {token}"}, json=payload)
        if response.status_code == 401:
            with self._lock:
                self._token = ""
                self._token_expiry = 0
            token = self._access_token()
            response = self.http.request(method, self.config.feishu_base + path, headers={"Authorization": f"Bearer {token}"}, json=payload)
        response.raise_for_status()
        body = response.json()
        if body.get("code", 0) != 0:
            raise FeishuApiError(f"Feishu API failed with code {body.get('code')}")
        return body

    @staticmethod
    def _chunks(text: str, limit: int) -> Iterable[str]:
        text = text or "(空消息)"
        for start in range(0, len(text), limit):
            yield text[start:start + limit]

    def reply(self, message_id: str, text: str) -> None:
        chunks = list(self._chunks(text, self.config.max_message_chars))
        first = chunks.pop(0)
        self._request("POST", f"/open-apis/im/v1/messages/{message_id}/reply", {"msg_type": "text", "content": json.dumps({"text": first}, ensure_ascii=False)})
        # A reply is threaded to the original message.  Additional chunks are
        # sent to the chat by the caller when it has the chat id; this method
        # intentionally sends only the first chunk to keep the API contract
        # explicit.

    def send_chat(self, chat_id: str, text: str) -> None:
        for chunk in self._chunks(text, self.config.max_message_chars):
            self._request("POST", "/open-apis/im/v1/messages?receive_id_type=chat_id", {"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": chunk}, ensure_ascii=False)})
