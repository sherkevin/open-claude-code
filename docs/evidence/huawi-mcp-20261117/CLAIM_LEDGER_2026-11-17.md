# 华微内网 MCP 桥接声明台账

- 台账日期：2026-11-18（初版 2026-11-17）
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
| CLM-010 | 发布基线已经冻结且客户拿到的是由唯一集成提交重建的候选包 | `unverified` | `1.2.1` 已从提交 `9ccfdf5` 的干净隔离工作树构建，但尚未合入集成主线，也尚未推送本轮提交 | `CONNECTOR_1.2.1_BUILD_EVIDENCE_2026-11-18.md` | 由 `SHA256SUMS` 校验 | 2026-11-18 / 文档审计 | HWC-043 完成主分支集成后更新 |
| CLM-011 | 历史 Windows 候选包 `kaiwu-connector-1.1.0-windows-amd64.zip` 的 SHA-256 为 `2BF5E93E5E27476A28CE92E1A7C9A22182E222194D1C73C547111415B8BF3964` | `independently_reproduced` | 当前保存的历史 `1.1.0` 候选包 | `artifacts/kaiwu-connector-1.1.0-windows-amd64.zip` | `2BF5E93E5E27476A28CE92E1A7C9A22182E222194D1C73C547111415B8BF3964` | 2026-11-17 / 学校云项目构建与哈希检查 | 包被替换或重新构建时 |
| CLM-012 | 历史 Linux 候选包 `kaiwu-connector-1.1.0-linux-amd64.tar.gz` 的 SHA-256 为 `39D5057EA9A811009E2CC4856F081A3E1F5DB2AB4BB99A398831C6C4E0CB9176` | `independently_reproduced` | 当前保存的历史 `1.1.0` 候选包 | `artifacts/kaiwu-connector-1.1.0-linux-amd64.tar.gz` | `39D5057EA9A811009E2CC4856F081A3E1F5DB2AB4BB99A398831C6C4E0CB9176` | 2026-11-17 / 学校云项目构建与哈希检查 | 包被替换或重新构建时 |
| CLM-013 | `1.1.0` 在 Go 1.27.4 下通过 `go test ./...`、`go test -race ./...` 和 `go vet ./...` | `independently_reproduced` | 合成单元测试和竞态检查；不包含 Windows 实机、C 段或真实客户数据 | `CONNECTOR_1.1.0_BUILD_EVIDENCE_2026-11-17.md` | 由 `SHA256SUMS` 校验 | 2026-11-17 / 学校云项目构建检查 | 源码或依赖变化时 |
| CLM-014 | `govulncheck v1.9.0` 对历史 `1.1.0` 依赖返回 `No vulnerabilities found.` | `independently_reproduced` | 扫描时为 2026-11-11 漏洞库；只代表 Go 漏洞库结果，不代表恶意软件或秘密扫描 | `CONNECTOR_1.1.0_BUILD_EVIDENCE_2026-11-17.md` | 由 `SHA256SUMS` 校验 | 2026-11-17 / 学校云项目构建检查 | 漏洞库、源码或依赖变化时 |
| CLM-015 | `1.1.0` Windows 产物是来自提交 `b098e3a` 的 x86-64 PE，二进制的 Go VCS 信息为 `vcs.modified=false` | `independently_reproduced` | 只证明交叉编译与构建来源，不证明 Windows 运行 | `artifacts/kaiwu-connector-1.1.0-windows-amd64.zip` | `2BF5E93E5E27476A28CE92E1A7C9A22182E222194D1C73C547111415B8BF3964` | 2026-11-17 / 学校云项目构建检查 | 包被替换或重新构建时 |
| CLM-016 | Linux 最终包解包后可以启动本机页面，且未配置云端时状态为 `UNPAIRED`、`cloud_configured=false`、`edge_configured=false` | `independently_reproduced` | Linux 包级启动和失败关闭检查；不代表 Windows 行为 | `CONNECTOR_1.1.0_BUILD_EVIDENCE_2026-11-17.md` | 由 `SHA256SUMS` 校验 | 2026-11-17 / 学校云项目包级烟雾检查 | 包被替换或重新构建时 |
| CLM-017 | `1.1.0` 已在 Windows 机器完成安装、服务启动、MCP 导入、重启恢复和卸载 | `unverified` | 当前没有 Windows 运行环境和实机证据 | 尚无 | 尚无 | 尚未执行 | Windows 实机验收后更新 |
| CLM-018 | `1.1.0` 已通过恶意软件扫描、秘密扫描、Authenticode 签名和正式发布独立双构建 | `unverified` | HWC-061/HWC-059 正式门禁；当前只完成 SBOM、许可证、漏洞扫描和单次干净构建 | 尚无 | 尚无 | 尚未执行 | 完成扫描、签名和独立构建比对后更新 |
| CLM-019 | `1.2.0` 在 Windows 10 本地测试机完成安装、服务、回环页面、模拟 HTTP MCP、错误凭据拒绝、服务重启和卸载 | `report_claim` | 报告和原始证据已归档；测试机不是客户目标 Windows Server 2019，且没有执行整机重启 | `windows-1.2.0/KaiwuConnectorWindowsAcceptanceReport-1.2.0.md`、`windows-1.2.0/KaiwuConnectorWindowsEvidence.zip` | `ad471c6045c887b0ef3e5a169c8198ff6224af0fbe60f1d5912a48169342a528`（证据压缩包） | 2026-11-17 / Windows 现场测试人员 | 重测或报告被修订时 |
| CLM-020 | `1.2.0` 重复导入同一 MCP 地址会清空管理员批准的工具允许列表 | `independently_reproduced` | 现场证据和源码路径均已复核；由 `1.2.1` 自动化回归覆盖 | `WINDOWS_1.2.0_ACCEPTANCE_REVIEW_2026-11-17.md` | 由 `SHA256SUMS` 校验 | 2026-11-17 / 学校云项目复核 | 相关源码再次变化时 |
| CLM-021 | `1.2.1` 重复导入保持稳定来源编号并保留管理员批准的工具允许列表 | `independently_reproduced` | Go 自动化测试 `TestReimportPreservesApprovedToolAllowlist`；不代表 Windows 页面已复测 | `CONNECTOR_1.2.1_BUILD_EVIDENCE_2026-11-18.md` | 由 `SHA256SUMS` 校验 | 2026-11-18 / 学校云项目测试 | 源码变化或 Windows 重测后更新 |
| CLM-022 | 当前 Windows 包 `kaiwu-connector-1.2.1-windows-amd64.zip` 的 SHA-256 为 `34e0f560287b8649c677e557c36cf1cc6dbf80578ec02411db792c3c3d175266` | `independently_reproduced` | 当前保存的 `1.2.1` 干净构建候选包 | `artifacts/kaiwu-connector-1.2.1-windows-amd64.zip` | `34e0f560287b8649c677e557c36cf1cc6dbf80578ec02411db792c3c3d175266` | 2026-11-18 / 学校云项目构建与哈希检查 | 包被替换或重新构建时 |
| CLM-023 | 当前 Linux 包 `kaiwu-connector-1.2.1-linux-amd64.tar.gz` 的 SHA-256 为 `e2c9c4d040c2876b5599d73a035b134279d29e2a6070f9257aee4e7556fea6f8` | `independently_reproduced` | 当前保存的 `1.2.1` 干净构建候选包 | `artifacts/kaiwu-connector-1.2.1-linux-amd64.tar.gz` | `e2c9c4d040c2876b5599d73a035b134279d29e2a6070f9257aee4e7556fea6f8` | 2026-11-18 / 学校云项目构建与哈希检查 | 包被替换或重新构建时 |
| CLM-024 | `1.2.1` Windows 产物来自提交 `9ccfdf5` 的 x86-64 PE，Go VCS 信息为 `vcs.modified=false` | `independently_reproduced` | 只证明干净交叉编译和构建来源，不证明 Windows 运行 | `artifacts/kaiwu-connector-1.2.1-windows-amd64.zip` | `34e0f560287b8649c677e557c36cf1cc6dbf80578ec02411db792c3c3d175266` | 2026-11-18 / 学校云项目构建检查 | 包被替换或重新构建时 |
| CLM-025 | `1.2.1` Linux 最终包可以启动本机页面，未配置云端时状态为 `UNPAIRED`、`cloud_configured=false`、`edge_configured=false` | `independently_reproduced` | Linux 包级启动和失败关闭检查；不代表 Windows 行为 | `CONNECTOR_1.2.1_BUILD_EVIDENCE_2026-11-18.md` | 由 `SHA256SUMS` 校验 | 2026-11-18 / 学校云项目包级烟雾检查 | 包被替换或重新构建时 |
| CLM-026 | `1.2.1` 已在 Windows 实机完成 D01、D02、整机重启和安全软件默认开启复测 | `unverified` | 当前没有 Windows 运行环境和实机复测证据 | 尚无 | 尚无 | 尚未执行 | Windows 实机复测后更新 |

## 状态升级规则

1. `report_claim` 升级为 `independently_reproduced` 前，必须收集原始证据或重新执行，并保存命令、环境、执行人、时间、结果和 SHA-256。
2. `planned_target` 只有实际测试达到门槛并有原始时间序列时才能升级；部分成功、平均延迟或口头确认不能替代。
3. 任何适用范围、版本、来源或客户环境变化都使原证据失效，需要重新评审。
4. 对外材料只能引用已批准范围的状态；`unverified` 和 `planned_target` 不得写成“已完成”“已闭环”“已验证”或“保证”。
