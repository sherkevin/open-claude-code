"""Versioned local knowledge, bounded document parsing and hybrid retrieval."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import os
import re
import threading
import zipfile
from collections import Counter
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import dump, now, uid
from .security import AppError, safe_name

MAX_PAGES = 200
MAX_TEXT = 2_000_000
MAX_CHUNKS = 5000
MODEL = "BAAI/bge-small-zh-v1.5"
GRAPH_STOPWORDS = {"设备", "系统", "数据", "文件", "内容", "说明", "检查", "需要", "可以", "以及", "进行", "其中", "这个", "当前"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_bases(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,
 owner_id TEXT NOT NULL,scope TEXT NOT NULL,enabled INTEGER NOT NULL,default_for_new_assistants INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_documents(id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
 name TEXT NOT NULL,current_version INTEGER NOT NULL,deleted INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_versions(document_id TEXT NOT NULL REFERENCES knowledge_documents(id),version INTEGER NOT NULL,
 mime_type TEXT NOT NULL,sha256 TEXT NOT NULL,size INTEGER NOT NULL,path TEXT NOT NULL,status TEXT NOT NULL,error TEXT,
 page_count INTEGER NOT NULL DEFAULT 0,chunk_count INTEGER NOT NULL DEFAULT 0,extraction TEXT NOT NULL DEFAULT '[]',
 embedding_model TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,PRIMARY KEY(document_id,version));
CREATE TABLE IF NOT EXISTS knowledge_pages(document_id TEXT NOT NULL,version INTEGER NOT NULL,page INTEGER NOT NULL,
 text TEXT NOT NULL,extraction TEXT NOT NULL,PRIMARY KEY(document_id,version,page));
CREATE TABLE IF NOT EXISTS knowledge_chunks(id TEXT PRIMARY KEY,document_id TEXT NOT NULL,version INTEGER NOT NULL,
 page INTEGER NOT NULL,section TEXT NOT NULL,paragraph INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
 text TEXT NOT NULL,terms TEXT NOT NULL,vector TEXT NOT NULL,embedding_model TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS knowledge_chunks_document ON knowledge_chunks(document_id,version);
CREATE TABLE IF NOT EXISTS knowledge_run_snapshots(run_id TEXT PRIMARY KEY,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_run_documents(run_id TEXT NOT NULL,document_id TEXT NOT NULL,version INTEGER NOT NULL,
 PRIMARY KEY(run_id,document_id));
CREATE TABLE IF NOT EXISTS knowledge_graph_nodes(id TEXT PRIMARY KEY,kb_id TEXT NOT NULL,document_id TEXT NOT NULL,version INTEGER NOT NULL,
 label TEXT NOT NULL,normalized TEXT NOT NULL,kind TEXT NOT NULL,source_page INTEGER,source_paragraph INTEGER,source_start INTEGER,source_end INTEGER,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS knowledge_graph_edges(id TEXT PRIMARY KEY,kb_id TEXT NOT NULL,document_id TEXT NOT NULL,version INTEGER NOT NULL,
 source_node_id TEXT NOT NULL,target_node_id TEXT NOT NULL,relation TEXT NOT NULL,source_page INTEGER,source_paragraph INTEGER,source_start INTEGER,source_end INTEGER,
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS knowledge_graph_nodes_kb ON knowledge_graph_nodes(kb_id,document_id,version);
CREATE INDEX IF NOT EXISTS knowledge_graph_edges_kb ON knowledge_graph_edges(kb_id,document_id,version);
"""


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)
    scope: Literal["private", "team"] = "private"
    enabled: bool = True
    default_for_new_assistants: bool = False


class KnowledgePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    scope: Literal["private", "team"] | None = None
    enabled: bool | None = None
    default_for_new_assistants: bool | None = None


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=6, ge=1, le=20)


def terms(text):
    text = text.lower()
    words = re.findall(r"[a-z0-9_]+", text)
    for sequence in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(sequence)
        words.extend(sequence[i:i + 2] for i in range(len(sequence) - 1))
    return Counter(words)


def lexical_vector(counts, size=512):
    """Explicitly lexical fallback, not a learned semantic embedding."""
    values = [0.0] * size
    for token, count in counts.items():
        digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % size
        values[bucket] += (1 if digest[4] & 1 else -1) * (1 + math.log(count))
    norm = math.sqrt(sum(value * value for value in values)) or 1
    return [round(value / norm, 7) for value in values]


class LocalEmbeddings:
    def __init__(self, root):
        # No automatic network requests unless explicitly installed/configured.
        self.name = os.getenv("AGENT_KNOWLEDGE_EMBEDDING_MODEL", "local-lexical-v1")
        self.cache = Path(os.getenv("AGENT_KNOWLEDGE_MODEL_CACHE", str(root / "models")))
        self.model_path = os.getenv("AGENT_KNOWLEDGE_MODEL_PATH")
        self.model = None
        self.lock = threading.RLock()

    def embed(self, texts, query=False):
        if self.name == "local-lexical-v1":
            return [lexical_vector(terms(text)) for text in texts]
        if self.name != MODEL:
            raise AppError(503, "EMBEDDING_CONFIG", "配置的本地向量模型未受支持")
        with self.lock:
            try:
                if self.model is None:
                    from fastembed import TextEmbedding
                    self.model = TextEmbedding(model_name=self.name, cache_dir=str(self.cache), threads=2,
                                               specific_model_path=self.model_path,
                                               local_files_only=os.getenv("AGENT_KNOWLEDGE_ALLOW_MODEL_DOWNLOAD", "false").lower() != "true")
                values = self.model.query_embed(texts[0]) if query and len(texts) == 1 else self.model.embed(texts, batch_size=16)
                return [[float(item) for item in vector] for vector in values]
            except AppError:
                raise
            except Exception as exc:
                raise AppError(503, "EMBEDDING_UNAVAILABLE", "本地向量模型尚未准备好，请管理员检查模型安装") from exc


class DocumentParser:
    def __init__(self):
        self.ocr = None
        self.lock = threading.Lock()

    def ocr_image(self, path):
        """Run optional local OCR for raster uploads without network access."""
        try:
            import numpy as np
            from PIL import Image
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise AppError(422, "OCR_REQUIRED", "图片需要安装本地 OCR 组件后重新索引") from exc
        with self.lock:
            if self.ocr is None:
                self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
            try:
                with Image.open(path) as image:
                    image.thumbnail((2600, 2600))
                    pixels = np.asarray(image.convert("RGB")).copy()
            except Exception as exc:
                raise AppError(422, "INVALID_DOCUMENT", "图片文件无法读取") from exc
            result, _ = self.ocr(pixels)
            return "\n".join(item[1] for item in (result or []) if len(item) > 2 and float(item[2]) >= 0.35)

    @staticmethod
    def _xlsx_column(reference):
        letters = re.match(r"[A-Z]+", reference or "")
        if not letters:
            return 0
        value = 0
        for char in letters.group(0):
            value = value * 26 + ord(char) - ord("A") + 1
        return value - 1

    def parse_xlsx(self, path):
        """Extract bounded worksheet rows using only the standard library."""
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 10000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
                raise AppError(413, "DOCUMENT_TOO_LARGE", "文档解压大小超过处理上限")
            names = set(archive.namelist())
            shared = []
            if "xl/sharedStrings.xml" in names:
                raw = archive.read("xl/sharedStrings.xml")
                if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                    raise AppError(422, "INVALID_DOCUMENT", "文档 XML 结构不受支持")
                root = ElementTree.fromstring(raw)
                shared = ["".join(item.itertext()) for item in root.findall(f"{{{namespace}}}si")]
            sheet_names = []
            if "xl/workbook.xml" in names:
                raw = archive.read("xl/workbook.xml")
                if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                    raise AppError(422, "INVALID_DOCUMENT", "文档 XML 结构不受支持")
                root = ElementTree.fromstring(raw)
                sheet_names = [item.attrib.get("name", f"Sheet {index + 1}") for index, item in enumerate(root.findall(f".//{{{namespace}}}sheet"))]
            worksheet_names = sorted((name for name in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)), key=lambda value: int(re.search(r"(\d+)", value).group(1)))
            if not worksheet_names:
                raise AppError(422, "EMPTY_DOCUMENT", "Excel 文件没有可读取的工作表")
            if len(worksheet_names) > MAX_PAGES:
                raise AppError(413, "DOCUMENT_TOO_LARGE", f"单个知识文档最多支持 {MAX_PAGES} 个工作表")
            pages = []
            for index, worksheet in enumerate(worksheet_names):
                raw = archive.read(worksheet)
                if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
                    raise AppError(422, "INVALID_DOCUMENT", "文档 XML 结构不受支持")
                root = ElementTree.fromstring(raw)
                rows = []
                for row in root.findall(f".//{{{namespace}}}row"):
                    cells = {}
                    for cell in row.findall(f"{{{namespace}}}c"):
                        value = cell.find(f"{{{namespace}}}v")
                        if cell.attrib.get("t") == "inlineStr":
                            text = "".join(cell.find(f"{{{namespace}}}is").itertext()) if cell.find(f"{{{namespace}}}is") is not None else ""
                        elif value is None:
                            text = ""
                        else:
                            text = value.text or ""
                            if cell.attrib.get("t") == "s":
                                try:
                                    text = shared[int(text)]
                                except (IndexError, ValueError):
                                    text = ""
                            elif cell.attrib.get("t") == "b":
                                text = "是" if text == "1" else "否"
                        cells[self._xlsx_column(cell.attrib.get("r"))] = text.strip()
                    if cells:
                        width = max(cells) + 1
                        line = "\t".join(cells.get(column, "") for column in range(width)).rstrip()
                        if line:
                            rows.append(line)
                text = "\n".join(rows)
                if text.strip():
                    title = sheet_names[index] if index < len(sheet_names) else f"Sheet {index + 1}"
                    pages.append({"page": index + 1, "text": f"[{title}]\n{text}", "extraction": "xlsx-cells"})
            return pages

    def ocr_page(self, page):
        try:
            import numpy as np
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            raise AppError(422, "OCR_REQUIRED", "文件包含扫描页，需要安装本地 OCR 组件后重新索引") from exc
        with self.lock:
            if self.ocr is None:
                self.ocr = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
            scale = min(2.2, 2200 / max(page.get_width(), page.get_height()))
            bitmap = page.render(scale=scale)
            try:
                # PDFium defaults to BGR; RapidOCR's ndarray interface uses BGR.
                pixels = bitmap.to_numpy().copy()
            finally:
                bitmap.close()
            result, _ = self.ocr(pixels)
            return "\n".join(item[1] for item in (result or []) if float(item[2]) >= 0.35)

    def parse(self, path, name):
        suffix = Path(name).suffix.lower()
        pages = []
        if suffix == ".pdf":
            try:
                import pypdfium2 as pdfium
                with pdfium.PdfDocument(path) as document:
                    if not 0 < len(document) <= MAX_PAGES:
                        raise AppError(413, "DOCUMENT_TOO_LARGE", f"单个知识文档最多支持 {MAX_PAGES} 页")
                    total = 0
                    for index in range(len(document)):
                        page = document[index]
                        try:
                            textpage = page.get_textpage()
                            try:
                                text = textpage.get_text_range().replace("\r\n", "\n").strip()
                            finally:
                                textpage.close()
                            extraction = "pdf-text"
                            if len(text) < 20:
                                text = self.ocr_page(page)
                                extraction = "local-ocr"
                        finally:
                            page.close()
                        total += len(text)
                        if total > MAX_TEXT:
                            raise AppError(413, "DOCUMENT_TOO_LARGE", "文档可提取文本超过处理上限")
                        pages.append({"page": index + 1, "text": text, "extraction": extraction})
            except ImportError as exc:
                raise AppError(503, "PARSER_UNAVAILABLE", "PDF 解析组件尚未安装") from exc
        elif suffix == ".docx":
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 10000 or sum(item.file_size for item in entries) > 100 * 1024 * 1024:
                    raise AppError(413, "DOCUMENT_TOO_LARGE", "文档解压大小超过处理上限")
                info = archive.getinfo("word/document.xml")
                if info.file_size > 20 * 1024 * 1024:
                    raise AppError(413, "DOCUMENT_TOO_LARGE", "文档内容超过处理上限")
                xml = archive.read(info)
                if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
                    raise AppError(422, "INVALID_DOCUMENT", "文档 XML 结构不受支持")
                root = ElementTree.fromstring(xml)
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                text = "\n\n".join("".join(p.itertext()) for p in root.findall(".//w:p", ns))
                pages = [{"page": 1, "text": text, "extraction": "docx-paragraphs"}]
        elif suffix == ".xlsx":
            pages = self.parse_xlsx(path)
        elif suffix in {".txt", ".md", ".csv", ".json"}:
            raw = path.read_bytes()
            encoding = "utf-16" if raw[:2] in {b"\xff\xfe", b"\xfe\xff"} else "utf-8-sig"
            text = raw.decode(encoding)
            if "\x00" in text:
                raise AppError(422, "INVALID_DOCUMENT", "文件内容不是可读取的文本")
            pages = [{"page": 1, "text": text, "extraction": "text"}]
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
            text = self.ocr_image(path)
            pages = [{"page": 1, "text": text, "extraction": "local-ocr"}]
        else:
            raise AppError(415, "DOCUMENT_TYPE_UNSUPPORTED", "支持 PDF、DOCX、XLSX、Markdown、TXT、CSV、JSON 和常见图片文件")
        if sum(len(page["text"]) for page in pages) > MAX_TEXT:
            raise AppError(413, "DOCUMENT_TOO_LARGE", "文档文本超过处理上限")
        if not any(page["text"].strip() for page in pages):
            raise AppError(422, "EMPTY_DOCUMENT", "未提取到可检索文本，请检查文件内容或 OCR 结果")
        return pages


def chunk_pages(pages):
    chunks = []
    for page in pages:
        text, section, paragraph = page["text"], "", 0
        # Keep offsets into extracted page text, never invent original PDF paragraphs.
        for match in re.finditer(r"[^\n]+(?:\n(?!\n)[^\n]+)*", text):
            block = match.group(0).strip()
            if not block:
                continue
            paragraph += 1
            heading = re.match(r"^#{1,6}\s+([^\n]+)", block)
            if heading:
                section = heading.group(1)[:200]
            for offset in range(0, len(block), 900):
                value = block[offset:offset + 1100]
                chunks.append({"id": uid(), "page": page["page"], "section": section, "paragraph": paragraph,
                               "start": match.start() + offset, "end": match.start() + offset + len(value), "text": value})
                if len(chunks) > MAX_CHUNKS:
                    raise AppError(413, "DOCUMENT_TOO_LARGE", "知识片段数量超过单文档上限")
    return chunks


class KnowledgeService:
    def __init__(self, service):
        self.service, self.db = service, service.db
        with self.db.lock:
            self.db.conn.executescript(SCHEMA)
        self.root = service.settings.data_dir / "knowledge"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.parser = DocumentParser()
        self.embeddings = LocalEmbeddings(self.root)
        self.index_lock = threading.RLock()
        # A crashed parse must not stay apparently in progress forever.
        self.db.execute("UPDATE knowledge_versions SET status='failed',error='上次索引中断，请重新索引' WHERE status='processing'")

    def require(self, kb_id, user, edit=False):
        row = self.db.one("SELECT * FROM knowledge_bases WHERE id=? AND deleted=0", (kb_id,))
        access = self.service.resource_editable if edit else self.service.resource_access
        if not row or not access(row, user):
            raise AppError(404, "NOT_FOUND", "知识库不存在或无访问权限")
        return row

    def view(self, row, user):
        count = self.db.one("SELECT COUNT(*) n FROM knowledge_documents WHERE kb_id=? AND deleted=0", (row["id"],))["n"]
        statuses = self.db.all("SELECT v.status,COUNT(*) n FROM knowledge_documents d JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.current_version WHERE d.kb_id=? AND d.deleted=0 GROUP BY v.status", (row["id"],))
        return {**row, "enabled": bool(row["enabled"]), "default_for_new_assistants": bool(row["default_for_new_assistants"]),
                "can_edit": self.service.resource_editable(row, user), "document_count": count,
                "index_status": {item["status"]: item["n"] for item in statuses}, "retrieval": self.retrieval_info()}

    def retrieval_info(self):
        return {"mode": "hybrid", "lexical": "BM25", "vector_model": self.embeddings.name,
                "semantic": self.embeddings.name != "local-lexical-v1", "execution": "server-local"}

    def list(self, user):
        return [self.view(row, user) for row in self.db.all("SELECT * FROM knowledge_bases WHERE deleted=0 ORDER BY updated_at DESC") if self.service.resource_access(row, user)]

    def save(self, data, user, kb_id=None):
        data = dict(data)
        old = self.require(kb_id, user, edit=True) if kb_id else None
        values = {**({"name": "", "description": "", "scope": "private", "enabled": True, "default_for_new_assistants": False} if old is None else old), **data}
        if values["scope"] == "team" and user["role"] != "admin":
            raise AppError(403, "ADMIN_REQUIRED", "团队共享知识库由管理员管理")
        if values["default_for_new_assistants"] and (values["scope"] != "team" or user["role"] != "admin"):
            raise AppError(403, "ADMIN_REQUIRED", "仅管理员可设置团队默认知识库")
        if old and old["scope"] != values["scope"]:
            self.service.validate_resource_scope_change("knowledge_bases", kb_id, values["scope"], old["owner_id"])
        stamp = now()
        if old:
            self.db.execute("UPDATE knowledge_bases SET name=?,description=?,scope=?,enabled=?,default_for_new_assistants=?,revision=revision+1,updated_at=? WHERE id=?",
                            (values["name"].strip(), values["description"], values["scope"], int(values["enabled"]), int(values["default_for_new_assistants"]), stamp, kb_id))
        else:
            kb_id = uid()
            self.db.execute("INSERT INTO knowledge_bases VALUES(?,?,?,?,?,?,?,1,0,?,?)", (kb_id, values["name"].strip(), values["description"], user["id"], values["scope"], int(values["enabled"]), int(values["default_for_new_assistants"]), stamp, stamp))
        return self.view(self.require(kb_id, user), user)

    def delete(self, kb_id, user):
        self.require(kb_id, user, edit=True)
        self.db.execute("UPDATE knowledge_bases SET deleted=1,enabled=0,revision=revision+1,updated_at=? WHERE id=?", (now(), kb_id))

    def document(self, document_id, user, edit=False):
        row = self.db.one("SELECT * FROM knowledge_documents WHERE id=? AND deleted=0", (document_id,))
        if not row:
            raise AppError(404, "NOT_FOUND", "知识文件不存在或无访问权限")
        self.require(row["kb_id"], user, edit)
        return row

    def version(self, document_id, version, user):
        document = self.document(document_id, user)
        row = self.db.one("SELECT * FROM knowledge_versions WHERE document_id=? AND version=?", (document_id, version or document["current_version"]))
        if not row:
            raise AppError(404, "NOT_FOUND", "知识文件版本不存在")
        return {**document, **row}

    def document_view(self, row):
        return {key: row[key] for key in ("id", "kb_id", "name", "mime_type", "version", "sha256", "size", "status", "error", "page_count", "chunk_count", "embedding_model", "created_at")} | {
            "extraction": json.loads(row["extraction"]), "content_url": f"/api/v1/knowledge-documents/{row['id']}/content?version={row['version']}"}

    def documents(self, kb_id, user):
        self.require(kb_id, user)
        return [self.document_view(row) for row in self.db.all("SELECT d.*,v.* FROM knowledge_documents d JOIN knowledge_versions v ON v.document_id=d.id AND v.version=d.current_version WHERE d.kb_id=? AND d.deleted=0 ORDER BY d.created_at DESC", (kb_id,))]

    def versions(self, document_id, user):
        document = self.document(document_id, user)
        return [self.document_view({**document, **row}) for row in self.db.all("SELECT * FROM knowledge_versions WHERE document_id=? ORDER BY version DESC", (document_id,))]

    def upload(self, kb_id, user, name, data, document_id=None):
        self.require(kb_id, user, edit=True)
        name = safe_name(name)
        suffix = Path(name).suffix.lower()
        mime = {".pdf": "application/pdf", ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".md": "text/markdown", ".txt": "text/plain", ".csv": "text/csv", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff"}.get(suffix)
        if not mime:
            raise AppError(415, "DOCUMENT_TYPE_UNSUPPORTED", "支持 PDF、DOCX、XLSX、Markdown、TXT、CSV、JSON 和常见图片文件")
        if not data or len(data) > self.service.settings.upload_max_bytes:
            raise AppError(413, "DOCUMENT_TOO_LARGE", "文件为空或超过上传大小限制")
        digest, stamp = hashlib.sha256(data).hexdigest(), now()
        with self.index_lock:
            document = self.document(document_id, user, edit=True) if document_id else None
            if document and document["kb_id"] != kb_id:
                raise AppError(404, "NOT_FOUND", "知识文件不属于此知识库")
            if document and Path(document["name"]).suffix.lower() != suffix:
                raise AppError(422, "DOCUMENT_TYPE_CHANGED", "新版本需要保持原文件格式")
            document_id = document_id or uid()
            version = document["current_version"] + 1 if document else 1
            directory = self.root / kb_id / document_id
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / f"{version}{suffix}"
            with path.open("xb") as target:
                target.write(data)
            path.chmod(0o400)
            with self.db.transaction():
                if document:
                    self.db.execute("UPDATE knowledge_documents SET current_version=? WHERE id=?", (version, document_id))
                else:
                    self.db.execute("INSERT INTO knowledge_documents VALUES(?,?,?,?,0,?)", (document_id, kb_id, name, version, stamp))
                self.db.execute("INSERT INTO knowledge_versions(document_id,version,mime_type,sha256,size,path,status,created_at) VALUES(?,?,?,?,?,?,'processing',?)", (document_id, version, mime, digest, len(data), str(path), stamp))
            self._index(document_id, version, path, name)
            self.db.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), kb_id))
            return self.document_view(self.version(document_id, version, user))

    def _index(self, document_id, version, path, name):
        try:
            pages = self.parser.parse(path, name)
            chunks = chunk_pages(pages)
            if not chunks:
                raise AppError(422, "EMPTY_DOCUMENT", "未找到可检索的文本片段")
            vectors = self.embeddings.embed([chunk["text"] for chunk in chunks])
            with self.db.transaction():
                self.db.execute("DELETE FROM knowledge_pages WHERE document_id=? AND version=?", (document_id, version))
                self.db.execute("DELETE FROM knowledge_chunks WHERE document_id=? AND version=?", (document_id, version))
                for page in pages:
                    self.db.execute("INSERT INTO knowledge_pages VALUES(?,?,?,?,?)", (document_id, version, page["page"], page["text"], page["extraction"]))
                for chunk, vector in zip(chunks, vectors):
                    self.db.execute("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (chunk["id"], document_id, version, chunk["page"], chunk["section"], chunk["paragraph"], chunk["start"], chunk["end"], chunk["text"], dump(terms(chunk["text"])), dump(vector), self.embeddings.name))
                self._build_graph(document_id, version, chunks)
                self.db.execute("UPDATE knowledge_versions SET status='ready',error=NULL,page_count=?,chunk_count=?,extraction=?,embedding_model=? WHERE document_id=? AND version=?", (len(pages), len(chunks), dump(sorted({page["extraction"] for page in pages})), self.embeddings.name, document_id, version))
        except Exception as exc:
            message = exc.message if isinstance(exc, AppError) else "文档解析或索引失败，请检查格式后重试"
            self.db.execute("UPDATE knowledge_versions SET status=?,error=? WHERE document_id=? AND version=?", ("needs_ocr" if isinstance(exc, AppError) and exc.code == "OCR_REQUIRED" else "failed", message, document_id, version))

    def _build_graph(self, document_id, version, chunks):
        """Build a deterministic, source-linked co-occurrence graph for a version."""
        document = self.db.one("SELECT kb_id FROM knowledge_documents WHERE id=?", (document_id,))
        if not document:
            return
        kb_id = document["kb_id"]
        stamp = now()
        self.db.execute("DELETE FROM knowledge_graph_edges WHERE document_id=? AND version=?", (document_id, version))
        self.db.execute("DELETE FROM knowledge_graph_nodes WHERE document_id=? AND version=?", (document_id, version))
        nodes = {}
        edges = {}
        for chunk in chunks:
            labels = []
            for value in re.findall(r"[\u3400-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,30}", chunk["text"]):
                normalized = value.casefold().strip(" _-")
                if len(normalized) < 2 or normalized in GRAPH_STOPWORDS or normalized in labels:
                    continue
                labels.append(normalized)
                if len(labels) >= 8:
                    break
            if len(labels) < 1:
                continue
            source = (chunk["page"], chunk["paragraph"], chunk["start"], chunk["end"])
            node_ids = []
            for normalized in labels:
                node_id = hashlib.sha256(f"{kb_id}:{document_id}:{version}:{normalized}".encode()).hexdigest()[:32]
                node_ids.append(node_id)
                nodes.setdefault(node_id, {"id": node_id, "label": normalized, "normalized": normalized, "kind": "term" if normalized.isascii() else "entity", "source": source})
            for left, right in zip(node_ids, node_ids[1:]):
                source_id, target_id = sorted((left, right))
                edge_id = hashlib.sha256(f"{document_id}:{version}:{source_id}:{target_id}:{chunk['id']}".encode()).hexdigest()[:32]
                edges.setdefault(edge_id, {"id": edge_id, "source": source_id, "target": target_id, "relation": "co_occurs", "location": source})
        for node in nodes.values():
            page, paragraph, start, end = node["source"]
            self.db.execute("INSERT INTO knowledge_graph_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (node["id"], kb_id, document_id, version, node["label"], node["normalized"], node["kind"], page, paragraph, start, end, stamp))
        for edge in edges.values():
            page, paragraph, start, end = edge["location"]
            self.db.execute("INSERT INTO knowledge_graph_edges VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (edge["id"], kb_id, document_id, version, edge["source"], edge["target"], edge["relation"], page, paragraph, start, end, stamp))

    def graph(self, kb_id, user, query=None, limit=120):
        self.require(kb_id, user)
        limit = max(1, min(200, int(limit)))
        values = [kb_id]
        query_clause = ""
        if query and query.strip():
            query_clause = " AND (n.label LIKE ? OR n.normalized LIKE ?)"
            needle = f"%{query.strip()[:80]}%"
            values.extend([needle, needle])
        values.append(limit)
        nodes = self.db.all(f"""SELECT n.* FROM knowledge_graph_nodes n
            JOIN knowledge_documents d ON d.id=n.document_id AND d.current_version=n.version AND d.deleted=0
            JOIN knowledge_versions v ON v.document_id=n.document_id AND v.version=n.version AND v.status='ready'
            WHERE n.kb_id=?{query_clause} ORDER BY n.label,n.id LIMIT ?""", values)
        node_ids = {node["id"] for node in nodes}
        edges = []
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            edge_values = [kb_id, *node_ids, *node_ids]
            edges = self.db.all(f"""SELECT e.* FROM knowledge_graph_edges e
                JOIN knowledge_documents d ON d.id=e.document_id AND d.current_version=e.version AND d.deleted=0
                JOIN knowledge_versions v ON v.document_id=e.document_id AND v.version=e.version AND v.status='ready'
                WHERE e.kb_id=? AND e.source_node_id IN ({placeholders}) AND e.target_node_id IN ({placeholders})
                ORDER BY e.id LIMIT ?""", [*edge_values, limit * 3])
        def source(row):
            return {"document_id": row["document_id"], "version": row["version"], "page": row["source_page"], "paragraph": row["source_paragraph"], "start": row["source_start"], "end": row["source_end"]}
        return {"nodes": [{"id": row["id"], "label": row["label"], "kind": row["kind"], "source": source(row)} for row in nodes],
                "edges": [{"id": row["id"], "source": row["source_node_id"], "target": row["target_node_id"], "relation": row["relation"], "provenance": source(row)} for row in edges],
                "versioned": True, "source": "local-extraction", "query": query or ""}

    def reindex(self, document_id, user):
        self.document(document_id, user, edit=True)
        row = self.version(document_id, None, user)
        with self.index_lock:
            self.db.execute("UPDATE knowledge_versions SET status='processing',error=NULL WHERE document_id=? AND version=?", (document_id, row["version"]))
            self._index(document_id, row["version"], self.content_path(row), row["name"])
            self.db.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), row["kb_id"]))
        return self.document_view(self.version(document_id, None, user))

    def content_path(self, row):
        path = Path(row["path"])
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()) or not path.is_file():
            raise AppError(404, "NOT_FOUND", "知识原件不可用")
        return path

    def chunks(self, document_id, user, version=None):
        row = self.version(document_id, version, user)
        result = [{key: chunk[key] for key in ("id", "document_id", "version", "page", "section", "paragraph", "start", "end", "text")} for chunk in self.db.all("SELECT * FROM knowledge_chunks WHERE document_id=? AND version=? ORDER BY page,start", (document_id, row["version"]))]
        if Path(row["name"]).suffix.lower() != ".pdf":
            for item in result:
                item["page"] = None
        return result

    def _search(self, kb_ids, user, query, limit, run_id=None):
        kb_ids = list(dict.fromkeys(kb_ids))[:100]
        for kb_id in kb_ids:
            if not self.require(kb_id, user)["enabled"]:
                raise AppError(403, "KNOWLEDGE_DISABLED", "知识库已停用")
        if not kb_ids:
            return {"results": [], "retrieval": self.retrieval_info()}
        placeholders = ",".join("?" for _ in kb_ids)
        version_clause = "EXISTS(SELECT 1 FROM knowledge_run_documents rd WHERE rd.run_id=? AND rd.document_id=c.document_id AND rd.version=c.version)" if run_id else "d.current_version=c.version"
        values = ([run_id] if run_id else []) + kb_ids
        rows = self.db.all(f"SELECT c.*,d.name,d.kb_id,v.sha256,v.created_at AS imported_at,k.name AS knowledge_base FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND {version_clause} JOIN knowledge_versions v ON v.document_id=c.document_id AND v.version=c.version JOIN knowledge_bases k ON k.id=d.kb_id WHERE d.kb_id IN ({placeholders}) AND d.deleted=0 AND v.status='ready' LIMIT 50000", values)
        if not rows:
            return {"results": [], "retrieval": self.retrieval_info()}
        counts, query_terms = [json.loads(row["terms"]) for row in rows], terms(query)
        average = sum(sum(count.values()) for count in counts) / len(counts) or 1
        document_frequency = {token: sum(token in count for count in counts) for token in query_terms}
        vector = self.embeddings.embed([query], query=True)[0]
        ranked = []
        for row, count in zip(rows, counts):
            length = sum(count.values()) or 1
            score = 0.0
            for token in query_terms:
                frequency = count.get(token, 0)
                inverse = math.log(1 + (len(rows) - document_frequency[token] + .5) / (document_frequency[token] + .5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / average))
            dense = json.loads(row["vector"])
            cosine = sum(a * b for a, b in zip(vector, dense)) if row["embedding_model"] == self.embeddings.name else 0
            ranked.append({**row, "lexical_score": score, "vector_score": cosine})
        lexical = sorted(ranked, key=lambda row: row["lexical_score"], reverse=True)
        dense = sorted(ranked, key=lambda row: row["vector_score"], reverse=True)
        scores = Counter()
        for order, key in ((lexical, "lexical_score"), (dense, "vector_score")):
            for index, row in enumerate(order):
                if row[key] > (0 if key == "lexical_score" else .1):
                    scores[row["id"]] += 1 / (60 + index + 1)
        result = []
        for row in sorted(ranked, key=lambda item: scores[item["id"]], reverse=True):
            if not scores[row["id"]]:
                continue
            result.append({key: row[key] for key in ("document_id", "version", "page", "section", "paragraph", "start", "end", "text", "name", "kb_id", "knowledge_base", "sha256", "imported_at")} | {"title": row["name"], "excerpt": row["text"], "chunk_id": row["id"], "score": round(scores[row["id"]], 6), "page": row["page"] if Path(row["name"]).suffix.lower() == ".pdf" else None})
            if len(result) >= limit:
                break
        return {"results": result, "retrieval": self.retrieval_info()}

    def search(self, kb_ids, user, query, limit=6, run_id=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise AppError(422, "INVALID_QUERY", "请输入有效的知识检索问题")
        return self._search(kb_ids, user, query, max(1, min(int(limit), 20)), run_id)

    def snapshot_for_run(self, run_id, assistant, user):
        """Called inside prepare_run's transaction; no nested transaction here."""
        with self.db.lock:
            if self.db.one("SELECT run_id FROM knowledge_run_snapshots WHERE run_id=?", (run_id,)):
                return
            kb_ids = assistant.get("knowledge_base_ids", [])
            kb_ids = json.loads(kb_ids) if isinstance(kb_ids, str) else kb_ids
            for kb_id in kb_ids:
                kb = self.require(kb_id, user)
                if not kb["enabled"]:
                    continue
                for document in self.db.all("SELECT id,current_version FROM knowledge_documents WHERE kb_id=? AND deleted=0", (kb_id,)):
                    self.db.execute("INSERT OR IGNORE INTO knowledge_run_documents VALUES(?,?,?)", (run_id, document["id"], document["current_version"]))
            self.db.execute("INSERT OR IGNORE INTO knowledge_run_snapshots VALUES(?,?)", (run_id, now()))

    def _assistant_ids(self, user, assistant, run_id):
        run = self.service.require("runs", run_id, user)
        conversation = self.service.require("conversations", run["conversation_id"], user)
        if conversation["assistant_id"] != assistant["id"]:
            raise AppError(403, "ASSISTANT_UNAVAILABLE", "知识工具与当前助手不匹配")
        current = self.service.usable_assistant(assistant["id"], user)
        snapshot_ids, current_ids = assistant.get("knowledge_base_ids", []), current.get("knowledge_base_ids", [])
        snapshot_ids = json.loads(snapshot_ids) if isinstance(snapshot_ids, str) else snapshot_ids
        current_ids = json.loads(current_ids) if isinstance(current_ids, str) else current_ids
        return [item for item in snapshot_ids if item in current_ids]

    def _register_chunk(self, run_id, item):
        if Path(item["name"]).suffix.lower() != ".pdf":
            item = {**item, "page": None}
        locator = {"knowledge_base": item["knowledge_base"], "knowledge_base_id": item["kb_id"], "file": item["name"],
                   "version": item["version"], "sha256": item["sha256"], "page": item["page"], "section": item["section"],
                   "paragraph": item["paragraph"], "chunk_id": item["chunk_id"], "start": item.get("start"), "end": item.get("end"), "imported_at": item["imported_at"]}
        source = self.service.evidence.register(run_id, "knowledge", item["name"], locator, item["text"], resource_type="knowledge_base", resource_id=item["kb_id"], document_id=item["document_id"], document_version=item["version"])
        return {**item, "source_id": source["id"], "citation": f"[[source:{source['id']}]]"}

    async def search_for_run(self, user, assistant, run_id, query, limit=6):
        kb_ids = self._assistant_ids(user, assistant, run_id)
        self.snapshot_for_run(run_id, assistant, user)
        result = await asyncio.to_thread(self.search, kb_ids, user, query, limit, run_id)
        result["results"] = [self._register_chunk(run_id, item) for item in result["results"]]
        return result

    async def read_for_run(self, user, assistant, run_id, document_id, page=None, version=None):
        kb_ids = self._assistant_ids(user, assistant, run_id)
        self.snapshot_for_run(run_id, assistant, user)
        pinned = self.db.one("SELECT version FROM knowledge_run_documents WHERE run_id=? AND document_id=?", (run_id, document_id))
        if not pinned:
            raise AppError(404, "NOT_FOUND", "文件不在本次运行的知识版本范围内")
        # A historical citation may ask for an older immutable version; newer
        # versions imported while the run executes are never silently adopted.
        if version is not None and version > pinned["version"]:
            raise AppError(409, "KNOWLEDGE_VERSION_CHANGED", "此版本在本次运行开始之后导入，请在下一次任务读取")
        row = self.version(document_id, version or pinned["version"], user)
        if row["kb_id"] not in kb_ids:
            raise AppError(404, "NOT_FOUND", "文件不在当前助手知识范围内")
        if row["status"] != "ready":
            raise AppError(409, "KNOWLEDGE_NOT_READY", "知识文件尚未完成索引")
        kb = self.require(row["kb_id"], user)
        pages = self.db.all("SELECT * FROM knowledge_pages WHERE document_id=? AND version=? AND (? IS NULL OR page=?) ORDER BY page LIMIT 5", (document_id, row["version"], page, page))
        if not pages:
            raise AppError(404, "NOT_FOUND", "请求的页码不存在")
        result = []
        budget = 18000
        for item in pages:
            text = item["text"][:budget]
            budget -= len(text)
            if not text:
                break
            result.append(self._register_chunk(run_id, {"document_id": document_id, "kb_id": row["kb_id"], "name": row["name"], "knowledge_base": kb["name"], "version": row["version"], "sha256": row["sha256"], "imported_at": row["created_at"], "page": item["page"], "section": "", "paragraph": None, "chunk_id": None, "start": 0, "end": len(text), "text": text}))
        return {"results": result, "page_count": row["page_count"], "returned_pages": [item["page"] for item in result], "truncated": len(pages) < row["page_count"] if page is None else len(pages[0]["text"]) > 18000}


def create_knowledge_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1")
    @router.get("/knowledge-bases")
    def bases(user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.list(user)
    @router.post("/knowledge-bases", status_code=201)
    def create(data: KnowledgeInput, user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.save(data.model_dump(), user)
    @router.get("/knowledge-bases/{kb_id}")
    def base(kb_id: str, user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.view(service.knowledge.require(kb_id, user), user)
    @router.patch("/knowledge-bases/{kb_id}")
    def update(kb_id: str, data: KnowledgePatch, user=Depends(authenticated), service=Depends(svc)):
        values = data.model_dump(exclude_unset=True)
        if any(value is None for value in values.values()):
            raise AppError(422, "VALIDATION_ERROR", "知识库字段不能设置为空值")
        return service.knowledge.save(values, user, kb_id)
    @router.delete("/knowledge-bases/{kb_id}", status_code=204)
    def delete(kb_id: str, user=Depends(authenticated), service=Depends(svc)):
        service.knowledge.delete(kb_id, user)
    @router.get("/knowledge-bases/{kb_id}/documents")
    def documents(kb_id: str, user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.documents(kb_id, user)
    @router.get("/knowledge-bases/{kb_id}/graph")
    def graph(kb_id: str, q: str | None = Query(default=None, max_length=80), limit: int = Query(default=120, ge=1, le=200), user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.graph(kb_id, user, q, limit)
    async def ingest(kb_id, file, user, service, document_id=None):
        service.knowledge.require(kb_id, user, edit=True)
        data = await file.read(service.settings.upload_max_bytes + 1)
        return await asyncio.to_thread(service.knowledge.upload, kb_id, user, file.filename or "document.txt", data, document_id)
    @router.post("/knowledge-bases/{kb_id}/documents", status_code=201)
    async def upload(kb_id: str, file: UploadFile = File(...), user=Depends(authenticated), service=Depends(svc)):
        return await ingest(kb_id, file, user, service)
    @router.post("/knowledge-bases/{kb_id}/documents/{document_id}/versions", status_code=201)
    async def upload_version(kb_id: str, document_id: str, file: UploadFile = File(...), user=Depends(authenticated), service=Depends(svc)):
        return await ingest(kb_id, file, user, service, document_id)
    @router.get("/knowledge-documents/{document_id}/versions")
    def versions(document_id: str, user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.versions(document_id, user)
    @router.post("/knowledge-documents/{document_id}/reindex")
    async def reindex(document_id: str, user=Depends(authenticated), service=Depends(svc)):
        return await asyncio.to_thread(service.knowledge.reindex, document_id, user)
    @router.delete("/knowledge-documents/{document_id}", status_code=204)
    def delete_document(document_id: str, user=Depends(authenticated), service=Depends(svc)):
        row = service.knowledge.document(document_id, user, edit=True)
        service.db.execute("UPDATE knowledge_documents SET deleted=1 WHERE id=?", (document_id,))
        service.db.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), row["kb_id"]))
    @router.get("/knowledge-documents/{document_id}/content")
    def content(document_id: str, version: int | None = Query(default=None, ge=1), user=Depends(authenticated), service=Depends(svc)):
        row = service.knowledge.version(document_id, version, user)
        path = service.knowledge.content_path(row)
        return FileResponse(path, media_type=row["mime_type"], filename=row["name"], content_disposition_type="inline" if row["mime_type"] == "application/pdf" else "attachment", headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
    @router.get("/knowledge-documents/{document_id}/chunks")
    def chunks(document_id: str, version: int | None = Query(default=None, ge=1), user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.chunks(document_id, user, version)
    @router.post("/knowledge-bases/{kb_id}/search")
    async def search(kb_id: str, data: SearchInput, user=Depends(authenticated), service=Depends(svc)):
        return await asyncio.to_thread(service.knowledge.search, [kb_id], user, data.query, data.limit)
    return router
