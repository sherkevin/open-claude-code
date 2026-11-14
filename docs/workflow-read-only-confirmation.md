# Explicit host confirmation for live workflow reads

The optional legacy workflow `mcp_read` route previously treated automatic
approval as evidence of read-only behavior. It now refuses execution unless the
assistant binding has an explicit host confirmation of the current discovered
tool definition. This change does not enable the retired workflow product
surface, change existing business-tool approval rules, or contact a real source.

## Configuration contract

`Binding.read_only_confirmations` is an optional map keyed by already-allowed
remote tool name (maximum 128 entries):

```json
{
  "query_records": {
    "connection_revision": 1,
    "definition_digest": "<server-returned 64-character SHA-256>",
    "basis": "Describe the implementation/account review supporting this judgment"
  }
}
```

Each confirmation needs an integer revision, current definition digest, and a
nonblank human rationale of at most 500 characters. Known transport credentials
must not be included in the rationale. The server never creates confirmations
from a tool name, automatic approval, or a remote annotation. The confirmation
does not grant tool access or automatic approval.

Connection discovery returns each tool's `definition_digest`,
`read_only_confirmation_eligible`, and `read_only_confirmation_reason`. The digest
uses the same projection during saved discovery and immediately before a live
call: name, the existing 4,000-character description projection, complete input
schema, and the four supported MCP boolean annotations (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`). Generated display fields and
the digest itself are excluded. Definition size is limited to 128 KB; the schema
must pass the existing offline validator. Successful shape validation caches
only 64 hashes, never schema text, sample arguments, credentials, or permissions.

Missing/duplicate tool names, malformed annotation values, unknown/unsupported
schemas, disabled or untested connections fail closed. An explicit
`readOnlyHint: false` or `destructiveHint: true` conflicts with the workflow use
and prevents confirmation. No hint may be confirmed by a human who provides a
basis; absence of a hint never grants permission. Only an actual boolean `true`
in the parsed metadata is displayed as a positive declaration. The MCP SDK can
normalize original wire values, so this is **not** strict raw-wire validation or
proof that the implementation is safe.

New or changed confirmations are validated on save. A stale page gets HTTP 409
(`READ_ONLY_CONFIRMATION_STALE`). Unchanged stale records can be preserved during
unrelated assistant edits so the user can inspect/reconfirm them, but cannot run
while the current revision/definition differs. This fingerprint is not an
append-only discovery history: restoring an identical projected definition at
the same revision matches the same fingerprint. Changes beyond the existing
4,000-character description projection do not alter it.

## Execution and approval boundaries

A live read must satisfy **all** existing authorization checks, be auto-approved,
and have a matching host confirmation. Preflight only inspects permission;
execution still validates the exact rendered arguments through `authorize_tool`.
This retains opt-in argument constraints without filling defaults or rewriting
requests. The confirmation alone does not impose field/row limits.

Workflow scopes pin connection revision, argument-constraint digest, confirmation
digest, definition digest, conversation, assistant, user, and associated active
chat run. They are rechecked before connection, after initialization, before and
after every discovery page, before `call_tool`, and after the response. Fresh
remote discovery is mandatory and bounded to 20 pages/1,000 tools; ambiguity or
a changed definition prevents the business call. A revoked permission,
confirmation, archived conversation, ended/replaced associated run, or changed
connection prevents subsequent execution/consumption. Async cancellation is
propagated. Calls already executed before a later revocation cannot be undone;
their results are rejected.

Both model and synthetic/tool workflow permission snapshots include the new
fingerprints. A changed confirmation or definition cannot use an old pending
approval, even if the host confirms the new definition for future runs.

## Returned evidence and limits

Workflow MCP results use shared `clean_mcp_value` with the known transport
credential **before** result truncation, source extraction, model projection or
persistence. This covers ordinary text, structured content, source excerpts and
JSON-escaped credential strings. Transport/serialization failures expose fixed
messages without raw remote exception text. Evidence titles/locators remain
available after cleaning. HTTP response size, timeout and redirects use the
business-tool transport boundaries.

A host confirmation is a documented human judgment, not a remote database
permission guarantee. The customer must still provision an actually read-only
endpoint/account and enforce business permissions server-side. No tests here
prove a real customer endpoint safe. This patch does not redesign all roles or
apply the workflow-only confirmation requirement to normal SDK/Harness calls.
It also does not cover the separate existing risk of credentials echoed inside
**discovered tool metadata** (description/schema); do not put credentials there.

## Local verification

`server/tests/test_workflow_read_only.py` uses synthetic service/API fixtures and
mock MCP sessions. It covers missing/contradictory declarations, malformed
confirmations, stale saves, snapshot changes, revoked permissions at asynchronous
boundaries, fresh/paginated remote discovery, cancellation, approval recovery,
argument preservation and credential-free returned evidence/persistence.
Existing positive workflow fixtures now make explicit host confirmations;
unknown legacy bindings are not grandfathered into execution.
