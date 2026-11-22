"""Bounded preview pages for authorized original files and exported artifacts."""
from pathlib import Path

from .harness_attachments import TEXT_SUFFIXES, _read_upload, _text
from .security import AppError

PREVIEW_PAGE_CHARS = 40_000
PREVIEW_OFFICE = {".docx", ".xlsx"}


def file_preview(service, item_id, user, page):
    row = service.require("files", item_id, user)
    suffix = Path(row["name"]).suffix.lower()
    if suffix not in TEXT_SUFFIXES | PREVIEW_OFFICE:
        raise AppError(422, "PREVIEW_UNSUPPORTED", "此文件类型暂不提供分页预览")
    if row["size"] > 50 * 1024 * 1024:
        raise AppError(413, "PREVIEW_TOO_LARGE", "文件较大，请下载后查看")
    original = Path(row["path"])
    data_root = Path(service.settings.data_dir)
    if original.parent.resolve() == (data_root / "files").resolve():
        data = _read_upload(service, row)
    else:
        path = original.resolve()
        if not path.is_relative_to(data_root.resolve() / "workspaces") or path != original.absolute():
            raise AppError(404, "NOT_FOUND", "附件不可访问")
        try:
            with path.open("rb") as stream:
                data = stream.read(row["size"] + 1)
        except OSError:
            raise AppError(404, "NOT_FOUND", "附件不可访问") from None
        if len(data) != row["size"]:
            raise AppError(409, "PREVIEW_CHANGED", "文件已变化，请重新打开预览")
    text, pages = _text(service, row, data, enforce_inline_limit=False)
    sections = [(f"第 {item['page']} 页或工作表", item["text"]) for item in pages] if suffix in PREVIEW_OFFICE else [("", text)]
    segments = [(label, content[start:start + PREVIEW_PAGE_CHARS]) for label, content in sections
                for start in range(0, len(content) or 1, PREVIEW_PAGE_CHARS)]
    if page > len(segments):
        raise AppError(404, "PREVIEW_PAGE_NOT_FOUND", "预览页不存在")
    label, content = segments[page - 1]
    return {"page": page, "total_pages": len(segments), "label": label, "text": content}
