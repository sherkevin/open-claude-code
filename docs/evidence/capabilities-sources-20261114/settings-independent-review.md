# Independent capability-settings review

- Environment: school cloud only.
- Reviewer: `/root/independent_source_review` (real collaboration agent; no Cloud Code ST).
- Reviewed commit: `845a9b96b688481ad52cc572bfa703207e44df1f`.
- Baseline: `f8f1f86`.
- Verdict: **PASS for the search/fetch administrator-settings slice and integration into main.** No outstanding blocking finding in this scope. Full merged regression and any service rollout remain separate gates.

## Reviewed boundaries

- Actual App navigation renders the new settings page only for normal-deployment administrators. Both GET and PUT are server-side administrator-protected; controlled trial routes fail closed.
- The persisted settings contain only bounded enums, enabled flags, timeout and revision. Credentials/private service addresses are not sent to the browser, saved in the settings table, or included in its audit log.
- Capability/provider compatibility and configured-state validation happen on save. Unsupported OCR and image-search controls remain read-only and explicitly not integrated.
- Each capability accepts one preferred provider and at most one distinct fallback, matching the visible page; invalid or duplicate routes are rejected.
- Writes use a transactional revision comparison and re-read the administrator's current role and enabled status inside the transaction. Stale administrator objects cannot write after demotion.
- Search and fetch route independently. Authorization checks current user, assistant permission, deployment policy, global enabled state and revision before attempts and after results. Mid-flight revocation/configuration changes cannot send fallback calls or admit stale results.
- Permission/safety/input refusal does not trigger fallback. Eligible service errors/rate limits can fall back within the same total time budget. Timeout/cancellation does not create source records.
- Global disablement prevents future external execution; it intentionally does not delete prior evidence. Historical source access still uses the source service's current user/assistant/resource authorization. User-initiated original-page navigation is distinct from platform fetch execution.
- `git diff --check` passes and the reviewed implementation worktree is clean.

## Test evidence and ownership

1. I independently executed the earlier complete related group: **76 passed in 133.20 seconds**, one existing Starlette deprecation warning. Log: `/media/data3/dengkw/tmp/oc-independent-source-review/capability-result.txt`. This run preceded the two additional tests and minor final changes; it is not represented as a stable-commit 78-test run.
2. On stable commit `845a9b9`, I independently reran the two newly added runtime-authorization and stale-admin tests: **2 passed, 31 deselected in 4.49 seconds**, one existing warning. Log: `/media/data3/dengkw/tmp/oc-independent-source-review/capability-845a9b9-result.txt`.
3. On the stable commit, I independently checked schema acceptance/rejection: duplicate preferred/fallback rejected; two fallbacks rejected; one distinct fallback accepted. Log: `/media/data3/dengkw/tmp/oc-independent-source-review/capability-route-845a9b9-result.txt`.
4. The implementer executed the final complete stable-source related group: **78 passed in 175.68 seconds**, one existing warning. I inspected the completed log `/media/data3/dengkw/tmp/capability-tests-final.log` and matched the final source/commit scope.
5. The implementer's final frontend log reports **24 files / 159 tests passed** at `/media/data3/dengkw/tmp/capability-client-tests-final.log`, and its build succeeded. These are implementer executions, not claimed as personally rerun by the independent reviewer.

## Visual evidence

I opened and inspected `/media/data3/dengkw/tmp/capability-browser/desktop-fixed.png`, `mobile-top.png`, `conflict.png`, and `member.png`.

The actual App displays the administration entry, separate search/fetch controls, configured-but-not-verified labels, honest OCR/image-search limitations, a readable narrow-screen layout, and a visible concurrent-update conflict. Member navigation does not expose the administration entry. The implementer's `VALIDATION.md` records real isolated API saves/409 conflicts/member 403, with loading/network-error states injected only in the local browser fixture. It also documents correction of an existing CSS class collision. These are synthetic loopback checks, not production or paid-provider validation.

## Requirement/document review

`REQUIREMENTS_ALIGNMENT.md` section 18 and `design/EXTERNAL_CAPABILITIES_AND_SOURCES.md` correctly preserve the school-cloud-only gate, source provenance semantics, separate model versus tool providers, and global-disablement versus historical-source-access distinction.

I recommended explicitly retaining the unfinished original roadmap in the requirement status: per-assistant separate capability toggles, domain/data-egress restrictions, quota/retry/diagnostic controls, key-configuration workflow, independent OCR and image search. Completing this slice must not be called completion of all stage-two/stage-three requirements. The project owner was informed; no implementation expansion is required for this scoped pass.

## Operational limits

No real credentials/customer data were accessed, no real provider/model call was executed, and no shared/production service or Tencent environment was modified by this reviewer. No task-board running state, review state, or ST was fabricated. This report permits code integration only, subject to the owner's final combined regression.
