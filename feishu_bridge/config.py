from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BridgeConfig:
    app_id: str
    app_secret: str
    api_base: str
    bridge_token: str
    feishu_base: str = "https://open.feishu.cn"
    poll_seconds: float = 1.0
    progress_seconds: float = 8.0
    max_message_chars: int = 3500
    allow_group_chats: bool = False

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        token = os.getenv("AGENT_FEISHU_BRIDGE_TOKEN", "")
        token_file = os.getenv("AGENT_FEISHU_BRIDGE_TOKEN_FILE", "")
        if token_file:
            with open(token_file, encoding="utf-8") as handle:
                token = handle.read().strip()
        return cls(
            app_id=os.getenv("FEISHU_APP_ID", "").strip(),
            app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
            api_base=os.getenv("AGENT_API_BASE", "http://127.0.0.1:8000").rstrip("/"),
            bridge_token=token.strip(),
            feishu_base=os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/"),
            poll_seconds=max(0.25, float(os.getenv("FEISHU_POLL_SECONDS", "1"))),
            progress_seconds=max(2.0, float(os.getenv("FEISHU_PROGRESS_SECONDS", "8"))),
            max_message_chars=max(500, min(6000, int(os.getenv("FEISHU_MAX_MESSAGE_CHARS", "3500")))),
            allow_group_chats=_bool("FEISHU_ALLOW_GROUP_CHATS"),
        )

    def validate(self) -> None:
        missing = [name for name, value in (("FEISHU_APP_ID", self.app_id), ("FEISHU_APP_SECRET", self.app_secret), ("AGENT_FEISHU_BRIDGE_TOKEN", self.bridge_token)) if not value]
        if missing:
            raise ValueError("missing required bridge settings: " + ", ".join(missing))
