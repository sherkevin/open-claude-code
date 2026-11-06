# Server 0.2.0 verification — 2026-11-05

Tencent Linux/Python3.10 full regression: **74 passed**, 10.88 seconds, with the existing upstream Starlette TestClient deprecation warning. The Linux run includes the symlink isolation test skipped on Windows. Run from `server/`: `../.venv/bin/python -m pytest -q -p no:cacheprovider`.

The suite covers authentication, owner access, bounded uploads, encrypted MCP credentials, host/tool restrictions, real HTTP MCP handshake/discovery, idempotency, approvals, cancellation, event persistence, provider error mapping, model configuration precedence, shared native-memory file isolation and quotas, optimistic revisions, concurrent scope initialization, native context counting/cache ownership, compaction evidence and follow-up interruption states, and per-turn literal input. SDK doubles in unit tests verify application behavior; they do not stand in for real inference.

Independent deployed-product acceptance also passed:

- Actual provider manual compaction emitted a native boundary, retained the same native session, preserved product history/SSE and recalled the synthetic marker afterward.
- Actual native memory Write, natural recall from a different session using native Read, and clear/new-session absence passed.
- Four controlled restart cases (graceful/crash, pending approval/slow read) passed26checks over8real tasks. Old approvals cannot execute; tasks are not automatically replayed; follow-up tasks succeed.
- The previously compacted session recalled its marker after those restarts using the same SDK session and zero tools.
- Ordinary input and compaction-instruction synthetic @path checks observed no outside-workspace sentinel in answers or raw SDK events.
- The client independently exercised real MCP retrieval and new-session memory recall.

See `../docs/DELIVERY_VERIFICATION.md` for the release-wide evidence and `../docs/verification/NATIVE_CONTEXT_MEMORY.md` for exact native measurement boundaries. The installed SDK is0.2.178 and the tested provider model isdeepseek-v4.1-flash-1017. Unknown provider fees remain unknown.

Earlier0.1.0 results (30server tests,9API checks and9lifecycle checks) remain historical; current claims above were tested separately. Customer MCP/data correctness, load qualification, automatic-compaction threshold stress, container execution and image interpretation are not claimed by these tests.
