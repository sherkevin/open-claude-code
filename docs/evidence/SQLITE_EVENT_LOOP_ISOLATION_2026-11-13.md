# SQLite 事件循环隔离（synchronous=FULL 持久化不再阻塞 asyncio）

日期：2026-11-13（第 2 轮返修，对应独立复核报告 `oc_t19_independent_review_20261113.md` 的 F1–F5）
范围：学校云开发工作树 `server/app/*` 与相关测试。
不部署、不重启服务、不使用真实客户数据或付费模型；`PRAGMA synchronous=FULL` 保持不变。

> 表述更正：第 1 轮报告中的“全部异步数据库访问已迁移到事件循环之外”属于过度表述，
> 现予撤回。本轮只保证下文列出的路径已迁移；`knowledge.py`、`workflows.py`、
> `subagents.py` 的 `run_plan/_run_child/_await_child_approval`、`harness_memory.py`、
> `harness_tools.py`、`feishu_h5.py` 等异步函数仍在事件循环线程直接调用同步数据库，
> 已如实登记为后续风险（见 §6）。

## 1. 背景与问题证据

数据库以 `PRAGMA synchronous=FULL` 运行（WAL 模式、单连接、`BEGIN IMMEDIATE`）。在
学校云磁盘上，一次提交的 fsync 约 76–80 ms。此前这些数据库调用直接发生在 `async def`
函数里，因此每次提交都会把整个 asyncio 事件循环卡住一个 fsync 的时长。

已有探针证据（`.runtime/evidence/`）：

| 探针 | 场景 | 结果 |
| --- | --- | --- |
| `probe_prepare_run_loop.json` | 直接事件循环 vs 线程执行 prepare_run | loop lag p50 **758.99 ms** vs thread **0.22 ms** |
| `probe_message_send_block_full.json` | 25 路并发 message_send | submit p50 **8.91 s**，loop lag p95 **1975 ms** |

约束：不改变 `synchronous=FULL` 的耐久性语义，不吞错误，不靠延长超时掩盖问题。

## 2. 方案：明确的 async 卸载助手

在 `server/app/db.py` 新增 `offload` / `offload_write` / `shielded`：

```python
async def offload(fn, *args):
    return await asyncio.to_thread(fn, *args)

async def offload_write(fn, *args):
    future = asyncio.get_running_loop().run_in_executor(None, fn, *args)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError as cancelled:
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise exc from cancelled
        raise

async def shielded(coro):        # prepare_run -> submit 的不可取消中断单元
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancelled:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            raise exc from cancelled
        raise
```

### 2.1 连接与锁语义（为什么不会死锁、不会乱序）

- 仍然使用**同一个** `sqlite3` 连接（`check_same_thread=False`，`isolation_level=None`）
  和**同一把** `threading.RLock`。`offload` 只把调用搬到工作线程，没有新增连接，
  没有改变事务边界，因此事务内读取可见性、`BEGIN IMMEDIATE` 的串行写、WAL 读一致性
  全部保持原样。
- 工作线程只做数据库调用，**从不 await 事件循环上的任何 Future**；`RLock` 的持有者
  不会反向等待事件循环，因此不存在“事件循环等工作线程、工作线程等事件循环”的死锁环。
- 顺序性：`offload_write` 在提交者协程内被 `await`，后续代码只在该写操作返回后才执行。
- 取消语义：`asyncio.shield` 保证等待方被取消时不会遗弃已经开始的写；`offload_write`
  先等工作线程结束，再把取消向外传播。如果工作线程在这期间抛错，**抛出的真实异常优先**，
  取消作为它的 `cause` 保留（第 2 轮修复 F4），不会静默吞掉失败的事务。

### 2.2 已覆盖的持久化路径（限定范围）

1. `message_send` 的完整 `prepare_run` 与入队（第 2 轮合并为不可取消中断的
   `prepare_and_submit_async`）。
2. `SessionActor.emit` 的持久化（`_emit` 整段事务在工作线程）。
3. `RunManager.finish` / `cancel`（`finish_async`、`mark_cancelling`、`_settle_run`）。
4. SSE 轮询中的 `public_events`、`require("runs")`、鉴权与终态判断。
5. `SDKRuntime.execute` 与 guard/permission/post 钩子、`harness_runtime`、`context.refresh`、
   `context.compact`、`feishu` 网关、`subagents.cancel_plan`、`model_approval.review`、
   `upload`、`decide`、`run_cancel`。

### 2.3 两轮并发/边界修正

第 1 轮：

- 先注册 `manager.approvals[approval_id]` 的 Future，再让审批行可见，避免双人审批在
  Future 注册前完成投票而丢失唤醒。
- 单审批终态在 HTTP 决策端点内以 `WHERE id=? AND status='pending'` 原子落库。
- `dual_denied` 时立即移除刚注册的 Future，避免泄漏。

第 2 轮（对应复核 F1–F5）：

- **F1（prepare 后、submit 前取消）**：新增 `shielded` 和 `Service.prepare_and_submit_async`，
  把“准备事务 + 入队”变成一个不可被取消打断的单元；`RunManager.ensure_submitted` 对
  `queued` 运行做幂等补投递，`SessionActor` 用 `queued_run_ids` + `current_run_id` 去重；
  事件循环在取到运行后若发现已是终态则直接跳过，重复投递不会二次执行。`feishu` 与
  `context.compact` 同样改用该单元（`compact` 还需先写入 `kind='compact'`）。
- **F2（审批超时与 HTTP 决策竞态）**：运行时改用 `asyncio.wait_for(asyncio.shield(future), ...)`，
  超时不再取消 Future；决策端点只在条件更新胜出后、在事件循环线程上检查 `future.done()`
  再 `set_result`。新增 `SessionActor.resolve_waited_approval`：以
  `UPDATE ... WHERE status='pending'` 的**实际胜者**为审计真相，数据库为 allowed 时事件也
  报 allow，为 denied 时报 deny；取消与策略撤回仍覆盖已落库的 allow（fail-closed）。
- **F3**：`high_risk_approval._wake_waiter` 不再在工作线程直接 `Future.set_result`；
  若当前线程不是 Future 所属事件循环，改用 `loop.call_soon_threadsafe`，并在回调内再次
  检查 `done()`。
- **F4**：`offload_write` 取消分支不再用 `suppress(BaseException)` 吞掉工作线程异常；
  真实异常优先抛出，取消作为 cause。
- **F5**：`dual_denied` 的“插入审批行 + 策略拒绝”放进同一个 `db.transaction()`，
  注释与实现一致。

## 3. 修改文件

| 文件 | 修改要点 |
| --- | --- |
| `server/app/db.py` | `offload` / `offload_write`（取消路径错误语义）/ `shielded` |
| `server/app/service.py` | `prepare_run_async`、`prepare_and_submit_async`、`public_events_async`、`require_async` |
| `server/app/main.py` | `message_send`、SSE `run_events`、`run_cancel`、`decide`（条件更新 + 循环线程唤醒）、`upload`、subagent 端点 |
| `server/app/runtime.py` | `SDKRuntime` DB 调用；`SessionActor.emit/_emit`、入队去重、终态跳过、`approve` + `resolve_waited_approval`；`RunManager.ensure_submitted/submit_async/finish_async/cancel`；`dual_denied` 单事务 |
| `server/app/context.py` | `refresh`、`compact`（`shielded` 单元 + `ensure_submitted`） |
| `server/app/feishu.py` | 入站消息改用 `prepare_and_submit_async`、取消、审批决策 |
| `server/app/harness_runtime.py` | `_execute_chat`、`_compact_usage`、`_execute_compact`、`context_usage` |
| `server/app/model_approval.py` | `review` 中的会话/策略/工具授权读取 |
| `server/app/subagents.py` | `cancel_plan` 的标记、汇总、事件写入 |
| `server/app/high_risk_approval.py` | `_wake_waiter` 线程安全唤醒 |
| `server/tests/test_event_loop_isolation.py` | 新增 F1、F4 回归（共 9 项） |
| `server/tests/test_approval_race.py` | 新增 F2、F3 确定性竞态测试（共 3 项） |

未修改：`DeploymentPolicy`、能力门禁、project-board 共享代码、数据库 schema 与
`synchronous` 设置。

## 4. 新增测试

### 4.1 `server/tests/test_event_loop_isolation.py`（9 项）

原有 5 项继续覆盖：慢库注入下心跳持续、所有被插桩的数据库调用都在非事件循环线程、
12 路并发无永久排队、取消/幂等/关闭顺序，以及“取消等待方不遗弃已开始的写”。

第 2 轮新增：

- `test_cancellation_between_prepare_and_submit_still_delivers_the_run`
  在 `prepare_run` 提交后、`ensure_submitted` 前取消调用方；断言取消照常传播，但运行
  仍被投递并完成，`active_run_id` 归 NULL，只有一条 `run.completed`（F1）。
- `test_retry_redelivers_an_orphaned_queued_run_exactly_once`
  直接构造“已提交、已占用 `active_run_id`、但从未入队”的运行；同一 `client_request_id`
  重试返回原运行并补投递；运行中的第二次重试与再次 `ensure_submitted` 均不重复执行，
  助手消息只出现一次（F1）。
- `test_offloaded_write_propagates_a_worker_failure`：正常路径不吞异常（F4）。
- `test_offloaded_write_cancel_path_surfaces_a_worker_failure`：取消等待方时工作线程
  随后抛错，调用方看到真实异常（F4）。

### 4.2 `server/tests/test_approval_race.py`（3 项）

- `test_timeout_wins_and_a_late_decision_is_a_conflict_not_a_500`
  审批超时先结算为 denied/timeout；迟到的 HTTP allow 得到 409 而不是 500；运行以
  “已拒绝”结束；`approval.resolved` 恰好一条且 decision/source 与数据库一致（F2）。
- `test_waiter_alignment_matches_the_durable_row`
  直接驱动 `resolve_waited_approval` 的三种状态：HTTP 先赢且行为 allowed → 等待方对齐为
  allow；超时先赢且行为 pending → denied/timeout；策略撤回覆盖已落库的 allow →
  denied/policy。每次只有一条与数据库一致的 `approval.resolved`（F2）。
- `test_dual_vote_wakes_the_waiter_on_the_event_loop_thread`
  在工作线程调用 `_wake_waiter`，断言 Future 的结果回调在事件循环线程执行（F3）。

## 5. 运行命令与结果

统一环境：

```bash
cd /media/data3/dengkw/projects/open-claude-code/server
export TMPDIR=/media/data3/dengkw/projects/open-claude-code/.runtime/tmp
export PYTHONDONTWRITEBYTECODE=1
export HF_HOME=$TMPDIR/hf
PY=/media/data3/dengkw/projects/open-claude-code/.venv/bin/python
```

| 命令 | 结果 | 原始日志 |
| --- | --- | --- |
| `$PY -m pytest tests/test_event_loop_isolation.py -q` | **9 passed** | `.runtime/evidence/oc_t20_event_loop_isolation_20261113.log` |
| `$PY -m pytest tests/test_approval_race.py -q` | **3 passed** | `.runtime/evidence/oc_t20_approval_race_20261113.log` |
| `$PY -m pytest tests/test_high_risk_approval.py tests/test_model_approval.py tests/test_api.py -q` | **25 passed** | `.runtime/evidence/oc_t20_approval_targeted_20261113.log` |
| `$PY -m pytest tests/test_runtime_selection.py tests/test_runtime_sources_04.py tests/test_trial_sse_tail.py tests/test_trial_capacity_slots.py tests/test_concurrency_load.py -q`（复核报告中的 37 项定向集） | **37 passed** | `.runtime/evidence/oc_t20_targeted_37_20261113.log` |
| `$PY -m pytest tests/test_feishu_gateway.py tests/test_feishu_h5.py tests/test_context.py tests/test_harness_compaction.py tests/test_harness_context.py -q` | **88 passed, 4 skipped** | `.runtime/evidence/oc_t20_feishu_context_20261113.log` |
| `$PY -m pytest tests/test_resources_04.py tests/test_memory.py tests/test_session_isolation_042.py tests/test_subagent.py -q` | **36 passed** | `.runtime/evidence/oc_t20_memory_session_20261113.log` |
| `$PY -m pytest tests/test_sdk_adapter.py tests/test_harness_tool_execution.py tests/test_identity_metadata.py -q` | **39 passed** | `.runtime/evidence/oc_t20_sdk_harness_20261113.log` |

第 1 轮日志（`oc_t18_*_20261113.log`）仍然有效，但新增的 5 个用例使
`test_event_loop_isolation.py` 的计数从 5 变为 9。

全量回归按要求**未执行**，交给代码冻结后的独立会话。

## 6. 剩余风险（如实登记，不在本轮清零）

1. **其他 async 函数仍直接调用同步数据库**（复核报告 F6）：
   - `knowledge.py`：`read_for_run` 等；
   - `workflows.py`：`debug_node`、`run_model`、`run_workflow`、`run_synthetic`、
     `decide_workflow_approval` 中的多处读写；
   - `subagents.py`：`run_plan`、`_run_child`、`_await_child_approval`；
   - `harness_memory.py`、`harness_tools.py` 的工具回调；
   - `feishu_h5.py` 的登录会话写入。

   这些路径仍可能在高并发下造成毫秒级到数百毫秒级的事件循环卡顿。建议后续按同一
   `offload` 模式增量处理，并把“async 函数内不得直接调用同步 `service.db`”写入评审清单。
2. **SDKRuntime 逐事件写的延迟未被实测**：代码已改为 `offload_write`，但隔离测试使用
   轻量 `ImmediateRuntime`，未对 `SDKRuntime` 的 `raw_events`、`session_id`、`cost`、
   `timing` 等写入做延迟度量。
3. **测试注入盲区**：`SlowDatabase` 只包装 `transaction/execute/executemany/one/all`，
   不包装 `service.db.conn`；直接使用底层连接的路径不会被检测。
4. **默认线程池容量**：`offload` 使用事件循环默认执行器（通常 `min(32, CPU+4)` 线程）。
   并发 fsync 写很多时写操作会在池内排队；事件循环保持响应，但单个写请求的完成延迟
   受池大小限制，后续应观察线程池队列深度。
5. **子代理审批链路**：本轮只卸载了 `subagents.cancel_plan` 的数据库操作；child 审批的
   完整链路仍需专项覆盖。
6. **启动阶段**：`Database` 初始化、迁移与 `manager.recover()` 仍在事件循环启动前同步执行，
   不影响服务中的请求延迟，但会拉长冷启动时间。
7. **审批双写路径**：HTTP 决策端点与运行时结算都会写审批终态；条件更新
   `WHERE status='pending'` 保证只有一个胜者，后续修改审批逻辑时必须保留该条件。
8. **未做真实磁盘压测**：本轮证据来自确定性慢库注入与现有定向套件；部署前应用同一组
   25 路并发探针复测 loop lag p95，确认从约 1975 ms 降到合理范围。
