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

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .db import dump, now, uid
from .deepseek_api import DeepSeekAPI
from .openai_visual import MAX_IMAGE_BYTES, call_visual_json
from .provider_policy import classify_provider_url
from .security import AppError, safe_name
from .vision import IMAGE_SUFFIXES, VisionRuntime

# Public industrial standards can exceed 200 pages. Keep the parser bounded,
# but allow the 316-page NIST SP 800-82r3 sample without forcing a lossy split;
# MAX_TEXT and MAX_CHUNKS remain the independent content limits.
MAX_PAGES = 400
MAX_TEXT = 2_000_000
MAX_CHUNKS = 5000
MAX_IMAGE_REGIONS = 16
MAX_IMAGE_REGION_ROWS = 4
MAX_IMAGE_REGION_COLUMNS = 4
IMAGE_REGION_THUMBNAIL = (320, 240)
MAX_MODEL_VISUAL_ENTITIES = 64
MAX_MODEL_VISUAL_RELATIONS = 128
# Search responses can contain multiple hits from the same image. Keep media
# evidence bounded while the client still has a useful visual preview.
MAX_SEARCH_MEDIA_REGIONS = 4
MODEL = "BAAI/bge-small-zh-v1.5"
GRAPH_STOPWORDS = {"设备", "系统", "数据", "文件", "内容", "说明", "检查", "需要", "可以", "以及", "进行", "其中", "这个", "当前"}
# These patterns intentionally stay deterministic and explainable.  They are
# useful for graph navigation and entity-focused acceptance fixtures, but are
# not a substitute for a trained named-entity or relation extraction model.
GRAPH_ENTITY_PATTERNS = (
    ("standard", re.compile(r"(?i)\b(?:NIST\s+)?(?:GB(?:/T)?|ISO|IEC|ASTM|NIST|OSHA|CFR|SP|IR)\s*[-/]?\s*[A-Z]?\d{2,5}(?:[./-]\d+)*(?:\s*[-/]?r?\d+)?(?:\s+Rev\.?\s*\d+)?")),
    ("metric", re.compile(r"(?i)(?:合格率|良率|压力|温度|扭矩|厚度|流量|转速|yield_rate)\s*(?:[:=：为是]\s*)[-+]?\d+(?:\.\d+)?\s*(?:%|bar|kPa|MPa|℃|°C|mm|cm|Nm|rpm|件|小时|天)?")),
    ("equipment", re.compile(r"(?i)(?:\b[A-Z]{1,5}[-_]?\d{1,5}\b|[\u3400-\u9fff]{1,12}(?:泵|阀|炉|机|线|站|罐|管|床|压机|机器人))")),
    ("material", re.compile(r"(?:不锈钢|碳钢|铝合金|聚乙烯|聚氯乙烯|铜合金|钛合金|钢材|铝材)")),
    ("process", re.compile(r"(?:焊接|铸造|锻造|车削|铣削|磨削|热处理|喷涂|装配|校准|机加工|检验)")),
    ("organization", re.compile(r"(?i)\b(?:NIST|OSHA|Better\s+AI|华微世纪|华为)\b")),
)
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
CREATE TABLE IF NOT EXISTS knowledge_image_assets(
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 mime_type TEXT NOT NULL,
 width INTEGER NOT NULL,
 height INTEGER NOT NULL,
 thumbnail_path TEXT,
 thumbnail_width INTEGER NOT NULL DEFAULT 0,
 thumbnail_height INTEGER NOT NULL DEFAULT 0,
 bytes INTEGER NOT NULL DEFAULT 0,
 metadata_json TEXT NOT NULL DEFAULT '{}',
 ocr_status TEXT NOT NULL DEFAULT 'pending',
 visual_index_status TEXT NOT NULL DEFAULT 'needs_model',
 created_at TEXT NOT NULL,
 PRIMARY KEY(document_id,version));
CREATE INDEX IF NOT EXISTS knowledge_image_assets_document ON knowledge_image_assets(document_id,version);
CREATE TABLE IF NOT EXISTS knowledge_page_previews(
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 page INTEGER NOT NULL,
 thumbnail_path TEXT NOT NULL,
 width INTEGER NOT NULL,
 height INTEGER NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(document_id,version,page));
CREATE INDEX IF NOT EXISTS knowledge_page_previews_document ON knowledge_page_previews(document_id,version,page);
CREATE TABLE IF NOT EXISTS knowledge_image_regions(
 id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 grid_row INTEGER NOT NULL,
 grid_col INTEGER NOT NULL,
 grid_rows INTEGER NOT NULL,
 grid_cols INTEGER NOT NULL,
 x INTEGER NOT NULL,
 y INTEGER NOT NULL,
 width INTEGER NOT NULL,
 height INTEGER NOT NULL,
 thumbnail_path TEXT NOT NULL,
 thumbnail_width INTEGER NOT NULL,
 thumbnail_height INTEGER NOT NULL,
 visual_index_status TEXT NOT NULL DEFAULT 'needs_model',
 entity_index_status TEXT NOT NULL DEFAULT 'needs_model',
 created_at TEXT NOT NULL,
 UNIQUE(document_id,version,grid_row,grid_col));
CREATE INDEX IF NOT EXISTS knowledge_image_regions_document ON knowledge_image_regions(document_id,version,grid_row,grid_col);
CREATE TABLE IF NOT EXISTS knowledge_visual_embeddings(
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 region_id TEXT NOT NULL DEFAULT '',
 model TEXT NOT NULL,
 dimensions INTEGER NOT NULL,
 vector TEXT NOT NULL,
 device TEXT NOT NULL DEFAULT 'cpu',
 created_at TEXT NOT NULL,
 PRIMARY KEY(document_id,version,region_id));
CREATE INDEX IF NOT EXISTS knowledge_visual_embeddings_document ON knowledge_visual_embeddings(document_id,version);
CREATE TABLE IF NOT EXISTS knowledge_visual_ocr(
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 region_id TEXT NOT NULL DEFAULT '',
 model TEXT NOT NULL,
 text TEXT NOT NULL,
 boxes TEXT NOT NULL DEFAULT '[]',
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(document_id,version,region_id));
CREATE INDEX IF NOT EXISTS knowledge_visual_ocr_document ON knowledge_visual_ocr(document_id,version);
CREATE TABLE IF NOT EXISTS knowledge_visual_entities(
 id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 region_id TEXT NOT NULL DEFAULT '',
 label TEXT NOT NULL,
 normalized TEXT NOT NULL,
 kind TEXT NOT NULL,
 confidence REAL NOT NULL DEFAULT 0,
 source_text TEXT NOT NULL DEFAULT '',
 source_boxes TEXT NOT NULL DEFAULT '[]',
 source_offset_start INTEGER,
 source_offset_end INTEGER,
 evidence_kind TEXT NOT NULL DEFAULT 'source_bound',
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS knowledge_visual_entities_document ON knowledge_visual_entities(document_id,version);
CREATE TABLE IF NOT EXISTS knowledge_visual_relations(
 id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 source_entity_id TEXT NOT NULL,
 target_entity_id TEXT NOT NULL,
 relation TEXT NOT NULL,
 confidence REAL NOT NULL DEFAULT 0,
 evidence TEXT NOT NULL DEFAULT '',
 evidence_kind TEXT NOT NULL DEFAULT 'evidence_only',
 created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS knowledge_visual_relations_document ON knowledge_visual_relations(document_id,version);
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
    # Human-facing retrieval mode.  ``hybrid`` is the safe default used by
    # assistant runs; users can explicitly compare lexical BM25 and local
    # vector retrieval without changing the indexed data.
    # Keep this a bounded string so an unknown value reaches the shared
    # deployment-policy check as 403 FEATURE_DISABLED in trial mode instead of
    # being rejected earlier by Pydantic as a generic 422.
    mode: str = Field(default="hybrid", max_length=32)
    context_chars: int = Field(default=12000, ge=0, le=24000)


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
        if not any(page["text"].strip() for page in pages) and suffix not in IMAGE_SUFFIXES:
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
            # Knowledge schema bootstrap follows the same rule as the main
            # database: one transaction, so synchronous=FULL fsyncs the
            # migration once instead of once per CREATE/ALTER statement.
            self.db.conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
            self.db.conn.execute("BEGIN IMMEDIATE")
            # Visual tables were introduced after the first knowledge release.
            # Add provenance columns in place so existing local indexes gain
            # the same auditable image binding without destructive migration.
            entity_columns = {row["name"] for row in self.db.conn.execute("PRAGMA table_info(knowledge_visual_entities)")}
            for column, definition in (
                ("source_boxes", "TEXT NOT NULL DEFAULT '[]'"),
                ("source_offset_start", "INTEGER"),
                ("source_offset_end", "INTEGER"),
                ("evidence_kind", "TEXT NOT NULL DEFAULT 'source_bound'"),
            ):
                if column not in entity_columns:
                    self.db.conn.execute(f"ALTER TABLE knowledge_visual_entities ADD COLUMN {column} {definition}")
            relation_columns = {row["name"] for row in self.db.conn.execute("PRAGMA table_info(knowledge_visual_relations)")}
            if "evidence_kind" not in relation_columns:
                self.db.conn.execute("ALTER TABLE knowledge_visual_relations ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT 'evidence_only'")
            self.db.conn.execute("COMMIT")
        self.root = service.settings.data_dir / "knowledge"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.parser = DocumentParser()
        self.embeddings = LocalEmbeddings(self.root)
        self.vision = VisionRuntime(self.root)
        self.index_lock = threading.RLock()
        # Only an explicitly prepared, source-bound index may be injected.
        # Normal service startup never imports HippoRAG or builds its index.
        self.hipporag_backend = None
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
        visual = self.vision.status()
        return {"mode": "hybrid", "available_modes": list(self.service.policy.retrieval_modes()),
                "lexical": "BM25", "vector_model": self.embeddings.name,
                "semantic": self.embeddings.name != "local-lexical-v1", "execution": "server-local",
                "visual_embedding": {"status": visual["visual_embedding"], "model": visual["model"] if visual["visual_embedding"] == "ready" else None,
                                     "dimensions": visual["embedding_dimensions"], "scope": "page-or-region", "local_only": True},
                "visual_ocr": {"status": visual["ocr"], "model": "rapidocr-onnxruntime" if visual["ocr"] == "ready" else None,
                               "entity_extraction": visual["entity_extraction"], "relation_extraction": visual["relation_extraction"]},
                "device": visual["device"], "cuda_devices": visual["cuda_devices"]}

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
        result = {key: row[key] for key in ("id", "kb_id", "name", "mime_type", "version", "sha256", "size", "status", "error", "page_count", "chunk_count", "embedding_model", "created_at")} | {
            "extraction": json.loads(row["extraction"]), "content_url": f"/api/v1/knowledge-documents/{row['id']}/content?version={row['version']}"}
        asset = self.db.one("SELECT * FROM knowledge_image_assets WHERE document_id=? AND version=?", (row["id"], row["version"]))
        if asset:
            region_count = self.db.one("SELECT COUNT(*) AS n FROM knowledge_image_regions WHERE document_id=? AND version=?", (row["id"], row["version"]))["n"]
            result["image"] = {
                "width": asset["width"], "height": asset["height"], "bytes": asset["bytes"],
                "thumbnail_width": asset["thumbnail_width"], "thumbnail_height": asset["thumbnail_height"],
                "thumbnail_url": f"/api/v1/knowledge-documents/{row['id']}/thumbnail?version={row['version']}",
                "region_count": region_count,
                "regions_url": f"/api/v1/knowledge-documents/{row['id']}/regions?version={row['version']}",
                "metadata": json.loads(asset["metadata_json"]),
                "ocr_status": asset["ocr_status"],
                "visual_index_status": asset["visual_index_status"],
                "visual_embedding": self.db.one("SELECT model,dimensions,device FROM knowledge_visual_embeddings WHERE document_id=? AND version=? AND region_id=''", (row["id"], row["version"])) or None,
                "entity_count": self.db.one("SELECT COUNT(*) AS n FROM knowledge_visual_entities WHERE document_id=? AND version=?", (row["id"], row["version"]))["n"],
                "evidence_boundary": "图片缩略图与实体候选绑定到文件版本；OCR实体有文字框证据，模型实体仅是候选，关系统一标记 evidence_only，不等同于人工金标准。",
            }
        previews = self.db.all("SELECT page,width,height FROM knowledge_page_previews WHERE document_id=? AND version=? ORDER BY page LIMIT 12", (row["id"], row["version"]))
        if previews:
            result["page_previews"] = [{"page": item["page"], "width": item["width"], "height": item["height"], "thumbnail_url": f"/api/v1/knowledge-documents/{row['id']}/thumbnail?version={row['version']}&page={item['page']}"} for item in previews]
        return result

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
            if mime.startswith("image/"):
                self._prepare_image_asset(document_id, version, path, mime, len(data))
            elif mime == "application/pdf":
                self._prepare_pdf_previews(document_id, version, path)
            self._index(document_id, version, path, name)
            self.db.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), kb_id))
            return self.document_view(self.version(document_id, version, user))

    def _prepare_image_asset(self, document_id, version, path, mime_type, size):
        """Create a bounded local thumbnail and metadata without a vision model.

        This deliberately does not infer labels, objects, or claims from pixels.
        Such fields remain unavailable until an approved local visual model exists.
        """
        try:
            from PIL import Image, ImageOps
            with Image.open(path) as source:
                source.load()
                image = ImageOps.exif_transpose(source).convert("RGB")
                width, height = image.size
                if width < 1 or height < 1 or width > 20000 or height > 20000:
                    raise ValueError("图片尺寸超出限制")
                thumbnail = image.copy()
                # Pillow 9 exposes the resampling constants at Image.LANCZOS;
                # newer releases move them under Image.Resampling.
                lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                thumbnail.thumbnail((640, 480), lanczos)
                thumbnail_path = path.with_name(f"{path.stem}.thumb.jpg")
                thumbnail.save(thumbnail_path, format="JPEG", quality=78, optimize=True)
                thumbnail_path.chmod(0o400)
                metadata = {"format": source.format or mime_type.split("/", 1)[-1], "mode": source.mode, "orientation_normalized": True}
                self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_image_assets(document_id,version,mime_type,width,height,thumbnail_path,thumbnail_width,thumbnail_height,bytes,metadata_json,ocr_status,visual_index_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,'needs_model',?)",
                    (document_id, version, mime_type, width, height, str(thumbnail_path), thumbnail.width, thumbnail.height, size, dump(metadata), "pending", now()),
                )
                self._prepare_image_regions(document_id, version, path, image)
        except Exception as exc:
            # Keep the upload/version so the regular parser reports the exact
            # OCR or invalid-image state instead of making metadata mandatory.
            self.db.execute("DELETE FROM knowledge_image_regions WHERE document_id=? AND version=?", (document_id, version))
            self.db.execute(
                "INSERT OR REPLACE INTO knowledge_image_assets(document_id,version,mime_type,width,height,bytes,metadata_json,ocr_status,visual_index_status,created_at) VALUES(?,?,?,?,?,?,'{}','pending','needs_model',?)",
                (document_id, version, mime_type, 0, 0, size, now()),
            )

    def _prepare_image_regions(self, document_id, version, path, image):
        """Persist a bounded deterministic grid of safe preview regions.

        This is layout segmentation only.  A region has coordinates and a
        compressed thumbnail, but no inferred label, object, embedding or
        relationship.  Those fields stay ``needs_model`` until an approved
        visual model is configured.
        """
        width, height = image.size
        columns = max(1, min(MAX_IMAGE_REGION_COLUMNS, math.ceil(width / 640)))
        rows = max(1, min(MAX_IMAGE_REGION_ROWS, math.ceil(height / 480)))
        while rows * columns > MAX_IMAGE_REGIONS:
            if columns >= rows and columns > 1:
                columns -= 1
            elif rows > 1:
                rows -= 1
            else:
                break
        self.db.execute("DELETE FROM knowledge_image_regions WHERE document_id=? AND version=?", (document_id, version))
        from PIL import Image
        lanczos = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        for grid_row in range(rows):
            y0, y1 = (height * grid_row) // rows, (height * (grid_row + 1)) // rows
            for grid_col in range(columns):
                x0, x1 = (width * grid_col) // columns, (width * (grid_col + 1)) // columns
                region_width, region_height = x1 - x0, y1 - y0
                if region_width < 1 or region_height < 1:
                    continue
                region = image.crop((x0, y0, x1, y1))
                region.thumbnail(IMAGE_REGION_THUMBNAIL, lanczos)
                target = path.with_name(f"{path.stem}.r{grid_row + 1}c{grid_col + 1}.thumb.jpg")
                region.save(target, format="JPEG", quality=76, optimize=True)
                target.chmod(0o400)
                region_id = hashlib.sha256(f"{document_id}:{version}:{grid_row}:{grid_col}".encode()).hexdigest()[:32]
                self.db.execute(
                    "INSERT INTO knowledge_image_regions(id,document_id,version,grid_row,grid_col,grid_rows,grid_cols,x,y,width,height,thumbnail_path,thumbnail_width,thumbnail_height,visual_index_status,entity_index_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (region_id, document_id, version, grid_row, grid_col, rows, columns, x0, y0, region_width, region_height,
                     str(target), region.width, region.height, "needs_model", "needs_model", now()),
                )

    def image_asset(self, document_id, version, user):
        row = self.version(document_id, version, user)
        asset = self.db.one("SELECT * FROM knowledge_image_assets WHERE document_id=? AND version=?", (document_id, row["version"]))
        if not asset:
            raise AppError(404, "NOT_FOUND", "此版本不是图片文件或尚未生成图片索引")
        return row, asset

    def _image_region_view(self, row):
        """Expose only bounded, source-linked region metadata to clients."""
        return {
            "id": row["id"], "document_id": row["document_id"], "version": row["version"],
            "row": row["grid_row"], "column": row["grid_col"], "rows": row["grid_rows"], "columns": row["grid_cols"],
            "x": row["x"], "y": row["y"], "width": row["width"], "height": row["height"],
            "thumbnail_width": row["thumbnail_width"], "thumbnail_height": row["thumbnail_height"],
            "thumbnail_url": f"/api/v1/knowledge-documents/{row['document_id']}/regions/{row['id']}/thumbnail?version={row['version']}",
            "visual_index_status": row["visual_index_status"],
            "entity_index_status": row["entity_index_status"],
                "evidence_boundary": "区域坐标和缩略图绑定到文件版本；OCR实体有文字框证据，模型实体仅是候选，关系统一标记 evidence_only。",
        }

    @staticmethod
    def _visual_entity_view(row):
        """Return an entity with direct image/region links for graph clients."""
        item = dict(row)
        try:
            item["source_boxes"] = json.loads(item.get("source_boxes") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            item["source_boxes"] = []
        item["thumbnail_url"] = f"/api/v1/knowledge-documents/{item['document_id']}/thumbnail?version={item['version']}"
        item["region_thumbnail_url"] = (
            f"/api/v1/knowledge-documents/{item['document_id']}/regions/{item['region_id']}/thumbnail?version={item['version']}"
            if item.get("region_id") else None
        )
        return item

    def image_regions(self, document_id, version, user, limit=MAX_IMAGE_REGIONS):
        """Return current or historical regions only after version ACL checks."""
        row, asset = self.image_asset(document_id, version, user)
        limit = max(1, min(MAX_IMAGE_REGIONS, int(limit)))
        regions = self.db.all(
            "SELECT * FROM knowledge_image_regions WHERE document_id=? AND version=? ORDER BY grid_row,grid_col LIMIT ?",
            (document_id, row["version"], limit),
        )
        return {
            "document_id": document_id, "version": row["version"], "name": row["name"],
            "width": asset["width"], "height": asset["height"], "region_count": len(regions),
            "grid": {
                "rows": regions[0]["grid_rows"] if regions else 0,
                "columns": regions[0]["grid_cols"] if regions else 0,
                "max_regions": MAX_IMAGE_REGIONS,
            },
            "regions": [self._image_region_view(item) for item in regions],
            "visual_embedding": self.vision.status()["visual_embedding"],
            "entity_extraction": self.vision.status()["entity_extraction"],
            "relation_extraction": self.vision.status()["relation_extraction"],
            "evidence_boundary": "区域来自有界网格裁剪，坐标和缩略图绑定到文件版本并经过访问控制；OCR实体有文字框证据，模型实体仅是候选，关系统一标记 evidence_only。",
        }

    def image_region_thumbnail(self, document_id, region_id, version, user):
        row, _ = self.image_asset(document_id, version, user)
        region = self.db.one(
            "SELECT * FROM knowledge_image_regions WHERE id=? AND document_id=? AND version=?",
            (region_id, document_id, row["version"]),
        )
        if not region:
            raise AppError(404, "NOT_FOUND", "图片区域不存在或不属于请求的文件版本")
        path = Path(region["thumbnail_path"])
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()) or not path.is_file():
            raise AppError(404, "NOT_FOUND", "图片区域缩略图不可用")
        return path, row

    def _prepare_pdf_previews(self, document_id, version, path):
        """Render a bounded set of page thumbnails locally for visual review.

        This is a preview artifact only. It does not create visual embeddings or
        infer figures/entities from pixels.
        """
        try:
            import pypdfium2 as pdfium
            from PIL import Image
            with pdfium.PdfDocument(path) as document:
                for index in range(min(len(document), 12)):
                    page = document[index]
                    try:
                        width, height = page.get_width(), page.get_height()
                        scale = min(1.4, 640 / max(width, 1), 480 / max(height, 1))
                        bitmap = page.render(scale=max(scale, 0.1))
                        try:
                            image = bitmap.to_pil().convert("RGB")
                        finally:
                            bitmap.close()
                        target = path.with_name(f"{path.stem}.p{index + 1}.thumb.jpg")
                        image.save(target, format="JPEG", quality=76, optimize=True)
                        target.chmod(0o400)
                        self.db.execute("INSERT OR REPLACE INTO knowledge_page_previews(document_id,version,page,thumbnail_path,width,height,created_at) VALUES(?,?,?,?,?,?,?)", (document_id, version, index + 1, str(target), image.width, image.height, now()))
                    finally:
                        page.close()
        except Exception:
            # PDF parsing/indexing reports its own status. Preview generation is
            # optional and must not make an otherwise searchable PDF fail.
            return

    @staticmethod
    def _visual_entity_candidates(text):
        """Extract explainable typed candidates from OCR text.

        This intentionally mirrors the text graph extractor.  OCR output is
        noisy, so candidates are labelled ``ocr_entity`` and their source text
        is retained; no semantic relation is claimed without an approved model.
        """
        candidates, seen = [], set()
        for kind, pattern in GRAPH_ENTITY_PATTERNS:
            for match in pattern.finditer(text or ""):
                label = match.group(0).strip(" _-")
                normalized = label.casefold().strip(" _-")
                if len(normalized) < 2 or normalized in GRAPH_STOPWORDS or normalized in seen:
                    continue
                seen.add(normalized)
                candidates.append({"label": label, "normalized": normalized, "kind": kind,
                                   "offset": match.start(), "confidence": 0.55})
                if len(candidates) >= 32:
                    return candidates
        return candidates

    @staticmethod
    def _bounded_confidence(value, default=0.5):
        """Clamp model/OCR confidence, rejecting NaN and infinity."""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return max(0.0, min(1.0, value)) if math.isfinite(value) else default

    @staticmethod
    def _entity_source_boxes(text, boxes, start, end):
        """Bind an OCR entity span back to the OCR boxes that contain it.

        OCR rows are stored as a newline-joined string, so the offset is
        relative to that exact source string.  The returned payload contains
        only bounded coordinates/text and can be rendered as a clickable
        highlight on the source image.
        """
        if not isinstance(text, str) or not isinstance(boxes, list):
            return []
        cursor, selected = 0, []
        for item in boxes[:256]:
            if not isinstance(item, dict):
                continue
            label = str(item.get("text") or "")
            if not label:
                continue
            item_start, item_end = cursor, cursor + len(label)
            if item_end > start and item_start < end:
                polygon = item.get("polygon") or item.get("box")
                if isinstance(polygon, list) and len(polygon) <= 16:
                    selected.append({
                        "text": label[:120],
                        "polygon": polygon,
                        "confidence": KnowledgeService._bounded_confidence(item.get("confidence"), 0.0),
                    })
                if len(selected) >= 8:
                    break
            cursor = item_end + 1  # rows are joined with a single newline
        return selected

    def _index_visual_asset(self, document_id, version, path):
        """Index local image embeddings, OCR boxes and bounded entity links.

        Every artifact is pinned to the immutable source version.  A missing
        visual model is a visible ``needs_model`` state, not a fake vector.
        """
        asset = self.db.one("SELECT * FROM knowledge_image_assets WHERE document_id=? AND version=?", (document_id, version))
        if not asset:
            return
        regions = self.db.all("SELECT * FROM knowledge_image_regions WHERE document_id=? AND version=? ORDER BY grid_row,grid_col", (document_id, version))
        self.db.execute("DELETE FROM knowledge_visual_embeddings WHERE document_id=? AND version=?", (document_id, version))
        self.db.execute("DELETE FROM knowledge_visual_ocr WHERE document_id=? AND version=?", (document_id, version))
        self.db.execute("DELETE FROM knowledge_visual_entities WHERE document_id=? AND version=?", (document_id, version))
        self.db.execute("DELETE FROM knowledge_visual_relations WHERE document_id=? AND version=?", (document_id, version))
        self.db.execute("UPDATE knowledge_image_assets SET ocr_status='pending',visual_index_status='needs_model' WHERE document_id=? AND version=?", (document_id, version))
        self.db.execute("UPDATE knowledge_image_regions SET visual_index_status='needs_model',entity_index_status='needs_model' WHERE document_id=? AND version=?", (document_id, version))

        # OCR and vector model failures are independent.  A model can produce
        # a visual vector even when the OCR runtime is unavailable, and vice
        # versa, without conflating the two acceptance signals.
        try:
            ocr = self.vision.ocr_image(path)
        except Exception:
            ocr = None
        if ocr:
            self.db.execute(
                "INSERT OR REPLACE INTO knowledge_visual_ocr(document_id,version,region_id,model,text,boxes,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (document_id, version, "", ocr["model"], ocr["text"], dump(ocr["boxes"]), "ready", now()),
            )
            self.db.execute("UPDATE knowledge_image_assets SET ocr_status='ready' WHERE document_id=? AND version=?", (document_id, version))
            for region in regions:
                selected = [box for box in ocr["boxes"] if self.vision.region_for_box(box, [dict(region)]) == region["id"]]
                text = "\n".join(box["text"] for box in selected)
                self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_visual_ocr(document_id,version,region_id,model,text,boxes,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (document_id, version, region["id"], ocr["model"], text, dump(selected), "ready", now()),
                )
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO knowledge_visual_ocr(document_id,version,region_id,model,text,boxes,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (document_id, version, "", "", "", "[]", "needs_ocr", now()),
            )
            self.db.execute("UPDATE knowledge_image_assets SET ocr_status='needs_ocr' WHERE document_id=? AND version=?", (document_id, version))

        try:
            full_vector = self.vision.embed_image(path)
        except Exception:
            full_vector = None
        if full_vector:
            self.db.execute(
                "INSERT OR REPLACE INTO knowledge_visual_embeddings(document_id,version,region_id,model,dimensions,vector,device,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (document_id, version, "", full_vector["model"], full_vector["dimensions"], dump(full_vector["vector"]), full_vector["device"], now()),
            )
            self.db.execute("UPDATE knowledge_image_assets SET visual_index_status='ready' WHERE document_id=? AND version=?", (document_id, version))
            for region in regions:
                region_path = Path(region["thumbnail_path"])
                if not region_path.is_file() or not region_path.resolve().is_relative_to(self.root.resolve()):
                    continue
                try:
                    vector = self.vision.embed_image(region_path)
                except Exception:
                    vector = None
                if vector:
                    self.db.execute(
                        "INSERT OR REPLACE INTO knowledge_visual_embeddings(document_id,version,region_id,model,dimensions,vector,device,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (document_id, version, region["id"], vector["model"], vector["dimensions"], dump(vector["vector"]), vector["device"], now()),
                    )
                    self.db.execute("UPDATE knowledge_image_regions SET visual_index_status='ready' WHERE id=?", (region["id"],))

        # OCR text is the only source for deterministic image entities.  Each
        # entity stores the region and text span; relations stay explicitly
        # co-occurrence links until a relation model is authorized.
        entity_ids_by_region = {}
        for row in self.db.all("SELECT * FROM knowledge_visual_ocr WHERE document_id=? AND version=? AND text<>''", (document_id, version)):
            if not row["region_id"]:
                continue
            candidates = self._visual_entity_candidates(row["text"])
            try:
                ocr_boxes = json.loads(row["boxes"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                ocr_boxes = []
            entity_ids = []
            for candidate in candidates:
                entity_id = hashlib.sha256(f"{document_id}:{version}:{row['region_id']}:{candidate['normalized']}".encode()).hexdigest()[:32]
                entity_ids.append(entity_id)
                source_boxes = self._entity_source_boxes(row["text"], ocr_boxes, candidate["offset"], candidate["offset"] + len(candidate["label"]))
                self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_visual_entities(id,document_id,version,region_id,label,normalized,kind,confidence,source_text,source_boxes,source_offset_start,source_offset_end,evidence_kind,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entity_id, document_id, version, row["region_id"], candidate["label"], candidate["normalized"], "ocr_entity:" + candidate["kind"], candidate["confidence"], row["text"], dump(source_boxes), candidate["offset"], candidate["offset"] + len(candidate["label"]), "source_bound", now()),
                )
            entity_ids_by_region[row["region_id"]] = entity_ids
            if entity_ids:
                self.db.execute("UPDATE knowledge_image_regions SET entity_index_status='ready' WHERE id=?", (row["region_id"],))
            for source_id, target_id in zip(entity_ids, entity_ids[1:]):
                relation_id = hashlib.sha256(f"{document_id}:{version}:{source_id}:{target_id}:co_occurs".encode()).hexdigest()[:32]
                self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_visual_relations(id,document_id,version,source_entity_id,target_entity_id,relation,confidence,evidence,evidence_kind,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (relation_id, document_id, version, source_id, target_id, "co_occurs", 0.35, row["region_id"], "evidence_only", now()),
                )

        # TokenHub vision/entity/relation inference is an explicit second
        # stage. It is disabled by default so a normal upload never incurs a
        # provider call; when enabled, only the already-authorized image file
        # and a bounded schema prompt are sent. Model candidates remain
        # source-bound and never replace deterministic OCR evidence.
        self._index_provider_visual_candidates(document_id, version, path, regions)
        return bool(self.db.one("SELECT 1 AS ok FROM knowledge_visual_embeddings WHERE document_id=? AND version=? LIMIT 1", (document_id, version)))

    @staticmethod
    def _bounded_visual_label(value):
        if not isinstance(value, str):
            return ""
        value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
        return " ".join(value.split())[:120]

    def _provider_visual_selection(self):
        settings = self.service.settings
        if not getattr(settings, "knowledge_vision_enabled", False):
            return None
        public_id = str(getattr(settings, "knowledge_vision_model_id", "") or "").strip()
        if not public_id:
            return None
        try:
            selection = self.service.catalog.select(public_id)
            item = self.service.catalog.models.get(public_id, {})
            if "multimodal" not in item.get("capabilities", []):
                return None
            provider = self.service.effective_provider(selection)
        except Exception:
            return None
        token = provider.get("ANTHROPIC_AUTH_TOKEN") or provider.get("ANTHROPIC_API_KEY") or ""
        base_url = str(getattr(settings, "knowledge_vision_base_url", "") or "").strip()
        if not base_url or not token:
            return None
        route = classify_provider_url(base_url)
        if not (route.production_ready or (getattr(settings, "provider_test_mode", False) and route.allowed)):
            # Vision indexing must not silently call a direct provider URL.
            # A loopback adapter is permitted only as a locally controlled
            # bridge whose upstream remains TokenHub.
            return None
        return {"public_id": public_id, "upstream_model": selection["upstream_model"], "base_url": base_url,
                "token": token, "timeout": float(getattr(settings, "knowledge_vision_timeout_seconds", 30) or 30),
                "provider_route": route.kind}

    def _provider_visual_failure(self, document_id, version, selection, reason="provider_response_unusable"):
        """Persist a bounded provider failure without response text or secrets."""
        try:
            asset = self.db.one("SELECT metadata_json FROM knowledge_image_assets WHERE document_id=? AND version=?", (document_id, version))
            metadata = json.loads(asset["metadata_json"] or "{}") if asset else {}
            metadata["provider_visual"] = {"status": "failed", "model": selection["public_id"], "evidence_kind": "model_candidate", "failure": reason[:80]}
            self.db.execute("UPDATE knowledge_image_assets SET metadata_json=? WHERE document_id=? AND version=?", (dump(metadata), document_id, version))
        except Exception:
            return

    def _index_provider_visual_candidates(self, document_id, version, path, regions):
        selection = self._provider_visual_selection()
        if not selection:
            return
        try:
            raw = path.read_bytes()
            if len(raw) > MAX_IMAGE_BYTES:
                self._provider_visual_failure(document_id, version, selection, "image_too_large")
                return
            prompt = (
                "你是工业知识抽取器。只返回 JSON，不要 Markdown、推理过程或额外文字。"
                "格式必须是 {\"entities\":[{\"label\":\"实体\",\"kind\":\"equipment|standard|process|metric|material|organization|other\",\"confidence\":0.0}],"
                "\"relations\":[{\"source\":\"实体标签或实体序号\",\"target\":\"实体标签或实体序号\",\"relation\":\"关系\",\"confidence\":0.0}]}。"
                "只记录图中可直接观察或文字明确支持的工业实体；不确定就省略。"
            )
            suffix = path.suffix.lower().lstrip(".")
            mime_type = "image/jpeg" if suffix in {"jpg", "jpeg"} else "image/" + (suffix or "jpeg")
            result = asyncio.run(call_visual_json(
                base_url=selection["base_url"], model=selection["upstream_model"], image=raw,
                mime_type=mime_type, prompt=prompt,
                timeout_seconds=selection["timeout"], auth_token=selection["token"],
            ))
            payload = result.get("json") if isinstance(result, dict) else None
            if not isinstance(payload, dict):
                self._provider_visual_failure(document_id, version, selection)
                return
            entities = payload.get("entities") if isinstance(payload.get("entities"), list) else []
            relations = payload.get("relations") if isinstance(payload.get("relations"), list) else []
            entity_ids, entity_labels = {}, []
            allowed_regions = {str(region["id"]) for region in regions}
            for index, item in enumerate(entities[:MAX_MODEL_VISUAL_ENTITIES]):
                if not isinstance(item, dict):
                    continue
                label = self._bounded_visual_label(item.get("label"))
                if len(label) < 2:
                    continue
                normalized = label.casefold().strip(" _-")
                if normalized in entity_ids:
                    continue
                kind = self._bounded_visual_label(item.get("kind") or "other").casefold()
                if kind not in {"equipment", "standard", "process", "metric", "material", "organization", "other"}:
                    kind = "other"
                confidence = self._bounded_confidence(item.get("confidence", 0.5))
                region_id = str(item.get("region_id") or "")
                if region_id not in allowed_regions:
                    region_id = ""
                entity_id = hashlib.sha256(f"{document_id}:{version}:provider:{normalized}".encode()).hexdigest()[:32]
                entity_ids[normalized] = entity_id
                entity_labels.append((label, entity_id))
                self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_visual_entities(id,document_id,version,region_id,label,normalized,kind,confidence,source_text,source_boxes,source_offset_start,source_offset_end,evidence_kind,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (entity_id, document_id, version, region_id, label, normalized, "model_entity:" + kind, confidence, "provider_visual_candidate", "[]", None, None, "model_candidate", now()),
                )
            def resolve(value):
                if isinstance(value, int) and 0 <= value < len(entity_labels):
                    return entity_labels[value][1]
                label = self._bounded_visual_label(value).casefold().strip(" _-")
                return entity_ids.get(label)
            for item in relations[:MAX_MODEL_VISUAL_RELATIONS]:
                if not isinstance(item, dict):
                    continue
                source_id, target_id = resolve(item.get("source")), resolve(item.get("target"))
                relation = self._bounded_visual_label(item.get("relation") or "related_to")
                if not source_id or not target_id or not relation or source_id == target_id:
                    continue
                confidence = self._bounded_confidence(item.get("confidence", 0.5))
                relation_id = hashlib.sha256(f"{document_id}:{version}:provider:{source_id}:{target_id}:{relation.casefold()}".encode()).hexdigest()[:32]
                self.db.execute(
                    "INSERT OR REPLACE INTO knowledge_visual_relations(id,document_id,version,source_entity_id,target_entity_id,relation,confidence,evidence,evidence_kind,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (relation_id, document_id, version, source_id, target_id, relation, confidence, "provider_visual_candidate", "evidence_only", now()),
                )
            # Persist only bounded audit metadata; never raw model text, image
            # bytes, request headers or credentials.
            asset = self.db.one("SELECT metadata_json FROM knowledge_image_assets WHERE document_id=? AND version=?", (document_id, version))
            metadata = json.loads(asset["metadata_json"] or "{}") if asset else {}
            relation_count = self.db.one("SELECT COUNT(*) AS n FROM knowledge_visual_relations WHERE document_id=? AND version=? AND evidence='provider_visual_candidate'", (document_id, version))["n"]
            metadata["provider_visual"] = {"status": "ready" if entity_ids else "empty", "model": selection["public_id"],
                                            "entity_count": len(entity_ids), "relation_count": relation_count,
                                            "latency_ms": result.get("latency_ms") if isinstance(result.get("latency_ms"), (int, float)) else None,
                                            "response_sha256": result.get("response_sha256") if isinstance(result.get("response_sha256"), str) else None,
                                            "usage": {key: value for key, value in (result.get("usage") or {}).items() if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and type(value) is int and value >= 0},
                                            "evidence_kind": "model_candidate"}
            self.db.execute("UPDATE knowledge_image_assets SET metadata_json=? WHERE document_id=? AND version=?", (dump(metadata), document_id, version))
        except Exception:
            # Provider inference is additive and fail-closed. Local OCR/CLIP
            # evidence remains valid when a route times out or returns invalid
            # JSON; the failed provider call is not presented as success.
            self._provider_visual_failure(document_id, version, selection)
            return

    def _index(self, document_id, version, path, name):
        try:
            try:
                pages = self.parser.parse(path, name)
            except AppError as exc:
                # Local OCR remains the default.  The official OCR capability
                # is an explicit, bounded fallback for scanned files only; it
                # never runs when disabled or when no separate key is present.
                if exc.code != "OCR_REQUIRED":
                    raise
                pages = self._deepseek_ocr_pages(path, name)
            chunks = chunk_pages(pages)
            is_image = Path(name).suffix.lower() in IMAGE_SUFFIXES
            # A diagram or photograph can be a valid knowledge asset even when
            # OCR finds no text. Keep the page/image evidence and let the
            # visual index provide the searchable artifact instead of marking
            # the upload as an empty failed document.
            if not chunks and not is_image:
                raise AppError(422, "EMPTY_DOCUMENT", "未找到可检索的文本片段")
            vectors = self.embeddings.embed([chunk["text"] for chunk in chunks]) if chunks else []
            with self.db.transaction():
                self.db.execute("DELETE FROM knowledge_pages WHERE document_id=? AND version=?", (document_id, version))
                self.db.execute("DELETE FROM knowledge_chunks WHERE document_id=? AND version=?", (document_id, version))
                for page in pages:
                    self.db.execute("INSERT INTO knowledge_pages VALUES(?,?,?,?,?)", (document_id, version, page["page"], page["text"], page["extraction"]))
                for chunk, vector in zip(chunks, vectors):
                    self.db.execute("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (chunk["id"], document_id, version, chunk["page"], chunk["section"], chunk["paragraph"], chunk["start"], chunk["end"], chunk["text"], dump(terms(chunk["text"])), dump(vector), self.embeddings.name))
                self._build_graph(document_id, version, chunks)
                self.db.execute("UPDATE knowledge_versions SET status='ready',error=NULL,page_count=?,chunk_count=?,extraction=?,embedding_model=? WHERE document_id=? AND version=?", (len(pages), len(chunks), dump(sorted({page["extraction"] for page in pages})), self.embeddings.name, document_id, version))
                if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                    self.db.execute("UPDATE knowledge_image_assets SET ocr_status='ready' WHERE document_id=? AND version=?", (document_id, version))
            if Path(name).suffix.lower() in IMAGE_SUFFIXES:
                try:
                    self._index_visual_asset(document_id, version, path)
                except Exception:
                    # Visual enrichment is additive.  A model/runtime failure
                    # must not invalidate a successfully OCR-indexed document.
                    self.db.execute("UPDATE knowledge_image_assets SET visual_index_status='needs_model' WHERE document_id=? AND version=?", (document_id, version))
        except Exception as exc:
            message = exc.message if isinstance(exc, AppError) else "文档解析或索引失败，请检查格式后重试"
            self.db.execute("UPDATE knowledge_versions SET status=?,error=? WHERE document_id=? AND version=?", ("needs_ocr" if isinstance(exc, AppError) and exc.code == "OCR_REQUIRED" else "failed", message, document_id, version))
            if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                self.db.execute("UPDATE knowledge_image_assets SET ocr_status=? WHERE document_id=? AND version=?", ("needs_ocr" if isinstance(exc, AppError) and exc.code == "OCR_REQUIRED" else "failed", document_id, version))

    def _deepseek_ocr_pages(self, path, name):
        """Run the opt-in external OCR fallback and return parser-compatible pages."""
        settings = self.service.settings
        if not (getattr(settings, "deepseek_api_enabled", False)
                and getattr(settings, "deepseek_api_ocr_enabled", False)):
            raise AppError(422, "OCR_REQUIRED", "文件需要安装本地 OCR 组件后重新索引")
        token = str(getattr(settings, "deepseek_api_key", "") or "").strip()
        if not token:
            raise AppError(422, "OCR_REQUIRED", "文件需要安装本地 OCR 组件后重新索引")
        client = DeepSeekAPI(
            base_url=str(getattr(settings, "deepseek_api_base_url", "") or ""),
            api_key=token,
            timeout_seconds=float(getattr(settings, "deepseek_api_timeout_seconds", 30) or 30),
        )
        try:
            result = asyncio.run(client.ocr(path, filename=name, purpose="rich_layout_recognition"))
        except RuntimeError as exc:
            # ``_index`` normally runs in a worker thread.  If a caller invokes
            # it from an active event loop, fail closed instead of nesting one.
            if "asyncio.run() cannot be called" in str(exc):
                raise AppError(503, "OCR_UNAVAILABLE", "外部识别服务暂时不可用") from exc
            raise
        pages = [{"page": item["page"], "text": item["text"], "extraction": item["extraction"]}
                 for item in result.get("pages", []) if isinstance(item, dict)]
        if not pages or not any(str(item.get("text") or "").strip() for item in pages):
            raise AppError(422, "EMPTY_DOCUMENT", "外部识别未提取到可检索文本")
        return pages

    def _build_graph(self, document_id, version, chunks):
        """Build a deterministic, source-linked graph with explainable entities.

        Recognized entities carry a coarse ``kind`` (standard, equipment,
        metric, material, process or organization) and a precise text offset.
        Remaining terms are retained as ``term`` nodes for backwards
        compatibility.  Edges intentionally remain co-occurrence edges until
        an approved relation-extraction model is available.
        """
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
            # Keep the first occurrence for each normalized entity in a chunk;
            # its offset is what makes graph evidence auditable in the source.
            entities = []
            seen = set()
            for kind, pattern in GRAPH_ENTITY_PATTERNS:
                for match in pattern.finditer(chunk["text"]):
                    value = match.group(0).strip(" _-")
                    normalized = value.casefold().strip(" _-")
                    if len(normalized) < 2 or normalized in GRAPH_STOPWORDS or normalized in seen:
                        continue
                    seen.add(normalized)
                    entities.append({"label": normalized, "kind": kind, "offset": match.start()})
                    if len(entities) >= 8:
                        break
                if len(entities) >= 8:
                    break
            # A small lexical fallback keeps ordinary Chinese headings and
            # identifiers discoverable when no typed pattern matched.
            if len(entities) < 8:
                for match in re.finditer(r"[\u3400-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,30}", chunk["text"]):
                    value = match.group(0)
                    normalized = value.casefold().strip(" _-")
                    if len(normalized) < 2 or normalized in GRAPH_STOPWORDS or normalized in seen:
                        continue
                    seen.add(normalized)
                    entities.append({"label": normalized, "kind": "term" if normalized.isascii() else "entity", "offset": match.start()})
                    if len(entities) >= 8:
                        break
            if not entities:
                continue
            source = (chunk["page"], chunk["paragraph"], chunk["start"], chunk["end"])
            node_ids = []
            for entity in entities:
                normalized = entity["label"]
                node_id = hashlib.sha256(f"{kb_id}:{document_id}:{version}:{normalized}".encode()).hexdigest()[:32]
                node_ids.append(node_id)
                # Point to the matched span while retaining the chunk bounds
                # in the surrounding evidence record.  Offsets are relative
                # to the original extracted page text.
                start = chunk["start"] + entity["offset"]
                end = start + len(normalized)
                nodes.setdefault(node_id, {"id": node_id, "label": normalized, "normalized": normalized, "kind": entity["kind"], "source": (chunk["page"], chunk["paragraph"], start, end)})
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

    def _graph_quality(self, kb_id):
        """Read bounded quality metadata for a curated graph import.

        The receipt is written by the project-local seeding job after it has
        replaced rule-only candidates with source-bound, manually reviewed
        nodes and edges. It is metadata only; graph access still goes through
        the normal knowledge-base permission checks.
        """
        path = self.root / kb_id / "graph-quality.json"
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()) or path.stat().st_size > 64 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None

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
        image_assets = self.db.all("""SELECT a.document_id,a.version,a.width,a.height,a.thumbnail_width,a.thumbnail_height,
            a.ocr_status,a.visual_index_status,d.name FROM knowledge_image_assets a
            JOIN knowledge_documents d ON d.id=a.document_id AND d.current_version=a.version AND d.deleted=0
            JOIN knowledge_versions v ON v.document_id=a.document_id AND v.version=a.version
            WHERE d.kb_id=? ORDER BY d.name LIMIT 64""", (kb_id,))
        images = []
        for item in image_assets:
            region_rows = self.db.all(
                "SELECT * FROM knowledge_image_regions WHERE document_id=? AND version=? ORDER BY grid_row,grid_col LIMIT ?",
                (item["document_id"], item["version"], MAX_IMAGE_REGIONS),
            )
            images.append({
                "document_id": item["document_id"], "version": item["version"], "name": item["name"],
                "width": item["width"], "height": item["height"], "thumbnail_width": item["thumbnail_width"],
                "thumbnail_height": item["thumbnail_height"], "ocr_status": item["ocr_status"],
                "visual_index_status": item["visual_index_status"], "region_count": len(region_rows),
                "regions_url": f"/api/v1/knowledge-documents/{item['document_id']}/regions?version={item['version']}",
                "regions": [self._image_region_view(region) for region in region_rows],
            })
        visual_entities = self.db.all(
            "SELECT id,document_id,version,region_id,label,normalized,kind,confidence,source_text,source_boxes,source_offset_start,source_offset_end,evidence_kind FROM knowledge_visual_entities WHERE document_id IN (SELECT id FROM knowledge_documents WHERE kb_id=? AND deleted=0) AND version=(SELECT current_version FROM knowledge_documents WHERE id=knowledge_visual_entities.document_id) ORDER BY document_id,region_id,label LIMIT 400",
            (kb_id,),
        )
        visual_relations = self.db.all(
            "SELECT id,document_id,version,source_entity_id,target_entity_id,relation,confidence,evidence,evidence_kind FROM knowledge_visual_relations WHERE document_id IN (SELECT id FROM knowledge_documents WHERE kb_id=? AND deleted=0) AND version=(SELECT current_version FROM knowledge_documents WHERE id=knowledge_visual_relations.document_id) ORDER BY id LIMIT 800",
            (kb_id,),
        )
        return {"nodes": [{"id": row["id"], "label": row["label"], "kind": row["kind"], "source": source(row)} for row in nodes],
                "edges": [{"id": row["id"], "source": row["source_node_id"], "target": row["target_node_id"], "relation": row["relation"], "provenance": source(row)} for row in edges],
                "versioned": True, "source": "local-extraction", "query": query or "",
                "quality": self._graph_quality(kb_id),
                "multimodal": {
                    "images": images,
                    "visual_embedding": self.vision.status()["visual_embedding"],
                    # Keep the historical field stable for existing clients;
                    # the richer mode is exposed alongside it.
                    "entity_extraction": "ocr_only",
                    "entity_extraction_mode": self.vision.status()["entity_extraction"],
                    "relation_extraction": self.vision.status()["relation_extraction"],
                    "visual_entities": [self._visual_entity_view(item) for item in visual_entities],
                    "visual_relations": [{"id": item["id"], "document_id": item["document_id"], "version": item["version"], "source": item["source_entity_id"], "target": item["target_entity_id"], "relation": item["relation"], "confidence": item["confidence"], "evidence": item["evidence"], "evidence_kind": item["evidence_kind"]} for item in visual_relations],
                    "region_segmentation": "bounded_grid",
                    "evidence_boundary": "图谱节点和边来自文本、表格或本地 OCR；图片 OCR实体保留文字框、区域和版本证据，模型实体仅为候选，关系统一标记 evidence_only。",
                }}

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

    def _hipporag_graph_scores(self, kb_ids, query, rows, run_id=None):
        """Return bounded, source-linked HippoRAG2-lite scores.

        This is deliberately a local deterministic approximation of HippoRAG2:
        query entities seed a two-hop graph neighborhood, eight PageRank
        iterations propagate relevance, and node scores are projected back to
        the immutable chunks that provided the node evidence.  It never calls a
        model and it fails closed when a graph or seed is unavailable.
        """
        trace = {
            "algorithm": "hipporag2-lite", "damping": 0.85, "iterations": 8,
            "max_hops": 2, "seed_nodes": [], "seed_count": 0,
            "node_count": 0, "edge_count": 0, "visited_node_count": 0,
            "score_count": 0, "fallback": None,
        }
        if not rows:
            trace["fallback"] = "no_retrieval_chunks"
            return {}, trace
        placeholders = ",".join("?" for _ in kb_ids)
        if run_id:
            node_scope = "EXISTS (SELECT 1 FROM knowledge_run_documents rd WHERE rd.run_id=? AND rd.document_id=n.document_id AND rd.version=n.version)"
            edge_scope = "EXISTS (SELECT 1 FROM knowledge_run_documents rd WHERE rd.run_id=? AND rd.document_id=e.document_id AND rd.version=e.version)"
            # The ``IN (...)`` placeholders appear before the snapshot EXISTS
            # predicate in each statement, so keep parameters in SQL order.
            node_values = [*kb_ids, run_id]
            edge_values = [*kb_ids, run_id]
        else:
            node_scope = "d.current_version=n.version"
            edge_scope = "d.current_version=e.version"
            node_values = list(kb_ids)
            edge_values = list(kb_ids)
        nodes = self.db.all(
            f"SELECT n.* FROM knowledge_graph_nodes n "
            f"JOIN knowledge_documents d ON d.id=n.document_id AND d.deleted=0 "
            f"JOIN knowledge_versions v ON v.document_id=n.document_id AND v.version=n.version AND v.status='ready' "
            f"WHERE n.kb_id IN ({placeholders}) AND {node_scope} "
            "ORDER BY n.id LIMIT 4000",
            node_values,
        )
        edges = self.db.all(
            f"SELECT e.* FROM knowledge_graph_edges e "
            f"JOIN knowledge_documents d ON d.id=e.document_id AND d.deleted=0 "
            f"JOIN knowledge_versions v ON v.document_id=e.document_id AND v.version=e.version AND v.status='ready' "
            f"WHERE e.kb_id IN ({placeholders}) AND {edge_scope} "
            "ORDER BY e.id LIMIT 10000",
            edge_values,
        )
        trace["node_count"], trace["edge_count"] = len(nodes), len(edges)
        if not nodes:
            trace["fallback"] = "no_graph_index"
            return {}, trace
        by_id = {node["id"]: node for node in nodes}
        adjacency = {node_id: set() for node_id in by_id}
        for edge in edges:
            source, target = edge["source_node_id"], edge["target_node_id"]
            if source in by_id and target in by_id and source != target:
                adjacency[source].add(target)
                adjacency[target].add(source)

        query_text = query.casefold()
        query_terms = terms(query)
        candidates = []
        for node in nodes:
            normalized = str(node["normalized"] or node["label"] or "").casefold().strip()
            if len(normalized) < 2:
                continue
            exact = normalized in query_text
            overlap = sum(1 for token in terms(normalized) if token in query_terms)
            if exact or overlap:
                candidates.append((0 if exact else 1, -overlap, normalized, node["id"]))
        candidates.sort()
        seeds = [item[3] for item in candidates[:32]]
        trace["seed_nodes"] = seeds
        trace["seed_count"] = len(seeds)
        if not seeds:
            trace["fallback"] = "no_query_entity_seed"
            return {}, trace

        # Restrict propagation to the seed two-hop neighborhood. This keeps
        # latency and the evidence surface bounded while retaining local paths.
        visited = set(seeds)
        frontier = set(seeds)
        for _ in range(2):
            frontier = {neighbor for node_id in frontier for neighbor in adjacency.get(node_id, ())} - visited
            visited.update(frontier)
        trace["visited_node_count"] = len(visited)
        if not visited:
            trace["fallback"] = "empty_graph_neighborhood"
            return {}, trace
        seed_prior = {node_id: (1.0 / len(seeds) if node_id in seeds else 0.0) for node_id in visited}
        rank = dict(seed_prior)
        damping = 0.85
        for _ in range(8):
            updated = {node_id: (1.0 - damping) * seed_prior[node_id] for node_id in visited}
            for source in sorted(visited):
                neighbors = sorted(adjacency.get(source, ()) & visited)
                if not neighbors:
                    updated[source] += damping * rank[source]
                    continue
                share = damping * rank[source] / len(neighbors)
                for target in neighbors:
                    updated[target] += share
            rank = updated

        # Map graph evidence to chunks. Direct page/paragraph matches retain
        # full score; same-document propagation is a small, explicit fallback
        # for a neighboring entity whose paragraph has no separate chunk.
        graph_scores = Counter()
        for node_id in sorted(visited):
            node, node_score = by_id[node_id], rank[node_id]
            for row in rows:
                if row["document_id"] != node["document_id"] or int(row["version"]) != int(node["version"]):
                    continue
                direct = (row["page"] == node["source_page"] and row["paragraph"] == node["source_paragraph"])
                if direct:
                    graph_scores[row["id"]] += node_score
                elif row["page"] == node["source_page"]:
                    graph_scores[row["id"]] += node_score * 0.25
        trace["score_count"] = len(graph_scores)
        if not graph_scores:
            trace["fallback"] = "graph_nodes_without_chunk_evidence"
        return dict(graph_scores), trace

    def _search_rows(self, kb_ids, run_id=None):
        placeholders = ",".join("?" for _ in kb_ids)
        version_clause = "EXISTS(SELECT 1 FROM knowledge_run_documents rd WHERE rd.run_id=? AND rd.document_id=c.document_id AND rd.version=c.version)" if run_id else "d.current_version=c.version"
        values = ([run_id] if run_id else []) + kb_ids
        return self.db.all(f"SELECT c.*,d.name,d.kb_id,v.sha256,v.created_at AS imported_at,k.name AS knowledge_base FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND {version_clause} JOIN knowledge_versions v ON v.document_id=c.document_id AND v.version=c.version JOIN knowledge_bases k ON k.id=d.kb_id WHERE d.kb_id IN ({placeholders}) AND d.deleted=0 AND v.status='ready' LIMIT 50000", values)

    def _official_search_user(self, kb_ids, user, run_id):
        user = self.service.require("users", user["id"])
        if not user["enabled"]:
            raise AppError(403, "USER_DISABLED", "账号已停用")
        for kb_id in kb_ids:
            if not self.require(kb_id, user)["enabled"]:
                raise AppError(403, "KNOWLEDGE_DISABLED", "知识库已停用")
        if run_id:
            run = self.service.require("runs", run_id, user)
            conversation = self.service.require("conversations", run["conversation_id"], user)
            if (run["status"] not in {"queued", "running"} or conversation["archived"]
                    or conversation["active_run_id"] != run_id):
                raise AppError(409, "KNOWLEDGE_RUN_CHANGED", "任务状态已变化，请重新检索")
            try:
                snapshot = json.loads(run.get("capability_snapshot") or "null")
            except (TypeError, ValueError):
                snapshot = None
            if not isinstance(snapshot, dict) or snapshot.get("id") != conversation["assistant_id"]:
                raise AppError(409, "KNOWLEDGE_SNAPSHOT_MISSING", "任务缺少有效的知识权限快照")
            if not set(kb_ids).issubset(self._assistant_ids(user, snapshot, run_id)):
                raise AppError(403, "KNOWLEDGE_SCOPE_CHANGED", "助手知识范围已变化，请重新检索")
        return user

    def _recheck_official_results(self, kb_ids, user, run_id, rows, results):
        with self.db.lock:
            user = self._official_search_user(kb_ids, user, run_id)
            if rows != self._search_rows(kb_ids, run_id):
                raise AppError(409, "KNOWLEDGE_SNAPSHOT_CHANGED", "检索期间知识版本或索引已变化，请重新检索")
            # Pure images need their own check: they need not have text chunks.
            for item in results:
                version = self.version(item["document_id"], item["version"], user)
                expected = version["current_version"]
                if run_id:
                    pinned = self.db.one("SELECT version FROM knowledge_run_documents WHERE run_id=? AND document_id=?", (run_id, item["document_id"]))
                    expected = pinned["version"] if pinned else None
                if (version["kb_id"] not in kb_ids or version["version"] != expected
                        or version["status"] != "ready" or version["sha256"] != item["sha256"]):
                    raise AppError(409, "KNOWLEDGE_SNAPSHOT_CHANGED", "检索期间知识版本或索引已变化，请重新检索")

    def _search(self, kb_ids, user, query, limit, run_id=None, mode="hybrid", context_chars=12000, cancel_event=None):
        policy = self.service.policy
        if policy.is_trial:
            policy.require_retrieval_mode(mode)
        elif mode not in {"hybrid", "bm25", "vector", "hipporag"}:
            raise AppError(422, "INVALID_RETRIEVAL_MODE", "不支持的检索模式")
        kb_ids = list(dict.fromkeys(kb_ids))[:100]
        for kb_id in kb_ids:
            if not self.require(kb_id, user)["enabled"]:
                raise AppError(403, "KNOWLEDGE_DISABLED", "知识库已停用")
        if not kb_ids:
            return {"results": [], "retrieval": {**self.retrieval_info(), "mode": mode, "algorithm": "hipporag2-lite" if mode == "hipporag" else mode, **({"backend": "lite"} if mode == "hipporag" else {})}, "context": {"text": "", "characters": 0, "truncated": False}}
        backend = self.hipporag_backend if mode == "hipporag" else None
        if backend is not None:
            user = self._official_search_user(kb_ids, user, run_id)
        rows = self._search_rows(kb_ids, run_id)
        if not rows:
            visual = self._visual_search(kb_ids, query, limit, run_id=run_id) if mode in {"hybrid", "vector", "hipporag"} else []
            trace = {"algorithm": "hipporag2-lite", "fallback": "no_text_chunks"} if mode == "hipporag" else None
            retrieval = {**self.retrieval_info(), "mode": mode, "algorithm": "hipporag2-lite" if mode == "hipporag" else mode}
            if trace:
                retrieval["graph_trace"] = trace
                retrieval["backend"] = "lite"
            if backend is not None:
                retrieval["fallback"] = "no_text_chunks"
                self._recheck_official_results(kb_ids, user, run_id, rows, visual)
            return {"results": visual, "retrieval": retrieval, "context": {"text": "", "characters": 0, "truncated": False}}
        official_scores, official_stats, official_fallback = None, None, None
        if backend is not None:
            from .hipporag import HippoRAGError
            knowledge = self

            class Cancellation:
                def is_set(self):
                    if cancel_event is not None and cancel_event.is_set():
                        return True
                    try:
                        knowledge._official_search_user(kb_ids, user, run_id)
                    except Exception:
                        return True
                    return False

            cancellation = Cancellation()
            try:
                # Pass a copy so backend bookkeeping cannot mutate local evidence.
                options = {"cancel_event": cancellation} if getattr(backend, "supports_cancellation", False) is True else {}
                candidate_scores, candidate_stats = backend.search(query, rows=[dict(row) for row in rows], limit=limit, **options)
                valid_ids = {row["id"] for row in rows}
                if (type(candidate_scores) is not dict or len(candidate_scores) > limit
                        or any(key not in valid_ids or type(value) not in (float, int)
                               or not math.isfinite(value) or abs(value) > 1e12
                               for key, value in candidate_scores.items())):
                    official_fallback = "result_invalid"
                elif not candidate_scores:
                    official_fallback = "no_results"
                else:
                    official_scores = candidate_scores
                    # Never forward arbitrary upstream trace, model text or paths.
                    official_stats = {"algorithm": "hipporag2-official"}
                    for key in ("source_count", "unique_text_count", "returned_count", "graph_seed_count"):
                        value = candidate_stats.get(key) if isinstance(candidate_stats, dict) else None
                        if type(value) is int and 0 <= value <= 50000:
                            official_stats[key] = value
            except HippoRAGError as exc:
                if exc.code == "cancelled":
                    self._official_search_user(kb_ids, user, run_id)
                    raise AppError(409, "KNOWLEDGE_SEARCH_CANCELLED", "知识检索已取消") from None
                official_fallback = exc.code if exc.code in {
                    "unavailable", "invalid_input", "identity_mismatch", "index_untrusted",
                    "index_failed", "index_not_ready", "result_invalid", "retrieval_failed",
                    "timeout", "worker_failed", "worker_protocol",
                } else "backend_failed"
            except Exception:
                official_fallback = "backend_failed"
            # Retrieval can call models. Revocation and version changes during
            # that wait must not return stale results or fall back to stale rows.
            user = self._official_search_user(kb_ids, user, run_id)
            if cancel_event is not None and cancel_event.is_set():
                raise AppError(409, "KNOWLEDGE_SEARCH_CANCELLED", "知识检索已取消")
            if rows != self._search_rows(kb_ids, run_id):
                raise AppError(409, "KNOWLEDGE_SNAPSHOT_CHANGED", "检索期间知识版本或索引已变化，请重新检索")
        counts, query_terms = [json.loads(row["terms"]) for row in rows], terms(query)
        average = sum(sum(count.values()) for count in counts) / len(counts) or 1
        document_frequency = {token: sum(token in count for count in counts) for token in query_terms}
        # Pure BM25 must remain usable even when an optional local embedding
        # model is not installed. Hybrid/vector modes are the only paths that
        # initialize the embedding model.
        vector = None if mode == "bm25" or official_scores is not None else self.embeddings.embed([query], query=True)[0]
        ranked = []
        for row, count in zip(rows, counts):
            length = sum(count.values()) or 1
            score = 0.0
            for token in query_terms:
                frequency = count.get(token, 0)
                inverse = math.log(1 + (len(rows) - document_frequency[token] + .5) / (document_frequency[token] + .5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (.25 + .75 * length / average))
            dense = json.loads(row["vector"])
            cosine = sum(a * b for a, b in zip(vector, dense)) if vector is not None and row["embedding_model"] == self.embeddings.name else 0
            ranked.append({**row, "lexical_score": score, "vector_score": cosine})
        lexical = sorted(ranked, key=lambda row: row["lexical_score"], reverse=True)
        dense = sorted(ranked, key=lambda row: row["vector_score"], reverse=True)
        scores = Counter()
        base_mode = "hybrid" if mode == "hipporag" else mode
        if base_mode == "bm25":
            scores.update({row["id"]: row["lexical_score"] for row in lexical if row["lexical_score"] > 0})
        elif base_mode == "vector":
            scores.update({row["id"]: row["vector_score"] for row in dense if row["vector_score"] > .1})
        else:
            for order, key in ((lexical, "lexical_score"), (dense, "vector_score")):
                for index, row in enumerate(order):
                    if row[key] > (0 if key == "lexical_score" else .1):
                        scores[row["id"]] += 1 / (60 + index + 1)
        graph_scores, graph_trace = self._hipporag_graph_scores(kb_ids, query, rows, run_id=run_id) if mode == "hipporag" and official_scores is None else ({}, official_stats)
        if graph_scores:
            maximum = max(graph_scores.values()) or 1.0
            # Reciprocal-rank fusion keeps lexical/vector evidence useful while
            # allowing a source-linked graph path to promote related chunks.
            for item in ranked:
                graph_value = graph_scores.get(item["id"], 0.0) / maximum
                if mode == "hipporag":
                    scores[item["id"]] = scores[item["id"]] * 0.45 + graph_value * 0.55
        if official_scores is not None:
            scores = Counter(official_scores)
            by_id = {row["id"]: row for row in ranked}
            ordered = [by_id[chunk_id] for chunk_id in official_scores]
        else:
            ordered = sorted(ranked, key=lambda item: (-scores[item["id"]], item["id"]))
        selected = []
        selected_ids = set()
        for row in ordered:
            if official_scores is None and not scores[row["id"]]:
                continue
            selected.append((row, False))
            selected_ids.add(row["id"])
            if len(selected) >= limit:
                break

        # A paragraph-level chunker can separate a formula, table heading, or
        # definition from the sentence that introduces it.  The lightweight
        # lexical fallback then ranks the sentence but gives the adjacent
        # formula a score of zero, even though it is part of the same source
        # section.  Fill unused result slots with immediate source neighbors so
        # RAG receives the bounded local context needed to verify an answer.
        # Neighbors are explicitly marked and never treated as a semantic hit.
        if official_scores is None and selected and len(selected) < limit:
            by_source = {}
            for candidate in ranked:
                by_source.setdefault((candidate["document_id"], candidate["version"]), []).append(candidate)
            positions = {}
            for group in by_source.values():
                group.sort(key=lambda item: (item["page"] or 0, item["start"] or 0, item["id"]))
                for position, candidate in enumerate(group):
                    positions[candidate["id"]] = (group, position)
            for row, _ in list(selected):
                group, position = positions.get(row["id"], ([], -1))
                for neighbor in group[max(0, position - 1):position + 2]:
                    if neighbor["id"] in selected_ids:
                        continue
                    selected.append((neighbor, True))
                    selected_ids.add(neighbor["id"])
                    if len(selected) >= limit:
                        break
                if len(selected) >= limit:
                    break

        result = []
        for rank, (row, adjacent) in enumerate(selected, start=1):
            item = {key: row[key] for key in ("document_id", "version", "page", "section", "paragraph", "start", "end", "text", "name", "kb_id", "knowledge_base", "sha256", "imported_at")} | {"title": row["name"], "excerpt": row["text"], "chunk_id": row["id"], "score": round(scores[row["id"]], 6), "rank": rank, "lexical_score": round(row["lexical_score"], 6), "vector_score": round(row["vector_score"], 6), "retrieval_mode": mode, "page": row["page"] if Path(row["name"]).suffix.lower() == ".pdf" else None}
            if mode == "hipporag":
                maximum = max(graph_scores.values()) if graph_scores else 1.0
                if official_scores is None:
                    item["graph_score"] = round(graph_scores.get(row["id"], 0.0) / maximum if maximum else 0.0, 6)
                item["graph_trace"] = graph_trace
                item["retrieval_algorithm"] = "hipporag2-official" if official_scores is not None else "hipporag2-lite"
            item["content_url"] = f"/api/v1/knowledge-documents/{row['document_id']}/content?version={row['version']}"
            if adjacent:
                item["evidence_type"] = "adjacent_source_context"
            # Text retrieval remains the ranking source. When the source also
            # has a local image/page preview, attach it as bounded, immutable
            # evidence rather than pretending pixels were semantically indexed.
            media = self._search_media(row)
            if media:
                item["media"] = media
            result.append(item)
        # Cross-modal retrieval is additive: a local CLIP text vector can
        # surface an image that has no OCR text at all. It never replaces
        # BM25/vector text ranking and is unavailable when the local checkpoint
        # is absent. The result keeps an explicit visual evidence boundary.
        if official_scores is None and mode in {"hybrid", "vector", "hipporag"} and len(result) < limit:
            result.extend(self._visual_search(kb_ids, query, limit - len(result), run_id=run_id))
        # Return a bounded, copyable context preview for human inspection and
        # downstream RAG prompts. It is assembled only from authorized hits.
        context_parts, used = [], 0
        for index, item in enumerate(result, start=1):
            label = f"[{index}] {item['title']} v{item['version']}"
            if item.get("page"):
                label += f" 第{item['page']}页"
            text = f"{label}\n{item['text']}"
            if context_chars <= 0:
                break
            if not item.get("text"):
                continue
            separator = "\n\n" if context_parts else ""
            remaining = context_chars - used - len(separator)
            if remaining <= 0:
                break
            if len(text) > remaining:
                text = text[:remaining]
            context_parts.append(text)
            used += len(separator) + len(text)
            if used >= context_chars:
                break
        context_text = "\n\n".join(context_parts)
        retrieval = {**self.retrieval_info(), "mode": mode, "algorithm": "hipporag2-official" if official_scores is not None else "hipporag2-lite" if mode == "hipporag" else mode}
        if mode == "hipporag":
            retrieval["backend"] = "official" if official_scores is not None else "lite"
        if official_fallback:
            retrieval["fallback"] = official_fallback
        if graph_trace is not None:
            retrieval["graph_trace"] = graph_trace
        if backend is not None:
            self._recheck_official_results(kb_ids, user, run_id, rows, result)
        return {"results": result, "retrieval": retrieval, "context": {"text": context_text, "characters": len(context_text), "truncated": bool(result and len(context_text) >= context_chars and context_chars > 0)}}

    def _visual_search(self, kb_ids, query, limit, run_id=None):
        query_vector = self.vision.embed_text(query)
        if not query_vector:
            return []
        placeholders = ",".join("?" for _ in kb_ids)
        values = list(kb_ids)
        snapshot_clause = ""
        if run_id:
            snapshot_clause = " AND EXISTS (SELECT 1 FROM knowledge_run_documents rd WHERE rd.run_id=? AND rd.document_id=a.document_id AND rd.version=a.version)"
            values.append(run_id)
        # A normal search uses the current document version, while a running
        # turn must use the immutable snapshot version.  Applying the current
        # version join in both cases silently dropped historical image hits.
        document_version_clause = "d.current_version=a.version" if not run_id else "1=1"
        rows = self.db.all(
            f"SELECT a.document_id,a.version,a.mime_type,a.visual_index_status,d.name,d.kb_id,k.name AS knowledge_base,v.sha256,v.created_at AS imported_at,e.vector,e.model,e.dimensions,e.device "
            f"FROM knowledge_image_assets a JOIN knowledge_documents d ON d.id=a.document_id AND {document_version_clause} AND d.deleted=0 "
            f"JOIN knowledge_versions v ON v.document_id=a.document_id AND v.version=a.version AND v.status='ready' "
            f"JOIN knowledge_bases k ON k.id=d.kb_id "
            f"JOIN knowledge_visual_embeddings e ON e.document_id=a.document_id AND e.version=a.version AND e.region_id='' "
            f"WHERE d.kb_id IN ({placeholders}){snapshot_clause} LIMIT 256", values,
        )
        scored = []
        for row in rows:
            try:
                # Never compare vectors produced by different checkpoints or
                # dimensions.  A model change therefore fails closed instead
                # of producing an attractive but meaningless score.
                vector = json.loads(row["vector"])
                if row["model"] != query_vector.get("model") or int(row["dimensions"] or 0) != int(query_vector.get("dimensions") or 0):
                    continue
                if len(vector) != len(query_vector["vector"]):
                    continue
                score = sum(a * b for a, b in zip(query_vector["vector"], vector))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if score < 0.12:
                continue
            item = dict(row)
            item["name"] = row["name"]
            item["page"] = None
            item["text"] = ""
            item["lexical_score"] = 0.0
            item["vector_score"] = score
            scored.append(item)
        result = []
        for rank, row in enumerate(sorted(scored, key=lambda value: value["vector_score"], reverse=True)[:limit], start=1):
            item = {key: row.get(key) for key in ("document_id", "version", "page", "name", "kb_id", "knowledge_base", "sha256", "imported_at")}
            item.update({"title": row["name"], "excerpt": "图片视觉召回（无可用 OCR 文本）", "chunk_id": None,
                         "text": "", "score": round(row["vector_score"], 6), "rank": rank,
                         "lexical_score": 0.0, "vector_score": round(row["vector_score"], 6),
                         "section": "", "paragraph": None, "start": None, "end": None,
                         "retrieval_mode": "visual", "evidence_type": "visual_embedding",
                         "content_url": f"/api/v1/knowledge-documents/{row['document_id']}/content?version={row['version']}"})
            media = self._search_media(row)
            if media:
                item["media"] = media
            result.append(item)
        return result

    def _search_media(self, row):
        """Return source-bound visual evidence for a retrieved text chunk.

        This deliberately exposes only thumbnails, coordinates, and model
        readiness. No labels or relationships are inferred without an approved
        visual model.
        """
        suffix = Path(row["name"]).suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
            asset = self.db.one("SELECT * FROM knowledge_image_assets WHERE document_id=? AND version=?", (row["document_id"], row["version"]))
            if not asset:
                return None
            regions = self.db.all(
                "SELECT * FROM knowledge_image_regions WHERE document_id=? AND version=? ORDER BY grid_row,grid_col LIMIT ?",
                (row["document_id"], row["version"], MAX_SEARCH_MEDIA_REGIONS),
            )
            return {
                "kind": "image",
                "document_id": row["document_id"], "version": row["version"], "sha256": row["sha256"],
                "mime_type": asset["mime_type"], "width": asset["width"], "height": asset["height"],
                "thumbnail_url": f"/api/v1/knowledge-documents/{row['document_id']}/thumbnail?version={row['version']}",
                "regions": [self._image_region_view(region) for region in regions],
                "ocr_status": asset["ocr_status"], "visual_index_status": asset["visual_index_status"],
                "visual_embedding": self.db.one("SELECT model,dimensions,device FROM knowledge_visual_embeddings WHERE document_id=? AND version=? AND region_id=''", (row["document_id"], row["version"])) or None,
                "evidence_boundary": "图片预览和安全网格绑定到文件版本；OCR实体有文字框证据，模型实体仅是候选，关系统一标记 evidence_only。",
            }
        if suffix == ".pdf":
            preview = self.db.one(
                "SELECT * FROM knowledge_page_previews WHERE document_id=? AND version=? AND page=?",
                (row["document_id"], row["version"], row["page"]),
            ) if row["page"] else None
            return {
                "kind": "page_preview",
                "document_id": row["document_id"], "version": row["version"], "sha256": row["sha256"],
                "page": row["page"],
                "thumbnail_url": f"/api/v1/knowledge-documents/{row['document_id']}/thumbnail?version={row['version']}&page={row['page']}" if preview else None,
                "width": preview["width"] if preview else None, "height": preview["height"] if preview else None,
                "visual_index_status": self.vision.status()["visual_embedding"],
                "evidence_boundary": "页面预览绑定到文件版本；页面视觉向量需先将页面导出为图片后索引。",
            }
        return None

    def search(self, kb_ids, user, query, limit=6, run_id=None, mode="hybrid", context_chars=12000, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            raise AppError(409, "KNOWLEDGE_SEARCH_CANCELLED", "知识检索已取消")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise AppError(422, "INVALID_QUERY", "请输入有效的知识检索问题")
        policy = self.service.policy
        if policy.is_trial:
            policy.require_retrieval_mode(mode)
        elif mode not in {"hybrid", "bm25", "vector", "hipporag"}:
            raise AppError(422, "INVALID_RETRIEVAL_MODE", "不支持的检索模式")
        result = self._search(kb_ids, user, query, max(1, min(int(limit), 20)), run_id, mode, max(0, min(int(context_chars), 24000)), cancel_event)
        if cancel_event is not None and cancel_event.is_set():
            raise AppError(409, "KNOWLEDGE_SEARCH_CANCELLED", "知识检索已取消")
        return result

    async def search_async(self, *args, disconnected=None, **kwargs):
        cancellation = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(self.search, *args, **kwargs, cancel_event=cancellation))
        worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())

        async def watch_disconnect():
            while not cancellation.is_set():
                if await disconnected():
                    cancellation.set()
                    return
                await asyncio.sleep(.05)

        watcher = asyncio.create_task(watch_disconnect()) if disconnected is not None else None
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation.set()
            # A process backend reaps its own worker before returning. Shield the
            # thread handoff so cancelling the coroutine does not lose cleanup.
            if getattr(self.hipporag_backend, "supports_cancellation", False) is True:
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=5)
                except (Exception, asyncio.CancelledError):
                    pass
            raise
        finally:
            cancellation.set()
            if watcher is not None:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

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
        # Visual-only hits have no text chunk or paragraph locator, but still
        # need an auditable source record so an agent turn can cite the image.
        # Keep the excerpt bounded and explicitly label its evidence boundary.
        if item.get("retrieval_mode") == "visual" or item.get("evidence_type") == "visual_embedding":
            item = {
                "knowledge_base": item.get("knowledge_base", ""),
                "kb_id": item["kb_id"], "name": item["name"],
                "version": item["version"], "sha256": item["sha256"],
                "page": None, "section": "", "paragraph": None,
                "chunk_id": None, "start": None, "end": None,
                "imported_at": item["imported_at"], "document_id": item["document_id"],
                "text": item.get("excerpt") or "图片视觉召回（无可用 OCR 文本）",
                **{key: value for key, value in item.items() if key not in {"knowledge_base", "kb_id", "name", "version", "sha256", "page", "section", "paragraph", "chunk_id", "start", "end", "imported_at", "document_id", "text"}},
            }
        if Path(item["name"]).suffix.lower() != ".pdf":
            item = {**item, "page": None}
        locator = {"knowledge_base": item["knowledge_base"], "knowledge_base_id": item["kb_id"], "file": item["name"],
                   "version": item["version"], "sha256": item["sha256"], "page": item["page"], "section": item["section"],
                   "paragraph": item["paragraph"], "chunk_id": item["chunk_id"], "start": item.get("start"), "end": item.get("end"), "imported_at": item["imported_at"]}
        source = self.service.evidence.register(run_id, "knowledge", item["name"], locator, item["text"], resource_type="knowledge_base", resource_id=item["kb_id"], document_id=item["document_id"], document_version=item["version"])
        return {**item, "source_id": source["id"], "source_url": source["url"], "content_url": source.get("content_url") or item.get("content_url"), "citation": f"[[source:{source['id']}]]"}

    async def search_for_run(self, user, assistant, run_id, query, limit=6):
        kb_ids = self._assistant_ids(user, assistant, run_id)
        self.snapshot_for_run(run_id, assistant, user)
        mode = "hybrid" if self.service.policy.is_trial else ("hipporag" if self.hipporag_backend is not None else "hybrid")
        self.service.policy.require_retrieval_mode(mode)
        rows = self._search_rows(kb_ids, run_id) if mode == "hipporag" else None
        result = await self.search_async(kb_ids, user, query, limit, mode=mode, run_id=run_id)
        # The worker-to-event-loop handoff is another revocation boundary.
        with self.db.lock:
            if rows is not None:
                self._recheck_official_results(kb_ids, user, run_id, rows, result["results"])
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
    @router.get("/knowledge-documents/{document_id}/image")
    def image_metadata(document_id: str, version: int | None = Query(default=None, ge=1), user=Depends(authenticated), service=Depends(svc)):
        row, asset = service.knowledge.image_asset(document_id, version, user)
        regions = service.knowledge.image_regions(document_id, row["version"], user)
        return {
            "document_id": document_id, "version": row["version"], "name": row["name"],
            "mime_type": asset["mime_type"], "width": asset["width"], "height": asset["height"],
            "bytes": asset["bytes"], "thumbnail_width": asset["thumbnail_width"],
            "thumbnail_height": asset["thumbnail_height"],
            "thumbnail_url": f"/api/v1/knowledge-documents/{document_id}/thumbnail?version={row['version']}",
            "region_count": regions["region_count"], "regions_url": f"/api/v1/knowledge-documents/{document_id}/regions?version={row['version']}",
            "grid": regions["grid"], "regions": regions["regions"],
            "metadata": json.loads(asset["metadata_json"]), "ocr_status": asset["ocr_status"],
            "visual_index_status": asset["visual_index_status"],
            "visual_embedding": service.db.one("SELECT model,dimensions,device FROM knowledge_visual_embeddings WHERE document_id=? AND version=? AND region_id=''", (document_id, row["version"])) or None,
            "ocr": ({**dict(ocr_row), "boxes": json.loads(ocr_row["boxes"])} if (ocr_row := service.db.one("SELECT model,text,boxes,status FROM knowledge_visual_ocr WHERE document_id=? AND version=? AND region_id=''", (document_id, row["version"]))) else None),
            "entities": [service.knowledge._visual_entity_view({"id": item["id"], "document_id": document_id, "version": row["version"], **dict(item)}) for item in service.db.all("SELECT id,region_id,label,kind,confidence,source_text,source_boxes,source_offset_start,source_offset_end,evidence_kind FROM knowledge_visual_entities WHERE document_id=? AND version=? ORDER BY region_id,label", (document_id, row["version"]))],
            "relations": [dict(item) for item in service.db.all("SELECT source_entity_id,target_entity_id,relation,confidence,evidence,evidence_kind FROM knowledge_visual_relations WHERE document_id=? AND version=? ORDER BY id", (document_id, row["version"]))],
            # Preserve the original API's ``needs_model`` state for clients
            # that treat it as a conservative not-ready value; the richer OCR
            # status is available in ``ocr.status`` and ``retrieval``.
            "entity_index_status": "ready" if service.db.one("SELECT 1 AS ok FROM knowledge_visual_entities WHERE document_id=? AND version=? LIMIT 1", (document_id, row["version"])) else ("needs_model" if asset["visual_index_status"] == "needs_model" else "needs_ocr"),
            "evidence_boundary": regions["evidence_boundary"],
        }
    @router.get("/knowledge-documents/{document_id}/regions")
    def image_regions(document_id: str, version: int | None = Query(default=None, ge=1), limit: int = Query(default=MAX_IMAGE_REGIONS, ge=1, le=MAX_IMAGE_REGIONS), user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.image_regions(document_id, version, user, limit)
    @router.get("/knowledge-documents/{document_id}/thumbnail")
    def thumbnail(document_id: str, version: int | None = Query(default=None, ge=1), page: int | None = Query(default=None, ge=1, le=MAX_PAGES), user=Depends(authenticated), service=Depends(svc)):
        row = service.knowledge.version(document_id, version, user)
        thumbnail_path = None
        if page is not None:
            preview = service.db.one("SELECT thumbnail_path FROM knowledge_page_previews WHERE document_id=? AND version=? AND page=?", (document_id, row["version"], page))
            thumbnail_path = preview["thumbnail_path"] if preview else None
        else:
            _, asset = service.knowledge.image_asset(document_id, version, user)
            thumbnail_path = asset.get("thumbnail_path")
        if not thumbnail_path:
            raise AppError(404, "NOT_FOUND", "此版本尚未生成本地缩略图")
        path = Path(thumbnail_path)
        if path.is_symlink() or not path.resolve().is_relative_to(service.knowledge.root.resolve()) or not path.is_file():
            raise AppError(404, "NOT_FOUND", "图片缩略图不可用")
        return FileResponse(path, media_type="image/jpeg", filename=f"{Path(row['name']).stem}.thumb.jpg", headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
    @router.get("/knowledge-documents/{document_id}/regions/{region_id}/thumbnail")
    def image_region_thumbnail(document_id: str, region_id: str, version: int | None = Query(default=None, ge=1), user=Depends(authenticated), service=Depends(svc)):
        path, row = service.knowledge.image_region_thumbnail(document_id, region_id, version, user)
        return FileResponse(path, media_type="image/jpeg", filename=f"{Path(row['name']).stem}.{region_id}.thumb.jpg", headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
    @router.get("/knowledge-documents/{document_id}/chunks")
    def chunks(document_id: str, version: int | None = Query(default=None, ge=1), user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.chunks(document_id, user, version)
    @router.post("/knowledge-bases/{kb_id}/search")
    async def search(kb_id: str, data: SearchInput, request: Request, user=Depends(authenticated), service=Depends(svc)):
        return await service.knowledge.search_async([kb_id], user, data.query, data.limit, mode=data.mode,
                                                   context_chars=data.context_chars, disconnected=request.is_disconnected)
    return router
