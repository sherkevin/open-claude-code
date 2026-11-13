# 飞书受阻目标排队修复证据

## 问题与变更

- 真实消息：华微世纪群 `@dengkw-华微世纪 hello`，2026-11-10 16:37:28 收到。
- 原始任务编号：`feishu-412a65e4aab9a5fbd1cd3110cb0366ce`。该任务已持久化，但原代码把 `blocked` 目标也判定为忙碌。
- `integrations/feishu_project_bot/orca_transport.py` 仅将目标检查的允许状态扩展为 `completed`、`blocked`、`paused`。不修改原生目标状态，不允许活动或未知目标状态，不绕过执行轮次、输入队列、身份、面板和草稿检查。
- 计划见 `docs/plans/2026-11-10-feishu-blocked-goal-dispatch-fix.md`。

## 离线验证

先运行新测试复现失败：受阻和暂停目标不能通过空闲检查；第二次检查前恢复活动的场景提前被旧逻辑挡住。

修复后执行：

```bash
TMPDIR=/media/data3/dengkw/.tmp \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.tmp/open-claude-pycache \
python3 -m unittest discover -s integrations/feishu_project_bot -p 'test_*.py' -v
```

结果：52 项通过，耗时 52.601 秒。新增 4 项测试覆盖：

1. 受阻、暂停和完成目标允许真正空闲的会话接收消息，目标状态保持不变。
2. 活动、未知、空值等状态仍拒绝。
3. 受阻或暂停目标不能绕过正在执行的轮次与未消费输入。
4. 第二次检查之前目标恢复活动时不发送，也不记录为已提交。

其余测试继续验证去重、发送不确定时不重试、当前会话身份、私人输入混入时不向群发送等边界。独立只读安全审查通过，并独立复跑 52 项全部通过。离线日志在 `.runtime/verification/feishu-goal-queue-20261110.log`，没有真实 Orca 输入或飞书发送。

## 获批生效操作

用户明确批准“允许，仅重启飞书入口”，范围仅为 `feishu-huawei-project-bot.service`。没有批准或执行工作台、模型服务、当前会话的重启。

原入口进程为 2600564，自动重启次数为 0。停止前收件箱中只有原始 `hello` 任务，状态 `queued`，发件箱为空。

该用户服务的停止超时为 30 秒，而单个飞书读取超时可达 45 秒。为避免正常停止被期限强制打断，使用 `systemctl --user kill --kill-who=main --signal=SIGTERM feishu-huawei-project-bot.service` 请求程序已有的优雅退出处理器；只观察状态，不强杀、不启动第二个监听者。确认 `inactive/dead`、`Result=success`、`ExecMainStatus=0` 后，再执行 `systemctl --user start feishu-huawei-project-bot.service`。

实际结果：

- 16:53:08 新进程 2639389 启动，`active/running`，自动重启次数 0。
- 16:54:41 核验新进程日志同时出现事件监听 ready 与飞书长连接 connected 标记。
- 原任务编号、消息编号、排队状态均保留；没有手动改队列、复制任务或另发测试消息。
- 传输记录仍为空，符合当前会话还在执行本轮修复时必须排队的限制。

## 尚未证明的结果

本轮正在执行时，不能把排队状态视为新故障，也不能宣称已经自动投递或回复飞书。本轮结束并且输入框为空后，入口会按原重试间隔再次检查；需随后核对同一任务的 `submitted`、匹配轮次结果和 `sent` 收据，才能完成真实闭环验收。

附带只读检查发现 16:54 的工作台 8000/18086 端口请求未能建立连接；本轮没有操作这两个服务，未获批准重启，不将飞书入口恢复误写成工作台服务也健康。该现象需独立排查，不能据此推断由本次入口重启造成。
