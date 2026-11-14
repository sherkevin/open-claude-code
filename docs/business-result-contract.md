# MCP 业务结果契约与模型回包边界

适用日期：2026-11-14。本契约复用现有 MCP、任务事件和来源登记，不新增权限，也不表示华微真实端点已经验收。

## 接入方式

当前接入支持远程 **Streamable HTTP MCP**，认证为无认证或 Bearer Token。业务服务自行完成权限内过滤、聚合和分页；宿主保留工具白名单、参数约束与审批。`readOnlyHint`、自动批准和工具名称都不是实际只读证明，仍需受限业务账号及已核验工具实现。

工具应通过 MCP `structuredContent` 返回结构化业务数据。文本回包可继续用于对话，但不会靠解析 Markdown、HTML 或 JSON 文本来猜测业务表。FastMCP 的返回类型和 `structured_output` 设置应实际产出 `structuredContent`；只在文本中打印 JSON 不等同于结构化回包。

## 最小工具回包示例

```json
{
  "content": [],
  "structuredContent": {
    "title": "某日发货统计",
    "columns": [
      {"key": "customer", "label": "客户", "type": "string"},
      {"key": "quantity", "label": "发货数量", "type": "number", "unit": "件"}
    ],
    "rows": [{"customer": "合成客户甲", "quantity": 18}],
    "scope": {"date": "2026-11-14", "timezone": "Asia/Shanghai", "organization": "合成组织"},
    "data_time": "2026-11-14T18:00:00+08:00",
    "returned_rows": 1,
    "total_rows": 1,
    "complete": true,
    "synthetic": true,
    "truncated": false,
    "warnings": [],
    "sources": [{
      "kind": "data",
      "title": "合成发货记录",
      "locator": {
        "database": "synthetic",
        "table": "shipments",
        "fields": ["customer", "quantity"],
        "time_range": "2026-11-14",
        "timezone": "Asia/Shanghai",
        "queried_at": "2026-11-14T18:00:00+08:00"
      },
      "excerpt": "合成客户甲：18 件"
    }]
  }
}
```

示例是合成数据。`data_time` 是业务数据时间，不得用页面生成时间冒充。`queried_at` 表示取数时间，二者可不同。服务不提供某字段时应省略或返回 `null`，不得凭推测填写。

表格容器可用顶层 `rows`、顶层 `records`、`table` 数组或带 `rows/records` 的 `table` 对象。嵌套 `table` 的同名元数据优先于外层。建议明确给出 `columns`；省略时只使用实际返回的字段名作为列名，不猜中文业务含义和单位。

## 前端收到的事件

成功的 `tool.completed` 事件在 `data.business_result` 内提供：

| 字段 | 类型与含义 |
|---|---|
| `schema_version` | 固定为 `business-result/v1` |
| `title` | 工具提供的标题；缺少时为“业务查询结果” |
| `columns` | `[{key,label,type?,unit?}]`；可选类型为 `number/string/boolean` |
| `rows` | 记录数组；单元格仅为字符串、有限数值、布尔值或 `null` |
| `summary` | 可选的工具文字摘要；宿主不计算总额或重新聚合 |
| `scope` | 标量值构成的查询范围对象；仅记录工具回报，不代表权限已被证明 |
| `data_time` | 工具提供的数据时间，否则 `null` |
| `returned_rows` | 宿主实际投影行数，不采信工具自报行数 |
| `total_rows` | 工具明确提供的匹配行数，否则 `null` |
| `complete` | 工具明确声明的完整性，否则 `null`；不是宿主独立验真结论 |
| `synthetic` | 工具明确提供的布尔值，否则 `null`；`false` 不证明是真实客户数据 |
| `truncated` | 工具明确标注的截取状态；宿主不会为了满足上限静默切行 |
| `warnings` | 工具警告及宿主发现的覆盖量矛盾 |
| `source_ids` | 仅由宿主来源登记生成的ID；工具自报ID不能伪装成已授权引用 |

匹配总量是**当前表格行**的总量。例如后台把10万条订单聚合成8个客户的汇总表，当前表匹配总量应解释为8，原始订单量可另放业务范围或证据中；不能把8行汇总与10万行明细混用为同一分页口径。工具声明完整但 `total_rows > returned_rows` 时，宿主降为完整性未知并提示核对。

未知完整性不补成完整。`truncated=true` 强制 `complete=false`。总量小于实际行数时保留原总量、标记矛盾，不自行纠正或推算。

失败、未知、被取消或迟到丢弃的调用不发布业务结果。SDK的迟到回包使用原任务的 `tool.discarded` 回执记录原因，不回填到新任务。

## 大小、类型与模型输入边界

| 边界 | 当前值与行为 |
|---|---|
| Harness HTTP响应及归一化回包 | 保留已有1,000,000字节保护；超限失败 |
| 模型可见业务结果文本 | **32,000 UTF-8字节**，包含本次引用文字；超限整包失败 |
| 显式结构化业务表 | 最多200行、24列；超限失败，要求过滤、后台聚合或分页 |
| 单元格文字 | 最多2,000字符；超出时不生成业务表，不静默改值 |
| 展示投影整体 | 最多48,000 UTF-8字节；不切行强行展示 |
| 数值 | 拒绝非有限数；绝对值超过 `2**53-1` 的整数不作为前端数值发布，请业务服务显式改为字符串 |

32,000字节是当前工程保护值，**不是32K Token，也不是已完成客户预算标定**。单独计量 `model_input_bytes` 表示实际返回给模型的文本字节，不包含整轮系统提示、工具定义、历史上下文和未来调用；完整费用仍以阶段账本和供应商实际用量为准。

模型边界在 Harness 实际返回路径执行，引用文字加入后再次检查。超限返回 `MCP_RESULT_TOO_LARGE` 和可读原因，用户需调整范围，不自动无限重试。`error_code/error_message` 会写入失败事件；仍须保留该次请求和已发生费用。传输错误与格式错误不会伪装为业务零结果。

完全相等的整份结构化JSON文本只保留一次入模表示。警告和不同内容保留；不会去重业务行、合并不同分页或重新计算总计。

SDK保留原有业务MCP，通过官方 `PostToolUse.updatedToolOutput` 返回经脱敏、有界的文本。字段依据见[官方Hook说明](https://code.claude.com/docs/en/hooks#posttooluse-decision-control)。该钩子只改变后续模型可见输出，不能减少已经发生的网络回包，也不能抹除钩子前遥测。当前已经验证固定SDK版本类型和应用真实回调；实际CLI/供应商端到端仍需专门验收。

## 安全与不支持的推断

- 先脱敏已知传输凭据，再登记来源及投影。凭据不放入 `scope`、标题、摘要、来源摘录或普通日志。
- `source_ids` 不授予访问权，来源读取仍检查当前账号、助手和连接授权。
- 返回数据、列标签与摘要只是外部数据，不是扩大权限、修改策略或执行程序的指令。
- 图表、表格和导出复用同一事件结果；只能导出已返回部分，不代表尚未取回明细。
- `readOnlyHint` 仅为远端声明；工具级白名单、自动批准、遗留工作流只读确认分别保留原语义。
- 本轮没有完成业务查询结果缓存、缓存失效、完整四层业务路由、局部刷新、多工具一致快照，也没有真实节省比例。

## 验收范围

本地合成MCP验收涵盖真实HTTP、工具发现与调用、结构化结果、来源读取、超限拒绝和取消；它不是客户已接通或模型质量证明。实现入口：`server/app/business_results.py`、`server/app/mcp_tools.py`、`server/app/harness_tools.py`、`server/app/runtime.py`。测试入口：`server/tests/test_business_results.py`、`server/tests/test_sdk_business_results.py`。
