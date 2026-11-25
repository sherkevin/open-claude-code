# 2026-11-26｜官方 Team 只读任务板接线验收

基线 2edcf89，独立工作树 kaiwu-team-state-20261126。复用固定官方 fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 的 dsh-agent-team、dsh-subagent、真实 AgentLoop、session projection 和 JSONL 持久层；不重写任务板、CAS 或依赖图。官方源码 HEAD 与 tracked diff 已核对。复用官方测试夹具搭建独立合成上下文，不使用任何客户内容、真实模型、外部 MCP、生产会话或外部遥测。

server/harness/team_state.ts 是尚未挂载的宿主只读接口，限定初始化完成、固定源版本、确切的配置 sessionId、真实空闲根 Agent；仅接受 get。官方 agentTeam 投影有错误或超出 256 项 / 1 MiB 响应边界时拒绝返回，不用部分过期任务伪装成功。返回分离的投影副本，不返回原始事件日志、不改变 Team mode、不会唤醒 Agent。executionEnabled=false 表示此接线不授权执行，并非替代未来真实 Agent 状态。

## 实际验收

Node v24.9.0、固定源码的 tsx resolver、清空继承环境，独立 data3 HOME 和临时目录。证据 /media/data3/dengkw/acceptance/kaiwu-team-state-20261126/probe-2.log。首轮 6/6，补充两项边界后完整复验 8/8，退出码 0。

| 检查 | 结果 |
|---|---|
| 根会话默认关闭 Team | 通过 |
| 未初始化、会话不匹配、非法操作及错误源码版本拒绝 | 通过 |
| 原生任务 CAS、依赖完成前后 readiness | 通过 |
| 不同根会话的任务板隔离 | 通过 |
| 官方 idle resident 测试夹具中的子 Agent 不能冒充根 | 通过 |
| 非法持久事件导致投影异常，拒绝返回旧任务 | 通过 |
| 分离快照、无日志追加、零模型请求 | 通过 |
| 关闭 Team 后从实际 JSONL 重建上下文，完整恢复任务、零模型请求 | 通过 |

## 下一步产品接入

尚未挂载到锁定 profile、Python RPC 或工作台，不是完成的产品功能。后续首先复用 Goal 的本人归属/非 trial/空闲 actor reservation 与独立 native 进程，限定无模型的根会话只读接口；在真实 Harness + 隔离 API + 浏览器中验证跨用户、归档、native session epoch、错误回执、重启；然后才逐步引入 Team 模式变更及写任务审批，实际跨轮子 Agent 恢复、通信、任务归属和企业角色授权仍需独立验收。原 SubagentOrchestrator 保持不变。官方同一 checkout、跨进程不协调、writeScope 警告非锁、不自动释放所有者的限制仍成立。不得挂载 native shell/tool 或默认开启 Team。生产 A2.10 闸门未解除。
