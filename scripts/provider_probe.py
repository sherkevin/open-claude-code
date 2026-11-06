"""Minimal live provider compatibility probe using the user's installed CLI.

Credentials are read only from the explicitly configured owner settings file and
are never printed. This is an infrastructure check, not application acceptance.
"""
import json
import os
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = Path(os.getenv("AGENT_PROVIDER_SETTINGS", str(Path.home() / ".claude/settings.json")))
settings = json.loads(source.read_text())
allowed = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"}
env = dict(os.environ)
env.update({k: str(v) for k, v in settings.get("env", {}).items() if k in allowed})
work = root / ".runtime/provider-probe"
work.mkdir(parents=True, exist_ok=True, mode=0o700)
config = work / "config"
config.mkdir(exist_ok=True, mode=0o700)
env["CLAUDE_CONFIG_DIR"] = str(config)
env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
binary = str(Path.home() / ".local/bin/claude")
command = [binary, "-p", "只回复：连接成功", "--model", env.get("ANTHROPIC_MODEL", "deepseek-flash[1m]"), "--output-format", "json", "--tools", "", "--max-turns", "1", "--effort", "medium", "--setting-sources", ""]
result = subprocess.run(command, cwd=work, env=env, text=True, capture_output=True, timeout=90)
try:
    value = json.loads(result.stdout)
    output = {k: value.get(k) for k in ("type", "subtype", "is_error", "result", "duration_ms", "usage")}
except (ValueError, TypeError):
    output = {"type": "unparsed", "exit_code": result.returncode}
output["exit_code"] = result.returncode
print(json.dumps(output, ensure_ascii=False, indent=2))
