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
