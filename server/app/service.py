from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .config import Settings
from .db import Database, dump, now, uid
from .model_catalog import ModelCatalog
from .resources import TABLES, resource_access, resource_editable, scope_fields, choose_scope
from .security import AppError, SecretStore, password_hash, redact, tool_risk_profile, validate_mcp_url, workspace_path
from .subagents import SubagentOrchestrator
from .model_approval import ModelApprovalService
from .high_risk_approval import HighRiskApprovalService


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def user_view(row):
    return {key: bool(row[key]) if key == "enabled" else row[key] for key in ("id", "username", "display_name", "role", "enabled")}


def connection_view(row, user=None):
    return {"id": row["id"], "name": row["name"], "transport": "http", "url": row["url"], "auth_type": row["auth_type"], "credential_present": bool(row["secret"]), "enabled": bool(row["enabled"]), "status": row["status"], "tools": json.loads(row["tools"]), "last_error": row["last_error"], "last_tested_at": row["last_tested_at"], "revision": row["revision"], "source_metadata": json.loads(row.get("source_metadata") or "{}"), **scope_fields(row, user)}


def assistant_view(row, user=None):
    return {"id": row["id"], "name": row["name"], "description": row["description"], "instructions": row["instructions"], "enabled": bool(row["enabled"]), "access": json.loads(row["access"]), "connections": json.loads(row["connections"]), "revision": row["revision"], **scope_fields(row, user), "knowledge_base_ids": json.loads(row.get("knowledge_base_ids") or "[]"), "skill_ids": json.loads(row.get("skill_ids") or "[]"), "web_search_enabled": bool(row.get("web_search_enabled")), "default_model_id": row.get("default_model_id"), "default_reasoning_level": row.get("default_reasoning_level")}


def conversation_view(row):
    return {**{key: bool(row[key]) if key in {"archived", "auto_model_routing"} else row[key] for key in ("id", "assistant_id", "title", "archived", "active_run_id", "created_at", "updated_at", "model_id", "model_label", "reasoning_level", "auto_model_routing")}, "has_native_session": bool(row.get("sdk_session_id")), "category": row.get("category", ""), "tags": json.loads(row.get("tags") or "[]")}


def message_view(row):
    return {**row, "file_ids": json.loads(row["file_ids"]), "skills": json.loads(row.get("skills") or "[]"), "citations": json.loads(row.get("citations") or "[]")}


def file_view(row):
    return {key: row[key] for key in ("id", "name", "mime_type", "size", "created_at")}


def approval_view(row):
    profile = tool_risk_profile(row["tool_name"])
    return {
        **{key: redact(json.loads(row[key])) if key == "arguments" else row[key] for key in ("id", "run_id", "tool_name", "arguments", "status", "created_at", "expires_at")},
        **profile,
        # The digest lets clients and audit exports prove that the approved
        # arguments are the same immutable server-side snapshot without
        # exposing secrets contained in those arguments.
        "argument_digest": hashlib.sha256(row["arguments"].encode("utf-8")).hexdigest(),
        "decided_at": row.get("decided_at"),
        "decided_by": row.get("decided_by"),
        "decision_source": row.get("decision_source"),
        "strategy": "dual" if row.get("approval_mode") == "dual" else (row.get("strategy") or "user"),
        "approval_mode": row.get("approval_mode") or "single",
        "required_approvals": row.get("required_approvals") or 1,
        "approver_ids": json.loads(row.get("approver_ids") or "[]"),
        "approval_policy_id": row.get("approval_policy_id"),
        "approval_policy_revision": row.get("approval_policy_revision"),
        "approval_decision_reason": row.get("approval_decision_reason"),
        # A child approval is still owned by the parent run for authorization,
        # but the child identifier lets the progress view correlate the vote
        # without exposing its task prompt.
        "subagent_child_id": row.get("subagent_child_id"),
        "review": {
            "model_id": row.get("review_model_id"),
            "policy_id": row.get("review_policy_id"),
            "policy_revision": row.get("review_policy_revision"),
            "policy_digest": row.get("review_policy_digest"),
            "operation": row.get("review_operation"),
            "data_scope": row.get("review_data_scope"),
            "amount": row.get("review_amount"),
            "reason": row.get("review_reason"),
            "result": json.loads(row["review_result"]) if row.get("review_result") else None,
            "decided_at": row.get("review_decided_at"),
        } if row.get("review_model_id") or row.get("strategy") == "model" else None,
    }


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.prepare()
        self.db = Database(settings.data_dir / "app.sqlite3")
        self.catalog = ModelCatalog(settings)
        self._migrate_model_selections()
        self.secrets = SecretStore(settings.data_dir)
        self.manager = None
        from .memory import NativeMemory
        self.memory = NativeMemory(self)
        from .evidence import EvidenceService
        from .knowledge import KnowledgeService
        self.evidence = EvidenceService(self)
        self.knowledge = KnowledgeService(self)
        # The first sub-agent slice is local synthetic fan-out only.  It does
        # not share the parent SDK session and cannot access models, MCP or
        # network tools.
        self.subagents = SubagentOrchestrator(self)
        self.model_approval = ModelApprovalService(self)
        self.high_risk_approval = HighRiskApprovalService(self)

    def model_approval_policy(self, assistant_id: str) -> dict:
        return self.model_approval.policy_view(assistant_id)

    def save_model_approval_policy(self, assistant_id: str, data: dict, user: dict) -> dict:
        return self.model_approval.save_policy(assistant_id, data, user)

    def high_risk_approval_policy(self, assistant_id: str) -> dict:
        return self.high_risk_approval.policy_view(assistant_id)

    def save_high_risk_approval_policy(self, assistant_id: str, data: dict, user: dict) -> dict:
        return self.high_risk_approval.save_policy(assistant_id, data, user)

    def _migrate_model_selections(self):
        with self.db.transaction():
            for row in self.db.all("SELECT * FROM conversations"):
                if row["model_snapshot"]:
                    selection = json.loads(row["model_snapshot"])
                    if "provider_fingerprint" not in selection:
                        # Previous versions never recorded provider credential
                        # digests. Pin the current configuration from upgrade
                        # forward; do not invent historical provider evidence.
                        self._save_model_selection(row["id"], selection)
                    continue
                run = self.db.one("SELECT execution_meta FROM runs WHERE conversation_id=? AND execution_meta IS NOT NULL ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["id"],))
                metadata = json.loads(run["execution_meta"]) if run else {}
                selection = self.catalog.historical(metadata.get("model") or self.settings.model)
                self._save_model_selection(row["id"], selection)

    def _save_model_selection(self, conversation_id, selection):
        selection = self.pin_provider(selection)
        self.db.execute("UPDATE conversations SET model_id=?,model_label=?,reasoning_level=?,model_snapshot=?,context_usage=NULL WHERE id=?",
                        (selection["model_id"], selection["model_label"], selection["reasoning_level"], dump(selection), conversation_id))

    def model_selection(self, conversation):
        selection = json.loads(conversation["model_snapshot"])
        self.catalog.validate_snapshot(selection)
        self.validated_provider(selection)
        return selection

    def effective_provider(self, selection):
        provider = {**self.settings.provider_env(), **self.catalog.provider_overrides(selection)}
        for key in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL"):
            provider[key] = selection["upstream_model"]
        return provider

    @staticmethod
    def provider_fingerprint(provider):
        return hashlib.sha256(json.dumps(provider, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def pin_provider(self, selection):
        return {**selection, "provider_fingerprint": self.provider_fingerprint(self.effective_provider(selection))}

    def validated_provider(self, selection):
        provider = self.effective_provider(selection)
        if selection.get("provider_fingerprint") != self.provider_fingerprint(provider):
            raise AppError(409, "MODEL_SELECTION_REQUIRED", "模型服务配置已变化，请重新选择模型后继续")
        return provider

    def public_run_model(self, row):
        metadata = json.loads(row.get("execution_meta") or "{}")
        selection = metadata.get("selection") or {}
        return {"model": selection.get("model_label", "历史模型"), "model_id": selection.get("model_id", "legacy-model"),
                "reasoning_level": selection.get("reasoning_level"), "provider": "模型服务"}

    def public_context_data(self, value, label):
        # Historical compaction records may predate aliases. Only rewrite
        # structured metadata; user content is not searched or rewritten.
        if isinstance(value, dict):
            return {key: label if key == "model" else self.public_context_data(item, label) for key, item in value.items()}
        if isinstance(value, list):
            return [self.public_context_data(item, label) for item in value]
        return value

    def public_events(self, run_id, after):
        row = self.require("runs", run_id)
        label = self.public_run_model(row)["model"]
        return [{**event, "data": self.public_context_data(event["data"], label)} if event["type"].startswith("context.") else event for event in self.db.events(run_id, after)]

    def require(self, table, item_id, user=None):
        row = self.db.one(f"SELECT * FROM {table} WHERE id=?", (item_id,))
        if not row or row.get("deleted") or (user and row.get("user_id") != user["id"]):
            raise AppError(404, "NOT_FOUND", "资源不存在或无访问权限")
        return row

    def require_admin(self, user):
        if user["role"] != "admin":
            raise AppError(403, "ADMIN_REQUIRED", "此操作需要管理员权限")

    resource_access = staticmethod(resource_access)
    resource_editable = staticmethod(resource_editable)

    def require_resource(self, table, item_id, user, edit=False):
        if table not in TABLES:
            raise ValueError("Invalid resource table")
        row = self.require(table, item_id)
        if not self.resource_access(row, user):
            raise AppError(404, "NOT_FOUND", "资源不存在或无访问权限")
        if edit and not self.resource_editable(row, user):
            raise AppError(403, "RESOURCE_READ_ONLY", "您可以使用此共享配置，修改需由管理员完成")
        return row

    def validate_resource_scope_change(self, table, item_id, scope, owner_id):
        if scope != "private":
            return
        field = {"connections": "connections", "skills": "skill_ids", "knowledge_bases": "knowledge_base_ids"}.get(table)
        if not field:
            return
        for assistant in self.db.all("SELECT * FROM assistants WHERE deleted=0"):
            bindings = json.loads(assistant[field])
            ids = [item["connection_id"] for item in bindings] if table == "connections" else bindings
            if item_id in ids and (assistant["scope"] == "team" or assistant["owner_id"] != owner_id):
                raise AppError(409, "RESOURCE_IN_USE", "该配置仍被其他范围的助手使用，请先解除绑定")

    def assistant_access(self, assistant, user):
        access = json.loads(assistant["access"])
        return bool(self.resource_access(assistant, user) and assistant["enabled"] and (assistant.get("scope") == "private" or user["role"] == "admin" or access["all_members"] or user["id"] in access["user_ids"]))

    def usable_assistant(self, assistant_id, user):
        assistant = self.require_resource("assistants", assistant_id, user)
        if not self.assistant_access(assistant, user):
            raise AppError(403, "ASSISTANT_UNAVAILABLE", "此助手已停用或您没有使用权限")
        return assistant

    def create_user(self, data):
        if self.db.one("SELECT id FROM users WHERE username=?", (data["username"],)):
            raise AppError(409, "USERNAME_EXISTS", "用户名已存在")
        item_id = uid()
        self.db.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (item_id, data["username"], data["display_name"], password_hash(data["password"]), data["role"], 1, now()))
        return user_view(self.require("users", item_id))

    def patch_user(self, item_id, data, current):
        row = self.require("users", item_id)
        if row["role"] == "admin" and (data.get("enabled") is False or data.get("role") == "member"):
            count = self.db.one("SELECT COUNT(*) n FROM users WHERE role='admin' AND enabled=1")["n"]
            if count <= 1:
                raise AppError(409, "LAST_ADMIN", "至少保留一个启用的管理员账号")
        for key in ("display_name", "role", "enabled"):
            if data.get(key) is not None:
                self.db.execute(f"UPDATE users SET {key}=? WHERE id=?", (data[key], item_id))
        if data.get("password"):
            self.db.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash(data["password"]), item_id))
        if "password" in data or data.get("enabled") is False or "role" in data:
            self.db.execute("DELETE FROM sessions WHERE user_id=?", (item_id,))
        return user_view(self.require("users", item_id))

    def save_connection(self, data, item_id=None, user=None):
        old = (self.require_resource("connections", item_id, user, edit=True) if user else self.require("connections", item_id)) if item_id else None
        scope, owner_id = choose_scope(data, old, user)
        if old:
            self.validate_resource_scope_change("connections", item_id, scope, owner_id)
        merged = dict(old or {"name": "", "url": "", "auth_type": "none", "enabled": True, "secret": None})
        for key in ("name", "url", "auth_type", "enabled"):
            if data.get(key) is not None:
                merged[key] = data[key]
        validate_mcp_url(merged["url"], self.settings.mcp_hosts)
        if data.get("clear_credential"):
            merged["secret"] = None
        elif data.get("credential") is not None:
            merged["secret"] = self.secrets.encrypt(data["credential"]) if data["credential"] else None
        if merged["auth_type"] == "none":
            merged["secret"] = None
        if merged["auth_type"] == "bearer" and not merged["secret"]:
            raise AppError(422, "CREDENTIAL_REQUIRED", "Bearer 认证需要凭据")
        metadata = data.get("source_metadata") if data.get("source_metadata") is not None else json.loads((old or {}).get("source_metadata") or "{}")
        if any(not isinstance(k, str) or len(k) > 100 or not isinstance(v, str) or len(v) > 4000 for k, v in metadata.items()):
            raise AppError(422, "INVALID_SOURCE_METADATA", "数据源说明的名称或内容过长")
        if old:
            connection_changed = any(merged[k] != old[k] for k in ("url", "auth_type", "secret"))
            self.db.execute("UPDATE connections SET name=?,url=?,auth_type=?,secret=?,enabled=?,scope=?,owner_id=?,source_metadata=?,revision=revision+1 WHERE id=?", (merged["name"], merged["url"], merged["auth_type"], merged["secret"], merged["enabled"], scope, owner_id, dump(metadata), item_id))
            if connection_changed:
                self.db.execute("UPDATE connections SET status='untested',tools='[]',last_error=NULL WHERE id=?", (item_id,))
        else:
            item_id = uid()
            self.db.execute("INSERT INTO connections(id,name,url,auth_type,secret,enabled,status,scope,owner_id,source_metadata) VALUES(?,?,?,?,?,?,'untested',?,?,?)", (item_id, merged["name"], merged["url"], merged["auth_type"], merged["secret"], merged["enabled"], scope, owner_id, dump(metadata)))
        return connection_view(self.require("connections", item_id), user)

    async def discover(self, row):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        validate_mcp_url(row["url"], self.settings.mcp_hosts)
        headers = {}
        credential = self.secrets.decrypt(row["secret"])
        if credential:
            headers["Authorization"] = f"Bearer {credential}"

        async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(20, connect=10), follow_redirects=False, trust_env=False) as http:
            async with streamable_http_client(row["url"], http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    tools, cursor = [], None
                    for _ in range(20):
                        result = await session.list_tools(cursor=cursor)
                        tools.extend({"name": tool.name, "description": (tool.description or "")[:4000], "inputSchema": tool.inputSchema} for tool in result.tools)
                        cursor = result.nextCursor
                        if not cursor:
                            break
                    if cursor or len(tools) > 1000:
                        raise ValueError("Too many tools")
                    return tools

    async def test_connection(self, item_id, user=None):
        row = self.require_resource("connections", item_id, user, edit=True) if user else self.require("connections", item_id)
        if not row["enabled"]:
            raise AppError(409, "CONNECTION_DISABLED", "请先启用此连接")
        status, error, tools = "connected", None, []
        try:
            tools = await asyncio.wait_for(self.discover(row), timeout=35)
        except Exception as exc:
            # Never persist remote exception strings: URLs/headers can include secrets.
            def unauthorized(error):
                if isinstance(error, httpx.HTTPStatusError):
                    return error.response.status_code in (401, 403)
                return any(unauthorized(child) for child in getattr(error, "exceptions", []))
            status = "needs_auth" if unauthorized(exc) else "failed"
            error = "认证失败，请检查凭据和业务权限" if status == "needs_auth" else "连接失败，请检查执行服务器到 MCP 的网络、地址及协议"
        self.db.execute("UPDATE connections SET status=?,tools=?,last_error=?,last_tested_at=? WHERE id=? AND revision=?", (status, dump(tools), error, now(), item_id, row["revision"]))
        return connection_view(self.require("connections", item_id), user)

    def save_assistant(self, data, item_id=None, user=None):
        old_row = (self.require_resource("assistants", item_id, user, edit=True) if user else self.require("assistants", item_id)) if item_id else None
        if old_row and data.get("expected_revision") is not None and data["expected_revision"] != old_row["revision"]:
            raise AppError(409, "ASSISTANT_CONFLICT", "助手配置已更新，请重新加载后再保存")
        scope, owner_id = choose_scope(data, old_row, user)
        defaults = [row["id"] for row in self.db.all("SELECT * FROM knowledge_bases WHERE deleted=0 AND enabled=1 AND default_for_new_assistants=1 AND scope='team'")] if not item_id and self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_bases'") else []
        old = assistant_view(old_row) if old_row else {"name": "", "description": "", "instructions": "", "enabled": True, "access": {"all_members": True, "user_ids": []}, "connections": [], "knowledge_base_ids": defaults, "skill_ids": [], "web_search_enabled": False, "default_model_id": None, "default_reasoning_level": None}
        merged = {**old, **{k: v for k, v in data.items() if v is not None}}
        for key in ("default_model_id", "default_reasoning_level"):
            if key in data:
                merged[key] = data[key] or None
        if merged["default_model_id"] or merged["default_reasoning_level"]:
            self.catalog.select(merged["default_model_id"], merged["default_reasoning_level"])
        def bound(table, resource_id):
            row = self.require_resource(table, resource_id, user) if user else self.require(table, resource_id)
            if scope == "team" and row.get("scope", "team") != "team":
                raise AppError(422, "PRIVATE_DEPENDENCY", "团队助手只能绑定团队共享能力，请先创建共享配置")
            return row
        seen = set()
        for binding in merged["connections"]:
            row = bound("connections", binding["connection_id"])
            if row["id"] in seen:
                raise AppError(422, "DUPLICATE_CONNECTION", "同一助手不能重复绑定连接")
            seen.add(row["id"])
            discovered = {tool["name"] for tool in json.loads(row["tools"])}
            allowed, automatic = set(binding["allowed_tools"]), set(binding["auto_approved_tools"])
            if not automatic <= allowed or not allowed <= discovered:
                raise AppError(422, "INVALID_TOOL_SELECTION", "请先测试连接，再从工具清单选择允许及自动批准的工具")
        for user_id in merged["access"]["user_ids"]:
            self.require("users", user_id)
        for field, table in (("knowledge_base_ids", "knowledge_bases"), ("skill_ids", "skills")):
            merged[field] = list(dict.fromkeys(merged[field]))
            for resource_id in merged[field]:
                bound(table, resource_id)
        values = (merged["name"], merged["description"], merged["instructions"], merged["enabled"], dump(merged["access"]), dump(merged["connections"]), scope, owner_id, dump(merged["knowledge_base_ids"]), dump(merged["skill_ids"]), merged["web_search_enabled"], merged["default_model_id"], merged["default_reasoning_level"])
        if item_id:
            updated = self.db.execute("UPDATE assistants SET name=?,description=?,instructions=?,enabled=?,access=?,connections=?,scope=?,owner_id=?,knowledge_base_ids=?,skill_ids=?,web_search_enabled=?,default_model_id=?,default_reasoning_level=?,revision=revision+1 WHERE id=? AND revision=? AND deleted=0", (*values, item_id, old_row["revision"]))
            if updated.rowcount != 1:
                raise AppError(409, "ASSISTANT_CONFLICT", "助手配置已更新，请重新加载后再保存")
        else:
            item_id = uid()
            self.db.execute("INSERT INTO assistants(id,name,description,instructions,enabled,access,connections,scope,owner_id,knowledge_base_ids,skill_ids,web_search_enabled,default_model_id,default_reasoning_level) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item_id, *values))
        # Return exactly the authorized version written by this request. A
        # global reread could expose a subsequent editor's now-private version.
        written = dict(zip(("name", "description", "instructions", "enabled", "access", "connections", "scope", "owner_id", "knowledge_base_ids", "skill_ids", "web_search_enabled", "default_model_id", "default_reasoning_level"), values))
        return assistant_view({**written, "id": item_id, "revision": old_row["revision"] + 1 if old_row else 1}, user)

    def run_view(self, row):
        result = {key: row[key] for key in ("id", "conversation_id", "status", "assistant_message_id", "error", "created_at", "completed_at")}
        result["usage"] = json.loads(row["usage"]) if row["usage"] else None
        result["kind"] = row.get("kind", "chat")
        result["compaction"] = self.public_context_data(json.loads(row["compaction"]), self.public_run_model(row)["model"]) if row.get("compaction") else None
        result["approvals"] = [approval_view(item) for item in self.db.all("SELECT * FROM approvals WHERE run_id=? AND status='pending'", (row["id"],))]
        result.update(self.public_run_model(row))
        result["timings"] = json.loads(row.get("timings") or "{}")
        metadata = json.loads(row.get("execution_meta") or "{}")
        product_metadata = metadata.get("execution_kind") == "product_metadata"
        result["execution_kind"] = "product_metadata" if product_metadata else "model"
        result["identity_source"] = "run_selection" if product_metadata else None
        result["model_call_count"] = 0 if product_metadata else None
        return result

    def create_conversation(self, data, user):
        with self.db.transaction():
            return self._create_conversation(data, user)

    def _create_conversation(self, data, user):
        assistant = self.usable_assistant(data["assistant_id"], user)
        model_id = data.get("model_id") or assistant.get("default_model_id")
        level = data.get("reasoning_level") or (assistant.get("default_reasoning_level") if not data.get("model_id") or data.get("model_id") == assistant.get("default_model_id") else None)
        selection = self.pin_provider(self.catalog.select(model_id, level))
        item_id, timestamp = uid(), now()
        # Keep API clients that predate task-submit routing deterministic; the
        # current client opts in explicitly on the create request.
        auto_route = bool(data.get("auto_model_routing"))
        self.db.execute("INSERT INTO conversations(id,user_id,assistant_id,title,created_at,updated_at,model_id,model_label,reasoning_level,model_snapshot,auto_model_routing) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (item_id, user["id"], data["assistant_id"], data["title"], timestamp, timestamp, selection["model_id"], selection["model_label"], selection["reasoning_level"], dump(selection), int(auto_route)))
        self.memory.initialize_empty(item_id)
        self.db.execute("UPDATE conversations SET category=?,tags=? WHERE id=?", (data.get("category", ""), dump(list(dict.fromkeys(data.get("tags", [])))), item_id))
        return conversation_view(self.require("conversations", item_id, user))

    def list_conversations(self, user, q=None, assistant_id=None, archived=None, category=None):
        clauses, values = ["c.user_id=?"], [user["id"]]
        for key, value in (("assistant_id", assistant_id), ("archived", archived), ("category", category)):
            if value is not None:
                clauses.append(f"c.{key}=?")
                values.append(value)
        if q and q.strip():
            query = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            clauses.append("(c.title LIKE ? ESCAPE '\\' OR c.category LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM json_each(c.tags) tag WHERE tag.value LIKE ? ESCAPE '\\') OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.text LIKE ? ESCAPE '\\'))")
            values.extend([query, query, query, query])
        return [conversation_view(row) for row in self.db.all("SELECT c.* FROM conversations c WHERE " + " AND ".join(clauses) + " ORDER BY c.updated_at DESC LIMIT 1000", values)]

    def patch_conversation(self, item_id, data, user):
        with self.db.transaction():
            row = self.require("conversations", item_id, user)
            if row["active_run_id"] and data.get("archived"):
                raise AppError(409, "CONVERSATION_BUSY", "请先停止当前任务再归档")
            if data.get("model_id") is not None or data.get("reasoning_level") is not None:
                if row["active_run_id"]:
                    raise AppError(409, "CONVERSATION_BUSY", "请先等待或停止当前任务再切换模型")
                if self.manager and item_id in self.manager.context_requests:
                    raise AppError(409, "CONTEXT_BUSY", "正在读取上下文，请稍后切换模型")
                model_id = data.get("model_id") or row["model_id"]
                level = data.get("reasoning_level")
                if not data.get("model_id"):
                    level = level or row["reasoning_level"]
                selection = self.catalog.select(model_id, level)
                if row["sdk_session_id"] and model_id != row["model_id"]:
                    previous = json.loads(row["model_snapshot"])
                    if self.catalog.requires_new_conversation(previous) or self.catalog.requires_new_conversation(selection):
                        raise AppError(409, "MODEL_NEW_CONVERSATION_REQUIRED", "请新建对话使用此模型；原对话和记忆保留")
                self._save_model_selection(item_id, selection)
            for key in ("title", "archived", "category"):
                if data.get(key) is not None:
                    self.db.execute(f"UPDATE conversations SET {key}=? WHERE id=?", (data[key], item_id))
            if data.get("tags") is not None:
                self.db.execute("UPDATE conversations SET tags=? WHERE id=?", (dump(list(dict.fromkeys(data["tags"]))), item_id))
            if data.get("auto_model_routing") is not None:
                self.db.execute("UPDATE conversations SET auto_model_routing=? WHERE id=?", (int(data["auto_model_routing"]), item_id))
            elif data.get("model_id") is not None or data.get("reasoning_level") is not None:
                # Choosing a model in the selector is the durable manual
                # override. It can be re-enabled with the explicit flag.
                self.db.execute("UPDATE conversations SET auto_model_routing=0 WHERE id=?", (item_id,))
            self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now(), item_id))
            return conversation_view(self.require("conversations", item_id, user))

    def prepare_run(self, conversation_id, data, user):
        with self.db.transaction():
            conversation = self.require("conversations", conversation_id, user)
            prior = self.db.one("SELECT * FROM runs WHERE conversation_id=? AND client_request_id=?", (conversation_id, data["client_request_id"]))
            if prior:
                return {"message": message_view(self.require("messages", prior["message_id"])), "run_id": prior["id"]}, False
            assistant = self.usable_assistant(conversation["assistant_id"], user)
            if conversation["archived"]:
                raise AppError(409, "CONVERSATION_ARCHIVED", "请先恢复此对话")
            if conversation["active_run_id"]:
                raise AppError(409, "CONVERSATION_BUSY", "当前任务仍在执行，请等待或停止后重试")
            if self.manager and conversation_id in self.manager.context_requests:
                raise AppError(409, "CONTEXT_BUSY", "正在读取原生上下文，请稍后重试")
            selection = self.catalog.validate_snapshot(json.loads(conversation["model_snapshot"]))
            route_decision = {"enabled": False, "applied": False, "mode": "manual", "reason": None}
            requested_model = data.get("model_id")
            route_enabled = data.get("auto_route") if data.get("auto_route") is not None else bool(conversation.get("auto_model_routing"))
            if requested_model:
                # A per-request model is a bounded, explicit user override.
                selection = self.catalog.select(requested_model, data.get("reasoning_level"))
                if conversation["sdk_session_id"] and selection["model_id"] != conversation["model_id"]:
                    previous = json.loads(conversation["model_snapshot"])
                    if self.catalog.requires_new_conversation(previous) or self.catalog.requires_new_conversation(selection):
                        raise AppError(409, "MODEL_NEW_CONVERSATION_REQUIRED", "请新建对话使用此模型；原对话和记忆保留")
                selection = self.pin_provider(selection)
                route_decision = {"enabled": False, "applied": selection["model_id"] != conversation["model_id"], "mode": "manual", "reason": "用户明确指定模型"}
            elif route_enabled:
                route_decision = {"enabled": True, "applied": False, "mode": "bounded_local", "reason": None}
                try:
                    from .model_recommendation import bounded_router
                    candidates = [item["id"] for item in self.catalog.public() if item["available"]]
                    recommendation = bounded_router(self.catalog, data.get("text", ""), candidates)
                    routed = self.catalog.select(recommendation["model_id"], recommendation["reasoning_level"])
                    previous = selection
                    if routed["model_id"] != previous["model_id"] and conversation["sdk_session_id"] and (self.catalog.requires_new_conversation(previous) or self.catalog.requires_new_conversation(routed)):
                        raise AppError(409, "MODEL_NEW_CONVERSATION_REQUIRED", "当前会话不能自动切换模型")
                    selection = self.pin_provider(routed)
                    self._save_model_selection(conversation_id, selection)
                    route_decision.update({"applied": selection["model_id"] != previous["model_id"], "model_id": selection["model_id"], "reason": recommendation["reason"]})
                except (AppError, ValueError, TypeError) as error:
                    # Routing is advisory: a bad summary, unavailable model or
                    # provider mismatch falls back to the pinned selection.
                    route_decision.update({"mode": "fallback", "reason": getattr(error, "message", "路由失败，沿用当前模型")})
            selection = self.catalog.validate_snapshot(selection)
            provider_env = self.validated_provider(selection)
            self.memory.refresh(user["id"], conversation_id)
            if not data["text"].strip() and not data["file_ids"] and not data.get("skill_ids"):
                raise AppError(422, "EMPTY_MESSAGE", "请输入消息、选择 Skill 或添加附件")
            for file_id in data["file_ids"]:
                self.require("files", file_id, user)
            from .skills import selected_skills
            skills = selected_skills(self, assistant, user, data.get("skill_ids", []))
            snapshot = assistant_view(assistant, user)
            snapshot["connection_revisions"] = {binding["connection_id"]: self.require_resource("connections", binding["connection_id"], user)["revision"] for binding in snapshot["connections"]}
            message_id, assistant_message_id, run_id, timestamp = uid(), uid(), uid(), now()
            self.db.execute("INSERT INTO messages(id,conversation_id,role,text,file_ids,run_id,created_at,skills) VALUES(?,?,?,?,?,?,?,?)", (message_id, conversation_id, "user", data["text"], dump(data["file_ids"]), run_id, timestamp, dump([{key: skill[key] for key in ("id", "name", "revision")} for skill in skills])))
            self.db.execute("INSERT INTO messages(id,conversation_id,role,text,file_ids,run_id,created_at) VALUES(?,?,?,?,?,?,?)", (assistant_message_id, conversation_id, "assistant", "", "[]", run_id, timestamp))
            self.db.execute("INSERT INTO runs(id,conversation_id,user_id,client_request_id,message_id,assistant_message_id,status,created_at) VALUES(?,?,?,?,?,?,'queued',?)", (run_id, conversation_id, user["id"], data["client_request_id"], message_id, assistant_message_id, timestamp))
            self.db.execute("UPDATE runs SET capability_snapshot=?,skill_snapshots=? WHERE id=?", (dump(snapshot), dump(skills), run_id))
            self.knowledge.snapshot_for_run(run_id, snapshot, user)
            from urllib.parse import urlsplit
            try:
                provider = urlsplit(provider_env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")).hostname
            except Exception:
                provider = None
            self.db.execute("UPDATE runs SET execution_meta=? WHERE id=?", (dump({"model": selection["upstream_model"], "effort": selection["upstream_effort"], "provider": provider, "selection": selection, "model_routing": route_decision}), run_id))
            title = data["text"].strip()[:40] if conversation["title"] == "新对话" and data["text"].strip() else conversation["title"]
            if title == "新对话" and skills:
                title = " / ".join(skill["name"] for skill in skills)[:40]
            self.db.execute("UPDATE conversations SET active_run_id=?,updated_at=?,title=? WHERE id=?", (run_id, timestamp, title, conversation_id))
            return {"message": message_view(self.require("messages", message_id)), "run_id": run_id}, True

    def authorize_tool(self, conversation_id: str, tool_name: str, arguments: dict):
        conversation = self.require("conversations", conversation_id)
        user = self.require("users", conversation["user_id"])
        if not user["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        assistant = self.usable_assistant(conversation["assistant_id"], user)
        snapshot = None
        if conversation["active_run_id"]:
            active = self.require("runs", conversation["active_run_id"])
            snapshot = json.loads(active.get("capability_snapshot") or "null")
        root = self.settings.data_dir / "workspaces" / conversation_id
        if tool_name in {"Read", "Write", "Edit", "Glob", "Grep"}:
            raw = arguments.get("file_path", arguments.get("path", str(root)))
            if not isinstance(raw, str):
                raise AppError(403, "PATH_DENIED", "文件路径无效")
            try:
                workspace_path(root, raw)
            except AppError:
                memory = self.memory.root(user["id"], conversation_id)
                path = self.memory.path(memory, raw, allow_directory=tool_name in {"Glob", "Grep"})
                self.memory.validate_write(memory, path, tool_name, arguments)
            if tool_name == "Glob" and (".." in str(arguments.get("pattern", "")).replace("\\", "/").split("/") or Path(str(arguments.get("pattern", ""))).is_absolute()):
                raise AppError(403, "PATH_DENIED", "文件查找范围超出会话")
            # Only read-only workspace operations are safe to auto-approve.
            # Write/Edit still pass path and memory guards, then require the
            # active approval strategy in the SDK permission callback.
            return tool_name in {"Read", "Glob", "Grep"}
        if tool_name in {"mcp__workspace__knowledge_search", "mcp__workspace__knowledge_read"}:
            ids = json.loads(assistant.get("knowledge_base_ids") or "[]")
            if snapshot:
                ids = [item for item in ids if item in snapshot.get("knowledge_base_ids", [])]
            authorized = []
            for item_id in ids:
                row = self.db.one("SELECT * FROM knowledge_bases WHERE id=?", (item_id,))
                if self.resource_access(row, user) and row["enabled"]:
                    authorized.append(item_id)
            if authorized:
                return True
        if tool_name in {"mcp__workspace__web_search", "mcp__workspace__web_read"} and assistant.get("web_search_enabled") and (not snapshot or snapshot.get("web_search_enabled")):
            return True
        for binding in json.loads(assistant["connections"]):
            connection = self.require_resource("connections", binding["connection_id"], user)
            prefix = f"mcp__c{connection['id']}__"
            if tool_name.startswith(prefix):
                name = tool_name[len(prefix):]
                if not connection["enabled"] or name not in binding["allowed_tools"]:
                    break
                if snapshot:
                    prior_binding = next((item for item in snapshot["connections"] if item["connection_id"] == connection["id"]), None)
                    if not prior_binding or name not in prior_binding["allowed_tools"]:
                        break
                validate_mcp_url(connection["url"], self.settings.mcp_hosts)
                return name in binding["auto_approved_tools"] and (not snapshot or name in prior_binding["auto_approved_tools"])
        raise AppError(403, "TOOL_DENIED", "此工具未获授权或连接已经停用")

    def runtime_config(self, conversation_id):
        conversation = self.require("conversations", conversation_id)
        selection = None
        run = None
        if conversation["active_run_id"]:
            run = self.require("runs", conversation["active_run_id"])
            metadata = json.loads(run["execution_meta"] or "{}")
            selection = metadata.get("selection")
        selection = self.catalog.validate_snapshot(selection or json.loads(conversation["model_snapshot"]))
        provider_env = self.validated_provider(selection)
        user = self.require("users", conversation["user_id"])
        if not user["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        assistant = self.usable_assistant(conversation["assistant_id"], user)
        assistant_snapshot = json.loads(run.get("capability_snapshot") or "null") if run else None
        assistant_snapshot = assistant_snapshot or assistant_view(assistant, user)
        skill_snapshots = json.loads(run.get("skill_snapshots") or "[]") if run else []
        servers, versions = {}, [assistant["revision"]]
        for binding in assistant_snapshot["connections"]:
            connection = self.require_resource("connections", binding["connection_id"], user)
            pinned_revision = assistant_snapshot.get("connection_revisions", {}).get(connection["id"])
            if pinned_revision is not None and pinned_revision != connection["revision"]:
                raise AppError(409, "CONFIG_CHANGED", "任务提交后数据源配置已改变，请重新发送任务")
            if not connection["enabled"]:
                continue
            validate_mcp_url(connection["url"], self.settings.mcp_hosts)
            config = {"type": "http", "url": connection["url"]}
            credential = self.secrets.decrypt(connection["secret"])
            if credential:
                config["headers"] = {"Authorization": f"Bearer {credential}"}
            servers[f"c{connection['id']}"] = config
            versions.append((connection["id"], connection["revision"], connection["scope"], connection["owner_id"]))
        for item_id in assistant_snapshot["knowledge_base_ids"]:
            kb = self.require_resource("knowledge_bases", item_id, user)
            versions.append(("knowledge", item_id, kb.get("revision"), kb["enabled"], kb["scope"], kb["owner_id"]))
        for skill in skill_snapshots:
            current = self.require_resource("skills", skill["id"], user)
            if not current["enabled"] or skill["id"] not in json.loads(assistant["skill_ids"]):
                raise AppError(403, "SKILL_UNAVAILABLE", "所选 Skill 已停用或不再配置给此助手，请重新选择")
            versions.append(("skill", skill["id"], skill["revision"], current["enabled"], current["scope"], current["owner_id"]))
        memory = self.memory.root(user["id"], conversation_id)
        versions.append(("memory", conversation_id, self.memory.revision(user["id"], conversation_id)["revision"]))
        versions.append(("model", selection))
        return {"conversation": conversation, "instructions": assistant_snapshot["instructions"], "mcp_servers": servers, "fingerprint": dump(versions), "memory_dir": str(memory), "assistant_snapshot": assistant_snapshot, "skill_snapshots": skill_snapshots, "user": user_view(user),
                "selection": selection, "public_model_id": selection["model_id"], "public_model_label": selection["model_label"],
                "reasoning_level": selection["reasoning_level"], "upstream_model": selection["upstream_model"], "upstream_effort": selection["upstream_effort"],
                "provider_env": provider_env}

    def collect_artifacts(self, conversation_id, run_id):
        conversation = self.require("conversations", conversation_id)
        root = self.settings.data_dir / "workspaces" / conversation_id
        directory = root / "artifacts"
        if not directory.exists():
            return
        # Copy immutable snapshots; never expose arbitrary paths or symlink targets.
        for path in list(directory.rglob("*"))[:100]:
            if not path.is_file() or path.is_symlink():
                continue
            try:
                workspace_path(root, str(path))
                size = path.stat().st_size
                if size > self.settings.upload_max_bytes:
                    continue
                import hashlib
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                artifact_name = path.name
                prior = self.db.one("SELECT id FROM files WHERE conversation_id=? AND name=? AND path LIKE ?", (conversation_id, artifact_name, f"%{digest}%"))
                if prior:
                    continue
                file_id = uid()
                stored = self.settings.data_dir / "files" / f"{file_id}-{digest}"
                shutil.copyfile(path, stored)
                self.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", (file_id, conversation["user_id"], artifact_name, mimetypes.guess_type(artifact_name)[0] or "application/octet-stream", size, str(stored), now(), conversation_id))
                value = file_view(self.require("files", file_id))
                self.db.event(run_id, "artifact.created", value)
                row = self.require("runs", run_id)
                message = self.require("messages", row["assistant_message_id"])
                ids = json.loads(message["file_ids"])
                ids.append(file_id)
                self.db.execute("UPDATE messages SET file_ids=? WHERE id=?", (dump(ids), message["id"]))
            except (OSError, AppError):
                continue

    def usage(self, user):
        rows = self.db.all("SELECT runs.*,users.display_name FROM runs JOIN users ON users.id=runs.user_id" + ("" if user["role"] == "admin" else " WHERE runs.user_id=?"), () if user["role"] == "admin" else (user["id"],))
        def aggregate(items):
            values = [json.loads(row["usage"]) for row in items if row["usage"]]
            costs = [value.get("estimated_cost_usd") for value in values]
            return {"total_runs": len(items), "successful_runs": sum(row["status"] == "succeeded" for row in items), "input_tokens": sum(value.get("input_tokens", 0) for value in values), "output_tokens": sum(value.get("output_tokens", 0) for value in values), "estimated_cost_usd": sum(costs) if costs and all(value is not None for value in costs) else None, "currency": "USD"}
        result = aggregate(rows)
        result["users"] = [{"user_id": user_id, "display_name": next(row["display_name"] for row in rows if row["user_id"] == user_id), **aggregate([row for row in rows if row["user_id"] == user_id])} for user_id in sorted({row["user_id"] for row in rows})] if user["role"] == "admin" else []
        return result
