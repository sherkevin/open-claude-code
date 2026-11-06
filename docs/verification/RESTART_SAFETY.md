# 执行中重启验收准备

`scripts/restart_safety_smoke.py` 用于完整 Agent 产品的受控执行中重启验收。2026-11-05在腾讯云合成MCP环境完整执行四组，通过26项检查、8个真实推理任务，所有专用资源已清理。

每个组合需要两轮真实模型调用：`prepare` 一轮，把任务推进到指定状态；操作人员重启隔离 API；`check` 先验证恢复状态，再发送一轮新的纯文本用户消息。两个场景、两种停机模式的完整矩阵共四个组合、八轮调用。可以只运行所需组合，但不能将结果推广到未运行组合。

| 参数 | 准备状态 | 重启后的必要状态 |
| --- | --- | --- |
| `--case pending-write` | `create_ticket` 等待人工审批 | 原审批 `denied` 或 `expired`，提交旧审批的拒绝决定返回 409，合成工单数为零 |
| `--case slow-read` | `slow_lookup(delay_seconds=30)` 已出现执行前 hook，运行未完成 | 原调用没有成功完成或被重新发起 |
| `--mode graceful` | 操作者正常停止 API | 原运行 `cancelled` |
| `--mode crash` | 操作者强制终止 API 后启动 | 原运行 `interrupted` |

脚本不会发送批准写入的决定。旧审批探测只发送 `deny`，即使服务有缺陷，也不会由测试授予写权限。脚本没有 shell 命令、重启或终止任意进程的参数。

在 Linux 执行主机的项目根目录运行，例如：

```bash
.venv/bin/python scripts/restart_safety_smoke.py plan
.venv/bin/python scripts/restart_safety_smoke.py prepare --case pending-write --mode graceful --real-model --password-file .runtime/admin-password
# 操作者正常重启隔离 API，保留原数据与工作区，并更新 .runtime/processes.json 中的 server PID。
.venv/bin/python scripts/restart_safety_smoke.py check --case pending-write --mode graceful --real-model --password-file .runtime/admin-password
.venv/bin/python scripts/restart_safety_smoke.py cleanup --case pending-write --mode graceful --password-file .runtime/admin-password
```

切换 `--case`、`--mode` 运行其他组合；脚本为每个组合生成不同的状态及报告文件。`slow-read` 仅有 30 秒窗口，准备完成后必须立即重启；工具已成功完成的结果不能算作执行中重启通过。状态标记失败后应保留证据并清理，重测使用新的 `--state`、`--report`，不能覆盖旧结果。

默认地址与已有隔离预览一致：API 18080，客户端来源 18081，演示 MCP 8767；通过 `--base-url`、`--origin`、`--mcp-url` 调整。`--server-data-dir` 默认 `.runtime/data`，`--server-process-file` 默认 `.runtime/processes.json`，`--demo-data-dir` 默认 `.runtime/demo-mcp`。操作者必须确保指定的合成工单数据库属于这一个演示 MCP。凭据只从私有密码文件或环境变量读取。

产品数据仅通过正常 API 操作；SQLite 与 Linux 进程记录只读。检查包括：API 进程确已更换、旧 SDK 进程消失、原生会话编号保留、没有自动新增运行／工具调用、历史用户文本和已有助手文本保留、唯一终态事件可通过 SSE 重放。随后新用户消息必须成功且不调用工具，原写入仍然没有工单。失败会取消本次未完成运行；`cleanup` 只归档本次会话并停用本次连接和助手。

证据边界：停机模式由操作者声明，必须另外保留实际正常停机／强制终止记录。`tool.started` 来源于执行前 hook，不能独立证明 MCP 已收到请求。此工具核对的是指定合成 request_key 和应用持久事件；**不能证明任意外部写入具有 exactly-once 保证**，也不覆盖已经批准并提交到外部系统的写入。

无网络准备检查：

```bash
server/.venv/Scripts/python.exe -m unittest discover -s examples -p test_restart_safety_acceptance.py -v
```

上述断言测试验证错误状态、重复调用和已完成工具不会被误判通过，以及旧审批探测不会发送 `allow`；它们不替代真实供应商、MCP 和进程重启验收。

本次systemd正常停机使用 `KillMode=mixed`：先给主API发送TERM，保留其清理SDK会话的机会，超时才终止整个进程组。最初control-group配置曾使任务提前记录为failed，失败报告保留；修复后使用新state完整重跑通过。crash两组使用对本项目server unit发送SIGKILL，由Restart=on-failure自动恢复，并记录了实际MainPID与NRestarts变化。
