"""Offline pinned Codex stdio metadata smoke; never send a turn or approve a tool."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

DIGEST = "c06a67fd8ac76508dc78bd99ce20c9f317b8d568beda59485f3f4134df9962d5"
CHECK_NET = """import os,sys
from pathlib import Path
n=[x.split(':')[0].strip() for x in Path('/proc/net/dev').read_text().splitlines()[2:]]
assert n==['lo'] and len(Path('/proc/net/route').read_text().splitlines())==1
assert os.environ['HOME']=='/isolated/home'
assert not Path('/home').exists()
os.execv('/codex',['/codex','app-server','--stdio'])
"""


def verify(binary: Path, root: Path):
    with binary.open("rb") as file:
        digest = hashlib.sha256()
        for chunk in iter(lambda: file.read(1024*1024), b""):
            digest.update(chunk)
        assert digest.hexdigest() == DIGEST
    # Require a fresh test directory on the designated project data disk.
    assert root.resolve().is_relative_to(Path("/media/data3"))
    root.mkdir(mode=0o700)
    for name in ("home", "codex-home", "tmp", "cache", "config", "data", "work"):
        (root / name).mkdir(mode=0o700)
    command = [
        "/usr/bin/bwrap", "--unshare-net", "--unshare-pid", "--unshare-ipc",
        "--die-with-parent", "--new-session", "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
        "--ro-bind", str(binary.resolve()), "/codex", "--bind", str(root), "/isolated",
        "--bind", str(root/"tmp"), "/tmp", "--chdir", "/isolated/work",
        "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/isolated/home", "--setenv", "CODEX_HOME", "/isolated/codex-home",
        "--setenv", "TMPDIR", "/isolated/tmp", "--setenv", "XDG_CACHE_HOME", "/isolated/cache",
        "--setenv", "XDG_CONFIG_HOME", "/isolated/config", "--setenv", "XDG_DATA_HOME", "/isolated/data",
        "--", "/usr/bin/python3", "-c", CHECK_NET,
    ]
    checks = []
    incoming = queue.Queue()
    with (root / "stderr.log").open("w") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=stderr, text=True, encoding="utf-8", env={})
        def reader():
            for line in process.stdout:
                incoming.put(line)
            incoming.put(None)
        threading.Thread(target=reader, daemon=True).start()
        def send(value):
            process.stdin.write(json.dumps(value, separators=(",", ":"))+"\n")
            process.stdin.flush()
        def rpc(request_id, method, params):
            send({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic()+25
            while time.monotonic()<deadline:
                try:
                    line=incoming.get(timeout=max(.01,deadline-time.monotonic()))
                except queue.Empty:
                    raise RuntimeError(f"{method}: response timeout") from None
                if line is None:
                    raise RuntimeError(f"{method}: process ended")
                event=json.loads(line)
                if "method" in event and "id" in event:
                    raise RuntimeError("Unexpected server request; no approval or credentials supplied")
                if event.get("id")==request_id and "method" not in event:
                    if "error" in event:
                        raise RuntimeError(f"{method}: {event['error']}")
                    return event["result"]
            raise RuntimeError(f"{method}: response timeout")
        try:
            result=rpc(1,"initialize",{"clientInfo":{"name":"kaiwu_isolation_probe","version":"0.1.0"}})
            assert isinstance(result,dict)
            checks.append("isolated stdio initialize")
            print(checks[-1],flush=True)
            send({"method":"initialized"})
            listing=rpc(2,"thread/list",{"limit":5,"cwd":"/isolated/work"})
            assert listing["data"]==[]
            checks.append("fresh scoped thread list empty")
            print(checks[-1],flush=True)
            started=rpc(3,"thread/start",{"cwd":"/isolated/work","sandbox":"read-only",
                         "approvalPolicy":"never","ephemeral":False})
            thread=started["thread"]
            assert thread["id"] and thread["cwd"]=="/isolated/work"
            assert not thread.get("turns")
            checks.append("thread created with no turn and isolated cwd")
            print(checks[-1],flush=True)
            read=rpc(4,"thread/read",{"threadId":thread["id"],"includeTurns":False})
            assert read["thread"]["id"]==thread["id"]
            assert not read["thread"].get("turns")
            checks.append("thread metadata read by exact identity")
            print(checks[-1],flush=True)
            print(json.dumps({"passed":len(checks),"checks":checks,"turns_started":0}),flush=True)
        finally:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary",type=Path)
    parser.add_argument("root",type=Path)
    args=parser.parse_args()
    verify(args.binary,args.root)
