# OpenAI-compatible local adapter evidence

**Scope:** PROT-001 local compatibility slice only. This evidence does not
authorize a real model endpoint, API key, customer call, production deploy, or
service restart.

## Implementation

- `server/app/openai_compat.py` normalizes Chat Completions `messages` and
  Responses `input` into a bounded internal request.
- The adapter has no network client and receives an explicitly injected
  transport. It enforces model allowlists, bounded messages/text/tools/output
  tokens, supported text/image content parts, duplicate-tool rejection and
  function-call shape checks.
- It renders both non-streaming envelopes and protocol-specific stream events.
  Usage fields are copied only when the transport supplied non-negative integer
  values. Provider statuses map to stable product errors without forwarding
  provider response text.

## Verification

```text
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests/test_openai_compat.py
10 passed
```

The test fixture is an in-process synthetic transport. It covers both
protocols, non-streaming and streaming responses, function calls, bounds,
model authorization, timeout/rate-limit/auth/service errors, and diagnostic
redaction. No HTTP request is made.

## Remaining evidence

Real gateway field compatibility, authentication behavior, provider timeout
and rate-limit semantics, accounting fidelity, latency and model quality
remain unverified until the user supplies an approved endpoint, credentials
through the approved secret path, model scope and quota. The product must keep
PROT-001 marked **partial** until that evidence exists.
