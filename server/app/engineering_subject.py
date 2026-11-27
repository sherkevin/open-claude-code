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
    "数据库": "database sql sqlite postgresql", "索引": "index indexes", "服务": "service services",
    "连接": "connection connections", "网络": "network", "磁盘": "disk", "进程": "process processes",
    "队列": "queue queues", "容器": "container docker", "版本": "version versions",
    "定时任务": "定时计划 schedule scheduler", "权限组": "角色 role roles",
}
_ACTIONS = {
    "导出": "export exporting", "导入": "import importing", "预览": "preview",
    "打印": "print printing", "上传": "upload", "下载": "download",
    "搜索": "检索 搜寻 search", "筛选": "过滤 filter", "排序": "sort", "分页": "翻页 pagination",
    "创建": "新建 新增 create", "保存": "存储 save", "编辑": "edit", "删除": "移除 delete",
    "复制": "拷贝 copy", "粘贴": "paste", "重命名": "改名 rename", "分享": "共享 share",
    "发送": "send", "接收": "receive", "同步": "sync", "刷新": "refresh", "加载": "load loading",
    "登录": "登陆 login", "注册": "register", "重置": "reset", "验证": "校验 verify validation",
    "审批": "审核 approval", "提交": "submit", "撤销": "撤回 undo",
    "播放": "play", "裁剪": "裁切 crop", "压缩": "compress", "解压": "unzip",
    # Lifecycle operations are independent of the component they act on.
    "启动": "start", "停止": "stop", "重启": "restart", "重试": "retry",
    "取消": "cancel", "归档": "archive", "解档": "取消归档 unarchive", "备份": "backup",
    "恢复": "还原 找回 restore", "回滚": "rollback", "迁移": "migration migrate",
    "清理": "清除 cleanup", "部署": "deploy", "升级": "upgrade", "重建": "rebuild",
    "授权": "赋权 grant", "撤权": "撤销授权 revoke", "订阅": "subscribe",
}


# When a question describes what is wrong rather than naming an operation,
# a fixed diagnostic purpose can qualify the component instead.
_PURPOSES = (
    ("性能", re.compile(r"性能|耗时|延迟|超时|很慢|太慢|卡顿|latency|timeout|performance", re.I)),
    ("容量", re.compile(r"容量|占用|不足|剩余空间|磁盘空间|capacity|disk space", re.I)),
    ("状态核对", re.compile(r"核实|核对|确认是否|是否(?:成功|完成|生效)|有没有(?:成功|完成|生效)|verify whether", re.I)),
    ("重复", re.compile(r"重复|多次出现|duplicat", re.I)),
    ("缺失", re.compile(r"丢失|缺失|消失|找不到|漏掉|missing", re.I)),
    ("内容异常", re.compile(r"空白|白屏|乱码|错位|显示不全|blank screen", re.I)),
    ("无响应", re.compile(r"没(?:有)?反应|无响应|卡住|不动|unresponsive", re.I)),
)


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
_SENTENCE = re.compile(r"[。！？.!?；;\n]")
_RESTRICTION = re.compile(
    r"(?:不要|请勿|无需|无须|不必|不用|不需要|不希望|禁止|(?:请|先|千万)别|(?<![\u3400-\u9fff])别"
    r"|\b(?:do not|don't|without)\b)[^,，。！？.!?；;\n]*", re.I,
)


def engineering_subject(text: str, category: str) -> str:
    """Prefer a nearby object/action pair; retain specific existing categories."""
    best_pair = None
    closest = 25
    first_object = first_action = None
    # A comma can separate a feature from the action failing on it. A new
    # sentence or a negative instruction ends that association.
    positive_text = _RESTRICTION.sub("。", _OPAQUE.sub(" ", text[:500]))
    for positive in _SENTENCE.split(positive_text):
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
    purpose = next((label for label, pattern in _PURPOSES if pattern.search(positive_text)), None)
    broad = category in {"工程咨询", "错误排查", "测试与验证", "代码重构"}
    # A technical category can itself be the component. A bare operation
    # is still informative for it even when no separate object was matched.
    primary = first_object or (first_action if broad else None) or category
    qualifier = (first_action if primary != first_action else None) or purpose
    if not qualifier and primary != category:
        qualifier = category
    if qualifier and primary != qualifier and qualifier not in primary:
        return f"{primary} · {qualifier}"
    return primary

