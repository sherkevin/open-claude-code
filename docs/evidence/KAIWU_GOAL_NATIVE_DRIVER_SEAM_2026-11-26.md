# 2026-11-26｜官方 Goal round driver 单轮宿主许可隔离验收

基线 2edcf89，独立工作树 kaiwu-goal-native-driver-seam-20261126。Claude Code 工人实际交付 probe、pytest 入口与候选记录，退出码 0；总控独立修正并验证。固定官方 fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 源码、Node v24.9.0、官方 tsx resolver。实际加载官方 dsh-goal-round-driver、dsh-goal、AgentLoop、session projection 与 testkit；合成模型及合成宿主租约仅用于隔离实验。清空继承环境、全部测试文件在 data3，不使用客户数据、真实模型或业务工具。

## 真实验收

证据 /media/data3/dengkw/acceptance/kaiwu-goal-native-driver-seam-20261126/：controller-direct.log、controller-pytest-4.log 及 controller-direct/ 下合成 checkpoint。直接运行 5/5 场景，pytest 封装 1/1 通过，均退出码 0。

| 场景 | 合成模型请求 | 原生正轮次历史 |
|---|---:|---|
| 无宿主授权拒绝 | 0 | [] |
| 一次授权仅允许一轮，下一轮拒绝 | 1 | [1] |
| 旧 revision 拒绝，新 revision 明确授权后运行 | 1 | [1] |
| 上轮权威费用未知，拒绝自动第二轮 | 1 | [1] |
| 卸载/重挂 driver 与 session-start 边沿，无自动重放，明确重新授权后一轮 | 2 | [1,2] |

真实 paid model 请求 0，结算费用为 unknown/null。第 5 场景验证重挂后第三轮也被宿主下游 veto 拒绝；这里是同进程 driver 重建和 session-start 边沿，不是跨进程生产重启。官方真实 JSONL 跨重启状态行为另有 B1/B2.1 验收，本切片不重复证明。

首轮失败为包路径错误（Cordis 实际 vendor/cordis，testkit 实际 packages/test-support），总控按固定源码改为唯一明确源路径，删除候选允许跳过 pin 的选项，并要求工作目录位于 data3。第二轮 4/5，重挂 driver 后原 pre-step hook 注册顺序不正确，实际 goal 进入 paused 而非预期 prompt-rejected。总控先卸载旧 fence，重挂 disarmed driver 后重新注册下游 fence，完整直接及 pytest 复验通过。未放宽任何产品权限，未修改上游源码。

## 产品接线合同

官方 driver 先向 inbox 预留正轮次，然后在 agent/pre-step 对精确 goalId/revision/round 进行检查并调用下游 next；宿主必须位于该 middleware 下游才能在原生正轮次进入历史和模型请求之前拒绝，且 driver 能观察 veto。正轮次计数来自原生 user/message，不从预留推断。resume 增 revision；activation 为进程内状态，不能代表权限。driver 每轮结算后在下一轮前 flush；hook 注册顺序及撤销/重启失效是必要条件。

下一步 B2.4 将实际企业 SQLite run、lease、权限、FIFO 与原生 driver 边沿进行原子对齐；仍需真实进程重启、审批、取消、跨进程防重与用量验收。当前 probe 的合成 checkpoint 不是产品事务、不是权威真实费用或跨进程持久执行；测试内明确允许第一合成轮仅为验证 seam，不能成为实际执行授权。权威费用/单轮硬预算缺失时实际无人值守执行必须保持 fail-closed，不以零或估算冒充结算。实际生产 profile 没有挂载 driver，默认关闭不变。生产 A2.10 闸门不变。

运行入口 scripts/oss/harness_goal_native_driver_seam_probe.mts（--source、--work-dir，固定 pin 无跳过）与 server/tests/test_goal_native_driver_seam.py（显式 HARNESS_TEST_NODE、HARNESS_TEST_SOURCE、data3 basetemp）。工人参考目录及派工文件不提交。
