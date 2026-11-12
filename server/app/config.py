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
RUNTIME_TYPES = frozenset({"claude", "harness"})


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AGENT_DATA_DIR", "./data")).resolve())
    # New conversations use Harness; explicit claude remains a rollback option.
    # Existing conversations keep their stored runtime and separate resume ID.
    runtime_type: str = field(default_factory=lambda: os.getenv("AGENT_RUNTIME_TYPE", "harness"))
    # Server-owned launch configuration; never accepted from conversation input.
    harness_node_path: str = field(default_factory=lambda: os.getenv("AGENT_HARNESS_NODE_PATH", "").strip())
    harness_source_dir: str = field(default_factory=lambda: os.getenv("AGENT_HARNESS_SOURCE_DIR", "").strip())
    # Per model response limit; independent of goal budgets and turn limits.
    harness_max_output_tokens: int = field(default_factory=lambda: int(os.getenv("AGENT_HARNESS_MAX_OUTPUT_TOKENS", "8192")))
    # One owned Node process executes a manual compaction, including its
    # auxiliary model request. Timeout closes and reaps that process; it never
    # manufactures a completed summary.
    harness_compaction_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_HARNESS_COMPACTION_TIMEOUT_SECONDS", "600")))
    brand_name: str = field(default_factory=lambda: os.getenv("AGENT_BRAND_NAME", "知行工作台"))
    model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", ""))
    effort: str | None = field(default_factory=lambda: os.getenv("AGENT_EFFORT"))
    legacy_effort: str = field(init=False)
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
    # Public catalog id used for fitness recommendations. Deployments may pin
    # an exact authorized route (for example a 1007 deployment); an unavailable
    # id is reported as evidence gap and never fabricated.
    model_router_model_id: str = field(default_factory=lambda: os.getenv("AGENT_MODEL_ROUTER_MODEL_ID", "deepseek-v4.1-flash-1007"))
    # Tab completion is an independent low-risk route. It must not silently
    # reuse the fitness router target because the two authorizations can differ.
    tab_completion_model_id: str = field(default_factory=lambda: os.getenv("AGENT_TAB_COMPLETION_MODEL_ID", "deepseek-v4.1-flash-1017"))
    # Knowledge model roles are independent from the chat and Tab routes.  An
    # Empty role IDs use only an explicitly capability-declared catalog entry;
    # the server must not infer a visual/entity/relation model from generic
    # chat capability.
    knowledge_embedding_model_id: str = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL_ID", ""))
    knowledge_vision_model_id: str = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_VISION_MODEL_ID", "deepseek-v4-flash-202605"))
    knowledge_entity_model_id: str = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_ENTITY_MODEL_ID", ""))
    knowledge_relation_model_id: str = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_RELATION_MODEL_ID", ""))
    # The verified school-cloud visual adapter is OpenAI Chat Completions
    # compatible. It remains opt-in so indexing cannot unexpectedly incur a
    # paid request; the local proxy has no credential in this process.
    knowledge_vision_base_url: str = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_VISION_BASE_URL", "http://127.0.0.1:3425/v1").rstrip("/"))
    knowledge_vision_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_VISION_ENABLED", "false").lower() == "true")
    knowledge_vision_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_KNOWLEDGE_VISION_TIMEOUT_SECONDS", "30")))
    model_router_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_MODEL_ROUTER_TIMEOUT_SECONDS", "5")))
    model_router_max_output_tokens: int = field(default_factory=lambda: int(os.getenv("AGENT_MODEL_ROUTER_MAX_OUTPUT_TOKENS", "256")))
    model_router_budget_usd: float = field(default_factory=lambda: float(os.getenv("AGENT_MODEL_ROUTER_BUDGET_USD", "0.02")))
    # Image generation is a separately gated capability. A generic chat route
    # is never treated as an image endpoint and no provider is called by
    # default.
    image_generation_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_IMAGE_GENERATION_ENABLED", "false").lower() == "true")
    image_model_id: str = field(default_factory=lambda: os.getenv("AGENT_IMAGE_MODEL_ID", ""))
    image_generator_url: str = field(default_factory=lambda: os.getenv("AGENT_IMAGE_GENERATOR_URL", ""))
    # Only the reviewed OpenAI-images-compatible request/response shape is
    # accepted. The credential is optional because a loopback proxy may own
    # authentication; it is never returned by capability endpoints.
    image_generator_contract: str = field(default_factory=lambda: os.getenv("AGENT_IMAGE_GENERATOR_CONTRACT", "openai-images-v1"))
    image_generator_api_key: str = field(default_factory=lambda: os.getenv("AGENT_IMAGE_GENERATOR_API_KEY", ""))
    image_generation_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_IMAGE_GENERATION_TIMEOUT_SECONDS", "60")))
    image_generation_max_bytes: int = field(default_factory=lambda: int(os.getenv("AGENT_IMAGE_GENERATION_MAX_BYTES", str(10 * 1024 * 1024))))
    # The Feishu bridge is deliberately a separate process.  The API server
    # only accepts its loopback requests when a dedicated bridge token is
    # configured; no Feishu credential is ever returned by this object.
    feishu_bridge_token: str = field(default_factory=lambda: os.getenv("AGENT_FEISHU_BRIDGE_TOKEN", ""))
    feishu_bridge_token_file: str = field(default_factory=lambda: os.getenv("AGENT_FEISHU_BRIDGE_TOKEN_FILE", ""))
    feishu_user_id: str = field(default_factory=lambda: os.getenv("AGENT_FEISHU_USER_ID", ""))
    feishu_username: str = field(default_factory=lambda: os.getenv("AGENT_FEISHU_USERNAME", ""))
    feishu_assistant_id: str = field(default_factory=lambda: os.getenv("AGENT_FEISHU_ASSISTANT_ID", ""))
    feishu_allowed_open_ids: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip() for x in os.getenv("AGENT_FEISHU_ALLOWED_OPEN_IDS", "").split(",") if x.strip()))
    feishu_allowed_chat_ids: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip() for x in os.getenv("AGENT_FEISHU_ALLOWED_CHAT_IDS", "").split(",") if x.strip()))
    feishu_allow_group_chats: bool = field(default_factory=lambda: os.getenv("AGENT_FEISHU_ALLOW_GROUP_CHATS", "false").lower() == "true")
    # The Feishu bridge and the H5 web application are separate custom apps.
    # Keep their credentials in separate fields so enabling the workbench
    # cannot silently change or reuse the bot identity.
    feishu_h5_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_FEISHU_H5_ENABLED", "false").lower() == "true")
    feishu_app_id: str = field(default_factory=lambda: os.getenv("FEISHU_APP_ID", "").strip())
    feishu_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_APP_SECRET", "").strip())
    feishu_h5_app_id: str = field(default_factory=lambda: os.getenv("FEISHU_H5_APP_ID", "").strip())
    feishu_h5_app_secret: str = field(default_factory=lambda: os.getenv("FEISHU_H5_APP_SECRET", "").strip())
    feishu_api_base: str = field(default_factory=lambda: os.getenv("FEISHU_API_BASE_URL", "https://open.feishu.cn").rstrip("/"))
    feishu_h5_allowed_open_ids: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip() for x in os.getenv("AGENT_FEISHU_H5_ALLOWED_OPEN_IDS", os.getenv("AGENT_FEISHU_ALLOWED_OPEN_IDS", "")).split(",") if x.strip()))
    feishu_h5_user_map: str = field(default_factory=lambda: os.getenv("AGENT_FEISHU_H5_USER_MAP", "").strip())

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_type, str) or self.runtime_type.strip().lower() not in RUNTIME_TYPES:
            raise ValueError("AGENT_RUNTIME_TYPE must be claude or harness")
        self.runtime_type = self.runtime_type.strip().lower()
        # Pre-catalog Claude sessions used medium when no effort was configured.
        self.legacy_effort = self.effort if self.effort is not None else "medium"
        if self.effort is None:
            self.effort = "high" if self.runtime_type == "harness" else "medium"
        if type(self.harness_max_output_tokens) is not int or not 1 <= self.harness_max_output_tokens <= 131072:
            raise ValueError("AGENT_HARNESS_MAX_OUTPUT_TOKENS must be an integer from 1 to 131072")
        if type(self.harness_compaction_timeout_seconds) is not int or not 1 <= self.harness_compaction_timeout_seconds <= 3600:
            raise ValueError("AGENT_HARNESS_COMPACTION_TIMEOUT_SECONDS must be an integer from 1 to 3600")
        if not self.model:
            self.model = self.provider_env().get("ANTHROPIC_MODEL") or DEFAULT_MODEL

    def prepare(self) -> None:
        for sub in ("", "files", "workspaces", "sdk-config", "logs"):
            path = self.data_dir / sub
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def feishu_token(self) -> str:
        """Return the bridge token without placing a file secret in logs."""
        if self.feishu_bridge_token_file:
            value = Path(self.feishu_bridge_token_file).read_text(encoding="utf-8").strip()
            if value:
                return value
        return self.feishu_bridge_token.strip()

    @property
    def feishu_h5_ready(self) -> bool:
        """Whether the public client may start the H5 login handshake."""
        return bool(self.feishu_h5_enabled and self.feishu_h5_app_id and self.feishu_h5_app_secret)

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
