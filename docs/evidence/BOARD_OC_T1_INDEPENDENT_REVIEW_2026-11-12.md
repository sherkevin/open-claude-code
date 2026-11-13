# OC-T1 独立审核复核记录

日期：2026-11-12（Asia/Shanghai）

任务节点：`OC-T1`（独立复核受控记忆与旧内容隔离）

颜色组：红色“上下文与记忆”

审核角色：红色-子任务审核人（独立 Cloud Code 审核会话，不代表执行者）

被审执行：红色-项目子负责人

审核范围：仅 `OC-T1`。本审核会话未修改产品代码、未部署、未重启服务、未使用真实客户数据、真实凭据或付费模型。

## 1. 逐项结论

| 验收项 | 结论 | 独立依据 |
|---|---|---|
| C1 记忆编辑、删除、清空后的隔离自动化测试全部通过 | 通过 | 独立复跑 `tests/test_harness_memory.py`：4 项通过，退出码 0 |
| C2 旧内容不会通过原生历史重新进入模型输入 | 通过 | 阅读实现并独立运行原生历史隔离探针；旧运行被拒绝、旧原生会话不恢复、旧哨兵不进入活动快照 |
| C3 受审批保护的模型读写有独立证据 | 通过 | 证据文件齐全；独立复跑审批接口测试 16 项通过，退出码 0；代码路径确认模型写入必须完整正文人工批准 |

总判定：三项全部通过。建议项目负责人在任务板上记录验收结果并提交审核通过；本审核会话没有代为修改任务板。

## 2. 复核环境与方法

- 代码目录：`/media/data3/dengkw/projects/open-claude-code`
- 测试工作目录：`/media/data3/dengkw/projects/open-claude-code/server`
- 解释器：项目内 `.venv`，Python 3.10.12
- 数据：测试全部使用 `pytest` 临时目录和合成内容，没有读取客户数据，没有调用真实供应商模型。
- 负载说明：复核期间本机负载较高（load average 约 20 至 23），复跑耗时明显长于执行者原始日志；用例数量和通过结论一致。
- 判定方法：不采信执行者总结，逐项执行命令、查看原始输出、阅读实现代码和证据文件。

### 执行者原始日志与本次独立复跑对照

| 项目 | 执行者原始日志 | 本次独立复跑 | 结论 |
|---|---|---|---|
| `tests/test_harness_memory.py` | `4 passed, 1 warning in 28.03s` | `4 passed, 1 warning in 277.97s`，退出码 0 | 用例数和结果一致 |
| `tests/test_harness_memory_api.py` | `16 passed, 1 warning in 103.76s` | `16 passed, 1 warning in 335.79s`，退出码 0 | 用例数和结果一致 |

耗时差异与复核期间的高系统负载一致，不改变通过结论。两轮唯一警告都是 FastAPI `TestClient` 对当前 httpx/Starlette 组合的弃用提示。

## 3. C1：自动化隔离测试

执行命令：

```bash
cd /media/data3/dengkw/projects/open-claude-code/server
../.venv/bin/python -m pytest -q tests/test_harness_memory.py
```

独立复跑结果：

- `4 passed, 1 warning in 277.97s (0:04:37)`
- 退出码：`0`
- 原始输出：`docs/evidence/BOARD_OC_T1_INDEPENDENT_MEMORY_2026-11-12.log`

覆盖的四个场景（逐条阅读 `server/tests/test_harness_memory.py` 确认）：

1. 编辑记忆后快照标识、revision 和 `native_epoch` 变化；旧哨兵只保留在旧快照；`harness_session_id` 被清空；应用聊天历史仍保留用于审计。
2. 删除单文件和清空全部记忆后活动快照为空；清空时代次增加。
3. 过期 revision 写入返回 409 且不改变活动绑定；`../escape.md` 越界路径失败关闭，活动内容不变。
4. 已登记的历史工具结果和旧附件内容不进入活动记忆快照。

判定：**通过**。执行者原始日志为 `4 passed`、声明的退出码为 0；本次独立复跑得到相同用例数和通过结果，并实际捕获到退出码 0。

## 4. C2：旧内容不通过原生历史重新进入模型输入

### 4.1 实现路径核对

- 记忆快照发布时，只要内容发生变化或执行清空，就在同一个 SQLite 事务内把 `native_epoch` 加一、把 `harness_session_id` 置空、把 `context_usage` 置空：`server/app/memory_snapshots.py:627-639`。
- 运行开始时把记忆绑定和代次写入任务执行元数据；运行时重新取当前绑定，若与任务记录的绑定不一致则返回 `409 MEMORY_REVISION_CONFLICT`：`server/app/service.py:704-709`。
- 原生会话标识由当前执行器决定，Harness 读取 `harness_session_id`：`server/app/service.py:45-47`。
- Harness 只有在配置中存在原生会话标识时才调用 `resume_session`，否则生成新的 `workspace-<id>` 会话：`server/app/harness_runtime.py:253`、`server/app/harness_runtime.py:306-307`。
- 每轮发给模型的提示只包含当前用户消息文本和本轮 Skills，不重放应用消息历史：`server/app/harness_runtime.py:381-384`。

### 4.2 独立探针

为避免只依赖测试文件里的断言，本次另写只读探针，在临时数据目录中模拟“旧原生会话 + 旧聊天记录”，然后编辑记忆，再检查旧任务和新任务的行为。探针不调用供应商，不修改仓库代码。

- 探针脚本：`.runtime/verification/oc-t1-independent-review-20261112/c2_native_history_probe.py`
- 原始输出：`docs/evidence/BOARD_OC_T1_INDEPENDENT_C2_PROBE_2026-11-12.log`
- 退出码：`0`

探针结果：

- 编辑后 `native_epoch` 从 1 增加到 2，`context_reset=true`。
- 编辑后 `harness_session_id` 为 `null`。
- 活动记忆快照中不含旧哨兵 `C2-OLD-SENTINEL-771`，包含新哨兵 `C2-NEW-SENTINEL-772`。
- 已结束的旧任务被拒绝：`409 MEMORY_RUN_CHANGED`。
- 把旧任务强制改回运行中后仍被拒绝：`409 MEMORY_REVISION_CONFLICT`（任务记录的旧绑定与当前代次不一致）。
- 新任务的记忆绑定与编辑后的快照一致。
- 新任务的原生会话标识为 `null`，不会恢复 `stale-native-session`。
- 应用聊天历史仍保留旧文本用于审计（这是设计要求，不是泄漏；该历史不会被 Harness 作为模型输入重放）。

判定：**通过**。旧内容不会通过旧原生会话恢复，也不会进入新任务的活动记忆绑定；旧任务在两种生命周期状态下都被拒绝。

边界：本项证明到应用层绑定、代次、会话标识和运行时配置校验，没有在供应商协议层抓包。若需要“模型实际收到什么”的线级证据，应另开受控真实线路验证。

## 5. C3：受审批保护的模型读写证据

### 5.1 证据文件核对

`docs/evidence/` 中与本节点直接相关的文件：

- `BOARD_OC_T1_MEMORY_REVIEW_2026-11-12.md`：记录测试范围、两条命令、结果、原始日志路径和未覆盖边界。
- `BOARD_OC_T1_MEMORY_REVIEW_2026-11-12.log`：`4 passed, 1 warning in 28.03s`。
- `BOARD_OC_T1_MEMORY_APPROVAL_2026-11-12.log`：`16 passed, 1 warning in 103.76s`。
- `HARNESS_MEMORY_2026-11-11.md`：既有受控记忆、完整正文审批、提交与回滚的完整回归证据。
- 执行者证据文件时间顺序合理：测试文件修改时间早于对应日志；日志早于提交审核。

### 5.2 独立复跑审批测试

执行命令：

```bash
cd /media/data3/dengkw/projects/open-claude-code/server
../.venv/bin/python -m pytest -q tests/test_harness_memory_api.py
```

独立复跑结果：

- `16 passed, 1 warning in 335.79s (0:05:35)`
- 退出码：`0`
- 原始输出：`docs/evidence/BOARD_OC_T1_INDEPENDENT_APPROVAL_2026-11-12.log`

覆盖内容（逐条阅读 `server/tests/test_harness_memory_api.py` 确认）：

- 完整正文只对会话所有者开放，响应禁止缓存，公共审批视图不含正文，其他用户访问返回 404。
- 批准必须提交与完整正文完全匹配的摘要；缺失、全零或格式错误的摘要被拒绝。
- 允许和拒绝都会记录决策人，但批准本身不会发布记忆（测试断言批准后记忆文件仍为空）；最终提交必须等任务成功结束并关闭执行器，这一步由运行时/存储测试覆盖，不在本文件的 16 项内。
- 读取正文后，助手停用、用户停用、任务取消、代次变化、审批过期、正文变化、双人审批、审批已结束、操作已丢弃九种变化都会让批准失效。
- 非记忆审批不能走完整正文路由；非法摘要返回 422。

### 5.3 代码路径核对

- `memory_write` 被定义为需要用户确认的中风险工具：`server/app/security.py:92-94`。
- 模型发起写入时先暂存不可变提案，再请求审批，然后重新校验并读取可核验的人工批准记录：`server/app/harness_memory.py:165-197`。
- 完整正文审批校验要求决策人是当前用户、来源为用户、审批未过期、正文和策略绑定未变化：`server/app/harness_memory.py:90-119`。
- 提交阶段再次核对审批摘要、授权回调、最终任务成功终态和会话空闲，否则拒绝：`server/app/memory_snapshots.py:616-647`。

判定：**通过**。证据文件存在且内容与原始日志一致，本次独立复跑 16 项通过；代码路径确认模型写入不能绕过完整正文人工审批。

边界：审批接口测试直接插入审批记录和等待中的 future，并不是从模型工具调用到审批、提交的完整端到端驱动；模型工具调用路径由 `server/tests/test_harness_memory_runtime.py` 和既有 `HARNESS_MEMORY_2026-11-11.md` 覆盖。本次未复跑运行时测试，因为本机没有配置 `AGENT_HARNESS_NODE_PATH` 和 `AGENT_HARNESS_SOURCE_DIR`，需要固定 Node 工具链。

## 6. 未覆盖边界

1. 没有调用真实供应商模型，因此不能证明模型对记忆内容的语义利用质量，也没有供应商协议层抓包。
2. 没有使用真实客户数据、生产凭据，没有在共享服务或生产环境部署、重启、切换。
3. 被审测试文件和 Harness 实现文件当前在 Git 中未跟踪（`??`），证据没有绑定到某个提交哈希。工作树一旦变化，本记录需要重新复跑。
4. C2 的探针只直接覆盖编辑后的旧会话隔离；删除和清空的会话清空由测试的代次断言和发布代码共同证明，测试文件没有对删除/清空单独断言 `harness_session_id` 为空。
5. 审批测试的审批记录是测试夹具直接插入的，不是模型工具真实发起；完整端到端模型工具审批路径未在本次复跑。
6. 复核期间系统负载较高，复跑耗时（C1 277.97 秒，C3 335.79 秒）远高于执行者原始日志（28.03 秒、103.76 秒），但用例数量和结论一致；没有做独立的长稳或压力测试。
7. 附件投递隔离和失败回滚没有在本节点的 4 项测试中直接驱动：4 项测试只验证旧的 workspace 上传文件不出现在活动记忆快照；真正的附件投递隔离由 `server/app/harness_attachments.py:184-244` 的 `native_epoch` 校验实现，失败/取消回滚由 `server/tests/test_harness_memory_runtime.py`、`server/tests/test_harness_memory_storage.py` 等既有测试覆盖。本次没有复跑这些文件。

## 7. 建议的项目负责人操作

以下命令应由项目负责人或任务板授权角色执行，本审核会话没有执行：

```bash
/media/data3/dengkw/projects/project-board/boardctl check set --task OC-T1 --check C1 --status passed --actor reviewer \
  --evidence "独立复跑 tests/test_harness_memory.py：4 passed，退出码 0；日志 docs/evidence/BOARD_OC_T1_INDEPENDENT_MEMORY_2026-11-12.log"

/media/data3/dengkw/projects/project-board/boardctl check set --task OC-T1 --check C2 --status passed --actor reviewer \
  --evidence "独立探针确认编辑后 native_epoch+1、harness_session_id 清空、旧任务 409、新任务不恢复旧会话；日志 docs/evidence/BOARD_OC_T1_INDEPENDENT_C2_PROBE_2026-11-12.log"

/media/data3/dengkw/projects/project-board/boardctl check set --task OC-T1 --check C3 --status passed --actor reviewer \
  --evidence "证据文件齐全；独立复跑 tests/test_harness_memory_api.py：16 passed，退出码 0；日志 docs/evidence/BOARD_OC_T1_INDEPENDENT_APPROVAL_2026-11-12.log"

/media/data3/dengkw/projects/project-board/boardctl review --task OC-T1 --result pass --actor reviewer \
  --reason "独立复跑 C1 4 项、C3 16 项均通过且退出码 0；C2 由实现核对和独立探针确认旧内容不通过原生历史进入新输入；未覆盖边界已记录"
```
