"""Bounded local artifact generation.

The native agent can already write files in its conversation workspace.  This
module adds one explicit, auditable path for spreadsheet exports and keeps the
image-generation boundary honest: no image is fabricated when an approved
provider adapter is not configured.
"""
from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends

from .db import now, uid
from .image_generation import (
    create_job,
    finish_job,
    generate_image as run_image_adapter,
    image_generation_capability,
    job_view,
    store_image_file,
)
from .models import ImageGenerationRequest, XlsxArtifactRequest
from .security import AppError, safe_name
from .service import file_view

MAX_SHEETS = 10
MAX_ROWS = 5000
MAX_COLUMNS = 50
MAX_CELL_CHARS = 20_000
FORMULA_PREFIX = re.compile(r"^[=+\-@]")


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        # Keep the export deterministic and useful without evaluating arbitrary
        # objects supplied by a model.
        import json
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _safe_cell(value) -> tuple[str, str]:
    text = _cell_text(value)
    if len(text) > MAX_CELL_CHARS:
        raise AppError(422, "ARTIFACT_CELL_TOO_LARGE", "Excel 单元格内容超过大小限制")
    # Never let a generated export turn user/model text into a spreadsheet
    # formula.  A leading apostrophe is rendered as ordinary text by Excel.
    if FORMULA_PREFIX.match(text):
        text = "'" + text
    return text, "inlineStr"


def _column_name(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _normalise_sheet(sheet: dict, index: int) -> tuple[str, list[str], list[list[object]]]:
    name = safe_name(str(sheet.get("name") or f"Sheet {index + 1}"))[:31] or f"Sheet {index + 1}"
    name = re.sub(r"[\[\]:*?/\\]", "_", name) or f"Sheet {index + 1}"
    raw_rows = sheet.get("rows") or []
    if not isinstance(raw_rows, list):
        raise AppError(422, "ARTIFACT_ROWS_INVALID", "Excel 工作表 rows 必须是数组")
    columns = sheet.get("columns") or []
    if not isinstance(columns, list) or any(not isinstance(value, str) or not value.strip() for value in columns):
        raise AppError(422, "ARTIFACT_COLUMNS_INVALID", "Excel columns 必须是非空字符串数组")
    columns = [value.strip()[:MAX_CELL_CHARS] for value in columns]
    if len(columns) > MAX_COLUMNS:
        raise AppError(422, "ARTIFACT_TOO_MANY_COLUMNS", "Excel 工作表列数超过限制")
    normalised: list[list[object]] = []
    if raw_rows and all(isinstance(row, dict) for row in raw_rows):
        if not columns:
            keys: list[str] = []
            for row in raw_rows:
                for key in row:
                    if str(key) not in keys:
                        keys.append(str(key))
            columns = keys[:MAX_COLUMNS]
        for row in raw_rows:
            normalised.append([row.get(column, "") for column in columns])
    else:
        if any(not isinstance(row, list) for row in raw_rows):
            raise AppError(422, "ARTIFACT_ROWS_INVALID", "Excel rows 必须全部是数组或全部是对象")
        width = max([len(row) for row in raw_rows] + [len(columns), 0])
        if width > MAX_COLUMNS:
            raise AppError(422, "ARTIFACT_TOO_MANY_COLUMNS", "Excel 工作表列数超过限制")
        if not columns:
            columns = [f"列 {_column_name(position + 1)}" for position in range(width)]
        normalised = [list(row) + [""] * (width - len(row)) for row in raw_rows]
        if len(columns) < width:
            columns.extend(f"列 {_column_name(position + 1)}" for position in range(len(columns), width))
    if len(normalised) > MAX_ROWS:
        raise AppError(422, "ARTIFACT_TOO_MANY_ROWS", "Excel 工作表行数超过限制")
    return name, columns, normalised


def xlsx_bytes(sheets: list[dict]) -> bytes:
    """Build a small Office Open XML workbook with no external dependencies."""
    if not isinstance(sheets, list) or not sheets or len(sheets) > MAX_SHEETS:
        raise AppError(422, "ARTIFACT_SHEETS_INVALID", "Excel 至少需要一个工作表，最多十个")
    values = [_normalise_sheet(sheet, index) for index, sheet in enumerate(sheets)]
    names = [value[0].casefold() for value in values]
    if len(set(names)) != len(names):
        raise AppError(422, "ARTIFACT_DUPLICATE_SHEET", "Excel 工作表名称不能重复")
    workbook_sheets = []
    workbook_rels = []
    content_overrides = []
    worksheet_files: list[tuple[str, bytes]] = []
    for index, (name, columns, rows) in enumerate(values, 1):
        sheet_rows = []
        all_rows = [columns] + rows if columns else rows
        for row_number, row in enumerate(all_rows, 1):
            cells = []
            for column_number, value in enumerate(row, 1):
                text, cell_type = _safe_cell(value)
                cell_ref = f"{_column_name(column_number)}{row_number}"
                cells.append(f'<c r={quoteattr(cell_ref)} t="{cell_type}"><is><t xml:space="preserve">{escape(text)}</t></is></c>')
            sheet_rows.append(f'<row r="{row_number}">' + "".join(cells) + "</row>")
        worksheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                     '<sheetData>' + "".join(sheet_rows) + '</sheetData></worksheet>').encode()
        worksheet_files.append((f"xl/worksheets/sheet{index}.xml", worksheet))
        workbook_sheets.append(f'<sheet name={quoteattr(name)} sheetId="{index}" r:id="rId{index}"/>')
        workbook_rels.append(f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>')
        content_overrides.append(f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    content = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>'
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
               + "".join(content_overrides) + '</Types>')
    # The workbook override above is intentionally kept as a normal element;
    # this assertion catches accidental malformed XML if it is edited later.
    if content.count("<Override") != len(values) + 1:
        raise RuntimeError("invalid xlsx content types")
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets>' + "".join(workbook_sheets) + '</sheets></workbook>').encode()
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>').encode()
    workbook_rels_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                         + "".join(workbook_rels) + '</Relationships>').encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml)
        for filename, payload in worksheet_files:
            archive.writestr(filename, payload)
    return output.getvalue()


def write_xlsx(root: Path, filename: str, sheets: list[dict]) -> tuple[Path, int]:
    """Write a generated workbook below a conversation's artifacts directory."""
    name = safe_name(filename)
    if not name.lower().endswith(".xlsx"):
        name += ".xlsx"
    if name in {".xlsx", "..xlsx"}:
        raise AppError(422, "ARTIFACT_FILENAME_INVALID", "Excel 文件名无效")
    directory = root / "artifacts"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = xlsx_bytes(sheets)
    path = (directory / name).resolve()
    if path.parent != directory.resolve():
        raise AppError(403, "PATH_DENIED", "产物路径无效")
    path.write_bytes(payload)
    return path, len(payload)


_MAIN_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_CELL_REF = re.compile(r"^([A-Z]+)([0-9]+)$")
_MAX_PREVIEW_ROWS = 200
_MAX_PREVIEW_COLUMNS = 50
_MAX_PREVIEW_SHEETS = 10
_MAX_PREVIEW_CELL_TEXT = 2_000


def _column_index(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + (ord(letter) - 64)
    return value


def _display_cell_text(value: str) -> tuple[str, bool]:
    text = value[:_MAX_PREVIEW_CELL_TEXT]
    # The exporter prefixes formula-like text with an apostrophe. The escape
    # is intentionally not evaluated; the preview shows the original business
    # text and records that it was escaped rather than executed.
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@":
        return text[1:], True
    return text, False


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    values = []
    for item in root.findall(f"{_MAIN_NS}si"):
        parts = [node.text or "" for node in item.iter(f"{_MAIN_NS}t")]
        values.append("".join(parts))
    return values


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return []
    targets = {node.get("Id"): node.get("Target", "") for node in rels.findall("{http://schemas.openxmlformats.org/package/2006/relationships}Relationship")}
    result = []
    for sheet in workbook.findall(f"{_MAIN_NS}sheets/{_MAIN_NS}sheet"):
        rel_id = sheet.get(f"{_REL_NS}id")
        target = targets.get(rel_id, "")
        if target.startswith("/"):
            target = target.lstrip("/")
        elif not target.startswith("xl/"):
            target = "xl/" + target
        if target:
            result.append((sheet.get("name") or "Sheet", target))
    return result


def _preview_sheet(archive: zipfile.ZipFile, name: str, target: str, shared: list[str], max_rows: int, max_columns: int) -> dict:
    try:
        root = ET.fromstring(archive.read(target))
    except (KeyError, ET.ParseError):
        return {"name": name, "columns": [], "rows": [], "row_count": 0, "truncated": False, "formula_cells": 0, "escaped_text_cells": 0}
    grid: list[list[str]] = []
    formula_cells = 0
    escaped_text_cells = 0
    for row in root.findall(f"{_MAIN_NS}sheetData/{_MAIN_NS}row"):
        values: list[str] = []
        for cell in row.findall(f"{_MAIN_NS}c"):
            ref = cell.get("r", "")
            match = _CELL_REF.fullmatch(ref)
            column = _column_index(match.group(1)) - 1 if match else len(values)
            if column >= max_columns:
                continue
            while len(values) <= column:
                values.append("")
            formula = cell.find(f"{_MAIN_NS}f")
            if formula is not None:
                formula_cells += 1
            cell_type = cell.get("t", "")
            if cell_type == "inlineStr":
                text = "".join(node.text or "" for node in cell.iter(f"{_MAIN_NS}t"))
            else:
                value = cell.find(f"{_MAIN_NS}v")
                raw = value.text if value is not None and value.text is not None else ""
                if cell_type == "s":
                    try:
                        text = shared[int(raw)]
                    except (ValueError, IndexError):
                        text = ""
                elif cell_type == "b":
                    text = "TRUE" if raw == "1" else "FALSE" if raw == "0" else raw
                elif formula is not None and not raw:
                    text = "（公式未计算）"
                else:
                    text = raw
            text, escaped = _display_cell_text(text)
            escaped_text_cells += int(escaped)
            values[column] = text
        grid.append(values)
    truncated = len(grid) > max_rows
    grid = grid[:max_rows]
    width = min(max((len(row) for row in grid), default=0), max_columns)
    if width == 0:
        return {"name": name, "columns": [], "rows": [], "row_count": 0, "truncated": truncated, "formula_cells": formula_cells, "escaped_text_cells": escaped_text_cells}
    header = [grid[0][index] if index < len(grid[0]) else "" for index in range(width)]
    if not any(header):
        header = [f"列 {_column_name(index + 1)}" for index in range(width)]
        body = [[row[index] if index < len(row) else "" for index in range(width)] for row in grid]
    else:
        body = [[row[index] if index < len(row) else "" for index in range(width)] for row in grid[1:]]
    return {"name": name, "columns": header, "rows": body, "row_count": len(body), "truncated": truncated, "formula_cells": formula_cells, "escaped_text_cells": escaped_text_cells}


def xlsx_preview(path: Path, *, max_rows: int = _MAX_PREVIEW_ROWS, max_sheets: int = _MAX_PREVIEW_SHEETS, max_columns: int = _MAX_PREVIEW_COLUMNS) -> dict:
    """Read a workbook locally for an interactive preview without evaluation."""
    max_rows = max(1, min(int(max_rows), _MAX_PREVIEW_ROWS))
    max_sheets = max(1, min(int(max_sheets), _MAX_PREVIEW_SHEETS))
    max_columns = max(1, min(int(max_columns), _MAX_PREVIEW_COLUMNS))
    try:
        with zipfile.ZipFile(path) as archive:
            shared = _shared_strings(archive)
            targets = _sheet_targets(archive)
            if not targets:
                targets = [(f"Sheet {index + 1}", name) for index, name in enumerate(sorted(item for item in archive.namelist() if item.startswith("xl/worksheets/sheet") and item.endswith(".xml")))]
            sheets = [_preview_sheet(archive, name, target, shared, max_rows, max_columns) for name, target in targets[:max_sheets]]
    except (zipfile.BadZipFile, OSError) as error:
        raise AppError(422, "XLSX_PREVIEW_INVALID", "无法读取这个 Excel 文件，请下载后核对") from error
    return {
        "sheets": sheets,
        "sheet_count": len(targets),
        "truncated": len(targets) > max_sheets or any(sheet["truncated"] for sheet in sheets),
        "evaluated_formulas": False,
        "formula_cells": sum(sheet["formula_cells"] for sheet in sheets),
        "escaped_text_cells": sum(sheet["escaped_text_cells"] for sheet in sheets),
        "max_rows": max_rows,
        "max_columns": max_columns,
    }


def create_artifact_router(svc, authenticated):
    router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])

    @router.get("/image-capability")
    def image_capability(user=Depends(authenticated), service=Depends(svc)):
        return image_generation_capability(service.settings, service.catalog)

    @router.post("/xlsx", status_code=201)
    def export_xlsx(data: XlsxArtifactRequest, user=Depends(authenticated), service=Depends(svc)):
        payload = xlsx_bytes([sheet.model_dump() for sheet in data.sheets])
        file_id = uid()
        name = safe_name(data.filename)
        if not name.lower().endswith(".xlsx"):
            name += ".xlsx"
        path = service.settings.data_dir / "files" / file_id
        path.write_bytes(payload)
        service.db.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,NULL)", (file_id, user["id"], name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", len(payload), str(path), now()))
        return {"file": file_view(service.require("files", file_id, user)), "format": "xlsx", "download_path": f"/api/v1/files/{file_id}/content", "preview_path": f"/api/v1/files/{file_id}/xlsx-preview"}

    @router.post("/image")
    async def generate_image(data: ImageGenerationRequest, user=Depends(authenticated), service=Depends(svc)):
        conversation_id = data.conversation_id
        if conversation_id:
            conversation = service.require("conversations", conversation_id, user)
            if conversation["archived"]:
                raise AppError(409, "CONVERSATION_ARCHIVED", "归档会话不能生成图片")
        capability = image_generation_capability(service.settings, service.catalog)
        job_id = create_job(service, user_id=user["id"], conversation_id=conversation_id, prompt=data.prompt, model_id=data.model_id or capability.get("model_id") or "")
        if not capability["available"]:
            finish_job(service, job_id, status="unavailable", error_code="IMAGE_MODEL_UNAVAILABLE", error_message=capability["reason"])
            raise AppError(503, "IMAGE_MODEL_UNAVAILABLE", capability["reason"])
        try:
            result = await run_image_adapter(service.settings, service.catalog, data.prompt, model_id=data.model_id, size=data.size)
        except AppError as error:
            finish_job(service, job_id, status="failed", error_code=error.code, error_message=error.message)
            raise
        file_id = store_image_file(
            service,
            user_id=user["id"],
            conversation_id=conversation_id,
            name=f"图片-{job_id[:8]}{result['extension']}",
            payload=result["image_bytes"],
            mime_type=result["mime_type"],
        )
        finish_job(service, job_id, status="succeeded", file_id=file_id, nbytes=len(result["image_bytes"]), usage=result["usage"])
        file = file_view(service.require("files", file_id, user))
        return {
            "job_id": job_id,
            "file": file,
            "format": result["extension"].lstrip("."),
            "download_path": f"/api/v1/files/{file_id}/content",
            "preview_path": f"/api/v1/files/{file_id}/content",
            "usage": result["usage"],
            "capability": capability,
        }

    @router.get("/image-jobs")
    def image_jobs(limit: int = 20, user=Depends(authenticated), service=Depends(svc)):
        limit = max(1, min(int(limit), 50))
        rows = service.db.all("SELECT * FROM image_generation_jobs WHERE user_id=? ORDER BY created_at DESC,id DESC LIMIT ?", (user["id"], limit))
        return [job_view(row) for row in rows]

    return router
