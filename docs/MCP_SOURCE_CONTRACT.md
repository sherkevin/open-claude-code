# MCP 来源元数据约定

0.4 接受真实 MCP 工具结果的 `structuredContent.sources`，或 JSON 文本结果中的 `sources`。没有此字段的工具仍可调用，但应用不会编造数据库位置。客户端只显示服务端登记的来源；模型不能通过普通文本创建可访问的来源记录。

每个来源应描述本次实际查询结果，使用可公开给当前业务用户的位置，不放凭据、连接串或服务器路径。推荐格式：

```json
{
  "rows": [{"batch_id": "B001", "inspected_count": 1000, "qualified_count": 980}],
  "sources": [{
    "kind": "data",
    "title": "批次质量统计",
    "locator": {
      "data_source": "质量系统",
      "database": "quality",
      "schema": "public",
      "table": "batches",
      "fields": ["batch_id", "inspected_count", "qualified_count"],
      "record_keys": ["B001"],
      "filters": {"line": "L01"},
      "time_range": {"field": "production_date", "from": "2026-11-01", "to": "2026-11-04", "inclusive": true},
      "timezone": "Asia/Shanghai",
      "queried_at": "2026-11-05T07:00:00+00:00",
      "aggregation": "SUM(qualified_count) / SUM(inspected_count)",
      "trace_id": "business-query-opaque-id",
      "row_count": 1
    },
    "excerpt": "批次 B001：检验1000件，合格980件。",
    "original_url": "https://business.example/quality/B001"
  }]
}
```

`original_url` 可省略；即使没有业务系统网页，应用仍提供经过权限验证的查询快照和定位信息。数据库结果的位置来自 MCP 提供方，应用记录其来源连接与查询任务，并不冒充已直接访问数据库。准确性需要业务 MCP 按实际查询生成元数据。

知识库来源由应用的文件索引自动生成；网页来源由公开网页读取工具自动生成。外部数据 MCP 的 `sources` 不能借此伪造应用知识库中的文件 ID 或版本。
