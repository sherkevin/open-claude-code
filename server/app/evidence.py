"""Evidence recorded by trusted tool adapters, never by a model-facing HTTP write API."""
from __future__ import annotations

import hashlib
import html
import json
import posixpath
import re
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends

from .db import dump, now, uid
from .security import AppError

KINDS = {"data", "knowledge", "web"}
MARKER = re.compile(r"\[\[source:([A-Za-z0-9_-]{1,100})\]\]")
FIELDS = {
    "data": {"data_source", "database", "schema", "table", "tables", "fields", "record_keys", "filters", "time_range", "timezone", "queried_at", "aggregation", "trace_id", "dataset", "dataset_purpose", "dataset_version", "missing_fields", "row_count", "query", "tool"},
    # ``capability``/``provider`` let OCR sources be reauthorized against the
    # current capability grant instead of inheriting an old run's authority.
    "knowledge": {"knowledge_base", "knowledge_base_id", "file", "version", "sha256", "page", "pages", "section", "paragraph", "chunk_id", "imported_at", "extraction", "start", "end", "capability", "provider", "complete", "truncated", "character_start", "character_end"},
    "web": {"url", "title", "fetched_at", "published_at", "updated_at", "search_query", "provider", "content_type", "character_start", "character_end", "truncated", "capability", "image_url", "thumbnail_url", "width", "height", "mode"},
}
SENSITIVE = re.compile(r"password|secret|token|authorization|api.?key|credential|internal_path|file_path", re.I)


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password:
            # Signed/credential query strings are not public evidence links.
            if not any(SENSITIVE.search(key) for key in (pair.split("=", 1)[0] for pair in parts.query.split("&"))):
                return value
    except ValueError:
        pass
    return None


def safe_public_link(value):
    """Public evidence link plus the shared private-address/DNS policy check."""
    try:
        from .web_tools import public_url as public_http_url
        public_http_url(value)
    except (AppError, ValueError, TypeError):
        return None
    return public_url(value)


def internal_source_link(value):
    """Match browser/Markdown URL normalization before allowing an anchor."""
    value = value.strip().strip("<>")
    for _ in range(3):
        decoded = unquote(html.unescape(value))
        if decoded == value:
            break
        value = decoded
    value = re.sub(r"[\x00-\x20]", "", value).replace("\\", "/")
    try:
        path = posixpath.normpath(urlsplit(value).path)
        return bool(re.search(r"(?:^|/)api/v1/sources(?:/|$)", path, re.I))
    except ValueError:
        return False


def mcp_payloads(result):
    """Normalize the actual SDK result envelopes without scanning arbitrary fields.

    HTTP MCP uses content blocks; the native SDK may flatten those blocks to
    ``{"content": "{...JSON...}"}``. Both are tool data, never instructions.
    Depth/node/decoded-text limits also apply to nested JSON wrapper strings.
    """
    pending, payloads = [(result, 0)], []
    budget, visited = 1_000_000, 0
    while pending and visited < 128:
        item, depth = pending.pop(0)
        visited += 1
        if depth > 6:
            continue
        if isinstance(item, str):
            if len(item) > min(512_000, budget):
                continue
            budget -= len(item)
            try:
                parsed = json.loads(item)
            except (ValueError, TypeError, RecursionError):
                continue
            if isinstance(parsed, (dict, list)):
                pending.append((parsed, depth + 1))
        elif isinstance(item, list):
            pending.extend((value, depth + 1) for value in item[:40])
        elif isinstance(item, dict):
            if item.get("isError") or item.get("is_error"):
                continue
            if isinstance(item.get("sources"), list):
                payloads.append(item)
            for key in ("structuredContent", "content"):
                if key in item:
                    pending.append((item[key], depth + 1))
            if item.get("type") == "text" and "text" in item:
                pending.append((item["text"], depth + 1))
    return payloads


def _safe_value(value, depth=0):
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {str(key)[:100]: _safe_value(item, depth + 1) for key, item in list(value.items())[:100] if not SENSITIVE.search(str(key))}
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return value[:12000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:200]


class EvidenceService:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.db.execute("""CREATE TABLE IF NOT EXISTS evidence_sources(
          id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),kind TEXT NOT NULL,title TEXT NOT NULL,
          locator TEXT NOT NULL,excerpt TEXT NOT NULL,resource_type TEXT,resource_id TEXT,
          original_url TEXT,document_id TEXT,document_version INTEGER,created_at TEXT NOT NULL,digest TEXT NOT NULL,
          UNIQUE(run_id,digest))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS evidence_run ON evidence_sources(run_id)")
        if "tool_name" not in {row["name"] for row in self.db.all("PRAGMA table_info(evidence_sources)")}:
            self.db.execute("ALTER TABLE evidence_sources ADD COLUMN tool_name TEXT")

    def _run(self, run_id, user=None):
        return self.service.require("runs", run_id, user)

    def _authorize(self, row, user):
        # Runtime callbacks may outlive the request that authenticated a user.
        # Refresh identity as well as assistant/resource grants at every read.
        user = self.service.require("users", user["id"])
        if not user["enabled"]:
            raise AppError(404, "SOURCE_UNAVAILABLE", "该账号已无权访问来源")
        run = self._run(row["run_id"], user)
        conversation = self.service.require("conversations", run["conversation_id"], user)
        assistant = self.service.usable_assistant(conversation["assistant_id"], user)
        locator = json.loads(row["locator"]) if isinstance(row["locator"], str) else row["locator"] or {}
        capability = locator.get("capability") if isinstance(locator, dict) else None
        resource_type, resource_id = row.get("resource_type"), row.get("resource_id")
        if resource_type == "knowledge_base":
            kb = self.service.knowledge.require(resource_id, user)
            ids = assistant.get("knowledge_base_ids", "[]")
            ids = json.loads(ids) if isinstance(ids, str) else ids
            if not kb["enabled"] or resource_id not in ids:
                raise AppError(404, "SOURCE_UNAVAILABLE", "该来源已不在助手授权范围内")
            version = self.service.knowledge.version(row.get("document_id"), row.get("document_version"), user)
            if version["kb_id"] != resource_id:
                raise AppError(404, "SOURCE_UNAVAILABLE", "知识文件已不可访问")
        elif resource_type == "connection":
            connection = self.service.require_resource("connections", resource_id, user)
            bindings = assistant["connections"]
            bindings = json.loads(bindings) if isinstance(bindings, str) else bindings
            if not connection["enabled"] or resource_id not in {item["connection_id"] for item in bindings}:
                raise AppError(404, "SOURCE_UNAVAILABLE", "该数据源已不在助手授权范围内")
            bound = next(item for item in bindings if item["connection_id"] == resource_id)
            if not row.get("tool_name") or row["tool_name"] not in bound["allowed_tools"]:
                raise AppError(404, "SOURCE_UNAVAILABLE", "该取数工具已不在助手授权范围内")
        elif resource_type == "file" and row["kind"] == "knowledge":
            file = self.service.require("files", resource_id, user)
            in_conversation = file.get("conversation_id") == conversation["id"] or any(resource_id in json.loads(message["file_ids"]) for message in self.db.all("SELECT file_ids FROM messages WHERE conversation_id=?", (conversation["id"],)))
            if not in_conversation:
                raise AppError(404, "SOURCE_UNAVAILABLE", "附件不属于此会话")
        elif row["kind"] != "web":
            raise AppError(404, "SOURCE_UNAVAILABLE", "来源缺少可核对的授权资源")
        elif capability is None and not assistant.get("web_search_enabled"):
            # Tagged sources use the granular policy below. Only untagged
            # legacy web rows keep their historical boolean check.
            raise AppError(404, "SOURCE_UNAVAILABLE", "助手的网页访问能力已停用")
        if capability is not None:
            self._authorize_capability_source(row, user)
        return row

    def _authorize_capability_source(self, row, user):
        """Current capability/domain grant for a recorded source.

        The policy module is provided by the capability-governance owner.  When
        it is absent (this branch before integration) capability-tagged sources
        fail closed rather than bypassing revocation.
        """
        try:
            from .capability_policy import authorize_source_access
        except ImportError:
            raise AppError(404, "SOURCE_UNAVAILABLE", "该能力来源暂不可重新授权") from None
        authorize_source_access(self.service, row, user)

    def view(self, row):
        locator = json.loads(row["locator"]) if isinstance(row["locator"], str) else row["locator"]
        return {key: row.get(key) for key in ("id", "kind", "title", "excerpt", "original_url", "document_id", "document_version", "created_at")} | {
            "locator": locator, "url": f"/api/v1/sources/{row['id']}",
            "content_url": f"/api/v1/knowledge-documents/{row['document_id']}/content?version={row['document_version']}" if row.get("document_id") else f"/api/v1/files/{row['resource_id']}/content" if row.get("resource_type") == "file" else None,
        }

    def register(self, run_id, kind, title, locator, excerpt, resource_type=None, resource_id=None, original_url=None, document_id=None, document_version=None, tool_name=None):
        if kind not in KINDS or not isinstance(locator, dict):
            raise AppError(422, "INVALID_SOURCE", "来源类型或定位信息无效")
        run = self._run(run_id)
        user = self.service.require("users", run["user_id"])
        cleaned = {key: _safe_value(value) for key, value in locator.items() if key in FIELDS[kind]}
        if kind == "web" and cleaned.get("capability") == "image_search":
            # Both links come from the provider, but only a safe public pair may
            # reach the UI.  Never repair one link by guessing from the other.
            image_url = safe_public_link(cleaned.get("image_url"))
            if not image_url:
                raise AppError(422, "INVALID_SOURCE", "图片来源缺少安全的原始图片地址")
            cleaned["image_url"] = image_url
            if cleaned.get("thumbnail_url"):
                cleaned["thumbnail_url"] = safe_public_link(cleaned["thumbnail_url"])
            if cleaned.get("url") and not safe_public_link(cleaned["url"]):
                raise AppError(422, "INVALID_SOURCE", "图片来源缺少安全的原网页地址")
        if kind == "data":
            required = ("database", "table", "fields", "time_range", "timezone", "queried_at")
            cleaned["missing_fields"] = [key for key in required if not cleaned.get(key)]
        row = {"id": uid(), "run_id": run_id, "kind": kind, "title": str(title)[:500], "locator": dump(cleaned),
               "excerpt": str(excerpt)[:24000], "resource_type": resource_type, "resource_id": resource_id,
               "original_url": public_url(original_url), "document_id": document_id, "document_version": document_version, "created_at": now(), "tool_name": tool_name}
        if kind == "data":
            cleaned["tool"] = tool_name
            row["locator"] = dump(cleaned)
        self._authorize(row, user)
        if kind == "knowledge" and resource_type == "knowledge_base":
            version = self.service.knowledge.version(document_id, document_version, user)
            if version["kb_id"] != resource_id:
                raise AppError(422, "INVALID_SOURCE", "来源文件与知识库不一致")
        digest = hashlib.sha256(dump({key: value for key, value in row.items() if key not in {"id", "created_at"}}).encode()).hexdigest()
        row["digest"] = digest
        keys = ("id", "run_id", "kind", "title", "locator", "excerpt", "resource_type", "resource_id", "original_url", "document_id", "document_version", "created_at", "digest", "tool_name")
        self.db.execute("INSERT OR IGNORE INTO evidence_sources(" + ",".join(keys) + ") VALUES(" + ",".join("?" for _ in keys) + ")", tuple(row[key] for key in keys))
        return self.view(self.db.one("SELECT * FROM evidence_sources WHERE run_id=? AND digest=?", (run_id, digest)))

    def get(self, source_id, user):
        row = self.db.one("SELECT * FROM evidence_sources WHERE id=?", (source_id,))
        if not row:
            raise AppError(404, "NOT_FOUND", "来源不存在或无访问权限")
        return self.view(self._authorize(row, user))

    def list(self, run_id, user):
        self._run(run_id, user)
        result = []
        for row in self.db.all("SELECT * FROM evidence_sources WHERE run_id=? ORDER BY created_at,id", (run_id,)):
            try:
                result.append(self.view(self._authorize(row, user)))
            except AppError:
                continue
        return result

    def register_mcp(self, run_id, connection_id, result, tool_name=None):
        """Only structured provenance supplied by the actual successful MCP result."""
        prefix = f"mcp__c{connection_id}__"
        if isinstance(tool_name, str) and tool_name.startswith(prefix):
            tool_name = tool_name[len(prefix):]
        if not tool_name or str(tool_name).startswith("mcp__"):
            return []
        candidates = [source for payload in mcp_payloads(result) for source in payload["sources"][:30]][:30]
        sources, source_ids = [], set()
        for item in candidates[:30]:
            if not isinstance(item, dict) or item.get("kind", "data") != "data" or not isinstance(item.get("locator"), dict):
                continue
            source = self.register(run_id, "data", item.get("title", "业务数据"), item["locator"], item.get("excerpt", ""),
                                   resource_type="connection", resource_id=connection_id, original_url=item.get("original_url"), tool_name=tool_name)
            if source["id"] not in source_ids:
                sources.append(source)
                source_ids.add(source["id"])
        return sources

    def _source_for_finalization(self, run, source_id, user):
        row = self.db.one("SELECT * FROM evidence_sources WHERE id=?", (source_id,))
        if not row:
            return None
        origin = self._run(row["run_id"])
        # Same-conversation historical sources are explicitly reauthorized and copied;
        # unrelated run/conversation IDs can never be laundered into citations.
        if origin["conversation_id"] != run["conversation_id"] or origin["user_id"] != run["user_id"]:
            return None
        self._authorize(row, user)
        if row["run_id"] == run["id"]:
            return self.view(row)
        return self.register(run["id"], row["kind"], row["title"], json.loads(row["locator"]), row["excerpt"], row["resource_type"], row["resource_id"], row["original_url"], row["document_id"], row["document_version"], row.get("tool_name"))

    @staticmethod
    def _bounded_source_ids(value, *, limit=30):
        """Return only opaque source identifiers from a trusted tool envelope.

        Tool results are still untrusted data until each identifier is looked up
        against the current run below.  In particular, this never treats a
        citation marker or an arbitrary URL in model text as a source.
        """
        if not isinstance(value, list):
            return []
        found, seen = [], set()
        for item in value:
            if not isinstance(item, str) or item in seen or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item):
                continue
            seen.add(item)
            found.append(item)
            if limit is not None and len(found) >= limit:
                break
        return found

    def trusted_sources(self, run_id, source_ids, user, *, same_run=True):
        """Resolve source IDs emitted by a completed tool after ACL checks.

        ``source_ids`` comes from a runtime event or a real workspace tool
        result, never from assistant text.  Rechecking the run owner,
        conversation, assistant and current permissions here prevents a stale
        or cross-run ID from becoming a citation.
        """
        run = self._run(run_id, user)
        result, seen = [], set()
        # Individual tool events are bounded; do not cap their aggregated run
        # ledger before later web results have had a chance to be considered.
        for source_id in self._bounded_source_ids(source_ids, limit=None):
            if source_id in seen:
                continue
            row = self.db.one("SELECT * FROM evidence_sources WHERE id=?", (source_id,))
            if not row or (same_run and row["run_id"] != run["id"]):
                continue
            try:
                source = self._source_for_finalization(run, source_id, user)
            except AppError:
                continue
            if source and source["id"] not in seen:
                result.append(source)
                seen.add(source["id"])
        return result

    def used_sources(self, run_id, user):
        """Build the run-scoped source ledger from successful tool events."""
        self._run(run_id, user)
        after, sources, seen = 0, [], set()
        while True:
            rows = self.db.all("SELECT id,data FROM events WHERE run_id=? AND type='tool.completed' AND id>? ORDER BY id LIMIT 200", (run_id, after))
            if not rows:
                return sources
            after = rows[-1]["id"]
            ids = []
            for row in rows:
                data = json.loads(row["data"])
                if data.get("success") is not True:
                    continue
                ids.extend(source_id for source_id in self._bounded_source_ids(data.get("source_ids")) if source_id not in seen)
            for source in self.trusted_sources(run_id, ids, user):
                if source["id"] not in seen:
                    sources.append(source)
                    seen.add(source["id"])

    def source_ids_from_tool_result(self, result):
        """Extract bounded ``source_id`` fields from an MCP result envelope.

        The IDs are only hints.  ``trusted_sources`` performs the authoritative
        database and permission validation before they enter the run ledger.
        """
        pending, found, visited, budget = [(result, 0)], [], 0, 1_000_000
        while pending and visited < 128 and len(found) < 30:
            item, depth = pending.pop(0)
            visited += 1
            if depth > 6:
                continue
            if isinstance(item, str):
                if len(item) > min(512_000, budget):
                    continue
                budget -= len(item)
                try:
                    parsed = json.loads(item)
                except (ValueError, TypeError, RecursionError):
                    continue
                if isinstance(parsed, (dict, list)):
                    pending.append((parsed, depth + 1))
                continue
            if isinstance(item, list):
                pending.extend((value, depth + 1) for value in item[:40])
                continue
            if not isinstance(item, dict):
                continue
            if item.get("isError") or item.get("is_error"):
                continue
            value = item.get("source_id")
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value) and value not in found:
                found.append(value)
            # OCR returns page-scoped source IDs, not search-style results.
            for key in ("structuredContent", "content", "result", "results", "pages"):
                if key in item:
                    pending.append((item[key], depth + 1))
            if item.get("type") == "text" and "text" in item:
                pending.append((item["text"], depth + 1))
        return found

    def finalize(self, run_id, text, used_source_ids=None):
        run = self._run(run_id)
        user = self.service.require("users", run["user_id"])
        citations, numbers = [], {}
        # Only registry markers can mint internal source hyperlinks. Strip raw
        # links first, including query/fragment and absolute URL variants.
        text = re.sub(r"\[[^\]]*\]\(\s*([^)]*)\)", lambda match: "（来源未核验）" if internal_source_link(match.group(1).split()[0] if match.group(1).split() else "") else match.group(0), text)
        text = re.sub(r"(?m)^\s*\[[^\]]+\]:\s*(\S+).*$", lambda match: "" if internal_source_link(match.group(1)) else match.group(0), text)
        text = re.sub(r"(?:https?://|/?api/v1/)[^\s<>]+|<[^\s<>]+>", lambda match: "来源未核验" if internal_source_link(match.group(0)) else match.group(0), text, flags=re.I)
        def replace(match):
            try:
                source = self._source_for_finalization(run, match.group(1), user)
            except AppError:
                source = None
            if not source:
                return "（来源未核验）"
            if source["id"] not in numbers:
                numbers[source["id"]] = len(citations) + 1
                citations.append({**source, "number": numbers[source["id"]], "attribution": "cited"})
            number = numbers[source["id"]]
            return f"[{number}]({source['url']})"
        result = MARKER.sub(replace, text)
        # The model may omit citation markers even though a web tool completed
        # successfully.  Attach only sources recorded in this run's durable
        # tool ledger, after the same ACL checks used by explicit markers.
        if used_source_ids is None:
            used_sources = self.used_sources(run_id, user)
        else:
            used_sources = self.trusted_sources(run_id, used_source_ids, user)
        explicit_ids = {source["id"] for source in citations}
        web_sources = [source for source in used_sources
                       if source["kind"] == "web" and source["id"] not in explicit_ids]
        # Full page reads are stronger evidence than search snippets.  If no
        # page was read, retain a bounded set of search candidates instead.
        # Image candidates stay visible on their own budget so a busy text
        # search cannot push a successful image result out of the source bar.
        page_sources = [source for source in web_sources if source.get("locator", {}).get("content_type") == "page_text"]
        search_sources = [source for source in web_sources if source.get("locator", {}).get("content_type") == "search_excerpt"]
        image_sources = [source for source in web_sources if source.get("locator", {}).get("content_type") == "image_search_result"]
        if page_sources:
            auto_sources = page_sources[:5] + image_sources[:3]
        else:
            auto_sources = search_sources[:5] + image_sources[:5]
        for source in auto_sources:
            numbers[source["id"]] = len(citations) + 1
            attribution = {"page_text": "read", "search_excerpt": "search_candidate",
                           "image_search_result": "image_candidate"}[source["locator"].get("content_type")]
            citations.append({**source, "number": numbers[source["id"]], "attribution": attribution})
        # Models cannot create clickable internal source links without registration.
        permitted = {source["id"] for source in citations}
        result = re.sub(r"\[([^\]]*)\]\(/api/v1/sources/([A-Za-z0-9_-]+)\)",
                        lambda m: m.group(0) if m.group(2) in permitted else "（来源未核验）", result)
        if citations:
            labels = {"data": "数据", "knowledge": "知识", "web": "网页"}
            lines = []
            for source in citations:
                title = re.sub(r"[\[\]\n\r]", " ", source["title"])
                location = source["locator"]
                suffix = f" · 第{location['page']}页" if location.get("page") else ""
                if source.get("document_version"):
                    suffix += f" · v{source['document_version']}"
                kind_label = labels[source["kind"]]
                if source["kind"] == "web":
                    kind_label = {"cited": "回答引用的网页", "read": "网页正文已读取，未明确引用",
                                  "search_candidate": "外部检索候选，未核验正文",
                                  "image_candidate": "图片检索候选，未读取网页正文"}[source["attribution"]]
                lines.append(f"[{source['number']}] [{kind_label} · {title}{suffix}]({source['url']})")
            result = result.rstrip() + "\n\n来源\n\n" + "\n\n".join(lines)
        summaries = []
        for source in citations:
            summary = {key: source.get(key) for key in (
                "id", "number", "kind", "title", "url", "document_id", "document_version", "attribution")}
            # Cards need type/page/completeness after message persistence, but
            # external URLs and excerpts must still be fetched with fresh ACLs.
            display = {}
            if source["locator"].get("capability") in {"ocr", "image_search"}:
                display = {key: source["locator"][key] for key in (
                    "capability", "page", "provider", "extraction", "complete", "truncated")
                    if key in source["locator"]}
            if display:
                summary["locator"] = display
            summaries.append(summary)
        return {"text": result, "citations": summaries}


def create_evidence_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1")
    @router.get("/runs/{run_id}/sources")
    def sources(run_id: str, user=Depends(authenticated), service=Depends(svc)):
        return service.evidence.list(run_id, user)
    @router.get("/sources/{source_id}")
    def source(source_id: str, user=Depends(authenticated), service=Depends(svc)):
        return service.evidence.get(source_id, user)
    return router
