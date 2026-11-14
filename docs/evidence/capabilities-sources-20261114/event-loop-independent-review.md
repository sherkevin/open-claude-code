# Event-loop isolation regression diagnosis

Commit: 5eed11a199d09e063292fbde1005b2f6a4b0c910.
Status: original failure not reproduced; no root cause established.

## Observed evidence

- The second integrated full-suite attempt emitted an F at result position 172.
  Mapping the separately collected 1707 node IDs identifies
  `tests/test_event_loop_isolation.py::test_concurrent_submissions_do_not_block_or_queue_forever`.
- The integration owner interrupted that attempt before pytest printed its
  failure report. The captured KeyboardInterrupt and temporary-fixture teardown
  KeyError are interruption artifacts, not the original assertion or a complete
  suite result. This attempt is recorded as interrupted, never passed.
- After the owner confirmed its process exited, the independent reviewer ran
  the entire event-loop file in isolation on the same commit:

```sh
PYTHONPATH=server /media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  server/tests/test_event_loop_isolation.py -x -vv \
  --basetemp=/media/data3/dengkw/tmp/oc-independent-source-review/pytest-event-loop-5eed11a
```

Result: **9 passed in 64.45 seconds**. Raw log:
`event-loop-5eed11a-result.txt` beside this report.

## Static analysis and remaining uncertainty

The candidate test submits 12 conversations with 16 active session slots. It
uses actual SQLite FULL durability plus an injected 20 ms delay on each public
DB method. It checks DB thread isolation, submission elapsed time below 20 s,
max event-loop scheduling gap below 0.30 s, successful HTTP admission, all runs
settling within 60 s and cleared active run references. The gather timeout is
30 s. Without the original failure stack there is no evidence to select any
one of those conditions as the failing assertion.

An isolated pass neither proves that the original failure was harmless timing
noise nor that a product bug has been fixed. No implementation or threshold was
changed. The integration owner will add an external pytest report hook that
persists a failure's node ID and full traceback immediately, then rerun the
integrated suite. A repeated failure must be diagnosed from that evidence.

No implementation files, shared services, supplier credentials, or real
customer data were touched by this reviewer. The temporary test process has
exited.
