# DeepSeek Harness 业务 MCP 连接验收

日期：2026-11-10。服务器：学校云。工作目录：`open-claude-code`。

## 结论与范围

接续 [真实执行链路切片](HARNESS_RUNTIME_2026-11-10.md)，已将业务 MCP 连接接入 Harness，并复用现有授权、人工审批、双人审批、来源与执行记录。真实官方 Node Harness、Python 官方 MCP 客户端和本地 HTTP MCP 服务已完成端到端验收。模型回答和业务数据均为明确标注的合成数据，没有使用真实客户连接或付费模型。

默认执行器仍为 Claude；没有重启现有服务、切换生产、发布版本或替换旧会话。此结论只覆盖业务连接切片，不代表全部功能迁移完成。

## 实现与复用

| 位置 | 本轮变化 |
|---|---|
| `server/app/mcp_tools.py` | 官方 MCP Python SDK 1.30.0 负责 HTTP 协议；jsonschema 4.26.0 和 referencing 0.37.0 离线验证参数；可信路由、凭据、定义变化检查、超时和响应限制 |
| `server/app/harness_tools.py` | 业务别名映射、完整参数审批、当前授权复核、真实来源登记与引用标记；来源事件只归属本次调用 |
| `server/app/harness_runtime.py` | 移除业务连接一律拒绝的试点限制，仍按会话固定执行器并每轮启动独立进程 |
| `server/harness/profile.py`、`workspace_plugin.ts` | 仅允许宿主下发的 `external_` 摘要别名；传递完整参数定义，禁止连接地址、凭据和执行代码；不加载原生 MCP 模块 |
| `server/pyproject.toml` | 直接固定 JSON Schema 验证依赖；现有 MCP 版本保持不变 |
| `scripts/evaluation_gate.py` | 新适配模块纳入交付门禁范围 |

没有自行实现 MCP 协议，也没有让 Node 持有业务凭据。三个复用组件均为 MIT；本机许可证文件摘要和官方来源记录在 [上游清单](../upstream-sources.json)。选择官方 Python 客户端而不启用 Harness 原生 MCP 的原因、退出方式见 [设计第 8 节](../design/DEEPSEEK_HARNESS_MIGRATION.md)。

## 验证结果

以下是不同批次及针对性补测。补测有重叠，不能直接相加作为一次完整测试总数。

| 批次 | 结果 | 证据说明 |
|---|---|---|
| 真实 Harness 与 HTTP MCP | 4 项通过，0 失败/跳过，90.33 秒 | 9 个真实 Harness 进程、17 次本地合成模型请求、4 次业务工具 HTTP 调用、0 次付费请求；全部子进程及本地服务线程已回收 |
| 最终来源归属改动的真实进程补测 | 1 项通过，30.56 秒 | 复跑上述第一项；3 个进程、6 次本地模型请求、2 次业务调用，不增加独立场景数量 |
| 真实 HTTP 与宿主边界 | 40 项通过，0 失败/跳过，267.87 秒 | 官方 Python 客户端连接本地 JSON/SSE MCP；审批、撤权、定义变化、错误与传输故障 |
| 来源归属边界补测 | 2 项通过，25.14 秒 | 一项已有读取路径、一项新增并发来源场景；与前批次合并覆盖 41 个独立边界场景 |
| 插件配置与隔离 | 最终 59 项通过，3.41 秒 | 原始 85 项批次中一条测试误把临时路径名中的 `mcp` 当成原生插件，修正为模块列表断言后复跑全部 59 项 |
| 原有工具审批、传输和启动器 | 26 项通过 | 来自上述 85 项批次中未失败的其余用例；后续仅改业务 MCP 分支及测试断言 |
| Schema、凭据和异常日志 | 最终 26 项通过，0.79 秒 | 特殊字符凭据、三层 JSON 编码、合法定义不被改变、本地引用与草案语义、官方异常解析日志 |
| 类型、构建和门禁 | 严格 TypeScript 检查、Python wheel 构建与安装布局导入通过；门禁测试 2 项通过；合成基准 4/4 | 逐字节核验 7 个应用/插件资源与当前源码一致、3 个依赖版本准确；没有发布或测量真实模型质量 |

真实进程的四个场景包括：

1. 只展示已授权工具，完整传递嵌套对象/数组；成功返回实际 MCP 来源并生成工作台引用；远端错误不产生新来源；未授权工具不派发。
2. 人工拒绝时远端执行次数为零；人工允许后参数保持一致，只执行一次并保留审批人和决策来源。
3. 双人审批拒绝申请人自批和同一管理员重复投票；第一位管理员同意后仍不执行，第二位不同管理员同意后才执行一次。
4. 审批等待期间取消、撤回连接或撤回工具授权，均不向业务服务发出工具调用。取消后再次发送可以恢复原生 Harness 历史。

HTTP 边界还覆盖初始化/工具发现期间的撤权、禁用、策略变化与取消；远端定义变化、数据库定义变化但 revision 未更新；外部 schema 引用、非法参数和路由字段；JSON 与 SSE 凭据回显；响应超限、压缩、同源重定向、坏协议、总期限与取消。模型代审没有放开高风险工具，验证的是按原规则回到人工审批。

本轮审查实际发现并修复了三个问题：官方客户端自行跟随同源重定向可能重放 POST；特殊字符密钥可通过 JSON 编码绕过简单替换；官方解析异常会将远端正文写进日志。增加拒绝重定向、递归编码检查及调用上下文日志过滤后，回归通过。日志测试同时验证独立并发任务和调用结束后的正常日志不受影响。

另修正了 `tool.completed` 的来源归属：业务 HTTP 等待期间其他工具产生的来源，不能混入本次事件；使用本次 `register_mcp` 返回的来源编号，包括同内容复用的已有编号。该最后改动由真实进程第一项补测和专门的来源边界测试覆盖，没有把早先已导入的测试进程视为自动覆盖了新代码。

## 原始证据

以下路径位于忽略目录，不属于正式发布包：

- `.runtime/verification/harness-mcp-real-20261110-d.{log,xml}`
- `.runtime/verification/harness-mcp-real-20261110-final-source.{log,xml}`
- `.runtime/verification/harness-mcp-boundary-final-20261110T131859Z.{log,xml}`
- `.runtime/verification/harness-mcp-attribution-20261110T132254Z.{log,xml}`
- `.runtime/verification/harness-mcp-regression-security-20261110.{log,xml}`（首次批次，包括已修正的测试断言失败）
- `.runtime/verification/harness-mcp-regression-profile-final-20261110.{log,xml}`
- `.runtime/verification/harness-mcp-regression-schema-final-20261110.{log,xml}`
- `.runtime/verification/harness-mcp-regression-typescript-final-20261110.log`
- `.runtime/verification/harness-mcp-evaluation-tests-final-20261110.xml`
- `.runtime/verification/harness-mcp-evaluation-final-20261110.json`
- `.runtime/verification/harness-mcp-wheel-final-20261110.{log,json}`

端到端 a/b 轮用于修正合成模型和测试清理逻辑，c 轮为同步应用修复主动中断；验收只使用 d 轮及后续来源补测。其他中断的边界批次同样不充当最终证据。

## 保留的限制与下一步

当前兼容文本和结构化 JSON 的 Streamable HTTP MCP，支持既有无认证/Bearer 连接。拒绝外部 schema 引用、`$id`、重定向、压缩响应和图片/资源内容；参数、定义、响应与期限限制见 [试点操作](../operations/HARNESS_PILOT.md)。这些是明确的试点兼容边界，客户连接仍需逐一验收。

“不自动重试”是指传输层不重发业务 POST；不能保证模型或用户另起一次调用时业务系统会自动去重。已经发给外部服务的写操作也不能通过本地取消撤销。网络失败后应先核对外部状态。现有连接发现路径没有在本轮追加日志改造。

这是 2026-11-10 的阶段结论。后续增量已补齐附件、上下文统计、受控记忆和手动压缩；下一阶段仍需完整用量、自动整理、文件工具、真实子代理、通用受控插件、真实供应商功能和质量验证。图片生成及官方 HippoRAG 的既有缺口不因业务连接迁移完成而改变。需求 `HAR-003` 保持部分实现，`HAR-004` 仍为部分实现。
