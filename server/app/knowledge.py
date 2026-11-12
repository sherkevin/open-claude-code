"""Versioned local knowledge, bounded document parsing and hybrid retrieval."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import math
import os
import re
import threading
import time
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
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
MAX_IMAGE_REGIONS = 16
MAX_IMAGE_REGION_ROWS = 4
MAX_IMAGE_REGION_COLUMNS = 4
IMAGE_REGION_THUMBNAIL = (320, 240)
# Search responses can contain multiple hits from the same image. Keep media
# evidence bounded while the client still has a useful visual preview.
MAX_SEARCH_MEDIA_REGIONS = 4
# Unified ingestion is bounded in both work and retries. Deterministic
# problems (bad format, empty text, over-limit input) fail once; transient
# environment failures are retried a small, fixed number of times.
INGEST_MAX_ATTEMPTS = 3
INGEST_RETRY_DELAY = 0.25
INGEST_LEASE_SECONDS = 30
INGEST_EVENT_LIMIT = 40
RETRYABLE_INGEST_CODES = {"PARSER_UNAVAILABLE", "EMBEDDING_UNAVAILABLE", "INDEX_UNAVAILABLE"}
IMAGE_FILE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
INGEST_STAGE_LABELS = {
    "queued": "排队等待",
    "prepare": "准备解析",
    "parse": "解析文本",
    "ocr": "光学字符识别",
    "chunk": "结构化分块",
    "embed": "建立词法与向量索引",
    "graph": "生成图谱候选",
    "retry_wait": "等待自动重试",
    "done": "索引完成",
    "failed": "索引失败",
    "cancelled": "索引已取消",
}
MODEL = "BAAI/bge-small-zh-v1.5"


class IngestionCancelled(Exception):
    """Raised between bounded stages when a user cancels an active job."""


def _stamp_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))).isoformat()


GRAPH_STOPWORDS = {"设备", "系统", "数据", "文件", "内容", "说明", "检查", "需要", "可以", "以及", "进行", "其中", "这个", "当前"}
# These patterns intentionally stay deterministic and explainable.  They are
# useful for graph navigation and entity-focused acceptance fixtures, but are
# not a substitute for a trained named-entity or relation extraction model.
GRAPH_ENTITY_PATTERNS = (
    ("standard", re.compile(r"(?i)\b(?:GB(?:/T)?|ISO|IEC|ASTM|NIST|OSHA|CFR|SP|IR)\s*[-/]?\s*\d{2,5}(?:[-/]\d+)*(?:\s+Rev\.?\s*\d+)?")),
    ("metric", re.compile(r"(?i)(?:合格率|良率|压力|温度|扭矩|厚度|流量|转速|yield_rate)\s*(?:[:=：为是]\s*)[-+]?\d+(?:\.\d+)?\s*(?:%|bar|kPa|MPa|℃|°C|mm|cm|Nm|rpm|件|小时|天)?")),
    ("equipment", re.compile(r"(?i)(?:\b[A-Z]{1,5}[-_]?\d{1,5}\b|[\u3400-\u9fff]{1,12}(?:泵|阀|炉|机|线|站|罐|管|压机|机器人))")),
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
CREATE TABLE IF NOT EXISTS knowledge_ingest_jobs(
 job_id TEXT PRIMARY KEY,
 document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
 version INTEGER NOT NULL,
 kb_id TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued',
 stage TEXT NOT NULL DEFAULT 'queued',
 runner TEXT NOT NULL DEFAULT 'worker',
 attempts INTEGER NOT NULL DEFAULT 0,
 max_attempts INTEGER NOT NULL DEFAULT 3,
 error_code TEXT,
 error_message TEXT,
 events TEXT NOT NULL DEFAULT '[]',
 cancel_requested INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT,
 lease_expires_at TEXT,
 available_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 finished_at TEXT);
CREATE INDEX IF NOT EXISTS knowledge_ingest_jobs_document ON knowledge_ingest_jobs(document_id,version,created_at);
CREATE INDEX IF NOT EXISTS knowledge_ingest_jobs_status ON knowledge_ingest_jobs(status,available_at);
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
    mode: Literal["hybrid", "bm25", "vector"] = "hybrid"
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
        # The ingestion job installs a bounded stage callback for the duration
        # of one parse. It only reports coarse stages, never document text.
        self.stage_callback = None

    def _notify_stage(self, stage, detail=""):
        callback = self.stage_callback
        if callback:
            callback(stage, detail)

    def ocr_image(self, path):
        """Run optional local OCR for raster uploads without network access."""
        self._notify_stage("ocr", "图片本地 OCR")
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
        self._notify_stage("ocr", "扫描页本地 OCR")
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

    def parse(self, path, name, check_cancel=None):
        suffix = Path(name).suffix.lower()
        pages = []

        def checkpoint():
            if check_cancel:
                check_cancel()

        def invalid(exc):
            if isinstance(exc, AppError):
                raise exc
            raise AppError(422, "INVALID_DOCUMENT", "文件无法解析，请检查格式是否损坏") from exc

        if suffix == ".pdf":
            try:
                import pypdfium2 as pdfium
                with pdfium.PdfDocument(path) as document:
                    if not 0 < len(document) <= MAX_PAGES:
                        raise AppError(413, "DOCUMENT_TOO_LARGE", f"单个知识文档最多支持 {MAX_PAGES} 页")
                    total = 0
                    for index in range(len(document)):
                        checkpoint()
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
            except AppError:
                raise
            except Exception as exc:
                invalid(exc)
        elif suffix == ".docx":
            checkpoint()
            try:
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
            except AppError:
                raise
            except Exception as exc:
                invalid(exc)
        elif suffix == ".xlsx":
            checkpoint()
            try:
                pages = self.parse_xlsx(path)
            except AppError:
                raise
            except Exception as exc:
                invalid(exc)
        elif suffix in {".txt", ".md", ".csv", ".json"}:
            checkpoint()
            try:
                raw = path.read_bytes()
                encoding = "utf-16" if raw[:2] in {b"\xff\xfe", b"\xfe\xff"} else "utf-8-sig"
                text = raw.decode(encoding)
            except AppError:
                raise
            except Exception as exc:
                invalid(exc)
            if "\x00" in text:
                raise AppError(422, "INVALID_DOCUMENT", "文件内容不是可读取的文本")
            pages = [{"page": 1, "text": text, "extraction": "text"}]
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
            checkpoint()
            text = self.ocr_image(path)
            pages = [{"page": 1, "text": text, "extraction": "local-ocr"}]
        else:
            raise AppError(415, "DOCUMENT_TYPE_UNSUPPORTED", "支持 PDF、DOCX、XLSX、Markdown、TXT、CSV、JSON 和常见图片文件")
        checkpoint()
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
            version_columns = {row["name"] for row in self.db.conn.execute("PRAGMA table_info(knowledge_versions)")}
            if "error_code" not in version_columns:
                self.db.conn.execute("ALTER TABLE knowledge_versions ADD COLUMN error_code TEXT")
            job_columns = {row["name"] for row in self.db.conn.execute("PRAGMA table_info(knowledge_ingest_jobs)")}
            if "runner" not in job_columns:
                self.db.conn.execute("ALTER TABLE knowledge_ingest_jobs ADD COLUMN runner TEXT NOT NULL DEFAULT 'worker'")
        self.root = service.settings.data_dir / "knowledge"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.parser = DocumentParser()
        self.embeddings = LocalEmbeddings(self.root)
        self.index_lock = threading.RLock()
        self.retry_limit = max(1, int(os.getenv("AGENT_KNOWLEDGE_INGEST_MAX_ATTEMPTS", str(INGEST_MAX_ATTEMPTS))))
        self.retry_delay = max(0.0, float(os.getenv("AGENT_KNOWLEDGE_INGEST_RETRY_SECONDS", str(INGEST_RETRY_DELAY))))
        self.lease_seconds = max(1.0, float(os.getenv("AGENT_KNOWLEDGE_INGEST_LEASE_SECONDS", str(INGEST_LEASE_SECONDS))))
        self.worker_owner = f"{os.getpid()}:{uid()}"
        self._worker = None
        self._worker_stop = threading.Event()
        self._worker_wakeup = threading.Event()
        # A crashed process must not leave apparently running jobs forever.
        # Requeue an interrupted job while budget remains; otherwise expose an
        # explicit terminal failure for the user to retry manually.
        self.recover()

    def _latest_job(self, document_id, version):
        return self.db.one(
            "SELECT * FROM knowledge_ingest_jobs WHERE document_id=? AND version=? ORDER BY rowid DESC LIMIT 1",
            (document_id, version),
        )

    def _active_job(self, document_id, version):
        return self.db.one(
            "SELECT * FROM knowledge_ingest_jobs WHERE document_id=? AND version=? AND status IN ('queued','running') ORDER BY rowid DESC LIMIT 1",
            (document_id, version),
        )

    def _append_job_event(self, job_id, kind, detail=""):
        row = self.db.one("SELECT events FROM knowledge_ingest_jobs WHERE job_id=?", (job_id,))
        if not row:
            return
        try:
            events = json.loads(row["events"] or "[]")
        except json.JSONDecodeError:
            events = []
        events.append({"at": now(), "kind": kind, "detail": str(detail or "")[:300]})
        self.db.execute(
            "UPDATE knowledge_ingest_jobs SET events=?, updated_at=? WHERE job_id=?",
            (dump(events[-INGEST_EVENT_LIMIT:]), now(), job_id),
        )

    def _set_stage(self, job_id, stage, detail=""):
        self.db.execute(
            "UPDATE knowledge_ingest_jobs SET stage=?, updated_at=? WHERE job_id=?",
            (stage, now(), job_id),
        )
        self._append_job_event(job_id, f"stage:{stage}", detail)

    def _check_cancel(self, job_id):
        row = self.db.one("SELECT status,cancel_requested FROM knowledge_ingest_jobs WHERE job_id=?", (job_id,))
        if not row or row["status"] != "running" or row["cancel_requested"]:
            raise IngestionCancelled()
        # A live worker renews its lease between pages/stages so a long OCR or
        # index pass is never mistaken for a crashed process by another start.
        self.db.execute(
            "UPDATE knowledge_ingest_jobs SET lease_expires_at=?,updated_at=? WHERE job_id=? AND status='running'",
            (_stamp_after(self.lease_seconds), now(), job_id),
        )

    def recover(self):
        """Resume queued jobs and reclaim leases left by an interrupted process."""
        stamp = now()
        with self.db.lock, self.db.transaction():
            for row in self.db.all("SELECT * FROM knowledge_ingest_jobs WHERE status='running'"):
                if row["lease_expires_at"] and row["lease_expires_at"] >= stamp:
                    continue
                if row["cancel_requested"]:
                    self._finish_job(row["job_id"], "cancelled", "CANCELLED", "本次索引已取消", "cancelled")
                    continue
                if row["attempts"] >= row["max_attempts"]:
                    self._finish_job(
                        row["job_id"], "failed", "LEASE_EXPIRED",
                        "上次索引中断且自动重试次数已用尽，请手动重新解析", "failed",
                    )
                    continue
                self.db.execute(
                    "UPDATE knowledge_ingest_jobs SET status='queued',stage='queued',runner='worker',lease_owner=NULL,lease_expires_at=NULL,available_at=?,updated_at=? WHERE job_id=?",
                    (stamp, stamp, row["job_id"]),
                )
                self._append_job_event(row["job_id"], "requeued", "检测到中断的索引任务，已重新排队")
                self.db.execute(
                    "UPDATE knowledge_versions SET status='processing',error=NULL,error_code=NULL WHERE document_id=? AND version=?",
                    (row["document_id"], row["version"]),
                )
            # Versions created by older releases (or jobs lost before being
            # persisted) are adopted instead of being silently abandoned.
            orphans = self.db.all(
                """SELECT v.document_id,v.version,d.kb_id FROM knowledge_versions v
                   JOIN knowledge_documents d ON d.id=v.document_id AND d.deleted=0
                   WHERE v.status IN ('processing','queued')
                     AND NOT EXISTS(SELECT 1 FROM knowledge_ingest_jobs j
                                    WHERE j.document_id=v.document_id AND j.version=v.version
                                      AND j.status IN ('queued','running'))""",
            )
            for row in orphans:
                job_id = uid()
                self.db.execute(
                    "INSERT INTO knowledge_ingest_jobs(job_id,document_id,version,kb_id,status,stage,attempts,max_attempts,created_at,updated_at) VALUES(?,?,?,?, 'queued','queued',0,?,?,?)",
                    (job_id, row["document_id"], row["version"], row["kb_id"], self.retry_limit, stamp, stamp),
                )
                self._append_job_event(job_id, "adopted", "启动时接管未完成的索引版本")

    def _create_job(self, document_id, version, kb_id, inline):
        stamp = now()
        job_id = uid()
        if self._active_job(document_id, version):
            raise AppError(409, "INGESTION_BUSY", "此版本正在索引，请等待完成或先取消")
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO knowledge_ingest_jobs(job_id,document_id,version,kb_id,status,stage,runner,attempts,max_attempts,lease_owner,lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, document_id, version, kb_id, "running" if inline else "queued",
                 "queued", "inline" if inline else "worker", 1 if inline else 0, self.retry_limit,
                 self.worker_owner if inline else None,
                 _stamp_after(self.lease_seconds) if inline else None, stamp, stamp),
            )
            self.db.execute(
                "UPDATE knowledge_versions SET status='processing',error=NULL,error_code=NULL WHERE document_id=? AND version=?",
                (document_id, version),
            )
        self._append_job_event(job_id, "created", "同步执行" if inline else "后台排队")
        return job_id

    def _claim_job(self, job_id=None):
        stamp = now()
        with self.db.lock, self.db.transaction():
            if job_id:
                row = self.db.one("SELECT * FROM knowledge_ingest_jobs WHERE job_id=? AND status='queued' AND cancel_requested=0", (job_id,))
            else:
                row = self.db.one(
                    "SELECT * FROM knowledge_ingest_jobs WHERE status='queued' AND runner<>'inline' AND cancel_requested=0 AND (available_at IS NULL OR available_at<=?) ORDER BY created_at,rowid LIMIT 1",
                    (stamp,),
                )
            if not row:
                return None
            self.db.execute(
                "UPDATE knowledge_ingest_jobs SET status='running',attempts=attempts+1,stage='prepare',lease_owner=?,lease_expires_at=?,available_at=NULL,updated_at=? WHERE job_id=? AND status='queued'",
                (self.worker_owner, _stamp_after(self.lease_seconds), stamp, row["job_id"]),
            )
            return self.db.one("SELECT * FROM knowledge_ingest_jobs WHERE job_id=?", (row["job_id"],))

    def _finish_job(self, job_id, status, code, message, version_status):
        stamp = now()
        self.db.execute(
            "UPDATE knowledge_ingest_jobs SET status=?,stage=?,error_code=?,error_message=?,lease_owner=NULL,lease_expires_at=NULL,available_at=NULL,updated_at=?,finished_at=? WHERE job_id=?",
            (status, "done" if status == "succeeded" else status, code, message, stamp, stamp, job_id),
        )
        self._append_job_event(job_id, status, message)
        row = self.db.one("SELECT document_id,version,kb_id FROM knowledge_ingest_jobs WHERE job_id=?", (job_id,))
        if row:
            self.db.execute(
                "UPDATE knowledge_versions SET status=?,error=?,error_code=? WHERE document_id=? AND version=?",
                (version_status, None if status == "succeeded" else message, None if status == "succeeded" else code,
                 row["document_id"], row["version"]),
            )
            document = self.db.one("SELECT name FROM knowledge_documents WHERE id=?", (row["document_id"],))
            if document and Path(document["name"]).suffix.lower() in IMAGE_FILE_SUFFIXES:
                image_status = "ready" if status == "succeeded" else ("needs_ocr" if code == "OCR_REQUIRED" else status)
                self.db.execute(
                    "UPDATE knowledge_image_assets SET ocr_status=? WHERE document_id=? AND version=?",
                    (image_status, row["document_id"], row["version"]),
                )
            self.db.execute(
                "UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?",
                (stamp, row["kb_id"]),
            )

    def _classify_failure(self, exc):
        if isinstance(exc, AppError):
            return exc.code, exc.message, exc.code in RETRYABLE_INGEST_CODES
        return "INGESTION_FAILED", "文档解析或索引失败，请检查格式后重试", True

    def _run_stages(self, job):
        document_id, version, job_id = job["document_id"], job["version"], job["job_id"]
        row = self.db.one(
            "SELECT v.*,d.kb_id,d.name FROM knowledge_versions v JOIN knowledge_documents d ON d.id=v.document_id WHERE v.document_id=? AND v.version=?",
            (document_id, version),
        )
        if not row:
            raise AppError(404, "NOT_FOUND", "知识文件版本不存在")
        path, name = self.content_path(row), row["name"]
        suffix = Path(name).suffix.lower()
        self.parser.stage_callback = lambda stage, detail="": self._set_stage(job_id, stage, detail)
        try:
            self._set_stage(job_id, "prepare", "生成本地预览证据")
            self._check_cancel(job_id)
            if row["mime_type"].startswith("image/"):
                self._prepare_image_asset(document_id, version, path, row["mime_type"], row["size"])
            elif row["mime_type"] == "application/pdf":
                self._prepare_pdf_previews(document_id, version, path)
            self._set_stage(job_id, "parse", "解析原文并保留页码")
            try:
                accepts_cancel = "check_cancel" in inspect.signature(self.parser.parse).parameters
            except (TypeError, ValueError):
                accepts_cancel = False
            pages = (
                self.parser.parse(path, name, check_cancel=lambda: self._check_cancel(job_id))
                if accepts_cancel else self.parser.parse(path, name)
            )
            self._check_cancel(job_id)
            self._set_stage(job_id, "chunk", "按段落分块并保留字符偏移")
            chunks = chunk_pages(pages)
            if len(chunks) > MAX_CHUNKS:
                raise AppError(413, "DOCUMENT_TOO_LARGE", f"单个知识文档最多生成 {MAX_CHUNKS} 个文本片段")
            is_image = suffix in IMAGE_FILE_SUFFIXES
            if not chunks and not is_image:
                raise AppError(422, "EMPTY_DOCUMENT", "未找到可检索的文本片段")
            self._check_cancel(job_id)
            self._set_stage(job_id, "embed", "建立 BM25 词项与本地向量")
            vectors = self.embeddings.embed([chunk["text"] for chunk in chunks]) if chunks else []
            self._check_cancel(job_id)
            self._set_stage(job_id, "graph", "写入页码、片段和图谱候选")
            with self.db.transaction():
                self.db.execute("DELETE FROM knowledge_pages WHERE document_id=? AND version=?", (document_id, version))
                self.db.execute("DELETE FROM knowledge_chunks WHERE document_id=? AND version=?", (document_id, version))
                for page in pages:
                    self.db.execute("INSERT INTO knowledge_pages VALUES(?,?,?,?,?)", (document_id, version, page["page"], page["text"], page["extraction"]))
                for chunk, vector in zip(chunks, vectors):
                    self.db.execute("INSERT INTO knowledge_chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (chunk["id"], document_id, version, chunk["page"], chunk["section"], chunk["paragraph"], chunk["start"], chunk["end"], chunk["text"], dump(terms(chunk["text"])), dump(vector), self.embeddings.name))
                self._build_graph(document_id, version, chunks)
                self.db.execute(
                    "UPDATE knowledge_versions SET status='ready',error=NULL,error_code=NULL,page_count=?,chunk_count=?,extraction=?,embedding_model=? WHERE document_id=? AND version=?",
                    (len(pages), len(chunks), dump(sorted({page["extraction"] for page in pages})), self.embeddings.name, document_id, version),
                )
                if is_image:
                    self.db.execute("UPDATE knowledge_image_assets SET ocr_status='ready' WHERE document_id=? AND version=?", (document_id, version))
            self._set_stage(job_id, "done", f"{len(pages)} 页 / {len(chunks)} 个片段")
        finally:
            self.parser.stage_callback = None

    def _execute_job(self, job, inline):
        job_id, attempt = job["job_id"], job["attempts"]
        try:
            self._run_stages(job)
            self._finish_job(job_id, "succeeded", None, None, "ready")
        except IngestionCancelled:
            self._finish_job(job_id, "cancelled", "CANCELLED", "本次索引已取消，可重新解析", "cancelled")
            return
        except Exception as exc:
            code, message, retryable = self._classify_failure(exc)
            max_attempts = job["max_attempts"] or self.retry_limit
            if retryable and attempt < max_attempts:
                delay = self.retry_delay * (2 ** max(0, attempt - 1))
                self.db.execute(
                    "UPDATE knowledge_ingest_jobs SET status='queued',stage='retry_wait',error_code=?,error_message=?,lease_owner=NULL,lease_expires_at=NULL,available_at=?,updated_at=? WHERE job_id=?",
                    (code, message, _stamp_after(delay), now(), job_id),
                )
                self._append_job_event(job_id, "retry_scheduled", f"{code}：第 {attempt + 1}/{max_attempts} 次尝试将在 {delay:.2f} 秒后进行")
                if inline:
                    if delay:
                        time.sleep(delay)
                    claimed = self._claim_job(job_id)
                    if claimed:
                        self._execute_job(claimed, inline=True)
                return
            version_status = "needs_ocr" if code == "OCR_REQUIRED" else "failed"
            self._finish_job(job_id, "failed", code, message, version_status)

    def process_pending(self, limit=50):
        """Process queued jobs in the caller's thread; useful for recovery tests."""
        processed = 0
        while processed < max(1, limit):
            job = self._claim_job()
            if not job:
                break
            with self.index_lock:
                self._execute_job(job, inline=False)
            processed += 1
        return processed

    def start_worker(self):
        if self._worker and self._worker.is_alive():
            return
        self.recover()
        self._worker_stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="knowledge-ingest", daemon=True)
        self._worker.start()

    def stop_worker(self):
        self._worker_stop.set()
        self._worker_wakeup.set()
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=5)
        self._worker = None

    def _worker_loop(self):
        while not self._worker_stop.is_set():
            job = self._claim_job()
            if not job:
                self._worker_wakeup.wait(0.5)
                self._worker_wakeup.clear()
                continue
            with self.index_lock:
                self._execute_job(job, inline=False)

    def _job_view(self, row):
        if not row:
            return None
        try:
            events = json.loads(row["events"] or "[]")
        except json.JSONDecodeError:
            events = []
        return {
            "job_id": row["job_id"], "status": row["status"], "stage": row["stage"],
            "stage_label": INGEST_STAGE_LABELS.get(row["stage"], row["stage"]),
            "attempts": row["attempts"], "max_attempts": row["max_attempts"],
            "retryable": row["error_code"] in RETRYABLE_INGEST_CODES or (
                row["status"] == "failed" and row["error_code"] == "INGESTION_FAILED"
            ),
            "error_code": row["error_code"], "error_message": row["error_message"],
            "can_cancel": row["status"] in {"queued", "running"},
            "can_retry": row["status"] in {"failed", "cancelled"},
            "cancel_requested": bool(row["cancel_requested"]),
            "available_at": row["available_at"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "finished_at": row["finished_at"],
            "events": events[-20:],
        }

    def ingestion_view(self, document_id, version, user):
        row = self.version(document_id, version, user)
        return {"document_id": document_id, "version": row["version"],
                "limits": self.ingestion_limits(), "job": self._job_view(self._latest_job(document_id, row["version"]))}

    def ingestion_limits(self):
        return {"max_bytes": self.service.settings.upload_max_bytes, "max_pages": MAX_PAGES,
                "max_text_characters": MAX_TEXT, "max_chunks": MAX_CHUNKS,
                "max_attempts": self.retry_limit, "retry_delay_seconds": self.retry_delay,
                "chunk_characters": 900, "chunk_window_characters": 1100,
                "behavior": "超出单文件、页数、文本或片段上限时明确拒绝，不静默截断。"}

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
                "index_status": {item["status"]: item["n"] for item in statuses}, "retrieval": self.retrieval_info(),
                "ingestion": self.ingestion_limits()}

    def retrieval_info(self):
        return {"mode": "hybrid", "available_modes": ["hybrid", "bm25", "vector"],
                "lexical": "BM25", "vector_model": self.embeddings.name,
                "semantic": self.embeddings.name != "local-lexical-v1", "execution": "server-local",
                "visual_embedding": {"status": "needs_model", "model": None, "scope": "page-or-region", "local_only": True}}

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
            "extraction": json.loads(row["extraction"]), "error_code": row.get("error_code"),
            "content_url": f"/api/v1/knowledge-documents/{row['id']}/content?version={row['version']}"}
        job = self._latest_job(row["id"], row["version"])
        result["ingestion"] = self._job_view(job)
        ready = self.db.one("SELECT MAX(version) AS v FROM knowledge_versions WHERE document_id=? AND status='ready'", (row["id"],))
        result["last_ready_version"] = ready["v"] if ready and ready["v"] is not None else None
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
                "evidence_boundary": "仅登记尺寸、缩略图、网格区域和本地 OCR 状态；未启用视觉模型，不将像素内容冒充实体或语义检索。",
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

    def upload(self, kb_id, user, name, data, document_id=None, wait=True):
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
            job_id = self._create_job(document_id, version, kb_id, inline=wait)
            self.db.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), kb_id))
        if wait:
            job = self.db.one("SELECT * FROM knowledge_ingest_jobs WHERE job_id=?", (job_id,))
            with self.index_lock:
                self._execute_job(job, inline=True)
        else:
            # The application lifespan owns a single background worker. Direct
            # service callers can use process_pending() for deterministic work.
            self._worker_wakeup.set()
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
            "evidence_boundary": "仅按原图坐标生成本地缩略图；未启用视觉模型，不推断对象、实体、文字或关系。",
        }

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
            "visual_embedding": "needs_model",
            "entity_extraction": "needs_model",
            "evidence_boundary": "区域来自有界网格裁剪，坐标和缩略图绑定到文件版本并经过访问控制；没有视觉模型时不产生像素实体或语义结论。",
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
        return {"nodes": [{"id": row["id"], "label": row["label"], "kind": row["kind"], "source": source(row)} for row in nodes],
                "edges": [{"id": row["id"], "source": row["source_node_id"], "target": row["target_node_id"], "relation": row["relation"], "provenance": source(row)} for row in edges],
                "versioned": True, "source": "local-extraction", "query": query or "",
                "multimodal": {
                    "images": images,
                    "visual_embedding": "needs_model",
                    "entity_extraction": "ocr_only",
                    "region_segmentation": "bounded_grid",
                    "evidence_boundary": "图谱节点和边来自文本、表格或本地 OCR；图片仅提供版本绑定的网格缩略图，像素尚未经过视觉模型，不推断对象或关系。",
                }}

    def reindex(self, document_id, user, wait=True):
        self.document(document_id, user, edit=True)
        row = self.version(document_id, None, user)
        with self.index_lock:
            self.content_path(row)
            job_id = self._create_job(document_id, row["version"], row["kb_id"], inline=wait)
        if wait:
            job = self.db.one("SELECT * FROM knowledge_ingest_jobs WHERE job_id=?", (job_id,))
            with self.index_lock:
                self._execute_job(job, inline=True)
        else:
            self._worker_wakeup.set()
        return self.document_view(self.version(document_id, None, user))

    def cancel(self, document_id, user):
        self.document(document_id, user, edit=True)
        row = self.version(document_id, None, user)
        job = self._active_job(document_id, row["version"])
        if job:
            self.db.execute(
                "UPDATE knowledge_ingest_jobs SET cancel_requested=1,updated_at=? WHERE job_id=?",
                (now(), job["job_id"]),
            )
            self._append_job_event(job["job_id"], "cancel_requested", "用户请求取消本次索引")
            if job["status"] == "queued":
                self._finish_job(job["job_id"], "cancelled", "CANCELLED", "本次索引已取消，可重新解析", "cancelled")
            else:
                self._worker_wakeup.set()
        return self.document_view(self.version(document_id, None, user))

    def cancel_jobs_for_document(self, document_id):
        with self.db.transaction():
            for job in self.db.all(
                "SELECT job_id FROM knowledge_ingest_jobs WHERE document_id=? AND status IN ('queued','running')",
                (document_id,),
            ):
                self._finish_job(job["job_id"], "cancelled", "DELETED", "文件已删除", "cancelled")

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

    def _search(self, kb_ids, user, query, limit, run_id=None, mode="hybrid", context_chars=12000):
        if mode not in {"hybrid", "bm25", "vector"}:
            raise AppError(422, "INVALID_RETRIEVAL_MODE", "不支持的检索模式")
        kb_ids = list(dict.fromkeys(kb_ids))[:100]
        for kb_id in kb_ids:
            if not self.require(kb_id, user)["enabled"]:
                raise AppError(403, "KNOWLEDGE_DISABLED", "知识库已停用")
        if not kb_ids:
            return {"results": [], "retrieval": {**self.retrieval_info(), "mode": mode}, "context": {"text": "", "characters": 0, "truncated": False}}
        placeholders = ",".join("?" for _ in kb_ids)
        version_clause = "EXISTS(SELECT 1 FROM knowledge_run_documents rd WHERE rd.run_id=? AND rd.document_id=c.document_id AND rd.version=c.version)" if run_id else "d.current_version=c.version"
        values = ([run_id] if run_id else []) + kb_ids
        rows = self.db.all(f"SELECT c.*,d.name,d.kb_id,v.sha256,v.created_at AS imported_at,k.name AS knowledge_base FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id AND {version_clause} JOIN knowledge_versions v ON v.document_id=c.document_id AND v.version=c.version JOIN knowledge_bases k ON k.id=d.kb_id WHERE d.kb_id IN ({placeholders}) AND d.deleted=0 AND v.status='ready' LIMIT 50000", values)
        if not rows:
            return {"results": [], "retrieval": {**self.retrieval_info(), "mode": mode}, "context": {"text": "", "characters": 0, "truncated": False}}
        counts, query_terms = [json.loads(row["terms"]) for row in rows], terms(query)
        average = sum(sum(count.values()) for count in counts) / len(counts) or 1
        document_frequency = {token: sum(token in count for count in counts) for token in query_terms}
        # Pure BM25 must remain usable even when an optional local embedding
        # model is not installed. Hybrid/vector modes are the only paths that
        # initialize the embedding model.
        vector = None if mode == "bm25" else self.embeddings.embed([query], query=True)[0]
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
        if mode == "bm25":
            scores.update({row["id"]: row["lexical_score"] for row in lexical if row["lexical_score"] > 0})
        elif mode == "vector":
            scores.update({row["id"]: row["vector_score"] for row in dense if row["vector_score"] > .1})
        else:
            for order, key in ((lexical, "lexical_score"), (dense, "vector_score")):
                for index, row in enumerate(order):
                    if row[key] > (0 if key == "lexical_score" else .1):
                        scores[row["id"]] += 1 / (60 + index + 1)
        ordered = sorted(ranked, key=lambda item: scores[item["id"]], reverse=True)
        selected = []
        selected_ids = set()
        for row in ordered:
            if not scores[row["id"]]:
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
        if selected and len(selected) < limit:
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
            if adjacent:
                item["evidence_type"] = "adjacent_source_context"
            # Text retrieval remains the ranking source. When the source also
            # has a local image/page preview, attach it as bounded, immutable
            # evidence rather than pretending pixels were semantically indexed.
            media = self._search_media(row)
            if media:
                item["media"] = media
            result.append(item)
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
        return {"results": result, "retrieval": {**self.retrieval_info(), "mode": mode}, "context": {"text": context_text, "characters": len(context_text), "truncated": bool(result and len(context_text) >= context_chars and context_chars > 0)}}

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
                "evidence_boundary": "图片预览和安全网格绑定到文件版本；未启用视觉模型，不推断像素实体或关系。",
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
                "visual_index_status": "needs_model",
                "evidence_boundary": "页面预览绑定到文件版本；未启用视觉模型，不推断像素实体或关系。",
            }
        return None

    def search(self, kb_ids, user, query, limit=6, run_id=None, mode="hybrid", context_chars=12000):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise AppError(422, "INVALID_QUERY", "请输入有效的知识检索问题")
        if mode not in {"hybrid", "bm25", "vector"}:
            raise AppError(422, "INVALID_RETRIEVAL_MODE", "不支持的检索模式")
        return self._search(kb_ids, user, query, max(1, min(int(limit), 20)), run_id, mode, max(0, min(int(context_chars), 24000)))

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
        result = await asyncio.to_thread(self.search, kb_ids, user, query, limit, mode="hybrid", run_id=run_id)
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
    async def ingest(kb_id, file, user, service, document_id=None, background=False):
        service.knowledge.require(kb_id, user, edit=True)
        data = await file.read(service.settings.upload_max_bytes + 1)
        return await asyncio.to_thread(service.knowledge.upload, kb_id, user, file.filename or "document.txt", data, document_id, not background)
    @router.post("/knowledge-bases/{kb_id}/documents", status_code=201)
    async def upload(kb_id: str, background: bool = Query(default=False), file: UploadFile = File(...), user=Depends(authenticated), service=Depends(svc)):
        return await ingest(kb_id, file, user, service, background=background)
    @router.post("/knowledge-bases/{kb_id}/documents/{document_id}/versions", status_code=201)
    async def upload_version(kb_id: str, document_id: str, background: bool = Query(default=False), file: UploadFile = File(...), user=Depends(authenticated), service=Depends(svc)):
        return await ingest(kb_id, file, user, service, document_id, background=background)
    @router.get("/knowledge-documents/{document_id}/versions")
    def versions(document_id: str, user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.versions(document_id, user)
    @router.get("/knowledge-documents/{document_id}/ingestion")
    def ingestion(document_id: str, version: int | None = Query(default=None, ge=1), user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.ingestion_view(document_id, version, user)
    @router.post("/knowledge-documents/{document_id}/reindex")
    async def reindex(document_id: str, background: bool = Query(default=False), user=Depends(authenticated), service=Depends(svc)):
        return await asyncio.to_thread(service.knowledge.reindex, document_id, user, not background)
    @router.post("/knowledge-documents/{document_id}/cancel")
    def cancel(document_id: str, user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.cancel(document_id, user)
    @router.delete("/knowledge-documents/{document_id}", status_code=204)
    def delete_document(document_id: str, user=Depends(authenticated), service=Depends(svc)):
        row = service.knowledge.document(document_id, user, edit=True)
        service.knowledge.cancel_jobs_for_document(document_id)
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
            "entity_index_status": "needs_model",
            "evidence_boundary": regions["evidence_boundary"],
        }
    @router.get("/knowledge-documents/{document_id}/regions")
    def image_regions(document_id: str, version: int | None = Query(default=None, ge=1), limit: int = Query(default=MAX_IMAGE_REGIONS, ge=1, le=MAX_IMAGE_REGIONS), user=Depends(authenticated), service=Depends(svc)):
        return service.knowledge.image_regions(document_id, version, user, limit)
    @router.get("/knowledge-documents/{document_id}/thumbnail")
    def thumbnail(document_id: str, version: int | None = Query(default=None, ge=1), page: int | None = Query(default=None, ge=1, le=200), user=Depends(authenticated), service=Depends(svc)):
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
    async def search(kb_id: str, data: SearchInput, user=Depends(authenticated), service=Depends(svc)):
        return await asyncio.to_thread(service.knowledge.search, [kb_id], user, data.query, data.limit, mode=data.mode, context_chars=data.context_chars)
    return router
