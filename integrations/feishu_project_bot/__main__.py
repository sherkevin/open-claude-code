"""Run explicitly: python -m integrations.feishu_project_bot CONFIG.json [--status]."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import time
from pathlib import Path

from .bridge import Bridge, validate_config
from .lark_client import EventStream, LarkClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("--status", action="store_true", help="read local task status; no external calls")
    parser.add_argument("--check", action="store_true", help="verify bot identity and read group context; no subscription or sends")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    validate_config(config)
    os.umask(0o077)
    state = Path(config["state_dir"]).expanduser()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.status:
        import sqlite3
        db_path = state / "inbox.sqlite3"
        if not db_path.exists():
            print("[]")
            return
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            print(json.dumps([dict(row) for row in db.execute(
                "SELECT seq,request_id,message_id,status,reason,created FROM inbox ORDER BY seq")], ensure_ascii=False, indent=2))
        return
    with (state / "bridge.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another bridge owns this state directory; do not start a second listener")
        if config.get("enabled") is not True and not args.check:
            raise SystemExit("integration disabled: complete deployment checks before setting enabled=true")
        client = LarkClient(config)
        client.verify_identity()
        if args.check:
            context = client.context({"chat_id": config["chat_id"], "create_time": str(int(time.time() * 1000))})
            print(json.dumps({"ok": True, "identity": "bot", "app_id": config["expected_app_id"],
                              "chat_id": config["chat_id"], "observed_members": len(context["members"]),
                              "history_readable": True}, ensure_ascii=False))
            return
        from .orca_transport import OrcaTransport
        transport_config = {**config.get("transport", {}), "session_id": config["session_id"],
                            "state_dir": str(state / "transport")}
        bridge = Bridge(config, client, OrcaTransport(transport_config))
        stream = EventStream(client, bridge.ingest)
        stop = False

        def stopping(*_):
            nonlocal stop
            stop = True

        signal.signal(signal.SIGTERM, stopping)
        signal.signal(signal.SIGINT, stopping)
        try:
            stream.start()
            next_tick = 0.0
            while not stop:
                time.sleep(0.5)
                if stream.failed.is_set():
                    raise RuntimeError("event stream stopped; inspect CLI diagnostics before restarting")
                if time.monotonic() >= next_tick:
                    bridge.tick()
                    next_tick = time.monotonic() + config.get("poll_interval_seconds", 5)
        finally:
            stream.close()
            bridge.close()


if __name__ == "__main__":
    main()
