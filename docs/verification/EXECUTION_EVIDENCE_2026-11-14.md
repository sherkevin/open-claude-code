# Execution evidence and timing review — 2026-11-14

This change belongs to the existing chat UI and runtime. It does not implement an ERP, a new frontend, or a completed four-layer business router.

## Customer requirement mapping

The controlled Huawi source matrix maps T-01/T-02 to request-level data, cost and concurrency observability, T-03 to bounded fields, T-07 to a proposed knowledge/SOP/tool funnel, and T-11 to proposed measurement criteria rather than customer quotes. Original recordings remain outside the repository. Task scope, golden answers and the customer baseline are not yet agreed.

The knowledge hierarchy (external knowledge, internal knowledge, SOP, tools) and business query planning (domain, process, object, metric) are different structures. Neither is proved by emitting named UI stages.

## Corrected behavior

- No unconditional `scoping`/`routing` events in SDK or Harness execution. A tool attempt precedes approval and does not imply network transmission.
- A successful call requires strict boolean true; failed or unconfirmed results do not present stale business facts or sources. Unknown evidence has no success check.
- The result card identifies the tool and reports bounded tool-supplied facts. Unknown canonical keys do not hide available aliases. This is reporting, not scope enforcement.
- Run-wide usage appears once. Ledger totals, observed subtotals, missing usage and real zero values are distinct. Non-success runs and legacy flat counters remain subtotals. Estimated cost is not a provider invoice.
- Event timings are `first_server_progress_ms`, `first_tool_attempt_ms`, `first_tool_success_ms`, `first_text_ms` and existing runtime/model/total measurements. They do not measure browser paint or verified business correctness. Legacy ambiguous fields are not reused.
- Timing and triggering events commit atomically. Concurrent provider and tool updates use the same database lock; first values remain stable.
- Offline reporting consumes explicitly selected schema-v1 trace exports. See [the measurement contract](run-measurements.md). Missing data, failures and nonterminal runs stay visible.

## Validation

- Frontend: 180 tests in 26 files, TypeScript and production build passed. Existing bundle-size warning remains.
- Final timing/statistics set: 55 tests passed. Local HTTP MCP and event-loop isolation were also exercised in a 96-test combined run before the final statistics-only changes; those statistics were subsequently rechecked. Counts overlap.
- A wider suite retained one Linux `/proc` memory-sampling failure on macOS (115 passed, 1 failed). This is not a claim that the full server suite passes.
- Local 1440×1000 CSS / DPR2 screenshot was visually reviewed and stored in the controlled project evidence directory. It uses production components with synthetic inputs, not a customer session. No altered image is represented as original evidence.

## Not yet accepted

Customer MCP and field contracts, actual scope/paging/aggregation enforcement, complete context-token breakdown, customer p50/p95, browser first feedback, validated first result, partial retry/refresh and production acceptance remain open. No customer savings or ROI is asserted by this patch.

## Follow-up: bounded MCP requests and duplicate representation

The assistant editor now supports optional per-tool argument constraints, enforced by the shared authorization path. Existing discovered parameter names must be used; the host does not invent fields, fill defaults or silently trim requests. Constraint/schema changes invalidate prepared tasks and persisted workflow approval snapshots. This is request enforcement, not validation of the returned business data. See [the contract and limits](../business-tool-argument-constraints.md).

Harness no longer appends a second complete JSON copy into model-visible text. Warnings, structured content and sources remain; [projection evidence](mcp-payload-projection.md) records the exact boundary and same-payload byte measurements.

- Client: 190 tests in 27 files; production build passed. Existing bundle-size warning remains.
- Constraints/API/workflow/Harness regression group: 125 passed. Tests neutralized only the test process's ambient model overrides; no application settings or provider accounts changed.
- Strict payload and actual local HTTP-to-plugin replay: 28 passed. These counts are not a full server suite or a live customer acceptance run.
- Browser component replay: malformed JSON and valid unapplied edits block saving; apply/save, discard and remove/save preserve explicit intent and revision checks. Save uses a browser-only fake response. Desktop viewport 1440×1000 CSS, DPR2; two native 2880×2000 screenshots individually reviewed. A 390px viewport check found no document-level horizontal overflow; this is not full mobile acceptance.
- Independent review corrected complex constraint validation, undeclared object-constant fields and missing schema digests during approval recovery.

Release blocker retained: existing workflow `mcp_read` selection equates allowed/auto-approved with read-only. The new request constraints do not establish read-only behavior. Resolve this before enabling production workflow tool execution; no production rollout was performed here.
