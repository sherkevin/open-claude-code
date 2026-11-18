# Acceptance matrix, 2026-11-18

## Scope and evidence levels

This matrix separates four different claims:

1. **Code implemented**: the source and configuration exist in this worktree.
2. **Synthetic verification**: the code ran locally with fixed synthetic data.
3. **Target Windows verified**: the package ran on the target Windows Server.
4. **Real public/customer verified**: production DNS, certificates, relay and the
   customer's real MCP were used.

The current pass reaches levels 1 and, for most technical checks, level 2. It
does not claim level 3 or 4. The user explicitly paused Windows remote testing,
and no production service, domain or customer environment was changed.

## Environment

| Item | Value |
| --- | --- |
| Worktree | `/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118` |
| Branch | `feat/huawi-enterprise-bridge-20261118` |
| Baseline | `476ff14f9a4663512925e5b14ba29a5729effdce` |
| Go | `go1.27.4` |
| Python test environment | `/media/data3/dengkw/projects/open-claude-code/.venv/bin/python` |
| FRP | official `frps`/`frpc` 0.61.1 |
| Synthetic evidence | `e2e-synthetic-latest.json` |
| Synthetic runtime directory | `.runtime/huawi-enterprise-e2e-20261118T112608Z` |

## Results

| ID | Check | Status | Evidence and boundary |
| --- | --- | --- | --- |
| T01 | Standard JSON import | **Pass, parser level** | `backend import/discovery` accepted the generated `mcpServers` shape and discovered only `lookup_order`. This used the existing import parser, not a browser login to the production Kaiwu web UI. |
| T02 | MCP lifecycle | **Pass, synthetic** | `initialize` -> `notifications/initialized` -> `tools/list` -> `tools/call` returned `SYNTHETIC-ORDER-42` through the service path. |
| T03 | Complete tunnel path | **Pass, loopback synthetic** | The run used the official `frps`, cloud visitor, connector core and Edge. Stopping the relay failed closed with 503; this was loopback, not real public DNS/TLS. |
| T04 | Huawei signature compatibility | **Pass, offline vector** | `TestOfflineVectorAndShortProtocolValidKey` verifies the fixed vector. Wrong key, expired time, future time and forged identity headers are rejected in `TestOfflineRejectsWrongKeyExpiredWindowAndForgedIdentity`. Customer-side server clock/window behavior remains unverified. |
| T05 | SSE behavior | **Pass, automated** | `TestProxyStreamsFirstSSEEventBeforeUpstreamClose` observes the first event before upstream close. `TestProxyMergesMultilineSSEDataOnce` verifies one payload after multiline data. Bounded readers and limits are covered by the proxy implementation/tests; no long-duration production load profile was run. |
| T06 | Session and tenant isolation | **Pass, synthetic** | Tests cover different integration credentials, cross-source rejection, Edge session binding, and two concurrent integrations returning their own synthetic results without crossing. |
| T07 | Authorization and revocation | **Pass, synthetic** | Wrong/expired/revoked K, cross-source access, connector revocation, and credential rotation are covered. Revocation stopped the dedicated bridge in `0.501` seconds; Edge returned 401, frps/visitor ports closed, and the control-plane visitor route was released. |
| T08 | Source/tool scope | **Pass, synthetic** | `tools/list` is filtered, unapproved `tools/call` returns 403, the allowlist is preserved on reimport, and the existing D01 duplicate-import protection remains intact. |
| T09 | Disconnect and recovery | **Partial** | Normal stop -> 503 in `0.164` seconds; dedicated frps hard kill -> about `0.101` seconds unavailable and about `2.745` seconds automatic recovery; relay restart -> initialize 200. Recovery now also requires every frpc proxy to report `running`. A simulated network partition was not executed. |
| T10 | Secret and log hygiene | **Partial** | The synthetic scan covered logs, the control database and the Windows-style `frpc.toml`; no K, customer bearer, HMAC key, R or T was found, and the TOML is secret-free. Actual Windows event/log inspection is not complete because Windows testing was paused. |
| T11 | Windows Server 2019 acceptance | **Not executed** | The package includes cross-compiled Windows binary, DPAPI/ACL code, unit tests and PowerShell smoke scripts, but no Windows Server 2019 graphical install, service reboot, upgrade-migration, or uninstall result from this pass. |
| T12 | Actual Kaiwu web conversation | **Not executed** | The existing import parser and MCP protocol lifecycle were exercised, but no logged-in Kaiwu browser session was available/used, and the actual platform conversation was not run. |
| T13 | Customer business acceptance | **Not executed** | No customer Windows machine, real business MCP, customer bearer/HMAC values or real business data were used. Customer validation remains a separate event. |

## Commands used in this pass

```text
GOCACHE=/media/data3/dengkw/.cache/go-build/huawi-20261118 \
GOTMPDIR=/media/data3/dengkw/.tmp/huawi-20261118 \
TMPDIR=/media/data3/dengkw/.tmp/huawi-20261118 \
/media/data3/dengkw/toolchains/go1.27.4/bin/go test ./...

GOCACHE=/media/data3/dengkw/.cache/go-build/huawi-20261118 \
GOTMPDIR=/media/data3/dengkw/.tmp/huawi-20261118 \
TMPDIR=/media/data3/dengkw/.tmp/huawi-20261118 \
/media/data3/dengkw/toolchains/go1.27.4/bin/go vet ./...

TMPDIR=/media/data3/dengkw/.tmp/huawi-20261118 \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.cache/pycache/huawi-20261118 \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python \
  -m pytest -q server/tests/test_kaiwu_bridge.py \
  server/tests/test_kaiwu_control.py \
  server/tests/test_kaiwu_enterprise.py \
  server/tests/test_mcp_import.py \
  server/tests/test_resources_04.py

git diff --check
```

## Minimum remaining actions

1. On a Windows Server 2019 machine with the customer-like synthetic MCP,
   install as Administrator, verify DPAPI files and ACLs, run the real
   initialize/list/call path, restart the service and machine, upgrade/reinstall,
   then uninstall.
2. Deploy a test public relay/Edge with controlled DNS and a certificate, run
   the import through the actual Kaiwu account, and verify a real conversation
   call against the synthetic MCP.
3. Only after the customer supplies authorized local values, repeat the same
   flow against the real HwMain MCP and record a separate customer business
   acceptance result.
