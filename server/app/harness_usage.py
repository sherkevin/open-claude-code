"""Host-owned receipts for actual HTTP attempts, never normalized SDK defaults."""
from __future__ import annotations

from .token_savings import TokenAccounting

FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
MAX_ATTEMPTS = 4096


def count(value):
    return value if type(value) is int and 0 <= value <= 10**15 else None


class HarnessUsageLedger:
    def __init__(self, session_id, run_id):
        self.session_id, self.run_id = session_id, run_id
        self.attempts = {}
        self.complete = False
        self.invalid = False
        self.revision = 0

    def observe(self, value):
        if not isinstance(value, dict) or value.get("sessionId") != self.session_id or value.get("runId") != self.run_id:
            return
        identity, revision = value.get("attemptId"), value.get("revision")
        if (value.get("schema") != "workspace-usage/v1" or not isinstance(identity, str) or not 1 <= len(identity) <= 128
                or type(revision) is not int or revision < 0 or value.get("purpose") not in {"answer", "compaction"}
                or value.get("phase") not in {"started", "usage", "finished"}
                or type(value.get("terminalObserved")) is not bool
                or not isinstance(value.get("usage"), dict)):
            self.invalid = True
            return
        previous = self.attempts.get(identity)
        if previous is None and len(self.attempts) >= MAX_ATTEMPTS:
            self.invalid = True
            return
        if previous and revision <= previous["revision"]:
            return
        if previous and previous["phase"] == "finished":
            self.invalid = True
            return
        raw = value["usage"]
        usage = {field: count(raw.get(field)) for field in FIELDS}
        # A message_start output zero is an initial sample, not a terminal bill.
        if value["terminalObserved"] is not True:
            usage["output_tokens"] = None
        usage["reported_prompt_tokens"] = count(raw.get("reported_prompt_tokens"))
        self.attempts[identity] = {"revision": revision, "phase": value["phase"],
            "stage": "compression" if value["purpose"] == "compaction" else "answer", "usage": usage,
            "outcome": value.get("outcome") if value.get("outcome") in
                {"pending", "completed", "incomplete", "http_error", "transport_error", "cancelled", "observer_limited"} else None}
        self.revision += 1

    def settle(self, snapshot):
        if not isinstance(snapshot, list) or len(snapshot) > MAX_ATTEMPTS:
            self.invalid = True
            return
        for value in snapshot:
            self.observe(value)
        identities = [value.get("attemptId") for value in snapshot if isinstance(value, dict)
                      and value.get("sessionId") == self.session_id and value.get("runId") == self.run_id]
        self.complete = (not self.invalid and len(identities) == len(snapshot) == len(set(identities))
                         and set(identities) == set(self.attempts)
                         and all(item["phase"] == "finished" for item in self.attempts.values()))

    def receipt(self):
        ledger = TokenAccounting()
        totals, known_total = [], 0
        for identity, item in self.attempts.items():
            usage = item["usage"]
            ledger.record(item["stage"], {**usage, "cache_write_input_tokens": usage["cache_creation_input_tokens"]},
                          source="harness_provider")
            buckets = [usage[key] for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
            total = usage["reported_prompt_tokens"]
            if total is None and all(number is not None for number in buckets):
                total = sum(buckets)
            elif total is not None and (sum(number for number in buckets if number is not None) > total
                    or all(number is not None for number in buckets) and sum(buckets) != total):
                total = None
            totals.append(total)
            known_total += total or 0
        accounting = ledger.summary()
        for stage, (identity, item) in zip(accounting["stages"], self.attempts.items()):
            stage.update(attempt_id=identity, outcome=item["outcome"],
                         reported_prompt_tokens=item["usage"]["reported_prompt_tokens"])
        accounting.update(receipt_schema="harness-http-usage/v1", capture_complete=self.complete,
                          known_model_calls=len(self.attempts))
        if not self.complete:
            accounting["provider_totals"] = {key: None for key in accounting["provider_totals"]}
            accounting["provider_usage_complete"] = False
        total_input = sum(totals) if self.complete and all(value is not None for value in totals) else None
        values = accounting["provider_totals"]
        return {"source": "harness_provider", "input_tokens": values["input_tokens"],
                "output_tokens": values["output_tokens"], "cache_read_input_tokens": values["cache_read_input_tokens"],
                "cache_creation_input_tokens": values["cache_write_input_tokens"],
                "total_input_tokens": total_input, "known_total_input_tokens": known_total,
                "estimated_cost_usd": None, "currency": "USD", "cost_is_estimate": True,
                "token_accounting": accounting}
