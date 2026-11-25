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
DELEGATE_TASKS_TOOL = "mcp__workspace__delegate_tasks"


def tool_risk_profile(tool_name: str) -> dict[str, object]:
    """Return the conservative, product-level approval profile for a tool.

    This is intentionally a small classification layer rather than a second
    authorization system. ``authorize_tool`` still checks the current
    conversation, assistant and connection grants on every call. Unknown or
    external tools default to the stricter profile so a newly discovered tool
    cannot accidentally inherit a read-only policy.
    """
    if tool_name in {"Read", "Glob", "Grep", "mcp__workspace__knowledge_search", "mcp__workspace__knowledge_read", "mcp__workspace__web_search", "mcp__workspace__web_read", "mcp__workspace__file_read", "mcp__workspace__file_search", "mcp__workspace__memory_list", "mcp__workspace__memory_read"}:
        return {
            "risk_level": "low",
            "approval_policy": "safe_read",
            "impact": "只读取当前会话已授权的数据，不修改工作区或外部系统",
        }
    if tool_name in {"Write", "Edit", "mcp__workspace__file_write"}:
        return {
            "risk_level": "medium",
            "approval_policy": "user_confirmation",
            "impact": "修改当前会话工作区或记忆，执行前需要用户明确确认",
        }
    if tool_name in {"mcp__workspace__schedule_list","mcp__workspace__schedule_view"}:
        return {"risk_level":"low","approval_policy":"safe_read","impact":"只读取当前人工会话的定时计划，不修改或执行任务"}
    if tool_name == "mcp__workspace__schedule_edit":
        return {"risk_level":"medium","approval_policy":"user_confirmation",
                "impact":"修改或取消当前会话的未来计划；须本人确认完整变更，不追溯修改历史"}
    if tool_name == "mcp__workspace__schedule_create":
        return {"risk_level":"medium","approval_policy":"user_confirmation",
                "impact":"为当前会话创建持续运行计划，须本人确认完整指令、规则和首次执行时间；每次执行仍沿用业务审批"}
    if tool_name == "mcp__workspace__memory_write":
        return {"risk_level": "medium", "approval_policy": "user_confirmation",
                "impact": "完整内容须所属用户人工审阅；本轮成功并关闭执行器后才提交记忆并隔离旧上下文"}
    if tool_name == "mcp__workspace__generate_xlsx":
        return {
            "risk_level": "medium",
            "approval_policy": "user_confirmation",
            "impact": "在当前会话产物目录生成可下载的 Excel 文件",
        }
    if tool_name == "mcp__workspace__generate_image":
        return {
            "risk_level": "high",
            "approval_policy": "deny_by_default",
            "impact": "图片生成依赖单独授权的模型适配器，未验收时默认拒绝",
        }
    if tool_name == "mcp__workspace__ocr":
        return {
            "risk_level": "medium",
            "approval_policy": "user_confirmation",
            "impact": "识别当前会话已授权的附件；选定文件可能发送到外部识别服务，识别结果仅作为未受信任资料",
        }
    if tool_name == "mcp__workspace__image_search":
        return {
            "risk_level": "medium",
            "approval_policy": "user_confirmation",
            "impact": "检索外部图片；检索词或选定的授权图片可能发送到外部服务，返回链接不代表正文已读、图片已下载或版权可用",
        }
    if tool_name.startswith("mcp__"):
        return {
            "risk_level": "high",
            "approval_policy": "connection_policy",
            "impact": "调用外部业务系统，可能产生业务副作用；默认按连接策略审批",
        }
    return {
        "risk_level": "high",
        "approval_policy": "deny_by_default",
        "impact": "未知工具默认拒绝，不能仅凭用户确认绕过服务端授权",
    }


def redact(value, truncate=True):
    if isinstance(value, dict):
        return {str(k): "[redacted]" if str(k) == "__oc_capability_ticket" or SENSITIVE.search(str(k)) else redact(v, truncate) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, truncate) for v in (value[:100] if truncate else value)]
    if isinstance(value, str):
        return value[:16000] if truncate else value
    return value


def public_tool_arguments(tool_name, arguments):
    if tool_name == DELEGATE_TASKS_TOOL:
        return {"tasks": "[redacted]"}
    return redact(arguments)


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
