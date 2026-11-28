# 华微只读业务 Skill 候选（HW-028a）

**核心判断：本候选只实现了合同允许的“有界通用只读规则”并可通过现有 Skill 导入机制落库，但它不是客户原始 Skill 的完整副本，也没有验证模型实际遵从度或客户生产等价。** 是否能用于真实同题验收，仍需主控在受控环境独立运行。

## 交付文件

| 文件 | 作用 |
| --- | --- |
| `examples/huawi-parity-readonly/SKILL.md` | 候选 Skill 正文；带简单 front matter，可直接走 `POST /api/v1/skills/import` |
| `server/tests/test_huawi_parity_skill_candidate.py` | 解析/导入、内容护栏、权限边界、合成分页与计数场景测试 |
| `examples/huawi-parity-readonly/README.md` | 本说明 |

没有修改 `server/app/skills.py`、任何在线助手、共享后端、运行态或审批逻辑。

## 设计要点

- **只声明说明性规则**：front matter 只有 `name` 和 `description`，不写 `allowed-tools`。现有导入器即使收到 `allowed-tools` 也只当元数据，不会授予工具；候选本身不声明任何可执行工具。
- **工具角色而非固定名称**：正文用 `describe`（检索/字段详情）、`query`（读取已验证字段）、`call`（仅发现，不执行）描述职责。部署时的实际工具名仍由管理员绑定，候选不假设具体名称。
- **权限不放大**：正文明确“不注册、不授予、不扩大任何可执行工具或数据权限”，审批、租户隔离、凭据隔离和幂等约束保持平台现状。
- **来源边界前置**：正文开头声明这是有界通用规则输入，不是客户原始 SKILL.md、插件清单、运行配置、业务数据或答案的完整副本，也不声称逐字等价或生产等价。

## 要求覆盖矩阵

| 合同要求 | 候选位置 | 测试证据 |
| --- | --- | --- |
| 语义对象/范围/指标/单位 | 第 2 节 | `semantic_decomposition` 标记 + `test_candidate_content_passes_the_bounded_rule_audit` |
| 先 describe 再 query | 第 3 节 | `describe_before_query` 标记；负例 `unverified_field`、`unverified_object` |
| 不猜字段/ID | 第 3 节 | `no_field_guessing` 标记；`select ["yield_rate"]` 负例 |
| 有限发现 | 第 4 节 | `finite_discovery` 标记 |
| 显式 COUNT 别名 | 第 6 节 | `COUNT(DISTINCT batch_id) AS batch_count` 标记；`COUNT(*)` 负例 |
| 主关系缺失时有效辅助统计 | 第 7 节 | 辅助统计正例（≤3 个、对象/范围/别名/单位齐全）与 `auxiliary_claimed_as_primary` 负例 |
| 不替换 | 第 8 节 | `no_substitution` 标记；`substitution` 负例 |
| 零与空、未查询、失败、无权限 | 第 9 节 | 五种状态分别对应的正/负例 |
| 部分页 | 第 10 节 | `has_more` 规则；`partial_page_claimed_complete` 负例 |
| 不执行不安全动作 | 第 11 节 | `no_unsafe_action` 标记；`validate_actions` 拒绝 export/write/message/execute_call |
| 不授予工具/不改权限 | 第 1 节 | `required_tools == []`；已导入 Skill 仍拒绝 `Bash`；声明 `[Bash, call]` 也仍拒绝执行 |

## 合成场景说明

测试不调用模型、不连接客户 MCP、不访问网络。查询计划中的字段来自现有本地夹具 `fixtures/synthetic-industrial-v1/tables/quality-batches.csv` 的表头：

`batch_id, line, production_date, inspected_count, qualified_count`

测试内的 `synthetic_quality_batches` 和 `synthetic_equipment_failure` 是纯合成对象名，用于表达规则，不是任何客户数据集。

正例：

- 未分组 `COUNT(*) AS record_count`，范围 `production_date 2026-11-01..2026-11-04`。
- 未分组 `COUNT(DISTINCT batch_id) AS batch_count`。
- 主对象无已发布关联时，只做一个独立辅助统计，并标注对象、范围、字段、别名、单位。

负例：

- 选择未 describe 字段、`*` 取行、无别名 COUNT、group 字段与 `group_by` 不一致。
- 主对象不存在却不说明、用替代对象顶替。
- 未分组证据不足就做分组聚合。
- 把未查询/失败/无权限/空页写成 0，或把部分页写成完整结果。
- 辅助统计冒充主关系结论。

## 精确测试命令

从本工作树根目录执行：

```bash
cd server
../.venv/bin/python -m pytest tests/test_huawi_parity_skill_candidate.py -q -p no:cacheprovider
```

若该工作树根目录没有 `.venv`，改用项目现有服务端测试所用的同一解释器（要求已安装 `fastapi`、`pydantic`、`pytest`），并仍从上述 `server/` 目录执行，以保证 `app` 与 `test_api` 的导入路径正确。

## 预期断言

- `import_skill` 返回名称 `huawi-parity-readonly`、正文以 `# ` 开头、`required_tools == []`、长度不超过 50000。
- 内容审计无违规：12 个必需规则标记全部存在；无能力清单标识（如 `CAP-*`）、无客户/租户内部标识、无危险执行承诺、无真实凭据样式。
- 对候选做 5 类合成篡改（宣称完整副本、去掉 COUNT 别名、改成可以猜字段、追加自动执行/导出、插入合成租户标识 `tenant-acme-0001`）时，审计分别报出对应违规码。
- 合成正例计划返回空错误列表；9 类负例分别返回预期错误码。
- 5 类错误回答状态被拒绝，2 类正确回答状态被接受。
- API 导入返回 201，修订版从 1 升到 2，版本列表为 `[2, 1]`。
- 导入后的 Skill 不授权 `Bash`；即使 front matter 声明 `allowed-tools: [Bash, call]`，执行仍返回 `TOOL_DENIED`。

## 证据边界与限制

1. **没有客户原始资产**：合同明确“Full original export not recovered in this round”。本候选只使用合同第 5 行的只读规则增量；不能据此声称与客户原始 Skill 等价，也没有客户数据、答案或标识。
2. **未执行测试**：本轮按任务约束只写文件，未运行 pytest、未启动服务、未提交或推送。上述结果均为预期断言，需主控独立执行确认。
3. **未验证模型遵从**：测试验证的是文本、导入、版本和权限边界，不证明真实模型会遵守规则；同题真实业务验收必须由主控在授权测试 ERP 环境完成。
4. **分组聚合与分页契约未证明**：候选把分组聚合、复杂分页标为“未单独证明前不视为已验证”，不声称这些能力已可用。
5. **工具名未绑定**：`describe/query/call` 是职责名称；实际工具名、连接和审批由部署方控制，候选不写死、不申请权限。
6. **测试中的替代元数据仅用于护栏**：`allowed-tools: [Bash, call]` 的变体只用于证明“声明不等于授权”，不是候选内容。

## 交接

主控可直接审查两个交付文件，并在独立环境运行上面的 pytest 命令。若要求进一步贴近真实导入，可补充一次通过管理 API 的导入回执和版本快照截图；本轮未做，以免触碰在线助手或生产环境。
