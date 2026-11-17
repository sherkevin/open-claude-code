# 华微内网 MCP 桥接声明台账

- 台账日期：2026-11-17
- 适用计划：`docs/plans/2026-11-17-huawi-kaiwu-mcp-bridge-requirements-and-delivery-plan.md`
- 规则：`independently_reproduced` 必须有项目内证据和 SHA-256；`report_claim` 只代表收到报告；`planned_target` 是待验收目标；`unverified` 不能对外形成结论。

| claim_id | statement | status | scope | evidence_path | evidence_sha256 | executed_at / executed_by | expires_on |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CLM-001 | 候选 Windows 包 SHA-256 为 `9305E142580CE3908B1B5C334F6F3FEA160F0767BE23702AAAB99C2C46FE5E1F` | `independently_reproduced` | 当前保存的 1.0.1 候选包 | `artifacts/kaiwu-connector-1.0.1-windows-amd64.zip` | `9305E142580CE3908B1B5C334F6F3FEA160F0767BE23702AAAB99C2C46FE5E1F` | 2026-11-17 / 学校云项目证据检查 | 包被替换或重新构建时 |
| CLM-002 | 1.0.1 在报告所述 Windows 测试机完成安装、服务、回环页面、服务重启和卸载 | `report_claim` | 报告所述 SHERVIN 测试机，非客户目标系统；系统版本字符串存在不一致，缺少 `windows-info.txt` | `handoff/03_windows_connector_acceptance_1.0.1.md` | 由 `SHA256SUMS` 校验 | 报告日期 2026-11-17 / Windows 测试执行人 | 补齐原始证据或重测时 |
| CLM-003 | 1.0.1 完成模拟 HTTP MCP 工具发现、静态 Bearer、错误凭据拒绝和服务重启后诊断 | `report_claim` | 报告所述模拟 MCP 和合成库存数据 | `handoff/03_windows_connector_acceptance_1.0.1.md`、`handoff/04_test_summary_1.0.1.json` | 由 `SHA256SUMS` 校验 | 报告日期 2026-11-17 / Windows 测试执行人 | 补齐原始诊断 JSON 和截图或重测时 |
| CLM-004 | 正常停止连接器后 Edge 在 5 秒内返回 `503 SOURCE_UNAVAILABLE` | `planned_target` | C 段测试环境 | 尚无 | 尚无 | 尚未执行 | 不适用，验证后更新 |
| CLM-005 | 同机进程强杀 10 秒、网络黑洞 45 秒、relay 故障 15 秒内标记不可用 | `planned_target` | C 段测试环境 | 尚无 | 尚无 | 尚未执行 | 不适用，验证后更新 |
| CLM-006 | 控制面、relay、visitor、Edge 和客户只读 MCP 已完成 C 段联调 | `unverified` | 当前候选包和当前云端 | 尚无 | 尚无 | 尚未执行 | 不适用，验证后更新 |
| CLM-007 | 每来源 20 并发、10 请求/秒、20 条 SSE 的 30 分钟稳态、2 倍突发和 2 小时长稳达到 HWC-045 | `planned_target` | 合成基线；客户峰值待提供 | 尚无 | 尚无 | 尚未执行 | 不适用，验证后更新 |
| CLM-008 | 企业容量、端到端 p95/p99 和代理额外延迟达到约定值 | `unverified` | 客户场景未定义，合成报告未执行 | 尚无 | 尚无 | 尚未执行 | 不适用，验证后更新 |
| CLM-009 | Token 消耗或成本相对客户现有助手下降 | `unverified` | 未定义同模型、同任务、同工具、同数据的对照 | 尚无 | 尚无 | 尚未执行 | 不适用，验证后更新 |
| CLM-010 | 发布基线已经冻结且客户拿到的是由唯一集成提交重建的候选包 | `unverified` | 1.0.1 当前仍在本地修复分支 | `handoff/03_windows_connector_acceptance_1.0.1.md` | 由 `SHA256SUMS` 校验 | 2026-11-17 / 文档审计 | HWC-043 完成后更新 |

## 状态升级规则

1. `report_claim` 升级为 `independently_reproduced` 前，必须收集原始证据或重新执行，并保存命令、环境、执行人、时间、结果和 SHA-256。
2. `planned_target` 只有实际测试达到门槛并有原始时间序列时才能升级；部分成功、平均延迟或口头确认不能替代。
3. 任何适用范围、版本、来源或客户环境变化都使原证据失效，需要重新评审。
4. 对外材料只能引用已批准范围的状态；`unverified` 和 `planned_target` 不得写成“已完成”“已闭环”“已验证”或“保证”。
