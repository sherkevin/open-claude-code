# 离线运行统计

此工具只读取显式列出的 `/runs/{id}/trace` 导出文件（`schema_version: 1`），不连接服务器、不调用模型，也不读取账号配置。用于整理测量证据，不代表客户验收。

```bash
python scripts/report_run_measurements.py \
  --experiment-id huawi-orders-A-v1 \
  --source-kind synthetic \
  --output /private/tmp/orders-A-report.json \
  /private/tmp/run-001.json /private/tmp/run-002.json
```

真实客户授权数据使用 `--source-kind customer`。这个标签由操作者提供，程序不会核验来源。`--output` 必须是尚不存在的文件，创建权限为 `0600`；不填写则向标准输出打印。输入文件 SHA-256 和顺序保留在报告中；报告不复制路径、运行 ID、消息正文、工具参数、错误文本或凭据。原始 trace 仍需按客户数据权限保管。

## 统计口径

- 每个运行 ID 只能出现一次；重复文件或同一运行的多次快照会报错，避免扩大分母。事件若提供 `run_id`，必须与导出所属运行一致，否则拒绝统计。
- 所有运行都计入总分母。成功、失败、取消、中断和非终态分别统计；运行成功不等于业务答案正确。非终态不计入完整耗时。
- 每项报告样本数、已知数、缺失数及 p50、p95、max。缺失或无效数值不补零。分位数只使用已知值，采用 nearest-rank：排序后取第 `ceil(p × n / 100)` 个值。
- 只使用 `run.timings` 中的 `first_server_progress_ms`、`first_tool_attempt_ms`、`first_tool_success_ms`、`first_text_ms`、`total_ms`、`runtime_ready_ms`、`first_model_event_ms`。不从旧 `first_feedback/structure/valid` 或 `duration_ms` 推导替代值。
- 服务器首个进度不是浏览器首个反馈；工具执行成功不是经人工核验的业务有效结果。浏览器渲染、网络传输和业务正确性需要另外的测量证据。
- 业务缓存仅在 `tool.completed.data.success` 严格等于 true 时采纳 `result_facts.cache_hit` 的严格布尔值，划分 true、false、unknown、mixed。失败、未确认的完成记录和无工具完成记录均为 unknown；同时出现不同值，包括已知与未知的组合，均为 mixed。Provider 缓存 Token 单独统计，不能据此判断业务缓存命中。
- Token 以顶层 `usage` 为快照，绝不把累计 `usage.updated` 事件相加。失败、取消、中断和非终态快照只能贡献已知小计：它们可能遗漏最后一次供应商用量回报，因此整次 Token 保持未知。没有阶段账本的旧平面 `usage` 也只能保留已知小计，因为它无法证明所有调用的贡献均已记录。只有 succeeded 且阶段账本证明相关字段已完整报告的运行，才进入上报用量合计；该合计仍不是供应商结算凭证。
- 存在阶段账本时，优先采用 `provider_totals`，缺失贡献保持未知。某字段至少有一个 `stage.provider_usage` 的有效实报值时才采纳 `known_subtotals`；账本自动初始化的 0 不算实报，真实报告的 0 则保留。缺少任何运行贡献或包含未成功运行时，`complete_total` 保持 null。Token 指标的 `known_count` 只计算可以进入完整上报合计的运行，`subtotal_reporting_count` 另外记录有已知小计的运行。Provider cache creation 字段归一到 cache write；不同 Token 字段不会合成含义不明确的“总 Token”。

## 实验约束

实验编号只是一张外部实验记录的索引，不是“同任务、同模型、同数据范围、同权限”的自动证明。比较前需在受控实验记录中固定任务输入、模型版本、数据快照、权限、网络环境和冷/热缓存条件，并保存全部尝试，不能只挑成功运行。

默认基线状态为 `not_supplied`。可用 `--baseline-reference baseline-A-v1` 登记外部基线索引，状态仍是 `external_reference_unverified`。工具不计算节省百分比、收益或客户通过结论。取消成功率、局部刷新恢复率和业务正确率需要相应尝试分母与判定记录，不能拿当前状态计数替代。
