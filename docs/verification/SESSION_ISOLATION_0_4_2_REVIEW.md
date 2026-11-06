# Conversation isolation implementation and review (0.4.2)

Date: 2026-11-05. Owner: core agent. Runtime identity routing and deployment are separate root-owned changes.

## Implemented boundary

Each conversation already had a separate SDK actor, working directory, SDK configuration directory, native session ID, event stream, run IDs and approval IDs. The remaining user/assistant-wide memory directory and busy checks have now been replaced by conversation-scoped memory, revisions and context invalidation. Two conversations using one assistant may execute concurrently within the existing global active-session capacity. A single conversation still permits only one active task, and a context measurement blocks mutations/execution only for that conversation.

The native memory directory is `conversation-memory/<user_id>/<conversation_id>`. Runtime fingerprints include the conversation ID and its memory revision. File tools may access only that conversation's workspace and memory directory; another conversation's directory and the old shared memory directory are rejected. Changing A's memory invalidates only A's cached context and runtime fingerprint. It does not delete messages, attachments or existing native context.

Changed implementation: `server/app/db.py`, `memory.py`, `service.py`, two isolation points in `runtime.py`, and the API version in `main.py`. Root separately owns the runtime pure-identity route. `run_view` exposes only its fixed public metadata fields (`execution_kind`, `identity_source`, `model_call_count`), never private execution metadata.

## API contract

The new base is `/api/v1/conversations/{conversation_id}/memory`, authorized by the conversation owner, including archived conversations:

- GET base: `conversation_id`, `assistant_id`, `scope="conversation"`, `origin`, `read_only=false`, `revision`, `busy`, `files`, size limits.
- GET `/file?path=...`: existing `path`, `content`, `revision`, `updated_at` shape.
- PUT `/file`: `{path, content, revision}`; existing revision conflict and size/path checks remain.
- DELETE `/file?path=...&revision=N`: delete one file.
- DELETE base `?revision=N`: clear only this conversation's memory.

Original `/assistants/{assistant_id}/memory` and `/file` GET endpoints remain caller-owned historical exports, with `scope="legacy_user_assistant"` and `read_only=true`. PUT/DELETE operations return `403 MEMORY_LEGACY_READ_ONLY`. New sessions do not read or copy these exports.

## Persistent migration and recovery

1. Database startup creates `conversation_memory_scopes` and backfills only conversations missing a scope row with `origin="legacy_pending"`.
2. New conversation creation inserts its `origin="empty"` row in the same transaction as the conversation. A crash cannot leave an officially created new conversation that later inherits shared memory.
3. First access to an old pending conversation takes the DB transaction/lock, validates the bounded Markdown source set and copies it into a private temporary sibling directory. Publishing the directory uses one atomic rename; source files are untouched.
4. The final snapshot digest and `origin="legacy_snapshot"` are persisted. Subsequent reads or restarts never repeat the copy, including after the user clears all files.
5. If the process stops after directory publication but before the marker commits, the next access validates and adopts that existing snapshot instead of overwriting it. An incomplete unpublished temporary directory is never treated as a completed snapshot.
6. Oversized or invalid sources fail for the affected pending conversation, preserving the original and the pending marker for repair/retry. They do not prevent the service from starting or block a newly created empty conversation. The same existing 128 KiB/file, 1 MiB/scope, 100-file, Markdown/path/symlink restrictions apply.

This migration preserves prior conversation history, which may already contain text from the formerly shared memory. It isolates subsequent files and execution; it does not retroactively remove facts from native history. Historical absolute shared-memory paths remain denied by the current tool policy.

## Verification

Updated `test_memory.py`, `test_context.py` and the native SDK adapter directory assertion. Added `test_session_isolation_042.py` with parallel approvals/cancellation, same-conversation rejection, independent content/revision/fingerprint/context caches, context-probe exclusion, persistent upgrade/restart behavior, publication-before-marker recovery, empty new sessions and bounded-source failure/retry.

Final focused regression: **64 passed, 1 skipped** in 37.48s. The skip is Windows' inability to create a test symlink; the preexisting guard remains tested on systems that support it. Command from the repository root:

```powershell
server/.venv/Scripts/python.exe -m pytest server/tests/test_memory.py server/tests/test_context.py server/tests/test_session_isolation_042.py server/tests/test_api.py server/tests/test_resources_04.py server/tests/test_sdk_adapter.py --basetemp .runtime/session-isolation-focused-03 -p no:cacheprovider -q
```

Independently source-reviewed the client's `memory.ts`, `MemoryPage.tsx` and App integration: memory routes use conversation IDs, returned indexes must match both scope and ID, owned session lists include archived records, per-session workspace keys reject stale reads, and dirty/saving guards cover selection, navigation and blank newly created files. No blocker was found in that client review. The client owner's independent backend migration review also found no blocker: it confirmed the atomic new-conversation marker, snapshot adoption after publication-before-commit, preservation after clear/restart, and old-directory tool denial.

The independent runtime review did find that an owner disabled after queue submission could still receive a pure-identity response; the same missing runtime_config check affected ordinary execution. `runtime_config` now rechecks the current user's enabled flag and raises `USER_DISABLED` before either path runs. A parameterized queue regression prepares a request, disables its owner, then submits it: both identity and ordinary cases finish failed, with empty assistant text and no constructed runtime. The final targeted rerun passed **2/2** (`.runtime/session-owner-disabled-final`). This narrow correction happened after the 64-test run above; root was notified to synchronize the final service/test and run relevant remote checks.

The implementation and independent review are closed with no remaining finding in this scope. Shared memory originals and existing native histories are retained; no absolute all-workload concurrency guarantee is implied by these bounded tests.

All core-agent tests use local isolated fixtures or the injected fake runtime. Root owns the Linux full suite, backup/migration-copy probe, deployed API checks and actual-provider concurrency run. This agent performed no deployed-service changes or paid model calls.
