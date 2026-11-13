# Local model catalog evidence — 2026-11-07

## Scope

This record covers the school-cloud local development service only. It does not
authorize production deployment, restart, customer data, or paid-model
acceptance.

## Finding

The local service had no `AGENT_MODEL_CATALOG`. The server therefore used its
single-model compatibility fallback and the client received only `legacy-model`
(`默认模型`). The local TokenHub bridge did expose metadata for the requested
Tencent routes, but metadata inspection did not send an inference request.

## Local change

Created the ignored, owner-only runtime file
`.runtime/config/model-catalog.json` with four public entries:

- `dano-v2.1` — enabled with `light/high/max` mapped to the confirmed
  DeepSeek-V4.1-Flash-1017 route.
- `deepseek-v4.1-flash` — enabled.
- `hy4` — enabled.
- `kimi-k3.1-pro` — enabled.

The default for new conversations is `dano-v2.1`. The existing synthetic
`业务演示助手` was updated to use `dano-v2.1/high`. The file is mode `0600` and
is not committed.

The user-facing text `deepseek-v4.1flash-1007` does not match the authorized
local/provider metadata, project records, or current bridge route, which all
identify `deepseek-v4.1-flash-1017`. The local catalog does not fabricate a
1007 route; an exact 1007 mapping still requires an authorized endpoint or an
explicit confirmation that 1017 is intended.

## Verification

After a local-only server restart, authenticated `GET /api/v1/models` returned
four entries, all marked available, with Dano's three reasoning levels. The
health check remained ready and no model inference or customer data was used.
