"""Print bounded preview diagnostics with provider credentials redacted."""
import json
import re
import sqlite3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
secrets = []
path = Path.home() / ".claude/settings.json"
if path.is_file():
    env = json.loads(path.read_text()).get("env", {})
    secrets = [str(v) for k, v in env.items() if any(t in k.upper() for t in ("KEY", "TOKEN", "PASSWORD", "SECRET")) and isinstance(v, str) and len(v) > 5]

def safe(value):
    text = json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)

database = root / ".runtime/data/app.sqlite3"
if database.is_file():
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = [dict(row) for row in connection.execute("SELECT id,status,error,usage,created_at,completed_at FROM runs ORDER BY created_at DESC LIMIT 8")]
    print(safe({"runs": rows}))
    for row in connection.execute("SELECT payload FROM raw_events ORDER BY id DESC LIMIT 5"):
        print(safe(row["payload"][:5000]))
    connection.close()
logfile = root / ".runtime/server.log"
if logfile.is_file():
    print(safe(logfile.read_text(errors="replace")[-10000:]))
