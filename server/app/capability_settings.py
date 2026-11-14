"""Provider-neutral, secret-free policy for the existing web tool executors."""
from __future__ import annotations

import json
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .db import dump, now
from .security import AppError

PROVIDERS = {
    "search": ("bing_rss", "searxng", "brave", "deepseek_api"),
    "fetch": ("public_http", "deepseek_api"),
}


class CapabilityRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    preferred: Literal["bing_rss", "searxng", "brave", "deepseek_api", "public_http"]
    fallbacks: list[Literal["bing_rss", "searxng", "brave", "deepseek_api", "public_http"]] = Field(max_length=1)
    timeout_seconds: int = Field(strict=True, ge=1, le=60)

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


class CapabilitySettings:
    def __init__(self, service):
        self.service = service

    def defaults(self):
        settings = self.service.settings
        provider = settings.web_search_provider
        fetch = "deepseek_api" if provider == "deepseek_api" else "public_http"
        return {
            "search": {"enabled": True, "preferred": provider,
                       "fallbacks": ["bing_rss"] if provider != "bing_rss" and settings.web_search_fallback_bing else [],
                       "timeout_seconds": 60},
            "fetch": {"enabled": True, "preferred": fetch,
                      "fallbacks": ["public_http"] if fetch != "public_http" and settings.web_search_fallback_bing else [],
                      "timeout_seconds": 60},
        }

    def effective(self):
        row = self.service.db.one("SELECT * FROM capability_settings WHERE id=1")
        if row:
            return {"revision": row["revision"], "origin": "administrator", "updated_at": row["updated_at"],
                    **json.loads(row["config"])}
        return {"revision": 0, "origin": "environment", "updated_at": None, **self.defaults()}

    def providers(self):
        settings = self.service.settings
        # Inspect only readiness booleans: no credential value or endpoint is returned.
        from .web_tools import public_url

        def public_endpoint(value):
            try:
                parsed = urlsplit(public_url(value))
                return bool(parsed.hostname)
            except (AppError, ValueError, TypeError):
                return False

        from .deepseek_api import _endpoint
        try:
            _endpoint(settings.deepseek_api_base_url, "/v1/tools/search")
            official = bool(settings.deepseek_api_enabled and settings.deepseek_api_key)
        except AppError:
            official = False
        availability = {
            "bing_rss": True,
            "public_http": True,
            "searxng": public_endpoint(settings.web_search_searxng_url),
            "brave": bool(settings.web_search_brave_api_key) and public_endpoint(settings.web_search_brave_url),
            "deepseek_api": official,
        }
        return [{"id": name, "configured": configured, "verification": "not_tested",
                 "capabilities": [kind for kind, providers in PROVIDERS.items() if name in providers]}
                for name, configured in availability.items()]

    def view(self):
        result = self.effective()
        result["providers"] = self.providers()
        result["ocr"] = {"status": "not_integrated", "configurable": False,
                         "knowledge_fallback_configured": bool(self.service.settings.deepseek_api_ocr_enabled
                             and next(p for p in result["providers"] if p["id"] == "deepseek_api")["configured"])}
        result["image_search"] = {"status": "not_integrated", "configurable": False}
        return result

    def require_enabled(self, capability):
        if not self.effective()[capability]["enabled"]:
            raise AppError(403, "CAPABILITY_DISABLED", "管理员已停用此能力")

    def save(self, user, payload: CapabilityUpdate):
        self.service.require_admin(user)
        if self.service.policy.is_trial:
            raise AppError(403, "FEATURE_DISABLED", "受控试用不允许修改能力配置")
        config = {key: getattr(payload, key).model_dump() for key in PROVIDERS}
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
            current = self.effective()
            if current["revision"] != payload.revision:
                raise AppError(409, "CAPABILITY_REVISION_CONFLICT", "配置已被其他管理员修改，请重新加载后再保存")
            revision, timestamp = payload.revision + 1, now()
            self.service.db.execute(
                "INSERT INTO capability_settings(id,revision,config,updated_at,updated_by) VALUES(1,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,config=excluded.config,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
                (revision, dump(config), timestamp, user["id"]))
            self.service.db.execute("INSERT INTO capability_settings_audit VALUES(?,?,?,?)", (revision, dump(config), timestamp, user["id"]))
        return self.view()


def create_capability_settings_router(svc, admin):
    router = APIRouter(prefix="/api/v1/capability-settings")

    @router.get("")
    def read(user=Depends(admin), service=Depends(svc)):
        return CapabilitySettings(service).view()

    @router.put("")
    def update(payload: CapabilityUpdate, user=Depends(admin), service=Depends(svc)):
        return CapabilitySettings(service).save(user, payload)

    return router
