# Exact receipt, identity and credential-revision supplement

## Scope / evidence

- Frozen tree: `/media/data3/dengkw/projects/open-claude-code-capability-contract-review`, HEAD `f3f88077db916791587b369470f10f8c0c466778`.
- Review window: 2026-11-14 12:43:29Z to 12:47:40Z. Git worktree was clean before and after; the three principal module hashes were unchanged.
- Supplementary first-slice audit only, not the concurrent formal Claude review and not whole-runtime acceptance. No redirect finding is repeated here.
- No product/formal test edits. Independent synthetic probes live only in this directory. No original runtime data or credentials were read; a fresh synthetic database and dummy credentials were created here. The subprocess uses an explicit environment whitelist, dedicated data3 HOME/TMPDIR/cache, offline model flags and a Python audit hook that refuses socket connect/name resolution. No application server, model/provider call, shared service or full test suite was run.
- Executed artifacts: `run-20261114T124611Z/command.json`, `probe.log`, `results.json`, `exit.json`. Exit code **0**, elapsed **12.914 s**, recorded network attempts **[]**. Exit 0 means the diagnostic script completed, not that security passed.

| Input | SHA-256 |
|---|---|
| capability_receipts.py | `6fa6795ec37d69e75d1ed2aa918ea19f25092b1218749a37f938576f0e4575b5` |
| capability_execution.py | `b37bea65bf8c23908c4c0fbe18a2e453ad0732059db1b5ce7abd6fdfdef916f9` |
| capability_usage.py | `e55a9b4df5ea88f7a7418d0c0acb38ea09e8ff7e2376fa52f2d9df48c0e370ee` |
| capability_credentials.py | `895609f0583c47e6e1277092bd66509ad835999305f098158b97bdd66b7efc46` |
| capability_settings.py | `a48c4ddead0e86a89b7a46ca86a6b7567b20b9593cc8afa31c4c3fc95ce43d53` |
| capability_policy.py | `5c6fe27be2e7ae67d110264b74d37df14aad239c260695ef225052b9f52980a0` |
| db.py | `7fc6e9b5e7d6a8c1cfeceeac90929959b18f7269f4f7961a0531c00681e3bb01` |
| probe.py | `318109b1d703471d4e01bcc07e3276cc988c622bf8697426034ff07837ec45ef` |
| run-20261114T124611Z/results.json | `a131b4a9ba626b8722dccedce84fcbd2fbbdbf5e79c20bc267c16d5f307b8b71` |

## Confirmed findings

### 1. P1: same-call receipt replay dispatches twice; usage uniqueness does not prevent it

Locations: `server/app/capability_execution.py:124`, `server/app/capability_execution.py:128`, `server/app/capability_receipts.py:85`, `server/app/capability_receipts.py:93`, `server/app/capability_usage.py:37`, `server/app/db.py:454`.

The executor supplies `call_scope_id=context.tool_call_id`. A second executor entry with the same host call ID therefore looks like the original claim owner, rather than a separate attempted dispatch. `claim_approval_receipt` intentionally accepts a matching prior claim; `_claimed` also lets subsequent calls on the same instance go straight to read-only verification. Neither construct proves that this is merely an in-flight recheck rather than a new execute call.

The ledger does not rescue this: `capability_calls` uses `INSERT OR IGNORE`, then reserves a new attempt regardless of an existing call's state. `capability_attempts_call(call_id,attempt_index)` is an ordinary index, **not unique**; only the randomly generated attempt row id is unique.

Reproduction results:

| Probe | Outcome | Actual synthetic operation dispatches | Attempt rows |
|---|---|---:|---|
| Sequential same receipt/context, two fresh executor instances | accepted, accepted | 2 | same call id, indices `[0,0]`, both ok |
| Concurrent same receipt/context, two fresh executor instances | accepted, accepted | 2 | same call id, indices `[0,0]`, both ok |

The concurrent operation holds both invocations in flight until both have entered; this is not merely two retrospective successful return values. Run/day counters correctly become 2. Thus this is **replayed consent/duplicate execution, not free uncounted requests**. The probe uses one process and database connection; cross-process replay was not executed, although the persistent claim semantics also lack a different-invocation discriminator.

Negative controls: a different host call ID receives `APPROVAL_REQUIRED`, mutated arguments receive `APPROVAL_REQUIRED`, and a missing conversation receives `NOT_FOUND`; all dispatch zero operations. These demonstrate that the flaw is specifically repeated execution under the same call identity, not arbitrary receipt borrowing.

Minimum fix direction: distinguish the externally bound host call ID from an opaque executor-entry ownership token. Generate the latter once per `execute_for_run` entry; keep it only in that invocation's recheck closure, not a persistent executor-wide `_claimed` shortcut. Atomically permit the first ownership claim and reject every later entry, while allowing that owner's rechecks and explicitly permitted attempt indices. Add persistent logical-dispatch protection for auto-approved calls too. An appropriately scoped uniqueness constraint on run/call/attempt is useful defense in depth, not a replacement for consent ownership. Review the current globally keyed `call_id` model before assuming call IDs are globally unique across every run/session.

### 2. P1: credential rotation/deletion does not invalidate the shared execution or returned result

Locations: `server/app/capability_execution.py:176`, `server/app/capability_execution.py:181`, `server/app/capability_credentials.py:115`, `server/app/capability_credentials.py:135`, `server/app/capability_settings.py:209`.

The executor pins/rechecks only settings revision. Credential operations increment a separate credential revision and clear diagnostic records, without changing settings revision. The policy result does not include a selected provider's credential revision/readiness. Therefore the shared recheck can continue under an invalidated credential generation.

Synthetic reproductions use the real credential store and settings/service policy, with an operation callback rather than a network adapter:

| Probe | Credential revision | Presence after | Settings revision | In-operation recheck | Final executor result / attempt state |
|---|---|---|---|---|---|
| Rotate during operation | 1 -> 2 | true | 1 -> 1 | accepted | accepted / ok |
| Delete during operation | 1 -> 2 | false | 1 -> 1 | accepted | accepted / ok |

After the operation returns, the executor's own post-response recheck also accepts; it does not discard the result. The provided host callback calls current `Service.authorize_tool`, rather than being a no-op. This matches the inspected existing web host recheck (`server/app/web_tools.py:386`), which reauthorizes business policy but does not validate credential revision.

A narrow related static check confirms the current web adapter still captures Settings/environment credentials (`server/app/web_tools.py:283`, `server/app/web_tools.py:307`, `server/app/web_tools.py:406`) rather than resolving the new credential store. This was not exercised with any real credential or HTTP request. The storage tombstone itself works; the problem is enforcement at execution/adapter boundaries.

Also, `capability_attempts.provider_revision` defaults to 1 (`server/app/db.py:444`) and `CapabilityUsage.reserve_attempt` never supplies the actual value. This cannot become reliable credential-generation evidence without explicit binding. The probe's first operation started at revision 1, so its recorded 1 is not independently proof of a wrong-start-revision row; the broader audit limitation follows from the insertion code.

Minimum fix direction: acquire the selected provider's credential and revision consistently at request preparation, without exposing the value in RunContext/logs; bind that generation to the attempt and persist the actual revision. Recheck current presence/generation before dispatch, retry/fallback and after receiving the result. Change/delete must stop further sends and discard stale results under the stated revocation contract. Adapters must use this authoritative resolver, not an old environment/closure value. Public unauthenticated providers need an explicit no-credential sentinel, not a fake revision. Do not claim a local cancel can undo a provider-accepted request.

### 3. P2: RunContext.assistant_id is not bound to the actual conversation; ledger attribution is forgeable at the host boundary

Locations: `server/app/capability_execution.py:91`, `server/app/capability_execution.py:102`, `server/app/capability_execution.py:197`.

`_load` checks the run's conversation, the conversation's owner, active run and run state, then loads policy from the **actual** conversation assistant. It never compares `context.assistant_id` to that assistant. Quota/audit reservation nevertheless records the caller-supplied context assistant id.

Reproduction: conversation uses assistant A with search allowed; context claims assistant B with web disabled. Execution succeeds and both logical-call and attempt rows name B. This is a concrete identity-integrity gap.

**The suspected quota bypass was not confirmed.** With the actual run limit set to 1, a second call is rejected with `CAPABILITY_QUOTA_EXCEEDED`; only one operation dispatches, and run/day counters remain 1. Current counters are keyed by run/capability and actor/capability/day, not assistant. Changing context assistant id neither selects B's policy nor bypasses those counters. Report this as false attribution/missing boundary validation, not as proven budget or assistant-policy escalation.

Minimum fix direction: validate the full host context before receipt/quota/dispatch: run/conversation/user coherence (including run.user_id), actual assistant id and snapshot assistant identity as applicable. Use validated canonical IDs from loaded records for accounting instead of copying unchecked context fields. The absent run.user_id/snapshot-id comparisons are static completeness observations; this probe did not manufacture those additional inconsistent database states or prove a public route can create them.

## Trusted-host boundary versus demonstrated bugs

- The probe directly calls the public shared executor with host-constructed RunContext and uses the real receipt API to seed an allowed synthetic human decision. It does not forge a user-facing approval, bypass an SDK nonce, or show that a model can supply RunContext fields.
- A correctly implemented SDK/Harness bridge might reject duplicate delivery and canonicalize identity before these calls. That remains an additional defense, not evidence that the shared module's documented exact-once guarantees hold. No future/incomplete runtime hookup is counted as complete.
- The issue_approval_receipt API is a trusted-host writer by design; this report does not treat its ability to create a synthetic decision as an unauthenticated remote exploit.
- No same-process synthetic probe can prove provider-side idempotency or undo side effects after request acceptance. The observed operation counts show dispatch reachability at the shared executor boundary only.

## Reproduction and follow-up scope

Original exact subprocess arguments/environment are in `run-20261114T124611Z/command.json`; stdout and exit evidence are adjacent. To rerun into a fresh dedicated directory without overwriting the observed fixture:

```sh
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -B \
  /media/data3/dengkw/tmp/oc-capabilities-completion-20261114/receipt-adversarial/run_probe.py
```

The reproduction helper refuses a changed HEAD and preserves the environment whitelist/network guard. It was added after the recorded run and is equivalent orchestration, not an additional executed test claim.

Formal stable-candidate negatives should cover sequential/concurrent duplicate entry on fresh/shared executors, restart after unknown dispatch, automatic-approval duplicate delivery, cross-run call-id collisions, legitimate same-owner rechecks/retries, wrong assistant/user/snapshot identity, and rotation/deletion before send/between attempts/before return. Keep them in the implementation owner's formal tests; this audit modified none.

Conclusion: two demonstrated high-risk shared-executor defects and one demonstrated attribution defect. No formal approval verdict is issued, and no claim is made that the whole capability integration has been accepted.
