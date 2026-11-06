"""Locate the user-owned model proxy configuration without revealing secrets."""
import json
import re
import subprocess
from pathlib import Path

listing = subprocess.run(["ss", "-ltnp", "sport = :3425"], capture_output=True, text=True).stdout
pids = sorted(set(re.findall(r"pid=(\d+)", listing)))
result = {"listen_3425": bool(pids), "processes": []}
for pid in pids:
    process = {"pid": int(pid)}
    base = Path("/proc") / pid
    try:
        process["exe"] = str((base / "exe").resolve())
        process["cwd"] = str((base / "cwd").resolve())
        args = (base / "cmdline").read_bytes().decode(errors="replace").split("\0")
        process["configuration_paths"] = [a for a in args if a.startswith("/") and a.endswith((".py", ".js", ".mjs", ".json", ".yaml", ".yml", ".toml"))]
        process["ssh_forwards"] = [a for a in args if re.fullmatch(r"\d+:127\.0\.0\.1:\d+", a)]
    except Exception as exc:
        process["read_error"] = type(exc).__name__
    result["processes"].append(process)
for directory in (Path.home() / ".config/claude-proxy", Path.home() / ".cc-switch", Path.home() / ".config/cc-switch"):
    if directory.is_dir():
        result[str(directory)] = sorted(p.name for p in directory.iterdir())
print(json.dumps(result, ensure_ascii=False, indent=2))
