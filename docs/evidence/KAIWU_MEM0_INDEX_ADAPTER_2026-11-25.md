# 2026-11-25｜Mem0 已批准记忆派生索引适配（草稿）

状态：**总控已独立验证候选适配器及真实 Mem0 2.2.1 SDK。** 本模块默认关闭，未接入正式产品调用路径、未安装到生产、未使用客户数据。下文 §1–9 保留工程师原交付说明与未运行测试清单，实际测试以本节和末尾总控复验为准。

## 总控验收（2026-11-26）

工程师实际于 11-25 22:46:04 启动，23:18:55 退出码 0；候选代码真实存在。初轮总控隔离组合回归 74/74 通过（controller-pytest.log），但官方 SDK get_all 默认只返回 20 条，无法覆盖宿主最多 100 份记忆的约束。

修复：Mem0SdkBackend 明确 top_k=201、show_expired=True，以覆盖当前和旧代次（各最多 100）并预留截断哨兵；达到 201 条则拒绝同步、不会在不完整目录上写入或删除。外层适配器也检查同一边界；防止列表重复返回同一 ID 时误删保留行。超过边界的异常积压仍需明确修复，而非自动删除权威事实。没有实现无限分页或放宽权限。新增两项测试，最终适配器 20/20 通过（controller-pagination.log）。

新增可复现真实 SDK 测试 scripts/oss/mem0_index_adapter_probe.py，复用已有隔离 pilot venv、回环模型/embedding 服务、真实本地 Qdrant 和网络限制，实际 5/5 通过（controller-real-sdk.log），退出码 0；100 份记忆枚举与二次同步幂等、真实元数据与正文回读、版本替换 100 新增/100 删除、相同向量下的跨用户隔离、空快照清理且保留其他用户均通过。206 次合成 embedding，0 次提取模型调用。spaCy/FastEmbed 可选扩展未安装，不代表真实语义检索质量。生产集成、正式授权/版本绑定、跨进程单写、真实 embedding/记忆质量和删除留存策略仍需验收。

证据目录：/media/data3/dengkw/acceptance/kaiwu-mem0-index-adapter-20261125/。


## 1. 交付范围

新增一个默认关闭、可注入 Mem0 `Memory` 接口的派生索引适配器，以及独立边界测试。没有修改产品主流程、正式记忆 API、权限、数据库迁移、部署或依赖清单。

| 文件 | 作用 |
|---|---|
| `server/app/mem0_index.py` | 派生索引核心、Mem0 SDK 薄包装、宿主快照读取器 |
| `server/tests/test_mem0_index_adapter.py` | 合成数据单元测试与真实宿主快照集成测试（未运行） |
| `docs/evidence/KAIWU_MEM0_INDEX_ADAPTER_2026-11-25.md` | 本证据草稿 |

`docs/architecture/OPEN_SOURCE_STACK.md` 的 Mem0 状态未改动。当前只能表述为“候选实现已完成代码与测试草稿”，不能写成“隔离通过”。

## 2. 权威边界

`NativeMemory`、不可变快照、SQLite 指针、`revision`、`digest`、`native_epoch`、审批和用户权限继续是唯一权威。Mem0 只保存派生文本，不保存正式事实，不参与用户编辑、清空、审批或版本推进。

适配器不解析审批记录或迁移来源；宿主必须只对“已批准并已提交”的活动快照调用 `sync`。若某快照来自 legacy 只读迁移且尚未完成产品口径确认，宿主不得把它交给派生索引。

- `TrustedScope` 只能由宿主从 `NativeMemory.snapshot_for_run(...)` 返回的绑定构造，包含 `user_id`、`conversation_id`、`native_epoch`、`revision`、`digest`、`snapshot_id`、`manifest_digest`。
- 模型可提供的唯一输入是检索词 `query`；`search` 没有 `user_id`、`conversation_id`、`native_epoch`、`revision`、`digest` 或原生 `filters` 参数。
- 传给 Mem0 的作用域固定为宿主绑定的 `user_id`、固定索引 `agent_id` 和 `run_id=conversation_id`；跨会话共享默认关闭。
- `generation = sha256(canonical(scope + schema))` 同时绑定用户、会话、原生代次、revision、digest 和快照身份。旧代次的行即使尚未清理，也不能通过检索复核。
- `NativeMemorySourceReader` 读取的是 SQLite 指针指向的已提交快照，不是目录中任意文件；读取前后调用 `snapshots.assert_binding(...)`，并用 `snapshots.fs.verify(...)` 校验清单、摘要、文件身份和权限。

## 3. 索引流程

### 3.1 `sync(scope)`

1. 读取权威快照；任何绑定/树变化都终止本轮。
2. 只取非空 Markdown 正文；空文件计入 `skipped_empty`，不发送给 Mem0。
3. 用宿主作用域调用 `get_all(filters={user_id, agent_id, run_id=conversation_id})`。
4. 逐行校验来源 ID、正文摘要、generation、用户、会话、native epoch、revision、digest 和来源路径。
5. 已有且完全匹配的行保留；重复行、旧代次行、来源已删除行、摘要变化行标记为待删除。
6. 缺失文档调用 `add(text, ..., metadata=..., infer=False)`。`infer=True` 在适配器和 SDK 包装层都被拒绝。
7. 删除待清理行；删除失败不发布成功状态，返回 `partial` 和 `cleanup_pending`。逐行清理使用 Mem0 既有 `delete(memory_id)`；`delete_all` 只用于权威快照已清空后的整域清理。
8. 再次读取权威快照；若来源在索引期间变化，尽力删除本轮新写入的行并抛出 `Mem0IndexSourceChanged`。

索引行元数据：

| 字段 | 含义 |
|---|---|
| `kaiwu_schema` | 当前为 `1` |
| `kaiwu_generation` | 宿主作用域与版本摘要 |
| `kaiwu_user_id` / `kaiwu_conversation_id` | 宿主可信身份 |
| `kaiwu_agent_id` / `kaiwu_run_id` | 固定索引命名空间与会话过滤值的冗余校验 |
| `kaiwu_native_epoch` / `kaiwu_revision` / `kaiwu_digest` | 原生代次与正式记忆版本 |
| `kaiwu_source_id` / `kaiwu_source_path` | 快照内相对 Markdown 路径 |
| `kaiwu_content_sha256` / `kaiwu_content_size` | 已批准正文摘要与字节数 |
| `kaiwu_kind` | 固定为 `approved_memory_text` |

第一版以“一个已提交 Markdown 文件正文”为一个索引文本片段，不做语义抽取、总结或事实推断，也不把 `infer=True` 的提取候选写入正式索引。适配器不按 token 裁剪命中正文；宿主必须按既有上下文预算决定注入哪些 `VerifiedHit`，或改用 `memory_read` 读取精确版本。后续若按段落切分，必须使用稳定来源 ID 和同一复核合同。

### 3.2 `search(query, scope)`

1. 读取权威快照 A。
2. 用宿主作用域调用 `search(query, filters=..., top_k=..., threshold=...)`。
3. 读取权威快照 B；A 与 B 的文档摘要必须一致，否则抛出 `Mem0IndexSourceChanged`，不返回候选。
4. 对每个候选复核元数据、generation、来源 ID、来源路径、正文摘要、字节数和正文完全相等。
5. 非法、过期、伪造、来源缺失或正文不一致的候选只丢弃该行并计入 `rejected`；按 `source_id` 去重后返回最多 `top_k` 条。
6. 后端检索失败抛出 `Mem0IndexUnavailable`，调用方必须回退现有权威记忆机制；不得把失败解释为“没有记忆”。
7. `SearchResult` 携带 `generation`、`fetched` 和 `rejected`。空 `hits` 只表示当前派生代次没有可复核候选，不是“用户没有记忆”的证明；宿主仍以权威记忆和 `memory_list/read` 为准。`rejected>0` 说明后端存在过期或非法行，应触发清理或降级观测。

### 3.3 `purge(scope)`

仅当当前权威快照为空时允许调用，使用 `delete_all(user_id=..., agent_id=..., run_id=conversation_id)` 清理该会话派生行。权威记忆非空时拒绝，避免误删仍有效的派生索引。`purge` 与用户编辑/同步的并发必须由宿主闸门串行化；适配器在 `delete_all` 后不再复核权威快照，若期间出现新内容，需要后续 `sync` 重建。`delete_all` 只证明记录从检索中消失，不代表日志、备份或底层存储已物理擦除。

## 4. 失败、版本与删除矩阵

| 场景 | 适配器行为 | 权威状态 |
|---|---|---|
| 默认关闭 | `sync/search/purge` 抛 `Mem0IndexDisabled`，不调用后端 | 不变 |
| `search` / `get_all` 失败 | 抛 `Mem0IndexUnavailable` | 不变；调用方回退 |
| `add` 普通异常或无确认行 | 返回 `partial`，记录 `add:*`，不声称已索引 | 不变 |
| 适配器级 `Mem0IndexUnavailable`（如响应格式无效） | 终止本轮并抛出 | 不变 |
| `delete` 失败 | 返回 `partial`，`cleanup_pending>0`；旧行仍被检索复核拒绝 | 不变 |
| revision/epoch/digest 变化 | 新 generation；旧行待清理，检索复核拒绝旧行 | 只由宿主提交改变 |
| 用户清空记忆 | 空快照同步删除旧行；`purge` 可清理 | 宿主已提交空快照 |
| 同一来源重复行 | 保留一行，其余删除 | 不变 |
| 来源在同步中变化 | 尽力删除本轮新行，抛 `Mem0IndexSourceChanged` | 不变 |
| 来源在检索中变化 | 抛 `Mem0IndexSourceChanged`，不返回候选 | 不变 |
| 伪造元数据/正文/来源 ID | 候选被拒绝 | 不变 |
| 进程崩溃 | 已写入行带 generation 标记；不会修改权威记忆 | 不变 |

## 5. 并发与一致性限制

- 适配器进程内用一把 `threading.RLock` 串行化 `sync/search/purge` 及对同一后端的调用；不提供跨进程分布式锁。一个 Mem0 客户端/后端实例应由一个适配器实例持有，避免多个锁对象并发操作同一客户端。
- 生产必须保证同一会话只有一个写入者：由宿主既有的运行/编辑闸门串行化 `sync`，或把适配器部署为单写独立服务。并发 `sync` 可能产生重复行；检索会按 `source_id` 去重并复核，后续 `sync` 清理重复行。
- 若 Mem0/向量后端写入后读不一致，下一次 `sync` 可能把已写入行当作缺失而重复添加；检索仍按 `source_id` 去重并复核，但行数会增加。生产需确认目标后端的读写一致性，或使用宿主侧 generation 目录/幂等键。
- 权威 SQLite 与 Mem0 不在同一事务中。Mem0 永远不是权威；检索失败、索引缺失或旧行残留都不能改变正式记忆或权限。
- 检索是“先读权威 A、查 Mem0、再读权威 B”的乐观复核。若 A/B 不一致，本次不返回候选；调用方应回退或重试。每次 `sync/search` 都会读取两次权威快照；在每会话 1 MiB 上限下可接受，生产仍需测量延迟和 IO。
- `get_all` 的分页/上限行为未在本轮用真实 SDK 验证。宿主当前最多 100 个记忆文件；若真实 SDK 默认返回上限更低，重复行可能累积，需要总分页或按 generation 维护清理目录。这是待验收问题。
- `sync` 的 `added` 计数基于 Mem0 `add` 返回的确认行；它不保证提供方已立即可检索，也不保证正文/元数据未被改写。最终进入上下文的每个候选都必须通过 `search` 的权威快照复核；若提供方改写或丢弃，会被拒绝并在后续 `sync` 重建。生产可增加写后读校验，但需要评估最终一致性延迟。
- Mem0 `add(infer=False)` 的 `metadata` 与正文 round-trip、以及 `search` 返回的 `metadata` 结构，必须用真实 2.2.1 SDK 复核。若提供方不返回或改写元数据，本适配器会拒绝候选，属于 fail-closed。
- 本适配器逐行清理依赖 Mem0 既有 `delete(memory_id)`；`delete_all` 只用于权威快照清空后的整域清理。若未来独立服务只暴露 `delete_all`，需要新增“重建式清理”或显式提供逐行 `delete`，不能直接清空后再假定检索仍完整。
- `Mem0IndexConfig.agent_id` 是派生索引命名空间。修改它会切换到新的行命名空间；旧命名空间的行不会被新配置自动识别或删除，必须作为独立迁移/清理操作处理。
- 本模块的 128 KiB/文件、1 MiB/会话、100 文件上限与当前 `memory_snapshots` 宿主上限对齐；宿主调整这些上限时必须同步调整适配器常量和测试，否则会 fail-closed。
- `delete_all` 只保证相应记录不再被检索，不保证物理删除；保留、备份、租户隔离和出站控制仍需按 `OPEN_SOURCE_STACK.md` §5.4 单独验收。
- 检索词会发送给宿主配置的 embedding 供应方；生产必须使用经批准的内网/租户线路，并确认检索词本身不包含不应外发的客户数据。适配器不提供默认供应方，也不做查询脱敏。

## 6. 配置与启用

- `Mem0IndexConfig.enabled` 默认 `False`；`Mem0DerivedIndex(...)` 在未启用时可以没有后端和读取器，启用时必须显式注入两者。
- 本模块没有被 `service.py`、`main.py`、`runtime.py` 或任何产品路由导入，因此默认不会运行，也不改变现有功能。
- `load_mem0_backend(config)` 只在显式调用时导入 Mem0：
  - 要求显式提供 `vector_store`、`embedder`、`llm`，不提供默认供应方；
  - 设置 `MEM0_TELEMETRY=false`；
  - 要求 `mem0ai==2.2.1`；
  - 导入后再次确认 `mem0.memory.telemetry.MEM0_TELEMETRY is False`，否则拒绝。
- 该函数自身不发起网络请求；构造 SDK 可能初始化宿主配置的本地向量库和供应商客户端。生产必须把模型/embedding 线路限制在经批准的内网或租户线路。
- 将来独立服务方式：复用同一 `Mem0Backend` / `SourceReader` 协议，由独立进程持有 Mem0 客户端；主应用只通过宿主绑定的作用域调用，不把 Mem0 安装进主服务进程。

参考接线（未写入产品主流程，仅说明依赖注入形状）：

```python
reader = NativeMemorySourceReader(service.memory)
backend = load_mem0_backend(explicit_self_hosted_config)  # 仅显式启用时
adapter = Mem0DerivedIndex(Mem0IndexConfig(enabled=True), backend, reader)
scope = TrustedScope.from_binding(
    service.memory.snapshot_for_run(user_id, conversation_id)
)
adapter.sync(scope)
result = adapter.search("model supplied query", scope=scope)
```

`scope` 必须由宿主在已授权、已提交的运行/管理上下文中生成；不能把 `user_id`、`conversation_id` 或过滤器作为模型工具参数传入。

## 7. 测试清单（全部未运行）

命令（在 `server/` 目录执行）：

```bash
cd server
python -m pytest tests/test_mem0_index_adapter.py -q
```

回归组合：

```bash
cd server
python -m pytest tests/test_harness_memory_storage.py tests/test_mem0_index_adapter.py -q
```

所需配置：

- `server/pyproject.toml` 中的测试依赖（`pytest==8.4.2`）及服务端现有依赖；单元测试不需要安装 Mem0。
- 测试使用 `tmp_path` 合成数据；如运行器需要指定临时目录，使用 data3 项目目录下的可写路径。
- 原生快照集成用例在 fixture 内设置 `AGENT_KNOWLEDGE_EMBEDDING_MODEL=local-lexical-v1`，不使用模型或外网。
- 真实 Mem0 SDK 不在本测试文件中运行；既有 `scripts/oss/mem0_probe.py` 使用独立 pilot venv、回环合成 provider，需由总控另行执行并记录。

| 测试 | 验证点 | 状态 |
|---|---|---|
| `test_default_is_disabled_and_enabled_requires_injected_dependencies` | 默认关闭、启用必须注入依赖 | 未运行 |
| `test_sync_indexes_approved_text_with_infer_false_and_host_scope_metadata` | `infer=False`、来源/版本元数据、幂等 | 未运行 |
| `test_empty_files_are_not_sent_to_mem0_but_still_part_of_the_source_snapshot` | 空文件不发送、快照仍完整 | 未运行 |
| `test_search_uses_only_host_scope_filters_and_returns_verified_hits` | 宿主作用域过滤、复核命中 | 未运行 |
| `test_search_rejects_stale_forged_missing_and_mismatched_candidates` | 过期/伪造/来源缺失/摘要不符拒绝 | 未运行 |
| `test_new_generation_replaces_old_rows_and_old_scope_cannot_surface_old_text` | 版本变化、旧代次不可见 | 未运行 |
| `test_clear_deletes_generation_rows_and_purge_requires_empty_authority` | 清空、`delete_all` 仅限空权威 | 未运行 |
| `test_duplicate_rows_are_collapsed_and_retry_is_idempotent` | 重复行合并、重试幂等 | 未运行 |
| `test_backend_failures_raise_instead_of_looking_like_empty_memory` | 检索/枚举失败不伪装为空 | 未运行 |
| `test_delete_failure_is_reported_and_stale_rows_never_enter_context` | 删除失败、旧行拒绝、后续清理 | 未运行 |
| `test_source_change_during_sync_removes_rows_written_for_the_old_snapshot` | 同步期间来源变化、回滚新行 | 未运行 |
| `test_source_change_during_search_returns_no_candidates` | 检索期间来源变化 | 未运行 |
| `test_scope_requires_host_binding_fields_and_rejects_model_identity` | 绑定字段/模型身份拒绝 | 未运行 |
| `test_query_and_source_limits_fail_closed` | 查询、路径、单文件、文件数、总量边界 | 未运行 |
| `test_sdk_wrapper_matches_probe_contract_and_refuses_infer_true` | SDK 调用形状、拒绝 `infer=True` | 未运行 |
| `test_sdk_loader_requires_explicit_providers_before_any_import` | 禁止默认供应方 | 未运行 |
| `test_native_reader_reads_verified_snapshot_and_detects_version_change` | 真实宿主快照读取与版本变化 | 未运行 |
| `test_end_to_end_fake_mem0_over_real_native_snapshot` | 真实快照 + fake Mem0 全链路 | 未运行 |

## 8. 待验收问题

1. **同步触发点**：应在宿主提交事务成功后同步调用 `sync`，还是由独立服务异步消费已提交事件？当前未接线。
2. **宿主读取接口**：`NativeMemorySourceReader` 使用 `snapshots.fs.verify` 读取同一已验证树；是否应改为宿主新增一个批量只读接口，避免适配器依赖内部快照对象？
3. **真实 SDK 契约**：`mem0ai==2.2.1` 的 `add(..., metadata=..., infer=False)` 是否原样保存正文并回显 `id`；`get_all(filters=...)` 的分页/默认上限是多少。
4. **失败语义**：检索失败是静默回退、记录降级指标，还是向用户显示“语义索引暂不可用”？需要宿主统一错误码。
5. **并发模型**：同会话单写闸门放在宿主、独立服务还是数据库；跨进程重复 `sync` 的清理策略。
6. **清理策略**：旧 generation 行在 `sync` 之外的保留时间和自动清理授权；`purge` 是否只允许管理员/用户显式操作。
7. **索引粒度**：第一版按 Markdown 文件正文索引；是否需要按段落/事实片段切分，以及切分后的稳定来源 ID 规则。
8. **空文件/纯空白文件**：当前跳过空字符串；纯空白是否也应跳过，需要宿主产品口径。
9. **质量评估**：按 `OPEN_SOURCE_STACK.md` §5.4，仍需真实授权中文任务集、公开 benchmark、现有 NativeMemory 对照，不能以本单元测试代替。
10. **生产启用**：版本/传递依赖审计、独立存储、出站控制、备份/删除、限流、开关、回退和真实 SDK 隔离验收全部完成前，保持 `enabled=False`。

## 9. 未完成与明确边界

- 未执行任何测试；未证明测试通过。
- 未安装 Mem0，未调用真实 SDK，未连接 Qdrant 或任何模型/embedding 供应方。
- 未修改正式记忆 API、数据库 schema、审批流程、权限、运行器、前端或部署。
- 未接入主流程；适配器当前只能由后续宿主代码显式构造。
- 未做真实记忆质量、性能、成本、召回率或过期事实误用评估。
- 未声称物理删除、租户隔离或生产并发已经验收。
