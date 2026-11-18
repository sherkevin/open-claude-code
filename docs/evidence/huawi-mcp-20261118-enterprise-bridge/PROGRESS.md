# Engineering progress

## 2026-11-18

### Workspace and baseline

- Worktree: `/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118`
- Branch: `feat/huawi-enterprise-bridge-20261118`
- Baseline: `476ff14f9a4663512925e5b14ba29a5729effdce`
- The shared main worktree was not switched, reset, cleaned or overwritten.
- Root filesystem had only about 43 MiB free. All Go/Python caches, temporary
  files, runtime data and build artifacts were directed under
  `/media/data3/dengkw/`.

### Implemented

- Added enterprise integration tables and administrator APIs for create, list,
  inspect, rotate, expiry, revoke and connector/source binding.
- Added one-time issuance of the enterprise integration credential K and a
  standard `mcpServers` JSON document accepted by the existing Kaiwu import
  parser.
- Added the public MCP Edge with K validation, source authorization, Edge-owned
  MCP session mapping, incremental SSE forwarding and a revocation recheck
  during open streams.
- Added one dedicated official `frps` instance per connector, plus the cloud
  visitor, database-backed source routes and a revocation-aware supervisor.
- Kept relay token R and per-source STCP token T derived from the existing
  control-plane root. They are not cached in the integration database.
- Reworked the local authproxy so SSE is forwarded event by event and
  multi-line `data` is joined, filtered once and emitted once.
- Added a fixed Huawei HMAC offline vector, explicit rejection tests for wrong
  key/window/forged identity headers, and removed the unsupported 16-byte key
  minimum.
- Added Windows machine-scoped DPAPI files, strict data-directory ACL checks,
  upgrade backup, legacy plaintext migration and service restart coverage.
- Added deployment templates and updated old connector wording so the obsolete
  `AT_user`/OAuth Token Exchange prerequisite no longer blocks this scope.

### Verification completed

- `go test ./...`: passed for the complete connector module.
- `go vet ./...`: passed with no findings.
- Kaiwu bridge/control/enterprise/import/resource Python tests: `45 passed`, with one
  Starlette/httpx deprecation warning from the test dependency.
- `git diff --check`: passed.
- Synthetic end-to-end run used real official `frps` 0.61.1, visitor, connector
  core and Edge on loopback addresses.
- Latest evidence: `e2e-synthetic-latest.json` (rerun against release commit
  `8c889fe` after the build-script changes).
- Latest run directory:
  `.runtime/huawi-enterprise-e2e-20261118T113707Z`.
- Total synthetic run time: `25.222` seconds.
- Import/discovery returned only `lookup_order`; the complete MCP lifecycle
  returned `SYNTHETIC-ORDER-42`.
- Wrong K returned 401, a source outside the integration returned 403, and an
  unapproved tool returned 403.
- Wrong relay token made frpc exit with code 1; wrong STCP key and wrong proxy
  name could not connect.
- SSE first event arrived in about `0.186` seconds. A normal relay stop failed
  closed with 503 in about `0.184` seconds. Killing the dedicated frps child
  caused about `0.101` seconds of downtime and automatic recovery in about
  `2.691` seconds.
- Revocation stopped the dedicated instance in about `0.502` seconds; Edge then
  returned 401, both frps/visitor ports were closed, and the visitor route row
  was released.
- Secret scans of synthetic logs, the control database and the Windows-style
  `frpc.toml` found no K, customer bearer, HMAC key, R or T. The TOML retains
  official `.Envs` placeholders only.

### Explicitly not completed

- The user paused Windows remote testing before the Windows Server 2019
  graphical install/service/reboot/upgrade/uninstall acceptance pass.
- Simulated network outage was not executed; normal stop, hard child kill and
  relay restart were executed.
- No real public DNS, certificate, cloud Edge or production service was
  changed or deployed by this development pass.
- No actual Kaiwu web conversation was run; only the existing import parser and
  MCP lifecycle were exercised.
- No customer Windows machine, real customer business MCP or real customer data
  was used.
- The task requested `tencent-cloud/deepseek-v4.1-flash-1017`, but the active
  runner reported `deepseek/deepseek-flash`. The requested model identifier is
  not claimed as the actual runtime.

### Final review corrections

- Tunnel recovery now checks the official frpc loopback status API and requires
  every configured proxy to report `running` before moving back to `READY`.
- The dedicated bridge systemd template sets both `WorkingDirectory=/opt/kaiwu`
  and `PYTHONPATH=/opt/kaiwu`, and the module entrypoint was executed with
  `--help` to verify Python module resolution.
- Connector revocation now deletes its visitor route rows in the same
  transaction as integration revocation, allowing the bounded visitor port
  range to be reused instead of leaking one port per retired connector.

### Release completion

- Added `windows-smoke.ps1` to the Windows ZIP and made release metadata
  reproducible across independent source checkouts.
- Release source commit: `8c889fec818be8ec4a440a86d742134f9f77927a`.
- Final package directory:
  `/media/data3/dengkw/releases/huawi-connector-1.3.0-8c889fe`.
- Linux package SHA-256:
  `407a371efa6f8acf071a0830f753a81c045250e86fe04eaac22325f08ee41509`.
- Windows package SHA-256:
  `e9cc49f1dfffeb68ba5061a47648f30ef74df1fd1d332fbb3ab86a784ff1f6ae`.
- Two builds from independent clean source directories produced byte-identical
  Linux and Windows archives and identical outer `SHA256SUMS`.
- The final release and test details are in `RELEASE_REPORT.md` and
  `TEST_REPORT.md`.

Final per-test status is in `ACCEPTANCE_MATRIX.md`. Code completion does not
imply Windows Server 2019, real public infrastructure or customer business
acceptance.
