"""Public failure summaries for errors caught by the host's web handlers.

Never parse an external tool response or copy an exception message into an
event. The one variable field accepted below is an HTTP status from our own
exact WEB_FETCH_FAILED template; response bodies and URLs are never retained.
"""
from __future__ import annotations

import re

from .security import AppError

WORKSPACE_WEB_TOOLS = frozenset({"mcp__workspace__web_read", "mcp__workspace__web_search"})
_MESSAGES = {
    "WEB_TIMEOUT": "网页读取超时，未取得正文，请尝试其他来源。",
    "WEB_SEARCH_TIMEOUT": "网页搜索超时，请稍后重试。",
    "WEB_NETWORK_UNAVAILABLE": "网络连接失败，未取得网页结果，请检查联网配置或尝试其他来源。",
    "WEB_DNS_UNAVAILABLE": "公开域名解析失败，未取得网页结果，请稍后重试或联系管理员。",
    "WEB_DNS_CONFIG_INVALID": "网页域名解析配置无效，请联系管理员。",
    "WEB_EGRESS_CONFIG_INVALID": "网页出站配置无效，请联系管理员。",
    "WEB_URL_DENIED": "网页地址未通过公开网络安全检查，未读取正文。",
    "WEB_QUERY_INVALID": "搜索词无效，请调整后重试。",
    "WEB_FETCH_FAILED": "目标网页未返回可用结果，未取得正文，请尝试其他来源。",
    "WEB_SEARCH_UNAVAILABLE": "搜索服务暂未返回可用结果，请稍后重试。",
    "WEB_SEARCH_PROVIDER_FAILED": "搜索服务请求失败，请稍后重试。",
    "WEB_SEARCH_PROVIDER_UNAVAILABLE": "网页服务尚未配置，请联系管理员。",
    "WEB_SEARCH_RESPONSE_TOO_LARGE": "搜索返回内容超过安全上限，未作为可用结果使用。",
    "WEB_PAGE_TOO_LARGE": "网页内容超过安全上限，请改用具体文本页面。",
    "WEB_REDIRECT_LIMIT": "网页重定向次数过多，未读取正文，请尝试其他来源。",
    "WEB_FORMAT_UNSUPPORTED": "该链接不是支持的文本网页，未读取正文。",
    "WEB_EMPTY": "该网页未提供可读取的正文，请尝试其他来源。",
    "WEB_ACCESS_CHALLENGE": "网页要求访问验证，未取得正文；请在浏览器查看或使用其他相关公开来源。",
    "WEB_JAVASCRIPT_REQUIRED": "网页只提供需要 JavaScript 加载的页面外壳，未取得正文；请使用其他相关公开来源。",
    "CAPABILITY_DISABLED": "当前任务或助手未获准使用此联网能力。",
    "CAPABILITY_DOMAIN_DENIED": "目标地址不在当前允许的域名范围内，未提供可核验正文。",
    "CAPABILITY_EGRESS_DENIED": "管理员已禁止此能力的数据外发，未提供可用网页结果。",
    "CAPABILITY_CHANGED": "联网能力配置已更新，请重新确认当前任务权限。",
    "CAPABILITY_CREDENTIAL_CHANGED": "联网能力凭据已更新，请重新确认当前任务权限。",
    "CAPABILITY_CONTEXT_DENIED": "当前调用与会话授权不匹配，未读取网页。",
    "CAPABILITY_TIMEOUT": "联网能力调用超时，未取得可用结果，请稍后重试。",
    "CAPABILITY_PROVIDER_FAILED": "联网服务暂时不可用，未取得可用结果，请稍后重试。",
    "TOOL_DENIED": "此工具未获授权，未取得网页结果。",
    "RUN_REQUIRED": "当前任务已结束或不可用，未继续读取网页。",
}


class RecentWebReadFailure(AppError):
    """Host-only suppression receipt; retain only a whitelisted public reason."""
    def __init__(self, error):
        self.public_reason = workspace_web_error("mcp__workspace__web_read", error)
        super().__init__(409, "WEB_RECENT_FAILURE", "近期读取此链接已失败，未重复发起网络请求")


def _read_recovery(details):
    code = details["error_code"]
    status = details.get("http_status")
    kind = {403: "access_denied", 404: "not_found", 410: "not_found", 429: "rate_limited"}.get(status)
    if kind is None:
        kind = {"WEB_TIMEOUT": "timeout", "CAPABILITY_TIMEOUT": "timeout",
                "WEB_NETWORK_UNAVAILABLE": "network_unavailable", "WEB_DNS_UNAVAILABLE": "dns_unavailable",
                "WEB_ACCESS_CHALLENGE": "access_verification", "WEB_JAVASCRIPT_REQUIRED": "javascript_required",
                "WEB_EMPTY": "no_readable_body"}.get(code, "read_unavailable")
    blocked = code.startswith(("CAPABILITY_", "TOOL_", "RUN_")) and code != "CAPABILITY_TIMEOUT"
    blocked = blocked or code in {"WEB_URL_DENIED", "WEB_DNS_CONFIG_INVALID", "WEB_EGRESS_CONFIG_INVALID"}
    return {**details, "failure_kind": kind, "body_available": False,
            "next_action": {"type": "check_authorization_or_configuration" if blocked else "consider_related_sources",
                "retry_same_url_now": False, "max_alternative_reads": 0 if blocked else 2,
                "verify_topic_before_search": True, "automatic_search_performed": False,
                "original_url_required": "report_unavailable",
                "citation_rule": "cite_only_successfully_read_bodies; label_search_excerpts_as_excerpts"}}


def workspace_web_error(tool_name, error=None):
    if tool_name not in WORKSPACE_WEB_TOOLS:
        return None
    default = ({"error_code": "WEB_READ_FAILED", "error_message": "网页读取未完成，未取得可核验正文，请尝试其他来源。"}
               if tool_name.endswith("web_read") else
               {"error_code": "WEB_SEARCH_FAILED", "error_message": "网页搜索未完成，未取得可核验结果，请稍后重试。"})
    reading = tool_name.endswith("web_read")
    if reading and isinstance(error, RecentWebReadFailure):
        return {**error.public_reason, "retry_suppressed": True,
                "error_message": error.public_reason["error_message"] + " 本任务近期已读取失败，未重复请求。"}
    if not isinstance(error, AppError) or not isinstance(error.code, str) or error.code not in _MESSAGES:
        return _read_recovery(default) if reading else default
    message = _MESSAGES[error.code]
    details = {"error_code": error.code}
    if error.code == "WEB_FETCH_FAILED" and isinstance(error.message, str):
        status = re.fullmatch(r"网页返回 HTTP ([45][0-9]{2})，可尝试其他来源", error.message)
        if status:
            details["http_status"] = int(status[1])
            description = {403: "目标网页拒绝访问", 404: "目标网页不存在或已移除", 410: "目标网页已移除",
                           429: "目标网页限制了请求频率"}.get(int(status[1]), "目标网页请求失败")
            message = f"{description}（HTTP {status[1]}），未取得正文，请尝试其他来源。"
    details["error_message"] = message
    return _read_recovery(details) if reading else details
