# Deployment and requirement self-audit

Date: 2026-11-06 (Asia/Shanghai)

This note records an independent fact check of the current local and Tencent Cloud deployment. It separates observed behavior from planned work. It is evidence for the human-readable requirements document and does not replace customer acceptance.

## Observed deployment facts

| Area | Observation | Evidence | Boundary |
|---|---|---|---|
| Tencent server | `zhixing-5400d3fe73-server.service` is active and `/api/v1/health/ready` returned `{"status":"ready","model_configured":true}`. | SSH `systemctl is-active`; loopback health request | Health does not prove future provider quota, uptime, or customer MCP availability. |
| Remote model catalog | `deepseek-v4.1-flash`, `glm-5.3`, and `dano-v2.1` are enabled; Dano points to the private DeepSeek 1017 route and exposes light/high/max; `hy4` (混元 4) and `kimi-k3.1-pro` (Kimi 3.1 PRO) are present but disabled with an explicit unavailable reason. | `/data/dengkw/workspace/open-claude-code/.runtime/model-catalog.json` read over SSH | The two new models are listed but cannot be selected until their upstream routes are configured and enabled. |
| Router | Remote service environment has router enabled, 15 s timeout, 256 max output tokens, and USD 0.02 per recommendation bound. An authenticated request for a Chinese industrial task returned Dano-v2.1/high with `router.mode=provider` and public router ID `deepseek-v4.1-flash`. | Remote authenticated `/api/v1/models/recommend`; `/data/dengkw/workspace/open-claude-code/.runtime/service.env` | This proves one successful route decision, not a quality or cost guarantee for all tasks. |
| Client static assets | Remote `index-BpwKTORa.js` SHA-256 is `fed0ef7359f68f76605a676c3333a8ac53696da292ba17cb541dbe7b94085556`.  It contains the model-selection button and Tab completion text. | `sha256sum` and `grep` over remote `client/dist/assets/index-BpwKTORa.js` | Browser cache can show an older asset until reload. |
| Remote backup | Pre-change backup exists at `.runtime/backups/pre-0.4.3-model-rag-20261105`, including server app, model catalog, client dist, and service env snapshot. | Remote `find` | Backup restore has not been rehearsed in this audit. |
| Local client | Source build assets are `index-BpwKTORa.js` and `index-JXJZVwh7.css`; the installed local client was previously copied from the same build and opened for UI verification. | `client/dist`, installed workbench directory, prior CUA verification | This audit did not repeat an end-to-end model run in the browser. |

## Requirement status check

### Implemented and observed

- Separate client and server deployment model.
- Per-assistant conversations with independent session state and idle recovery.
- Model selection for DeepSeek, GLM, and Dano; Dano light/high/max mapping.
- Public model catalog entries for Hunyuan 4 and Kimi 3.1 PRO with safe unavailable states.
- Explicit `Select the most fitness model` operation with provider-backed DeepSeek routing and bounded local fallback.
- MCP, Skills, knowledge-base and Web Search assistant configuration surfaces.
- Existing hybrid RAG path: permission-checked knowledge search, immutable document versions, local lexical/vector retrieval, run snapshots, and source citations.
- Three source classes in the evidence layer: business data, knowledge files, and web sources.
- Live execution progress based on persisted events, approvals, tool results, cancellation and reconnect states.
- Tab intent completion is local and only accepted when the user presses Tab.

### Partially implemented or gated

- Hunyuan 4 and Kimi 3.1 PRO are visible in the catalog but are not enabled in Tencent because no authorized upstream route was configured in the audited catalog.
- Multimodal knowledge graph: architecture and acceptance design exist in `docs/design/RAG_MULTIMODAL_GRAPH.md`; the graph index, visual retrieval layer, and graph UI are not claimed as production behavior.
- Autonomous Sub-agent fan-out: design and isolation contract exist in `docs/design/SUBAGENT_ORCHESTRATION.md`; production parent-child task execution is not claimed.
- RAG quality: retrieval and citations are implemented, but customer-specific precision/recall, latency and cost baselines are not measured.
- Automatic provider billing continuity: router bounds are configured, but no software can guarantee future balance, network uptime or provider availability.

## Evidence boundary

The audit confirms source/configuration/deployment state and one real recommendation response. It does not establish customer MCP correctness, production load capacity, universal identity-prompt resistance, multimodal graph quality, or Sub-agent task quality. Those require named fixtures, customer data contracts, and acceptance runs.

