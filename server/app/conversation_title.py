"""Short, local conversation titles that do not repeat the first message."""

import re


DEFAULT_TITLE = "新对话"

_TOPICS = tuple((re.compile(pattern, re.IGNORECASE), title) for pattern, title in (
    (r"(?:你|您)(?:可?以?|能)(?:帮我)?(?:做什么|干什么|做哪些|帮什么|协助什么)|(?:你|您)有什么(?:功能|能力)|(?:你|您)是谁", "助手能力介绍"),
    (r"库存|余量|现存量|缺货|\binventory\b|\bstock\b", "库存查询"),
    (r"振动|缺陷|抽检|合格率|质量|返工|良率|\bquality\b", "质量分析"),
    (r"订单|发货|交付|交期|\border\b|\bshipment\b", "订单与交付"),
    (r"风险|合规|审核|审计|\baudit\b|\brisk\b", "风险与合规"),
    (r"知识库|手册|规程|资料|文档|\bmanual\b", "资料查询"),
    (r"报告|纪要|总结|复盘|\breport\b", "报告整理"),
    (r"流程|方案|计划|需求|\bplan\b", "方案讨论"),
    (r"统计|趋势|同比|环比|产量|\bmetrics\b", "业务数据分析"),
))


def first_message_title(text: str) -> str:
    sample = re.sub(r"\s+", "", text[:500]).casefold()
    if not sample:
        return ""
    spaced = text[:500]
    return next((title for pattern, title in _TOPICS if pattern.search(sample) or pattern.search(spaced)), "业务咨询")
