"""Sync the user-authorized school Tencent model route without logging secrets.

Only the Tencent provider record is read. Credentials travel between SSH pipes,
remain in memory locally, and are stored owner-only on the destination.
"""
import json
import subprocess

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]
SOURCE = r'''
import json
from pathlib import Path
p = next(p for p in json.loads((Path.home()/".config/magpie/providers.json").read_text())["providers"] if p["id"] == "tencent-cloud")
assert p["anthropic"] == "https://tokenhub.tencentmaas.com"
assert "deepseek-v4.1-flash-1017" in p["models"]
assert isinstance(p["key"], str) and p["key"]
print(json.dumps({"base_url": p["anthropic"], "model": "deepseek-v4.1-flash-1017", "token": p["key"]}))
'''
DESTINATION = r'''
import json, os, sys, shutil
from pathlib import Path
from datetime import datetime, timezone
p = json.load(sys.stdin)
assert p["base_url"] == "https://tokenhub.tencentmaas.com"
assert p["model"] == "deepseek-v4.1-flash-1017"
env = {"ANTHROPIC_BASE_URL":p["base_url"], "ANTHROPIC_AUTH_TOKEN":p["token"], "ANTHROPIC_MODEL":p["model"]}
for tier in ("HAIKU", "SONNET", "OPUS"):
    env["ANTHROPIC_DEFAULT_" + tier + "_MODEL"] = p["model"]
env["ANTHROPIC_SMALL_FAST_MODEL"] = p["model"]
path = Path.home()/".claude/settings.json"
path.parent.mkdir(exist_ok=True, mode=0o700)
settings = json.loads(path.read_text()) if path.exists() else {}
backup = None
if path.exists():
    backup = path.with_name("settings.json.before-tencent-sync-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    fd = os.open(backup, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd,"wb") as stream:
        stream.write(path.read_bytes())
settings.setdefault("env",{}).pop("ANTHROPIC_API_KEY", None)
settings["env"].update(env)
settings["model"] = p["model"]
def write_private(target, value):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = target.with_name(target.name + ".sync.tmp")
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(fd,"w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temporary,target)
write_private(path, settings)
project = Path("/data/dengkw/workspace/open-claude-code/.runtime/provider-settings.json")
write_private(project, {"model":p["model"],"env":env})
print(json.dumps({"synced":True,"endpoint":p["base_url"],"model":p["model"],"settings":str(path),"project_settings":str(project),"backup":str(backup),"mode":oct(project.stat().st_mode & 0o777)}))
'''

def quoted(code):
    return "python3 -c '" + code.replace("'", "'\"'\"'") + "'"

def main():
    source = subprocess.run(SSH + ["viplabserver12", quoted(SOURCE)], capture_output=True, timeout=30)
    if source.returncode:
        raise SystemExit("Source provider read failed; no credentials printed or transferred")
    payload = source.stdout
    value = json.loads(payload)
    if set(value) != {"base_url", "model", "token"}:
        raise SystemExit("Unexpected provider data")
    destination = subprocess.run(SSH + ["tencent", quoted(DESTINATION)], input=payload, capture_output=True, timeout=30)
    if destination.returncode:
        raise SystemExit("Destination configuration write failed; inspect configuration paths without displaying values")
    print(destination.stdout.decode().strip())

if __name__ == "__main__":
    main()
