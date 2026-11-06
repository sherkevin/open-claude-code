from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def password_hash(password: str) -> str:
    if len(password) < 10 or len(password) > 256:
        raise AppError(422, "PASSWORD_POLICY", "密码长度须为 10–256 个字符")
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.b64encode(salt + value).decode()


def password_matches(password: str, encoded: str) -> bool:
    try:
        if len(password) > 256:
            return False
        value = base64.b64decode(encoded)
        actual = hashlib.scrypt(password.encode(), salt=value[:16], n=16384, r=8, p=1)
        return hmac.compare_digest(actual, value[16:])
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SecretStore:
    def __init__(self, root: Path):
        configured = os.getenv("AGENT_SECRET_KEY")
        if os.getenv("AGENT_SECRET_KEY_FILE"):
            configured = Path(os.environ["AGENT_SECRET_KEY_FILE"]).read_text().strip()
        if configured:
            self.fernet = Fernet(configured.encode())
            return
        key_path = root / "encryption.key"
        if not key_path.exists():
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(Fernet.generate_key())
            except FileExistsError:
                pass
        self.fernet = Fernet(key_path.read_bytes().strip())

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str | None) -> str | None:
        return self.fernet.decrypt(value.encode()).decode() if value else None


SENSITIVE = re.compile(r"(^|[_-])(password|passwd|secret|token|authorization|api.?key|cookie|credentials?)($|[_-])", re.I)


def redact(value, truncate=True):
    if isinstance(value, dict):
        return {str(k): "[redacted]" if SENSITIVE.search(str(k)) else redact(v, truncate) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, truncate) for v in (value[:100] if truncate else value)]
    if isinstance(value, str):
        return value[:16000] if truncate else value
    return value


def validate_mcp_url(value: str, allowed_hosts: tuple[str, ...]) -> str:
    url = urlsplit(value)
    if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.fragment:
        raise AppError(422, "INVALID_MCP_URL", "请输入有效 HTTP MCP 地址，不允许地址内嵌凭据")
    if url.hostname.lower() not in allowed_hosts:
        raise AppError(422, "MCP_HOST_NOT_ALLOWED", "该 MCP 主机尚未列入部署允许名单，请联系管理员")
    if url.query:
        raise AppError(422, "INVALID_MCP_URL", "请通过认证字段配置凭据，不在 MCP 地址中添加查询参数")
    return value


def safe_name(name: str) -> str:
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")[:180]
    return name or "attachment"


def workspace_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        relative = resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise AppError(403, "PATH_DENIED", "文件路径不在当前会话工作区内") from exc
    if any(part.startswith(".") for part in relative.parts):
        raise AppError(403, "PATH_DENIED", "不能访问运行配置目录")
    return resolved
