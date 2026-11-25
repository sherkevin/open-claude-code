# 2026-11-26｜原生 Team 只读产品 API 接入

基线 f91de0e，独立工作树 kaiwu-team-product-read-20261126。此前两个工人及 Goal seam 工人均已退出 0，主线与远程一致，本轮总控直接实施，未重复派工。B2.4 仍缺权威费用和单轮硬预算，未启用自动 Goal；独立推进 C2.2。

## 已验收后端增量

- AGENT_HARNESS_TEAM_STATE_ENABLED 默认关闭；非 trial 主会话显式配置后，只装配固定上游 fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 的 subagent 服务和 agent-team 服务，不注册 spawn/fork/external provider、Team/Subagent 模型工具、业务执行或额外权限。原有 delegate_tasks 不变。
- workspace/team-state 仅允许绑定的已初始化空闲根会话 get，严格核验源提交、sessionId、board 结构、任务数量与 executionEnabled=false。实际 Python API GET /api/v1/conversations/{conversation_id}/team 仅本人空闲、未归档、已建立 Harness 原生会话、助手可用且记忆不在变更时读取。复用 Goal 的 native-state actor reservation；读取前、中、进程关闭后重核归属与 epoch，保留既有互斥机制；错误不返回伪造空板。
- 真实固定 Node/Harness + 隔离 FastAPI/SQLite + 回环合成模型：先完成一次人工测试对话，验证默认关闭、未就绪拒绝、本人真实只读回执、POST 禁止、非所属用户拒绝、应用重建后读取及归档拒绝；读取和刷新没有额外模型请求或 run。非空板、依赖、CAS 和日志恢复的上游真实测试仍见 C2.1，此次 API 联调使用原生空板，不宣称非空板浏览器已验收。
- 初次真实 API 测试失败，诊断确认 Cordis 要求明确依赖注入（cannot get property "subagents" without inject）。改用仅开启试点时注册的官方 ctx.inject 依赖作用域，不改上游、不绕过权限。修复后首轮 64/64；移除临时合成诊断输出并补 profile 断言后，Team/profile/原 Goal 组合 74/74 通过，退出码 0。包括原 Goal 生命周期与原生执行授权回归。

## 前端独立候选与剩余工作

同一工作树已有 Team 只读弹窗候选，前端 4/4（含既有 Goal 两项）、TypeScript 及 /project/kaiwu/ 生产路径构建通过。真实 Chromium 验收脚本的新增和测试夹具复用请求被工具安全检查阻止，未写入、未运行、不绕过。因此本次仅合入后端 API 里程碑；前端候选单独提交，暂不合入，待正常可用的授权工具完成真实浏览器验收（打开、刷新、重载、零额外模型请求、无写入口）。生产 A2.10 闸门仍关闭。

证据 /media/data3/dengkw/acceptance/kaiwu-team-product-read-20261126/：controller.log（首次失败）、controller-debug.log（合成诊断）、controller-2.log（64/64）、controller-regression.log（74/74）、vitest.log（4/4）、tsc.log、build.log。根分区约113 MiB 可用，data3约82 GiB 可用；所有测试临时目录均放 data3。未读取生产数据库、客户数据或真实凭据，未上线。测试全部结束。
