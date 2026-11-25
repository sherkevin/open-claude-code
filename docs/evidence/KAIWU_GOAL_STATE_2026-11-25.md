# 2026-11-25｜官方 Goal 状态服务产品接入

基线 a81469e，独立工作树 kaiwu-goal-pilot-20261125。实际复用固定 fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 的 dsh-goal 原生状态服务和 session projection / JSONL persistence；不是自研 Goal 状态机。不安装第二个调度器。默认关闭工作台/API 功能（AGENT_HARNESS_GOAL_STATE_ENABLED=false），已在锁定 native profile 挂载仅状态插件，不挂载 goal-round-driver、tool-goal、command-goal 或额外模型工具。

- 主会话完成首次 native 对话之后，当前所有者可在空闲且未归档的 Harness 会话通过工作台/API 创建、查看、修改、暂停、恢复状态、完成、清除目标。目标内容、轮次上限（1–32）、原生 id/revision CAS 严格验证。非本人（含管理员）不可访问；trial 不启用。
- 使用原 SessionActor 串行维护保留、当前账号/会话/助手/native session 复核；维护期间阻止新模型运行及冲突的记忆更新、归档、上下文和分支操作。原生状态是唯一权威，不把副本写到 SQLite。变更后 disarm、官方 sessions.flush，关闭独占进程，返回经过 schema 校验的回执；不对状态不确定的写入自动重试。API 不触发业务工具或模型调用。
- 创建与 resume 只记录原生状态，不代表已授权无人值守续行。页面明确显示试点边界。跨应用重启状态保存，恢复后仍为 disarmed；轮次限制不等于 token、费用或时间预算。
- 尚未将 driver 接入原每轮独占进程、SQLite run/FIFO、审批和使用量生命周期。没有持续自动执行，没有正式启用。记忆改动造成 native epoch 替换、跨会话 fork 继承及 driver 启用后的预算/失败恢复仍需验收；不得声称 Goal 已全部完成。

## 实际验证

证据 /media/data3/dengkw/acceptance/kaiwu-goal-pilot-20261125/。使用隔离数据库、固定版实际 Node/Harness、回环合成模型及 Chromium。

| 项目 | 结果 |
|---|---|
| 原生 Goal 状态/输入及 profile 回归 | 69/69 通过（pytest-goal-2.log） |
| 前端 Goal、调度、反馈相关组件 | 24/24 通过（vitest-goal.log） |
| TypeScript/Vite 生产路径构建 | 通过（build-goal.log） |
| 真实浏览器及真实 native Goal | 1 个 E2E 用例含 5 项检查全部通过（browser-goal.log） |
| 扩展后端 Goal、context、fork、schedule、memory 回归 | 首轮 85 通过、16 个记忆夹具 setup error（regression-goal.log） |
| 原主线复现 memory fixture 缺凭据并补合成回环 fixture | 16/16 通过（baseline-memory.log、memory-regression.log） |

首轮 Goal 用例出现 domain error 后过早释放 HTTP 回执、下一条请求读到维护忙的竞态，修复为进程关闭及 reservation 释放后才结算 waiter，随后 69/69 通过。扩展组合错误均为旧记忆夹具未明确模拟模型凭据，未改生产鉴权；设置合成回环配置后单独复验 16/16，未声称整个扩展组合一次性重跑。真实浏览器验证创建、重新打开、暂停/恢复、完成/清除、无额外模型消息；服务端确认仅首次正常对话有模型调用。

## 真实工程师执行

普通 CLI /home/dengkw/.local/bin/claude 返回 not logged in；既有 /media/data3/dengkw/.private-dev/bin/claude 的普通配置健康探测返回 KAIWU_WORKER_READY，退出码 0（claude-configured-connectivity.json）。额外 restricted 模式忽略配置而未登录，未重配凭据。已用正常配置和文件工具白名单启动独立 Mem0 工程师（PID 3284543，session 883778cb-25d1-4a97-a4f2-c1823979e04d），其任务位于独立 worktree kaiwu-mem0-index-adapter-20261125/ENGINEERING_TASK.md，证据位于 /media/data3/dengkw/acceptance/kaiwu-mem0-index-adapter-20261125/。模型已响应并调用 Read/Glob/Grep/Write，实际创建 server/app/mem0_index.py；仍需独立审查及测试，不标记完成。后续自动推进监控其真实进程、活动事件、文件、退出和验收，不等待空闲工程师或重复派工。

下一项：官方 driver 与现有宿主实际 run/FIFO/usage/审批边界集成；同时继续 Mem0 及 Team 独立工作。生产 A2 发布闸门未解除。
