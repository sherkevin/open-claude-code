# Remote development handoff — Better AI / open-claude-code

**Date:** 2026-11-06 (Asia/Shanghai)  
**Primary developer:** remote Codex agent on Tencent Cloud  
**Project path:** `/data/dengkw/workspace/open-claude-code`  
**Source of truth for product requirements:** [REQUIREMENTS_ALIGNMENT.md](REQUIREMENTS_ALIGNMENT.md)

## 1. Why this handoff exists

The user wants all subsequent development of this project to happen directly on the Tencent Cloud host, in `/data/dengkw/workspace/open-claude-code`. This document transfers the context that previously lived in a local Codex thread.

The local thread was working on requirements alignment. It produced the requirements document but stopped before the final per-requirement audit and regression pass. The local Codex resume failure was a Codex provider mismatch (`openai` thread vs. current `magpie` provider), not a product-code failure. Do not change the Codex application configuration on the server because of that local issue.

## 2. Current environment facts

- Host: Tencent Cloud `VM-0-8-ubuntu`, user `ubuntu`, host alias `tencent` from the local workstation.
- Project: `/data/dengkw/workspace/open-claude-code`.
- Production services are running under systemd:
  - `zhixing-5400d3fe73-server.service`
  - `zhixing-5400d3fe73-client.service`
  - `zhixing-5400d3fe73-mcp.service`
- Server listens on `127.0.0.1:18080`; client preview uses `127.0.0.1:18081`; the demo MCP uses `127.0.0.1:8767/mcp`.
- Production data is under `.runtime/data/`; private model catalog is `.runtime/model-catalog.json`; service environment is `.runtime/service.env`; admin password is `.runtime/admin-password`.
- Python `3.10.12`, Node `22.14.0`, npm `10.9.2`, git `2.34.1`, rsync `3.2.7`.
- The project `.venv` already exists and is the intended Python environment.
- The parent `/data/dengkw/workspace` is a Git repository, but its `.git/index` is not readable by `ubuntu`. Do not rely on the parent repository for development. Use file backups or a project-local repository only after confirming with the user.
- The root file `l2-spec.md` is owned by `root` and was not readable through the normal `ubuntu` session during this handoff. If you can read it, treat it as a separate L2 specification; do not overwrite it. Ask the user if it conflicts with this document.

## 3. What was synced on 2026-11-06

The local project was incrementally synced into the remote project. The sync intentionally did not delete remote-only files and did not copy `.runtime/`, `.venv/`, `node_modules/`, build outputs, private keys, or test artifacts.

- 27 files had content changes; 17 overwritten files were backed up under `.runtime/sync-backups/20261106-remote-handoff/`.
- Key-file SHA-256 values were checked against the local source and matched.
- Production services stayed active and `GET /api/v1/health/ready` returned `{"status":"ready","model_configured":true}`.
- Remote baseline regression passed: server `322 passed`; client `53 passed`; production client build succeeded.
- `client/node_modules` was owned by `root` and its `.bin` entries were not executable by `ubuntu`; ownership and execute bits were repaired with the available passwordless `sudo`. Re-run `npm test` and `npm run build` if the environment is rebuilt.
- The client build briefly replaced the live Nginx root `client/dist`. The exact 0.4.2 build was restored from `dist/releases/0.4.2/zhixing-client-0.4.2.zip`, all four hashes were checked against `.runtime/product-0.4.2/remote-final.json`, and the new build was backed up under `.runtime/sync-backups/20261106-remote-handoff/new-client-dist/`.

Key documentation now present on the remote:

- `docs/REQUIREMENTS_ALIGNMENT.md` — the new requirements baseline.
- `docs/API_CONTRACT.md` — includes the public catalog entries for `hy4` (Hunyuan 4) and `kimi-k3.1-pro` (Kimi 3.1 PRO).
- `docs/CONFIGURABLE_WORKSPACE.md` — updated public model description.
- `docs/design/RAG_MULTIMODAL_GRAPH.md` — design only, not production.
- `docs/design/SUBAGENT_ORCHESTRATION.md` — design only, not production.
- `docs/ENGINEER_TODO.md` and `TODO.md` — updated status and remote handoff entries.

## 4. Required work, in order

### 4.1 Read-only audit first

Read these before changing code:

1. `docs/REQUIREMENTS_ALIGNMENT.md`
2. `docs/ENGINEER_TODO.md`
3. `TODO.md`
4. `docs/API_CONTRACT.md`
5. `docs/CONFIGURABLE_WORKSPACE.md`
6. `README.md`
7. The relevant source: `server/app/`, `client/src/`, `server/tests/`, `client/tests/`, `scripts/`.

Check production health without restarting anything:

```bash
curl -fsS http://127.0.0.1:18080/api/v1/health/ready
systemctl is-active zhixing-5400d3fe73-server.service
systemctl is-active zhixing-5400d3fe73-client.service
systemctl is-active zhixing-5400d3fe73-mcp.service
```

### 4.2 Produce the per-requirement audit

Create `docs/verification/REQUIREMENTS_AUDIT_NOTES.md`.

For every requirement ID in `docs/REQUIREMENTS_ALIGNMENT.md` (`ID-*`, `SES-*`, `MOD-*`, `MCP-*`, `SKL-*`, `WEB-*`, `RAG-*`, `CTX-*`), record:

- requirement ID and short title;
- status: `已实现`, `部分实现`, `设计中`, `待确认`, or `未实现`;
- code path(s) that implement it;
- test or command that verifies it;
- observed result (use exact counts/output where possible);
- remaining gap, owner, or evidence boundary.

Rules:

- Do not call a design-only item “已实现”. `RAG-005`, the multimodal knowledge graph, and production Sub-agent orchestration are currently design-only.
- Do not claim real-customer acceptance without customer MCP addresses, data samples, credentials, and a baseline. The current evidence is local/remote technical verification.
- If a document claim cannot be reproduced, mark it as a gap and fix the document or code; do not silently lower the requirement.

### 4.3 Fix inconsistencies

If the audit finds a mismatch, make the smallest safe change:

- code change: add or update a test;
- API change: update `docs/API_CONTRACT.md`;
- behavior change: update the requirements document status and the TODO;
- never change production data to make a test pass.

### 4.4 Regression

Run the full server and client checks from the project root:

```bash
cd /data/dengkw/workspace/open-claude-code/server
../.venv/bin/python -m pytest -q
../.venv/bin/python -m compileall app

cd /data/dengkw/workspace/open-claude-code/client
npm test
npx tsc -b
npx vite build --outDir /home/ubuntu/client-dist-build-$(date +%Y%m%d-%H%M%S) --emptyOutDir
```

If the existing `.venv` is missing a test dependency, install only the missing test extras into the existing environment; do not recreate the environment over production data. Record exact commands, test counts, failures, and environment versions in the audit note.

Do **not** run `npm run build` directly in `client/`: the production Nginx configuration serves `client/dist` live. Build to a separate `/home/ubuntu/client-dist-build-*` directory until a deployment is explicitly approved. If a temporary in-place build is unavoidable, back up `client/dist` first and restore it immediately after collecting the result.

### 4.5 Close the TODO

Update `docs/ENGINEER_TODO.md` and `TODO.md`:

- mark the requirements audit and regression as `DONE` only with evidence links;
- leave deployment and production restart as separate `TODO` items requiring user approval;
- record the audit artifact path and test commands.

### 4.6 Deployment is separate

Do not restart or redeploy the running systemd services as part of the audit. If a deployment is needed, first provide:

- changed files and behavior;
- backup location and restore command;
- test evidence;
- service restart procedure and rollback procedure;
- expected downtime and active-run handling.

Wait for explicit user approval before restarting production.

## 5. Safety and permission rules

- Never overwrite or delete `.runtime/data/`, `.runtime/backups/`, `.runtime/model-catalog.json`, `.runtime/service.env`, `.runtime/admin-password`, or `.venv/`.
- Never commit or display model API keys, MCP credentials, admin passwords, or private provider URLs.
- Do not run `rm -rf` on the project root. Use timestamped backups.
- Do not rebuild into `client/dist`; it is the live Nginx root. Use a separate build directory or restore the previous build after verification.
- Do not modify the root-owned `l2-spec.md`; read it only if permitted.
- Keep production services running while editing source; source changes take effect only after an explicit restart.
- Any real-model test that spends money or calls the customer’s MCP requires explicit user confirmation first.

## 6. Evidence boundaries

These remain open and must not be described as delivered:

- real Huawei/客户 MCP endpoints, credentials, tools, and business samples;
- real customer acceptance and baseline comparison (quality, completion time, cost per successful task);
- Hunyuan 4 and Kimi 3.1 PRO actual TokenHub activation and quota;
- production load, long-running stability, and disaster recovery drill;
- container deployment acceptance;
- multimodal knowledge graph and production Sub-agent fan-out;
- any guarantee that a prompt can resist all future prompt-injection attempts.

## 7. First response to the user

Start with a short status message:

> 我已接手，先做只读审计：读取需求文档和现有代码，生成逐项审计表，再跑 server/client 回归；不会重启生产服务，也不会改动 `.runtime/data`。

Then proceed with the audit.
