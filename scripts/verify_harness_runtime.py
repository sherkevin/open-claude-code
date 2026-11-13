#!/usr/bin/env python3
"""Handshake with the real pinned Node Harness and an isolated workspace profile.

The only configured provider is an ephemeral loopback server that rejects every
request. No prompt is submitted. This checks process boot, RPC restrictions,
tool composition, and shutdown; it does not validate model inference.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from setup_harness_runtime import NODE_VERSION, PIN, ROOT, SOURCE, TOOLCHAIN


def verify(source: Path, node: Path) -> dict:
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if commit != PIN:
        raise RuntimeError("Unexpected Harness source pin")
    sys.path[:0] = [str(source / "python/sdk/src"), str(ROOT / "server")]
    from deepseek_harness.client import HarnessClient, HarnessConfig
    from deepseek_harness.errors import JsonRpcError
    from harness.profile import write_profile
    from pydantic import BaseModel, Field

    class Inspection(BaseModel):
        schema_version: str = Field(alias="schema")
        sourceCommit: str
        sessionId: str
        tools: list[str]
        initialized: bool

    received = []

    class Provider(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.command)
            self.send_response(503)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    endpoint = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=endpoint.serve_forever, daemon=True)
    thread.start()
    temp_root = ROOT / ".runtime/tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="harness-real-handshake-", dir=temp_root) as directory:
            folder = Path(directory)
            workspace, home = folder / "workspace", folder / "dsh-home"
            workspace.mkdir()
            profile = write_profile(
                profile_root=home, source_dir=source, cwd=workspace, session_id="smoke-main",
                system_prompt="Synthetic initialization verification.",
                provider={"baseURL": f"http://127.0.0.1:{endpoint.server_port}", "model": "smoke", "reasoningEffort": "off"},
                tools=[],
            )
            env = {
                "PATH": f"{node.parent}:/usr/bin:/bin", "TMPDIR": str(folder),
                "DSH_HOME": str(home), "DSH_TELEMETRY_DISABLED": "true",
                "TSX_TSCONFIG_PATH": str(source / "tsconfig.json"),
                "WORKSPACE_MODEL_API_KEY": "synthetic-loopback-only",
            }
            arguments = [str(node), "--import", str(source / "node_modules/tsx/dist/esm/index.mjs"), str(source / "apps/cli/src/bin.ts")]
            launcher = folder / "dsh"
            launcher.write_text(
                "#!/usr/bin/python3\nimport os, sys\n"
                f"os.execve({str(node)!r}, {arguments!r} + sys.argv[1:], {env!r})\n",
            )
            launcher.chmod(0o700)
            client = HarnessClient(HarnessConfig(
                dsh_bin=str(launcher), profile=profile["profile"], dsh_home=str(home), cwd=str(workspace),
                initialize_timeout_seconds=60, request_timeout_seconds=15, shutdown_timeout_seconds=5,
            ))
            checks = []
            process = None
            try:
                client.start()
                initialized = client.initialize(cwd=str(workspace), provider="deepseek-official", model="smoke", reasoning_effort="off", approval_requests=True)
                process = client._proc
                if process is None or Path(f"/proc/{process.pid}/exe").resolve() != node.resolve():
                    raise AssertionError("Handshake did not come from the configured Node executable")
                checks.extend(["real_node_process", "official_sdk_initialization"])
                inspection = client.request("workspace/inspect", {}, response_model=Inspection)
                if (inspection.schema_version != "workspace-profile/v1" or inspection.tools
                        or not inspection.initialized or inspection.sourceCommit != PIN
                        or inspection.sessionId != "smoke-main"):
                    raise AssertionError("Workspace profile inspection mismatch")
                checks.append("empty_tool_registry")
                try:
                    client.request("session/query", {"sessionId": "smoke-main"}, response_model=Inspection)
                except JsonRpcError as error:
                    if "Unsupported or unauthorized workspace RPC request" not in error.message:
                        raise AssertionError("Unknown RPC failed for an unexpected reason") from error
                    checks.append("unknown_rpc_rejected")
                else:
                    raise AssertionError("Unreviewed session/query request was accepted")
                try:
                    client.session_prompt("foreign-session", [{"type": "text", "text": "not authorized"}])
                except JsonRpcError as error:
                    if "Unsupported or unauthorized workspace RPC request" not in error.message:
                        raise AssertionError("Foreign session failed for an unexpected reason") from error
                    checks.append("foreign_session_rejected")
                else:
                    raise AssertionError("Foreign session was accepted")
            finally:
                client.close()
            if process is None or process.poll() != 0:
                raise AssertionError("Runtime did not shut down cleanly")
            checks.append("clean_process_shutdown")
            if received:
                raise AssertionError("Initialization unexpectedly contacted the provider")
            checks.append("zero_provider_requests")
            return {
                "status": "PASS", "scope": "real-node-harness-handshake-only", "source_commit": commit,
                "node": str(node), "launch_arguments": arguments + profile["args"],
                "real_runtime_started": True, "provider_requests": len(received),
                "checks": [{"name": name, "status": "PASS"} for name in checks],
                "server_info": initialized.serverInfo.model_dump(),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "limitations": ["No model inference or tool execution", "No default executor change, service restart, or deployment"],
            }
    finally:
        endpoint.shutdown()
        endpoint.server_close()
        thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--node", type=Path, default=TOOLCHAIN / f"node-v{NODE_VERSION}-linux-x64/bin/node")
    args = parser.parse_args()
    try:
        report = verify(args.source.resolve(), args.node.resolve())
    except Exception as error:
        report = {"status": "FAIL", "scope": "real-node-harness-handshake-only", "error_type": type(error).__name__, "error": str(error)}
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
