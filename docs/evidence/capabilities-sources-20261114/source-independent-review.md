# Independent source-binding review

- Environment: school cloud only.
- Project: open-claude-code (Zhixing Workbench).
- Reviewer: `/root/independent_source_review` (real collaboration agent; no Claude Code ST assigned).
- Baseline: `f8f1f86`.
- Reviewed commit: `0dceb4be89f29bd290d2e0cd728d4f6c542103b5`.
- Verdict: **PASS for the source-binding change and integration into main.** No unresolved blocking finding in this scoped change. Full merged regression remains the project owner's gate; this is not a deployment or whole-product acceptance.

## Independently reproduced baseline defects

The independent `test_source_contract.py` outside the repository reproduced four failures on baseline: actual workspace MCP text-block envelope yielded no source IDs; Harness dropped reused source IDs; >30 earlier sources displaced a later full-page read; final citations lacked candidate-versus-reference semantics. Evidence: `main-result.txt` (4 failed, 12.15 seconds).

A further review found that disabled users were not refreshed in `EvidenceService._authorize`, so internal completion/finalization could still attach evidence after account disablement. Evidence: `disabled-result.txt` (1 failed, 6.09 seconds). This has been fixed and retested.

During implementation review, the new original-page action initially rejected non-web registry sources even though the dialog exposed the action for MCP data sources. The final commit restores authenticated, validated original-link navigation for those source types and adds a regression test.

## Final implementation review

- SDK post hooks now consume the real workspace MCP `content[type=text].text` envelope through the common bounded parser; failed and oversized envelopes do not mint evidence.
- Harness attributes only IDs returned by the exact completed tool result, instead of a global database-before/after difference. Reused and concurrent sources are handled without cross-attribution.
- The run source ledger reads successful completion events in pages, deduplicates identifiers, and no longer truncates the whole run to its earliest 30 IDs. Individual tool-result/event limits remain in place.
- Explicit references, read-but-not-explicitly-cited pages, and search candidates have distinct `attribution` values. A search candidate is not presented as proof of the answer.
- Both transports, finalization, and original-source access reauthorize registry IDs; disabled accounts and current assistant/resource revocation are enforced. Foreign-run IDs are rejected.
- Original URLs remain out of static message citations. Every original-page action performs a fresh no-store authorized source lookup, validates the returned HTTP(S) URL, and closes the reserved window on error.
- Internal knowledge/data citations remain supported; frontend card labels survive canonical footer removal. Source dialog resets stale content on ID changes.
- `git diff --check f8f1f86..0dceb4b` passes; final implementation worktree is clean.

## Tests personally executed

1. Stable commit backend chain tests plus independent contract tests: **21 passed in 149.94 seconds**. Command: `PYTHONPATH=server /media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest server/tests/test_web_source_binding.py /media/data3/dengkw/tmp/oc-independent-source-review/test_source_contract.py -q --basetemp=/media/data3/dengkw/tmp/oc-independent-source-review/pytest-source-0dceb4b`. Log: `source-0dceb4b-result.txt`.
2. Stable commit frontend source tests: **9 passed**, one test file. Log: `source-client-0dceb4b-result.txt`. A temporary node_modules symlink to existing dependencies was removed after the run; no dependency install or implementation edit was made.
3. An earlier independent five-contract run also passed (36.46 seconds): `final-contract-result.txt`.
4. A larger supplemental source-related run was **not completed**. After 12 minutes it was blocked in filesystem journal commit (`jbd2_log_wait_commit`); at the owner's request I interrupted only my own pytest process. Its interruption/teardown traceback is in `related-result.txt`. This run is not counted as passing or as a production failure. The owner will run a single full regression after integration to avoid parallel disk contention.

## Visual evidence reviewed

I opened and inspected the implementer's actual isolated-browser screenshots:

- `/media/data3/dengkw/tmp/web-source-browser/desktop.png`: distinct internal reference, read-only external record, and external candidate cards; visible original-page actions.
- `/media/data3/dengkw/tmp/web-source-browser/mobile-revoked.png`: readable 390px layout without visible horizontal overflow, clear candidate disclaimer, and visible source-permission-revoked error.

The browser interaction itself was executed by `/root/source_binding_hardening`; the screenshots were independently visually inspected, not claimed as my own live browser run. They use synthetic fixtures, not customer data or a production deployment.

## Acceptance boundaries

- No real paid model/provider endpoint was called; transport and source results were synthetic.
- No production/shared service was restarted; no Tencent Cloud sync or deployment was performed by this reviewer.
- No credentials or customer data were read or copied.
- Global search/fetch disablement is a future-execution restriction, not deletion of already-acquired evidence. Historical source access still requires current user and assistant/resource access. A user's deliberate original-page navigation is not platform Fetch execution.
- Do not claim the entire capability roadmap is complete: OCR, image search, credentials editing, per-assistant fine-grained capability policy, and broader deployment acceptance are outside this source change.
- No board `running`/review state or session ID was fabricated; collaboration identity has no registered Cloud Code ST.
