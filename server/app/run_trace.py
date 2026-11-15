"""Redact execution exports while retaining their host-generated usage ledger."""
from .security import redact


def _retain_accounting(original, public):
    if not isinstance(original, dict) or not isinstance(public, dict):
        return
    ledger = original.get("token_accounting")
    if isinstance(ledger, dict):
        # The wrapper names measured token counts, not an authentication token.
        # Its descendants (including unknown extensions) still use the normal
        # secret-key filter. Malformed scalar wrappers remain redacted.
        public["token_accounting"] = redact(ledger, truncate=False)


def redact_run_trace(trace):
    """Allow the ledger only at the server's explicit usage metadata positions.

    Do not exempt this key globally: model/tool payloads and arbitrary nested
    objects must not acquire a redaction bypass by naming it token_accounting.
    """
    public = redact(trace, truncate=False)
    _retain_accounting(trace.get("usage"), public.get("usage"))
    _retain_accounting(trace.get("run", {}).get("usage"), public.get("run", {}).get("usage"))
    for original, event in zip(trace.get("events", []), public.get("events", [])):
        if original.get("type") == "usage.updated":
            _retain_accounting(original.get("data"), event.get("data"))
        elif original.get("type") == "run.completed":
            _retain_accounting(original.get("data", {}).get("usage"), event.get("data", {}).get("usage"))
    return public
