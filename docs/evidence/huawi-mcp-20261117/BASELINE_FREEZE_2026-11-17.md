# 华微内网 MCP 桥接需求计划冻结基线

- 基线编号：`HWC-MCP-PLAN-v0.4-20261117`
- 冻结时间：`2026-11-17T18:03:19+08:00`
- 项目分支：`feature/intranet-mcp-connector-20261116`
- 冻结时 HEAD：`a30c8fb2bc84ece86a57ca6c70a72a21f0a74b9e`
- 需求计划：`docs/plans/2026-11-17-huawi-kaiwu-mcp-bridge-requirements-and-delivery-plan.md`
- 需求计划版本：`v0.4-review`
- 需求计划 SHA-256：`c3fbb5789202bdf474b4b77c59a5cc42c51872293973b9be9435ed63f825bace`
- 候选包 SHA-256：`9305e142580ce3908b1b5c334f6f3fea160f0767be23702aaab99c2c46fe5e1f`
- 需求编号：`HWC-001` 至 `HWC-061`，共 61 项；其中 59 项 P0、2 项 P1

## 冻结范围

本基线冻结需求计划及本目录全部证据文件，用于第三轮业务可用性、安全和交付验收独立只读复核。冻结后至三路审核全部返回前，计划与证据文件不得修改；审核子任务只能返回审核文本，不得写入本目录或业务代码。

第三轮审核只判断 `v0.4-review` 是否形成相互一致、可执行、可测试的计划，不把“文档要求已写明”误判为“功能已实现”，也不把已有报告摘要升级为独立复现证据。

此前基于持续漂移文件启动的第三轮审核尝试无效，其任何阶段性文本均不得作为通过证明。

## 冻结输入清单

| 文件 | SHA-256 |
| --- | --- |
| `docs/plans/2026-11-17-huawi-kaiwu-mcp-bridge-requirements-and-delivery-plan.md` | `c3fbb5789202bdf474b4b77c59a5cc42c51872293973b9be9435ed63f825bace` |
| `docs/evidence/huawi-mcp-20261117/CLAIM_LEDGER_2026-11-17.md` | `c456b4ac5fc18fbaa5c17323fe2f8f5999ead5ac7ec0d0b222c2a21a75d4c302` |
| `docs/evidence/huawi-mcp-20261117/INDEPENDENT_REVIEW_2026-11-17.md` | `ad5236df9dd69f8888a1638512d3e096a75ea46019cf87b98e89050dcd5b6803` |
| `docs/evidence/huawi-mcp-20261117/README.md` | `0cbcf5cca341c54f08c4209996258a01b53dc68fd2d039f8711b788e4e770778` |
| `docs/evidence/huawi-mcp-20261117/SECOND_ROUND_INDEPENDENT_REVIEW_2026-11-17.md` | `3e522a61bb876f7cbc8bcdec6f8fb0269edde8491e3fea814e427c4349105b91` |
| `docs/evidence/huawi-mcp-20261117/artifacts/kaiwu-connector-1.0.1-windows-amd64.zip` | `9305e142580ce3908b1b5c334f6f3fea160f0767be23702aaab99c2c46fe5e1f` |
| `docs/evidence/huawi-mcp-20261117/handoff/00_cloud_engineer_handoff.md` | `80e8fcb866d00cdf8605043858b98064e7a5032e2ab8e62b34af60fee04ce4fa` |
| `docs/evidence/huawi-mcp-20261117/handoff/01_cloud_interface_contract.md` | `393ce3401da76abbec2a1ebcafda69af3078c7c124c261a5ec4464b3f1834bae` |
| `docs/evidence/huawi-mcp-20261117/handoff/02_customer_interface_notes.docx` | `e63121b1332b91315635a63b0683f63b61751f539204a1b03331fb65129929de` |
| `docs/evidence/huawi-mcp-20261117/handoff/02_customer_interface_notes.txt` | `3ec71fe917c024b73ce8ba666e38f5e9647186e9df0b08afe3597480dd0d69fd` |
| `docs/evidence/huawi-mcp-20261117/handoff/03_windows_connector_acceptance_1.0.1.md` | `7ec44b3c6187977a6b7aac0a8f8b266096ac368265dd5988b611e2ed3ae7f1af` |
| `docs/evidence/huawi-mcp-20261117/handoff/04_test_summary_1.0.1.json` | `f2247d9bd12f5bef80ce370bef606675e965066b15d7735628e34a85ab00f619` |
| `docs/evidence/huawi-mcp-20261117/handoff/05_platform_export_1.0.1.json` | `c8a5a71c3c766d31695958eb4d8fbd9faa301ce86c0b4410082600cedeb0d8a5` |
| `docs/evidence/huawi-mcp-20261117/source/2026-11-17_customer_technical_confirmation_1658.docx` | `f50cf66826aa88163e8d4f0381eef8e33d05295fcd062e528649f82811d1d68f` |
| `docs/evidence/huawi-mcp-20261117/source/2026-11-17_customer_technical_confirmation_1658.txt` | `8563accb85bcdf6fab30ea112f0dbc38b3d4edb89db07b5d8a1a3bc0ecf08931` |
| `docs/evidence/huawi-mcp-20261117/source/2026-11-17_customer_technical_confirmation_initial.docx` | `fa333da87c51a854db6be711bc74ee1e47daa5fa8cc507d580fc0df1fef9a27a` |
| `docs/evidence/huawi-mcp-20261117/source/2026-11-17_customer_technical_confirmation_initial.txt` | `5dbfe182293a853a2d3689d5bb0c7f4bf6934d59351bd59389ca2e88ff86edaf` |
| `docs/evidence/huawi-mcp-20261117/source/2026-11-17_mcp_auth_rules.docx` | `113f1194b10cf18507cc0b7be73b0451f5137edce69f54e1ce30bf4043502434` |
| `docs/evidence/huawi-mcp-20261117/source/2026-11-17_mcp_auth_rules.txt` | `b3654b6862c4940d99fb17fa00ee0c94e30461cd8530d6fe916a9ef5fcd9b10d` |

## 审核后处理

1. 三路结论汇总保存为 `THIRD_ROUND_INDEPENDENT_REVIEW_2026-11-17.md`。
2. 如存在阻断项，只修订为新的计划版本并重新冻结，不沿用本基线审核结论。
3. 三路均确认阻断项关闭后，才可由项目负责人宣布该计划“可进入实现的冻结基线”；这不等于功能完成、C 段闭环或正式可交付。
4. 最终重新生成本目录 `SHA256SUMS`，使其包含冻结记录和第三轮复核文件。
