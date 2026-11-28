# 业务结果展示回归（HW-031a）

日期：2026-11-28
分支：`feat/huawi-result-display-20261128`
固定基线：`2c330bfd9a05bce9ca16b01fa46ba31af2c8da7a`

## 核心判断

现有前端卡片已经能正确承载新后端的规范 `business-result/v1` 聚合回执：标题原样展示、数值 `0` 与空结果可区分、`total_rows/complete` 未提供时保持未知、`source_ids` 为空时不伪造来源、警告可见且不宣称独立核验。**未发现需要修改生产代码的渲染缺口**，因此本轮只新增定向测试与本文档。

## 范围

本轮只验证前端展示层对“工具报告聚合值”规范回执的处理，不涉及：

- 后端回执生成、`count_receipt` 语义或 `count_distinct` 实现；
- 部署、生产、真实客户数据、真实账号或网络调用；
- 审批、权限、运行时或共享后端行为。

测试全部使用合成值和合成事件，不包含任何客户数据、业务编号或凭据。

## 精确测试命令

前置：`client/node_modules` 不存在时先执行 `cd client && npm ci`（`package-lock.json` 固定依赖）。随后在本工作树根目录执行：

```bash
cd client && npx vitest run src/huawiParityReceipt.test.tsx
```

等价的 `npm` 形式：

```bash
cd client && npm test -- src/huawiParityReceipt.test.tsx
```

本轮工程师**未执行**上述命令；按任务约定由主控独立运行并记录退出码与原始输出。

## 规范回执与断言映射

合成回执（`client/src/huawiParityReceipt.test.tsx` 中的 `canonical()`）对应契约字段如下：

| 回执字段 | 合成值 | 前端期望 |
|---|---|---|
| `title` | `工具报告聚合值` | `<h3>` 原样展示，不改成“宿主核验聚合值” |
| `columns` / `rows` | `[{key:'count',label:'记录数'}]` / `[{count:0}]` | 记录数显示为 `0`，`returned_rows=1`，不进入空结果分支 |
| `total_rows` | `null` | 显示“结果总行数未提供” |
| `complete` | `null` | 显示“完整性未说明”，不显示“数据源标记为完整/部分结果” |
| `source_ids` | `[]` | 显示“本次结果未提供来源详情。”，无“查看依据”按钮 |
| `warnings` | 未经独立核验的范围警告 | 警告可见；不出现“已验证/独立核验通过” |
| `count_receipt` | `{operation:'count',scope_verified:false}` | 前端解析时忽略，不转成任何“已核验”文案 |
| `truncated` | `false` | 不显示“结果已截取” |

## 预期断言

1. **标题与卡片唯一性**：`<h3>工具报告聚合值</h3>` 存在；`class="business-result br-object-result"` 恰好 1 个；`class="br-warnings"` 恰好 1 个。
2. **精确零值**：`parseBusinessResult` 得到 `rows=[{count:0}]`、`returned_rows=1`；`displayBusinessCell(0)` 返回 `0`；卡片含 `>0</dd>` 与“已返回 1 行”；CSV 含 `"0"`；不出现“本次查询未返回记录”。
3. **未知覆盖度**：`complete` 与 `total_rows` 均为 `null`；页面显示“结果总行数未提供”“完整性未说明”，且不含“数据源标记为完整”“部分结果”。
4. **空来源**：`source_ids=[]`；页面显示“本次结果未提供来源详情。”，不含“查看依据”；工具在 `business_result.source_ids` 内自报的来源不会进入投影。
5. **有界警告**：警告原文可见；超过 20 条的警告只保留前 20 条；非字符串警告被丢弃；不出现“独立核验通过”“已验证”。
6. **畸形/空结果**：`null`、空列、对象单元格、非数组行、`success=false` 均不生成卡片；`rows=[]` 只显示空结果提示，不生成 `0` 聚合。
7. **历史重载**：成功 trace 渲染恰好 1 张卡，重复加载结果一致的静态标记；失败 trace 保留 1 张卡并显示“业务数据已返回，回答未完成”；回答绑定不一致或任务未结束会被拒绝；403 后重试可重新读取；空 trace 渲染为空字符串。

## 发现

- **未发现渲染缺口**：现有 `BusinessResult.tsx` 的 `parseBusinessResult`、`resultsFromEvents` 和 `BusinessResultCard` 已满足上述展示边界。
- `count_receipt` 是后端回执的附加字段，前端按未知字段忽略；这不构成越权或过度声明，因为页面不会据此生成任何“已核验”文案。
- 空结果与数值零在组件中走不同分支：空结果进入 `br-empty`，数值零保留在对象/表格中。

## 证据边界

- 本轮**未执行任何测试**；上文是预期断言，不是通过记录。通过与否以主控独立运行的实际输出为准。
- 新后端实现位于其他工作树，按本轮硬限制不可读取；合成回执依据 `result-ui/CONTRACT.md` 第 6 行整理。警告原文与 `count_receipt` 的具体嵌套未在本工作树独立核验。
- 若新后端的范围警告实际只放在 `count_receipt` 而非顶层 `warnings`，当前前端不会展示它；这属于需要主控核对的接口假设，而不是本次已证实的缺陷。
- 测试只覆盖服务端渲染标记，未覆盖浏览器交互、真实模型、真实 MCP、真实权限或客户环境。
- 未接触后端、部署、生产、`.runtime` 或客户文件系统。

## 变更文件

- 新增：`client/src/huawiParityReceipt.test.tsx`
- 新增：`docs/huawi-parity/result-display.md`
- 生产代码：无改动

## 交接

主控在合并前独立运行：

```bash
cd client && npx vitest run src/huawiParityReceipt.test.tsx
```

需要核对：退出码、用例数、失败项原文。若需要同时回归既有业务结果测试：

```bash
cd client && npx vitest run src/businessResult.test.tsx src/historicalBusinessResults.test.tsx src/huawiParityReceipt.test.tsx
```
