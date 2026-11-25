# 2026-11-25｜B1 固定官方 Goal 基线

基线 0eaa5ae，独立工作树 kaiwu-goal-pilot-20261125；无同模块活跃工程师。固定源码 fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159，Node v24.9.0，沿用既有依赖。源码 tracked diff 为空。测试不继承模型凭据，HOME/TMPDIR/XDG_CACHE_HOME 位于 data3，未使用客户数据或生产调用。

| 官方测试 | 结果 |
|---|---|
| Goal lifecycle | 36/36 |
| Goal projection | 8/8 |
| Goal round driver | 62/62 |
| Goal tools | 30/30 |
| Goal command | 13/13 |
| 总计 | 149/149 |

Vitest 4.1.11，退出码 0，证据 /media/data3/dengkw/acceptance/kaiwu-goal-upstream-20261125/upstream.log。仅官方组件基线通过，尚非开物产品集成验收。

## 接入决策与下一断点

官方 Goal 状态由会话事件投影恢复，activation 是进程内状态而非持久授权；恢复或 fork 后不会自动续行，需用户显式 resume。官方 driver 用 revision/round fencing 及 checkpoint flush 控制轮次，maxGoalRounds 只是轮次上限，不等同 token、金额、时间预算。官方工具有独立运行时权限检查。继续复用上述组件，不另造循环调度器。

下一条具体任务：在本独立工作树先检查 server/harness/profile.py、server/app/harness_runtime.py、server/app/harness_bridge.py 的请求生命周期和审批边界；通过显式隔离 profile 挂载官方 Goal 状态服务，初始不挂载自动 round driver、不新增模型权限。使用回环合成模型和独立会话，验证持久 create/get、restart disarmed、revision 拒绝及显式用户恢复授权；随后才接入 driver，补宿主预算、取消、持久化失败和禁止重放的集成验收。生产 A2.10 闸门未解除，不执行任何生产切换。
