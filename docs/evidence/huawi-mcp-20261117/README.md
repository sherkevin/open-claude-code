# 华微内网 MCP 接入：2026-11-17 证据快照

## 用途

本目录保存华微内网 MCP 接入开物平台评审所依赖的客户说明、认证规则、云端交接契约、连接器验收报告、候选安装包、独立审核记录和声明台账。它用于让项目文档不再只依赖 `.runtime/` 临时目录。

本目录不是运行时依赖。代码和服务不得从本目录读取凭据、配置或业务数据。

## 来源

| 来源 | 说明 |
| --- | --- |
| 飞书“自己给自己”会话 `oc_3cd1684993ebcdb986de508b30cf15e1` | 2026-11-17 的客户技术说明初始版、16:58 更新版和 MCP 调用认证说明 |
| 飞书文件夹 `【华微 MCP 接入 · 云端交接 2026-11-17】` | 云端工程师交接说明、接口契约、客户接口说明、Windows 连接器 1.0.1 验收和平台导出示例 |
| 学校云项目 `.runtime/feishu-self-recent-20261117/` | 上述客户附件的本地下载与文本提取 |
| 学校云项目 `.runtime/feishu-kaiwu-handoff-20261117/` | 上述交接材料、转换文本和 Windows 候选包 |

未保存完整飞书聊天 JSON，避免把与本次技术评审无关的私人会话内容固化进项目。

## 文件清单

### `source/`

| 文件 | 原始文件或文本 | 用途 |
| --- | --- | --- |
| `2026-11-17_customer_technical_confirmation_initial.docx` | `01_华微灵犀_MCP_接入开物平台技术说明.docx` | 客户较早的 MCP 接入说明原件 |
| `2026-11-17_customer_technical_confirmation_initial.txt` | 对应文本提取 | 便于检索和差异比较 |
| `2026-11-17_customer_technical_confirmation_1658.docx` | `03_华微灵犀_MCP_接入开物平台技术说明_1658.docx` | 最新客户说明，明确 HwMain 业务 MCP 位于 Windows |
| `2026-11-17_customer_technical_confirmation_1658.txt` | 最新说明文本提取 | 当前需求分析的主要事实来源 |
| `2026-11-17_mcp_auth_rules.docx` | `02_MCP_调用与认证说明.docx` | 华微 HTTP MCP、固定 Bearer、动态签名和返回格式说明原件 |
| `2026-11-17_mcp_auth_rules.txt` | 对应文本提取 | HMAC 规则和离线样例的检索依据 |

### `handoff/`

| 文件 | 原始文件 | 用途 |
| --- | --- | --- |
| `00_cloud_engineer_handoff.md` | `00_云端工程师交接说明.md` | 云端 C 段交接背景和范围 |
| `01_cloud_interface_contract.md` | `01_云端接口契约.md` | 连接器实际调用的控制面接口和 JSON 示例 |
| `02_customer_interface_notes.docx` | `02_华微灵犀智能体接口接入技术说明.docx` | 较早的客户接口核实材料原件 |
| `02_customer_interface_notes.txt` | 对应转换文本 | 便于检索和差异比较 |
| `03_windows_connector_acceptance_1.0.1.md` | `03_KaiwuConnectorWindowsAcceptanceReport-1.0.1.md` | Windows 连接器 1.0.1 本地 A/B 验收和 C 段阻塞说明 |
| `04_test_summary_1.0.1.json` | `04_test-summary-1.0.1.json` | A/B/C 验收结果和证据索引 |
| `05_platform_export_1.0.1.json` | `05_mcp-platform-export-1.0.1.json` | 平台导入 JSON 形态和占位令牌示例 |

### `artifacts/`

| 文件 | 说明 |
| --- | --- |
| `kaiwu-connector-1.1.0-windows-amd64.zip` | 当前 Windows 本机诊断候选包，版本 `1.1.0`，提交 `b098e3a`，从干净 isolated worktree 交叉编译 |
| `kaiwu-connector-1.1.0-linux-amd64.tar.gz` | 当前 Linux 构建与包级启动验证产物，版本 `1.1.0`，提交 `b098e3a` |
| `kaiwu-connector-1.0.1-windows-amd64.zip` | 2026-11-17 的 Windows 候选安装包，版本 `1.0.1`，提交 `89c38b2`，构建时间 `2026-11-17T05:51:52Z` |

当前 `1.1.0` 候选包 SHA-256：

```text
2BF5E93E5E27476A28CE92E1A7C9A22182E222194D1C73C547111415B8BF3964  kaiwu-connector-1.1.0-windows-amd64.zip
39D5057EA9A811009E2CC4856F081A3E1F5DB2AB4BB99A398831C6C4E0CB9176  kaiwu-connector-1.1.0-linux-amd64.tar.gz
```

历史 `1.0.1` 包 SHA-256：

```text
9305E142580CE3908B1B5C334F6F3FEA160F0767BE23702AAAB99C2C46FE5E1F
```

`1.0.1` 哈希与历史验收报告和测试摘要一致。当前 `1.1.0` 包已经完成源码测试、竞态测试、漏洞扫描、SBOM、双平台交叉构建、内层哈希和 Linux 包级启动验证，但尚未在 Windows 实机执行。两个版本都不能作为“平台端到端已接通”的证明。

### 评审与声明文档

| 文件 | 用途 |
| --- | --- |
| `INDEPENDENT_REVIEW_2026-11-17.md` | 首轮业务、安全和交付三路独立审核记录，以及 `v0.2-review` 的处置 |
| `SECOND_ROUND_INDEPENDENT_REVIEW_2026-11-17.md` | 第二轮独立复核记录；明确 `v0.2-review` 不通过及 `v0.3-review` 的修订映射 |
| `CLAIM_LEDGER_2026-11-17.md` | 把“已独立复现”“仅报告声称”“计划目标”和“未验证”分开登记 |
| `BASELINE_FREEZE_2026-11-17.md` | `v0.4-review` 及其证据快照的冻结时间、提交和逐文件 SHA-256；第三轮独立审核必须针对该冻结对象 |

当前需求与交付计划位于
`docs/plans/2026-11-17-huawi-kaiwu-mcp-bridge-requirements-and-delivery-plan.md`，
状态为 `v0.7-diagnostic-candidate`。第三轮审核意见已经进入 `v0.6-execution-baseline`，当前 `v0.7` 记录 `1.1.0` 候选包的构建和验证进展；这仍不代表云端 C 段已闭环或产品已可正式交付。

## 已确认的关键事实

1. HwMain 业务 MCP 运行在 Windows Server 2019 Datacenter，64 位，Build `17763.9020`。
2. 候选销售 MCP 入口为 `https://svr39.bjhwsj.com/SysDevCore/mcp/sales_test`，初始化探测返回 HTTP 401。
3. Linux OpenClaw 身份代理历史监听 `127.0.0.1:18790/mcp`，Windows 连接器不能把该地址当作其他主机的回环入口。
4. `initialize`、`tools/list` 和 `ping` 使用固定 Bearer；`tools/call` 还需要 `appid`、`channel`、`userid`、`requestId`、`timestamp`、`sign`。
5. HMAC 原文顺序为 `appid=<...>&channel=<...>&userid=<...>&requestId=<...>&timestamp=<...>`，使用 HMAC-SHA256 和小写十六进制。
6. `1.0.1` 的 Windows 本机安装、服务、回环页面、本地 MCP 导入、诊断、错误凭据拒绝、服务重启和卸载通过；云端 C 段未闭环。
7. `1.1.0` 新增本机动态认证代理、固定上游、来源编号稳定化、传输和协议加固、默认失败关闭的云端操作以及受控构建合规文件；目前只有 Linux 合成和包级证据，尚未完成 Windows 实机。

## 证据完整性限制

验收报告引用了 `evidence/diagnose.json`、截图、服务配置、ACL、监听地址、防火墙和网络探测等原始文件，但这些文件当前没有随飞书交接材料或本地快照提供。因此：

1. Windows A/B 在当前证据快照中只能标记为“验收报告声称通过”，不能标记为“由本项目独立复现通过”。
2. `1.0.1` 候选包的 SHA-256 可以由当前 ZIP 独立复算，只证明该包未被替换，不证明 Windows A/B 执行过程。
3. 正常停止连接器后 5 秒内返回 `503` 尚未验证，只是 C 段联调的验收目标。
4. 补齐原始证据或重新执行后，必须更新 `CLAIM_LEDGER_2026-11-17.md`，不能只改文字结论。
5. 验收报告中的系统版本写作“Windows 10 Home China，2009，Build 26200”，该组合与常见 Windows 版本命名不一致；在没有 `windows-info.txt` 的情况下，不能把这段文字当作可审计环境证据。
6. `1.1.0` 的 Windows ZIP 只证明交叉编译成功和包内哈希一致；当前环境没有 Windows 运行时，不能据此声明安装脚本、Windows 服务、重启恢复或卸载已经通过。
7. `1.1.0` 尚未执行恶意软件扫描、秘密扫描、Authenticode 签名和正式发布独立双构建，不能进入 G7a 或正式交付。

## 敏感信息检查

本次快照中的认证说明使用虚构的 `demo-key-only`、示例用户和示例签名；平台导出使用 `<EDGE_ACCESS_TOKEN>` 占位符。没有复制真实客户 Bearer、HMAC 密钥、设备令牌、平台授权 A 或真实业务数据。

如果以后收到的真实凭据需要用于联调，应通过安全秘密渠道注入，不能复制到本目录、项目 Git、聊天正文或普通工单。

## 完整性校验

在本目录执行：

```bash
sha256sum -c SHA256SUMS
```

`SHA256SUMS` 使用本目录内的相对路径，必须在 `docs/evidence/huawi-mcp-20261117/` 目录执行。

## 保留和引用原则

- 本目录里的原件和文本提取都保留原始内容，不手工改写客户材料。
- 需求分析、冲突判断和验收结论写在工作计划与独立审核报告中，不回写历史原件。
- 若客户更新技术说明，新增带日期和后缀的文件，不覆盖本次快照。
- 正式交付时，以经双方确认的版本控制文件为准；本快照只用于可追溯性。
