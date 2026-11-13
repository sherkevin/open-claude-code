# 知行工作台试用整改证据（OC-T13，2026-11-12）

- 服务器：学校云（唯一开发与验收环境）
- 项目：知行工作台（open-claude-code）
- 证据类型：合成资源、进程内回环 HTTP、确定性注入运行时；零真实模型调用、零真实客户数据、无浏览器端到端
- 本次 dispatch 结论：**outcome=failed**。试用启用链路（16 项烟测、4 槽容量门禁、干净 1/5 短矩阵）全部通过；全量回归 57 个失败，经逐类重跑后归因于共享工作树交叉变更、解释器启动路径与环境变量污染，其中 6 个属于共享工作树内其他改动造成的真实失败，尚未达到“可信全绿”。
- 原始产物目录：`.runtime/evidence/`（本文件只引用实际落盘的 JSON 与日志，不复制旧数字）

## 1. 背景

知行工作台是面向企业知识问答与智能体工作流的试用产品。OC-T13 的质量评估要求证明：聊天、SSE 流、知识检索、附件、取消五条链路在合成并发下行为正确；默认 4 个执行槽的容量边界精确；SQLite FULL 持久化在验收环境中真实生效；会话执行器在取消、空闲回收和关闭时不遗留任务；SSE 断线重连不掩盖真实失败。本 dispatch 负责整改与试用验收的第一阶段：修复已定位的竞态与报告缺陷，复测 1/5/10/25 并发矩阵与全量回归，产出可复现证据。

约束：共享脏工作树，不得 `git reset/checkout/clean`，不得提交或覆盖他人修改；不修改 project-board 代码；不重启共享服务；不访问腾讯云；不读凭据、不调用真实模型；所有临时目录、虚拟环境、缓存放在 `/media/data3/dengkw` 下，`TMPDIR` 固定为 `.runtime/tmp`。

## 2. 环境与本次运行条件

- Linux 6.8.0-40-generic，32 个处理器，Python 3.10.12，uvicorn 0.54.0，httpx 0.28.1。
- 数据库：SQLite WAL，部署与验收默认 `AGENT_SQLITE_SYNCHRONOUS=FULL`；容量报告读取实际 `PRAGMA synchronous`，本次为 2（FULL），`journal_mode=wal`。
- 解释器：根 `.venv/bin/python3`，依赖已具备。
- 环境噪声与机器状态（如实记录，不修饰）：
  - 灰色发布负责人的发布安装脚本 `trial_release_install.py` 于本地时间 23:04:38 启动，约 23:15 结束；期间编译/安装占用高 CPU 与数据盘 IO，系统负载从 4.37 升到 10.08。
  - 根分区 `/dev/sde2` 在回归期间一度 100% 满、可用 0 字节。协调者把本项目 2026-11-06 遗留、无进程占用的 `/tmp/open-claude-pylib` 迁移到数据盘，恢复 387MB 可用；该操作未删除其他用户或共享服务文件。
  - 本会话所有测试进程的 `TMPDIR` 都指向数据盘 `.runtime/tmp`（回归期间核实进程环境），数据盘可用约 200GB；根盘写满不是本测试进程造成，也没有出现 `No space left on device` / `Errno 28` 失败签名（完整回归日志中 0 次）。
  - 共享工作树上同时有其他 Agent 的未提交改动和测试进程；本报告只声明自己修改的片段，不把整棵树的差异算作本 dispatch 的成果。

## 3. 本 dispatch 修改清单

只列本 dispatch 负责的文件与片段；这些文件同时包含其他 Agent 的未提交改动，未做整体回退。

| 文件 | 修改要点 | 对应回归 |
|---|---|---|
| `server/app/runtime.py` | `SessionActor.loop` 在 `asyncio.TimeoutError` 时检查 `pending` 与队列，队列非空则继续取任务，避免超时与入队同轮发生导致任务永久排队；`RunManager` 新增 `retiring` / `retiring_by_conversation` 跟踪正在回收的执行器任务，`close()` 等待现役与退役任务全部结束、取消残余并消费异常；`actor_for()` 在旧执行器尚未关闭时拒绝同一会话重建，并只淘汰真正空闲的执行器 | `test_timeout_race_does_not_leave_queued_work`、`test_close_waits_for_retired_actor_before_database_shutdown`、`test_closing_actor_rejects_same_conversation_until_native_runtime_closes` |
| `server/app/db.py` | 明确拒绝 `synchronous=OFF`，只允许 FULL/NORMAL | `test_config.py` 新增用例 |
| `server/app/config.py` | `AGENT_SQLITE_SYNCHRONOUS` 去空格转大写并校验只允许 FULL/NORMAL；FULL 为部署/验收默认，NORMAL 仅诊断 | 同上 |
| `scripts/trial_capacity.py` | 读取实际 `PRAGMA synchronous` 与 `journal_mode`，与请求值核对；关停过程显式记录 `attempted/ok/error/active_drained/residual_actors/residual_retiring/server_task_done`，不再吞掉 stop 异常；只有 FULL（实际 pragma=2）且关停干净才 `passed`，NORMAL 仅 `diagnostic_pass`；各阶段接受/拒绝/意外明细在断言前落盘；CLI 新增 `--sqlite-synchronous {FULL,NORMAL}` | `test_trial_capacity_report.py` 3 项 |
| `scripts/trial_smoke.py` | 每次传输尝试记录 `response_started`、已收字节数、事件数、异常类型；只有“可证实的响应前失败”才归类为 `stale_connection_pre_response` 并从重连计数中扣除，其余记 `unknown_transport_retry`，原始失败率保留 | `test_concurrency_load.py` 2 项 |
| `scripts/concurrency_load.py` | 汇总 `stale_connection_retries`、`unknown_transport_retries`、`transport_failures`、`transport_attempts`；Markdown 说明只有可证实响应前失败才扣除，未知失败仍计入重连 | 同上 |
| `server/tests/test_trial_capacity_slots.py` | 执行器生命周期、同会话回收期拒绝、超时竞态、关闭等待 5 项回归 | 5 passed |
| `server/tests/test_trial_capacity_report.py` | 状态分类矩阵（pragma 缺失=failed、NORMAL=diagnostic_pass）、OFF 拒绝与大小写归一、断言前记录明细 | 3 passed |
| `server/tests/test_config.py` | FULL 默认、`normal` 可接受、OFF 拒绝 | 1 项新增 |
| `server/tests/test_capabilities_04.py` | 曾临时把并行新增的 `file_read/file_write/file_search` 加入断言；收到协调者“不改他人断言”的要求后，已撤回该临时修改，保留共享工作树的真实失败 | 见 4.8 集群 C |

未修改：`server/app/workspace_tools.py`、`app/capabilities.py`、`scripts/build_release.py`、`trial_release_*` 及灰色发布负责人的专属文件。

## 4. 可复现命令与结果

以下命令均在 `/media/data3/dengkw/projects/open-claude-code` 或其中 `server/` 目录执行；`TMPDIR=/media/data3/dengkw/projects/open-claude-code/.runtime/tmp`。

### 4.1 语法编译

```bash
.venv/bin/python3 -m py_compile server/app/runtime.py server/app/db.py server/app/config.py server/app/main.py \
  scripts/trial_smoke.py scripts/concurrency_load.py scripts/trial_capacity.py
```

结果：`py_compile ok`（退出码 0）。

### 4.2 定向单元测试

```bash
cd server
TMPDIR=/media/data3/dengkw/projects/open-claude-code/.runtime/tmp \
/abs/path/open-claude-code/.venv/bin/python3 -m pytest -q \
  tests/test_trial_capacity_slots.py tests/test_trial_capacity_report.py \
  tests/test_concurrency_load.py tests/test_config.py tests/test_trial_sse_tail.py
```

结果：**59 passed in 22.80s**，退出码 0。原始日志：`.runtime/evidence/pytest_targeted_20261112.txt`。

### 4.3 16 项垂直烟测（合成）

```bash
.venv/bin/python3 scripts/trial_smoke.py --report .runtime/evidence/trial_smoke_full_v2.json --timeout 10
```

结果：`status=passed`，16 项检查全部 `passed`：`health_live`、`health_ready`、`login_admin`、`model_catalog`、`create_knowledge_base`、`upload_synthetic_document`、`retrieval_bm25`、`create_assistant`、`upload_attachment`、`create_conversation`、`chat_with_attachment`、`sse_stream`、`cancel_run`、`usage_endpoint`、`member_isolation`、`logout_admin`。原始 JSON：`.runtime/evidence/trial_smoke_full_v2.json`。

### 4.4 默认 4 槽容量门禁（FULL）

```bash
.venv/bin/python3 scripts/trial_capacity.py --json-out .runtime/evidence/trial_capacity_full_v2.json
```

结果：`status=passed`，摘要：`accepted=8, rejected=25, unexpected=0, peak_simultaneous_executions=4, synchronous_pragma=2, shutdown_ok=true`。分阶段实测：

- `burst_8`：4 接受 / 4 精确拒绝，峰值 4，耗时 3325.067ms；
- `cancel_and_recover`：取消释放槽位并可复用，`passed`；
- `burst_25`：4 接受 / 21 精确拒绝，峰值 4，耗时 7723.68ms；
- `recovery_after_release`：释放后重新执行 4 个，`passed`；
- 关停记录：`attempted=true, ok=true, error=null, active_drained=true, residual_actors=0, residual_retiring=0, server_task_done=true`；
- 环境实测：`synchronous_pragma=2`、`journal_mode=wal`。

原始 JSON：`.runtime/evidence/trial_capacity_full_v2.json`。

### 4.5 旧的 1/5/10/25 完整矩阵（保留失败，不覆盖）

```bash
.venv/bin/python3 scripts/concurrency_load.py --levels 1,5,10,25 --iterations 2 \
  --scenarios chat,sse,retrieval,attachment,cancel \
  --json-out .runtime/evidence/trial_concurrency_full.json \
  --markdown-out .runtime/evidence/trial_concurrency_full.md
```

结果：`status=failed`，410 请求，408 成功，2 个 `ReadError`，均出现在聊天 25 并发；该档位 p50=11530.661ms、p95=22536.259ms、max=25768.834ms；取消场景 82 次取消为预期；`stale_connection_retries=1`。失败与当时环境噪声重叠（同段观察负载约 8.6，存在 apt/依赖安装活动）。按协调者要求保留原始失败，未增加静默 POST 重试来抹平。

### 4.6 短矩阵噪声版（保留，不覆盖）

```bash
.venv/bin/python3 scripts/concurrency_load.py --levels 1,5 --iterations 2 \
  --json-out .runtime/evidence/trial_concurrency_short.json \
  --markdown-out .runtime/evidence/trial_concurrency_short.md
```

结果：`status=failed`，60 请求，53 成功，7 个错误全部集中在聊天 5 并发：5 个 `TIMEOUT`、1 个 `ReadError`、1 个 `RemoteProtocolError`；聊天 5 并发 p50=39144.807ms、p95=42041.954ms；聊天 1 并发 p50=8389.405ms。运行窗口与灰色发布安装完全重叠（本地 23:04:38 启动，负载 4.37→10.08）。该结果仅作为环境噪声归属证据，不作为正式数字。

### 4.7 短矩阵干净版（正式数字）

```bash
TMPDIR=/media/data3/dengkw/projects/open-claude-code/.runtime/tmp \
/abs/path/open-claude-code/.venv/bin/python3 scripts/concurrency_load.py --levels 1,5 --iterations 2 \
  --json-out .runtime/evidence/trial_concurrency_short_clean.json \
  --markdown-out .runtime/evidence/trial_concurrency_short_clean.md
```

结果：`status=passed`，60 请求，**0 错误、0 超时、12 次预期取消**，耗时 18 秒；运行起止负载 4.67→4.87，发布安装已结束，无其他 Agent 的 pytest 在跑。分档实测：

| 场景 | 并发 | 请求 | 成功 | p50 ms | p95 ms | 最大 ms | 错误 | 超时 | 取消 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 聊天 | 1 | 2 | 2 | 540.587 | 636.358 | 646.999 | 0 | 0 | 0 |
| 聊天 | 5 | 10 | 10 | 2993.221 | 3422.761 | 3424.91 | 0 | 0 | 0 |
| SSE | 1 | 2 | 2 | 840.913 | 1007.736 | 1026.272 | 0 | 0 | 0 |
| SSE | 5 | 10 | 10 | 1216.075 | 1630.598 | 1632.852 | 0 | 0 | 0 |
| 知识检索 | 1 | 2 | 2 | 12.262 | 14.231 | 14.45 | 0 | 0 | 0 |
| 知识检索 | 5 | 10 | 10 | 54.534 | 72.505 | 76.237 | 0 | 0 | 0 |
| 附件 | 1 | 2 | 2 | 41.446 | 59.232 | 61.208 | 0 | 0 | 0 |
| 附件 | 5 | 10 | 10 | 107.864 | 142.096 | 149.97 | 0 | 0 | 0 |
| 取消 | 1 | 2 | 2 | 42.514 | 50.769 | 51.686 | 0 | 0 | 2 |
| 取消 | 5 | 10 | 10 | 154.914 | 331.693 | 338.963 | 0 | 0 | 10 |

原始 JSON：`.runtime/evidence/trial_concurrency_short_clean.json`；Markdown：`.runtime/evidence/trial_concurrency_short_clean.md`。注意：短矩阵通过不等于全量回归通过，见 4.8。

### 4.8 全量回归与失败聚类

首个失败（`-x`）：

```bash
cd server
../.venv/bin/python3 -m pytest -x -q --tb=short
```

结果：`1 failed, 28 passed, 1 warning in 26.68s`；首个失败为 `tests/test_capabilities_04.py::test_workspace_tools_keep_submitted_scope_and_refresh_user_before_read`。原始日志：`.runtime/evidence/pytest_full_20261112.txt`。

完整汇总（不带 `-x`）：

```bash
cd server
../.venv/bin/python3 -m pytest -q --tb=short
```

结果：**57 failed, 1413 passed, 17 skipped, 1 warning in 1226.60s（20:26）**。原始日志：`.runtime/evidence/pytest_full_20261112_complete.txt`。

按完整 traceback 聚类（57 个失败）：

| 集群 | 数量 | 精确名称 | 根因与验证 | 是否阻塞试用启用集合 |
|---|---:|---|---|---|
| A. HippoRAG 执行器测试 | 46 | `tests/test_hipporag_process.py`（全部 46 项失败） | 全量回归通过相对路径 `../.venv/bin/python3` 启动，`sys.executable` 含 `..`；`app/hipporag_process.py:_absolute()` 明确拒绝含 `..` 的路径，构造函数统一抛 `HippoRAGError("invalid_input")`。用绝对解释器路径重跑该文件：**55 passed in 15.54s**（`.runtime/evidence/pytest_hipporag_abs_python.txt`），说明不是缺 HippoRAG 外部资源、也不是产品逻辑失败，而是启动路径与 fail-closed 校验的组合问题；未修改该文件 | 否（不在烟测/容量/短矩阵路径） |
| B. 模型预检/推荐 | 5 | `tests/test_model_capabilities.py::test_provider_preflight_is_authenticated_and_fail_closed`、`::test_default_loopback_endpoint_is_not_provider_authorization`；`tests/test_model_recommendation.py::test_completion_reports_missing_provider_authorization_without_calling_other_model`、`::test_provider_router_and_tab_completion_use_only_authorized_target_with_local_fake_gateway`、`::test_router_status_distinguishes_disabled_from_missing_provider` | 本会话 shell 存在 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL` 等变量（只核对变量名，从未读取/打印值），被应用视为“已配置供应商”，导致期望 `credentials_missing` 的用例失败。清除这些变量后重跑两个文件：**19 passed in 35.34s**（`.runtime/evidence/pytest_model_env_clean.txt`）。属于测试环境隔离问题，不是产品代码失败 | 否 |
| C. 共享断言不匹配 | 1 | `tests/test_capabilities_04.py::test_workspace_tools_keep_submitted_scope_and_refresh_user_before_read` | 并行新增的 `workspace_tools` 注册了 `file_read/file_write/file_search`，旧断言集未包含；属共享工作树交叉变更。协调者明确要求不修改该断言或 `workspace_tools`，本 dispatch 已撤回自己的临时补充并保留失败 | 否（不影响运行时行为；但阻塞全量全绿） |
| D. 文件工具功能 | 4 | `tests/test_file_tools.py::test_file_write_read_search_and_revision_conflict`、`::test_paths_are_confined_to_the_conversation_workbench`、`::test_untracked_and_denied_writes_do_not_create_files`、`::test_file_tools_reject_binary_and_oversized_content` | 真实失败：测试助手在 `json.loads(result["content"][0]["text"])` 处抛 `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`，即新文件工具返回的文本不是测试期望的 JSON。属并行文件工具特性的未完成/契约不一致，不在本 dispatch 写入范围 | 否（不在试用五链路烟测与容量路径；阻塞全量全绿） |
| E. 子代理测试导入缺陷 | 1 | `tests/test_subagent.py::test_synthetic_children_are_isolated_and_replayable` | `NameError: name 'pytest' is not defined`，测试文件缺少 `import pytest`；属共享测试代码缺陷，非本 dispatch 文件 | 否（阻塞全量全绿） |

补充：完整回归日志中 `No space left on device` / `Errno 28` 出现次数为 **0**；根分区写满虽发生在回归期间，但没有形成 ENOSPC 失败集群。A 集群的 46 项失败在修复启动方式后全部通过，B 集群 5 项在清理环境变量后全部通过，均有独立重跑记录。

## 5. 关键修复的验证细节

1. **超时与入队竞态**：Python 3.10 的 `asyncio.wait_for(queue.get(), idle)` 在计时器与入队同轮触发时可能抛出 `TimeoutError`，原实现直接退出循环，已入队任务永久悬挂。现实现检测 `pending` 或队列非空即继续消费；受控屏障测试 `test_timeout_race_does_not_leave_queued_work` 断言任务被执行且队列清空。
2. **执行器退役与关闭**：`RunManager` 记录 `retiring` 任务并在 `close()` 中与现役执行器一起等待；同一会话在旧执行器关闭前再次提交返回 `EXECUTION_CAPACITY`，避免新旧原生运行时重叠；关闭测试用一个阻塞在 `Event` 上的慢速运行时证明 `close()` 确实等待数据库关闭前完成退役。
3. **容量报告可信性**：`_classify_status` 只有在运行成功、关停干净且实际 `PRAGMA synchronous==2` 时才返回 `passed`；NORMAL 返回 `diagnostic_pass`；pragma 读取失败返回 `failed`。各阶段在断言前把接受/拒绝/意外明细写入报告，断言失败也不会丢证据。
4. **SSE 传输重试统计**：只有 `response_started=False` 且本次尝试收到 0 个事件时才归类 `stale_connection_pre_response`；响应已开始或已收到事件的失败一律 `unknown_transport_retry`，仍计入重连次数，原始失败数不因归类而减少。

## 6. FULL 持久化代价与事件循环阻塞诊断（下一阶段依据）

诊断脚本（仅合成、无模型）：`.runtime/tmp/probe_prepare_run_loop.py`、`.runtime/tmp/probe_message_send_block.py`；原始输出已落盘。

- **单次 `prepare_run` 直接跑在事件循环上**：10 次调用，p50=234.72ms、max=310.49ms；同期事件循环延迟 p50=758.99ms、max=761.94ms，采样器只拿到 3 个样本（被同步 FULL 提交阻塞）。
- **同样 10 次改为 `asyncio.to_thread`**：每次耗时 p50=220.18ms，事件循环延迟 p50=0.22ms、max=0.97ms，采样器拿到 449 个样本。证明阻塞来自同步 FULL 提交本身，而不是 SQLite 总吞吐天然不可用。
- **25 路并发真实 HTTP 提交**：全部返回 202；提交延迟 p50=8910.30ms、p95=10468.11ms；事件循环延迟 p50=279.88ms、p95=1975.27ms、max=2120.13ms；10.53 秒内只采到 20 个样本。与旧的聊天 25 并发 p50=11.5s、p95=22.5s、2 个传输错误一致。
- **一次性实验（已回滚，不作为交付）**：只把 `message_send` 里的 `prepare_run` 放到线程池后，25 个执行器同时启动，随后每个事件的同步 FULL `emit` 继续阻塞事件循环，事件循环延迟最高约 20 秒，25 个提交全部 `ReadTimeout`。因此正确方向不是只改 `prepare_run`，而是同时处理事件写入路径（或引入专用写库线程/批量提交方案），并保持 FULL 持久化、不放松超时、不关闭持久化。该方案需在下一阶段先做前后事件循环延迟对照，再决定最小改动。

结论：机器数据盘的 FULL fsync 成本约每次 0.2–0.3 秒；4 槽容量边界本身正确，但把同步提交放在单事件循环上会在高并发时造成排队放大。这是下一阶段“减少事件循环阻塞、同时保持 FULL”的核心输入，不应通过降低持久性或加大超时来掩盖。

## 7. 证据边界与未覆盖项

- 本报告全部为合成资源、进程内回环 HTTP、注入确定性运行时；**没有**真实模型/真实供应商调用、真实客户数据、浏览器端到端或生产容量认证。模型相关测试是在清除本会话环境变量后运行的，未读取任何凭据值。
- 全量回归仍为 57 失败，**不宣称全绿**；按协调者要求 `outcome=failed`，直到共享工作树出现可信全绿证据。A/B 两集群已用重跑证明非产品逻辑失败；C/D/E 为共享工作树内其他改动的真实失败，需要对应负责人修复或后续干净树复测。
- 能力白名单（`TRIAL_CAPABILITY_POLICY.md`）与 30 分钟最终长测**不在本 dispatch 开工**，属于下一明确阶段。
- 根分区目前仅约 387MB 可用，虽然本会话 TMPDIR 在数据盘，但后续长时间测试仍有机器级空间风险；建议在下一阶段开始前由机器负责人确认根盘余量或迁移更多遗留文件。
- 共享工作树包含大量其他 Agent 的未提交改动；本报告的文件清单只覆盖本 dispatch 实际改动的片段。

## 8. 文件与产物清单

修改/新增（本 dispatch）：

- `server/app/runtime.py`
- `server/app/db.py`
- `server/app/config.py`
- `scripts/trial_capacity.py`
- `scripts/trial_smoke.py`
- `scripts/concurrency_load.py`
- `server/tests/test_trial_capacity_slots.py`
- `server/tests/test_trial_capacity_report.py`
- `server/tests/test_concurrency_load.py`
- `server/tests/test_config.py`
- `docs/evidence/TRIAL_REMEDIATION_2026-11-12.md`（本文件）
- `docs/evidence/TRIAL_REMEDIATION_2026-11-12.json`

原始证据：

- `.runtime/evidence/trial_smoke_full_v2.json`
- `.runtime/evidence/trial_capacity_full_v2.json`
- `.runtime/evidence/trial_concurrency_full.json` / `.md`（旧的 410 请求失败，保留）
- `.runtime/evidence/trial_concurrency_short.json` / `.md`（噪声版，保留）
- `.runtime/evidence/trial_concurrency_short_clean.json` / `.md`（干净正式版）
- `.runtime/evidence/pytest_full_20261112.txt`（首个失败）
- `.runtime/evidence/pytest_full_20261112_complete.txt`（完整回归）
- `.runtime/evidence/pytest_hipporag_abs_python.txt`（A 集群重跑）
- `.runtime/evidence/pytest_model_env_clean.txt`（B 集群重跑）
- `.runtime/evidence/pytest_targeted_20261112.txt`（定向测试）
- `.runtime/evidence/probe_prepare_run_loop.json`
- `.runtime/evidence/probe_message_send_block_full.json`
