from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# No hooks, permissions, proxy configuration, MCP commands or arbitrary env imports.
PROVIDER_KEYS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "API_TIMEOUT_MS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}
DEFAULT_MODEL = "deepseek-v4.1-flash-1017"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AGENT_DATA_DIR", "./data")).resolve())
    brand_name: str = field(default_factory=lambda: os.getenv("AGENT_BRAND_NAME", "知行工作台"))
    model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", ""))
    effort: str = field(default_factory=lambda: os.getenv("AGENT_EFFORT", "medium"))
    provider_settings: str = field(default_factory=lambda: os.getenv("AGENT_PROVIDER_SETTINGS", ""))
    model_catalog: str = field(default_factory=lambda: os.getenv("AGENT_MODEL_CATALOG", ""))
    default_model_id: str = field(default_factory=lambda: os.getenv("AGENT_DEFAULT_MODEL_ID", ""))
    origins: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip().rstrip("/") for x in os.getenv("AGENT_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if x.strip()))
    mcp_hosts: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip().lower() for x in os.getenv("AGENT_MCP_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if x.strip()))
    cookie_secure: bool = field(default_factory=lambda: os.getenv("AGENT_COOKIE_SECURE", "false").lower() == "true")
    upload_max_bytes: int = 20 * 1024 * 1024
    session_hours: int = 24
    idle_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_IDLE_SECONDS", "600")))
    approval_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_APPROVAL_SECONDS", "600")))
    max_turns: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_TURNS", "30")))
    max_budget_usd: float = field(default_factory=lambda: float(os.getenv("AGENT_MAX_BUDGET_USD", "5")))
    max_active_sessions: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_ACTIVE_SESSIONS", "4")))
    provider_cost_reliable: bool = field(default_factory=lambda: os.getenv("AGENT_PROVIDER_COST_RELIABLE", "false").lower() == "true")
    # The fitness router is opt-in. Disabled deployments never make an extra
    # provider call and use the bounded local scorer instead.
    model_router_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_MODEL_ROUTER_ENABLED", "false").lower() == "true")
    model_router_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_MODEL_ROUTER_TIMEOUT_SECONDS", "5")))
    model_router_max_output_tokens: int = field(default_factory=lambda: int(os.getenv("AGENT_MODEL_ROUTER_MAX_OUTPUT_TOKENS", "256")))
    model_router_budget_usd: float = field(default_factory=lambda: float(os.getenv("AGENT_MODEL_ROUTER_BUDGET_USD", "0.02")))

    def __post_init__(self) -> None:
        if not self.model:
            self.model = self.provider_env().get("ANTHROPIC_MODEL") or DEFAULT_MODEL

    def prepare(self) -> None:
        for sub in ("", "files", "workspaces", "sdk-config", "logs"):
            path = self.data_dir / sub
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def provider_env(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if self.provider_settings:
            source = Path(self.provider_settings)
            document = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or not isinstance(document.get("env", {}), dict):
                raise ValueError("Provider settings must contain an env object")
            if isinstance(document.get("model"), str) and document["model"]:
                result["ANTHROPIC_MODEL"] = document["model"]
            for key, value in document.get("env", {}).items():
                if key in PROVIDER_KEYS and isinstance(value, (str, int)):
                    result[key] = str(value)
        for key in PROVIDER_KEYS:
            if key in os.environ:
                result[key] = os.environ[key]
        return result
