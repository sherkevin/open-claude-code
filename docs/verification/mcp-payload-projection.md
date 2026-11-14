# MCP result representation review

The Harness MCP host previously appended `structuredContent` as a text block even when a complete equivalent JSON text block already existed. The plugin renders all text blocks, so the same payload entered model-visible text twice. It could also push an otherwise acceptable response over the normalized response limit.

`include_structured_text` now appends only when no single existing text block represents the complete structured object. The comparison is typed and strict: duplicate JSON keys and non-finite numbers do not qualify; numbers use decimal parsing to avoid float-rounding equivalence; array order remains significant. The comparison precedes the existing credential sanitizer, which can normalize JSON text. Original text blocks, warnings, structured data and source registration are retained; this is not summarization, row filtering or byte-for-byte raw-response archiving. Structured-only results, including an empty object, still gain a model-visible representation.

The existing 1,000,000-byte transport and normalized response limits remain. Oversized responses fail rather than being truncated. Credentials, authorization, cancellation and source registration remain on the existing host path. The SDK's direct remote MCP path is unchanged.

## Local evidence

- 24 projection cases cover equivalence, precision, invalid JSON, warnings, array order, duplicate rows and structured-only values.
- Actual local HTTP tests exercise JSON and SSE, structured-only/empty payloads, credential cleanup, source registration and response limits.
- The production `workspace_plugin.ts` execute/render functions were exercised with the existing isolated upstream interface fixture. This does not boot the full pinned Harness or call a provider.
- Same-payload counterfactual: ordinary fixture model text changes from 1,389 to 752 UTF-8 bytes; the sanitized credential-echo fixture changes from 1,541 to 828. These are representation byte counts, **not tokens, financial savings or customer benchmark results**. Raw response transmission is not reduced.
- Final strict projection plus HTTP-to-plugin replay: 28 passed. The preceding transport/regression group passed 69 tests; counts overlap and predate the additional extreme-exponent case.

## Required next validation

Customer data and tool contracts are not yet connected. Returned scope/fields, semantic correctness, actual model usage, p50/p95 and complete customer task flows remain unverified. Request constraints and this representation correction are independent capabilities; neither completes the proposed knowledge funnel.
