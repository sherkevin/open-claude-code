# Independent lease recovery review

Status: PASS for stable fix commit 44d4f7cfd6ecf4610759f0036642e6fecb16de79.
No blocking finding remains in this lease-fencing slice. Scope is
ConnectorRegistry and OpenClawFacade ownership fencing, not provider performance
or deployment. Main full-suite regression remains the integration owner's gate.

## Baseline findings

Baseline: main e9792b5ee81262f7d6cd3987d466d27976f75e7d.

The integrated suite reported 1 failed, 1627 passed, 51 skipped. Its heartbeat
case uses a 0.3-second lease, an elapsed 0.7-second sleep, and synchronous SQLite
FULL writes. Seeing a newer deadline before constructing a second registry does
not prove that deadline remains valid when construction finishes and recovery
runs. That result alone is not evidence of premature production recovery.

Separate deterministic contracts use actual temporary fixture databases and an
explicit persisted expired deadline, not elapsed short sleeps. All 3 fail on
baseline (12.73 seconds):

1. Connector recovery sets failed/503, but the old executor can still return a
   success result and write a success audit despite its final UPDATE affecting
   no row.
2. Facade recovery sets failed/503, but an old successful executor overwrites it
   with succeeded and can publish tool-call references / response indexes.
3. Facade late upstream error overwrites the recovered INTERRUPTED terminal
   state with an unrelated upstream error.

Reproduction: `test_lease_contract.py` and `lease-baseline-result.txt` beside this
report. These use synthetic users and executors only, without network calls or
credentials. Independent reviewer modifies no implementation files.

## Acceptance gates

- Future valid lease is not recovered; missing legacy leases and expired leases
  are recovered once with a persistent INTERRUPTED result.
- Actual heartbeat is observed using an event. Virtual clock controls exact
  lease boundaries; no test simply increases the lease or disables durability.
- Renewal and success require the current owner, running status, and a still
  valid lease. An expired lease cannot be resurrected by late heartbeat.
- A renewal racing a recovery snapshot is protected by conditional UPDATE.
- Recovery terminal state cannot be overwritten by old-owner success, failure,
  or cancellation. A fenced old-owner success is not returned to the caller or
  recorded as a successful audit.
- Facade response indexes and issued tool references are committed atomically
  with the successful fenced turn; losing ownership publishes neither.
- Existing timeout/cancellation/revocation/idempotency tests remain green.

## Boundaries

This is not proof of customer-scale concurrency or distributed clock-skew
handling. Cancellation is cooperative: fencing prevents accepting late local
results, not reversing external operations already performed. No shared service
is restarted and no live supplier is contacted by this reviewer.


## Stable-commit review result

Commit reviewed: `44d4f7cfd6ecf4610759f0036642e6fecb16de79`, clean worktree
`/media/data3/dengkw/projects/open-claude-code-connector-lease`.

The independent reviewer personally ran the command below against that exact
commit after the implementation worker stopped its own tests:

```sh
PYTHONPATH=server /media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest -q \
  server/tests/test_openclaw_connector.py \
  server/tests/test_openclaw_compat.py \
  /media/data3/dengkw/tmp/oc-independent-source-review/test_lease_contract.py \
  -k 'lease or heartbeat or recovery or references_commit or renewal_checks_clock' \
  --basetemp=/media/data3/dengkw/tmp/oc-independent-source-review/pytest-lease-44d4f7c
```

Result: **25 passed, 39 deselected, 1 existing Starlette warning, 55.38 seconds**.
Log: `lease-44d4f7c-result.txt` beside this report. This includes all three
independent contracts that failed on the baseline, now passing. It is not a
claim that the 39 deselected tests or the full application suite were personally
rerun at this commit. `git diff --check HEAD^..HEAD` also passed.

Read-only review confirms:

- Connector renewal and successful finish acquire SQLite BEGIN IMMEDIATE before
  sampling time; facade renewal does likewise, and facade successful finish is
  inside its caller's write transaction. No current production call site nests
  these transactions.
- Heartbeat renewal and successful writes require owner identity, running state,
  and strictly future lease. Equality is expired. Lost heartbeat signals cancel
  with an INTERRUPTED result, not a user-cancel result.
- Failed completion CAS cannot return an old success or a late supplier error;
  it reports 503 INTERRUPTED and result_discarded audit. Recovered terminal rows
  are preserved, including timestamp and stored error fields.
- Facade successful turn, response index and tool references share one atomic
  transaction. The failure-in-reference-writing test confirms rollback.
- Expiration recovery remains conditional; a changed lease after SELECT is not
  incorrectly recovered. Legacy missing leases still recover once.
- The original timing-sensitive test is split into exact controlled-clock
  expiration and event-observed actual heartbeat tests. SQLite durability and
  production lease defaults are unchanged; no timeout inflation masks defects.

Implementation-worker evidence was reviewed separately: two-file run 58 passed
before the final write-lock refinement; stable lease-filter run 31 passed and a
separate stable facade-lock test 1 passed. These are worker results, not claimed
as the independent reviewer's execution. Root should run the integrated full
suite after merging before any service release.
