# 2026-11-26｜B2.4 Goal 明确授权后的端到端单轮持续执行（候选，未运行）

基线：工作树 `kaiwu-goal-e2e-20261126`，分支 `feat/kaiwu-goal-e2e-20261126`。固定官方源码 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`（0.2.2-rc.1），Node v24.9.0。

本切片实现的是**产品主路径**：owner 明确授权后，由既有 `Service.prepare_run`/`RunManager`/FIFO 为一个真实宿主 run 预留一次官方 Goal 正轮次；该 run 内由固定官方 `dsh-goal-round-driver` 通过官方 `ctx.goals.pause/resume` 精确 arm 宿主预留的 revision，再在 `agent/pre-step` 下游调用宿主 `workspace/goal-admission`，通过后才允许目标消息进入原生历史并发起模型请求。每轮结束后只从真实 `runs.usage` 结算；缺失或非权威费用记为 `unknown` 并阻断下一轮。停止、失败、取消、重启都撤销授权/中止租约，绝不重放。

**重要状态：本轮工程师只有受控文件工具，没有 shell。所有测试（Python、Node、客户端）均未执行；下方“预期验收”是待总控复验的断言，不是已通过结果。未部署、未开启生产、未修改配置/部署/Git。**

## 1. 实际写入的内容

### 1.1 数据模型与迁移

`server/app/db.py`

- `goal_execution_authorizations.objective_sha256`：授权绑定目标正文摘要，不落目标正文。旧授权该列为 `NULL`，启动路径按 fail-closed 处理（不能证明批准过哪段目标文本）。
- `goal_execution_leases` 新增：`expected_goal_revision`、`armed_goal_revision`、`native_rounds_before`、`arm_action`、`armed_at`、`usage_source`、`cost_authoritative`。
- 迁移只在列**首次新增**时把既有租约回填为已 arm（`armed_at=admitted_at`、`armed_goal_revision=goal_revision`），不会把 B2.4 新写入的“预留但未接纳”租约误标成已执行。
- 保留唯一索引 `goal_execution_leases(conversation_id) WHERE state='admitted'` 与 `UNIQUE(authorization_id,round_index)`。

### 1.2 宿主执行状态机

`server/app/goal_execution.py`

| 函数 | 作用 | 是否启动执行 |
|---|---|---|
| `authorize_goal_execution` | 记录 owner 对 goal id/revision/objective digest/session/assistant revision 的批准，以及轮次、token、微美元费用、秒四个独立预算；允许 `active` 或 `blocked`（可恢复）原生目标 | 否 |
| `start_goal_round` | 校验授权与原生目标，按官方 revision 边沿计算 `pause_resume`（active，+2）或 `resume`（paused/blocked，+1），经正常 `Service.prepare_run(goal_continuation=True)` 创建真实 run，再写“预留未接纳”租约；不启动模型、不改原生 Goal | 否（只 enqueue） |
| `admit_goal_round_armed` | Node seam 在官方 pre-step 内调用；校验租约、授权、会话、权限、助手 revision、目标 id/精确 expected revision、原生轮次基线、预算、时间、活动 run，才把租约从预留变为已接纳 | 否 |
| `require_goal_round_start` | 通用 actor 在打开 Harness 进程前再次 fail-closed 检查；发现已 arm、无租约、撤销或预算不足直接拒绝 | 否 |
| `goal_execution_runtime_config` | 只把 goalId、预 arm/expected revision、arm action、roundIndex、原生轮次基线、原生上限、目标摘要、lease/run/session id 冻结进 Node profile；不传目标正文/模型输出/凭据/业务数据 | 否 |
| `_settle_goal_round_locked` / `finish_goal_round_locked` | run 终态时从真实 `runs.usage` 结算；成功轮次记录 complete/unknown；失败/取消/中断/未接纳轮次撤销授权并中止租约；未接纳轮次不伪造零费用结算 | 否 |
| `stop_goal_execution` / `stop_goal_execution_locked` | 撤销批准并中止活动租约；不取消正在运行的 run | 否 |
| `recover_goal_execution` | 启动时撤销所有活动批准并中止租约，原因 `service_restarted` | 否 |

关键实现细节：

- `_totals` 区分 `rounds_used`（含未接纳预留）与 `rounds_executed`（已 arm，可能消耗过资源）；只有后者参与 token/费用/完整性合计。
- `_run_usage_detail` 只在以下条件同时成立时记 `complete`：`capture_complete`（顶层或 `token_accounting`）为真、token 可计算、费用为**非估算**数值。`estimated_cost_usd` 即使有值也不算权威费用；`cost_micros=None` 恒为 `unknown`。
- 合成来源（`synthetic_fixture`/`loopback_test`/`host_synthetic`）只在 `provider_test_mode=true` 下可满足合成预算，且 `cost_authoritative` 恒为 `false`；生产路径不会被误当权威计费。
- `start_goal_round` 的安全检查在创建 run 前和写租约前各做一次；第二次授权/目标/轮次变化会使 run 被显式失败（“未执行模型”）而不是留下可执行任务。
- 一个宿主 run 最多一个官方正轮次；下一轮必须由 owner 再次显式点击“开始一轮”，在同一授权和剩余预算内创建新的真实 run。

### 1.3 Node 侧官方 driver seam

`server/harness/profile.py`

- 有 `goal_execution` 时，把固定模块 `packages/goal/goal-round-driver/src/index.ts` 插在 `goals` 之后、`sdk-app-startup` 之前挂载；普通 profile 不挂 driver。
- 校验 11 个 Python 字段与 revision 边沿（pause_resume=+2，resume=+1），只把 10 个 camelCase 字段写入 `workspace-host.config.goalRound`（不含 session id，不含目标正文）。
- 模块文件不存在时在写 profile 前拒绝。

`server/harness/workspace_plugin.ts`

- `parseGoalRound` 严格校验 camelCase 字段、id/digest/revision 边沿、轮次上限。
- `workspace/goal-round` RPC：等待 agent idle，校验目标 id/预 arm revision/原生轮次/上限/`activation='disarmed'`/目标摘要；`pause_resume` 先 `pause`（rev+1）再 `resume`，`resume` 直接 `resume`；返回 `workspace-goal-round/v1` 回执（含 expected revision、roundsStarted、round、armed）。
- 第二个 `agent/pre-step`（注册在官方 driver 下游）：只接受 goal-sourced 消息；同一 run 只接纳一轮；无配置直接 veto；未 arm 报 `GOAL_ROUND_NOT_ARMED`；goalId/revision/round 不符报 `GOAL_ROUND_FENCE_MISMATCH`；通过后才发 `workspace/goal-admission` RPC；`next()` 失败报 `GOAL_STEP_ENTER_FAILED`，不会让宿主无限等待。
- `goalSource` 在 pre-step 批次中优先选择“原生下一正轮次”，避免同一原生会话恢复后历史中的旧 goal 消息抢占；找不到匹配时仍取最后一条 goal 消息并交给栅栏拒绝，保持 fail-closed。
- `turn/end` 观察者轮询官方 `ctx.goals.get`，确认 `roundsStarted` 后才发 `finished`；宿主再复核 round/revision/phase 才让 run 成功。
- goal 模式期间关闭 `workspace/goal-state`、`session/prompt`、`workspace/steer`、`workspace/compact`，避免第二调度器或旁路 prompt。

`server/app/harness_runtime.py`

- 新增 `workspace/goal-admission` 处理：只接受当前 run/session/lease 的严格请求，转调 `admit_goal_round_armed`，返回 `{admitted:true, leaseId, round}` 或固定错误码。
- `_execute_owned` 对 `goal_continuation=true` 的 run 走 `_execute_goal_round`：调用 `workspace/goal-round` arm 精确 revision，验证回执，等待 `workspace.goal_round` 的 admitted/rejected/failed/finished，转发 `workspace.stream` 的 `text_delta` 为可见 `message.delta`，验证原生 `roundsStarted` 后结束。
- 真实用量仍由既有 `HarnessUsageLedger` 在 `_run_with_usage` 中快照；本切片不伪造费用。

### 1.4 公共接线（最小改动）

- `server/app/service.py::prepare_run` 增加仅宿主可用的 `goal_continuation` 参数；开关关闭时该路径 404/拒绝。`_prepare_run` 对普通用户消息仍撤销旧授权（`user_instruction`）。
- `server/app/service.py::runtime_config` 仅当存在活动 run 时计算并冻结 `goal_execution`，并加入运行时 fingerprint；run 结束后重新计算为 `None`，下一个 Goal 状态读取会重建无 driver 的运行时。
- `server/app/runtime.py`：actor 在打开 Harness 前执行 `require_goal_round_start`；`_finish` 在同一终态事务内调用 `finish_goal_round_locked`；新增 `submit_goal_round`（提交前再检查一次，失败取消 run 并返回 false）；`recover` 继续以 `approval_source='restart'` 区分重启原因。
- `server/app/conversation_goal_execution.py`：`POST /goal/execution/rounds` 只接受 `{goal_id, goal_revision, confirm:true}`，先经同一原生 actor 读取当前 goal，再 `start_goal_round` + `submit_goal_round`；幂等重放返回 `replayed:true, submitted:true`，不会创建第二个 run。
- 默认关闭：`AGENT_HARNESS_GOAL_EXECUTION_ENABLED=false`，且要求非 trial；关闭时路由不挂载、UI 不显示执行面板。

### 1.5 产品可见性

`client/src/ConversationGoal.tsx` / `client/src/ChatPage.tsx`

- `config.harness_goal_execution===true` 时才显示执行面板。
- 表单分别收集授权轮次上限、token、微美元费用、秒预算，`confirm:true`；开始一轮只发送当前 goal id/revision，不发送预算或目标正文。
- 展示：授权状态、轮次已用/已执行/剩余、token/费用/时间、未知用量告警、合成计费提示（明确“不是生产权威计费”）、权威回执提示（仅在有已执行轮次时）、最近轮次、下一原生轮次。
- 停止按钮在运行中仍可用；开始按钮只在 `can_start` 时可用；每轮开始前重新读取原生 revision 防陈旧确认。

## 2. 关键不变量

1. **一个宿主 run = 一个官方正轮次**：租约在 run 预留时写入，`armed_at IS NULL`；只有 Node 在官方 pre-step 内通过 `workspace/goal-admission` 才置 `armed_at`。同一 run 第二次准入返回 `GOAL_ROUND_ALREADY_ARMED`，通用 actor 也不会执行。
2. **revision 精确绑定**：授权绑定 objective SHA-256；宿主预留 `pre_arm_revision` 与 `expected_goal_revision`；Node arm 前校验原生 revision/rounds/activation/digest，Python 准入再校验 expected revision 与原生轮次基线 `native_rounds_before`。任何 pause/resume/edit/会话替换都会在模型请求前失败。
3. **轮次链连续**：`roundsStarted == 授权原生基线 + 已 arm 租约数`；落后/超前都拒绝，不猜测 checkpoint。
4. **预算独立**：轮次、token、费用、时间四个上限独立存在；轮次数不能替代费用；预算是轮次间栅栏，不是运行中硬中断。
5. **未知不当零**：缺失/不完整/估算/非权威费用一律 `unknown`，下一轮 `GOAL_USAGE_UNVERIFIED`；真实 Harness 当前没有权威费用，因此真实轮次按设计只能跑一轮。
6. **停止/失败/重启不重放**：停止撤销授权并中止租约；run 失败/取消/中断在同一终态事务内撤销；重启把非终态 run 标为 interrupted 并以 `service_restarted` 撤销；旧租约永远不能再次准入，必须重新授权。
7. **合成≠生产**：合成回执只在 `provider_test_mode` 下满足合成预算，永远 `cost_authoritative=false`；UI 分开显示。
8. **不另造循环/调度器**：执行仍走 `Service.prepare_run`、RunManager/FIFO、既有审批与 usage；Goal 原生日志仍是唯一 Goal 状态事实源。

## 3. 测试清单（已写入，**全部未执行**）

### 3.1 新增 `server/tests/test_goal_continuation.py`

合成 Node seam 只替代“arm+usage 回执”的 Node 边界，仍走真实 FastAPI/SQLite/`prepare_run`/RunManager/lease；不替代真实模型。

| 验收要求 | 测试 |
|---|---|
| 默认关闭无接口、无 run、无租约 | `test_goal_round_surface_is_default_off` |
| 同一授权两轮显式启动、每轮真实 run/租约、结果可见、合成用量结算 | `test_two_explicit_rounds_are_visible_and_settled_as_synthetic` |
| blocked 目标用 `resume` 单 revision 边沿恢复并执行 | `test_blocked_goal_resumes_with_one_revision_edge` |
| 轮次预算阻止下一轮 | `test_round_count_budget_blocks_the_next_explicit_round` |
| token 与费用预算分别阻止下一轮；撤销后重新授权不重放 | `test_token_and_cost_budgets_block_the_next_explicit_round` |
| 真实 Harness 用量形态（capture 完整、费用缺失）→ unknown → 第二轮拒绝 | `test_real_harness_usage_gap_fails_closed_after_one_round` |
| 运行中停止：不取消当前 run、不再开始下一轮、仍结算已发生用量 | `test_stop_while_round_runs_prevents_replay_but_settles_usage` |
| arm 失败：授权撤销、租约中止、不执行模型、不重放 | `test_arm_failure_revokes_and_never_replays` |
| 重启恢复：预留未 arm 的 run 中断、授权撤销、无重放 | `test_restart_aborts_a_reserved_round_without_replay` |
| stale revision、paused 目标拒绝启动 | `test_start_rejects_stale_goal_and_unknown_usage` |
| 其他用户不能启动或停止 | `test_other_user_cannot_start_or_stop_round` |
| 视图含 owner 专用 session marker、round/expected/armed revision、可见消息 | `test_execution_view_requires_owner_and_reports_rounds` |
| **真实固定 Node + 官方 driver + 回环合成模型**：一轮真实 run、恰好一次模型请求、原生 roundsStarted=1、disarmed、usage unknown、第二轮 409 且无第二次请求 | `test_real_harness_goal_round_end_to_end`（无 `HARNESS_TEST_NODE`/`HARNESS_TEST_SOURCE` 时 skip） |

### 3.2 修改 `server/tests/test_goal_execution.py`

- `test_owner_authorizes_and_other_user_is_denied`：断言 `driver_integrated=true`、`single_round_per_run=true`；授权本身仍不产生 run/审批/模型。
- `test_generic_actor_cannot_execute_an_already_armed_continuation`（原 `test_generic_actor_cannot_execute_unintegrated_driver`）：已 arm 的续行 run 在通用 actor 打开 Harness 前被拒，`GOAL_ROUND_ALREADY_ARMED`，不连接运行时，租约中止。
- 既有 27 项继续覆盖授权/预算/结算/停止/恢复；取消与重启测试现在也经过新的终态结算钩子。

### 3.3 修改 `server/tests/test_harness_profile.py`

新增 profile 测试：

- `test_goal_round_profile_mounts_the_official_driver_before_startup`：driver 只挂一次、在 `goals` 后/`sdk-app-startup` 前、路径为固定官方模块、`goalRound` 精确 10 字段、目标正文不进入 profile。
- `test_goal_round_profile_is_absent_in_an_ordinary_run`
- `test_goal_round_resume_action_uses_one_revision_edge`
- `test_goal_round_profile_rejects_an_inconsistent_reservation`：14 个参数化拒绝用例（revision 边沿、arm action、digest、goal id、round index、原生基线、bool 上限、id 字符、session、额外 objective 字段）。
- `test_goal_round_profile_requires_the_pinned_driver_module`

新增 plugin 测试（真实 Node 执行插件源码 + 内存 transport，不启动 Harness）：

- `test_plugin_arms_only_the_reserved_native_revision`：pause+resume 各一次、回执 revision 精确、第二次 arm 拒绝。
- `test_plugin_goal_pre_step_rejects_an_unarmed_goal_message`：未 arm 的 goal 消息 veto、不发准入 RPC。
- `test_plugin_goal_pre_step_requires_host_admission_and_one_round`：准入 RPC 字段精确、`enter` 后通知 admitted、同 run 第二轮无第二次 RPC 直接 veto。

### 3.4 修改 `server/tests/test_harness_goal_state.py`

- `test_real_native_goal_authorization_does_not_run_driver`：断言 `driver_integrated=true`/`single_round_per_run=true`；授权和暂停不产生额外模型请求，原生 goal 仍 disarmed。

### 3.5 客户端

`client/src/ConversationGoal.test.tsx`：

- 精确授权/单轮 payload、非法预算/无 session 抛错。
- 静态渲染合成计费与未知用量告警，不声称权威计费。

## 4. 总控复验命令与预期验收

以下命令**本轮未执行**。请使用本仓库既有 FastAPI/pytest 虚拟环境，并把临时目录放在 data3（根分区空间问题由你按既有规范处理）：

```bash
cd /media/data3/dengkw/projects/kaiwu-goal-e2e-20261126/server
TMPDIR=/media/data3/dengkw/tmp-kaiwu-goal-e2e \
python -m pytest tests/test_goal_continuation.py -q
```

预期（无 pinned Node 环境变量）：**12 passed, 1 skipped**；真实 e2e 明确 skip，不得把 skip 当通过。

```bash
cd /media/data3/dengkw/projects/kaiwu-goal-e2e-20261126/server
TMPDIR=/media/data3/dengkw/tmp-kaiwu-goal-e2e \
HARNESS_TEST_NODE=/media/data3/dengkw/projects/open-claude-code/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node \
HARNESS_TEST_SOURCE=/media/data3/dengkw/projects/open-claude-code/.runtime/third_party/deepseek-harness \
python -m pytest tests/test_goal_continuation.py -q
```

预期：**13 passed**。真实 e2e 必须满足：恰好 1 次回环模型请求；run succeeded；原生 `roundsStarted=1`、`activation=disarmed`；assistant 文本非空；租约 `armed=true`、`armed_goal_revision=goal.revision+2`；`usage_status=unknown`、`cost_micros=null`；第二次 `POST /rounds` 为 409 `GOAL_USAGE_UNVERIFIED` 且无第二次模型请求。

```bash
cd /media/data3/dengkw/projects/kaiwu-goal-e2e-20261126/server
TMPDIR=/media/data3/dengkw/tmp-kaiwu-goal-e2e \
HARNESS_TEST_NODE=/media/data3/dengkw/projects/open-claude-code/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node \
HARNESS_TEST_SOURCE=/media/data3/dengkw/projects/open-claude-code/.runtime/third_party/deepseek-harness \
python -m pytest tests/test_goal_execution.py tests/test_harness_goal_state.py tests/test_harness_profile.py -q
```

预期：全部通过；`test_harness_profile.py` 中包含 8 个新测试函数、14 个参数化拒绝项；`test_harness_goal_state.py` 的 2 个真实 Node 用例实际执行（不得 skip）。若 B2.3 的 `test_goal_native_driver_seam.py` 也一起跑，应保持 1 passed。

```bash
cd /media/data3/dengkw/projects/kaiwu-goal-e2e-20261126/client
npm test -- ConversationGoal.test.tsx
npm run build
```

预期：vitest 全通过；`tsc -b && vite build` 成功。

定向失败排查顺序：

1. 若 `test_goal_continuation.py` 在授权 POST 失败：先查 fake runtime 是否设置 `session_id`、`service.memory.initialize` 是否先于写 `harness_session_id`（见 `setup_conversation`）。
2. 若两轮测试在第二轮返回 `GOAL_SESSION_CHANGED` 或 `GOAL_ROUND_FENCE_MISMATCH`：核对 `_start_checks` 的 session 比较与 `armed` 计数。
3. 若真实 e2e 在 `workspace/goal-round` 超时/失败：看 Node 侧是否加载官方 driver、`goalRound` 是否写入 profile、agent 是否 idle；这是最可能暴露未验证真实依赖的地方。
4. 若真实 e2e 的第一轮成功但 `usage_status=complete`：说明固定 ledger 新出现了权威费用字段；核对 `_run_usage_detail` 的 `cost_authoritative` 语义，不得放宽为估算费用。
5. 若 profile 测试在模块路径失败：确认 `packages/goal/goal-round-driver/src/index.ts` 在固定源码中存在。

## 5. 无法在本环境安全完成 / 尚未验证的真实依赖

1. **固定 Node 源码不在本工作树，且无 shell**：`test_real_harness_goal_round_end_to_end` 及三个 plugin 测试未执行。官方 driver 在 `ctx.goals.resume` 后是否异步开始回合、`agent/pre-step` 批次中的消息集合、`turn/end` 的 reason 形态、`ctx.goals.get` 投影时序，都只由 B2.3 独立 probe 证据支持，本切片未在真实进程复验。
2. **多步/工具调用回合未验收**：当前 fence 假设 `agent/pre-step` 的 `messages` 是该步新提议的消息（B2.3 probe 的同一假设）。如果固定 driver 在工具续步时重复给出已提交的 goal 消息，fence 会把它当作“同一 run 的第二轮”而 veto，导致工具续步失败；真实 e2e 的回环模型只产生单步文本，不能排除该风险。总控必须补一个真实工具续步的隔离用例后再扩大适用范围。
3. **真实权威费用缺失**：固定 `HarnessUsageLedger.receipt()` 的 `estimated_cost_usd=None`。真实 Harness 轮次必定 `usage_status=unknown`，因此**真实两轮不在本切片可验收范围内**；两轮验收只由合成回执覆盖。不得把合成或估算费用写成生产计费。
4. **单轮硬预算未实现**：token/费用/时间是轮次间栅栏；单轮运行中可能超支，当前不会实时杀模型进程。需要权威费用/预留/中断策略后才能谈无人值守或硬上限。
5. **真实进程重启与取消**：测试用 `RunManager.recover()`/`manager.finish` 模拟启动恢复与终态钩子，没有 OS 级 kill -9/重启；跨进程 checkpoint 重放仍需固定 driver 的真实恢复验收。
6. **Goal 轮次中的业务工具审批**：执行复用既有 bridge/审批，但本切片未用真实工具做端到端审批验收；不得据此声称审批链已在 Goal 轮次内验证。
7. **无人值守**：本切片没有自动续跑；`driver_integrated=true` 只表示官方 driver 已挂载并受宿主单轮准入控制，`unattended_execution=false`、`continuation_driver=false` 不变。
8. **浏览器入口**：客户端测试是静态渲染/纯函数测试；`test_goal_state_browser.py` 未扩展执行面板，执行面板的真实浏览器构建未验收。

## 6. 与其他工作线的并发接线冲突点

- `server/app/service.py`：`prepare_run(..., goal_continuation=...)` 与 `runtime_config` 的 `goal_execution` fingerprint。任何同时修改 run 创建、消息快照、memory 或 fingerprint 的工作线需要合并顺序。
- `server/app/runtime.py`：actor `_execute` 的 pre-runtime 守卫、`_finish` 终态钩子、`submit_goal_round`、`recover` 原因。任何取消/重启/审批/run 生命周期改动会冲突。
- `server/app/harness_runtime.py`：`workspace/goal-admission` 请求分支、`_execute_owned` 的 goal 分支、`write_profile(goal_execution=...)`。任何 chat 通知循环/用量/工具桥改动需复核。
- `server/harness/profile.py` / `workspace_plugin.ts`：KERNEL_MODULES 插入点、两个 `agent/pre-step` 顺序、goal 模式下的 RPC 关闭。其他 Harness 插件/审批/steering 工作线若调整 hook 顺序，会直接破坏单轮 fence。
- `server/app/db.py`：新增列与一次性回填；与任何重写 `_migrate` 或租约表的改动冲突。
- `client/src/ChatPage.tsx`：传入 `executionEnabled`；与 Goal 对话框/配置视图的其他 UI 工作线冲突。
- 测试文件：`test_goal_execution.py`、`test_harness_goal_state.py`、`test_harness_profile.py` 的断言已被本切片调整；其他工作线若同时改这些文件需以本切片的失败关闭语义为准重新合并。

## 7. 变更文件

```text
server/app/db.py
server/app/goal_execution.py
server/app/conversation_goal_execution.py
server/app/runtime.py
server/app/service.py
server/app/harness_runtime.py
server/harness/profile.py
server/harness/workspace_plugin.ts
server/tests/test_goal_continuation.py        (new)
server/tests/test_goal_execution.py
server/tests/test_harness_goal_state.py
server/tests/test_harness_profile.py
client/src/ConversationGoal.tsx
client/src/ChatPage.tsx
client/src/ConversationGoal.test.tsx
docs/evidence/KAIWU_GOAL_E2E_2026-11-26.md    (new)
```

未修改 `docs/plans/KAIWU_PARITY_EXECUTION.md`、`docs/architecture/OPEN_SOURCE_STACK.md`、配置、部署或 Git。`ENGINEERING_TASK.md` 为派工输入，不计入本切片产出。

## 8. 结论

本切片把“明确授权 → 真实 run/lease → 官方 driver arm → pre-step 宿主准入 → 可见结果 → 结算 → 停止/失败/重启不重放”的单轮产品路径写进了代码，并有合成与真实 Node 两层测试候选。默认关闭，未开无人值守，未伪造真实费用或验收。**在总控实际运行第 4 节命令、特别是真实固定 Node e2e 之前，不得声称已验证或可上线。** 真实权威费用缺失是下一轮持续执行的产品级阻塞；当前正确行为是 fail-closed。

## 总控首轮复验（2026-11-26 17:18 CST）

初验 118 passed、14 failed，均为无效 Goal 预留先创建私有目录的顺序问题；将完整预留校验移到创建目录前，profile/continuation 专项复验 96/96。前端 4/4，构建成功。Arm 回执缺失或不匹配的错误提示不再断言模型未执行，明确不自动重放。仍需最新 main 合并复验；真实权威费用、硬预算和自动多轮未完成。
