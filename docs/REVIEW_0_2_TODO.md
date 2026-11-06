# Independent 0.2.0 release review

2026-11-05. Scope: local source review only; no remote writes or model calls. Published0.1.0 artifacts remain unchanged. Root owns real provider/browser/deployment acceptance.

1. [DONE] Reviewed memory, context and runtime changes. Found refresh concurrency race below; root owns correction. Confirmed actual pinned SDK0.2.178 uses detached read tasks, so same-task anyio deadlines do not exit a persistently open SDK TaskGroup scope.
2. [DONE] Reviewed MCP import, client memory/context UI and managed preview/release helpers. Import remains transactional/admin-only/no automatic grants; final archive guard refuses overwrite and binds readiness to exact version. Client unsaved draft navigation issue below.
3. [DONE] Local deterministic two-thread initial-memory-read reproduction returned one success and one IntegrityError; no model or remote requests. Source review is not evidence of real memory loading, compaction or systemd recovery.
4. [DONE] Findings and local evidence communicated. Root owns their fixes and final native provider/browser/deployment acceptance; this review is complete, not a release sign-off.

## Findings communicated to root

- P2: `server/app/memory.py` refresh computed digest and selected/inserted scope row without one enclosing lock. Concurrent first reads could both see no row and the second INSERT raised IntegrityError; concurrent existing reads could spuriously increment revision. Reproduced using two threads and a barrier immediately after the empty SELECT. Root confirmed and is applying an RLock around refresh plus consistent view/read snapshots.
- P2: `client/src/MemoryPage.tsx` assistant selector unmounts the keyed MemoryWorkspace while unsaved content is dirty. Its beforeunload handler only protects browser navigation, not switching assistants or main SPA navigation. Root notified; no source change by reviewer.
- P2: `server/app/runtime.py` finish unconditionally overwrites manual compaction metadata on cancellation/interruption/failure, even if a real compact_boundary already occurred and only subsequent token counting is pending. A local synthetic lifecycle check persisted context.compacted, then finished cancelled; cached_context reported cancelled despite the persisted boundary. Preserve the fact that compaction occurred and distinguish interrupted follow-up/statistics from an unperformed compaction.

## Remaining evidence boundary

- Root is correcting the context timeout/task-identity failure found by Linux regression; this review checked actual installed SDK lifecycle internals, not a provider request.
- Real native memory initial loading under client_composed, cross-conversation recall and clear, native compact boundary and continuation, and systemd behavior require root/acceptance-agent evidence.
- Suggested a precise synthetic sentinel check for user-controlled compact instructions containing an `@absolute/path`; SDK source alone does not reveal the CLI slash/attachment dispatch order, so this is a verification question, not a claimed vulnerability.
- Acceptance helper's unavailable-context assertion was stricter than the current partial-values contract; acceptance agent notified to reconcile without further model calls.
- No additional blocking finding from reviewed MCP import, isolated memory authorization, manual compaction result states, release input selection or project-specific unit control.

## Focused correction re-review

5. [DONE] Read root's fixes and focused assertions. Memory refresh/view/read share the Database RLock, including digest and revision initialization, compatible with callers already holding transaction lock. Compaction records boundary_observed immediately and finish also checks persisted context.compacted; it keeps completed/pre_tokens and separately reports followup_status for failed/cancelled/interrupted follow-up. Existing tests cover both boundary states under all three exceptional endings. No duplicate full regression or provider calls by reviewer.
6. [DONE: BOUNDED STATIC CHECK] Pinned bundled CLI contains its client_composed contract and identifies compact as a local command loaded from a separate chunk. The actual attachment/command dispatch ordering is not available as reliable readable source in the executable; stopped static exploration. Defer to synthetic sentinel acceptance or explicit instruction-field validation, and do not call this a confirmed exposure.
7. [DONE] Final read-only re-review completed. Assistant switching preserves the dirty workspace until explicit discard; App protects main navigation, new work, history selection and logout. Context dialog, run label and events distinguish completed compaction from interrupted follow-up. Root reports17client tests and7/7actual finalUI checks; reviewer did not repeat tests. Selected handoff inputs all exist, selected Markdown relative links resolve, package/lock versions are0.2.0, and FINAL verification/README/API/operations descriptions agree on native evidence and pilot limits.

## Final review disposition

All three reported P2 findings are closed by inspected changes and root's focused/actual verification. No material outstanding issue was found in the final requested scope. Native memory/compact, four restart cases, post-restart recall and both synthetic @path checks are recorded as actual root/acceptance evidence in `DELIVERY_VERIFICATION.md`; this reviewer did not rerun them. Actual archive/source bytes and remote hashes remain root's final packaging check. No source files or published artifacts were changed by this review.
