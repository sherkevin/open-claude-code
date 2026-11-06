"""Read-only environment probe. Never print credential values."""
import json
import os
from pathlib import Path

home = Path.home()
result = {"home": str(home), "configuration": []}
for path in (home / ".claude/settings.json", home / ".claude/settings.local.json"):
    item = {"path": str(path), "exists": path.exists()}
    if path.exists():
        try:
            data = json.loads(path.read_text())
            env = data.get("env", {})
            item["keys"] = sorted(data)
            item["env_keys"] = sorted(env)
            item["safe_values"] = {
                k: v for k, v in env.items()
                if k in {"ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"}
            }
            item["model"] = data.get("model")
            item["mcp_names"] = sorted(data.get("mcpServers", {}))
        except Exception as exc:
            item["error"] = type(exc).__name__
    result["configuration"].append(item)
result["process_env_names"] = sorted(k for k in os.environ if any(t in k for t in ("ANTHROPIC", "CLAUDE", "DEEPSEEK")))
print(json.dumps(result, ensure_ascii=False, indent=2))
