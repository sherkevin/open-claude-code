from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .deployment_policy import CAPABILITIES, DEFAULT_TRIAL_CAPABILITIES, DeploymentPolicy, valid_deployment_mode


# No hooks, permissions, proxy configuration, MCP commands or arbitrary env imports.
PROVIDER_KEYS = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "API_TIMEOUT_MS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
}
DEFAULT_MODEL = "deepseek-v4.1-flash-1017"
DEFAULT_PROMPT_SAFETY_MODEL = "Qwen/Qwen3Guard-Gen-0.6B"
RUNTIME_TYPES = frozenset({"claude", "harness"})


def _trial_capabilities_from_env() -> tuple[str, ...] | None:
    """Explicit trial capability selection; unset means the documented default."""
    raw = os.getenv("AGENT_TRIAL_CAPABILITIES")
    if raw is None:
        return None
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AGENT_DATA_DIR", "./data")).resolve())
    # New conversations use Harness; explicit claude remains a rollback option.
    # Existing conversations keep their stored runtime and separate resume ID.
    runtime_type: str = field(default_factory=lambda: os.getenv("AGENT_RUNTIME_TYPE", "harness"))
    # Deployment-level capability boundary.  ``normal`` keeps every existing
    # feature; ``trial`` is an explicit allowlist and refuses unknown values at
    # startup instead of silently widening access.
    deployment_mode: str = field(default_factory=lambda: os.getenv("AGENT_DEPLOYMENT_MODE", "normal"))
    trial_capabilities: tuple[str, ...] | None = field(default_factory=_trial_capabilities_from_env)
    # The low-code workflow surface is retired from the product UI. Keep its
    # schema and implementation for migration compatibility, but expose the
    # API only when an operator explicitly enables the legacy surface.
    workflows_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_WORKFLOWS_ENABLED", "false").lower() == "true")
    # Server-owned launch configuration; never accepted from conversation input.
    harness_node_path: str = field(default_factory=lambda: os.getenv("AGENT_HARNESS_NODE_PATH", "").strip())
    harness_source_dir: str = field(default_factory=lambda: os.getenv("AGENT_HARNESS_SOURCE_DIR", "").strip())
    # State-only native Goal pilot. The autonomous driver is not mounted.
    harness_goal_state_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_HARNESS_GOAL_STATE_ENABLED", "false").lower() == "true")
    # Owner-only native Team projection; no providers or model tools mounted.
    harness_team_state_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_HARNESS_TEAM_STATE_ENABLED", "false").lower() == "true")
    # Separate, default-off task lifecycle (enable/create/claim/complete) on the
    # official Team board. It is metadata-only: no model turn, subagent spawn,
    # shell, provider or business write is mounted by this switch.
    harness_team_tasks_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_HARNESS_TEAM_TASKS_ENABLED", "false").lower() == "true")
    # Separate, default-off execution admission. Even when both switches are
    # on, this host only records an owner's explicit budget approval and a
    # durable round lease; it never starts a model round by itself.
    harness_goal_execution_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_HARNESS_GOAL_EXECUTION_ENABLED", "false").lower() == "true")
    # Optional Codex App Server engineering executor. Off by default. The
    # product host only speaks the pinned native JSON-RPC stdio contract and
    # never selects a model, reads real Codex credentials or grants tools.
    codex_appserver_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_CODEX_APPSERVER_ENABLED", "false").lower() == "true")
    codex_appserver_prepare_on_startup: bool = field(default_factory=lambda: os.getenv(
        "AGENT_CODEX_APPSERVER_PREPARE_ON_STARTUP", "false").lower() == "true")
    codex_appserver_binary: str = field(default_factory=lambda: os.getenv("AGENT_CODEX_APPSERVER_BINARY", "").strip())
    # Local fixed candidate from the 2026-11-26 baseline. An operator installing
    # a different official build must pin its own digest explicitly.
    codex_appserver_binary_sha256: str = field(default_factory=lambda: os.getenv("AGENT_CODEX_APPSERVER_BINARY_SHA256", "c06a67fd8ac76508dc78bd99ce20c9f317b8d568beda59485f3f4134df9962d5").strip().lower())
    codex_appserver_response_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_CODEX_APPSERVER_RESPONSE_TIMEOUT_SECONDS", "30")))
    codex_appserver_turn_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_CODEX_APPSERVER_TURN_TIMEOUT_SECONDS", "300")))
    codex_appserver_max_input_chars: int = field(default_factory=lambda: int(os.getenv("AGENT_CODEX_APPSERVER_MAX_INPUT_CHARS", "20000")))
    codex_appserver_max_output_chars: int = field(default_factory=lambda: int(os.getenv("AGENT_CODEX_APPSERVER_MAX_OUTPUT_CHARS", "131072")))
    # Per model response limit; independent of goal budgets and turn limits.
    harness_max_output_tokens: int = field(default_factory=lambda: int(os.getenv("AGENT_HARNESS_MAX_OUTPUT_TOKENS", "8192")))
    # One owned Node process executes a manual compaction, including its
    # auxiliary model request. Timeout closes and reaps that process; it never
    # manufactures a completed summary.
    harness_compaction_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_HARNESS_COMPACTION_TIMEOUT_SECONDS", "600")))
    brand_name: str = field(default_factory=lambda: os.getenv("AGENT_BRAND_NAME", "Flint 开物"))
    # New accounts join this private workspace only with an administrator invitation.
    registration_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_REGISTRATION_ENABLED", "false").lower() == "true")
    registration_invite_code: str = field(default_factory=lambda: os.getenv("AGENT_REGISTRATION_INVITE_CODE", ""))
    registration_ip_limit: int = field(default_factory=lambda: int(os.getenv("AGENT_REGISTRATION_IP_LIMIT", "5")))
    registration_global_limit: int = field(default_factory=lambda: int(os.getenv("AGENT_REGISTRATION_GLOBAL_LIMIT", "50")))
    business_data_catalog: str = field(default_factory=lambda: os.getenv("AGENT_BUSINESS_DATA_CATALOG", "").strip())
    model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", ""))
    effort: str | None = field(default_factory=lambda: os.getenv("AGENT_EFFORT"))
    legacy_effort: str = field(init=False)
    provider_settings: str = field(default_factory=lambda: os.getenv("AGENT_PROVIDER_SETTINGS", ""))
    # Synthetic/loopback model gateways are test-only and never count as a
    # production TokenHub route. This flag is server-side and must remain false
    # in deployed environments.
    provider_test_mode: bool = field(default_factory=lambda: os.getenv("AGENT_PROVIDER_TEST_MODE", "false").lower() == "true")
    model_catalog: str = field(default_factory=lambda: os.getenv("AGENT_MODEL_CATALOG", ""))
    default_model_id: str = field(default_factory=lambda: os.getenv("AGENT_DEFAULT_MODEL_ID", ""))
    origins: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip().rstrip("/") for x in os.getenv("AGENT_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if x.strip()))
    mcp_hosts: tuple[str, ...] = field(default_factory=lambda: tuple(x.strip().lower() for x in os.getenv("AGENT_MCP_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if x.strip()))
    mcp_oauth_profiles: str = field(default_factory=lambda: os.getenv("AGENT_MCP_OAUTH_PROFILES", "").strip())
    mcp_oauth_loopback: bool = field(default_factory=lambda: os.getenv("AGENT_MCP_OAUTH_LOOPBACK", "false").lower() == "true")
    cookie_secure: bool = field(default_factory=lambda: os.getenv("AGENT_COOKIE_SECURE", "false").lower() == "true")
    session_cookie_name: str = field(default_factory=lambda: os.getenv("AGENT_SESSION_COOKIE_NAME", "zhixing_session"))
    cookie_path: str = field(default_factory=lambda: os.getenv("AGENT_COOKIE_PATH", "/"))
    # Credential writes require authentic transport.  Forwarded headers are
    # only honored by a constrained middleware and only when the immediate
    # socket peer is one of these explicitly configured proxy addresses.  An
    # empty list means no forwarded header is ever trusted.
    trusted_proxy_peers: tuple[str, ...] = field(default_factory=lambda: tuple(
        item.strip() for item in os.getenv("AGENT_TRUSTED_PROXY_PEERS", "").split(",") if item.strip()))
    # Explicit local-development escape hatch.  It still requires the real
    # socket peer and request host to be loopback; it is never inferred from a
    # client-supplied Host header alone.
    capability_local_dev: bool = field(default_factory=lambda: os.getenv("AGENT_CAPABILITY_LOCAL_DEV", "false").lower() == "true")
    upload_max_bytes: int = 20 * 1024 * 1024
    chat_attachment_upload_max_bytes: int = field(default_factory=lambda: int(os.getenv("AGENT_CHAT_ATTACHMENT_UPLOAD_MAX_BYTES", str(50 * 1024 * 1024))))
    session_hours: int = 24
    idle_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_IDLE_SECONDS", "600")))
    approval_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_APPROVAL_SECONDS", "600")))
    # SQLite durability. FULL (the default) fsyncs every commit and is the
    # deployment and trial-acceptance setting. NORMAL exists only to diagnose
    # fsync cost on a slow measurement disk; it is never acceptance evidence
    # for the enterprise stability/capacity gate. OFF is rejected outright.
    sqlite_synchronous: str = field(default_factory=lambda: os.getenv("AGENT_SQLITE_SYNCHRONOUS", "FULL"))
    max_turns: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_TURNS", "30")))
    max_budget_usd: float = field(default_factory=lambda: float(os.getenv("AGENT_MAX_BUDGET_USD", "5")))
    max_active_sessions: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_ACTIVE_SESSIONS", "4")))
    provider_cost_reliable: bool = field(default_factory=lambda: os.getenv("AGENT_PROVIDER_COST_RELIABLE", "false").lower() == "true")
    # The fitness router is opt-in. Disabled deployments never make an extra
    # provider call and use the bounded local scorer instead.
    model_router_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_MODEL_ROUTER_ENABLED", "false").lower() == "true")
    # Prompt moderation is independent of the main model and only uses a local reviewer.
    prompt_safety_mode: str = field(default_factory=lambda: os.getenv("AGENT_PROMPT_SAFETY_MODE", "off").strip().lower())
    prompt_safety_policy: str = field(default_factory=lambda: os.getenv("AGENT_PROMPT_SAFETY_POLICY", "").strip())
    prompt_safety_qwen_url: str = field(default_factory=lambda: os.getenv("AGENT_PROMPT_SAFETY_QWEN_URL", "http://127.0.0.1:8091/v1").strip())
    prompt_safety_qwen_model: str = field(default_factory=lambda: os.getenv("AGENT_PROMPT_SAFETY_QWEN_MODEL", DEFAULT_PROMPT_SAFETY_MODEL).strip())
    prompt_safety_timeout_seconds: str = field(default_factory=lambda: os.getenv("AGENT_PROMPT_SAFETY_TIMEOUT_SECONDS", "10"))
    # Public catalog id used for fitness recommendations. Deployments may pin
    # an exact authorized route (for example a 1007 deployment); an unavailable
    # id is reported as evidence gap and never fabricated.
    model_router_model_id: str = field(default_factory=lambda: os.getenv("AGENT_MODEL_ROUTER_MODEL_ID", "deepseek-v4.1-flash-1007"))
    # Tab completion is an independent low-risk route. It must not silently
    # reuse the fitness router target because the two authorizations can differ.
    tab_completion_model_id: str = field(default_factory=lambda: os.getenv("AGENT_TAB_COMPLETION_MODEL_ID", "deepseek-v4.1-flash-1017"))
    # Tab has its own admission switch.  It defaults on so a deployment with
    # an explicitly authorized 1017 target is usable without also enabling
    # the Fitness router; provider/target/budget checks still fail closed.
    tab_completion_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_TAB_COMPLETION_ENABLED", "true").lower() == "true")
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
    # All external model roles use Tencent TokenHub. A loopback URL remains
    # valid only when an operator intentionally runs a controlled TokenHub
    # bridge for local development.
    knowledge_vision_base_url: str = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_VISION_BASE_URL", "https://tokenhub.tencentmaas.com/v1").rstrip("/"))
    knowledge_vision_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_KNOWLEDGE_VISION_ENABLED", "false").lower() == "true")
    knowledge_vision_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_KNOWLEDGE_VISION_TIMEOUT_SECONDS", "30")))
    model_router_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_MODEL_ROUTER_TIMEOUT_SECONDS", "5")))
    model_router_max_output_tokens: int = field(default_factory=lambda: int(os.getenv("AGENT_MODEL_ROUTER_MAX_OUTPUT_TOKENS", "256")))
    model_router_budget_usd: float = field(default_factory=lambda: float(os.getenv("AGENT_MODEL_ROUTER_BUDGET_USD", "0.02")))
    # Web search remains Bing RSS by default.  Additional providers are
    # opt-in and require explicit endpoints and authorization; an absent
    # configuration never causes a hidden external request.  The managed
    # application-tools route is separate from the TokenHub chat route.
    web_search_provider: str = field(default_factory=lambda: os.getenv("AGENT_WEB_SEARCH_PROVIDER", "bing_rss").strip().lower())
    web_search_searxng_url: str = field(default_factory=lambda: os.getenv("AGENT_WEB_SEARCH_SEARXNG_URL", "").strip())
    web_search_brave_url: str = field(default_factory=lambda: os.getenv("AGENT_WEB_SEARCH_BRAVE_URL", "https://api.search.brave.com/res/v1/web/search").strip())
    web_search_brave_api_key: str = field(default_factory=lambda: os.getenv("AGENT_WEB_SEARCH_BRAVE_API_KEY", "").strip(), repr=False)
    web_search_fallback_bing: bool = field(default_factory=lambda: os.getenv("AGENT_WEB_SEARCH_FALLBACK_BING", "true").lower() == "true")
    # Official Search/Fetch/OCR are disabled unless an operator explicitly
    # enables the capability and supplies its separate key.  This key is
    # never read from provider-settings.json or returned in public metadata.
    deepseek_api_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_DEEPSEEK_API_ENABLED", "false").lower() == "true")
    deepseek_api_base_url: str = field(default_factory=lambda: os.getenv("AGENT_DEEPSEEK_API_BASE_URL", "https://api.deepseek.com").strip().rstrip("/"))
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("AGENT_DEEPSEEK_API_KEY", "").strip(), repr=False)
    deepseek_api_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_DEEPSEEK_API_TIMEOUT_SECONDS", "30")))
    deepseek_api_ocr_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_DEEPSEEK_API_OCR_ENABLED", "false").lower() == "true")
    # Image generation is a separately gated capability. A generic chat route
    # is never treated as an image endpoint and no provider is called by
    # default.
    image_generation_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_IMAGE_GENERATION_ENABLED", "false").lower() == "true")
    image_model_id: str = field(default_factory=lambda: os.getenv("AGENT_IMAGE_MODEL_ID", ""))
    image_generator_url: str = field(default_factory=lambda: os.getenv("AGENT_IMAGE_GENERATOR_URL", ""))
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
    # Intranet-connector control plane. Disabled unless an operator supplies a
    # dedicated administrator token and relay endpoint. The administrator
    # token is intentionally separate from user sessions and connector device
    # tokens; none of these values is returned by /config or status APIs.
    kaiwu_control_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_ENABLED", "false").lower() == "true")
    kaiwu_native_relay_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_KAIWU_NATIVE_RELAY_ENABLED", "false").lower() == "true")
    # Only these tenants' signed devices may issue their own MCP integration.
    kaiwu_device_export_tenants: tuple[str, ...] = field(default_factory=lambda: tuple(
        value.strip() for value in os.getenv("AGENT_KAIWU_DEVICE_EXPORT_TENANTS", "").split(",") if value.strip()
    ))
    kaiwu_assist_tenants: tuple[str, ...] = field(default_factory=lambda: tuple(
        value.strip() for value in os.getenv("AGENT_KAIWU_ASSIST_TENANTS", "").split(",") if value.strip()
    ))
    kaiwu_assist_devices: tuple[str, ...] = field(default_factory=lambda: tuple(
        value.strip() for value in os.getenv("AGENT_KAIWU_ASSIST_DEVICES", "").split(",") if value.strip()
    ))
    kaiwu_assist_control_origin: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_ASSIST_CONTROL_ORIGIN", "").strip())
    kaiwu_assist_bridge_secret_file: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_ASSIST_BRIDGE_SECRET_FILE", "").strip())
    kaiwu_control_admin_token: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_ADMIN_TOKEN", "").strip(), repr=False)
    kaiwu_control_admin_token_file: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_ADMIN_TOKEN_FILE", "").strip())
    kaiwu_control_relay_address: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_RELAY_ADDRESS", "").strip())
    kaiwu_control_relay_port: int = field(default_factory=lambda: int(os.getenv("AGENT_KAIWU_CONTROL_RELAY_PORT", "0")))
    kaiwu_control_relay_protocol: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_RELAY_PROTOCOL", "wss").strip().lower())
    kaiwu_control_relay_tls_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_RELAY_TLS_ENABLED", "true").lower() == "true")
    kaiwu_control_relay_tls_server_name: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_RELAY_TLS_SERVER_NAME", "").strip())
    kaiwu_control_relay_tls_trusted_ca_file: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_RELAY_TLS_TRUSTED_CA_FILE", "").strip())
    # Cloud visitor and Windows frpc run on different machines, so a private
    # CA can require different local paths. The client path is returned to the
    # Windows connector; the cloud path is used only by the visitor process.
    kaiwu_control_relay_tls_client_trusted_ca_file: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_RELAY_TLS_CLIENT_TRUSTED_CA_FILE", "").strip())
    # frps listens on a private/controlled interface. The public WSS endpoint
    # normally terminates TLS in a reverse proxy and forwards WebSocket
    # upgrades to this internal bind port.
    kaiwu_control_relay_bind_address: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_RELAY_BIND_ADDRESS", "0.0.0.0").strip())
    kaiwu_control_relay_bind_port: int = field(default_factory=lambda: int(os.getenv("AGENT_KAIWU_CONTROL_RELAY_BIND_PORT", "0")))
    kaiwu_control_edge_user: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_EDGE_USER", "kaiwu-edge").strip())
    kaiwu_edge_public_base: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_EDGE_PUBLIC_BASE", "").strip().rstrip("/"))
    kaiwu_edge_mount_path: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_EDGE_MOUNT_PATH", "/projects/kaiwu/mcp").strip())
    kaiwu_edge_visitor_host: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_EDGE_VISITOR_HOST", "127.0.0.1").strip())
    kaiwu_edge_visitor_port_start: int = field(default_factory=lambda: int(os.getenv("AGENT_KAIWU_EDGE_VISITOR_PORT_START", "31000")))
    kaiwu_edge_visitor_port_end: int = field(default_factory=lambda: int(os.getenv("AGENT_KAIWU_EDGE_VISITOR_PORT_END", "31999")))
    kaiwu_edge_session_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_KAIWU_EDGE_SESSION_TTL_SECONDS", "1800")))
    kaiwu_edge_request_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("AGENT_KAIWU_EDGE_REQUEST_TIMEOUT_SECONDS", "60")))
    kaiwu_integration_default_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("AGENT_KAIWU_INTEGRATION_DEFAULT_TTL_SECONDS", "2592000")))
    # Public key only. The Edge private key must live in the Edge signing
    # service, never in this application process.
    kaiwu_control_edge_public_key: str = field(default_factory=lambda: os.getenv("AGENT_KAIWU_CONTROL_EDGE_PUBLIC_KEY", "").strip())
    # Opt-in product API for the derived Mem0 semantic index. Disabled by
    # default. Turning the route on does NOT import the Mem0 SDK, choose a
    # provider or grant data egress: an operator must explicitly inject a
    # backend/scoped factory. The authoritative NativeMemory snapshot stays the
    # only source of truth and is revalidated on every lookup/sync.
    mem0_product_enabled: bool = field(default_factory=lambda: os.getenv("AGENT_MEM0_PRODUCT_ENABLED", "false").lower() == "true")
    mem0_agent_id: str = field(default_factory=lambda: os.getenv("AGENT_MEM0_AGENT_ID", "kaiwu-memory-index-v1").strip())
    mem0_top_k: int = field(default_factory=lambda: int(os.getenv("AGENT_MEM0_TOP_K", "5")))
    mem0_threshold: float = field(default_factory=lambda: float(os.getenv("AGENT_MEM0_THRESHOLD", "0.0")))
    mem0_max_context_bytes: int = field(default_factory=lambda: int(os.getenv("AGENT_MEM0_MAX_CONTEXT_BYTES", "16384")))

    def __post_init__(self) -> None:
        if self.kaiwu_assist_control_origin:
            address = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", self.kaiwu_assist_control_origin)
            if not address or int(address[1]) > 65535:
                raise ValueError("AGENT_KAIWU_ASSIST_CONTROL_ORIGIN must be an HTTP loopback origin with a port")
        if type(self.chat_attachment_upload_max_bytes) is not int or self.chat_attachment_upload_max_bytes <= 0:
            raise ValueError("AGENT_CHAT_ATTACHMENT_UPLOAD_MAX_BYTES must be a positive integer")
        if not isinstance(self.session_cookie_name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,100}", self.session_cookie_name):
            raise ValueError("AGENT_SESSION_COOKIE_NAME must be a safe cookie token")
        if self.session_cookie_name.lower() in {"expires", "path", "comment", "domain", "max-age", "secure", "httponly", "version", "samesite", "partitioned"}:
            raise ValueError("AGENT_SESSION_COOKIE_NAME must not be a reserved cookie attribute")
        if (not isinstance(self.cookie_path, str) or not self.cookie_path.startswith("/")
                or len(self.cookie_path) > 512 or any(ord(char) < 33 or ord(char) > 126 or char in ';,\\"' for char in self.cookie_path)):
            raise ValueError("AGENT_COOKIE_PATH must be an absolute ASCII cookie path")
        if self.session_cookie_name.startswith("__Secure-") and not self.cookie_secure:
            raise ValueError("__Secure- session cookies require AGENT_COOKIE_SECURE=true")
        if self.session_cookie_name.startswith("__Host-") and (not self.cookie_secure or self.cookie_path != "/"):
            raise ValueError("__Host- session cookies require Secure and Path=/")
        if not isinstance(self.runtime_type, str) or self.runtime_type.strip().lower() not in RUNTIME_TYPES:
            raise ValueError("AGENT_RUNTIME_TYPE must be claude or harness")
        self.runtime_type = self.runtime_type.strip().lower()
        self.deployment_mode = valid_deployment_mode(self.deployment_mode)
        # The task lifecycle is a narrower opt-in on top of the native Team
        # projection. Never let it mount a mutation seam while the reviewed
        # read-only service is off.
        if self.harness_team_tasks_enabled and not self.harness_team_state_enabled:
            raise ValueError("AGENT_HARNESS_TEAM_TASKS_ENABLED requires AGENT_HARNESS_TEAM_STATE_ENABLED=true")
        self.web_search_provider = str(self.web_search_provider).strip().lower()
        if self.web_search_provider not in {"bing_rss", "searxng", "brave", "deepseek_api"}:
            raise ValueError("AGENT_WEB_SEARCH_PROVIDER must be bing_rss, searxng, brave or deepseek_api")
        if type(self.deepseek_api_timeout_seconds) not in {int, float} or not 1 <= float(self.deepseek_api_timeout_seconds) <= 180:
            raise ValueError("AGENT_DEEPSEEK_API_TIMEOUT_SECONDS must be between 1 and 180")
        if self.trial_capabilities is not None:
            if type(self.trial_capabilities) is not tuple or any(not isinstance(item, str) for item in self.trial_capabilities):
                raise ValueError("AGENT_TRIAL_CAPABILITIES must be a comma-separated list of capability names")
            unknown = sorted(set(self.trial_capabilities) - CAPABILITIES)
            if unknown:
                raise ValueError("AGENT_TRIAL_CAPABILITIES contains unknown capability: " + ", ".join(unknown))
            self.trial_capabilities = tuple(dict.fromkeys(self.trial_capabilities))
        elif self.deployment_mode == "trial":
            self.trial_capabilities = DEFAULT_TRIAL_CAPABILITIES
        self.sqlite_synchronous = str(self.sqlite_synchronous).strip().upper()
        if self.sqlite_synchronous not in {"FULL", "NORMAL"}:
            raise ValueError("AGENT_SQLITE_SYNCHRONOUS must be FULL or NORMAL; OFF is not permitted")
        # Pre-catalog Claude sessions used medium when no effort was configured.
        self.legacy_effort = self.effort if self.effort is not None else "medium"
        if self.effort is None:
            self.effort = "high" if self.runtime_type == "harness" else "medium"
        if type(self.harness_max_output_tokens) is not int or not 1 <= self.harness_max_output_tokens <= 131072:
            raise ValueError("AGENT_HARNESS_MAX_OUTPUT_TOKENS must be an integer from 1 to 131072")
        if type(self.harness_compaction_timeout_seconds) is not int or not 1 <= self.harness_compaction_timeout_seconds <= 3600:
            raise ValueError("AGENT_HARNESS_COMPACTION_TIMEOUT_SECONDS must be an integer from 1 to 3600")
        if self.codex_appserver_enabled and not self.codex_appserver_binary:
            raise ValueError("AGENT_CODEX_APPSERVER_BINARY is required when the Codex host is enabled")
        if self.codex_appserver_enabled and not self.codex_appserver_binary_sha256:
            raise ValueError("AGENT_CODEX_APPSERVER_BINARY_SHA256 is required when the Codex host is enabled")
        if self.codex_appserver_binary and not os.path.isabs(self.codex_appserver_binary):
            raise ValueError("AGENT_CODEX_APPSERVER_BINARY must be an absolute path")
        if self.codex_appserver_binary_sha256 and not re.fullmatch(r"[a-f0-9]{64}", self.codex_appserver_binary_sha256):
            raise ValueError("AGENT_CODEX_APPSERVER_BINARY_SHA256 must be a lowercase SHA-256 digest")
        if (type(self.codex_appserver_response_timeout_seconds) not in {int, float}
                or not 1 <= float(self.codex_appserver_response_timeout_seconds) <= 600):
            raise ValueError("AGENT_CODEX_APPSERVER_RESPONSE_TIMEOUT_SECONDS must be between 1 and 600")
        if (type(self.codex_appserver_turn_timeout_seconds) not in {int, float}
                or not 1 <= float(self.codex_appserver_turn_timeout_seconds) <= 3600):
            raise ValueError("AGENT_CODEX_APPSERVER_TURN_TIMEOUT_SECONDS must be between 1 and 3600")
        if type(self.codex_appserver_max_input_chars) is not int or not 1 <= self.codex_appserver_max_input_chars <= 131072:
            raise ValueError("AGENT_CODEX_APPSERVER_MAX_INPUT_CHARS must be between 1 and 131072")
        if type(self.codex_appserver_max_output_chars) is not int or not 1024 <= self.codex_appserver_max_output_chars <= 1048576:
            raise ValueError("AGENT_CODEX_APPSERVER_MAX_OUTPUT_CHARS must be between 1024 and 1048576")
        if not self.model:
            self.model = self.provider_env().get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        if type(self.kaiwu_control_relay_port) is not int or not 0 <= self.kaiwu_control_relay_port <= 65535:
            raise ValueError("AGENT_KAIWU_CONTROL_RELAY_PORT must be between 0 and 65535")
        if self.kaiwu_control_relay_protocol not in {"tcp", "quic", "websocket", "wss"}:
            raise ValueError("AGENT_KAIWU_CONTROL_RELAY_PROTOCOL must be tcp, quic, websocket or wss")
        if type(self.kaiwu_control_relay_bind_port) is not int or not 0 <= self.kaiwu_control_relay_bind_port <= 65535:
            raise ValueError("AGENT_KAIWU_CONTROL_RELAY_BIND_PORT must be between 0 and 65535")
        if not isinstance(self.kaiwu_edge_mount_path, str) or not self.kaiwu_edge_mount_path.startswith("/") or ".." in self.kaiwu_edge_mount_path.split("/"):
            raise ValueError("AGENT_KAIWU_EDGE_MOUNT_PATH must be an absolute path without parent traversal")
        if type(self.kaiwu_edge_visitor_port_start) is not int or type(self.kaiwu_edge_visitor_port_end) is not int:
            raise ValueError("Kaiwu visitor port bounds must be integers")
        if not 1 <= self.kaiwu_edge_visitor_port_start <= self.kaiwu_edge_visitor_port_end <= 65535:
            raise ValueError("Kaiwu visitor port bounds are invalid")
        if type(self.kaiwu_edge_session_ttl_seconds) is not int or not 60 <= self.kaiwu_edge_session_ttl_seconds <= 86400:
            raise ValueError("AGENT_KAIWU_EDGE_SESSION_TTL_SECONDS must be between 60 and 86400")
        if type(self.kaiwu_edge_request_timeout_seconds) not in {int, float} or not 1 <= float(self.kaiwu_edge_request_timeout_seconds) <= 300:
            raise ValueError("AGENT_KAIWU_EDGE_REQUEST_TIMEOUT_SECONDS must be between 1 and 300")
        if type(self.kaiwu_integration_default_ttl_seconds) is not int or not 60 <= self.kaiwu_integration_default_ttl_seconds <= 31536000:
            raise ValueError("AGENT_KAIWU_INTEGRATION_DEFAULT_TTL_SECONDS must be between 60 and 31536000")
        if not isinstance(self.kaiwu_device_export_tenants, tuple) or any(
            not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tenant)
            for tenant in self.kaiwu_device_export_tenants
        ):
            raise ValueError("AGENT_KAIWU_DEVICE_EXPORT_TENANTS must be a comma-separated list of tenant IDs")
        if type(self.mem0_product_enabled) is not bool:
            raise ValueError("AGENT_MEM0_PRODUCT_ENABLED must be a boolean")
        if not isinstance(self.mem0_agent_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.mem0_agent_id) is None:
            raise ValueError("AGENT_MEM0_AGENT_ID must be 1-128 characters from [A-Za-z0-9_-]")
        if type(self.mem0_top_k) is not int or not 1 <= self.mem0_top_k <= 50:
            raise ValueError("AGENT_MEM0_TOP_K must be an integer from 1 to 50")
        if type(self.mem0_threshold) is bool or not isinstance(self.mem0_threshold, (int, float)) or not 0.0 <= float(self.mem0_threshold) <= 1.0:
            raise ValueError("AGENT_MEM0_THRESHOLD must be between 0 and 1")
        self.mem0_threshold = float(self.mem0_threshold)
        if type(self.mem0_max_context_bytes) is not int or not 1 <= self.mem0_max_context_bytes <= 131072:
            raise ValueError("AGENT_MEM0_MAX_CONTEXT_BYTES must be an integer from 1 to 131072")

    def deployment_policy(self) -> DeploymentPolicy:
        return DeploymentPolicy(self.deployment_mode, self.trial_capabilities)

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

    def kaiwu_assist_bridge_secret(self) -> str:
        if not self.kaiwu_assist_bridge_secret_file:
            return ""
        try:
            with Path(self.kaiwu_assist_bridge_secret_file).open(encoding="ascii") as source:
                key = source.read(160).strip()
        except (OSError, UnicodeError):
            return ""
        return key if re.fullmatch(r"[A-Za-z0-9_-]{43,128}", key) else ""

    def kaiwu_control_token(self) -> str:
        """Return the dedicated control-plane administrator token."""
        if self.kaiwu_control_admin_token_file:
            try:
                value = Path(self.kaiwu_control_admin_token_file).read_text(encoding="utf-8").strip()
            except OSError:
                value = ""
            if value:
                return value
        return self.kaiwu_control_admin_token.strip()

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
