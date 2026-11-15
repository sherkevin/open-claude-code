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
    "CAPABILITY_DISABLED": "当前任务或助手未获准使用此联网能力。",
    "CAPABILITY_DOMAIN_DENIED": "目标地址不在当前允许的域名范围内，未提供可核验正文。",
    "CAPABILITY_EGRESS_DENIED": "管理员已禁止此能力的数据外发，未提供可用网页结果。",
    "CAPABILITY_TIMEOUT": "联网能力调用超时，未取得可用结果，请稍后重试。",
    "CAPABILITY_PROVIDER_FAILED": "联网服务暂时不可用，未取得可用结果，请稍后重试。",
    "TOOL_DENIED": "此工具未获授权，未取得网页结果。",
    "RUN_REQUIRED": "当前任务已结束或不可用，未继续读取网页。",
}


def workspace_web_error(tool_name, error=None):
    if tool_name not in WORKSPACE_WEB_TOOLS:
        return None
    default = ({"error_code": "WEB_READ_FAILED", "error_message": "网页读取未完成，未取得可核验正文，请尝试其他来源。"}
               if tool_name.endswith("web_read") else
               {"error_code": "WEB_SEARCH_FAILED", "error_message": "网页搜索未完成，未取得可核验结果，请稍后重试。"})
    if not isinstance(error, AppError) or not isinstance(error.code, str) or error.code not in _MESSAGES:
        return default
    message = _MESSAGES[error.code]
    if error.code == "WEB_FETCH_FAILED" and isinstance(error.message, str):
        status = re.fullmatch(r"网页返回 HTTP ([45][0-9]{2})，可尝试其他来源", error.message)
        if status:
            message = f"目标网页返回 HTTP {status[1]}，未取得正文，请尝试其他来源。"
    return {"error_code": error.code, "error_message": message}
