# 会话连续性与恢复验收

`scripts/lifecycle_smoke.py` 专门补足已有 API／浏览器验收未覆盖的会话生命周期证据。必须在 Linux 执行服务器运行，使用自己的验收助手和会话。它通过正常 HTTP API 操作产品，只读观察 `app.sqlite3` 中该会话的 SDK 编号及运行数量，以及 `/proc` 中服务器、SDK 子进程的 PID 和启动时间；不会修改数据库、读取进程环境变量或重启任何服务。

只有显式传入 `--real-model` 才允许提交模型消息。`plan` 无需凭据，不联网；此文档中的命令尚不代表真实模型验收已经完成。

## 四轮最小验收

从项目根目录执行。默认对应预览服务的 `.runtime/data`、`.runtime/processes.json`、API 18080 和客户端来源 18081；其他部署用 `--server-data-dir`、`--server-process-file`、`--base-url`、`--origin` 明确指定。PID 文件需包含 `{"server": 实际API进程PID}`，脚本会核验进程而非直接信任该编号。

```bash
.venv/bin/python scripts/lifecycle_smoke.py plan
.venv/bin/python scripts/lifecycle_smoke.py begin --real-model --password-file .runtime/admin-password
```

`begin` 创建专用助手与会话并进行两轮短文本推理：第一轮记住随机合成校验码，第二轮不重复该码而要求回忆。两轮之间必须是相同 SDK 会话编号、相同服务器进程及相同 SDK 子进程。脚本禁止此次测试中出现工具调用，并在异常审批出现时拒绝它。

等待服务器实际配置的 `AGENT_IDLE_SECONDS` 加少量回收时间。脚本不修改服务器配置，也不以单纯“等够了时间”作为回收证明。比如隔离预览配置为 15 秒，则第二阶段传入 `--idle-seconds 15`；默认配置为 600 秒。

```bash
.venv/bin/python scripts/lifecycle_smoke.py after-idle --real-model --password-file .runtime/admin-password --idle-seconds 600
```

第二阶段发请求之前，必须观察到服务器进程未变化、旧 SDK 子进程已经回收、历史及运行数量未变化。随后第三轮要求回忆同一校验码，必须启动新的 SDK 子进程并恢复原 SDK 会话编号。

由操作人员重启**隔离的验收服务**，继续使用原数据目录与工作区，并更新其 PID 记录。脚本没有重启命令或控制任意进程的能力。不要为了此验收重启正在被其他人使用的生产服务。

```bash
.venv/bin/python scripts/lifecycle_smoke.py after-restart --real-model --password-file .runtime/admin-password
.venv/bin/python scripts/lifecycle_smoke.py cleanup --password-file .runtime/admin-password
```

第三阶段先检查 API 进程确已改变、旧 SDK 进程没有遗留、没有自动新增运行、原消息哈希未改变，以及重启前已完成运行的 SSE 可以通过 `Last-Event-ID` 重放。第四轮才要求再次回忆校验码，并检查新服务器和新 SDK 进程恢复原会话。`cleanup` 只取消本次未完成运行、归档本次会话和停用本次助手；不删除文件、历史记录或其他业务数据。

四轮真实模型调用会产生用量。任何阶段失败时停止追加模型请求，先保留证据排查；不要反复执行 `begin` 重置结果。需要全新一轮时使用新的 `--state` 和 `--report` 路径。

## 输出与证据边界

私有状态文件默认为 `.runtime/lifecycle-state.json`，仅包含合成校验码、验收资源 ID 和阶段状态，不包含账号口令。报告默认为 `.runtime/lifecycle-report.json`，进一步省略校验码与完整模型回答，仅记录回答哈希、用量、进程身份、SDK 会话编号与逐项结果。仅在所有阶段完成后，报告才标记 `status: passed`；不要把某一阶段通过当成整体验收完成。

本地准备检查：

```bash
server/.venv/Scripts/python.exe -m unittest discover -s examples -p test_lifecycle_acceptance.py -v
```

这些针对验收断言的检查不会调用模型，不替代真实生命周期执行。它们验证“进程 PID 相同但启动时间不同”“会话编号变化”“只有文本回忆而无进程证据”等情况不能被误判为通过。

此最小流程不覆盖以下项目，仍需独立证据：

- 运行中或等待写操作审批时服务重启：旧审批必须不可继续批准，已执行的业务操作不能自动重复；正常优雅停机和突然中断需分别说明。
- 原生上下文压缩后的业务事实保留、项目记忆质量、多会话并发资源上限与长时稳定性。
- 真实客户 MCP 权限、业务正确率和与原生执行基准的成本／完成时间比较。

已有 API 冒烟中的“审批允许／拒绝”“取消运行”和“SSE 重连”不能替代“审批等待期间重启”的证据。现有后端单元测试对未调度任务的启动恢复进行了验证，但它也不能替代供应商执行中的进程中断测试。
