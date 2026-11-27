"""Short, local conversation titles that do not repeat the first message."""

import re

from .engineering_subject import engineering_subject


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


# As with business topics, only fixed labels can appear on the workbench.
# ASCII word boundaries also recognize "pwd命令" without matching "mypwd".
_ENGINEERING_TOPICS = tuple((re.compile(pattern, re.IGNORECASE | re.ASCII), title) for pattern, title in (
    (r"\bgit\s+(?:merge|rebase|cherry-pick)\b|合并冲突|合并.{0,6}分支", "Git 合并"),
    (r"\bgit\s+(?:branch|switch|checkout)\b|\bgit\b.{0,50}分支|(?:创建|列出|切换|删除)分支", "Git 分支"),
    (r"\bgit\s+(?:commit|log|show|revert|bisect)\b|\bgit\b.{0,60}提交|代码提交|提交代码", "Git 提交"),
    (r"\bgit\s+(?:diff|status)\b|工作区(?:差异|变更)|暂存区", "Git 工作区变更"),
    (r"\bpwd\b|当前(?:工作)?目录|工作目录", "当前工作目录"),
    (r"\bls\b|目录(?:条目|内容)|列出(?:目录|文件)", "目录内容"),
    (r"\b(?:calendar|date|cal|timezone)\b|日历|日期|日程|时区", "日期与日历"),
    (r"\b(?:npm|pnpm|yarn|pip3?|cargo)\s+(?:install|add)\b|依赖(?:项|包|安装|管理|冲突)|安装(?:依赖|软件包)", "依赖安装与管理"),
    (r"\b(?:pytest|vitest|jest|unittest|tests?)\b|测试|回归用例", "测试与验证"),
    (r"\b(?:debug|debugger|traceback)\b|调试|排查|堆栈|报错", "错误排查"),
    (r"\b(?:build|compile|cmake|makefile)\b|构建|编译|打包", "构建与编译"),
    (r"\b(?:grep|ripgrep|rg|find)\b|查找文件|搜索(?:文件|代码)|文本检索", "文件与代码搜索"),
    (r"\b(?:deploy|deployment|docker|kubernetes)\b|部署|容器", "部署与容器"),
    (r"\b(?:database|sql|sqlite|postgresql)\b|数据库", "数据库"),
    (r"\b(?:refactor|refactoring)\b|重构", "代码重构"),
    (r"\bgit\b|版本控制", "Git 版本控制"),
))


def first_message_title(text: str, *, engineering: bool = False) -> str:
    sample = re.sub(r"\s+", "", text[:500]).casefold()
    if not sample:
        return ""
    spaced = text[:500]
    topics = _ENGINEERING_TOPICS + _TOPICS if engineering else _TOPICS
    category = next((title for pattern, title in topics if pattern.search(sample) or pattern.search(spaced)),
                    "工程咨询" if engineering else "业务咨询")
    return engineering_subject(spaced, category) if engineering else category
