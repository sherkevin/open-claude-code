"""Make compact labels from public interface concepts, never from free-text excerpts."""

import re


# Each entry describes one concept, not one possible task. Both Chinese and
# English aliases map to the same public label. Identifiers and names supplied
# by a user are never used as display strings.
_OBJECTS = {
    "订单": "order orders", "发票": "invoice invoices", "账单": "bill billing",
    "库存": "inventory stock", "商品": "产品 product products", "客户": "customer customers",
    "合同": "contract contracts", "付款": "支付 payment payments", "报表": "报告 report reports",
    "表格": "spreadsheet spreadsheets", "表单": "form forms", "模板": "template templates",
    "附件": "attachment attachments", "文件": "file files", "文档": "document documents",
    "图片": "图像 image images", "视频": "video", "音频": "audio", "头像": "avatar",
    "表单字段": "字段 field fields", "列表": "list", "图表": "chart charts",
    "按钮": "button buttons", "页面": "page pages", "菜单": "menu menus",
    "账号": "帐号 账户 account accounts", "用户": "user users", "权限": "permission permissions",
    "密码": "password", "验证码": "captcha", "邮件": "邮箱 email", "消息": "message messages",
    "通知": "notification notifications", "任务": "task tasks", "会话": "对话 session sessions",
    "日志": "log logs", "缓存": "cache", "配置": "设置 configuration settings",
}
_ACTIONS = {
    "导出": "export exporting", "导入": "import importing", "预览": "preview",
    "打印": "print printing", "上传": "upload", "下载": "download",
    "搜索": "检索 搜寻 search", "筛选": "过滤 filter", "排序": "sort", "分页": "翻页 pagination",
    "创建": "新建 新增 create", "保存": "存储 save", "编辑": "edit", "删除": "移除 delete",
    "复制": "拷贝 copy", "粘贴": "paste", "重命名": "改名 rename", "分享": "共享 share",
    "发送": "send", "接收": "receive", "同步": "sync", "刷新": "refresh", "加载": "load loading",
    "登录": "登陆 login", "注册": "register", "重置": "reset", "验证": "校验 verify validation",
    "审批": "审核 approval", "提交": "submit", "撤销": "撤回 undo", "恢复": "restore",
    "播放": "play", "裁剪": "裁切 crop", "压缩": "compress", "解压": "unzip",
}


def _public_terms():
    names = {}
    for kind, concepts in (("object", _OBJECTS), ("action", _ACTIONS)):
        for label, aliases in concepts.items():
            for alias in (label, *aliases.split()):
                names[alias] = (kind, label)
    alternatives = []
    for term in sorted(names, key=len, reverse=True):
        escaped = re.escape(term)
        alternatives.append(r"\b" + escaped + r"\b" if term.isascii() else escaped)
    return names, re.compile("|".join(alternatives), re.IGNORECASE | re.ASCII)


_NAMES, _MATCH = _public_terms()
# Avoid treating path components, addresses or an instruction's quoted code
# as the feature under discussion. Output remains allowlisted in every case.
_OPAQUE = re.compile(r"\x60[^\x60]*\x60|(?:https?://|[A-Za-z]:[\\/]|\\\\|/)[^\s，。！？；、,;]*"
                     r"|[\w.+-]+@[\w.-]+\.[a-zA-Z]+|[\w.-]+=[^\s，。！？；,;]+")
_CLAUSE = re.compile(r"[,，。！？!?；;\n]")
_RESTRICTION = re.compile(r"不要|请勿|无需|不必|不用|禁止|\b(?:do not|don't|without)\b", re.I)


def engineering_subject(text: str, category: str) -> str:
    """Prefer a nearby object/action pair; retain specific existing categories."""
    best_pair = None
    closest = 25
    first_object = first_action = None
    for clause in _CLAUSE.split(_OPAQUE.sub(" ", text[:500])):
        positive = _RESTRICTION.split(clause, maxsplit=1)[0]
        previous = None
        for match in _MATCH.finditer(positive):
            kind, label = _NAMES[match.group().lower()]
            if kind == "object" and first_object is None:
                first_object = label
            if kind == "action" and first_action is None:
                first_action = label
            if previous is not None:
                prev_kind, prev_label, end = previous
                gap = match.start() - end
                if kind != prev_kind and gap < closest:
                    best_pair = label + prev_label if kind == "object" else prev_label + label
                    closest = gap
            previous = kind, label, match.end()
    if best_pair:
        return best_pair
    detail = first_object or first_action
    if detail and category in {"工程咨询", "错误排查", "测试与验证", "代码重构"}:
        return f"{detail} · {category}"
    return category

