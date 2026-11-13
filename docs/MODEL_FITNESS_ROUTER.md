# Model fitness router decision

## Current product contract: fixed Fitness 1007 (2026-11-11)

The user selected **1007 as the answering model**, not merely the model that
chooses another model. The canonical requirement is
[requirements v0.4.1](REQUIREMENTS_ALIGNMENT.md), MOD-004 / MOD-007. This section
supersedes the earlier recommendation behavior for the Fitness UI. It records a
required change, not an implemented route or successful provider call.

- Selecting Fitness binds an idle conversation to the exact authorized
  `deepseek-v4.1-flash-1007` answering route. Selection needs no extra prompt,
  does not replay a prior message, and makes no paid recommendation call.
  Ordinary answering can still use the conversation's authorized context.
- A new empty conversation may select the mode and wait for its first normal
  message. Existing drafts remain intact. Active runs cannot be switched.
- Missing, disabled, unauthorized or failed 1007 routes produce an explicit
  recoverable failure. No local scorer, 1017 route or differently mapped Dano
  alias may count as a successful Fitness fallback. A public alias is allowed
  only with verified exact upstream mapping and configuration fingerprint.
- A successful explicit manual model selection atomically leaves Fitness;
  failed selection preserves the old mode and draft. Later sends must not
  silently re-enable Fitness.
- The independent Tab target remains `deepseek-v4.1-flash-1017`; this decision
  does not change knowledge extraction, vision, embedding or image generation
  roles. Provider preflight is metadata, not proof of actual inference.
- Preserve the legacy advisory endpoint for compatibility, but its local
  candidate scoring must not drive the new fixed mode. Every actual helper or
  answering call belongs in the usage ledger, even if it creates no Run row;
  missing provider usage or settlement remains unknown, never zero.

The actual fixed-route implementation, 1007 availability and browser acceptance
remain pending. No credentials, model configuration or running service were
changed when recording this decision. Acceptance thresholds are defined once
in requirements sections 7.2 through 7.5.

## Earlier advisory implementation and historical evidence

The remainder describes the earlier bounded recommendation implementation and
its historical catalog observations. Override/local-fallback rules here do not
override the fixed Fitness contract above; dated availability is not a live
health check. In particular, a working 1017 alias does not prove 1007 exists.

The product exposes `POST /api/v1/models/recommend` as an advisory operation.
It accepts a task summary capped at 2,000 characters and an optional list of
public catalog IDs capped at 20. The endpoint returns a public model ID, its
catalog label, a supported reasoning level, a short reason, and router
metadata. It never patches a conversation, replaces an active run, creates a
native SDK session, or records a model run/usage row.

The Fitness router defaults to the exact public ID
`deepseek-v4.1-flash-1007`. A deployment may override
`AGENT_MODEL_ROUTER_MODEL_ID` only for an explicitly
authorized catalog entry. Deployments use
the deterministic bounded scorer by default (`AGENT_MODEL_ROUTER_ENABLED` is
false). This keeps local development and deployments without router
credentials functional. An administrator may explicitly enable one provider
call per request. That call is limited to a 2,000-character sanitized summary,
public candidate IDs/labels/capabilities, 256 output tokens by default, a
short timeout, and the configured `AGENT_MODEL_ROUTER_BUDGET_USD` bound. The
provider response is accepted only when it is valid JSON and names an
available candidate; otherwise the local scorer is returned.

The router receives no conversation ID, message history, attachments,
assistant instructions, MCP data, credentials, private upstream model names,
or provider response text. The returned `applied: false` marker distinguishes recommendation metadata from a model run. The UI operation is explicit: after the user clicks “Select the most fitness model”, the bounded result is applied automatically only when the conversation is idle. Normal model-switch guards remain authoritative.

The client builds the bounded summary from the current composer text, the last
four user messages, and the assistant description before sending it. The user
does not have to retype the task or edit a second prompt; a read-only preview
shows the exact bounded context used for this recommendation. A brand-new
conversation uses the assistant description or a visible fallback message.

## Route evidence

Every recommendation now carries `router.evidence` with schema
`model-route-evidence/v1`. It records the bounded-router policy version, the
decision mode, public candidate identifiers, selected public identifier, target
availability, and a SHA-256 digest of the sanitized task summary plus the
candidate/target shape. The digest is correlation evidence only: the task text
is not stored in the evidence object, and provider credentials or private
upstream model identifiers are never included. Tab completion uses the same
fields and records `decision_mode=provider` only after a valid provider result
is accepted. If the exact target is unavailable, the evidence remains
`target_status=unavailable` and the suggestion stays empty.

An exact `AGENT_MODEL_ROUTER_MODEL_ID` is fail-closed: if the catalog does not
contain that enabled route, the server records `target_status=unavailable` and
uses the local recommendation only. It does not silently call a different
provider model.

Route metadata also separates catalog availability from provider authorization:
`provider_configured` is false with `provider_status=credentials_missing` when
the route exists but no approved provider credential is configured. Such a
request has `provider_attempted=false` and remains local-only. If an explicitly
authorized provider response is malformed, outside the candidate allowlist, or
fails validation, the decision falls back locally with the bounded reason
`provider_response_rejected`; no provider response text or credential is
persisted. The evidence object contains only public candidate identifiers, a
policy version, a digest of the bounded sanitized input, and the attempted
state.

This is a recommendation aid, not a quality or cost guarantee. Provider
billing and actual model behavior remain subject to the administrator's
catalog and provider account. A future change that persists recommendations
or bills them as runs must add a separate usage record and budget policy.

## Real Tab completion

`POST /api/v1/models/complete` accepts only the current draft (500
characters). It calls the independent Tab target (default public ID
`deepseek-v4.1-flash-1017`, configured by `AGENT_TAB_COMPLETION_MODEL_ID`) and
returns a full completion only when the provider returns valid bounded JSON. The client
requests completion on an explicit Tab press and never inserts a hard-coded
phrase. When the target is missing, disabled, or not authorized, the response
contains `suggestion: null` and `router.target_status=unavailable`; this is an
evidence gap, not a simulated completion.

Tab admission is controlled independently by `AGENT_TAB_COMPLETION_ENABLED`
(default `true`). It does not require `AGENT_MODEL_ROUTER_ENABLED`; the exact
1017 target, provider authorization, positive bounded budget and TokenHub route
are still checked before a request is sent. Set the Tab switch to `false` to
close the capability without changing Fitness routing.

When the catalog target exists but provider credentials are absent, the target
is still reported as catalog `available` while `provider_status` is
`credentials_missing`, `provider_attempted=false`, and the mode is
`provider_unconfigured`. If a provider call is made and its completion does
not preserve the draft prefix or does not match the JSON contract, the mode is
`provider_response_rejected` and `suggestion` remains null. These fields make
it possible to distinguish a missing authorization from an invalid provider
response without exposing secrets.

The current school-cloud catalog has the authorized 1017 upstream route under
the public `dano-v2.1` entry. The server accepts that alias only when its
pinned upstream identifier explicitly contains `deepseek-v4.1-flash-1017`;
otherwise Tab remains `target_status=unavailable` and does not call a
different model. Fitness 1007 remains a separate target and still requires its
own exact catalog entry and authorization.

## Router preflight

`GET /api/v1/models/router-status` is an authenticated, metadata-only
readiness check. It reports the Fitness target (1007), the independent Tab
target (1017), local catalog status, independent Tab switch, router switch, positive budget state and
provider authorization state. `tab_completion_mode` and
`tab_completion_ready` describe the Tab route; the legacy top-level `ready`
field describes the Fitness route. It never calls a model provider and does
not return provider URLs, private upstream IDs, or credentials. A ready flag
only means an explicitly enabled and authorized route is eligible for a future
bounded request; it is not a paid-model success claim.

## Provider preflight and knowledge roles

`GET /api/v1/models/provider-preflight` is the authenticated, metadata-only
check for every model role. In addition to the Fitness route (`1007`) and the
independent Tab route (`1017`), it reports `embedding`, `vision`,
`entity_extraction`, and `relation_extraction`. A role is selected only when
its configured public catalog identifier is enabled or when a catalog entry
explicitly declares the matching capability. A generic chat model is never
silently reused for a knowledge role.

The optional role settings are `AGENT_KNOWLEDGE_EMBEDDING_MODEL_ID`,
`AGENT_KNOWLEDGE_VISION_MODEL_ID`, `AGENT_KNOWLEDGE_ENTITY_MODEL_ID`, and
`AGENT_KNOWLEDGE_RELATION_MODEL_ID`. Empty settings fail closed. The
preflight response never returns private upstream identifiers, provider URLs,
or credentials and always reports `provider_attempted=false` and
`call_enabled=false`; it is not evidence of semantic model quality. Role
adapters must be added and separately accepted before visual embeddings,
entity extraction, or semantic relation edges can replace the current local
preview and deterministic candidates.

The school-cloud catalog currently contains an authorized public alias for
the 1017 Tab route, but no catalog entries declaring embedding, entity, or
relation capabilities. The preflight therefore keeps those roles unavailable
until an administrator adds an approved route and adapter. This is deliberate
and avoids presenting a generic multimodal chat model as a visual index.
