"""Classify task descriptions into a fixed vocabulary for display."""

import re
import unicodedata

from .engineering_subject import public_feature_pair, public_feature_title

# Only fixed keys and labels leave this module.
_TITLES = {
    "product_count": (
        "商品数量统计", r"商品|产品|货品|\b(?:products?|items?|skus?)\b",
        r"统计|数量|总数|多少|计数|\b(?:counts?|quantities|quantity|number|totals?)\b",
    ),
    "delivery_checklist": (
        "交付检查清单", r"交付|验收|\b(?:delivery|deliverables?|handoff|acceptance)\b",
        r"检查|清单|核对|确认|要点|\b(?:checklist|checks?|verify|review)\b",
    ),
    "inventory_review": (
        "库存核对", r"库存|存货|\b(?:inventory|stock)\b",
        r"核对|确认|统计|检查|盘点|\b(?:counts?|audit|check|verify|review)\b",
    ),
    "order_summary": (
        "订单汇总", r"订单|\borders?\b",
        r"汇总|整理|分析|统计|\b(?:summary|summari[sz]e|analysis|count|report)\b",
    ),
    "sales_summary": (
        "销售汇总", r"销售|销量|销售额|\bsales\b",
        r"汇总|整理|分析|统计|\b(?:summary|summari[sz]e|analysis|report)\b",
    ),
    "risk_review": (
        "风险评估", r"风险|\brisks?\b",
        r"评估|分析|识别|梳理|\b(?:assess|assessment|analysis|identify|review)\b",
    ),
    "market_size": ("市场规模", r"市场|\bmarket\b", r"规模|体量|\b(?:size|sizing)\b"),
    "channel_strategy": ("渠道策略", r"渠道|\bchannels?\b", r"策略|规划|\b(?:strategy|planning)\b"),
}
_HEADINGS = {f"【{title}】": key for key, (title, _, _) in _TITLES.items()}


def display_title_key(task: str) -> str | None:
    text = unicodedata.normalize("NFKC", task).strip().casefold()
    heading, delimiter, _ = text.partition("】")
    key = _HEADINGS.get(heading + delimiter)
    if key:
        return key
    matches = [key for key, (_, subject, action) in _TITLES.items()
               if re.search(subject, text) and re.search(action, text)]
    if matches:
        return matches[0] if len(matches) == 1 else None
    pair = public_feature_pair(text)
    return "feature:" + ":".join(pair) if pair else None


def display_title(key: str | None) -> str | None:
    if key and key.startswith("feature:"):
        subject, _, action = key[len("feature:"):].partition(":")
        return public_feature_title(subject, action)
    entry = _TITLES.get(key)
    return entry[0] if entry else None
