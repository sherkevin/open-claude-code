"""Reviewed image-generation adapter and its fail-closed boundary.

The only accepted provider contract is an OpenAI-images-compatible
``POST {url}`` returning one ``b64_json`` image.  A generic chat endpoint is
never treated as an image provider.  Every attempt, including a request that
is rejected before any network call, is recorded as a bounded job so the UI
can show usage and an explicit error instead of a placeholder image.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .db import now, uid
from .security import AppError, safe_name

IMAGE_CONTRACT = "openai-images-v1"
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
    b"RIFF": ("image/webp", ".webp"),
}
_SIZE = re.compile(r"^[1-9][0-9]{2,4}x[1-9][0-9]{2,4}$")
_MAX_PROMPT = 2_000


def _capability(settings, catalog) -> dict:
    model_id = getattr(settings, "image_model_id", "") or ""
    model = catalog.models.get(model_id) if model_id else None
    contract = getattr(settings, "image_generator_contract", IMAGE_CONTRACT) or ""
    url = getattr(settings, "image_generator_url", "") or ""
    endpoint_valid = False
    if url:
        try:
            parsed = urlsplit(url)
            endpoint_valid = (
                parsed.scheme in {"http", "https"}
                and bool(parsed.hostname)
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and not parsed.query
                and parsed.hostname.lower() in tuple(getattr(settings, "mcp_hosts", ()))
            )
        except ValueError:
            endpoint_valid = False
    available = False
    status = "pending_authorization"
    if not getattr(settings, "image_generation_enabled", False):
        reason = "管理员尚未启用图片生成；不会伪造图片或调用未授权模型"
    elif not model_id or not model:
        reason = "未找到已授权的图片模型目录项"
    elif "image_generation" not in model.get("capabilities", []):
        reason = "当前模型目录未声明 image_generation 能力"
    elif contract != IMAGE_CONTRACT:
        reason = "图片生成适配器契约尚未验收，只接受 openai-images-v1"
    elif not url:
        reason = "尚未配置已批准的图片生成适配器地址"
    elif not endpoint_valid:
        reason = "图片生成适配器地址不在允许名单内或格式无效"
    else:
        available = True
        status = "available"
        reason = "已配置经过审核的图片生成适配器；每次调用会记录用量和错误"
    return {
        "available": available,
        "model_id": model_id or None,
        "status": status,
        "reason": reason,
        "contract": contract or None,
        "endpoint_configured": endpoint_valid,
        "credential_configured": bool(getattr(settings, "image_generator_api_key", "")),
    }


def image_generation_capability(settings, catalog) -> dict:
    """Describe the approved image route without exposing URL or credentials."""
    return _capability(settings, catalog)


def _image_type(payload: bytes) -> tuple[str, str]:
    for magic, value in _IMAGE_MAGIC.items():
        if payload.startswith(magic):
            return value
    raise AppError(502, "IMAGE_RESPONSE_INVALID", "图片服务返回的不是可识别的 PNG、JPEG 或 WebP 图片")


def _bounded_size(settings, value: str | None) -> str:
    size = value or "1024x1024"
    if not _SIZE.fullmatch(size):
        raise AppError(422, "IMAGE_SIZE_INVALID", "图片尺寸格式无效")
    return size


def _usage(payload: dict, nbytes: int, model_id: str) -> dict:
    provided = payload.get("usage")
    usage = {"images": 1, "bytes": nbytes, "model_id": model_id, "estimated_cost_usd": None, "currency": "USD"}
    if isinstance(provided, dict):
        for key in ("input_tokens", "output_tokens", "total_tokens", "images", "number_of_images"):
            value = provided.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1_000_000:
                usage[key] = value
        cost = provided.get("estimated_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 <= cost <= 1_000:
            usage["estimated_cost_usd"] = float(cost)
    return usage


async def generate_image(settings, catalog, prompt: str, *, model_id: str | None = None, size: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Call the reviewed adapter and return validated image bytes plus usage."""
    capability = _capability(settings, catalog)
    if not capability["available"]:
        raise AppError(503, "IMAGE_MODEL_UNAVAILABLE", capability["reason"])
    if not isinstance(prompt, str) or not prompt.strip():
        raise AppError(422, "IMAGE_PROMPT_INVALID", "请输入图片描述")
    prompt = prompt.strip()[:_MAX_PROMPT]
    selected = model_id or capability["model_id"]
    if selected != capability["model_id"]:
        raise AppError(422, "IMAGE_MODEL_NOT_ALLOWED", "只能使用管理员批准的图片模型")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    key = getattr(settings, "image_generator_api_key", "")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = {
        "model": selected,
        "prompt": prompt,
        "n": 1,
        "size": _bounded_size(settings, size),
        "response_format": "b64_json",
    }
    timeout = float(getattr(settings, "image_generation_timeout_seconds", 60))
    max_bytes = int(getattr(settings, "image_generation_max_bytes", 10 * 1024 * 1024))
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            response = await client.post(getattr(settings, "image_generator_url", ""), headers=headers, json=request)
    except httpx.TimeoutException as error:
        raise AppError(504, "IMAGE_GENERATION_TIMEOUT", "图片生成服务响应超时，请稍后重试") from error
    except httpx.HTTPError as error:
        raise AppError(502, "IMAGE_GENERATION_FAILED", "图片生成服务暂时不可达") from error
    if response.status_code >= 400:
        raise AppError(502, "IMAGE_GENERATION_FAILED", f"图片生成服务返回错误（HTTP {response.status_code}）")
    try:
        payload = response.json()
    except ValueError as error:
        raise AppError(502, "IMAGE_RESPONSE_INVALID", "图片生成服务返回了无法解析的结果") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
    encoded = first.get("b64_json") if isinstance(first, dict) else None
    if not isinstance(encoded, str) or not encoded:
        if isinstance(first, dict) and first.get("url"):
            raise AppError(502, "IMAGE_RESPONSE_UNSUPPORTED", "图片服务只返回了外链，未启用的外链下载已被拒绝")
        raise AppError(502, "IMAGE_RESPONSE_INVALID", "图片生成服务没有返回可保存的图片数据")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise AppError(502, "IMAGE_RESPONSE_INVALID", "图片生成服务返回的图片编码无效") from error
    if not image:
        raise AppError(502, "IMAGE_RESPONSE_INVALID", "图片生成服务返回了空图片")
    if len(image) > max_bytes:
        raise AppError(413, "IMAGE_TOO_LARGE", "生成的图片超过保存上限")
    mime_type, extension = _image_type(image)
    return {
        "image_bytes": image,
        "mime_type": mime_type,
        "extension": extension,
        "model_id": selected,
        "usage": _usage(payload, len(image), selected),
        "request_id": response.headers.get("x-request-id"),
    }


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def create_job(service, *, user_id: str, conversation_id: str | None, prompt: str, model_id: str) -> str:
    job_id, timestamp = uid(), now()
    service.db.execute(
        "INSERT INTO image_generation_jobs(id,user_id,conversation_id,prompt_digest,prompt_chars,model_id,status,error_code,error_message,file_id,bytes,usage_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, user_id, conversation_id, prompt_digest(prompt), len(prompt), model_id or "", "running", None, None, None, None, "{}", timestamp, None),
    )
    return job_id


def finish_job(service, job_id: str, *, status: str, error_code: str | None = None, error_message: str | None = None, file_id: str | None = None, nbytes: int | None = None, usage: dict | None = None) -> None:
    import json

    service.db.execute(
        "UPDATE image_generation_jobs SET status=?,error_code=?,error_message=?,file_id=?,bytes=?,usage_json=?,completed_at=? WHERE id=?",
        (status, error_code, (error_message or "")[:500] or None, file_id, nbytes, json.dumps(usage or {}, ensure_ascii=False, separators=(",", ":")), now(), job_id),
    )


def job_view(row: dict) -> dict:
    import json

    try:
        usage = json.loads(row.get("usage_json") or "{}")
    except (TypeError, ValueError):
        usage = {}
    return {
        "id": row["id"],
        "conversation_id": row.get("conversation_id"),
        "prompt_digest": row["prompt_digest"],
        "prompt_chars": row["prompt_chars"],
        "model_id": row["model_id"],
        "status": row["status"],
        "error_code": row.get("error_code"),
        "error_message": row.get("error_message"),
        "file_id": row.get("file_id"),
        "bytes": row.get("bytes"),
        "usage": usage,
        "created_at": row["created_at"],
        "completed_at": row.get("completed_at"),
    }


def store_image_file(service, *, user_id: str, conversation_id: str | None, name: str, payload: bytes, mime_type: str) -> str:
    """Persist one image in the user's file area and return its file id."""
    digest = hashlib.sha256(payload).hexdigest()
    file_id = uid()
    safe = safe_name(name)
    stored = service.settings.data_dir / "files" / f"{file_id}-{digest}"
    stored.write_bytes(payload)
    service.db.execute(
        "INSERT INTO files VALUES(?,?,?,?,?,?,?,?)",
        (file_id, user_id, safe, mime_type, len(payload), str(stored), now(), conversation_id),
    )
    return file_id


def write_image(root: Path, name: str, payload: bytes) -> tuple[Path, int]:
    """Write a generated image below a conversation's artifacts directory."""
    directory = root / "artifacts"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = safe_name(name)
    if not filename:
        raise AppError(422, "IMAGE_FILENAME_INVALID", "图片文件名无效")
    path = (directory / filename).resolve()
    if path.parent != directory.resolve():
        raise AppError(403, "PATH_DENIED", "图片产物路径无效")
    path.write_bytes(payload)
    return path, len(payload)
