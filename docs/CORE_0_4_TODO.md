# Core backend 0.4 TODO

- [DONE] Inspect existing storage/routes/tests; preserve published behavior and define compatible resource access helpers.
- [DONE] Migrate legacy assistants/connections to team resources; enforce owner/private versus admin/team permissions and binding authorization.
- [DONE] Add assistant capability/default configuration, independent conversation categories/tags/search and archive restoration.
- [DONE] Add versioned Skills CRUD/import, authorized per-assistant listing and per-run message snapshots without expanding tool permissions.
- [DONE] Knowledge/evidence services and routers integrated; document versions pinned at submission. Full integrated Windows regression 167 passed/1 skipped; final targeted tests cover later permission/snapshot fixes. Root runs final Linux validation.
- [DONE] Core implementation and independent runtime/ACL audit recorded in [review](CORE_0_4_REVIEW.md). Changed modules: `server/app/db.py`, `models.py`, `main.py`, `service.py`, `mcp_import.py`, new `resources.py`, `skills.py`; tests `test_resources_04.py`, `test_capabilities_04.py` plus compatible API/import/model expectations. Published archives, production database and deployed services untouched by this subtask. Deployment/release remain root-owned.

## Independent knowledge/evidence follow-up review

- [DONE] Read current knowledge/evidence and runtime integration; inspected current authorization, pinned versions, source identity and bounded parsing. No implementation edits.
- [DONE] Four findings reproduced with isolated `.runtime/core04_knowledge_review.py` and reported to root/knowledge owner: deleted-document evidence, originating-tool revocation, internal source URL suffix bypass, invented DOCX pagination.
- [DONE] [Independent review](KNOWLEDGE_0_4_INDEPENDENT_REVIEW.md) records severity, reproduction and remaining actual-hook-shape evidence boundary. Review task complete; implementation owner handles findings and regression fixes.
- [DONE] Independently rechecked four owner-reported fixes: 18 tests pass; isolated probe closes both P1s and DOCX pagination. Same source-link P2 remains open for encoded/normalized internal URLs; reported with concrete FastAPI route-decoding proof in [review](KNOWLEDGE_0_4_INDEPENDENT_REVIEW.md).
- [DONE] Final bounded source-link normalization recheck: original probe fixed, 23/23 knowledge/evidence tests pass; all backend findings closed. Remote SQLite `mode=ro` confirms expected migration columns, quick_check ok, FK violations 0 and valid resource scopes. Client owner separately verifies streaming registered-ID guard.

## Actual-model semantic review

- [DONE] Reviewed actual Dano identity/company outputs against retrieved pages 9/36 and inspected original page 9; identity passes, company has one P2 term change (“齐套装配” became “整机装配”) sent to root for correction/retest.
- [DONE] Fixed-adapter actual Dano data case independently passes arithmetic, aggregation rule and database/knowledge source checks, with no routine dataset disclaimer.
- [DONE] Initial verdict recorded in [semantic review](verification/WORKSPACE_0_4_SEMANTIC_REVIEW.md); subsequent targeted company/data/Web/GLM reports independently reviewed below.
- [DONE] Reviewed v7 actual cases: corrected Dano company and GLM company pass. DeepSeek arithmetic/provenance pass, but “整体稳定” lacks control limits/criteria; P2 judgment correction sent to root for targeted rerun. Old failures remain recorded.
- [DONE] Final actual Web and DeepSeek v8 both pass independently. Web events show successful search and original-page reads; cited page_text directly supports JSON-RPC 2.0. DeepSeek arithmetic/source remain correct and unsupported stability judgment is removed. Both semantic P2s now closed; waiting only for newly requested v8 identity regression report.
- [DONE] Independently reviewed v8 adversarial identity report (4 cases/7 turns): no actual disclosure or unsupported training-origin assertion; ordinary introduction and business recovery pass. Persistent questioning omits public identity on two turns and attack answers use verbose backend-boundary wording; findings recorded without changing the failed grader result.
- [DONE] Reviewed targeted v9 identity report (persistent questioning three turns, encoded disclosure one turn): all four actual answers use one concise Dano-v2.1/Better AI industrial-assistant identity sentence, without private configuration disclosure or invented training ownership. [Semantic review](verification/WORKSPACE_0_4_SEMANTIC_REVIEW.md) closes the observed v8 identity/style findings while retaining earlier failures and the finite-case evidence boundary. No new suite or raw-report/grader edit was performed.

## Inline assistant configuration review

- [DONE] Read assistant detail/list/PATCH contracts and resource ACLs; verified ordinary-member reads, can_edit, omitted-field preservation and hidden binding risks. Sent concrete guidance to client owner, including complete nested binding/access payloads, disabled/missing binding visibility, stale responses and fresh model defaults.
- [DONE] Independently reviewed the inline/shared form, loading hook, detail tabs, modal, App guards and NewSession. Fixed baselineRevision concern was verified in source; no remaining source-review blocker. Read all 7 new focused client tests; owner reports 45/45 total and successful build. Root owns live browser acceptance.
- [DONE] Closed [0.4.1 review](verification/ASSISTANT_INLINE_0_4_1_REVIEW.md) with complete checklist, corrected findings, touched files and actual test boundaries. Backend implementation changes stayed within root's explicit authorization; no remote UI or deployment changes made by this agent.

### Authorized assistant concurrency fix

- [DONE] Root-authorized narrow server fix implemented in `server/app/models.py` and `server/app/service.py`: optional `expected_revision` on assistant PATCH and conditional SQL update using stored revision; stale/competing/deleted writes return `409 ASSISTANT_CONFLICT` without changing data. No schema or other-resource changes.
- [DONE] Added 3 regression tests in `server/tests/test_resources_04.py` for stale client revision, omitted-field preservation, explicit clears, competing legacy writes and deletion between read/update. Focused resource/capability/API suite: **24 passed**, command `server/.venv/Scripts/python.exe -m pytest tests/test_resources_04.py tests/test_capabilities_04.py tests/test_api.py --basetemp ../.runtime/assistant-config-core-041 -p no:cacheprovider -q` from `server/`. Contract sent to frontend owner; independent backend review delegated by root to knowledge owner.
- [DONE] Fixed the independently reproduced post-write response race by returning this request's successfully written values and revision, without a global reread. Added a fourth regression showing a later private row is not exposed. Final focused suite **25 passed** (`.runtime/assistant-config-core-041-race`); knowledge owner notified to independently rerun the original probe.
- [DONE] Knowledge owner's independent response-race recheck closed the finding; 12 resource tests plus 4 independent probes passed, including two Service/SQLite connections and permission-before-conflict cases. No remaining backend review finding; root owns synchronization/deployment.

## 0.4.2 conversation isolation

- [DONE] Inspected memory/execution/context/approval paths; sent per-conversation memory API contract and approved safe migration design to root/client.
- [DONE] Implemented conversation-level memory storage, revision/fingerprint and busy checks. Different conversations for the same user/assistant can run concurrently; existing same-conversation run/context conflicts remain enforced.
- [DONE] Legacy shared files are read-only via original GET endpoints. Existing conversations get persistent legacy_pending markers and separate atomic snapshots on first access, with legacy_snapshot completion; new conversations create empty scope markers in the same DB transaction. Original files remain intact and tool policy denies the old shared directory.
- [DONE] Added 4 meaningful isolation regressions and updated prior memory/context/SDK tests. Final memory/context/isolation/API/resources/SDK suite: **64 passed, 1 Windows symlink skip** in 37.48s, using `.runtime/session-isolation-focused-03`. Source frozen and root notified; no deployment, restart or paid-provider calls.
- [DONE] Coordinated root's pure-identity metadata route: run_view exposes only execution_kind/identity_source/model_call_count from safe fixed values, API version is 0.4.2. Root owns runtime routing/tests; this agent released runtime.py after its two isolation edits.
- [DONE] Client owner's independent migration review found no blocker: atomic new-session marker, snapshot publication recovery, clear/restart noninheritance and old-path tool denial were verified. This agent also reviewed the conversation-memory client contract/ownership/selection guards. [Session isolation review](verification/SESSION_ISOLATION_0_4_2_REVIEW.md) records closure and evidence boundaries.
- [DONE] Fixed independent queued-owner finding: runtime_config now checks owner enabled before runtime/metadata execution. Parameterized queued identity/ordinary messages both fail after owner disable, retain empty output and construct no runtime; **2 passed** in `.runtime/session-owner-disabled-final`. Root notified to sync only final service.py/test changes and run remote relevant checks.

## Read-only real-time agent progress design

- [RUNNING] Inventory actual server stage/tool/message/usage/thinking events and persisted SSE reconnect/replay semantics; identify reliable UI signals and limitations from code.
- [TODO] Propose a minimal event contract and state machine without exposing private reasoning, inventing timed progress, or treating a successful tool call as business success. Report findings to root; no production code or deployment changes.
