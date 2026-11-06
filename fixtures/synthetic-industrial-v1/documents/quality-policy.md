# 合成产线质量统计口径

> `synthetic=true` · fixture `synthetic-industrial-v1` · 仅用于开发与验收

对同一产线和日期范围内的多个批次，先汇总 `qualified_count` 与
`inspected_count`，再计算加权合格率：

```text
yield_rate = SUM(qualified_count) / SUM(inspected_count)
```

本夹具使用 Asia/Shanghai 时区，日期范围包含开始日和结束日。检验总数为
4,700 件，合格总数为 4,537 件，加权合格率为 96.53%。这些数字是合成数据，
不代表客户的生产记录。
