"""Provider-neutral, secret-free policy for external capability executors.

Four capabilities share one versioned administrator document.  Search and fetch
keep their historical defaults until an administrator saves an explicit
granular policy; OCR and image search default to disabled with file egress
denied.  Provider credentials are never returned here.
"""
from __future__ import annotations

import json
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from .db import dump, now
from .security import AppError

PROVIDERS = {
    "search": ("bing_rss", "searxng", "brave", "deepseek_api"),
    "fetch": ("public_http", "deepseek_api"),
    "ocr": ("deepseek_api",),
    "image_search": ("deepseek_webtools", "brave_images"),
}
CAPABILITY_KINDS = tuple(PROVIDERS)

# Supported modes are part of the contract, not a readiness claim.  A provider
# only appears here after an adapter exists for that mode.
PROVIDER_MODES: dict[str, dict[str, list[str]]] = {
    "bing_rss": {"search": ["web"]},
    "searxng": {"search": ["web"]},
    "brave": {"search": ["web"]},
    "public_http": {"fetch": ["page"]},
    "deepseek_api": {
        "search": ["web"], "fetch": ["page"],
        "ocr": ["text_recognition", "rich_layout_recognition"],
    },
    "deepseek_webtools": {"image_search": ["reverse_image"]},
    "brave_images": {"image_search": ["text_search"]},
}

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_LIMITS = {"calls_per_run": 50, "calls_per_day": 500, "max_upload_bytes": 0}
OCR_LIMITS = {"calls_per_run": 20, "calls_per_day": 100, "max_upload_bytes": 20 * 1024 * 1024}
EGRESS_KEYS = ("query", "url", "upload")


class DomainPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["public", "allowlist"] = "public"
    hosts: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("hosts")
    @classmethod
    def canonical_hosts(cls, values):
        from .capability_policy import normalize_domain
        result = []
        for value in values:
            host = normalize_domain(value)
            if host is None:
                raise ValueError("host must be an exact public hostname")
            if host not in result:
                result.append(host)
        return result


class EgressPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Literal["deny", "approval", "allow"] | None = None
    url: Literal["deny", "approval", "allow"] | None = None
    upload: Literal["deny", "approval", "allow"] | None = None


class CapabilityLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls_per_run: int | None = Field(default=None, strict=True, ge=1, le=10_000)
    calls_per_day: int | None = Field(default=None, strict=True, ge=1, le=100_000)
    max_upload_bytes: int | None = Field(default=None, strict=True, ge=0, le=200 * 1024 * 1024)


def _provider_literal():
    return Literal["bing_rss", "searxng", "brave", "deepseek_api", "public_http",
                   "deepseek_webtools", "brave_images"]


class CapabilityRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    preferred: _provider_literal()
    fallbacks: list[_provider_literal()] = Field(default_factory=list, max_length=1)
    timeout_seconds: int = Field(strict=True, ge=1, le=60)
    max_attempts: int | None = Field(default=None, strict=True, ge=1, le=3)
    domains: DomainPolicy | None = None
    egress: EgressPolicy | None = None
    limits: CapabilityLimits | None = None

    @model_validator(mode="after")
    def unique_providers(self):
        if self.preferred in self.fallbacks or len(set(self.fallbacks)) != len(self.fallbacks):
            raise ValueError("provider route must not contain duplicates")
        return self


class CapabilityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=0)
    search: CapabilityRoute
    fetch: CapabilityRoute
    # Older clients may omit the new capabilities; they are carried forward
    # from the current effective policy rather than silently enabled.
    ocr: CapabilityRoute | None = None
    image_search: CapabilityRoute | None = None


def _egress_defaults(capability: str) -> dict:
    if capability in {"search", "fetch"}:
        return {"query": "allow", "url": "allow", "upload": "deny"}
    return {"query": "deny", "url": "deny", "upload": "deny"}


def _route_defaults(capability: str) -> dict:
    return {
        "enabled": False if capability in {"ocr", "image_search"} else True,
        "preferred": "deepseek_api" if capability == "ocr" else "deepseek_webtools",
        "fallbacks": [],
        "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
        "max_attempts": 1,
        "domains": {"mode": "public", "hosts": []},
        "egress": _egress_defaults(capability),
        "limits": dict(OCR_LIMITS if capability in {"ocr", "image_search"} else DEFAULT_LIMITS),
    }


def normalize_route(capability: str, route: dict, defaults: dict | None = None) -> dict:
    """Fill omitted v2 fields with capability-specific defaults."""
    base = dict(defaults or _route_defaults(capability))
    fallbacks = list(route.get("fallbacks") or [])
    max_attempts = route.get("max_attempts")
    if max_attempts is None:
        max_attempts = 1 if capability in {"ocr", "image_search"} else 1 + len(fallbacks)
    domains = route.get("domains") if route.get("domains") is not None else base["domains"]
    egress = dict(base["egress"])
    egress.update({key: value for key, value in (route.get("egress") or {}).items() if value is not None})
    limits = dict(base["limits"])
    limits.update({key: value for key, value in (route.get("limits") or {}).items() if value is not None})
    if capability in {"ocr", "image_search"} and max_attempts > 1:
        # The official OCR images continue remotely after a local cancel, so
        # automatic retry/fallback would duplicate upload/steps and charges.
        max_attempts = 1
    if capability == "ocr":
        fallbacks = []
    if max_attempts < 1 + len(fallbacks):
        raise AppError(422, "CAPABILITY_ROUTE_INVALID", "总尝试次数必须至少覆盖首选与回退提供方")
    return {
        "enabled": bool(route.get("enabled")),
        "preferred": route.get("preferred"),
        "fallbacks": fallbacks,
        "timeout_seconds": int(route.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS),
        "max_attempts": int(max_attempts),
        "domains": {"mode": domains.get("mode", "public"), "hosts": list(domains.get("hosts") or [])},
        "egress": {key: egress[key] for key in EGRESS_KEYS},
        "limits": {key: int(limits[key]) for key in DEFAULT_LIMITS},
    }


def migrate_config(config: dict, defaults: dict) -> dict:
    """Fill v1 documents (search/fetch only) with safe v2 defaults."""
    result = {}
    for capability in CAPABILITY_KINDS:
        existing = config.get(capability)
        if existing is None:
            result[capability] = json.loads(json.dumps(defaults[capability]))
        else:
            result[capability] = normalize_route(capability, existing, defaults[capability])
    return result


class CapabilitySettings:
    def __init__(self, service):
        self.service = service

    def defaults(self):
        settings = self.service.settings
        provider = settings.web_search_provider
        search_fallbacks = ["bing_rss"] if provider != "bing_rss" and settings.web_search_fallback_bing else []
        fetch = "deepseek_api" if provider == "deepseek_api" else "public_http"
        fetch_fallbacks = ["public_http"] if fetch != "public_http" and settings.web_search_fallback_bing else []
        return {
            "search": normalize_route("search", {
                "enabled": True, "preferred": provider, "fallbacks": search_fallbacks,
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS, "max_attempts": 1 + len(search_fallbacks)}),
            "fetch": normalize_route("fetch", {
                "enabled": True, "preferred": fetch, "fallbacks": fetch_fallbacks,
                "timeout_seconds": DEFAULT_TIMEOUT_SECONDS, "max_attempts": 1 + len(fetch_fallbacks)}),
            "ocr": normalize_route("ocr", _route_defaults("ocr")),
            "image_search": normalize_route("image_search", _route_defaults("image_search")),
        }

    def effective(self):
        row = self.service.db.one("SELECT * FROM capability_settings WHERE id=1")
        defaults = self.defaults()
        if row:
            config = migrate_config(json.loads(row["config"]), defaults)
            return {"revision": row["revision"], "origin": "administrator", "updated_at": row["updated_at"],
                    "schema_version": 2, **config}
        return {"revision": 0, "origin": "environment", "updated_at": None, "schema_version": 2, **defaults}

    def effective_route(self, capability: str) -> dict:
        state = self.effective()
        return {**state[capability], "revision": state["revision"]}

    def _credential_present(self, provider_id: str) -> bool:
        from .capability_credentials import CapabilityCredentials
        return CapabilityCredentials(self.service).present(provider_id)

    def providers(self):
        settings = self.service.settings
        # Inspect readiness booleans only; never return a credential or endpoint.
        from .web_tools import public_url

        def public_endpoint(value):
            try:
                parsed = urlsplit(public_url(value))
                return bool(parsed.hostname)
            except (AppError, ValueError, TypeError):
                return False

        def official_deepseek():
            try:
                parsed = urlsplit(settings.deepseek_api_base_url)
                return parsed.scheme == "https" and (parsed.hostname or "").lower() == "api.deepseek.com"
            except (ValueError, TypeError):
                return False

        deepseek_enabled = bool(settings.deepseek_api_enabled)
        deepseek_ready = deepseek_enabled and official_deepseek() and self._credential_present("deepseek_api")
        availability = {
            "bing_rss": True,
            "public_http": True,
            "searxng": public_endpoint(settings.web_search_searxng_url),
            "brave": public_endpoint(settings.web_search_brave_url) and self._credential_present("brave"),
            "deepseek_api": deepseek_ready,
            "deepseek_webtools": deepseek_enabled and official_deepseek() and self._credential_present("deepseek_webtools"),
            # The official Images endpoint is fixed; a present key is not proof
            # of product entitlement, so verification stays not_tested.
            "brave_images": self._credential_present("brave_images"),
        }
        verification = self._verification()
        result = []
        for name, configured in availability.items():
            modes = PROVIDER_MODES.get(name, {})
            checks = verification.get(name, {})
            result.append({
                "id": name,
                "configured": bool(configured),
                "credential_present": self._credential_present(name),
                "verification": self._verification_summary(checks),
                "verification_checks": checks,
                "capabilities": list(modes),
                "modes": modes,
                "revision": self._provider_revision(name),
            })
        return result

    def _verification(self) -> dict[str, dict[str, str]]:
        try:
            rows = self.service.db.all(
                "SELECT provider_id,capability,status,checked_at FROM capability_provider_verification")
        except Exception:
            return {}
        result: dict[str, dict[str, str]] = {}
        for row in rows:
            result.setdefault(row["provider_id"], {})[row["capability"]] = row["status"]
        return result

    @staticmethod
    def _verification_summary(checks: dict[str, str]) -> str:
        statuses = set(checks.values())
        if not statuses:
            return "not_tested"
        if statuses <= {"verified"}:
            return "verified"
        if statuses & {"failed", "not_configured"}:
            return "failed"
        return "not_tested"

    def _provider_revision(self, provider_id: str) -> int:
        from .capability_credentials import CapabilityCredentials
        return CapabilityCredentials(self.service).revision(provider_id)

    def view(self):
        result = self.effective()
        result["providers"] = self.providers()
        result["credentials"] = {
            item["id"]: {"present": item["credential_present"], "revision": item["revision"],
                         "verified": item["verification"] == "verified"}
            for item in result["providers"]
        }
        return result

    def narrow_assistant_grants(self, grants: dict, *, is_admin: bool) -> dict:
        """Persist only grants that narrow the current global route.

        Runtime always intersects again, but writing the honest narrowed value
        prevents a member editor (or a stale client) from appearing to widen a
        domain, egress mode or limit.  ``auto_approved`` is administrator-only.
        """
        from .capability_policy import _min_limit, intersect_domains, normalize_domain_policy, stricter_egress
        result = {}
        for capability in CAPABILITY_KINDS:
            grant = grants.get(capability)
            if not isinstance(grant, dict):
                continue
            route = self.effective_route(capability)
            domains = grant.get("domains")
            if domains is not None:
                domains = normalize_domain_policy(domains)
            global_egress = route.get("egress") or {}
            egress = {key: stricter_egress(global_egress.get(key), (grant.get("egress") or {}).get(key))
                      for key in EGRESS_KEYS}
            global_limits = route.get("limits") or {}
            limits = {key: _min_limit([global_limits.get(key), (grant.get("limits") or {}).get(key)],
                                      int(global_limits.get(key) or 0)) for key in DEFAULT_LIMITS}
            normalized = {
                "enabled": bool(grant.get("enabled")),
                "auto_approved": bool(grant.get("auto_approved")) and is_admin,
                "domains": intersect_domains(route.get("domains"), domains),
                "egress": egress,
                "limits": limits,
            }
            if grant.get("max_attempts"):
                normalized["max_attempts"] = min(int(grant["max_attempts"]), int(route.get("max_attempts") or 1))
            result[capability] = normalized
        return result

    def require_enabled(self, capability):
        if capability not in CAPABILITY_KINDS:
            raise AppError(422, "CAPABILITY_UNKNOWN", "未知能力")
        if not self.effective()[capability]["enabled"]:
            raise AppError(403, "CAPABILITY_DISABLED", "管理员已停用此能力")

    def knowledge_ocr_egress_allowed(self) -> bool:
        """Old knowledge-ingestion OCR may only send when explicitly allowed.

        There is no interactive approver on that background path, so an
        ``approval`` mode must not be treated as permission.  The tombstone /
        environment switch alone is not enough: the global OCR route must be
        enabled and its upload egress explicitly ``allow``.
        """
        state = self.effective()["ocr"]
        return bool(state["enabled"] and state["egress"]["upload"] == "allow")

    def save(self, user, payload: CapabilityUpdate):
        self.service.require_admin(user)
        if self.service.policy.is_trial:
            raise AppError(403, "FEATURE_DISABLED", "受控试用不允许修改能力配置")
        current = self.effective()
        raw = {}
        for capability in CAPABILITY_KINDS:
            supplied = getattr(payload, capability, None)
            if supplied is None:
                raw[capability] = {key: current[capability][key] for key in
                                   ("enabled", "preferred", "fallbacks", "timeout_seconds", "max_attempts",
                                    "domains", "egress", "limits")}
            else:
                raw[capability] = supplied.model_dump()
        config = {capability: normalize_route(capability, raw[capability], current[capability])
                  for capability in CAPABILITY_KINDS}
        availability = {item["id"]: item["configured"] for item in self.providers()}
        for kind, route in config.items():
            selected = [route["preferred"], *route["fallbacks"]]
            if any(item not in PROVIDERS[kind] for item in selected):
                raise AppError(422, "CAPABILITY_PROVIDER_INVALID", "提供方不支持所选能力")
            if route["enabled"] and any(not availability[item] for item in selected):
                raise AppError(422, "CAPABILITY_PROVIDER_UNCONFIGURED", "请先由部署管理员配置所选提供方的服务端依赖")
        with self.service.db.transaction():
            current_user = self.service.require("users", user["id"])
            self.service.require_admin(current_user)
            if not current_user["enabled"]:
                raise AppError(403, "USER_DISABLED", "账号已停用")
            if self.effective()["revision"] != payload.revision:
                raise AppError(409, "CAPABILITY_REVISION_CONFLICT", "配置已被其他管理员修改，请重新加载后再保存")
            revision, timestamp = payload.revision + 1, now()
            self.service.db.execute(
                "INSERT INTO capability_settings(id,revision,config,updated_at,updated_by) VALUES(1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,config=excluded.config,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                (revision, dump(config), timestamp, user["id"]))
            self.service.db.execute("INSERT INTO capability_settings_audit VALUES(?,?,?,?)", (revision, dump(config), timestamp, user["id"]))
        return self.view()


class CredentialUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=0)
    credential: str = Field(min_length=1, max_length=4096)


class CredentialDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(strict=True, ge=0)


def _require_secure_transport(request: Request, service) -> None:
    from .capability_credentials import secure_transport
    if not secure_transport(request, local_dev=bool(service.settings.capability_local_dev)):
        # The credential is write-only; refuse it before the body is consumed
        # or persisted when the channel is not proven HTTPS.
        raise AppError(403, "CAPABILITY_INSECURE_TRANSPORT", "凭据操作必须通过 HTTPS 或受信任的本地开发通道")


def create_capability_settings_router(svc, admin):
    router = APIRouter(prefix="/api/v1/capability-settings")

    @router.get("")
    def read(user=Depends(admin), service=Depends(svc)):
        return CapabilitySettings(service).view()

    @router.put("")
    def update(payload: CapabilityUpdate, user=Depends(admin), service=Depends(svc)):
        return CapabilitySettings(service).save(user, payload)

    @router.put("/providers/{provider_id}/credential")
    def put_credential(provider_id: str, payload: CredentialUpdate, request: Request,
                       user=Depends(admin), service=Depends(svc)):
        _require_secure_transport(request, service)
        from .capability_credentials import CapabilityCredentials
        return CapabilityCredentials(service).put(user, provider_id, payload.revision, payload.credential)

    @router.delete("/providers/{provider_id}/credential")
    def delete_credential(provider_id: str, payload: CredentialDelete, request: Request,
                          user=Depends(admin), service=Depends(svc)):
        _require_secure_transport(request, service)
        from .capability_credentials import CapabilityCredentials
        return CapabilityCredentials(service).delete(user, provider_id, payload.revision)

    @router.post("/providers/{provider_id}/diagnostics")
    async def diagnostics(provider_id: str, request: Request, user=Depends(admin), service=Depends(svc)):
        _require_secure_transport(request, service)
        from .capability_diagnostics import CapabilityDiagnostics
        return await CapabilityDiagnostics(service).run(provider_id)

    return router
