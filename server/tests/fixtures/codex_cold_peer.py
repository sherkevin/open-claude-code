"""Exercise concurrent cold initialization around the existing synthetic stdio peer."""
import fcntl
import importlib.util
import os
import sys
import threading
import time
from pathlib import Path


def wait(path):
    end = time.monotonic() + 25
    while not path.exists():
        if time.monotonic() > end:
            raise RuntimeError("fixture control did not release the barrier")
        time.sleep(0.02)


def main():
    original, control = Path(sys.argv[1]), Path(sys.argv[2])
    spec = importlib.util.spec_from_file_location("original_fake_peer", original)
    peer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(peer)
    native_home = Path(os.environ["CODEX_HOME"])
    ready = native_home / ".synthetic-initialization-ready"
    original_handle = peer.handle
    original_scenario = peer.turn_scenario
    cwd = Path.cwd().name

    def handle(message):
        if message.get("method") != "initialize":
            return original_handle(message)
        peer.log({"event": "fixture.initialize", "cwd": cwd, "pid": os.getpid()})
        if ready.exists():
            return original_handle(message)
        with (native_home / ".synthetic-initialization-lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                (control / "conflict-seen").touch()
                peer.log({"event": "fixture.cold_conflict"})
                os._exit(1)  # Native startup can exit without answering initialize.
            if not ready.exists():
                (control / "cold-entered").touch()
                wait(control / "release-initialize")
                try:
                    (control / "fail-one-initialize").unlink()
                except FileNotFoundError:
                    pass
                else:
                    peer.log({"event": "fixture.cold_failure"})
                    os._exit(1)
                ready.touch()
        return original_handle(message)

    def scenario(thread_id, turn_id, initial):
        if not (control / "hold-turns").exists():
            return original_scenario(thread_id, turn_id, initial)

        def finish():
            peer.log({"event": "fixture.turn_waiting", "thread": thread_id, "turn": turn_id,
                      "pid": os.getpid(), "monotonic_ns": time.monotonic_ns()})
            (control / ("turn-in-flight-" + cwd)).touch()
            wait(control / "release-turns")
            original_scenario(thread_id, turn_id, initial)

        threading.Thread(target=finish, daemon=True).start()

    peer.handle = handle
    peer.turn_scenario = scenario
    sys.argv = [str(original), "--log", str(control / f"peer-{os.getpid()}.jsonl")]
    peer.main()


if __name__ == "__main__":
    main()
