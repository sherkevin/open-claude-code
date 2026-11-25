# 2026-11-25｜B2.2 Goal 执行授权、宿主接纳、预算与租约边界（候选，未运行）

基线 `ee5b491`，独立工作树 `kaiwu-goal-driver-pilot-20261125`。本切片只做官方 driver 接入前的**宿主安全边界**：用户明确批准目标与预算、真实 run 绑定、活动轮次唯一、预算与时间上限、停止/重启不重放。**未挂载官方 `dsh-goal-round-driver`，未开启无人值守自动执行，未触发真实模型。**

> 状态说明：以下代码和合成测试已写入工作树；本轮**没有执行测试**，不把“已写”当作“已通过”。生产、客户数据、发布与权限均未改动。

## 1. 本切片实际写入的内容

### 1.1 独立、默认关闭的执行开关

`server/app/config.py` 新增：

```text
AGENT_HARNESS_GOAL_EXECUTION_ENABLED=false
```

它与 `AGENT_HARNESS_GOAL_STATE_ENABLED` 相互独立。`server/app/main.py` 只有在**两个开关都开启且非 trial**时才挂载执行授权路由；默认关闭时接口不存在（404），也不写任何执行授权/租约记录。

### 1.2 SQLite 宿主合同

`server/app/db.py` 新增：

- `goal_execution_authorizations`：本人对精确 goal id/revision/native session、assistant revision 的显式批准，以及轮次、token、费用（整数微美元）和时间四个独立预算。
- `goal_execution_leases`：一个批准下每次真实 run 的租约；`UNIQUE(authorization_id, round_index)` 与 `UNIQUE(conversation_id) WHERE state='admitted'` 保证同一会话同一时刻只有一个活动轮次。
- 租约只引用既有 `runs.id`，不新建 run、消息、审批或用量记录。
- 若旧候选库已有批准表但缺 `assistant_revision`，启动时补 0；旧批准会因 revision 不匹配而失效，必须重新授权，不会自动沿用。

### 1.3 宿主模块

`server/app/goal_execution.py`：

| 函数 | 作用 | 是否启动执行 |
|---|---|---|
| `authorize_goal_execution` | 校验本人、空闲 Harness 会话、当前原生 goal id/revision/session、权限模式与四个预算，写入批准 | 否 |
| `admit_goal_round` | 把一个**已存在的真实 run**绑定到批准，检查运行态、活动 run、轮次、时间、用量、token/费用和原生轮次栅栏，创建唯一租约 | 否 |
| `require_goal_lease` | 官方 driver 在每次内部续行前必须通过的租约栅栏；停止、重启、权限变化、用户停用、预算过期都会失败 | 否 |
| `settle_goal_round` | 只从 `runs.usage` 读取真实用量并结算；缺失/不完整记为 `unknown`，绝不当零 | 否 |
| `stop_goal_execution` | 撤销批准并中止活动租约；保留已发生的用量结算能力 | 否 |
| `recover_goal_execution` | 启动恢复时撤销所有活动批准、中止所有活动租约，要求用户重新授权 | 否 |

### 1.4 产品接口

`server/app/conversation_goal_execution.py`，只在两个开关都开启时挂载：

- `GET /api/v1/conversations/{id}/goal/execution`：本人查看最新批准、预算消耗、活动租约。
- `POST .../goal/execution`：必须带 `confirm: true`，并经同一原生 Goal actor 读取当前 goal；id/revision 不一致直接拒绝。只写批准，不写 run。
- `DELETE .../goal/execution`：撤销批准并中止活动租约，不取消当前已存在的 run。停止前先校验 owner，且**不要求会话空闲**，因此运行中的轮次也能立即停止；其他用户不能撤销。

响应固定包含 `continuation_driver: false`、`unattended_execution: false`、`driver_integrated: false`，避免把授权误解为已开启自动执行。

### 1.5 停止与失效钩子

- 执行试点开启时，任何原生 Goal 状态变更（create/edit/pause/resume/complete/clear）在变更前撤销执行批准，并先校验 owner，防止他人跨会话撤销：`server/app/conversation_goal.py`。
- 新的普通用户消息创建 run 前撤销批准并中止租约：`server/app/service.py::_prepare_run`。公开消息体不能传 `goal_continuation`；该参数只供宿主内部续行路径使用。
- 会话审批方式变化、归档时撤销：`server/app/service.py::patch_conversation`。
- owner 管理员角色被降级、账号被停用时，`patch_user` 会持久撤销该用户的目标执行批准并中止租约（`user_role_changed`/`user_disabled`），角色恢复也不会复活旧批准。
- 助手被停用、失去访问或配置 revision 变化时，`_require_assistant_revision` 让后续 admission 和每次租约检查直接失败；视图显示 `permission_matches=false`。
- 应用启动恢复时撤销并中止：`server/app/runtime.py::RunManager.recover`。
- 续行 run 以 `cancelled/failed/interrupted` 终止时立即撤销并中止；成功的轮次保留租约，等待显式结算和下一轮 admission。重启路径统一保留 `service_restarted` 原因。
- 每次租约检查都复核原生 session、会话权限模式、owner 角色、助手可用性、活动 run 与用户启用状态。

## 2. 关键不变量

1. **目标与配置绑定**：批准记录 `goal_id/goal_revision/harness_session_id/assistant_revision`；admission 的原生快照与当前 assistant 配置必须逐项一致，否则 `GOAL_STALE_REVISION`、`GOAL_SESSION_CHANGED` 或 `GOAL_PERMISSION_CHANGED`。
2. **活动任务唯一**：同一会话只允许一个 `state='admitted'` 租约；并发 admission 由唯一索引仲裁，只有一个成功。
3. **真实 run**：租约必须绑定本会话、本人、当前 `active_run_id` 的 `queued/running` 普通对话 run，且该 run 的 `execution_meta.goal_continuation` 必须为 `true`（只能由宿主内部续行路径写入）。admission 不改变 run 状态、不写审批、不调用模型。
4. **轮次与费用分离**：`max_rounds`、`token_budget`、`cost_budget_micros`、`time_budget_seconds` 是四个独立字段和独立拒绝码；轮次数不能当费用预算。
5. **缺失用量不当零**：结算只读 `runs.usage`；顶层或 `token_accounting.capture_complete` 不为 `true`、缺少 token、缺少权威费用任一成立即 `usage_status='unknown'`，下一轮直接 `GOAL_USAGE_UNVERIFIED`。
6. **不自动重放**：停止/重启把租约改为 `aborted`，批准改为 `revoked`；旧租约永远不能再通过 `require_goal_lease`，必须用户重新授权并创建新 run。
7. **原文不落新表**：批准/租约表只存预算、ID、状态和用量整数，不存目标正文、模型输出、业务数据或凭据。

## 3. 合成测试清单（已写，未运行）

文件：`server/tests/test_goal_execution.py`。不启动真实 Node Harness、模型、业务工具或 driver；run 由正常 `Service.prepare_run` 创建后以合成终态和合成 usage 结算。

| 要求 | 测试 |
|---|---|
| 关闭执行试点时无接口、无记录、普通消息不受影响 | `test_execution_surface_is_absent_when_pilot_is_off` |
| owner 可授权，other 不可见/不可授权/不可撤销 | `test_owner_authorizes_and_other_user_is_denied` |
| 旧 goal revision/session、暂停、超出原生轮次、严格预算字段 | `test_authorization_requires_current_goal_and_strict_budgets` |
| 并发 admission 唯一、无审批/模型调用、run 状态不变 | `test_admission_is_unique_and_never_starts_execution` |
| 普通用户 run 不能被冒充为目标续行 | `test_plain_user_run_is_not_admissible` |
| 缺失用量为 unknown 并阻断；结算幂等且不可改写 | `test_settlement_reads_real_run_usage_and_missing_is_unknown` |
| 估算费用不能当作已结算费用 | `test_estimated_cost_is_not_a_settled_cost` |
| 真实 Harness 的嵌套 usage 回执能被读取；缺权威费用仍为 unknown | `test_nested_harness_receipt_is_read_and_missing_cost_stays_unknown` |
| 完整用量允许下一轮 | `test_complete_usage_allows_the_next_round` |
| 轮次、token、费用、时间四个预算边界 | `test_round_budget_boundary`、`test_token_budget_boundary`、`test_cost_budget_boundary`、`test_time_budget_boundary` |
| stale revision/session/phase 与原生轮次栅栏 | `test_stale_revision_session_and_round_fence` |
| 停止后租约失效、仍可结算已发生用量、不能继续 | `test_stop_cancels_future_continuation_but_settles_usage` |
| 运行中的轮次可由 owner 立即停止 | `test_owner_can_stop_while_a_round_is_running` |
| 续行 run 被取消后撤销授权、租约中止且不重放 | `test_cancelled_run_revokes_execution_without_replay` |
| 恢复撤销/中止且二次恢复无操作 | `test_recovery_revokes_and_aborts_without_replay` |
| 应用重启不恢复批准 | `test_application_restart_does_not_resume_authorization` |
| 新用户指令、权限变化停止续行 | `test_new_user_instruction_and_permission_change_stop_continuation` |
| 管理员被降级后 full 权限批准持久撤销 | `test_admin_downgrade_stops_full_permission_continuation` |
| 账号被停用后批准持久撤销 | `test_disabled_account_revokes_execution` |
| 助手 revision 变化后不能继续 | `test_assistant_revision_change_stops_continuation` |
| 助手被停用后不能继续 | `test_disabled_assistant_stops_continuation` |
| 原生 pause 状态变更停止续行 | `test_goal_pause_transition_revokes_execution_authorization` |
| 用户停用不能 admission | `test_disabled_user_cannot_admit` |

拟执行命令（本轮未执行）：

```bash
cd /media/data3/dengkw/projects/kaiwu-goal-driver-pilot-20261125/server
python -m pytest tests/test_goal_execution.py -q
```

## 4. 已知限制与明确未做

- **未接官方 driver**：没有挂载 `dsh-goal-round-driver`、`tool-goal` 或 `command-goal`，没有引入第二套循环、调度器或 Goal 状态机。原生 activation 不会被本切片 arm。
- **未做真实模型/Node 联调**：本切片所有 run 都是合成终态；普通真实 Harness run、FIFO、审批和 usage 路径未改变，只在带 `goal_continuation=true` 的 run 终态新增撤销钩子。上线前必须补真实 Node 隔离联调。
- **真实 Harness 费用仍不可核验**：固定版 `HarnessUsageLedger.receipt()` 的 `estimated_cost_usd` 恒为 `None`，且 `capture_complete` 表示 HTTP 捕获完整而非费用完整。宿主已按真实的 `token_accounting.capture_complete` 结构读取，但真实 Harness 轮次仍会因缺权威费用结算为 `unknown` 并阻断下一轮，直到有权威费用来源。这是有意 fail-closed，不是缺陷掩盖。
- **只防宿主侧并发重复**：唯一活动租约和原生轮次栅栏能防同一时刻的重复 admission；驱动进程自身 checkpoint 丢失后的“顺序重放”需要官方 driver 的 checkpoint/activation 语义才能彻底判定。源码未取得前不猜测，见第 5 节。
- **预算是轮次间栅栏，不是运行中硬中断**：token、费用和时间在 admission 与租约检查时核验。单轮运行中可能超过预算；超出后只拒绝下一轮，不会中途杀模型进程。运行中停止由 owner 的 DELETE 或现有 run 取消路径负责。
- **没有前端表单**：只有 API；工作台未新增自动执行按钮，避免误导为已开启无人值守。
- **未做 trial 专项 UI**：trial 路由本就不挂载；模块 gate 也拒绝。
- **未验证跨进程并发**：唯一索引和 SQLite 事务提供数据库级防重；本切片合成测试只覆盖同进程多线程。跨进程需在隔离环境补一次。

## 5. 接入固定官方 driver 需要确认的具体源码与接口

当前工作树没有固定的上游 `packages/goal/` 源码，不能猜测或复制 driver。下一步必须先取得固定提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` 下以下路径的实际源码和测试：

- `packages/goal/goal-round-driver/src/index.ts`（及同目录测试）：轮次开始/结束钩子、checkpoint flush 点、round fence 字段。
- `packages/goal/goal/src/index.ts`：`activation` 的取值与生命周期、`revision`/`roundsStarted` 的更新点。
- `packages/goal/tool-goal/src/index.ts` / `command-goal`：模型工具与命令是否会在 driver 外直接推进轮次。
- `packages/goal/goal-round-driver` 的官方测试夹具：确认 driver 是否暴露“每轮开始前”的可注入 seam。

本切片只校验 `activation` 的字面值属于 `armed/disarmed`，不解释其语义、不 arm/disarm，也不把它当作授权。确认官方语义前不改变这一点。

宿主给 driver 的最小 checkpoint 合同：每轮把 `lease.id` 与 `round_index` 持久化到官方 checkpoint；同一轮恢复时先 `require_goal_lease(lease_id, run_id, goal=...)`，通过才继续，绝不能新建第二个 run。租约和批准都以 SQLite 行为唯一事实源，driver 侧只做引用。

必须确认的接口问题：

1. driver 是否在 arm/推进原生 activation **之前**调用宿主 admission；如果不是，宿主 admission 必须放在它的 before-round 钩子，且 lease id 需要能在 checkpoint 中恢复。
2. driver 的 checkpoint 是否持久化 round fence；重启后能否识别“旧 lease 已 aborted，不得重放”。
3. driver 生成内部 prompt 的位置；该 prompt 必须走宿主真实 run（`Service.prepare_run` + 既有 FIFO/审批/usage），不能形成未记账的旁路执行。
4. driver 的 `maxGoalRounds` 与宿主 `max_rounds` 的关系；两者都保留，取更小者，不能互相替代 token/费用预算。
5. pause/blocked/complete 与宿主 stop 的调用顺序；原生状态变更必须先撤销宿主批准。

建议接入顺序（待源码确认后实现）：

1. 用户 `POST /goal/execution` 批准预算（本切片已完成）。
2. driver 在 before-round 钩子读取原生 goal 快照和 checkpoint。
3. 宿主通过内部路径创建真实 run（`goal_continuation=True`，不暴露给公开消息体）。
4. `admit_goal_round` 绑定该 run，取得 `lease_id`。
5. `require_goal_lease` 通过后才允许内部 prompt / enqueue。
6. run 走既有 FIFO、审批、usage；终态后 `settle_goal_round`。
7. 任何暂停/撤销/重启/权限变化都走 `stop_goal_execution`，不自动续行。

## 6. 变更文件

```text
server/app/db.py
server/app/config.py
server/app/main.py
server/app/service.py
server/app/runtime.py
server/app/conversation_goal.py
server/app/conversation_goal_execution.py   (new)
server/app/goal_execution.py                (new)
server/tests/test_goal_execution.py         (new)
client/src/types.ts
docs/plans/KAIWU_PARITY_EXECUTION.md
docs/architecture/OPEN_SOURCE_STACK.md
docs/evidence/KAIWU_GOAL_DRIVER_PILOT_2026-11-25.md
```

## 7. 下一步

1. 由总控在隔离环境实际运行 `tests/test_goal_execution.py`，核对失败项并修复。
2. 取得固定上游 `packages/goal/goal-round-driver` 源码后，按第 5 节确认 seam；确认前不接入 driver。
3. 补真实 Node/Harness 隔离联调：真实 run、FIFO、审批、usage 完整链路；若费用仍未知，保持 fail-closed。
4. 补跨进程并发、真实浏览器入口及 trial 回归；仍不解除生产 A2.10 发布闸门。

## 总控复验（2026-11-26）

工程师 2026-11-25 22:53:56 启动、11-26 00:01:21 退出码 0；候选尚未合入。第一次组合 8 通过、25 失败、1 跳过。检查隔离测试数据库确认原生 session marker 为空，定位为 fake-runtime 夹具在初次记忆快照迁移前写入 marker，随后被原有迁移机制清空。修正 test_goal_execution.py 的初始化顺序，不修改生产权限、迁移或会话状态检查。独立 Goal 执行授权/租约/预算专项 26/26 通过（controller-fixed.log，88.73s）。原始工程师说明保留，下文未运行状态仅指其初始交付。尚需基于最新主线复验、独立审查实际预算/运行回执及官方 round driver 接入。现有真实费用回执不完整，必须 fail-closed；不能将未知费用当作 0。没有启用无人值守驱动或切换生产。
