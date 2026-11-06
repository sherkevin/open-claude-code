# Independent review: session isolation and identity 0.4.2

- [DONE] Review the product metadata runtime branch and API tests for permission/cancellation boundaries, task classification and truthful zero usage.
- [DONE] Review the live isolation smoke runner for actual overlapping execution evidence and cleanup isolation.
- [DONE] Additional core-agent request: review memory migration publication/restart, empty-session creation and legacy path denial.
- [DONE] Record material findings, verification evidence and remaining limits. Backend/runner source was not edited by this reviewer; no deployment or live model call.

Reviewer: client subagent, independent read-only source review requested by root.

## Finding resolved during review

**P2 — a queued identity request still completed after its owner was disabled.** The metadata branch in [runtime.py](../../server/app/runtime.py) rechecked `runtime_config`, but that function obtained the current user without checking `enabled`; assistant/resource checks did not enforce it either. An isolated fixture reproduced `prepare_run → disable owner → submit actor → succeeded/product_metadata` with a nonempty assistant response. Revoked authentication prevented further API access, so this did not expose another user's data; it nevertheless violated the execution authorization boundary.

Reported to root and core. Core added a `USER_DISABLED` check in [service.py](../../server/app/service.py) and a queued-owner regression for both metadata and business tasks. This reviewer reran the same isolated metadata probe after the fix: status `failed`, disabled-account error, empty assistant response. No remaining blocker found in the reviewed branch.

## Runtime and test conclusions

- Classification is an exact whole-message allowlist in [identity.py](../../server/app/identity.py), with attachment/Skill exclusions and normal execution for mixed tasks, quoted questions and negation. It does not rewrite model answers or classify arbitrary business requests using a broad substring match.
- The response uses the run's frozen public selection. The metadata branch never initializes or executes the SDK, does not fabricate raw SDK events, and leaves native session/cumulative SDK cost intact. `execution_kind=product_metadata`, zero usage and `model_call_count=0` are truthful for this branch; ordinary model runs report an unknown call count rather than inventing one.
- Send/get/cancel continue through the existing owner-authenticated endpoints. Queued cancellation is checked before the metadata branch. Metadata emission has no blocking await/provider operation where a second task could interleave in the single actor event loop; a cancellation arriving after completion correctly leaves the terminal result unchanged.
- The two [identity API tests](../../server/tests/test_identity_metadata.py) verify selected-name changes with retained native history, idempotent replay, zero calls/events/cost, and business/quoted-task execution. They do not constitute live provider acceptance. Independent command: `server/.venv/Scripts/python.exe -m pytest tests/test_identity_metadata.py -q -p no:cacheprovider --basetemp <fresh-workspace-review-directory>` from `server`: **2 passed**. The first attempt used an inaccessible preexisting default pytest temp/cache directory; the isolated retry passed without changing permissions.
- Metadata-only conversations do not incorrectly lock GLM switching in the client: `conversationHasModelHistory` gives explicit `has_native_session=false` precedence over application message count, already covered by the existing model tests.

## Live runner review

[session_isolation_smoke.py](../../scripts/session_isolation_smoke.py) observes A in `awaiting_approval` before starting B, waits for B to succeed and return its private marker, then checks A is **still** awaiting approval. This proves actual overlap rather than merely successful sequential runs. It also verifies duplicate execution in A is rejected, cancelling A does not change B's result, and A can subsequently read its own distinct marker. The markers are supplied through separate memory APIs, not leaked into the read prompts.

The runner grants only the existing fixture's `slow_lookup`, requires approval and never approves it. Cleanup cancels only run IDs created in this invocation, archives only its newly created sessions and disables only its newly created private assistant. It never updates the supplied MCP connection or searches by name to mutate existing resources. Failures preserve safe error types and cleanup results in the explicit private report.

Nonblocking evidence limit: `status=passed` describes the functional checks; cleanup errors are recorded separately and do not change that status. Final acceptance must inspect the cleanup array as well as the top-level status. This reviewer did **not** execute the live runner or spend model tokens; root owns the real report and cleanup verification.

## Memory migration review

[memory.py](../../server/app/memory.py) publishes each legacy snapshot through a temporary sibling directory and atomic rename. A restart after publication but before marker commit reuses the existing snapshot instead of recopying the legacy original. A committed `legacy_snapshot`/`empty` marker skips migration; clearing files does not remove the marker or reintroduce legacy content. New conversations insert their `empty` marker inside the same database transaction as conversation creation. Scope initialization serializes through the database transaction, and legacy originals remain read-only through the old HTTP endpoints.

Native tool authorization accepts only the current conversation workspace or its current conversation memory root; the legacy shared directory is outside both. Existing SDK configuration points `autoMemoryDirectory` and `add_dirs` to that same current memory root. Source review found no overwrite/repopulation issue; core/root own migration tests and real upgrade-copy verification.
