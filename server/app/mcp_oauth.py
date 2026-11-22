"""Preregistered OAuth clients for private MCP connections on the trusted host."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    issuers_match,
)
from mcp.shared.auth import OAuthMetadata, OAuthToken, ProtectedResourceMetadata

from .db import dump, now, offload, offload_write, uid
from .mcp_tools import transport_logs
from .security import AppError, token_hash, validate_mcp_url

CALLBACK_PATH = "/api/v1/connections/oauth/callback/"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
STATE_TTL = 600
MAX_PAYLOAD = 64 * 1024


class CallbackAccessLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            address, method, path, protocol, status = record.args
            if isinstance(path, str) and path.startswith(CALLBACK_PATH):
                record.args = address, method, path.partition("?")[0], protocol, status
        return True


callback_access_log_filter = CallbackAccessLogFilter()


@dataclass(frozen=True)
class Profile:
    key: str
    resource: str
    issuer: str
    client_id: str
    client_secret: str | None = field(repr=False)
    redirect_uri: str
    scope: str

    @property
    def digest(self):
        return token_hash(dump([self.key, self.resource, self.issuer, self.client_id,
                                self.client_secret, self.redirect_uri, self.scope]))

    @property
    def auth_method(self):
        return "client_secret_basic" if self.client_secret else "none"


def secure_url(value, settings):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Invalid OAuth URL")
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query
            or re.search(r"[\\\x00-\x20\x7f]", value)
            or parsed.scheme != "https" and not (settings.mcp_oauth_loopback and parsed.scheme == "http" and parsed.hostname in LOOPBACK)):
        raise ValueError("Invalid OAuth URL")
    return parsed


def profiles(settings):
    if not settings.mcp_oauth_profiles or settings.runtime_type != "harness" or settings.deployment_mode == "trial":
        return {}
    try:
        raw = Path(settings.mcp_oauth_profiles).read_bytes()
        data = json.loads(raw) if len(raw) <= MAX_PAYLOAD else None
        if not isinstance(data, dict) or len(data) > 32:
            raise ValueError()
        result = {}
        for key, value in data.items():
            if not re.fullmatch(r"[a-z0-9_-]{1,48}", key):
                raise ValueError()
            resource, issuer, redirect = (value[name] for name in ("resource", "issuer", "redirect_uri"))
            for url in (resource, issuer, redirect):
                secure_url(url, settings)
            validate_mcp_url(resource, settings.mcp_hosts)
            client_id, scope = value["client_id"], value["scope"]
            secret_env = value.get("client_secret_env")
            secret = os.getenv(secret_env) if secret_env else None
            if (resource in result or urlsplit(redirect).path != CALLBACK_PATH + key
                    or not isinstance(client_id, str) or not client_id or len(client_id) > 512
                    or not isinstance(scope, str) or not re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e ]{1,512}", scope) or not scope.split()
                    or secret_env and (not secret or len(secret) > 4096)):
                raise ValueError()
            result[resource] = Profile(key, resource, issuer, client_id, secret, redirect, " ".join(scope.split()))
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, AppError):
        raise ValueError("Invalid deployment MCP OAuth profiles") from None


class MCPOAuth:
    def __init__(self, service):
        self.service = service
        self.profiles = profiles(service.settings)
        if self.profiles:
            logging.getLogger("uvicorn.access").addFilter(callback_access_log_filter)

    def profile(self, resource):
        profile = self.profiles.get(resource)
        if not profile:
            raise AppError(403, "MCP_OAUTH_PROFILE_REQUIRED", "此地址尚未配置 OAuth，请联系工作台管理员")
        return profile

    def _owner(self, connection_id, user):
        row = self.service.require_resource("connections", connection_id, user, edit=True)
        if row["auth_type"] != "oauth" or row["owner_id"] != user["id"] or row["scope"] != "private":
            raise AppError(403, "MCP_OAUTH_OWNER_REQUIRED", "只有连接所有者可以管理自己的 MCP 授权")
        return row

    @staticmethod
    async def _request(method, url, **kwargs):
        with transport_logs():
            async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3), trust_env=False, follow_redirects=False,
                                         headers={"Accept-Encoding": "identity", "Accept": "application/json"}) as client:
                async with client.stream(method, url, **kwargs) as response:
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("Unexpected OAuth content encoding")
                    content = bytearray()
                    async for part in response.aiter_bytes():
                        content.extend(part)
                        if len(content) > MAX_PAYLOAD:
                            raise ValueError("OAuth response too large")
                    return response.status_code, bytes(content)

    async def _metadata(self, profile):
        try:
            protected = None
            for url in build_protected_resource_metadata_discovery_urls(None, profile.resource):
                status, data = await self._request("GET", url)
                if status == 200:
                    protected = ProtectedResourceMetadata.model_validate_json(data)
                    break
            if (not protected or not issuers_match(str(protected.resource), profile.resource)
                    or not any(issuers_match(str(issuer), profile.issuer) for issuer in protected.authorization_servers)):
                raise ValueError()
            if protected.scopes_supported is not None and not set(profile.scope.split()) <= set(protected.scopes_supported):
                raise ValueError()
            metadata = None
            for url in build_oauth_authorization_server_metadata_discovery_urls(profile.issuer, profile.resource):
                status, data = await self._request("GET", url)
                if status == 200:
                    metadata = OAuthMetadata.model_validate_json(data)
                    break
            if (not metadata or not issuers_match(str(metadata.issuer), profile.issuer)
                    or "S256" not in (metadata.code_challenge_methods_supported or ())
                    or "code" not in metadata.response_types_supported
                    or profile.auth_method not in (metadata.token_endpoint_auth_methods_supported or ["client_secret_basic"])
                    or not metadata.revocation_endpoint
                    or profile.auth_method not in (metadata.revocation_endpoint_auth_methods_supported or metadata.token_endpoint_auth_methods_supported or ["client_secret_basic"])):
                raise ValueError()
            endpoints = {"authorization": str(metadata.authorization_endpoint), "token": str(metadata.token_endpoint),
                         "revoke": str(metadata.revocation_endpoint)}
            self._validate_endpoints(profile, endpoints)
            return endpoints
        except (httpx.HTTPError, ValueError, TypeError):
            raise AppError(422, "MCP_OAUTH_METADATA_INVALID", "OAuth 元数据不符合已配置的地址、权限或安全要求") from None

    def _validate_endpoints(self, profile, endpoints):
        issuer = secure_url(profile.issuer, self.service.settings)
        for value in endpoints.values():
            url = secure_url(value, self.service.settings)
            if (url.scheme, url.hostname, url.port) != (issuer.scheme, issuer.hostname, issuer.port):
                raise ValueError("OAuth endpoint must belong to the pinned issuer")

    def _audit(self, row, event):
        self.service.db.execute("INSERT INTO mcp_oauth_events VALUES(?,?,?,?,?)",
                                (uid(), row["id"], row["owner_id"], event, now()))

    async def start(self, connection_id, user, session_hash):
        row = await offload(self._owner, connection_id, user)
        if not row["enabled"]:
            raise AppError(409, "CONNECTION_DISABLED", "请先启用此连接")
        if row["secret"]:
            raise AppError(409, "MCP_OAUTH_ALREADY_LINKED", "请先断开当前 OAuth 授权")
        profile = self.profile(row["url"])
        try:
            endpoints = await asyncio.wait_for(self._metadata(profile), timeout=10)
        except asyncio.TimeoutError:
            raise AppError(504, "MCP_OAUTH_METADATA_TIMEOUT", "读取 OAuth 元数据超时，请稍后重试") from None
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")

        def save_pending():
            db = self.service.db
            with db.transaction():
                current = self._owner(connection_id, user)
                if current["revision"] != row["revision"] or not current["enabled"]:
                    raise AppError(409, "MCP_OAUTH_CHANGED", "连接已更改，请重新开始 OAuth 授权")
                db.execute("DELETE FROM mcp_oauth_pending WHERE connection_id=? OR expires<=?", (connection_id, int(time.time())))
                db.execute("INSERT INTO mcp_oauth_pending VALUES(?,?,?,?,?,?,?,?,?)",
                           (token_hash(state), connection_id, row["revision"], user["id"], session_hash, profile.digest,
                            self.service.secrets.encrypt(verifier), dump(endpoints), int(time.time()) + STATE_TTL))
                self._audit(row, "link_started")

        await offload_write(save_pending)
        return endpoints["authorization"] + "?" + urlencode({
            "response_type": "code", "client_id": profile.client_id, "redirect_uri": profile.redirect_uri,
            "resource": profile.resource, "scope": profile.scope, "state": state,
            "code_challenge_method": "S256", "code_challenge": challenge,
        })

    async def _post(self, profile, endpoint, params):
        auth = None
        if profile.client_secret:
            auth = httpx.BasicAuth(quote_plus(profile.client_id), quote_plus(profile.client_secret))
        else:
            params = {**params, "client_id": profile.client_id}
        return await asyncio.wait_for(self._request("POST", endpoint, auth=auth, data=params), timeout=8)

    async def _token(self, profile, endpoint, data):
        try:
            status, content = await self._post(profile, endpoint, {**data, "resource": profile.resource})
            if status in {400, 401} and json.loads(content).get("error") == "invalid_grant":
                raise AppError(409, "MCP_OAUTH_RELINK_REQUIRED", "OAuth 授权已失效，请重新授权")
            if status != 200:
                raise ValueError()
            token = OAuthToken.model_validate_json(content)
            if (not token.expires_in or not 1 <= token.expires_in <= 604800
                    or not re.fullmatch(r"[A-Za-z0-9\-._~+/]+=*", token.access_token)
                    or len(token.access_token) > 8192 or len(token.refresh_token or "") > 8192
                    or token.scope is not None and not set(token.scope.split()) <= set(profile.scope.split())):
                raise ValueError()
            return token
        except (ValueError, TypeError, AttributeError, httpx.HTTPError, asyncio.TimeoutError):
            raise AppError(502, "MCP_OAUTH_ISSUER_UNAVAILABLE", "OAuth 供应方未能安全返回凭据，请稍后重试") from None

    def _consume(self, key, user, session_hash, state, issuer):
        db = self.service.db
        with db.transaction():
            pending = db.one("SELECT * FROM mcp_oauth_pending WHERE state_hash=? AND user_id=? AND session_hash=? AND expires>?",
                             (token_hash(state), user["id"], session_hash, int(time.time())))
            if not pending:
                raise AppError(403, "MCP_OAUTH_STATE_INVALID", "OAuth 授权请求已过期或不属于此登录会话")
            row = self._owner(pending["connection_id"], user)
            profile = self.profile(row["url"])
            if (not row["enabled"] or row["revision"] != pending["revision"] or profile.key != key
                    or profile.digest != pending["profile_digest"] or issuer and not issuers_match(issuer, profile.issuer)):
                raise AppError(409, "MCP_OAUTH_CHANGED", "连接已更改，请重新开始 OAuth 授权")
            db.execute("DELETE FROM mcp_oauth_pending WHERE state_hash=?", (pending["state_hash"],))
        return row, profile, pending

    async def complete(self, key, user, session_hash, state, code, issuer):
        row, profile, pending = await offload_write(self._consume, key, user, session_hash, state, issuer)
        if not code:
            await offload_write(self._audit, row, "link_denied")
            raise AppError(409, "MCP_OAUTH_DENIED", "OAuth 供应方未授予访问权限")
        endpoints = json.loads(pending["endpoints"])
        self._validate_endpoints(profile, endpoints)
        token = await self._token(profile, endpoints["token"], {
            "grant_type": "authorization_code", "code": code, "redirect_uri": profile.redirect_uri,
            "code_verifier": self.service.secrets.decrypt(pending["verifier"]),
        })

        def save_grant():
            db = self.service.db
            with db.transaction():
                try:
                    current = self._owner(row["id"], user)
                except AppError:
                    return False
                session = db.one("SELECT 1 FROM sessions JOIN users ON users.id=sessions.user_id "
                                 "WHERE token_hash=? AND user_id=? AND expires_at>? AND users.enabled=1",
                                 (session_hash, user["id"], now()))
                if current["revision"] != row["revision"] or not current["enabled"] or not session:
                    return False
                db.execute("UPDATE connections SET secret=?,revision=revision+1,status='untested',last_error=NULL WHERE id=?",
                           (self.service.secrets.encrypt(token.access_token), row["id"]))
                db.execute("INSERT OR REPLACE INTO mcp_oauth_grants(connection_id,profile_digest,endpoints,refresh_token,expires) VALUES(?,?,?,?,?)",
                           (row["id"], profile.digest, pending["endpoints"], self.service.secrets.encrypt(token.refresh_token) if token.refresh_token else None,
                            int(time.time()) + token.expires_in))
                self._audit(row, "linked")
            return True

        if not await offload_write(save_grant):
            await self._revoke_remote(profile, endpoints, token.refresh_token or token.access_token, bool(token.refresh_token))
            raise AppError(409, "MCP_OAUTH_CHANGED", "连接已更改，请重新开始 OAuth 授权")
        return row["id"]

    def configured(self, row):
        profile = self.profiles.get(row["url"])
        grant = self.service.db.one("SELECT profile_digest FROM mcp_oauth_grants WHERE connection_id=?", (row["id"],))
        return bool(profile and row["secret"] and grant and grant["profile_digest"] == profile.digest)

    def _lease(self, row, profile, lease):
        db = self.service.db
        with db.transaction():
            current = self.service.require("connections", row["id"])
            grant = db.one("SELECT * FROM mcp_oauth_grants WHERE connection_id=?", (row["id"],))
            timestamp = int(time.time())
            if (not current["enabled"] or current["revision"] != row["revision"]
                    or not current["secret"] or not grant or grant["profile_digest"] != profile.digest):
                raise AppError(409, "MCP_OAUTH_RELINK_REQUIRED", "OAuth 授权已失效，请重新授权")
            if grant["expires"] > timestamp + 15 or not grant["refresh_token"] and grant["expires"] > timestamp:
                return self.service.secrets.decrypt(current["secret"]), None
            if not grant["refresh_token"]:
                raise AppError(409, "MCP_OAUTH_RELINK_REQUIRED", "OAuth 授权已过期，请重新授权")
            if grant["refresh_until"] > timestamp:
                return None, None
            db.execute("UPDATE mcp_oauth_grants SET refresh_owner=?,refresh_until=? WHERE connection_id=?",
                       (lease, timestamp + 15, row["id"]))
            return None, grant

    async def access_token(self, row):
        profile, owner = self.profile(row["url"]), uid()
        current = await offload(self.service.db.one, "SELECT c.secret FROM connections c JOIN mcp_oauth_grants g "
                                "ON g.connection_id=c.id WHERE c.id=? AND c.enabled=1 AND c.deleted=0 AND c.revision=? "
                                "AND g.profile_digest=? AND g.expires>?", (row["id"], row["revision"], profile.digest, int(time.time()) + 15))
        if current and current["secret"]:
            return self.service.secrets.decrypt(current["secret"])
        for _ in range(41):
            access, grant = await offload_write(self._lease, row, profile, owner)
            if access:
                return access
            if grant:
                break
            await asyncio.sleep(0.2)
        if not grant:
            raise AppError(503, "MCP_OAUTH_REFRESH_BUSY", "OAuth 授权正在刷新，请稍后重试")
        db = self.service.db
        endpoints = json.loads(grant["endpoints"])
        try:
            self._validate_endpoints(profile, endpoints)
            token = await self._token(profile, endpoints["token"], {"grant_type": "refresh_token",
                "refresh_token": self.service.secrets.decrypt(grant["refresh_token"])})

            def save_refresh():
                with db.transaction():
                    if not db.one("SELECT 1 FROM connections WHERE id=? AND revision=? AND enabled=1 AND deleted=0",
                                  (row["id"], row["revision"])):
                        return False
                    updated = db.execute("UPDATE mcp_oauth_grants SET refresh_token=?,expires=? WHERE connection_id=? AND refresh_owner=?",
                        (self.service.secrets.encrypt(token.refresh_token) if token.refresh_token else grant["refresh_token"],
                         int(time.time()) + token.expires_in, row["id"], owner))
                    if updated.rowcount != 1:
                        return False
                    db.execute("UPDATE connections SET secret=? WHERE id=?", (self.service.secrets.encrypt(token.access_token), row["id"]))
                    self._audit(row, "refreshed")
                return True

            if not await offload_write(save_refresh):
                await self._revoke_remote(profile, endpoints, token.refresh_token or token.access_token, bool(token.refresh_token))
                raise AppError(409, "MCP_OAUTH_CHANGED", "OAuth 授权在刷新时已撤销或更改")
            return token.access_token
        except AppError as error:
            if error.code == "MCP_OAUTH_RELINK_REQUIRED":
                await offload_write(db.execute, "UPDATE connections SET status='needs_auth',last_error=? WHERE id=? AND revision=?",
                                    (error.message, row["id"], row["revision"]))
            raise
        finally:
            await offload_write(db.execute, "UPDATE mcp_oauth_grants SET refresh_owner=NULL,refresh_until=0 WHERE connection_id=? AND refresh_owner=?", (row["id"], owner))

    async def _revoke_remote(self, profile, endpoints, token, refresh):
        try:
            self._validate_endpoints(profile, endpoints)
            status, _ = await self._post(profile, endpoints["revoke"], {"token": token,
                "token_type_hint": "refresh_token" if refresh else "access_token"})
            return status == 200
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError):
            return False

    async def revoke(self, connection_id, user, *, delete=False):
        def remove_grant():
            db = self.service.db
            with db.transaction():
                row = self._owner(connection_id, user)
                grant = db.one("SELECT * FROM mcp_oauth_grants WHERE connection_id=?", (connection_id,))
                db.execute("DELETE FROM mcp_oauth_grants WHERE connection_id=?", (connection_id,))
                db.execute("DELETE FROM mcp_oauth_pending WHERE connection_id=?", (connection_id,))
                db.execute("UPDATE connections SET secret=NULL,revision=revision+1,status='needs_auth',last_error=?,deleted=?,enabled=? WHERE id=?",
                           ("OAuth 授权已断开", int(delete), int(row["enabled"] and not delete), connection_id))
                self._audit(row, "deleted" if delete else "unlinked")
            return row, grant

        row, grant = await offload_write(remove_grant)
        if not grant:
            return True
        profile = self.profiles.get(row["url"])
        refresh = self.service.secrets.decrypt(grant["refresh_token"])
        token = refresh or self.service.secrets.decrypt(row["secret"])
        remote = bool(profile and profile.digest == grant["profile_digest"] and token and await self._revoke_remote(
            profile, json.loads(grant["endpoints"]), token, bool(refresh)))
        await offload_write(self._audit, row, "issuer_revoked" if remote else "issuer_revoke_unconfirmed")
        return remote
