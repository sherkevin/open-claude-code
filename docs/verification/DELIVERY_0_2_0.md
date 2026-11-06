# 0.2.0 交付验证记录

更新日期：2026-11-05。

Release version: 0.2.0
Release readiness: FINAL

交付物是可直接使用的企业 Agent Web 客户端与执行服务。华微只需提供内部读取数据的 MCP；模型调用、持续会话、上下文与压缩、跨会话记忆由本产品负责。客户不需要自备 Harness。本记录的正式交付指本版本产品及约定技术验证完成；真实客户业务验收仍需客户提供 MCP 和样本。

## 本轮验证

| 验证项 | 实际结果 | 证据边界 |
| --- | --- | --- |
| server 自动测试 | 腾讯云 Python3.10 完整74/74通过 | 覆盖MCP导入、身份与文件隔离、记忆版本和并发、上下文缓存、真实边界与异常终态的应用逻辑 |
| client 单测与构建 | 17/17通过，TypeScript/Vite生产构建完成 | client与server版本均为0.2.0 |
| 真实模型浏览器主流程 | 8/8通过，2个真实推理任务 | 页面完成脱敏MCP导入、发现工具、绑定只读助手、真实MCP查询、新会话记忆回忆、手动原生计数；不是伪造API响应 |
| 最终客户端浏览器回归 | 7/7通过，0个推理任务 | 补验未保存草稿的取消/放弃导航、助手记忆隔离、保存稳态、桌面和390px移动端；无意外console/page/HTTP/network错误 |
| 原生手动压缩 | 实际compact_boundary，原会话后续回忆通过 | 原生统计从9016到7621 tokens，边界pre_tokens为9113；不同观测时点单独记录，不据此宣称节省比例或供应商真实窗口上限 |
| 原生跨会话记忆 | 模型Write成功，新会话自然问法回忆成功，清空后新会话无该记忆 | 记忆读取使用原生Read按需机制；同用户同助手共享，不是应用另外注入摘要 |
| 压缩后重启恢复 | 四次受控重启后，原SDK会话恢复且正确回忆随机标记 | 该回忆问题未重复标记，未调用工具或长期记忆；不推广为任意故障的无损恢复保证 |
| 执行中重启 | graceful/crash × 等待写审批/执行慢读取，四组共26检查通过 | 8个真实推理任务；旧审批拒绝决定返回409，无自动重放、无合成工单写入、历史与SSE终态保留，后续消息可运行 |
| systemd托管 | 三个项目专属unit非root运行，现有回环端口健康 | 两次强制退出后自动重新启动；正常停机TERM主进程、超时KILL进程组，避免提前终止SDK子进程 |
| 文件引用边界 | 普通消息与原生压缩说明中的合成外部@路径两项通过 | 验收自己创建并删除可读sentinel；未知内容未进入实际答案或原始SDK事件，仅覆盖本次固定SDK/CLI路径 |
| 独立审查修复 | 三项P2均修复并定向/实际回归 | 首次并发记忆读取、页面内未保存草稿、压缩已发生后中断的准确状态 |

真实模型为已配置的 `deepseek-v4.1-flash-1017`，经获授权的腾讯云模型服务执行。原生SDK固定为0.2.178。费用未可靠提供时显示未知，不使用其他模型单价替代账单。

74项server测试包含原生事件的应用层测试，但它们本身不是原生推理证据；表中的原生压缩、记忆、恢复和浏览器真实任务另行通过实际模型执行。自动压缩保持由原生运行时负责，本轮未通过压力输入触发其阈值。

## 发现并修复的问题

Linux回归发现上下文统计被 `asyncio.wait_for` 移到另一个Task，现已保持SDK操作由原会话actor执行。独立审查发现的记忆并发初始化竞争已用同一可重入锁保护；客户端离开未保存记忆时需要明确选择；真实压缩边界发生后保留完成证据，后续取消/失败另行记录。

首次正常重启检查曾因systemd向整个进程组同时发送TERM，导致待审批任务提前被记为失败。已改为 `KillMode=mixed`，保留首次失败报告，并使用新的状态文件完整重跑四组通过。未用修改预期状态的方式掩盖失败。

## 复现与运行资料

从项目根目录，使用已安装依赖和私有配置执行：

```bash
cd server
../.venv/bin/python -m pytest -q -p no:cacheprovider
cd ../client
npm test
npm run build
cd ..
node scripts/complete_client_browser.mjs --password-file .runtime/admin-password
.venv/bin/python scripts/native_context_smoke.py plan
.venv/bin/python scripts/restart_safety_smoke.py plan
```

浏览器 `REAL_MODEL=true`、原生脚本 `--real-model` 才执行对应真实推理任务。模型与Token计数可能产生用量。完整命令、数据范围和必要停机步骤见 [工作台指引](WORKSPACE_GUIDE.md)、[原生上下文验收](verification/NATIVE_CONTEXT_MEMORY.md)、[重启验收](verification/RESTART_SAFETY.md) 和 [运维手册](OPERATIONS.md)。

原始证据受控保存在维护者私有目录，包括 `complete-client-browser.json`、`complete-client-browser-final-ui.json`、`native-context-report.json`、`native-boundary-report.json`、四份 `restart-02-v2-*-report.json` 与 `managed-restart-02-v2.json`。这些文件不随产品包分发。已筛选的合成截图包括 `docs/screenshots/desktop-memory-0.2.0.png` 和 `mobile-memory-0.2.0.png`。验收专用连接/助手已停用、会话已归档、记忆及sentinel已清理；既有演示助手保留可用。

0.1.0的历史验收见 [历史记录](verification/DELIVERY_0_1_0.md)，它的三份已发布归档保持原摘要。历史记录不替代本轮新增功能的验收。

## 发布与证据边界

client包含源代码、锁定依赖、已构建静态页面及独立Web服务配置；server包含Python服务、固定依赖、测试与合成MCP；部署资料包包含组合配置、使用/接口/运维/验收文档与明确列入的脚本。正式0.2.0输出至 `dist/releases/0.2.0/`，归档大小、SHA-256和完整清单以随包 `manifest.json` 为准。

打包器检查同版本FINAL记录，拒绝覆盖已发布目录。收录使用白名单，排除运行数据、私有配置、凭据、内部会议材料和日志，执行明显密钥标记检查、归档读回与源码逐字节对应检查。Docker构建文件已提供，本次没有执行容器部署验证。

尚未覆盖真实客户MCP业务数据、原方案基线、压力容量、自动压缩阈值压力触发、长期记忆质量、图片理解、个人OAuth、本机MCP、SSO和多组织共享实例。当前为单组织、单worker的可验收试点；合成任务通过不等于客户业务正确率或性能收益已验证。
