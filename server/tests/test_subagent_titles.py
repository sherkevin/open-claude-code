from app.subagent_title import display_title, display_title_key


def test_only_allowlisted_labels_can_be_resolved():
    assert display_title(display_title_key("Count the products")) == "商品数量统计"
    assert display_title(display_title_key("【渠道策略】分析销售渠道")) == "渠道策略"
    assert display_title_key("评估市场规模和渠道策略") is None
    assert display_title_key("【林示例】说明") is None
    assert display_title("question:0:3:0123456789abcdef") is None
