# HippoRAG recovery and quality implementation plan

**Goal:** Make the optional official backend cancellable and recoverable before
evaluating real extraction and multimodal retrieval on public industrial data.

**Architecture:** Keep the existing source-binding adapter. Seal only indexes
created by a trusted host; recover a verified copy into a fresh private directory
before loading any upstream pickle. Run blocking upstream work in dedicated
processes with bounded JSON transport, deadlines and actual process cleanup.
The default retrieval mode and shared services remain unchanged.

**Implementation order:**

1. Add sealing and trusted recovery to `server/app/hipporag.py`, with tamper,
   identity, source-set, duplicate-source and permission tests in the existing
   adapter suite. A receipt held by the trusted caller is the trust anchor, not
   an index's self-declared hash. Closing and flush failures cannot publish trust.
2. Add a thin process host in `server/app/hipporag_process.py`, not another graph
   algorithm. Use a fresh child process, clean explicit environment, JSON rather
   than unpickling worker responses, bounded outputs and actual termination on
   timeout/cancellation. Test hung work, queued cancellation, abnormal exit,
   malformed responses, cleanup and successful recovery with no double indexing.
3. Integrate cancellation and safe fallback labels with the knowledge service
   only after checking the per-file baseline against parallel Harness work.
   Preserve post-retrieval and pre-citation authorization/version/image checks.
4. Validate real pinned upstream build, seal, restart, restore and retrieval
   using synthetic models first. Only after priority 1 is green, use the existing
   authorized model routes and public licensed sources for extraction, multi-hop
   and image-semantic measurements. Report fixture size, annotations, failures,
   model identities, token usage and evidence limits, without invented gains.
5. Preserve the exact upstream MIT notice at `server/LICENSE.HippoRAG`, explain
   dependency license boundaries, and verify inclusion in a local test wheel;
   do not distribute or deploy it. Update requirements and evidence after tests.

**Alternatives rejected:** Thread-only timeouts leave work running; reopening
arbitrary caches can execute pickle content; hashing a cache against a digest
stored only inside that same cache provides no independent trust; enabling the
official backend globally before quality and operational acceptance violates
the current rollout boundary.

**Work ownership:** Adapter/recovery and process host have separate agent-owned
files. The project owner handles knowledge integration, notices, evidence and
final regression. Do not edit Harness implementation or shared runtime config.
If another writer changes a planned shared file, report before modifying it.

**Constraints:** No production deployment, shared-service restart, default-route
switch, real customer data or unauthorized paid calls. Temporary data, caches,
virtual environments and weights must remain under `/media/data3/dengkw`.
An unchanged private receipt is a trusted-host assumption, not protection from
an attacker who controls the same operating-system account or the host process.

## Execution checkpoint

- Steps 1-3: implemented and independently reviewed. Focused contracts cover
  receipt tampering, unsafe file trees, worker cleanup, queued cancellation,
  browser disconnect, revocation, versions, image evidence and explicit fallback.
- Step 4 lifecycle: six checks passed using the real pinned upstream algorithm
  and synthetic models, including interruption at the model boundary and recovery
  without reindexing. This does not establish extraction quality.
- Step 4 quality: the first two authorized public-data runs remain incomplete
  evidence (response truncation, then upstream numpy-string incompatibility).
  Fixes passed offline contracts and a real-E5/official-algorithm compatibility
  gate before the third bounded run. That run completes index/search/sealing:
  official evidence coverage is 8/8, dense 7/8 and image-rank fusion 7/8. Image
  localization is still 0/6 and relation errors remain; this is a diagnostic,
  not delivery acceptance. See `../evidence/HIPPORAG_PUBLIC_2026-11-10.md`.
- Step 4 recovery supplement: the third run's actual sealed index restores to a
  verified fresh copy using real factories, with no new supplier calls, search
  or reindexing. All source bytes/modes/owners and the external receipt remain
  unchanged; the dedicated worker is reaped.
- Step 5: exact upstream MIT and notice files are included in a local test wheel.
  Full transitive dependency distribution review remains open.
- Full regression: the first run had 970 passes and one failure caused by a
  long test temporary path exceeding the existing memory API's 240-character
  query limit. All ten memory tests pass with a short data-disk temporary path;
  neither API constraints nor assertions changed. The final single-source-snapshot
  run passes all 1,140 server tests with no failures, errors or skips. Client
  123 tests and TypeScript checks also pass. Superseded runs are retained, not
  merged into this count; no Vite production build was performed.

No deployment, shared-service restart, default-retrieval change or Harness
implementation edit is part of these checkpoints.
