# Locked workspace profile

This application-owned profile launches the pinned official DeepSeek Harness
through its supported `dsh --profile workspace` entry point. Its bundle list is
empty. `profile.py` writes the complete audited plugin list; it never overlays a
stock SDK profile. Internal filesystem and working-directory services support
the session kernel. No filesystem, shell, scheduler, installation, native MCP,
subagent, or command-dispatch tool is registered.

Decision: share the official JSON-RPC transport between the official SDK server
and our workspace tools. Stock `approval/request` omits tool arguments, so it
cannot authorize business operations. Every workspace tool instead sends a
`workspace/tool` request with the complete arguments to the application host.
The host performs authorization, approval, revalidation, execution, and audit.
Node never performs the business operation after receiving an approval.

```python
from harness.profile import write_profile

profile = write_profile(
    profile_root=private_harness_home,
    source_dir=pinned_source,
    cwd=conversation_workspace,
    session_id=harness_session_id,
    system_prompt=trusted_system_prompt,
    provider={
        "baseURL": authorized_messages_base_url,
        "model": authorized_upstream_model,
        "apiKeyEnv": "WORKSPACE_MODEL_API_KEY",
        "reasoningEffort": "off",  # Explicitly selected; low/high/max also supported.
    },
    tools=authorized_workspace_definitions,
    max_steps=30,
)
```

The return value contains `args` for the official source launcher, `dsh_home`
for `DSH_HOME`, and the exact `initialize` payload. The launcher must use the
fixed Node executable and source resolver, a dedicated process, private HOME
and DSH_HOME, and an explicit environment allowlist. The profile rejects ambient
`.env` and home patch files. It contains a credential environment-variable
reference, never a credential. The host permits only the authorized HTTPS
TokenHub route in production. It selects a nonempty `ANTHROPIC_API_KEY` first,
then falls back to `ANTHROPIC_AUTH_TOKEN`, passing the selected value as
`WORKSPACE_MODEL_API_KEY` to the official Messages provider. This fallback does
not select an arbitrary Bearer-only HTTP transport or prove provider acceptance;
the pinned provider owns the wire authentication. Its endpoint resolver
preserves a base URL that already ends in `/v1`. Loopback and reserved test
routes require explicit `provider_test_mode=True` and are never production-ready.

`server/tests/test_harness_verification_config.py` checks this admission boundary
without starting a runtime or contacting a provider. Real-Node fixtures declare
their synthetic route and image capability explicitly; the image transport
fixture must declare `multimodal` to pass the same guard as a real deployment.
Follow `docs/operations/HARNESS_PILOT.md` from the repository root for the pinned
Linux installation and POSIX/real-Node matrix. A skipped real-Node test is not
evidence of a working runtime, and a loopback handshake is not model inference.

Protocol:

* Host requests allowed: `initialize`, `session/prompt`, `session/resume`,
  `session/wait`, `workspace/inspect`, and `shutdown`. Session requests must use
  the single configured session id. Prompts contain literal text blocks and
  host-authorized encoded static raster images only; file paths, URI image
  requests, arbitrary block fields and noncanonical Base64 are rejected.
  The private official attachment store can expose its normalized-copy path
  in model context; this does not grant a filesystem-reading tool.
* Tool request: `workspace/tool` with
  `{schema:"workspace-tool/v1", sessionId, callId, toolName, arguments}`.
  Tool names are the six short workspace names or `external_` followed by the
  first 32 lowercase hex characters of SHA-256 of the canonical MCP tool name
  encoded as UTF-8. These 41-character aliases are narrowed to the exact tool
  list supplied by the host. The Python host alone owns alias-to-connection
  routing, original server tool names, URLs and credentials; approvals and
  evidence use its canonical tool name, never a model-selected route.
* Tool response: `{content:[{type:"text",text}], is_error?:boolean}`. The host
  must respond only after its authorized operation finishes. Failure responses
  become failed Harness tool results. Oversized/invalid responses are refused.
* `workspace.tool_cancelled` carries `{sessionId,callId}` so the host can cancel
  its pending approval or operation. Late replies cannot settle an aborted call.
* `workspace.stream` carries `{sessionId,stepId,revision,event}`. Events are
  `start` (also `turn,step`), `text_delta` (also `text`) and `end` (also official
  `outcome` and `finishObserved`). End of an attempt is not success of a turn.
  The host must require a normal `turn/end` and a settled `session/wait`, and
  must fail an abandoned or unterminated attempt.
* `workspace.limit` carries `{sessionId,code:"MAX_STEPS"}` and fails the turn.
  The step bound resets for each official turn; it is not a goal token budget.
* `workspace/inspect` reveals only the fixed commit, session, tool names, step
  limit and initialization state. It is suitable for a no-model real handshake.

External definitions contain exactly `name`, `description`, and `input_schema`.
Object-rooted schemas retain local `$defs` / `$ref` and other JSON Schema
constraints. The official input validator supports only a limited subset, so
external inputs bypass that validator and must pass the Python host's complete
JSON Schema validation before approval and execution. Node checks argument
shape and size; it never resolves references or opens an MCP connection. Both
profile and plugin reject non-fragment references and schema base URI changes
(`$id`), including those within annotations. The host must also
disable remote schema resolution and validate server metadata before calling
`write_profile`. Definitions are limited to 512 KiB each and external schema
structures to depth 128. Tool registration and
execution remain restricted to the exact host-issued aliases for this session.

Native SDK durable session/status notifications remain available. Their raw
contents are internal; the application host must project only its existing safe
public events. Image input is a transport capability, not evidence that an
upstream model has passed visual-quality acceptance. The host validates upload
ownership, conversation scope, formats, bounded content, and historical file
receipts before admission. This profile deliberately has no native files,
native MCP clients, native compaction, or subagent support. The host must reject
those capabilities until their equivalent adapters are implemented.
