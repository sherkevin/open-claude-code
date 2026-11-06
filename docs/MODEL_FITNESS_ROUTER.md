# Model fitness router decision

The product exposes `POST /api/v1/models/recommend` as an advisory operation.
It accepts a task summary capped at 2,000 characters and an optional list of
public catalog IDs capped at 20. The endpoint returns a public model ID, its
catalog label, a supported reasoning level, a short reason, and router
metadata. It never patches a conversation, replaces an active run, creates a
native SDK session, or records a model run/usage row.

The router is pinned to the public ID `deepseek-v4.1-flash`. Deployments use
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

This is a recommendation aid, not a quality or cost guarantee. Provider
billing and actual model behavior remain subject to the administrator's
catalog and provider account. A future change that persists recommendations
or bills them as runs must add a separate usage record and budget policy.
