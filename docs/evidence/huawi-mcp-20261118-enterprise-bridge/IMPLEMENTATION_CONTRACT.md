# M0 implementation contract

- Date: 2026-11-18 (Asia/Shanghai)
- Worktree: `/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118`
- Branch: `feat/huawi-enterprise-bridge-20261118`
- Baseline commit: `476ff14f9a4663512925e5b14ba29a5729effdce`
- Task-specified model: `tencent-cloud/deepseek-v4.1-flash-1017`
- Observed runner model in this execution: `deepseek/deepseek-flash` (the runner did not report the task-specified provider/model identifier; do not claim the requested model was actually used)
- Main shared worktree was not switched, reset, cleaned or overwritten.

## Public paths

| Surface | Path |
| --- | --- |
| Control plane | `/api/kaiwu/v1/...` |
| Public Edge | `/projects/kaiwu/mcp/sources/{source_id}/mcp` |
| Local connector UI | `127.0.0.1:17872` by default |

The Edge public base is configured by `AGENT_KAIWU_EDGE_PUBLIC_BASE`. It must
be HTTPS except for loopback synthetic tests, and its path must exactly match
`AGENT_KAIWU_EDGE_MOUNT_PATH`.

## Enterprise integration

1. An administrator creates an integration for one connector and one or more
   registered source IDs.
2. The server verifies tenant ownership, connector state and exact source
   membership before issuing K.
3. K is returned only in the create/rotate response together with the standard
   `mcpServers` JSON. The database stores only SHA-256 verification material and
   a short display prefix.
4. Rotation replaces the verification hash atomically. Old K is invalid on the
   next request.
5. Revocation and connector revocation make all associated requests fail. A
   connector revocation also revokes all integrations and releases its visitor
   route rows in the same transaction, so the bounded port range is reusable.
6. The Edge checks K, expiry, connector/source state and the Edge-owned session
   binding on every request. K is never forwarded upstream.

## Dedicated FRP instance

One connector uses one official `frps` instance:

- `frps`, Windows `frpc` and cloud visitor use the same derived relay token R.
- Every source uses one derived STCP token T and one loopback visitor port.
- The visitor listens only on `AGENT_KAIWU_EDGE_VISITOR_HOST`.
- The Edge may connect only to that configured visitor host and the route stored
  in the control database.
- The bridge supervisor stops frps and the visitor after connector revocation
  and exits successfully so systemd `Restart=on-failure` does not revive it.
- The single-instance token is transport authentication, not a multi-tenant
  per-source authorization system.
- `frpc_running` means only that the child process exists. `tunnel_connected`
  is derived from the official frpc loopback status API and requires every
  configured proxy to report phase `running`; the UI does not treat process
  liveness as proof that the relay is connected.

## MCP behavior

- `initialize`, `notifications/initialized`, `tools/list` and `ping` use the
  customer Bearer.
- `tools/call` additionally receives fresh lower-case HMAC-SHA256 headers.
- The HMAC key is accepted when non-empty and line-free; the old undocumented
  16-byte minimum is removed.
- `tools/list` is filtered to the customer-approved allowlist.
- `tools/call` is denied locally unless the tool is approved.
- SSE is read event by event and flushed after each event. Multi-line `data`
  lines are joined, filtered once and emitted once.
- The customer URL is fixed at import time; Edge and cloud state cannot supply
  an arbitrary local target.

## Secrets

- Windows DPAPI stores device identity, device private key, customer Bearer,
  HMAC key and auth profiles. The Windows frpc TOML contains only official
  `.Envs` placeholders for relay/STCP material; R and T are passed through the
  child process environment and are never persisted in that file.
- Data-directory ACL must contain only SYSTEM and Administrators; failure
  aborts installation.
- Legacy plaintext JSON is migrated atomically on first read where DPAPI is
  available.
- K, B, S, R and T must not appear in ordinary logs, Git, or exported platform
  JSON except that K is intentionally returned once in the create/rotate
  response.
