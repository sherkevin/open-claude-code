"""Open a local SSH tunnel to the authorized Tencent development preview.

The password is saved privately for the current developer, never printed.
This development utility is intentionally excluded from product packages.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".runtime/remote-preview"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]

def main():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    password = subprocess.run(SSH + ["tencent", "cat /data/dengkw/workspace/open-claude-code/.runtime/admin-password"], capture_output=True, timeout=30, check=True).stdout
    target = STATE / "admin-password"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(password)
    command = SSH + ["-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-N", "-L", "127.0.0.1:18081:127.0.0.1:18081", "tencent"]
    with (STATE / "tunnel.log").open("ab") as logfile:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
    (STATE / "tunnel.json").write_text(json.dumps({"pid": process.pid, "port": 18081}), encoding="utf-8")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemExit("SSH tunnel exited; inspect private tunnel log")
        try:
            with urlopen("http://127.0.0.1:18081/api/v1/health/ready", timeout=1) as response:
                if response.status == 200:
                    print("Preview ready: http://127.0.0.1:18081")
                    print("Username: admin; private password file:", target)
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise SystemExit("Tunnel readiness timed out; inspect private tunnel log")

if __name__ == "__main__":
    main()
