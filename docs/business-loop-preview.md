# Business-loop integration preview

This extends the existing workbench; it is not a separate product or deployment.

## Local reproduction

Build the existing client, install the normal server/test dependencies, then run:

```sh
python examples/business_loop_preview.py serve
python examples/business_loop_preview.py verify
python examples/business_loop_preview.py benchmark
```

The preview binds only to loopback, serves the real product on port 8000 and a
Bearer-protected synthetic MCP on port 8877. Credentials and receipts are stored
outside the repository in a mode-0600 preview-private directory. No production
configuration or default runtime is changed. Do not expose this test runtime to
customers as a real model.

Use the synthetic assistant to ask `10万条华北订单总额` or `10万条今日发货`.
The regular product auth, connection discovery, assistant binding, MCP transport,
database aggregation, source registration and event paths are used. Planning and
answer text are deterministic fixture behavior: model usage is unknown, not zero.
See business-result-contract.md for the accepted table and model-input budget.

The UI preserves the question from the homepage until the user confirms sending.
A successful tool result exposes scope, units, coverage, table/chart, sources and
CSV from the same returned rows. CSV has formula-prefix protection but no Excel
column types; identifiers and dates require text import. It is not a full-data
server export. Failed/cancelled runs do not display successful business cards.

## Release gates not satisfied by this preview

- Real customer endpoint, scoped account, reference answer and independent setup.
- Real model/SDK CLI execution, complete usage accounting and latency measurement.
- Linux regression suite, two-account tenant/source/export isolation and concurrency.
- Query-result cache, editable scope/partial refresh, partial retry, typed Excel.

No production rollout is implied by passing synthetic tests. Existing knowledge,
workflow, memory and approval features remain in the same application.
