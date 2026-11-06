# 0.4 core migration and permission audit

Reviewed 2026-11-05, before deployment. This is a source/test review; it does not claim actual customer MCP, load or Linux deployment acceptance.

## Implemented boundaries

- Existing connections and assistants migrate to team scope without altering original instructions, bindings or history. New API-created resources default to private ownership; clients cannot provide owner IDs. SQLite changes are additive/idempotent.
- Members create/edit their own private resources and use authorized team resources. Administrators manage team resources but do not inherit access to another member's private credentials, instructions or knowledge. Shared assistants cannot bind private dependencies. Scope changes cannot leave incompatible assistant dependencies.
- Conversations remain owner-scoped and bound immutably to one assistant. Search covers titles, categories, tags and message text with escaped literal wildcards; results are bounded. Rename/archive/restore preserve messages and native session state.
- Skills are stored as versioned instructions. SKILL.md name, description and declared tools import without executing package code. Declarations never expand actual tools. Selected revisions are snapshotted on submission and removal from the assistant or disablement blocks use.
- New assistants inherit only explicitly designated default team knowledge bases; explicit empty selection opts out. Knowledge document versions are pinned at submission. Bound MCP revisions are pinned without credentials; a queued task rejects a subsequent endpoint/credential change.
- Existing runtime model routing/GLM history guard, native memory, compaction, approvals and cancellation remain intact.

## Findings resolved during review

1. Runtime workspace callbacks used the live assistant rather than the submitted capability snapshot. Root changed tool visibility and callbacks to the snapshot, intersected by live permissions, and refreshed the user record. Regression tests exercise newly added KB/Web capability exclusion.
2. SDK MCP handler failures used wire `isError`, while the installed Python SDK reads handler `is_error`. Root corrected the handler contract; regression test verifies denied calls become errors.
3. Skill removal after submission could still inject the old selected Skill. Core runtime configuration now rejects an absent current binding.
4. MCP endpoint/credential changes between submission and runtime creation could silently retarget a task. Core snapshots connection revisions and rejects changed revisions before execution.
5. Full source excerpts embedded in persisted citation metadata could bypass later source-detail revocation. Knowledge/evidence owner reduced message citations to reference metadata; full details remain behind source authorization.
6. An existing trace test searched for the bare digits `1017`, which accidentally matched a timestamp. It now checks the actual complete upstream model identifier and retains the private-route check.

## Verification

- Full Windows server suite at integrated baseline: **167 passed, 1 skipped**, one existing Starlette/httpx deprecation warning. Command: `server/.venv/Scripts/python.exe -m pytest server/tests -q --basetemp=.runtime/core04-full-final -p no:cacheprovider`.
- Subsequent focused checks cover Skill frontmatter tool declarations with no permission expansion, removed Skill rejection, snapshot callbacks, SDK error result semantics, literal tag search, and queued MCP revision changes. Root should run the complete final Linux suite after all concurrent changes stabilize.
- Core tests: [resource and migration coverage](../server/tests/test_resources_04.py), [workspace callback coverage](../server/tests/test_capabilities_04.py). Existing import/API/model test expectations changed only for authorized member configuration and the flaky identifier substring check.

No unresolved blocker found in reviewed core migration/ACL paths. Actual-provider workspace MCP behavior, OCR/vector provisioning, browser workflows, operational restart and release packaging remain root acceptance work. The existing per-user/assistant memory serialization remains; simultaneous sessions sharing that memory are not claimed to execute concurrently.
