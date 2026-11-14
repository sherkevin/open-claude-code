"""Narrow, audited, write-only credential store for capability providers.

Rules enforced here:

* Only an enabled, non-trial administrator may replace or delete a credential.
* The database stores only ``SecretStore`` ciphertext; no plaintext column.
* Reads never return the value to the client.  ``present``/``revision`` are the
  only public facts.
* Replacing or deleting advances a revision under ``BEGIN IMMEDIATE`` so a
  stale administrator cannot overwrite a newer secret.
* Deleting writes a tombstone.  ``get`` then returns ``None`` instead of
  silently falling back to an environment credential, so clearing the database
  override cannot reactivate an old environment key.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .db import now
from .security import AppError

# provider_id -> environment attribute on Settings.
ENV_FIELDS = {
    "deepseek_api": "deepseek_api_key",
    "deepseek_webtools": "deepseek_api_key",
    "brave": "web_search_brave_api_key",
    "brave_images": "web_search_brave_api_key",
}
KNOWN_PROVIDERS = tuple(ENV_FIELDS)


def _loopback(value: str | None) -> bool:
    return (value or "").strip().lower() in {"127.0.0.1", "::1"}


def secure_transport(request, *, trusted_proxy: bool = False, local_dev: bool = False) -> bool:
    """Prove the credential write used authentic transport.

    Only the server/middleware-validated scheme counts.  A client-supplied
    ``X-Forwarded-Proto`` is accepted solely when the deployment explicitly
    declares a trusted proxy that overwrites it.  The ``Host`` header is never
    transport evidence.  Local development is an explicit opt-in and still
    requires the actual socket peer to be loopback.
    """
    try:
        if request.url.scheme == "https":
            return True
        if trusted_proxy:
            forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if forwarded == "https":
                return True
        if local_dev and request.url.scheme == "http":
            peer = getattr(getattr(request, "client", None), "host", None)
            return _loopback(peer) and _loopback(request.url.hostname)
        return False
    except Exception:
        return False


class CapabilityCredentials:
    def __init__(self, service):
        self.service = service
        self.db = service.db

    def _env_value(self, provider_id: str) -> str | None:
        field = ENV_FIELDS.get(provider_id)
        if not field:
            return None
        value = getattr(self.service.settings, field, "") or ""
        return value.strip() or None

    def _row(self, provider_id: str):
        return self.db.one("SELECT * FROM capability_credentials WHERE provider_id=?", (provider_id,))

    def present(self, provider_id: str) -> bool:
        return bool(self.get(provider_id))

    def revision(self, provider_id: str) -> int:
        row = self._row(provider_id)
        if row:
            return int(row["revision"])
        return 1 if self._env_value(provider_id) else 0

    def get(self, provider_id: str) -> str | None:
        """Resolve the effective credential without exposing it publicly."""
        if provider_id not in KNOWN_PROVIDERS:
            return None
        row = self._row(provider_id)
        if row:
            if row["deleted"]:
                return None
            return self.service.secrets.decrypt(row["secret"])
        return self._env_value(provider_id)

    def _check_actor(self, user):
        self.service.require_admin(user)
        if self.service.policy.is_trial:
            raise AppError(403, "FEATURE_DISABLED", "受控试用不允许管理能力凭据")
        current = self.service.require("users", user["id"])
        self.service.require_admin(current)
        if not current["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")

    def _validate(self, provider_id: str, credential: str) -> str:
        if provider_id not in KNOWN_PROVIDERS:
            raise AppError(422, "CAPABILITY_PROVIDER_INVALID", "未知的能力提供方")
        if not isinstance(credential, str):
            raise AppError(422, "CAPABILITY_CREDENTIAL_INVALID", "凭据格式无效")
        value = credential.strip()
        if not 1 <= len(value) <= 4096 or any(char in value for char in "\r\n\x00"):
            raise AppError(422, "CAPABILITY_CREDENTIAL_INVALID", "凭据长度或字符无效")
        return value

    def put(self, user, provider_id: str, revision: int, credential: str) -> dict:
        value = self._validate(provider_id, credential)
        with self.db.transaction():
            self._check_actor(user)
            if self.revision(provider_id) != revision:
                raise AppError(409, "CAPABILITY_REVISION_CONFLICT", "凭据已被其他管理员修改，请重新加载")
            new_revision = revision + 1
            timestamp = now()
            self.db.execute(
                "INSERT INTO capability_credentials(provider_id,secret,revision,updated_at,updated_by,deleted) "
                "VALUES(?,?,?,?,?,0) ON CONFLICT(provider_id) DO UPDATE SET secret=excluded.secret,"
                "revision=excluded.revision,updated_at=excluded.updated_at,updated_by=excluded.updated_by,deleted=0",
                (provider_id, self.service.secrets.encrypt(value), new_revision, timestamp, user["id"]))
            self.db.execute(
                "INSERT INTO capability_credential_audit(provider_id,action,revision,updated_by,created_at) "
                "VALUES(?,?,?,?,?)", (provider_id, "replace", new_revision, user["id"], timestamp))
            # A new credential invalidates prior diagnostics.
            self.db.execute("DELETE FROM capability_provider_verification WHERE provider_id=?", (provider_id,))
        return {"provider_id": provider_id, "present": True, "revision": new_revision, "verified": False}

    def delete(self, user, provider_id: str, revision: int) -> dict:
        if provider_id not in KNOWN_PROVIDERS:
            raise AppError(422, "CAPABILITY_PROVIDER_INVALID", "未知的能力提供方")
        with self.db.transaction():
            self._check_actor(user)
            if self.revision(provider_id) != revision:
                raise AppError(409, "CAPABILITY_REVISION_CONFLICT", "凭据已被其他管理员修改，请重新加载")
            new_revision = revision + 1
            timestamp = now()
            self.db.execute(
                "INSERT INTO capability_credentials(provider_id,secret,revision,updated_at,updated_by,deleted) "
                "VALUES(?,?,?,?,?,1) ON CONFLICT(provider_id) DO UPDATE SET secret='',revision=excluded.revision,"
                "updated_at=excluded.updated_at,updated_by=excluded.updated_by,deleted=1",
                (provider_id, "", new_revision, timestamp, user["id"]))
            self.db.execute(
                "INSERT INTO capability_credential_audit(provider_id,action,revision,updated_by,created_at) "
                "VALUES(?,?,?,?,?)", (provider_id, "delete", new_revision, user["id"], timestamp))
            self.db.execute("DELETE FROM capability_provider_verification WHERE provider_id=?", (provider_id,))
        # The tombstone remains authoritative: an environment key is not
        # reactivated by deleting the database override.
        return {"provider_id": provider_id, "present": False, "revision": new_revision, "verified": False}
