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
from xml.sax.saxutils import escape, quoteattr

from fastapi import APIRouter, Depends

from .db import now, uid
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


def image_generation_capability(settings, catalog) -> dict:
    """Describe the approved image route without exposing credentials."""
    model_id = getattr(settings, "image_model_id", "")
    model = catalog.models.get(model_id) if model_id else None
    if not getattr(settings, "image_generation_enabled", False):
        reason = "管理员尚未启用图片生成；不会伪造图片或调用未授权模型"
    elif not model_id or not model:
        reason = "未找到已授权的图片模型目录项"
    elif "image_generation" not in model.get("capabilities", []):
        reason = "当前模型目录未声明 image_generation 能力"
    elif not getattr(settings, "image_generator_url", ""):
        reason = "尚未配置已批准的图片生成适配器地址"
    else:
        # A provider adapter is intentionally not guessed from a generic chat
        # endpoint. It needs a separately reviewed request/response contract.
        reason = "图片生成适配器尚未完成验收"
    return {"available": False, "model_id": model_id or None, "status": "pending_authorization", "reason": reason}


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
        return {"file": file_view(service.require("files", file_id, user)), "format": "xlsx", "download_path": f"/api/v1/files/{file_id}/content"}

    @router.post("/image", status_code=501)
    def generate_image(data: ImageGenerationRequest, user=Depends(authenticated), service=Depends(svc)):
        capability = image_generation_capability(service.settings, service.catalog)
        raise AppError(503, "IMAGE_MODEL_UNAVAILABLE", capability["reason"])

    return router
