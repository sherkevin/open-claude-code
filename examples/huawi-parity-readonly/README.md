# 只读业务 Skill 候选（HW-028b）

**核心判断：本版按有界实测顶层 schema 和结果矩阵语义收紧了候选规则：query 使用 `order_by`（不是 `sort`），describe 精确详情只按 `code`，select 要求显式聚合别名且不放入客户名称或原始值，`rows` 是页大小而不是 `len(data)`，`has_more=false` 只关闭当前查询范围、不证明全量业务覆盖，COUNT 零与 DISTINCT 零分别表述，辅助统计必须安全、有意义并标注用途。** 候选仍不是客户原始 Skill 的完整副本，也没有验证模型实际遵从度或客户生产等价；能否用于真实同题验收，仍需主控在受控环境独立运行。

## 交付文件

| 文件 | 作用 |
| --- | --- |
| `examples/huawi-parity-readonly/SKILL.md` | 候选 Skill 正文；带简单 front matter，可直接走 `POST /api/v1/skills/import` |
| `server/tests/test_huawi_parity_skill_candidate.py` | 解析/导入、内容护栏、权限边界、schema 语义、合成分页与计数场景测试 |
| `examples/huawi-parity-readonly/README.md` | 本说明 |

没有修改 `server/app/skills.py`、任何在线助手、共享后端、运行态或审批逻辑；没有默认权限变更。

## 相对 HW-028a 的修正

| 项目 | HW-028a | HW-028b |
| --- | --- | --- |
| 排序属性 | `sort` | `order_by`（实测顶层属性；`sort` 被审计拒绝） |
| describe 详情 | “如果只提供按代码取详情” | 精确详情只按 `code`；列表/检索用 `keyword` 或 `kind` |
| select | 仅要求已验证字段 | 已验证字段对象或带显式 `alias` 的聚合对象（非 SQL 字符串）；不放入客户名称、租户名称、个人数据或原始记录值 |
| 分页字段 | 未区分 `rows` 与 `len(data)` | `rows` 是页大小；`len(data)` 是本页实际返回行数；二者不等不表示缺页 |
| 完整性 | `has_more=false` 即声明完整 | `has_more=false` 只证明当前查询无后续页；全量业务覆盖还需范围、时间、权限、字段完整性证据 |
| 计数零 | 零与空、失败、无权限分开 | 另区分 `COUNT(*) = 0`（实际工具报告当前查询口径零记录）与 `COUNT(DISTINCT field) = 0`（去重计数零，不等于无记录） |
| 发现 | 最多 3 轮 | 最多 3 轮，且按需；当前上下文已核实时不重复 describe |
| 辅助统计 | 独立、有标注 | 独立、安全、有意义：只做聚合，不读客户身份/原始行，不跨租户，并说明用途 |

## 设计要点

- **只声明说明性规则**：front matter 只有 `name` 和 `description`，不写 `allowed-tools`。现有导入器即使收到 `allowed-tools` 也只当元数据，不会授予工具；候选本身不声明任何可执行工具。
- **工具角色而非固定名称**：正文用 `describe`（检索/字段详情）、`query`（读取已验证字段）、`call`（不调用；动作目录由 describe 发现）描述职责。部署时的实际工具名仍由管理员绑定，候选不假设具体名称。
- **权限不放大**：正文明确“不注册、不授予、不扩大任何可执行工具或数据权限”，审批、租户隔离、凭据隔离和幂等约束保持平台现状。
- **来源边界前置**：正文开头声明这是有界通用规则输入，不是客户原始 SKILL.md、插件清单、运行配置、业务数据或答案的完整副本，也不声称逐字等价或生产等价。
- **不强行发现**：只有缺少回答所必需的证据时才 describe；已有上下文核实过对象和字段时直接 query。

## 要求覆盖矩阵

| 合同要求 | 候选位置 | 测试证据 |
| --- | --- | --- |
| 语义对象/范围/指标/单位 | 第 2 节 | `semantic_decomposition` 标记 + `test_candidate_content_passes_the_bounded_rule_audit` |
| 先 describe 再 query | 第 3 节 | `describe_before_query` 标记；负例 `unverified_field`、`unverified_object` |
| describe 精确详情只按 code | 第 3 节 | `describe_code_only` 标记；`test_describe_detail_is_addressed_by_code_only` 拒绝 `dataset`、`fields` 选择器 |
| 不猜字段/ID | 第 3 节 | `no_field_guessing` 标记；`select ["yield_rate"]` 负例 |
| 按需有限发现 | 第 3、4 节 | `finite_discovery`、`no_forced_discovery` 标记；`test_discovery_is_on_demand_and_bounded` |
| query 顶层用 order_by | 第 5 节 | `order_by_clause` 标记；`test_query_schema_uses_order_by_and_requires_dataset_and_select`；`sort` 负例 |
| select 显式别名、无客户名/值 | 第 5、6 节 | `select_explicit_alias`、`no_customer_names_values` 标记；`COUNT(*)` 无别名负例；客户名称篡改负例 |
| 显式 COUNT 别名 | 第 6 节 | `{"field":"batch_id","aggregate":"count_distinct","alias":"batch_count"}` 标记；`COUNT(*)` 负例 |
| COUNT 零与 DISTINCT 零不同 | 第 6、9 节 | `count_star_zero`、`distinct_zero_semantics` 标记；`test_count_zero_distinct_zero_and_other_states_stay_distinct` |
| 主关系缺失时安全有意义的辅助统计 | 第 7 节 | `auxiliary_safe_meaningful` 标记；正例（≤3 个、对象/范围/别名/单位/用途齐全）；`auxiliary_missing_label`、`auxiliary_not_safe`、`auxiliary_not_aggregate`、`auxiliary_claimed_as_primary` 负例 |
| 不替换 | 第 8 节 | `no_substitution` 标记；`substitution` 负例 |
| 零与空、未查询、失败、无权限 | 第 9 节 | 各状态分别对应的正/负例 |
| rows 是页大小 | 第 5、10 节 | `rows_page_size` 标记；`test_rows_is_page_size_not_length_of_data`；页大小与 `len(data)` 混淆负例 |
| has_more=false 不等于全量覆盖 | 第 10 节 | `has_more_not_coverage` 标记；`test_has_more_false_only_closes_the_current_query_scope` 与 `has_more_not_business_coverage` 负例 |
| 部分页 | 第 10 节 | `partial_page_claimed_complete` 负例 |
| 不执行不安全动作 | 第 11 节 | `no_unsafe_action` 标记；`validate_actions` 拒绝 export/write/message/execute_call |
| 不授予工具/不改权限 | 第 1 节 | `required_tools == []`；已导入 Skill 仍拒绝 `Bash`；声明 `[Bash, call]` 也仍拒绝执行 |

## 合成场景说明

测试不调用模型、不连接客户 MCP、不访问网络。合成语义计划中的 SQL 风格表达式只用于描述指标含义，不是 MCP 请求；线上的 select 必须为结构化对象数组，另有独立请求形状测试。查询计划中的字段来自现有本地合成夹具 `fixtures/synthetic-industrial-v1/tables/quality-batches.csv` 的表头：

`batch_id, line, production_date, inspected_count, qualified_count`

测试内的 `synthetic_quality_batches` 和 `synthetic_equipment_failure` 是纯合成对象名，用于表达规则，不是任何客户数据集；用例中的人员/租户标识也是合成负例（如 `tenant-acme-0001`）。

正例：

- 顶层 schema：`dataset` + `select` + `order_by`；未分组 `{"field":"*","aggregate":"count","alias":"record_count"}`，范围 `production_date 2026-11-01..2026-11-04`。
- describe 精确详情：`{"code": "synthetic_quality_batches"}`；列表检索：`{"keyword": "quality", "kind": "dataset", "page": 1, "rows": 20}`。
- 未分组 `{"field":"batch_id","aggregate":"count_distinct","alias":"batch_count"}`。
- 主对象无已发布关联时，只做一个独立、安全、有意义的辅助统计，并标注对象、范围、字段、别名、单位和用途。
- 分页：`rows=50`、`data` 只有 2 行、`has_more=false`、`total=52` 时，页大小仍是 50，返回行数是 2。
- 已核实字段时不重复 describe（`describe_rounds=0`）。

负例：

- 选择未 describe 字段、`*` 取行、无别名 COUNT、group 字段与 `group_by` 不一致。
- query 使用 `sort` 或未观察到的顶层属性；describe 详情使用 `dataset`、`fields` 等未观察到的选择器。
- 主对象不存在却不说明、用替代对象顶替。
- 未分组证据不足就做分组聚合。
- 把未查询/失败/无权限/空页写成 0，或把部分页写成完整结果。
- 把 `has_more=false` 当成全量业务覆盖，或把 `len(data)` 当成 `rows`。
- 把 DISTINCT 零写成“没有记录”，或在已有记录时声称无记录。
- 辅助统计缺用途/标签、用原始行代替聚合、返回客户身份或跨租户、冒充主关系结论。
- 已核实字段仍重复发现，或超过 3 轮发现。

## 精确测试命令

从本工作树根目录执行：

```bash
cd server
../.venv/bin/python -m pytest tests/test_huawi_parity_skill_candidate.py -q -p no:cacheprovider
```

若该工作树根目录没有 `.venv`，改用项目现有服务端测试所用的同一解释器（要求已安装 `fastapi`、`pydantic`、`pytest`），并仍从上述 `server/` 目录执行，以保证 `app` 与 `test_api` 的导入路径正确。

## 预期断言

- `import_skill` 返回名称 `huawi-parity-readonly`、正文以 `# ` 开头、`required_tools == []`、长度不超过 50000。
- 内容审计无违规：必需规则标记全部存在；无能力清单标识（如 `CAP-*`）、无客户名称/租户内部标识、无危险执行承诺、无真实凭据样式、无废弃 `sort` 属性。
- 对候选做 7 类合成篡改（宣称完整副本、去掉 COUNT 别名、改成可以猜字段、追加自动执行/导出、插入合成租户标识 `tenant-acme-0001`、插入客户名称、把 `order_by` 改成 `sort`）时，审计分别报出对应违规码。
- query schema 正例返回空错误列表；`sort`、缺 `dataset`、空 `select`、非列表 `order_by` 分别返回预期错误码。
- describe 正例返回空错误列表；`dataset`、`fields` 选择器分别返回 `unsupported_describe_property:*`。
- 发现正例返回空错误列表；4 轮发现和已核实后重复发现分别返回 `too_many_discovery_rounds`、`unnecessary_discovery`。
- 合成查询计划正例返回空错误列表；查询计划负例分别返回预期错误码，包括 `deprecated_sort_clause`、`auxiliary_missing_label`、`auxiliary_not_safe`、`auxiliary_not_aggregate`。
- 分页正例返回空错误列表；页大小/返回行数/total/覆盖声明混淆分别返回预期错误码。
- 5 类错误回答状态被拒绝，2 类正确回答状态被接受；另外 DISTINCT 零不得写成无记录，COUNT(*) 零才可写“已核实为 0”。
- API 导入返回 201，修订版从 1 升到 2，版本列表为 `[2, 1]`。
- 导入后的 Skill 不授权 `Bash`；即使 front matter 声明 `allowed-tools: [Bash, call]`，执行仍返回 `TOOL_DENIED`。

## 证据边界与限制

1. **没有客户原始资产**：合同明确“Full original export not recovered in this round”。本候选只使用合同第 5 行的只读规则增量；不能据此声称与客户原始 Skill 等价，也没有客户数据、答案或标识。
2. **未执行测试**：本轮按任务约束只写文件，未运行 pytest、未启动服务、未提交或推送。上述结果均为预期断言，需主控独立执行确认。
3. **未验证模型遵从**：测试验证的是文本、导入、版本和权限边界，不证明真实模型会遵守规则；同题真实业务验收必须由主控在授权测试 ERP 环境完成。
4. **嵌套 schema 未证明**：顶层属性按合同列出的有界观察值实现；filters/order_by 的嵌套操作符、排序方向格式、分组聚合和真实分页边界未单独证明，候选不声称这些能力已可用。
5. **工具名未绑定**：`describe/query/call` 是职责名称；实际工具名、连接和审批由部署方控制，候选不写死、不申请权限。
6. **测试中的替代元数据仅用于护栏**：`allowed-tools: [Bash, call]` 的变体只用于证明“声明不等于授权”，不是候选内容。
7. **合成数据不等于生产验收**：夹具和计划只证明规则实现，不代表真实客户效果或生产可用。

## 交接

主控可直接审查三个交付文件，并在独立环境运行上面的 pytest 命令。若要求进一步贴近真实导入，可补充一次通过管理 API 的导入回执和版本快照截图；本轮未做，以免触碰在线助手或生产环境。
