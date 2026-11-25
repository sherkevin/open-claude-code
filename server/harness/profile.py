"""Generate a private, explicit dsh profile without inheriting shipped bundles.

Credentials belong in the launcher's allowlisted environment, never this file.
The caller owns a dedicated process and DSH_HOME for one conversation.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


PINNED_COMMIT = "fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159"
PROFILE_NAME = "workspace"
MODEL_RECOVERY_CODES = ("TRANSPORT", "RATE_LIMIT", "SERVER", "TIMEOUT")
TOOL_NAMES = frozenset({
    "knowledge_search", "knowledge_read", "web_search", "web_read",
    "generate_xlsx", "generate_image",
    "file_read", "file_write", "file_search",
    "ocr", "image_search",
    "memory_list", "memory_read", "memory_write",
    "ask_user", "review_plan",
    "delegate_tasks", "schedule_create", "schedule_list", "schedule_view",
})
EXTERNAL_TOOL_NAME = re.compile(r"external_[0-9a-f]{32}")
MAX_TOOL_DEFINITION_BYTES = 524_288
MAX_SCHEMA_DEPTH = 128


def _validate_external_schema(schema: dict) -> None:
    """Keep external schemas inert; full argument validation belongs to Python.

    Local references remain intact for the provider and the host's JSON Schema
    validator. No reference may change the base URI or retrieve another file or
    URL. The conservative walk also checks nested annotation values.
    """
    pending: list[tuple[object, int]] = [(schema, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_SCHEMA_DEPTH:
            raise ValueError("External tool schema exceeds the supported depth")
        if isinstance(value, dict):
            if "$id" in value:
                raise ValueError("External tool schemas cannot change the reference base URI")
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in value and (not isinstance(value[key], str) or not value[key].startswith("#")):
                    raise ValueError("External tool schema references must stay local")
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)

# Absolute source modules share the launcher's tsx/source resolver. No stock
# sdk/sdk-minimal/base bundle, native tool, plugin manager, command dispatcher,
# shell, scheduler, credential store, user rules, or MCP process is loaded.
KERNEL_MODULES = (
    ("llm", "llm/llm"),
    ("session", "core/session"),
    ("session-projection", "session/session-projection"),
    ("token-meter", "llm/token-meter"),
    ("commands", "interaction/commands"),
    ("system-prompt", "core/system-prompt"),
    ("filesystem", "fs/fs-local"),
    ("attachments", "attachment/attachment-local"),
    ("working-directory", "session/working-directory"),
    ("tools", "core/tools"),
    ("tool-executor-native", "core/tool-executor-native"),
    ("agent", "core/agent"),
    ("agent-loop", "core/agent-loop"),
    ("sessions", "session/session-persistence-jsonl"),
    ("llm-deepseek", "llm/llm-deepseek-api-key"),
    ("llm-retry", "llm/llm-retry"),
    ("sdk-app-startup", "bundle/sdk-app"),
)


def _write_private(path: Path, value: object) -> None:
    """Atomic private JSON, also valid YAML, with no executable YAML tags."""
    descriptor, temporary = tempfile.mkstemp(prefix=".workspace-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_profile(
    *, profile_root: Path, source_dir: Path, cwd: Path, session_id: str,
    system_prompt: str, provider: dict, tools: list[dict], max_steps: int = 30,
    tool_timeout_ms: int = 660_000,
    delegation_timeout_ms: int | None = None,
) -> dict:
    """Write one locked profile and return dsh arguments and SDK initialize data.

    ``profile_root`` is the private DSH_HOME, not the workspace. ``provider``
    accepts only baseURL, model, apiKeyEnv, reasoningEffort, maxTokens and
    contextWindow. Its API-key environment variable is resolved by the official
    Messages adapter; AUTH_TOKEN-only routes must be rejected by the caller.
    ``tools`` use the six workspace names or host-issued external SHA-256
    aliases. Only name, description and object-rooted JSON schema cross into
    Node; the caller retains the external alias-to-connection routing table.
    """
    root, source, workspace = (Path(item).resolve() for item in (profile_root, source_dir, cwd))
    if root == workspace or root == source or source.is_relative_to(root):
        raise ValueError("Harness home must be separate from source and workspace")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
        raise ValueError("Invalid Harness session identifier")
    if not isinstance(system_prompt, str):
        raise ValueError("System prompt must be literal text")
    if type(max_steps) is not int or not 1 <= max_steps <= 1000:
        raise ValueError("Harness max_steps must be between 1 and 1000")
    if type(tool_timeout_ms) is not int or not 1000 <= tool_timeout_ms <= 3_600_000:
        raise ValueError("Harness tool timeout is outside the supported range")
    if delegation_timeout_ms is not None and (type(delegation_timeout_ms) is not int
                                               or not tool_timeout_ms <= delegation_timeout_ms <= 86_400_000):
        raise ValueError("Harness delegation timeout is outside the supported range")
    if not isinstance(provider, dict) or set(provider) - {
        "baseURL", "model", "apiKeyEnv", "reasoningEffort", "maxTokens", "contextWindow",
    }:
        raise ValueError("Unsupported provider configuration; credentials must stay in environment")
    endpoint = provider.get("baseURL")
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if (parsed is None or parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Provider requires a credential-free HTTP(S) baseURL")
    model = provider.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ValueError("Provider model is required")
    api_key_env = provider.get("apiKeyEnv", "WORKSPACE_MODEL_API_KEY")
    if not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", api_key_env):
        raise ValueError("Invalid provider API-key environment reference")
    effort = provider.get("reasoningEffort")
    if effort not in {"off", "low", "high", "max"}:
        raise ValueError("Unsupported Harness reasoning effort")
    for key in ("maxTokens", "contextWindow"):
        if key in provider and (type(provider[key]) is not int or provider[key] <= 0):
            raise ValueError(f"Provider {key} must be a positive integer")
    names = set()
    if not isinstance(tools, list):
        raise ValueError("Tools must be a list")
    definitions = []
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"name", "description", "input_schema"}:
            raise ValueError("Tool definition requires name, description and input_schema")
        tool_name = tool["name"]
        external = isinstance(tool_name, str) and EXTERNAL_TOOL_NAME.fullmatch(tool_name) is not None
        if (not isinstance(tool_name, str) or (tool_name not in TOOL_NAMES and not external)
                or tool_name in names):
            raise ValueError("Tool is outside the reviewed workspace allowlist or duplicated")
        if not isinstance(tool["description"], str) or not tool["description"]:
            raise ValueError("Tool description is required")
        schema = tool["input_schema"]
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Tool input_schema must be an object JSON schema")
        if external:
            _validate_external_schema(schema)
        serialized = json.dumps(tool, ensure_ascii=False, allow_nan=False)
        if len(serialized.encode("utf-8")) > MAX_TOOL_DEFINITION_BYTES:
            raise ValueError("Tool definition exceeds the supported size")
        definition = json.loads(serialized)
        names.add(tool_name)
        definitions.append(definition)

    profile_dir = root / "profiles" / PROFILE_NAME
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    profile_dir.chmod(0o700)
    # Ambient files would defeat the composition guarantee. A private home is
    # allowed to be reused for persistence, never to import user customizations.
    for ambient in (root / "cordis.patch.yml", root / ".env", profile_dir / ".env", workspace / ".env"):
        if ambient.exists() or ambient.is_symlink():
            raise ValueError("Harness private launch directories contain ambient configuration")

    entries = []
    for identifier, package in KERNEL_MODULES:
        module = source / "packages" / package / "src" / "index.ts"
        if not module.is_file():
            raise ValueError(f"Pinned Harness module is missing: {package}")
        entry = {"id": identifier, "name": str(module)}
        if identifier == "system-prompt":
            entry["config"] = {"includeHarnessIdentity": False, "includeRuntimeContext": False, "personaPrefix": system_prompt}
        elif identifier == "agent-loop":
            entry["config"] = {"agents": []}
        elif identifier == "sessions":
            entry["config"] = {"root": str(root / "sessions"), "compression": "none"}
        elif identifier == "attachments":
            entry["config"] = {
                "dshHome": str(root), "maxImageBytes": 5 * 1024 * 1024,
                "maxImagesPerMessage": 20, "maxMessageImageBytes": 10 * 1024 * 1024,
                "maxImagePixels": 10_000_000, "maxImageDimension": 10_000,
                "imageCompressionConcurrency": 1,
            }
        elif identifier == "sdk-app-startup":
            entry["config"] = {"profile": PROFILE_NAME}
        elif identifier == "llm-deepseek":
            entry["config"] = {
                "baseURL": endpoint.rstrip("/"), "apiKeyEnv": api_key_env,
                # Transport capability, not a claim of provider visual quality.
                "models": [{"id": model, "inputModalities": ["text", "image"]}],
                "reasoningEffort": effort, "thinking": "disabled" if effort == "off" else "enabled",
                "streamIdleTimeoutMs": 120_000,
                # Recover only the failed model request, never replay a turn.
                # The host bridge additionally rejects retries after visible text
                # and limits the entire turn to one recovery attempt.
                "retryPolicy": {"mode": "normal", "maxRetries": 1,
                    "retryableCodes": list(MODEL_RECOVERY_CODES),
                    "backoff": {"initialDelayMs": 500, "maxDelayMs": 10_000, "jitterRatio": 0}},
                **({"maxTokens": provider["maxTokens"]} if "maxTokens" in provider else {}),
                **({"defaultContextWindow": provider["contextWindow"]} if "contextWindow" in provider else {}),
            }
        entries.append(entry)
    entries.append({
        "id": "workspace-compaction",
        "name": str(Path(__file__).with_name("compaction_plugin.ts").resolve()),
        "config": {
            "sourceDir": str(source),
            "auto": True,
            "maxTokens": min(provider.get("maxTokens", 8192), 131072),
        },
    })
    entries.append({
        "id": "workspace-host", "name": str(Path(__file__).with_name("workspace_plugin.ts").resolve()),
        "config": {
            "sourceDir": str(source), "sessionId": session_id, "cwd": str(workspace), "profileRoot": str(root),
            "requestRetryCodes": list(MODEL_RECOVERY_CODES),
            "usageEndpoint": endpoint.rstrip("/") + ("" if parsed.path.rstrip("/").endswith("/v1") else "/v1") + "/messages",
            "model": model, "tools": definitions, "maxSteps": max_steps,
            "toolTimeoutMs": tool_timeout_ms, "sourceCommit": PINNED_COMMIT,
            **({"delegationTimeoutMs": delegation_timeout_ms} if delegation_timeout_ms is not None else {}),
        },
    })
    _write_private(profile_dir / "package.json", {
        "name": "zhixing-harness-workspace", "private": True, "dependencies": {},
        "dsh": {"profile": {"bundles": []}},
    })
    _write_private(profile_dir / "cordis.patch.yml", [{"insert": entries}])
    return {
        "profile": PROFILE_NAME, "dsh_home": str(root), "profile_dir": str(profile_dir),
        "args": ["--profile", PROFILE_NAME],
        "initialize": {
            "cwd": str(workspace), "provider": "deepseek-official", "model": model,
            "approvalRequests": True, "reasoningEffort": effort,
            **({"maxTokens": provider["maxTokens"]} if "maxTokens" in provider else {}),
        },
    }
