"""Bounded, administrator-initiated provider diagnostics.

Diagnostics exist because "a credential is present" is not the same as "the
provider works".  The request intentionally accepts no target, query, URL or
credential: every probe uses one fixed public fixture, so the endpoint cannot
be turned into an arbitrary outbound request tool.  Each run is a single
attempt under a hard deadline with no retry, records only a status code and
timing, and never returns or logs a credential, response body or provider
error text.

Image-search providers have no adapter in this tree yet, so their diagnostics
honestly report ``unsupported`` instead of pretending the route was verified.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .capability_credentials import CapabilityCredentials
from .capability_settings import PROVIDER_MODES, CapabilitySettings
from .db import now
from .security import AppError

DIAGNOSTIC_TIMEOUT_SECONDS = 15
FIXED_SEARCH_QUERY = "example domain public information"
FIXED_FETCH_URL = "https://example.com/"
FIXED_OCR_SAMPLE_ID = "fixed-ocr-text-v1"
FIXED_SEARCH_SAMPLE_ID = "fixed-search-query-v1"
FIXED_FETCH_SAMPLE_ID = "fixed-fetch-url-v1"

# Adapter support is explicit: (provider, capability) pairs without an entry
# are reported as unsupported rather than probed or claimed as verified.
SUPPORTED = {
    ("bing_rss", "search"),
    ("searxng", "search"),
    ("brave", "search"),
    ("deepseek_api", "search"),
    ("deepseek_api", "fetch"),
    ("deepseek_api", "ocr"),
    ("public_http", "fetch"),
}


class CapabilityDiagnostics:
    def __init__(self, service):
        self.service = service

    def _official_deepseek(self) -> bool:
        try:
            parsed = urlsplit(self.service.settings.deepseek_api_base_url or "")
            return parsed.scheme == "https" and (parsed.hostname or "").lower() == "api.deepseek.com"
        except (ValueError, TypeError):
            return False

    def _deepseek_client(self):
        from .deepseek_api import DeepSeekAPI
        settings = self.service.settings
        credential = CapabilityCredentials(self.service).get("deepseek_api")
        if not (settings.deepseek_api_enabled and self._official_deepseek() and credential):
            raise AppError(503, "CAPABILITY_PROVIDER_UNCONFIGURED", "提供方尚未配置")
        remaining = max(1.0, min(float(settings.deepseek_api_timeout_seconds or 30), DIAGNOSTIC_TIMEOUT_SECONDS))
        return DeepSeekAPI(
            base_url=settings.deepseek_api_base_url,
            api_key=credential,
            timeout_seconds=remaining,
        )

    @staticmethod
    def _ocr_fixture(directory: str) -> Path:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (128, 64), "white")
        ImageDraw.Draw(image).text((12, 20), "A1", fill="black")
        path = Path(directory) / "capability-diagnostic.png"
        image.save(path, format="PNG")
        return path

    async def _probe(self, provider_id: str, capability: str, deadline: float) -> str:
        from .web_tools import WebTools, fetch_public

        tools = WebTools(self.service)
        settings = self.service.settings
        if (provider_id, capability) == ("bing_rss", "search"):
            await tools._bing(FIXED_SEARCH_QUERY, deadline=deadline)
        elif (provider_id, capability) == ("searxng", "search"):
            await tools._searxng(settings.web_search_searxng_url, FIXED_SEARCH_QUERY, 1, deadline=deadline)
        elif (provider_id, capability) == ("brave", "search"):
            key = CapabilityCredentials(self.service).get("brave")
            if not key:
                raise AppError(503, "CAPABILITY_PROVIDER_UNCONFIGURED", "提供方尚未配置")
            await tools._brave(settings.web_search_brave_url, key, FIXED_SEARCH_QUERY, 1, deadline=deadline)
        elif (provider_id, capability) == ("public_http", "fetch"):
            await fetch_public(FIXED_FETCH_URL, max_bytes=200_000, deadline=deadline)
        elif (provider_id, capability) == ("deepseek_api", "search"):
            client = self._deepseek_client()
            await asyncio.wait_for(client.search(FIXED_SEARCH_QUERY, count=1, need_content=False),
                                   timeout=max(0.1, deadline - time.monotonic()))
        elif (provider_id, capability) == ("deepseek_api", "fetch"):
            client = self._deepseek_client()
            await asyncio.wait_for(client.fetch(FIXED_FETCH_URL),
                                   timeout=max(0.1, deadline - time.monotonic()))
        elif (provider_id, capability) == ("deepseek_api", "ocr"):
            client = self._deepseek_client()
            with tempfile.TemporaryDirectory(prefix="capability-diagnostic-") as directory:
                path = self._ocr_fixture(directory)
                await asyncio.wait_for(
                    client.ocr(path, filename="capability-diagnostic.png", purpose="text_recognition"),
                    timeout=max(0.1, deadline - time.monotonic()))
        else:
            raise AppError(409, "CAPABILITY_DIAGNOSTIC_UNSUPPORTED", "当前提供方尚无诊断适配器")
        return "verified"

    def _record(self, provider_id: str, capability: str, status: str, *,
                latency_ms: int | None, sample_id: str, error_code: str | None,
                provider_revision: int, settings_revision: int) -> None:
        with self.service.db.transaction():
            self.service.db.execute(
                "INSERT INTO capability_provider_verification(provider_id,capability,status,checked_at,"
                "provider_revision,settings_revision,latency_ms,sample_id,error_code) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(provider_id,capability) DO UPDATE SET status=excluded.status,"
                "checked_at=excluded.checked_at,provider_revision=excluded.provider_revision,"
                "settings_revision=excluded.settings_revision,latency_ms=excluded.latency_ms,"
                "sample_id=excluded.sample_id,error_code=excluded.error_code",
                (provider_id, capability, status, now(), provider_revision, settings_revision,
                 latency_ms, sample_id, error_code))

    @staticmethod
    def _sample_id(capability: str) -> str:
        if capability == "search":
            return FIXED_SEARCH_SAMPLE_ID
        if capability == "fetch":
            return FIXED_FETCH_SAMPLE_ID
        return FIXED_OCR_SAMPLE_ID

    async def run(self, provider_id: str) -> dict:
        settings_api = CapabilitySettings(self.service)
        provider = next((item for item in settings_api.providers() if item["id"] == provider_id), None)
        if provider is None:
            raise AppError(422, "CAPABILITY_PROVIDER_INVALID", "未知的能力提供方")
        provider_revision = CapabilityCredentials(self.service).revision(provider_id)
        settings_revision = settings_api.effective()["revision"]
        results = []
        deadline = time.monotonic() + DIAGNOSTIC_TIMEOUT_SECONDS
        for capability in PROVIDER_MODES.get(provider_id, {}):
            sample_id = self._sample_id(capability)
            if (provider_id, capability) not in SUPPORTED:
                status, error_code, latency = "unsupported", "CAPABILITY_DIAGNOSTIC_UNSUPPORTED", None
            elif not provider["configured"]:
                status, error_code, latency = "not_configured", "CAPABILITY_PROVIDER_UNCONFIGURED", None
            else:
                started = time.monotonic()
                try:
                    status = await self._probe(provider_id, capability, deadline)
                    error_code = None
                except asyncio.CancelledError:
                    status, error_code = "failed", "cancelled"
                except (TimeoutError, asyncio.TimeoutError):
                    status, error_code = "failed", "CAPABILITY_DIAGNOSTIC_TIMEOUT"
                except AppError as exc:
                    status, error_code = "failed", exc.code
                except Exception:
                    status, error_code = "failed", "CAPABILITY_DIAGNOSTIC_FAILED"
                latency = int((time.monotonic() - started) * 1000)
            self._record(provider_id, capability, status, latency_ms=latency,
                         sample_id=sample_id, error_code=error_code,
                         provider_revision=provider_revision, settings_revision=settings_revision)
            results.append({
                "capability": capability, "status": status, "error_code": error_code,
                "latency_ms": latency, "sample_id": sample_id, "checked_at": now(),
            })
        return {"provider_id": provider_id, "provider_revision": provider_revision,
                "settings_revision": settings_revision, "results": results}
