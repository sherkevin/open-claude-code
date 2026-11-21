#!/usr/bin/env python3
"""Prepared only: isolated Linux 1.3.8 acceptance; never runs without --execute.
Root supplies frozen binary hashes after ROOT_BUILD and cloud deployment.
Imports reviewed acceptance helpers. This delivery executes --local-only;
new-tenant public acceptance is blocked by the verified single-relay deployment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

BASE = Path("/media/data3/dengkw")
CLOUD = "https://flintw.com/api/kaiwu"
EDGE = "https://flintw.com/projects/kaiwu/mcp"
SSH_TARGET = "ubuntu@43.161.249.13"
SSH_KEY = BASE / "t3-code/home/.ssh/kaiwu_hk_other_20261116"
REMOTE_ADMIN_FILE = "/data/dengkw/workspace/kaiwu-enterprise-bridge-20261119/secrets/admin-token"
HERE = Path(__file__).resolve().parent
HARNESS = HERE / "builtin_demo_acceptance_138.py"
HELPER = HERE.parent / "huawi-diagnostics-20261119/windows_diagnostics_acceptance_137.py"


class Stop(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Stop(code)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def confined(path):
    p = Path(path).resolve()
    require(BASE in p.parents, "PATH_OUTSIDE_DATA3")
    return p


def private_json(path, obj):
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def admin_token(args):
    if args.admin_token_file:
        p = confined(args.admin_token_file)
        metadata = p.stat()
        require(stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == 0o600,
                "ADMIN_TOKEN_FILE_MUST_BE_0600")
        value = p.read_text(encoding="utf8").strip()
    else:
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                   "-o", "ConnectTimeout=10", "-o", "UpdateHostKeys=no", "-i", str(confined(args.ssh_key)),
                   SSH_TARGET, "sudo -n cat -- " + REMOTE_ADMIN_FILE]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=20, check=False)
        require(result.returncode == 0, "ADMIN_TOKEN_SSH_READ_FAILED")
        value = result.stdout.decode("utf8").strip()
    require(value.isascii() and 16 <= len(value) <= 1024
            and not any(c.isspace() for c in value), "ADMIN_TOKEN_FORMAT_INVALID")
    return value


def read_private_hashes(data_dir):
    result = {}
    for name in ("secrets.json", "auth_profiles.json"):
        p = data_dir / name
        require(p.is_file(), "SYNTHETIC_SECRET_FILE_MISSING")
        require(stat.S_IMODE(p.stat().st_mode) == 0o600, "CREDENTIAL_FILE_NOT_0600")
        result[name] = sha(p)
    return result


def process_identity(pid):
    proc = Path("/proc") / str(pid)
    raw = (proc / "stat").read_text()
    start_ticks = raw[raw.rfind(")") + 2:].split()[19]
    argv = (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    return {"start_ticks": start_ticks, "argv": [x.decode("utf8") for x in argv],
            "exe": str((proc / "exe").resolve())}


def active_group_members(pgid):
    found = []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            raw = (proc / "stat").read_text()
            fields = raw[raw.rfind(")") + 2:].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                found.append(int(proc.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return found


def group_stopped(pgid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not active_group_members(pgid):
            return True
        time.sleep(0.1)
    return False


def cleanup_saved(args):
    """Explicit, repeatable cleanup of the recorded new test connector only."""
    work = confined(args.work_dir)
    require(work.parent == BASE / "acceptance" and work.name.startswith("huawi-demo138-"),
            "CLEANUP_WORK_SCOPE_REJECTED")
    owner_path = work / "private/live-owner.private.json"
    require(stat.S_IMODE(owner_path.stat().st_mode) == 0o600, "OWNER_RECEIPT_MUST_BE_0600")
    owner = json.loads(owner_path.read_text(encoding="utf8"))
    require(owner.get("owner_schema") == "kaiwu-linux138-isolated-v1"
            and owner.get("work_dir") == str(work), "OWNER_RECEIPT_INVALID")
    tenant, cid, pid = owner["tenant_id"], owner["connector_id"], owner["pid"]
    require(re.fullmatch(r"tenant-kaiwu-builtin138-[0-9a-f]{32}", tenant) is not None
            and re.fullmatch(r"conn_[0-9a-f]{32}", cid) is not None
            and isinstance(pid, int) and pid > 1, "CLEANUP_IDENTITY_INVALID")
    live = (Path("/proc") / str(pid)).exists()
    if live:
        observed = process_identity(pid)
        require(observed["start_ticks"] == owner["start_ticks"]
                and observed["exe"] == owner["connector_exe"]
                and "--data-dir" in observed["argv"], "CLEANUP_PID_IDENTITY_CHANGED")
        index = observed["argv"].index("--data-dir")
        require(observed["argv"][index + 1] == str(work / "data")
                and os.getpgid(pid) == pid, "CLEANUP_PROCESS_SCOPE_CHANGED")
    spec = importlib.util.spec_from_file_location("kaiwu_accept138_cleanup", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.load_helper(HELPER, owner["local_url"])
    helper, token = module.H, admin_token(args)
    client = helper.opener()
    def call(path, method="POST", payload=None):
        status, _, raw = helper.fetch(client, CLOUD + path, method=method,
                                     headers={"Authorization": "Bearer " + token},
                                     body=payload, timeout=45)
        require(status == 200, "CLEANUP_CLOUD_HTTP_" + str(status))
        return helper.decode_json(raw)
    rows = call("/v1/connectors", method="GET").get("connectors", [])
    matches = [row for row in rows if row.get("connector_id") == cid]
    require(len(matches) == 1 and matches[0].get("tenant_id") == tenant,
            "CLEANUP_CLOUD_TENANT_MISMATCH")
    result = {"case": "cleanup_isolated_linux138", "connector_id": cid,
              "tenant_id": tenant, "new_connector_revoked": False,
              "candidate_group_stopped": not live and not active_group_members(pid)}
    try:
        response = call("/v1/connectors/" + cid + "/revoke", payload={})
        result["new_connector_revoked"] = (response.get("revoked") is True
                                            and response.get("tenant_id") == tenant
                                            and response.get("connector_id") == cid)
    finally:
        if live:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline and (Path("/proc") / str(pid)).exists():
                time.sleep(0.25)
            if (Path("/proc") / str(pid)).exists():
                # Recheck identity before a scoped force-stop.
                require(process_identity(pid)["start_ticks"] == owner["start_ticks"],
                        "CLEANUP_PID_IDENTITY_CHANGED")
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            result["candidate_group_stopped"] = group_stopped(pid)
    result["passed"] = result["new_connector_revoked"] and result["candidate_group_stopped"]
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    target = work / ("cleanup-result-" + uuid.uuid4().hex[:8] + ".json")
    private_json(target, result)
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["passed"] else 1


def guard_public_new_tenant(args):
    # Read-only: a template does not imply that per-connector relays can coexist.
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=10",
               "-i", str(confined(args.ssh_key)), SSH_TARGET,
               "systemctl list-units --type=service --state=active,activating "
               "--no-legend --no-pager 'kaiwu-bridge@*.service'"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=20, check=False)
    require(result.returncode == 0, "PUBLIC_BRIDGE_STATE_COULD_NOT_BE_VERIFIED")
    units = re.findall(rb"kaiwu-bridge@[^\s]+\.service", result.stdout)
    require(not units, "PUBLIC_RELAY_ALREADY_OWNED_BY_ACTIVE_BRIDGE")
    # Even with no current unit, this task does not provision a replacement relay.
    raise Stop("PUBLIC_NEW_TENANT_RELAY_BOOTSTRAP_NOT_ENABLED")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--connector", type=Path)
    parser.add_argument("--connector-sha256")
    parser.add_argument("--frpc", type=Path)
    parser.add_argument("--frpc-sha256")
    parser.add_argument("--source-commit")
    parser.add_argument("--keep-running-for-ui", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--admin-token-file", type=Path)
    parser.add_argument("--ssh-key", type=Path, default=SSH_KEY)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"prepared_only": True, "executed": False,
                          "next": "Root reviews ROOT_BUILD, then runs --local-only --execute."}))
        return 0

    os.umask(0o077)
    if args.cleanup:
        try:
            require(not args.keep_running_for_ui, "CLEANUP_AND_KEEP_CONFLICT")
            return cleanup_saved(args)
        except BaseException as exc:
            print(json.dumps({"passed": False, "failure_category": str(exc) if isinstance(exc, Stop)
                              else "CLEANUP_EXECUTION_FAILURE", "exception_type": type(exc).__name__}))
            return 1
    report = {"case": "isolated_linux_builtin138", "passed": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "checks": [],
              "scope": "new synthetic tenant/connector only; excludes Windows and Kaiwu agent UI"}
    process = log = reserved_bad_socket = local = A = H = None
    connector_id = ""
    tenant_id = ""
    admin = ""
    existing_ids = set()
    work = None
    work_created = False
    identity_verified = False
    keep_live = False
    secret_values = []
    cleanup = {}
    exit_code = 1

    try:
        require(os.name == "posix" and sys.platform.startswith("linux"), "LINUX_REQUIRED")
        require(args.connector and args.connector_sha256, "FROZEN_ARTIFACT_ARGUMENTS_REQUIRED")
        require(not (args.local_only and args.keep_running_for_ui), "LOCAL_ONLY_CANNOT_KEEP_PUBLIC_UI")
        if not args.local_only:
            guard_public_new_tenant(args)
            require(args.frpc and args.frpc_sha256, "FROZEN_FRPC_ARGUMENTS_REQUIRED")
        require(re.fullmatch(r"[0-9a-f]{40}", str(args.source_commit)) is not None, "SOURCE_COMMIT_REQUIRED")
        connector = confined(args.connector)
        frpc = None if args.local_only else confined(args.frpc)
        artifacts = [(connector, args.connector_sha256)]
        if frpc is not None:
            artifacts.append((frpc, args.frpc_sha256))
        for p, expected in artifacts:
            require(p.is_file() and os.access(p, os.X_OK), "FROZEN_EXECUTABLE_MISSING")
            require(re.fullmatch(r"[0-9a-f]{64}", expected) is not None and sha(p) == expected,
                    "FROZEN_EXECUTABLE_HASH_MISMATCH")
        work = confined(args.work_dir)
        require(work.parent == BASE / "acceptance"
                and work.name.startswith("huawi-demo138-")
                and args.source_commit[:7] in work.name, "DEDICATED_WORK_DIRECTORY_REQUIRED")
        require(not work.exists(), "WORK_DIRECTORY_MUST_BE_NEW")
        work.mkdir(mode=0o700, parents=True, exist_ok=False)
        work_created = True
        data, private, evidence, tmp = [work / name for name in ("data", "private", "evidence", "tmp")]
        for directory in (data, private, evidence, tmp):
            directory.mkdir(mode=0o700)
        report.update({"work_dir": str(work), "source_commit": args.source_commit,
                       "connector_sha256": sha(connector),
                       "frpc_sha256": sha(frpc) if frpc is not None else None,
                       "local_only": args.local_only, "public_chain_executed": False})
        if args.local_only:
            report["scope"] = "unbound local MCP and synthetic source preservation only; no public/Windows claims"

        spec = importlib.util.spec_from_file_location("kaiwu_accept138", HARNESS)
        A = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(A)
        with socket.socket() as port_reservation:
            port_reservation.bind(("127.0.0.1", 0))
            ui_port = port_reservation.getsockname()[1]
        local_url = "http://127.0.0.1:" + str(ui_port)
        A.load_helper(HELPER, local_url)
        H = A.H
        if not args.local_only:
            admin = admin_token(args)
            secret_values.append(admin)
        public_client = H.opener()

        def cloud(path, payload=None, expected=200, method="POST"):
            status, _, raw = H.fetch(public_client, CLOUD + path, method=method,
                                    headers={"Authorization": "Bearer " + admin},
                                    body=payload, timeout=45)
            require(status == expected, "CLOUD_API_HTTP_" + str(status))
            body = H.decode_json(raw)
            require(isinstance(body, dict), "CLOUD_API_RESPONSE_INVALID")
            return body

        if not args.local_only:
            prior = cloud("/v1/connectors", method="GET")
            require(isinstance(prior.get("connectors"), list), "CONNECTOR_LIST_CONTRACT_INVALID")
            existing_ids = {row["connector_id"] for row in prior["connectors"]}
        env = os.environ.copy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                     "all_proxy", "KAIWU_ADMIN_TOKEN", "AGENT_KAIWU_CONTROL_ADMIN_TOKEN",
                     "AGENT_KAIWU_CONTROL_ADMIN_TOKEN_FILE", "KAIWU_CLOUD_BASE", "KAIWU_EDGE_BASE"):
            env.pop(name, None)
        env.update({"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
                    "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp),
                    "PYTHONDONTWRITEBYTECODE": "1", "XDG_CACHE_HOME": str(tmp / "cache")})
        log = open(private / "connector.log", "xb", buffering=0)
        command = [str(connector), "serve", "--data-dir", str(data), "--listen",
                   "127.0.0.1:" + str(ui_port)]
        if not args.local_only:
            command.extend(["--cloud", CLOUD, "--edge", EDGE, "--frpc", str(frpc)])
        process = subprocess.Popen(
            command,
            cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + 30
        initial = None
        while time.monotonic() < deadline:
            require(process.poll() is None, "CANDIDATE_EXITED_DURING_STARTUP")
            try:
                candidate_local = H.LocalClient()
                initial = candidate_local.get("/api/status")
                local = candidate_local
                break
            except Exception:
                time.sleep(0.25)
        require(local is not None and initial is not None, "LOCAL_UI_START_TIMEOUT")
        require(initial.get("bound") is False, "NEW_INSTANCE_WAS_ALREADY_BOUND")
        secret_values.append(local.token)

        common = ["--local-url", local_url, "--evidence-dir", str(evidence),
                  "--helper", str(HELPER)]
        def case(name, *extra, allow_failure=False):
            completed = subprocess.run([sys.executable, "-B", str(HARNESS), name, *common, *extra],
                                       cwd=work, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, timeout=170, check=False)
            try:
                body = json.loads(completed.stdout)
            except Exception:
                raise Stop("ACCEPTANCE_DRIVER_OUTPUT_INVALID") from None
            ok = completed.returncode == 0 and body.get("passed") is True
            report["checks"].append({"case": name, "passed": ok,
                                     "failure_category": body.get("failure_category")})
            if not allow_failure:
                require(ok, "ACCEPTANCE_CASE_FAILED_" + name)
            return body

        def local_post(path, payload, timeout=45):
            status, _, raw = H.fetch(
                local.client, local_url + path, method="POST",
                headers={"X-Connector-Token": local.token, "Origin": local_url},
                body=payload, timeout=timeout)
            body = H.decode_json(raw)
            require(status == 200 and isinstance(body, dict) and body.get("ok") is True
                    and isinstance(body.get("data"), dict), "LOCAL_API_FAILED_" + path.rsplit("/", 1)[-1])
            return body["data"]

        # This runs before self-test and before creating any other MCP.
        case("local-probe", "--expect-unbound")
        if args.local_only:
            require(initial.get("frpc_running") is False and initial.get("bound") is False,
                    "LOCAL_ONLY_UNEXPECTED_TUNNEL_OR_IDENTITY")
            demo_id = initial.get("builtin_demo", {}).get("source_id", "")
            report["demo_source_id"] = demo_id
        else:
            tenant_id = "tenant-kaiwu-builtin138-" + uuid.uuid4().hex
            enrollment = cloud("/v1/enrollments", {
                "tenant_id": tenant_id, "tenant_name": "Kaiwu 1.3.8 isolated synthetic acceptance",
                "ttl_seconds": 900})
            code = enrollment.pop("code", "")
            require(isinstance(code, str) and code.startswith("kw_"), "ENROLLMENT_CONTRACT_INVALID")
            secret_values.append(code)

            bound = local_post("/api/bind", {"code": code})
            connector_id = bound.get("connector_id", "")
            require(A.CONNECTOR_RE.fullmatch(connector_id) is not None
                    and connector_id not in existing_ids and bound.get("tenant_id") == tenant_id
                    and bound.get("bound") is True, "NEW_CONNECTOR_IDENTITY_MISMATCH")
            identity_verified = True
            report.update({"tenant_id": tenant_id, "connector_id": connector_id})
            case("local-probe")
            demo_id = A.expected_demo_id(connector_id)
            report["demo_source_id"] = demo_id


        # A bound, non-listening socket reserves a port while connections still fail.
        reserved_bad_socket = socket.socket()
        reserved_bad_socket.bind(("127.0.0.1", 0))
        bad_port = reserved_bad_socket.getsockname()[1]
        synthetic_bearer = "synthetic-only-" + secrets.token_urlsafe(24)
        secret_values.append(synthetic_bearer)
        saved = local_post("/api/source/save", {
            "display_name": "isolated intentionally unreachable synthetic source",
            "upstream_url": "http://127.0.0.1:" + str(bad_port) + "/mcp",
            "bearer_token": synthetic_bearer, "dynamic_auth": False,
            "tools": ["synthetic_unreachable_readonly"]})
        bad_id = saved.get("source", {}).get("source_id", "")
        require(A.SOURCE_RE.fullmatch(bad_id) is not None and bad_id != demo_id
                and saved.get("created") is True
                and saved["source"].get("disabled", False) is False, "BAD_SYNTHETIC_SOURCE_NOT_CREATED")
        report["bad_source_id"] = bad_id
        A.inspect_bad_source(local.get("/api/status"), bad_id)
        secret_baseline = read_private_hashes(data)
        case("snapshot")
        baseline = evidence / "builtin138-snapshot-latest.json"

        if args.local_only:
            # These local UI actions must preserve the separately saved bad source.
            local_post("/api/builtin-demo/enabled", {"enabled": False})
            case("demo-state", "--expected-demo-state", "off")
            case("preserved", "--baseline", str(baseline))
            require(read_private_hashes(data) == secret_baseline, "DEMO_OFF_CHANGED_SYNTHETIC_SECRET_FILES")
            local_post("/api/builtin-demo/enabled", {"enabled": True})
            case("demo-state", "--expected-demo-state", "on")
            case("local-probe", "--expect-unbound")
            case("preserved", "--baseline", str(baseline))
            unbound = local_post("/api/self-test", {}, timeout=30)
            stages = {x.get("id"): x.get("status") for x in unbound.get("stages", [])}
            require(unbound.get("ok") is False and not unbound.get("trace_id")
                    and stages == {"local_mcp": "passed", "enterprise_binding": "skipped",
                                   "tunnel": "skipped", "edge_connect": "skipped"},
                    "UNBOUND_SELF_TEST_STATUS_MISMATCH")
            probe = unbound.get("probe") or {}
            require(re.fullmatch(r"[A-Za-z0-9_-]{16,128}", str(probe.get("nonce", ""))) is not None,
                    "UNBOUND_SELF_TEST_NONCE_INVALID")
            report["unbound_self_test"] = {"ok": False, "public_chain_executed": False,
                                          "stages": stages,
                                          "local_probe": A.check_ping(probe, probe["nonce"])}
            after = local.get("/api/status")
            require(after.get("bound") is False and after.get("frpc_running") is False
                    and after.get("tunnel_connected") is False, "LOCAL_ONLY_TUNNEL_STARTED")
            A.inspect_bad_source(after, bad_id)
            case("preserved", "--baseline", str(baseline))
            require(read_private_hashes(data) == secret_baseline, "LOCAL_CHECKS_CHANGED_SYNTHETIC_SECRET_FILES")
            report["synthetic_secret_files_preserved"] = True
        else:
            # Retained public checks; public execution is blocked above until a separately
            # reviewed relay ownership/bootstrap design is supplied.
            tested = None
            for attempt in range(3):
                tested = case("self-test", "--allow-self-test", "--bad-source-id", bad_id, allow_failure=True)
                if tested.get("passed") is True:
                    break
                retryable = (tested.get("failure_category") == "PUBLIC_SELF_TEST_NOT_PASSED"
                             and local.get("/api/status").get("state") == "DEGRADED_RELAY")
                require(retryable and attempt < 2, "REAL_PUBLIC_SELF_TEST_FAILED")
                time.sleep(2)
            report["self_test_trace_id"] = tested["self_test"]["trace_id"]
            report["self_test_probe"] = tested["self_test"]["fresh_probe"]
            require(read_private_hashes(data) == secret_baseline, "SELF_TEST_CHANGED_SYNTHETIC_SECRET_FILES")

            integration = cloud("/v1/integrations", {
                "tenant_id": tenant_id, "connector_id": connector_id,
                "display_name": "Kaiwu builtin 1.3.8 isolated acceptance",
                "source_ids": [demo_id], "ttl_seconds": 3600 if args.keep_running_for_ui else 900}, expected=201)
            key = integration.get("access_token", "")
            require(isinstance(key, str) and len(key) >= 16, "INTEGRATION_CONTRACT_INVALID")
            secret_values.append(key)
            config_path = private / "demo-mcp.private.json"
            private_json(config_path, integration["mcp_config"])
            report["integration_expires_at"] = integration.get("integration", {}).get("expires_at")
            public = case("public-probe", "--private-mcp-json", str(config_path))
            report["public_trace_ids"] = public["public_mcp"]["traces"]
            report["public_fresh_probe"] = public["public_mcp"]["fresh_probe"]
            case("preserved", "--baseline", str(baseline))
            A.inspect_bad_source(local.get("/api/status"), bad_id)
            require(read_private_hashes(data) == secret_baseline, "PUBLIC_PROBE_CHANGED_SYNTHETIC_SECRET_FILES")
            report["synthetic_secret_files_preserved"] = True
            if args.keep_running_for_ui:
                owner = {"owner_schema": "kaiwu-linux138-isolated-v1", "work_dir": str(work),
                         "tenant_id": tenant_id, "connector_id": connector_id, "demo_source_id": demo_id,
                         "bad_source_id": bad_id, "pid": process.pid,
                         "start_ticks": process_identity(process.pid)["start_ticks"],
                         "connector_exe": str(connector), "local_url": local_url,
                         "private_mcp_json": str(config_path),
                         "integration_expires_at": report["integration_expires_at"]}
                private_json(private / "live-owner.private.json", owner)
                report["live_for_ui"] = {key: owner[key] for key in
                                         ("pid", "local_url", "private_mcp_json", "integration_expires_at")}
                report["live_for_ui"]["cleanup_command"] = " ".join(shlex.quote(value) for value in (
                    sys.executable, "-B", str(Path(__file__).resolve()),
                    "--cleanup", "--execute", "--work-dir", str(work)))
                keep_live = True
        report["passed"] = True
        exit_code = 0
    except BaseException as exc:
        report["failure_category"] = str(exc) if isinstance(exc, Stop) else "UNEXPECTED_EXECUTION_FAILURE"
        report["exception_type"] = type(exc).__name__
    finally:
        # A failed bind response may still have redeemed our brand-new tenant.
        # Resolve only that exact synthetic tenant; never guess or use a prior ID.
        if tenant_id and not identity_verified and admin and H is not None:
            try:
                rows = cloud("/v1/connectors", method="GET").get("connectors", [])
                matches = [row for row in rows if row.get("tenant_id") == tenant_id
                           and row.get("connector_id") not in existing_ids]
                if len(matches) == 1 and A.CONNECTOR_RE.fullmatch(matches[0].get("connector_id", "")):
                    connector_id = matches[0]["connector_id"]
                    identity_verified = True
                    report.update({"tenant_id": tenant_id, "connector_id": connector_id})
                elif matches:
                    cleanup["failed_bind_scope_resolved"] = False
            except BaseException:
                cleanup["failed_bind_scope_resolved"] = False
        # The only revoked connector is the verified ID newly created in this run.
        if identity_verified and admin and H is not None and not keep_live:
            try:
                require(A.CONNECTOR_RE.fullmatch(connector_id) is not None
                        and connector_id not in existing_ids and tenant_id.startswith("tenant-kaiwu-builtin138-"),
                        "CLEANUP_SCOPE_REJECTED")
                stopped = cloud("/v1/connectors/" + connector_id + "/revoke", {})
                cleanup["new_connector_revoked"] = (stopped.get("connector_id") == connector_id
                                                    and stopped.get("tenant_id") == tenant_id
                                                    and stopped.get("revoked") is True)
            except BaseException:
                cleanup["new_connector_revoked"] = False
        if process is not None and not keep_live:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
            cleanup["candidate_process_stopped"] = process.poll() is not None
            cleanup["candidate_group_stopped"] = group_stopped(process.pid)
        if log is not None:
            log.close()
        if reserved_bad_socket is not None:
            reserved_bad_socket.close()
        if keep_live:
            cleanup["deferred_for_ui"] = True
        report["cleanup"] = cleanup
        if any(value is False for value in cleanup.values()):
            report["passed"], exit_code = False, 1
            report["cleanup_requires_root_attention"] = True
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(report, ensure_ascii=False)
        if any(value in encoded for value in secret_values if len(value) >= 8):
            report = {"passed": False, "failure_category": "PRIVATE_VALUE_IN_RECEIPT_BLOCKED"}
            exit_code = 1
        if work_created and work is not None and work.is_dir():
            try:
                private_json(work / "acceptance-result.json", report)
            except BaseException:
                report["passed"], exit_code = False, 1
                report["failure_category"] = "RECEIPT_WRITE_FAILED"
        print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
