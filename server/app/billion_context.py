"""Fail-closed adapter boundary for the billion-context candidate.

The reviewed npm package is a DeepSeek Harness bundle that patches host
compaction and retry policy.  This module does *not* install or execute it.
It provides the contract a future reviewed integration must satisfy:

* pinned manifest (source, version, commit, license, SHA-256, permissions),
* explicit enable switch and one-call rollback,
* timeout / cancellation / failure paths that never cache a partial summary,
* independent fact and constraint assertions,
* a ledger that keeps the compression stage separate from the answer stage.

Tests use a clearly labelled local extractive compressor.  Results from that
compressor are reported as ``synthetic_reference``; they are never presented as
billion-context output or as provider token savings.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable, Protocol

from .plugin_catalog import PluginCandidate, validate_candidate
from .plugin_usage import UsageLedger, estimate_text_tokens


class CompressionCancelled(Exception):
    pass


class CompressionTimeout(Exception):
    pass


@dataclass(frozen=True)
class CompressionRequest:
    context_id: str
    text: str
    facts: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    def digest(self) -> str:
        payload = json.dumps(
            {
                "context_id": self.context_id,
                "text": self.text,
                "facts": list(self.facts),
                "constraints": list(self.constraints),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class Compressor(Protocol):
    id: str
    executes_third_party_code: bool
    provider_call: bool

    def compress(
        self,
        request: CompressionRequest,
        *,
        cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> str: ...


class SyntheticExtractiveCompressor:
    """Deterministic reference compressor used only to exercise the boundary.

    It is not billion-context, does not call a model, and its measurements are
    labelled ``synthetic_reference`` in every result.
    """

    id = "synthetic-extractive-v1"
    executes_third_party_code = False
    provider_call = False

    def compress(
        self,
        request: CompressionRequest,
        *,
        cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> str:
        keep_tokens = tuple(request.facts) + tuple(request.constraints)
        kept: list[str] = []
        for index, line in enumerate(request.text.splitlines()):
            if cancel and cancel():
                raise CompressionCancelled("cancelled by caller")
            if deadline is not None and time.monotonic() > deadline:
                raise CompressionTimeout("deadline exceeded")
            if index == 0 or any(token and token in line for token in keep_tokens):
                kept.append(line)
        return "\n".join(kept)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _preserved(text: str, required: tuple[str, ...]) -> dict:
    normalized = _normalize(text)
    result = {}
    for item in required:
        result[item] = bool(item) and _normalize(item) in normalized
    return result


def _fingerprint(manifest: PluginCandidate) -> str:
    payload = "|".join(
        (manifest.name, manifest.version, manifest.commit, manifest.package_sha256, manifest.license)
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class BillionContextAdapter:
    def __init__(
        self,
        manifest: PluginCandidate,
        *,
        enabled: bool = False,
        cache_limit: int = 32,
    ):
        if cache_limit < 1:
            raise ValueError("cache_limit must be positive")
        self._manifest = manifest
        self._enabled = bool(enabled)
        self._cache: dict[str, dict] = {}
        self._cache_limit = cache_limit

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def manifest(self) -> PluginCandidate:
        return self._manifest

    def rotate_manifest(self, manifest: PluginCandidate) -> None:
        """Adopt a new pinned version; old summaries stay keyed to the old one."""
        self._manifest = manifest
        self._enabled = False

    def enable(self, manifest: PluginCandidate | None = None) -> dict:
        """Explicit activation switch; invalid manifests stay disabled."""
        if manifest is not None:
            self._manifest = manifest
        reasons = validate_candidate(self._manifest)
        if reasons:
            self._enabled = False
            return {"enabled": False, "reasons": reasons}
        self._enabled = True
        return {"enabled": True, "reasons": []}

    def rollback(self, reason: str = "manual_rollback") -> dict:
        self._enabled = False
        self._cache.clear()
        return {
            "enabled": False,
            "cache_entries": 0,
            "reason": reason,
            "fallback": "original_context",
            "execution": "stopped",
        }

    def preflight(self) -> dict:
        reasons = validate_candidate(self._manifest)
        if reasons:
            return {
                "status": "blocked",
                "enabled": self._enabled,
                "reasons": reasons,
                "plugin_execution": "not_started",
            }
        return {
            "status": "ready" if self._enabled else "disabled",
            "enabled": self._enabled,
            "plugin": {
                "name": self._manifest.name,
                "version": self._manifest.version,
                "package_sha256": self._manifest.package_sha256,
                "license": self._manifest.license,
                "permissions": list(self._manifest.permissions),
                "install_state": self._manifest.install_state,
                "execution_state": self._manifest.execution_state,
            },
            "plugin_execution": "not_started",
            "measurement": "not_measured",
        }

    def _cache_key(self, request: CompressionRequest, compressor: Compressor) -> str:
        return "|".join(
            (_fingerprint(self._manifest), request.digest(), getattr(compressor, "id", "unknown"))
        )

    def _result(
        self,
        status: str,
        request: CompressionRequest,
        *,
        reason: str | None = None,
        summary: str | None = None,
        facts_preserved: dict | None = None,
        constraints_preserved: dict | None = None,
        cache_hit: bool = False,
        elapsed_ms: float = 0.0,
        compressor: Compressor | None = None,
        ledger: UsageLedger | None = None,
    ) -> dict:
        plugin_executed = bool(getattr(compressor, "executes_third_party_code", False))
        usable = status == "ok"
        original = estimate_text_tokens(request.text)
        compressed = estimate_text_tokens(summary) if summary is not None else None
        reduction = None
        if usable and compressed and original["estimated_tokens"] > 0:
            reduction = round(
                1 - compressed["estimated_tokens"] / original["estimated_tokens"], 6
            )
        value = {
            "status": status,
            "reason": reason,
            "context_id": request.context_id,
            "used_compressed": usable,
            "context": summary if usable else request.text,
            "summary": summary,
            "facts_preserved": facts_preserved or {},
            "constraints_preserved": constraints_preserved or {},
            "cache_hit": cache_hit,
            "elapsed_ms": round(elapsed_ms, 3),
            "plugin": {
                "name": self._manifest.name,
                "version": self._manifest.version,
                "package_sha256": self._manifest.package_sha256,
                "plugin_executed": plugin_executed,
                "compressor": getattr(compressor, "id", None) if compressor else None,
                "measurement": "plugin_measured" if plugin_executed else "synthetic_reference",
            },
            "tokens": {
                "original_local_estimate": original,
                "compressed_local_estimate": compressed,
                "reduction_ratio": reduction,
                "authoritative": False,
            },
            "ledger": ledger.summary() if ledger else None,
        }
        if ledger is not None and compressor is not None:
            ledger.record(
                "compression",
                getattr(compressor, "provider_usage", None) if compressor else None,
                source=getattr(compressor, "id", "unknown") if compressor else "unavailable",
                provider_call=bool(getattr(compressor, "provider_call", False)) if compressor else False,
                local_estimate=compressed,
                note=(
                    "local reference compressor; no provider call"
                    if compressor and not getattr(compressor, "provider_call", False)
                    else "compression stage; provider usage recorded separately"
                ),
            )
            # Re-read the ledger after recording so callers see this stage.
            value["ledger"] = ledger.summary()
        return value

    def compress(
        self,
        request: CompressionRequest,
        compressor: Compressor,
        *,
        timeout_seconds: float = 2.0,
        cancel: Callable[[], bool] | None = None,
        ledger: UsageLedger | None = None,
    ) -> dict:
        if not self._enabled:
            return self._result("disabled", request, reason="adapter_disabled")
        reasons = validate_candidate(self._manifest)
        if reasons:
            return self._result("blocked", request, reason=reasons[0]["code"])
        if not isinstance(request, CompressionRequest) or not request.text.strip():
            return self._result("invalid_request", request, reason="empty_context")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            return self._result("invalid_request", request, reason="invalid_timeout")
        if cancel and cancel():
            return self._result("cancelled", request, reason="cancelled_before_start")

        key = self._cache_key(request, compressor)
        cached = self._cache.get(key)
        if cached is not None:
            facts = _preserved(cached["summary"], request.facts)
            constraints = _preserved(cached["summary"], request.constraints)
            if all(facts.values()) and all(constraints.values()):
                return self._result(
                    "ok",
                    request,
                    summary=cached["summary"],
                    facts_preserved=facts,
                    constraints_preserved=constraints,
                    cache_hit=True,
                    compressor=compressor,
                    ledger=ledger,
                )

        started = time.monotonic()
        deadline = started + float(timeout_seconds)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                compressor.compress, request, cancel=cancel, deadline=deadline
            )
            try:
                summary = future.result(timeout=float(timeout_seconds))
            except FutureTimeout:
                return self._result(
                    "timeout",
                    request,
                    reason=f"compression_timeout_{timeout_seconds}s",
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    compressor=compressor,
                    ledger=ledger,
                )
            except CompressionCancelled:
                return self._result(
                    "cancelled",
                    request,
                    reason="cancelled_during_compression",
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    compressor=compressor,
                    ledger=ledger,
                )
            except CompressionTimeout:
                return self._result(
                    "timeout",
                    request,
                    reason="deadline_exceeded",
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    compressor=compressor,
                    ledger=ledger,
                )
            except Exception as error:  # noqa: BLE001 - third-party boundary
                return self._result(
                    "failed",
                    request,
                    reason="compressor_error:" + type(error).__name__,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    compressor=compressor,
                    ledger=ledger,
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        elapsed = (time.monotonic() - started) * 1000
        if cancel and cancel():
            return self._result(
                "cancelled",
                request,
                reason="cancelled_after_compression",
                elapsed_ms=elapsed,
                compressor=compressor,
                ledger=ledger,
            )
        if not isinstance(summary, str) or not summary.strip():
            return self._result(
                "failed",
                request,
                reason="invalid_summary",
                elapsed_ms=elapsed,
                compressor=compressor,
                ledger=ledger,
            )
        facts = _preserved(summary, request.facts)
        constraints = _preserved(summary, request.constraints)
        if not all(facts.values()) or not all(constraints.values()):
            return self._result(
                "assertion_failed",
                request,
                reason="fact_or_constraint_missing",
                summary=summary,
                facts_preserved=facts,
                constraints_preserved=constraints,
                elapsed_ms=elapsed,
                compressor=compressor,
                ledger=ledger,
            )

        self._cache[key] = {"summary": summary}
        while len(self._cache) > self._cache_limit:
            self._cache.pop(next(iter(self._cache)))
        return self._result(
            "ok",
            request,
            summary=summary,
            facts_preserved=facts,
            constraints_preserved=constraints,
            cache_hit=False,
            elapsed_ms=elapsed,
            compressor=compressor,
            ledger=ledger,
        )


SYNTHETIC_LONG_CONTEXT = (
    "USER: 我们在评估合成长上下文，不包含客户数据。\n"
    + "\n".join(
        f"filler line {index}: repeated background text used only for compression sizing"
        for index in range(1, 240)
    )
    + "\nFACT-001: the approved release is billion-context 0.1.213.\n"
    + "FACT-002: the package SHA-256 must match the pinned review record.\n"
    + "CONSTRAINT-A: never send credentials or customer data to the compressor.\n"
    + "CONSTRAINT-B: a failed compression must fall back to the original context.\n"
)


def run_synthetic_benchmark(repetitions: int = 3) -> dict:
    """Exercise timeout/failure/cancel plus repeatable success on fixed synthetic input."""
    from .plugin_catalog import catalog_for

    manifest = catalog_for(["billion-context"])[0]
    request = CompressionRequest(
        context_id="synthetic-oc-t11",
        text=SYNTHETIC_LONG_CONTEXT,
        facts=("FACT-001", "FACT-002"),
        constraints=("CONSTRAINT-A", "CONSTRAINT-B"),
    )
    adapter = BillionContextAdapter(manifest, enabled=True)
    compressor = SyntheticExtractiveCompressor()
    runs = []
    for _ in range(repetitions):
        ledger = UsageLedger()
        result = adapter.compress(request, compressor, timeout_seconds=1.0, ledger=ledger)
        ledger.record(
            "answer",
            {"input_tokens": 120, "output_tokens": 30},
            source="synthetic_answer_stub",
            provider_call=True,
            note="synthetic answer-stage usage; not a real provider bill",
        )
        runs.append({"result": result, "ledger": ledger.summary()})

    class FailingCompressor(SyntheticExtractiveCompressor):
        id = "synthetic-failing"

        def compress(self, request, *, cancel=None, deadline=None):
            raise RuntimeError("synthetic failure")

    failure = adapter.compress(request, FailingCompressor(), timeout_seconds=1.0)

    class SlowCompressor(SyntheticExtractiveCompressor):
        id = "synthetic-slow"

        def compress(self, request, *, cancel=None, deadline=None):
            time.sleep(0.25)
            return super().compress(request, cancel=cancel, deadline=deadline)

    timeout = adapter.compress(request, SlowCompressor(), timeout_seconds=0.05)
    cancelled = adapter.compress(
        request, compressor, timeout_seconds=1.0, cancel=lambda: True
    )
    return {
        "input": {
            "context_id": request.context_id,
            "characters": len(request.text),
            "local_estimate": estimate_text_tokens(request.text),
            "facts": list(request.facts),
            "constraints": list(request.constraints),
        },
        "runs": runs,
        "repeatable": len({run["result"]["summary"] for run in runs}) == 1,
        "failure": failure,
        "timeout": timeout,
        "cancelled": cancelled,
        "plugin_executed": False,
        "measurement": "synthetic_reference",
        "real_plugin_measurement": "not_measured",
    }
