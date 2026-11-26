# C2.3 原生 Team 持久协作产品切片：任务生命周期（证据草稿）

日期：2026-11-26
状态：**代码、单元测试、真实 Node 集成测试和探针已写入；本会话未执行任何测试**。
本会话只有受控文件工具（Read/Glob/Grep/Edit/Write），没有 shell。因此本文所有“预期结果”都不是实测结果，不能作为已通过、已合入或已上线依据。请总控按第 7 节命令复验。

## 1. 切片范围与结论

本切片在既有 owner-scoped、只读原生 Team 投影之上，增加一个**显式授权、严格绑定会话、限次限时的任务板元数据生命周期**：

- 启用/停用官方 `agentTeams` 任务板；
- 创建任务（含官方 `blockedBy` 依赖和 `writeScopes` 提示）；
- 认领（claim）、完成任务（complete）；
- 官方 CAS `expectedRevision` 冲突可核对；
- 宿主侧授权、操作审计、历史查询、结果未确认的人工确认；
- 服务重启后从官方 JSONL 恢复任务板，同时撤销旧授权并要求重新授权；
- 产品 API 和工作台面板。

任务板与任务历史仍完全由 pinned 官方 `agentTeams` 服务和 JSONL 会话投影持有。宿主没有第二任务板、第二任务图或第二 agent 管理引擎；授权表不保存任务标题/说明，只保存操作摘要和任务 id/revision/status。

**没有**启用：模型轮次、子智能体执行、任意 shell、跨用户通信、业务系统写入、权限扩展、自动重试。所有 `execution_enabled`、`task_execution_enabled`、`subagent_execution_enabled` 固定为 `false`。

与候选 commit `72873e0` 的关系：本切片**没有复用、修改或重放**该候选的任何代码或其被安全检查阻止的浏览器脚本；工作台入口是本任务独立新增的 `ConversationTeam` 面板。该候选未通过真实浏览器验收、未合入，本切片也不声称已通过浏览器验收。总控决定两者后续如何处理。

## 2. 开关与默认值

新增独立环境开关，默认关闭：

```bash
AGENT_HARNESS_TEAM_STATE_ENABLED=true
AGENT_HARNESS_TEAM_TASKS_ENABLED=true
```

约束：

- `TEAM_TASKS` 必须建立在 `TEAM_STATE` 之上；`Settings.__post_init__` 在只开 tasks 时直接报错：
  `AGENT_HARNESS_TEAM_TASKS_ENABLED requires AGENT_HARNESS_TEAM_STATE_ENABLED=true`。
- API 路由只在 `harness_team_state_enabled and harness_team_tasks_enabled` 时挂载；仅开 state 时仍只有只读 `/team`。
- 配置接口仅在 tasks 开关开启且非 trial 时返回 `harness_team_tasks: true`；工作台据此显示入口。
- `write_profile` 在 `team_tasks=True` 且 `team_state=False` 时拒绝生成 profile；host 配置只有在此条件下才带 `teamTasks: true`。
- trial 模式始终不启用（宿主检查 `policy.is_trial`）。

## 3. 变更文件

### 服务端

| 文件 | 作用 |
| --- | --- |
| `server/app/config.py` | 新增 `harness_team_tasks_enabled` 及 tasks→state 校验 |
| `server/app/db.py` | 新增 `team_task_authorizations`、`team_task_operations` 及部分唯一索引 |
| `server/app/team_tasks.py` | 授权、操作准入、幂等、回执校验、历史、撤销、重启恢复、用户停用/降权撤销 |
| `server/app/conversation_team_tasks.py` | `/api/v1/conversations/{id}/team/...` 产品路由 |
| `server/app/harness_runtime.py` | `workspace/team-task` RPC 客户端、严格回执模型、受控错误翻译、运行时关闭/回收 |
| `server/app/runtime.py` | `SessionActor.team_task_command`、`RunManager.team_task`、串行预约、启动恢复接入 |
| `server/app/main.py` | 条件挂载路由、配置开关透出 |
| `server/app/service.py` | 用户停用/降权、会话权限变更/首次归档时的撤销与未决操作围栏 |
| `server/harness/profile.py` | `team_state`/`team_tasks` 分离写入、布尔校验、依赖校验 |
| `server/harness/team_tasks.ts` | 官方 `agentTeams` 的唯一变更 seam：精确字段、会话/根 agent 校验、官方 CAS、回执规范化 |
| `server/harness/workspace_plugin.ts` | 注册 `workspace/team-task`，仅在 `teamTasks===true` 且已注入 Team 服务时可用 |

### 工作台

| 文件 | 作用 |
| --- | --- |
| `client/src/ConversationTeam.tsx` | 新增独立任务板面板：授权、撤销、启停、创建、依赖、认领、完成、历史、未确认确认 |
| `client/src/team-collaboration.css` | 面板样式 |
| `client/src/ChatPage.tsx` | 仅在 `config.harness_team_tasks` 且 harness 会话时显示入口 |
| `client/src/types.ts` | 新增 `harness_team_state?`、`harness_team_tasks?` |

### 测试与探针

| 文件 | 覆盖 |
| --- | --- |
| `server/tests/test_team_tasks.py` | 默认关闭、独立开关、回执失败关闭、授权/撤销/隔离、幂等/冲突/不确定/确认、次数/过期/重启恢复、用户角色变更撤销 |
| `server/tests/test_harness_team_tasks_real.py` | 真实 pinned Node + 官方 JSONL + 本地合成 Messages 服务：授权、启停、依赖、claim/complete、stale CAS、replay、撤销、非 owner、零额外模型请求、重启恢复与再授权 |
| `server/tests/test_harness_profile.py`（追加） | tasks 不能脱离 state；`teamTasks` 只在双开时出现；非法布尔值拒绝 |
| `client/src/ConversationTeam.test.tsx` | 纯函数规范化、CAS 只带 revision、任务板渲染、未确认标签、禁用入口 |
| `scripts/oss/harness_team_tasks_probe.mts` | 独立可复现的 pinned 官方服务/JSONL seam 探针，不启动模型 |

## 4. 产品 API

所有路由归 owner 所有；非 owner 返回 404。全部 `Cache-Control: no-store`。

| 方法/路径 | 说明 |
| --- | --- |
| `GET /team` | 既有只读官方任务板；本切片未改变其响应 |
| `GET /team/collaboration` | 授权状态、绑定匹配、已用/剩余次数、最近 50 条操作、未决操作 |
| `POST /team/authorization` | 显式授权：`harness_session_id`、`max_operations`、`expires_in_seconds`、`confirm:true` |
| `DELETE /team/authorization` | 撤销授权；围栏 pending 操作；不删除官方任务板/历史 |
| `POST /team/board` | `action: enable|disable`、`confirm:true`、客户端幂等 id |
| `POST /team/tasks` | 创建任务：`subject`、`description`、`blocked_by[]`、`write_scopes[]` |
| `POST /team/tasks/{task_id}/claim` | 认领；必须带 `expected_revision` |
| `POST /team/tasks/{task_id}/complete` | 完成；必须带 `expected_revision` |
| `POST /team/operations/{operation_id}/acknowledge` | 人工确认“结果未确认”的操作，之后才允许继续写 |

授权响应是 `team-collaboration/v1`；变更响应是：

```json
{
  "schema": "team-task-response/v1",
  "operation": {"id": "...", "action": "create", "status": "confirmed",
                 "task_id": "...", "task_revision": 1, "task_status": "pending", "...": "..."},
  "board": {"enabled": true, "tasks": []},
  "task": {"id": "...", "revision": 1, "status": "pending"},
  "execution_enabled": false,
  "replayed": false
}
```

`operation` 只暴露任务 id/revision/status，不暴露 subject/description；任务内容只从官方任务板读取。

## 5. 状态机与安全边界

### 5.1 授权

- 只有会话 owner 可授权；必须显式 `confirm:true`；次数 1–100，有效期 60 秒–7 天。
- 授权绑定：`harness_session_id`、助手 revision、会话 permission mode。
- 同一会话同时最多一条 active 授权（部分唯一索引兜底）。
- 授权不调用 Node，不修改任务板。

### 5.2 每笔操作

1. 校验请求字段集合与内容；生成规范化 native 载荷和 SHA-256 digest。
2. 以 `(conversation_id, client_request_id)` 做幂等：
   - confirmed → 直接回放，不再触达 Node；
   - pending → `TEAM_OPERATION_IN_PROGRESS`；
   - uncertain → `TEAM_OPERATION_UNCERTAIN`；
   - rejected → 返回上次稳定错误码；
   - 同一 id 不同 digest → `TEAM_REQUEST_CONFLICT`。
3. 重新校验 owner、未归档、无 active run、无记忆变更、助手可用、会话精确匹配、授权 active/未过期/绑定未变。
4. 如果有未确认操作或已有 pending，拒绝；如果次数用尽，拒绝。
5. 在事务中插入 `pending` 审计行。
6. 通过 `RunManager.team_task` 占用与 Goal/Team 读共享的 `goal_requests` 串行槽，交给会话 actor。
7. actor 再次校验会话，`ensure_runtime` 后再次校验，再调用 `workspace/team-task`。
8. Node seam 校验精确字段、绑定的 `sessionId`、根 agent 空闲且为 root；只调用官方 `agentTeams`。
9. Python 严格校验回执（schema、source commit、session、action、board/task 一致性、任务必须在板内、状态与动作匹配）。
10. 只允许 pending→confirmed；确认后关闭并回收 Node 进程，再检查会话绑定；绑定变化返回 `TEAM_BINDING_CHANGED`（操作已生效，提示刷新）。
11. 受控拒绝 → rejected；发送后任何未知错误或回执不匹配 → uncertain，绝不自动重试。

### 5.3 官方依赖与 CAS

- 依赖完全使用官方 `createTask({blockedBy})`；`ready` 由官方投影计算。
- claim/complete 使用官方 `updateTask({taskId, expectedRevision, action})`；宿主只做稳定的前置 stale 检查，官方仍是最终 CAS 权威。
- 稳定错误码：`TEAM_TASK_STALE_REVISION`、`TEAM_TASK_NOT_FOUND`、`TEAM_BOARD_DISABLED`、`TEAM_TASK_BLOCKED`、`TEAM_TASK_LIMIT`、`TEAM_TASK_TRANSITION`、`TEAM_REQUEST_INVALID`、`TEAM_SESSION_BUSY`。
- 官方 projection 中任务级未知字段被丢弃，不越过宿主边界。

### 5.4 取消/撤销边界

- 本切片不启动任何执行，因此“取消”只能是：
  - 撤销宿主授权，停止未来的元数据变更；
  - 围栏 pending 操作为 uncertain；
  - 官方任务板与 JSONL 历史保持可读。
- 无法取消“原生任务执行”，因为此切片从不启动它。
- 用户停用、角色变更、会话权限变更、首次归档、服务重启都会撤销 active 授权；用户停用/角色变更还会围栏 pending。
- 撤销不会删除授权/操作行；审计保留。

### 5.5 重启恢复

启动时 `RunManager.recover()` 调用 `recover_team_tasks`：

- 所有 active 授权 → revoked，`revoked_reason=service_restarted`；
- 所有 pending 操作 → uncertain，错误码 `SERVICE_RESTARTED`；
- 不触碰官方 JSONL；owner 仍可读任务板和历史；
- 用户必须显式重新授权，之后才能继续变更；
- uncertain 操作必须人工核对并 acknowledge 后才允许下一笔写。

## 6. 测试设计（未执行）

### 6.1 `server/tests/test_team_tasks.py`

不启动 Node，使用 `FakeRuntime` 只做会话搭建：

- 默认无路由；只开 state 时无 tasks 路由；
- 回执模型失败关闭：错误 schema/source/action/status/board/execution 标志、任务不在板内、未知任务字段丢弃；
- 授权：错误 session、重复授权、非 owner 404、撤销原因、次数/剩余；
- 操作：native 载荷字段、pending 重复、确认、replay、digest 冲突、uncertain 阻塞、人工 acknowledge 后恢复；
- 次数用尽、过期、重启恢复（撤销 + 围栏 + 不确定）；
- 用户角色变更撤销。

### 6.2 `server/tests/test_harness_team_tasks_real.py`

本文件是**真实运行时**测试，仅当 `HARNESS_TEST_NODE`/`HARNESS_TEST_SOURCE` 设置时执行，否则 skip。唯一替代是本地合成 Messages HTTP 端点；Node 进程、pinned 官方 Team 服务、JSONL、FastAPI、SQLite 全部真实。

流程：

1. 真实首轮对话建立 harness session；
2. 读取空板，确认未授权时创建被拒；
3. 显式授权（20 次、1 小时）；
4. 未启用板时创建 → `TEAM_BOARD_DISABLED`；
5. 启用板；
6. 创建 A，创建依赖 A 的 B，B 初始 `ready=false`；
7. claim A；用旧 revision complete → `TEAM_TASK_STALE_REVISION`；
8. 用 claim 后 revision complete A；B 变 `ready=true`；
9. 用同一 client_request_id 重放创建 A → `replayed=true` 且不新增任务；
10. 历史中同时有 confirmed 和 rejected；
11. 撤销授权后创建 → `TEAM_NOT_AUTHORIZED`；
12. 留一条 active 授权，重启应用；
13. 重启后任务板从 JSONL 恢复（A completed、B pending），授权为 revoked/service_restarted，历史保留；
14. 重新授权，claim/complete B；
15. 全程模型请求数保持首轮后的基线不变；非 owner 全部 404。

### 6.3 探针

`scripts/oss/harness_team_tasks_probe.mts` 直接加载 pinned 上游 fixture stack，验证：

- 未启用板时创建被拒；
- 开关/源码 commit/初始化/会话/精确字段失败关闭；
- 启用幂等；
- 未知任务字段不能跨边界；
- 创建/依赖/claim/stale CAS/complete/ready；
- 不同 root 板隔离，模型请求为 0；
- dispose 后从实际 JSONL 恢复；
- 重启后继续 claim/complete，模型请求仍为 0。

## 7. 总控复验命令

以下命令均未在本会话执行。路径按当前机器既有环境给出；若 controller 使用自己的 venv/Node/source，请替换。

```bash
cd /media/data3/dengkw/projects/kaiwu-team-collaboration-20261126
mkdir -p .tmp .probe-tmp
```

### 7.1 快速宿主单测

```bash
cd server
TMPDIR=/media/data3/dengkw/projects/kaiwu-team-collaboration-20261126/.tmp \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  tests/test_team_tasks.py -q
```

预期：全部通过；无 Node、无网络、无真实模型。若失败，优先检查：
`test_team_tasks.py::test_operation_limit_expiry_and_recovery` 的 raw SQL 更新、
`_TeamTaskReceipt` 的 advisory 默认值、以及 `Settings` 的 tasks→state 校验。

### 7.2 Profile 分离

```bash
cd server
TMPDIR=/media/data3/dengkw/projects/kaiwu-team-collaboration-20261126/.tmp \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  tests/test_harness_profile.py::test_team_profile_mounts_only_read_only_services \
  tests/test_harness_profile.py::test_team_task_profile_requires_state_and_stays_narrow -q
```

预期：全部通过；`teamTasks` 只在双开时出现；tasks-only 被拒绝。

### 7.3 真实 pinned Node 集成

```bash
cd server
export HARNESS_TEST_NODE=/media/data3/dengkw/projects/open-claude-code/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node
export HARNESS_TEST_SOURCE=/media/data3/dengkw/projects/open-claude-code/.runtime/third_party/deepseek-harness
TMPDIR=/media/data3/dengkw/projects/kaiwu-team-collaboration-20261126/.tmp \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  tests/test_harness_team_tasks_real.py -q
```

前置：pinned source 的 `git rev-parse HEAD` 必须为 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`，且工作树干净；Node 24 可执行；合成 Messages 端点由测试自身启动。预期：1 个测试通过，耗时约十余秒到一分钟（每个任务操作会启动并关闭一个 Node 进程）。

### 7.4 独立 seam 探针

```bash
cd /media/data3/dengkw/projects/kaiwu-team-collaboration-20261126
export HARNESS_TEST_SOURCE=/media/data3/dengkw/projects/open-claude-code/.runtime/third_party/deepseek-harness
node --import "$HARNESS_TEST_SOURCE/node_modules/tsx/dist/esm/index.mjs" \
  scripts/oss/harness_team_tasks_probe.mts \
  "$HARNESS_TEST_SOURCE" "$PWD/.probe-tmp"
```

预期：退出码 0，标准输出单行 JSON，包含：

```json
{"upstream":"fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159","passed":7,"checks":[...],"storage":"/media/data3/..."}
```

若 `passed` 不是 7，按 `checks` 定位；若报 commit 不匹配，说明 source 不是 pinned 版本。

### 7.5 工作台

```bash
cd client
npx tsc -b
npx vitest run src/ConversationTeam.test.tsx
```

前置：`client/node_modules` 已安装。预期：TypeScript 编译通过；4 个测试通过。本会话**没有运行** `tsc`/`vitest`，也没有真实浏览器验收。

### 7.6 全量回归建议

```bash
cd server
TMPDIR=/media/data3/dengkw/projects/kaiwu-team-collaboration-20261126/.tmp \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest -q
```

至少确认既有的 `test_harness_team_state.py`、`test_harness_profile.py`、`test_api.py` 未因路由/开关/profile 改动回归。

### 7.7 产品入口

- 开关：`AGENT_HARNESS_TEAM_STATE_ENABLED=true` 且 `AGENT_HARNESS_TEAM_TASKS_ENABLED=true`；两者都默认关闭。
- 工作台：Harness 会话工具栏的“团队任务板”图标（`Users`），仅在 `/api/v1/config` 返回 `harness_team_tasks:true` 时显示。
- API 前缀：`/api/v1/conversations/{conversation_id}/team`；只读板为 `GET /team`，协作视图为 `GET /team/collaboration`。
- 任务板唯一权威：官方 pinned `agentTeams` 服务 + `agentTeam` JSONL 投影；宿主表 `team_task_authorizations` / `team_task_operations` 只做授权和审计。

## 8. 验收映射

| 任务要求 | 实现 | 复验入口 |
| --- | --- | --- |
| 明确授权 | 显式 `confirm`、次数、有效期、绑定 revision/permission | 7.1 / 7.3 |
| 严格会话绑定 | 请求 session、授权 session、会话当前 session、Node config session 四重一致 | 7.1 / 7.3 |
| 任务生命周期 | enable/create/claim/complete + 官方依赖 | 7.2 / 7.3 / 7.4 |
| 可核对状态/历史 | `GET /team` + `GET /team/collaboration` + 操作历史 | 7.3 |
| 重启恢复 | JSONL 恢复；授权撤销；pending 围栏；重新授权 | 7.3 / 7.4 |
| 权限边界 | owner-only；非 owner 404；停用/降权/权限变化/归档撤销 | 7.1 / 7.3 |
| 取消边界 | 撤销未来写；保留板和历史；无执行可取消 | 7.1 / 7.3 |
| 不默认开启 | 双开关，默认 false；tasks 依赖 state | 7.1 / 7.2 |
| 产品接口/工作台 | 8 个 API + 独立 `ConversationTeam` 面板 | 7.3 / 7.5 |
| 不另造任务板/引擎 | 宿主只存授权/审计摘要；官方 `agentTeams` + JSONL 唯一权威 | 代码审查 |
| 不启用未核实执行 | 无模型轮次、subagent、shell、业务写入；所有执行标志 false | 7.3 / 7.4 |

## 9. 已知限制与风险

1. **未执行**：本会话没有 shell，所有测试、`tsc`、探针和浏览器验收都未运行；不能声称通过。
2. **无浏览器验收**：工作台是新增独立面板，但未在真实浏览器点过；布局、交互、错误提示可能仍需调整。
3. **仅元数据**：这是任务板生命周期，不是持久子智能体跨轮管理，也不执行任务。`claim` 的 native owner 是官方服务记录的 root agent，不是人类账号；人类授权由宿主 owner 行表达。
4. **每个操作一个 Node 进程**：每次变更都重新启动私人 Harness 进程、resume JSONL、调用 seam、关闭回收。功能边界清晰，但延迟和启动成本高于常驻进程。
5. **跨进程 owner 假设**：每次操作都是新 Node 进程。claim 的 owner 身份写入官方 JSONL，complete 依赖官方按 session id 恢复该身份。真实测试正是按“claim 进程与 complete 进程不同”设计；本会话未执行，因此这是最需要实测确认的上游语义。如果官方要求同一进程内的 owner 对象，claim→complete 会失败，需要改为在单次 actor 运行中完成或调整官方用法。
6. **单机/单库假设**：同一 SQLite 库通过部分唯一索引保证每会话最多一个 pending；两个独立部署若共享同一 JSONL 目录但不同数据库，宿主不会跨进程协调。该部署方式不受支持。
7. **不可删除任务**：官方 API 只有 enable/disable/create/claim/complete；停用板保留任务，已完成任务保留。
8. **writeScopes 只是提示**：官方投影的 `writeScopeWarnings` 会显示，但不构成文件系统或业务写权限。
9. **重启必重新授权**：这是有意的安全设计，不是缺陷；任务板和历史不受影响。
10. **不确定操作阻塞**：未知 RPC 结果进入 uncertain，必须人工核对并 acknowledge；没有自动回放。这会降低可用性，但避免重复写。
11. **任务内容不进入宿主审计**：宿主只存 id/revision/status 和操作摘要，历史无法还原 subject/description；任务板是唯一内容权威。
12. **上游投影字段兼容性**：任务级未知字段被丢弃，board 顶层未知字段仍失败关闭。若上游新增具有语义的 board 字段，需要重新审查，不能静默接受。
13. **trial 模式**：始终关闭，不会挂载路由或 Node seam。
14. **候选 72873e0**：未合入、未复用、未做浏览器验收；本切片不改变其状态。

## 10. 并发接线冲突点（给总控）

1. `server/app/runtime.py`：`goal_requests` 同时服务 Goal、Team 读和 Team 写；`team_task_command` 必须在所有异常路径 `pop`，否则后续维护操作永久 `TEAM_SESSION_BUSY`。任何并行修改该字典的 PR 都要检查 `finally`。
2. `server/app/runtime.py` 的 actor tuple 分派：`("team-task", ...)` 必须保持在 `("team", ...)` 之后、默认 context 分支之前；新增 tuple 类型需避免参数顺序冲突。
3. `server/app/service.py`：`patch_user`/`patch_conversation` 的撤销钩子位于事务内，和 Goal execution、归档、权限变更的并行改动容易冲突；合并时保留 `stop_team_tasks_locked` 的事务语义。
4. `server/app/db.py`：所有表在单个 `SCHEMA` executescript 中；新增表必须幂等，不能与并行分支重名。
5. `server/app/main.py`：路由条件挂载顺序与 `/config` 标志；tasks 路由必须嵌在 state 条件内，避免只开 tasks 时挂载出不可用 API。
6. `server/harness/profile.py`：模块 tuple、`teamState`/`teamTasks` host 配置、布尔/依赖校验；并行 harness profile 改动可能在同一 dict 展开处冲突。
7. `server/harness/workspace_plugin.ts`：`ctx.inject` 依赖列表与 RPC 分派；`teamContext` 是单一绑定，Team read/write 共用；不要在别处覆盖。
8. `client/src/ChatPage.tsx`：工具栏插入点与候选 72873e0 可能冲突；本切片入口受 `config.harness_team_tasks` 控制，若候选后续合入需人工决定保留哪一个，不能直接叠加。
9. `client/src/types.ts`：config 类型新增两个可选布尔字段；与并行 harness flag 改动合并时保持可选，避免旧配置接口类型破裂。
10. 证据文档：本文件是专项草稿，未改总计划或公共架构文档；总控统一维护。

## 11. 证据链

- `docs/evidence/KAIWU_TEAM_STATE_SEAM_2026-11-26.md`：只读 Team seam 的已接受边界。
- `docs/evidence/KAIWU_TEAM_PRODUCT_READ_2026-11-26.md`：owner-scoped 只读产品 API。
- `scripts/oss/harness_team_state_probe.mts`：本探针复用的上游 fixture stack 与 JSONL 恢复方法。
- `scripts/oss/harness_team_tasks_probe.mts`：本切片的独立 seam 探针。
- `server/tests/test_harness_team_tasks_real.py`：真实运行时验收脚本。
- `server/tests/test_team_tasks.py`：宿主授权/审计单测。
- `client/src/ConversationTeam.test.tsx`：工作台纯函数与静态渲染测试。

## 总控首轮复验（2026-11-26 17:18 CST）

初验 69 passed、2 failed、1 error；修复宿主回执夹具的 snake_case 契约及缺失 pytest fixture，专项 9/9；补充原生 RPC 发送前核对 pending 记录、最新授权、有效期、绑定及额度，授权测试 9/9。前端 4/4，构建成功。均为独立旧基线测试，尚未通过最新 main 合并复验。
