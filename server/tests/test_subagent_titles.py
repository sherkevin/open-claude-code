from app.subagent_title import display_title, display_title_key


def test_only_allowlisted_labels_can_be_resolved():
    assert display_title(display_title_key("Count the products")) == "商品数量统计"
    assert display_title(display_title_key("【渠道策略】分析销售渠道")) == "渠道策略"
    assert display_title_key("评估市场规模和渠道策略") is None
    assert display_title_key("【林示例】说明") is None
    assert display_title("question:0:3:0123456789abcdef") is None


def test_predefined_feature_pairs_distinguish_work_without_copying_private_arguments():
    examples = {
        "核对订单导出的日期。 internal=SECRET-DATE": "订单导出",
        "核对发票预览的格式。不要导出客户名单。": "发票预览",
        "Check database backup for Ada, skip /private/billing/export": "数据库备份",
        "只看 `export invoices` 以及 https://private.invalid/orders/export": None,
    }
    for request, expected in examples.items():
        key = display_title_key(request)
        assert display_title(key) == expected
        if key:
            assert "SECRET" not in key and "private" not in key and "Ada" not in key
    assert display_title("feature:私人名字:导出") is None
    assert display_title("feature:订单:主机内部参数") is None
