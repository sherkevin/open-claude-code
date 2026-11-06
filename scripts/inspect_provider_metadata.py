"""Print redacted model-provider metadata; never inspect login/session stores."""
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

safe_keys = {"name", "id", "type", "provider", "model", "models", "model_name", "base_url", "baseUrl", "baseURL", "url", "endpoint", "protocol", "api_format", "format", "enabled", "priority", "default", "selected", "active", "auth_type", "alias", "chat", "responses", "anthropic", "preset", "from", "to", "target", "source", "match", "upstream", "pattern", "route"}
secret_parts = ("key", "token", "secret", "password", "authorization", "cookie", "credential")

def scrub(value, key=""):
    if any(part in key.lower() for part in secret_parts):
        return {"present": bool(value)}
    if isinstance(value, dict):
        return {k: scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str) and key in safe_keys:
        if value.startswith(("http://", "https://")):
            url = urlsplit(value)
            return urlunsplit((url.scheme, url.hostname + ((":" + str(url.port)) if url.port else ""), url.path, "", ""))
        return value
    return "[value omitted]"

for name in ("providers.json", "applied.json", "stash.json"):
    path = Path.home() / ".config/magpie" / name
    data = json.loads(path.read_text())
    if name == "providers.json":
        data = {"providers": [{k:v for k,v in p.items() if k not in {"pickedFrom", "website", "keysUrl", "icon", "catalog"}} for p in data["providers"] if p.get("id") in {"deepseek", "tencent-cloud"}]}
    print(json.dumps({"path": str(path), "metadata": scrub(data)}, ensure_ascii=False, indent=2))

from collections import deque
directory = Path.home() / ".config/magpie/routing"
latest = sorted(directory.glob("*.jsonl"))[-1]
with latest.open() as stream:
    rows = deque(stream, maxlen=3)
for line in rows:
    value = json.loads(line)
    print("ROUTE_SCHEMA", list(value))
    print("ROUTE_METADATA", json.dumps({k:scrub(v, k) for k,v in value.items() if k in safe_keys or k in {"decision", "routing", "requestedModel", "upstreamModel", "providerId", "status"}}, ensure_ascii=False))
