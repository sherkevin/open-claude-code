# OpenClaw 兼容切片实现证据（OC-T12，2026-11-12）

## 1. 证据范围与边界

本记录对应任务 `OC-T12“OpenClaw 兼容入口与最小迁移适配”`，只证明在隔离工作树中完成了**第一阶段、合成夹具驱动的兼容切片**，不证明已经和客户真实 OpenClaw 实例完成版本级联调。

本版本是独立审核之后的返工版本，逐项对应 `/media/data3/dengkw/OC-T12-INDEPENDENT-REVIEW-2026-11-12.md` 的 F1–F5（以及 F6 的文档边界），不是原始切片的重复声明。

- 工作位置：学校云，隔离工作树 `.claude/worktrees/oc-t12-openclaw-fix`，分支 `oc-t12-openclaw-fix`，基线提交 `becf717 feat: add OpenClaw compatibility slice baseline`。
- 没有部署生产、没有重启共享服务、没有读取真实客户数据、没有使用凭据、没有调用付费模型。
- 所有测试使用合成租户、合成用户、合成执行器和合成 MCP 工具声明，不包含任何真实模型结果或客户样例。
- 没有修改 `/media/data3/dengkw/projects/project-board` 共享代码，也没有把任务板改为完成。
- 没有修改主工作树，只在上述隔离工作树内修改文件。
- 设计输入来自主工作区中未跟踪的 `docs/design/OPENCLAW_COMPATIBILITY_AND_MIGRATION.md` 与 `docs/evidence/OPENCLAW_COMPATIBILITY_2026-11-12.md`。为避免和主工作区未跟踪文件发生路径冲突，本工作树不复制这两个文件，改以本文件提供同主题实现证据。

## 2. 变更文件

基线切片提交了六个新文件；本次返工在其上修改四个代码/测试文件和一份合同文档，没有修改现有业务代码。

| 文件 | 当前行数 | 作用 |
| --- | --- | --- |
| `server/app/openclaw_compat.py` | 1884 | 独立、租户范围的 `/v1` 兼容 facade、认证、会话映射、幂等、取消/超时、实例租约恢复、审计、AgentHarnessV2 合同清单 |
| `server/app/openclaw_connector.py` | 832 | 标准 MCP 形状的合成工具安全边界：白名单、风险分类、审批、超时、取消、撤销、实例租约恢复、审计 |
| `server/tests/test_openclaw_compat.py` | 1015 | facade 合同测试 18 个（含 5 个返工回归） |
| `server/tests/test_openclaw_connector.py` | 629 | 连接器安全边界测试 15 个（含 3 个返工回归） |
| `docs/design/OPENCLAW_AGENT_HARNESS_V2_CONTRACT.md` | 151 | 锁定候选版本的 AgentHarnessV2 合同与当前不支持面 |
| `docs/evidence/OPENCLAW_COMPATIBILITY_IMPLEMENTATION_2026-11-12.md` | 本文件 | 实现与测试的真实证据 |

关键文件校验和（最终代码冻结后计算）：

```
06900f72cf3cc866da84a1f8b9688444  server/app/openclaw_compat.py
2ca6ff9a76b8d51ae96fd96faf8dcbcb  server/app/openclaw_connector.py
```

## 3. 实现内容

### 3.1 独立租户范围 `/v1` facade

`server/app/openclaw_compat.py` 提供独立 sidecar 应用工厂 `create_openclaw_sidecar_app(facade)`，路由为：

- `GET /v1/models`
- `GET /v1/models/{model_id}`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/runs/{run_id}/cancel`
- `POST /v1/responses/{response_id}/cancel`
- `POST /v1/embeddings`（固定返回 `501 EMBEDDINGS_UNSUPPORTED`，不伪造向量）

兼容版本常量：`COMPAT_VERSION = "openclaw-compat/2026-11-12.2"`。

该工厂**没有自动挂载到 `server/app/main.py`**，这是有意的集成边界：部署方必须显式创建并挂载 sidecar，默认不会改变现有员工网页入口和产品主线。

认证规则：

- 只接受 `Authorization: Bearer zxoc_...` 形式的租户访问令牌，数据库只保存 SHA-256 哈希，支持过期、撤销和停用租户。
- 员工网页 Cookie（`zhixing_session`）明确拒绝，返回 `401 EMPLOYEE_SESSION_NOT_ALLOWED`，不复用员工 Cookie 作为下游凭据。
- 任意 Bearer、OpenClaw 管理令牌、未知令牌统一返回 `401 AUTHENTICATION_FAILED`，不透传管理面令牌。
- 认证成功和失败都写入 `openclaw_audit`，审计不记录明文令牌。

### 3.2 Chat Completions 与 OpenResponses 合同

- Chat 非流式返回标准 `chat.completion` 信封；SSE 返回 `data:` 分块并以 `[DONE]` 结束。
- Responses 非流式返回 `response` 对象；SSE 使用 `event:` 加 `data:`，事件包括 `response.created`、输出项、`response.output_text.delta`、`response.output_text.done`、函数调用参数和 `response.completed`（用量在完成对象内）。
- 支持函数工具调用往返：助手返回 `tool_calls`，客户端用 `tool` 消息回传结果；Responses 支持 `function_call` 与 `function_call_output`。
- 只接受本会话此前真实签发过的工具调用编号，伪造的 `call_id` 返回 `400 TOOL_CALL_UNKNOWN`。
- `previous_response_id` 是**只做会话绑定的继续（binding-only）**：执行器只收到当前请求的消息，不回放历史消息；历史上下文应由后续原生 Harness 通过 `conversation_id` 读取。24 小时是**检索有效期**，超过后继续请求返回 `404 RESPONSE_NOT_FOUND`，但不会删除数据库中的会话、turn 或 response 行（见 3.6 与 5.7）。
- 模型白名单来自租户/令牌范围，支持模型别名和 `x-openclaw-agent-id` 助手映射，冲突返回 `400 MODEL_OVERRIDE_CONFLICT`。
- 请求校验错误返回 `400`（未知参数、图片/音频输入、重复工具、非法 `tool_choice` 等）；**超长文本返回 `413 REQUEST_TOO_LARGE`，超过 1 MiB 的请求体由现有中间件返回 `413 UPLOAD_TOO_LARGE`**。早期证据把这两类写成 `400`，本版本已按实际代码和测试更正。
- 上游执行器错误映射为稳定错误码，不泄露内部异常或凭据。

### 3.3 会话隔离映射

- 映射键：`(tenant_id, principal_id, agent_id, session_key)`。
- `principal_id` 使用稳定的 `tenant:<tenant_id>:user:<user_id>`，令牌轮换不会丢失既有会话。
- `sessionId` 由服务端签发，必须已存在且属于同一租户、同一主体、同一助手；不匹配返回 `404`。
- `user:` 命名空间由服务端保留。客户端显式传入以 `user:` 开头（大小写不敏感）的 `x-openclaw-session-key` 返回 `400 RESERVED_SESSION_KEY`；`user` 字段派生键固定为 `user:<sha256(user)[:40]>`，数据库不保存原始 `user` 值。因此同一租户另一个终端用户不能通过猜测或复用派生键命中他人会话。
- 显式 `x-openclaw-session-key` 创建的会话在首次携带 `user` 时写入该 `user` 的 SHA-256 摘要。之后通过显式 key、`session_id` 或 `previous_response_id` 继续时，如果携带不同的 `user`，返回 `409 SESSION_CONFLICT`。并发创建同一会话键时，写入失败的一方也会复核已有绑定的用户摘要。
- 精确边界：只有**携带了 `user`** 的继续请求才比对摘要。未携带 `user` 的请求按租户服务主体作用域处理，不改变也不比对绑定；显式会话 key 或 `previous_response_id` 本身是持有者能力凭据。本切片不把 `user` 当作独立的认证凭据，只阻止“同租户内用显式 key 命中他人派生会话”和“同一会话改用另一个 `user` 别名继续”。
- 同租户不同 `user` 别名在**都携带 `user`** 时通过上述绑定校验隔离；跨租户、跨主体、跨助手的读取、继续和取消仍然一律返回 `404`。早期证据中“同租户不同用户、不同租户、不同助手之间互相不可见”的笼统表述已废弃，以本节为准。
- 每个外部会话绑定一个真实产品 `conversations` 记录，标题为 `OpenClaw · <agent> · <哈希>`，便于后续原生 Harness 衔接。

### 3.4 MCP / Skill / 插件合成安全边界

`server/app/openclaw_connector.py` 只接受标准 MCP `tools/list` 形状的声明，并实现：

- 每个租户主体显式登记连接器和工具白名单；未登记返回 `404 TOOL_NOT_FOUND`。
- 风险分类按**固定顺序**执行，`readOnlyHint` 不能提前短路为低风险：
  1. `destructiveHint=true` 或名称命中破坏性、管理、执行、安装、配置、支付、撤销等高风险词元 → `high`，默认拒绝；
  2. 名称命中写类词元（write/create/update/upsert/insert/upload/send/post/patch/put/set/append/move/rename/submit/modify/edit/save/import/sync 等）→ `medium`，必须审批；
  3. 名称命中显式只读动词白名单（read/list/get/search/query/fetch/lookup/describe/inspect/view/show/find/count/status/health/ping/retrieve）且 `readOnlyHint=true` → `low`，自动放行；
  4. 命中只读动词但未声明 `readOnlyHint=true` → `medium`，必须审批；
  5. 其余未知名称，即使 `readOnlyHint=true` → `high`，默认拒绝。
- 名称匹配按 snake_case、kebab-case、camelCase 分词后做整词/连续词序列匹配，不做子串匹配，避免 `thread` 被误判为包含 `read`。
- 因此 `write_file`、`send_email`、`create_user`、`update_ticket` 即使带 `readOnlyHint=true` 也是 `medium` 审批；`mystery_readonly`、`thread_listener` 即使带 `readOnlyHint=true` 也是 `high` 拒绝；`delete_ticket` 优先按 `high` 拒绝。以上组合均有回归测试。
- 审批绑定工具名和参数摘要，单次使用，默认 5 分钟过期。
- 工具名不能跨连接器重复暴露，重复返回 `409 TOOL_NAME_CONFLICT`。
- 参数必须是 JSON 对象，大小、深度、字符串预算、必填字段、额外字段和基础类型都有上限；摘要使用排序后的规范 JSON。
- 执行超时返回 `504 TOOL_TIMEOUT`，取消返回 `499 TOOL_CANCELLED`，默认执行器不可用时返回 `503 TOOL_UNAVAILABLE`。
- 连接器和单个工具都可撤销；撤销后立即不可发现、不可调用。
- 审计只记录租户、主体、连接器、工具、动作、结果、参数摘要、输出摘要和长度，不记录原始参数或原始输出。
- 默认执行器为失败关闭的 `UnavailableToolExecutor`，不会意外连接真实 MCP 服务器。
- 本阶段**没有**真实 MCP 传输、网络访问、插件安装、自动更新或配置写入接口。
- MCP 服务器自报的 annotation 仍属于需要受信的输入；本切片只保证“即使 annotation 撒谎，写类和未知工具也不能自动放行”，不保证被攻陷的 MCP 服务器不提供恶意工具声明。

### 3.5 AgentHarnessV2 候选合同

`docs/design/OPENCLAW_AGENT_HARNESS_V2_CONTRACT.md` 和代码清单 `harness_contract_manifest()` 锁定：

- 合同版本：`zhixing-openclaw-agent-harness-v2/2026-11-12.2`
- 候选 OpenClaw 版本：`v2026.11.3`
- 锁定状态：`candidate_pending_customer_version_confirmation`
- `slice_scope`：`synthetic_first_slice_no_real_harness_executor_or_mcp_transport`

合同把“已实现”和“目标合同”分开：

- `supported` 只列本切片真实实现的能力：`turn.execute`、`turn.cancel`、`session.continue`、`tool.call`、`tool.cancel`、`approval.request`；`capability_scope` 逐项给出真实边界（例如 `session.continue = binding_only_no_history_replay`，`tool.call = synthetic_connector_boundary_no_real_mcp_transport`，`approval.request = synthetic_connector_local_approval_not_wired_to_facade_or_events`）。
- `target_only` 列目标合同但本切片未实现的能力：`context.summary`、`memory.read`、`memory.propose_write`、`approval.resume`，以及 `event.run.stage`、`event.tool`、`event.approval`、`event.message`、`event.usage`、`event.run.completed`。
- `turn_field_status` 逐项标注 `required_turn_fields` 的真实交付状态：`turn_id`、`session_id`、`agent_id`、`messages` 已交付执行器；`context_digest`、`tool_policy_digest`、`event_stream_id` 未交付，并通过 `turn_fields_not_yet_delivered` 明确列出。其中 `request_digest` 只是数据库幂等元数据，不是传给执行器的上下文摘要。
- `recovery_model`：`owner_instance_plus_lease_expiry_heartbeat`。

客户确认真实版本前，状态不得改为 `locked` 或 `verified`。本阶段没有产生任何真实模型结果。

### 3.6 多实例启动恢复：实例租约与所有权

返工前，任何 facade 或 `ConnectorRegistry` 构造都会把所有 `running` 行标记失败，会误伤另一个实例正在执行的 turn/call，并在审计中留下错误的 `interrupted` 事件。现在的行为：

- `openclaw_turns` 和 `openclaw_connector_calls` 都有 `owner_instance` 与 `lease_expires_at` 列；旧数据库通过 `PRAGMA table_info` + `ALTER TABLE` 自动补列并建立租约索引。
- 每个实例用 `pid:<进程号>:<随机编号>` 标识自己。turn/call 进入 `running` 时写入 `owner_instance` 和租约截止时间。
- 执行期间后台心跳按 `lease_seconds / 3` 的间隔续租，直到 turn/call 进入终态；终态清空 `lease_expires_at`。
- `recover_interrupted()` 只恢复 `status='running'` 且 `lease_expires_at` 为 `NULL`（旧数据）或已过期的行；更新语句带有状态和租约双重条件并用 `rowcount` 防并发重复恢复。
- 其他实例持有有效租约的 turn/call 不会被改写；第二个实例启动时，相同幂等键的重放返回 `409 REQUEST_IN_PROGRESS`，而不是 `503 INTERRUPTED`。
- 真正租约过期的 turn 恢复为 `failed` / `503 INTERRUPTED`，清空租约，并记录带 `owner_instance` 与 `lease_expired=true` 的 `turn.interrupted` 审计。
- 默认租约 120 秒，构造参数 `lease_seconds` 允许大于 0 且不超过 3600 秒。
- 这只解决“启动恢复误伤其他实例”。活动运行和取消信号仍在进程内存中，多 worker 的取消仍需要共享存储或消息总线（见限制列表）。

### 3.7 默认失败关闭

- `OpenClawFacade` 默认注入 `UnavailableExecutor`，未显式绑定经批准执行器时返回 `503 MODEL_UNAVAILABLE`。
- 连接器默认注入 `UnavailableToolExecutor`，返回 `503 TOOL_UNAVAILABLE`。
- 因此即使误挂载 sidecar，也不会自动调用付费模型或真实工具服务器。

## 4. 测试命令与真实结果

运行环境：Python 3.10 虚拟环境 `/media/data3/dengkw/projects/open-claude-code/.venv/bin/python`，工作目录 `server/`。为避开数据盘日志抖动，测试临时目录使用 `/dev/shm/oc-t12-fix`（tmpfs）。以下耗时是本次运行的实际观测值，会随机器负载变化；稳定可复核的是通过数量和测试名单。

### 4.1 定向编译与两个新增测试套件（最终代码）

```bash
cd /media/data3/dengkw/projects/open-claude-code/.claude/worktrees/oc-t12-openclaw-fix/server
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m py_compile \
  app/openclaw_compat.py app/openclaw_connector.py \
  tests/test_openclaw_compat.py tests/test_openclaw_connector.py
TMPDIR=/dev/shm/oc-t12-fix /media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  tests/test_openclaw_compat.py tests/test_openclaw_connector.py -q
```

真实结果：

```
33 passed, 1 warning in 4.22s
```

`py_compile` 退出码 0（本记录运行脚本打印 `COMPILE_OK` 作为标记）。唯一警告来自现有依赖 `fastapi/testclient.py` 的 Starlette 弃用提示，与本任务代码无关。

### 4.2 相关回归（最终代码）

```bash
cd /media/data3/dengkw/projects/open-claude-code/.claude/worktrees/oc-t12-openclaw-fix/server
TMPDIR=/dev/shm/oc-t12-fix /media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  tests/test_openai_compat.py tests/test_config.py \
  tests/test_session_isolation_042.py tests/test_api.py -q
```

真实结果：

```
33 passed, 1 warning in 15.06s
```

这些回归覆盖现有内部请求归一化、配置、会话隔离和公开 API，确认新增文件没有改变默认检索、现有 Harness 主线或其他产品功能的行为。

### 4.3 独立审核发现与返工回归映射

| 审核发现 | 返工内容 | 对应回归测试 |
| --- | --- | --- |
| F1 高：`readOnlyHint` 短路，写类/未知工具可自动放行 | 分类顺序改为“破坏性/高风险 → 写类 → 只读动词白名单 + hint → 只读动词无 hint → 未知拒绝”，分词匹配 | `test_read_only_hint_cannot_bypass_write_or_unknown_classification` |
| F2 中：`user:` 未保留，显式 key 可命中同租户他人会话 | 大小写不敏感保留 `user:` 命名空间；显式 key 会话写入 `user_digest` 并在继续时校验 | `test_user_namespace_is_reserved_and_sessions_are_user_bound` |
| F3 中：第二个实例启动恢复误伤活跃 turn/call，重放 503，假 `turn.interrupted` | turn/call 增加 `owner_instance` 与 `lease_expires_at`，心跳续租，只恢复 NULL/过期租约；活跃重放返回 409 | `test_second_facade_startup_does_not_recover_another_instances_live_turn`、`test_expired_lease_recovery_and_live_heartbeat`、`test_second_registry_startup_does_not_recover_another_instances_live_call`、`test_lease_expiry_and_heartbeat_drive_recovery` |
| F4 中：`supported` 混入未实现能力，`required_turn_fields` 未逐项标注交付状态 | 拆分 `supported` / `target_only` / `capability_scope` / `turn_field_status` / `turn_fields_not_yet_delivered` | `test_harness_manifest_separates_supported_from_target_only` |
| F5 中：证据把 413 写成 400，同租户用户隔离、写类/未知拒绝、24 小时保留期表述与实现不符 | 更正证据与合同；保留 `413 REQUEST_TOO_LARGE` / `413 UPLOAD_TOO_LARGE`；写明 24 小时只是检索 TTL | `test_request_size_retention_and_binding_only_continuation`、`test_validation_and_upstream_error_envelopes` |
| F6 低-中：`previous_response_id` 只做会话绑定 | 在合同和证据中明确写成 binding-only，补充执行器只收到当前消息的断言 | `test_request_size_retention_and_binding_only_continuation` |

### 4.4 测试覆盖矩阵

| 验收要求 | 对应测试 |
| --- | --- |
| 非流式、SSE、工具往返、会话继续、幂等、错误、取消、模型白名单 | `test_chat_non_stream_and_sse`、`test_responses_non_stream_previous_and_sse`、`test_tool_call_round_trip_chat_and_responses`、`test_idempotency_replay_conflict_and_stream_replay`、`test_validation_and_upstream_error_envelopes`、`test_timeout_and_cancellation_are_terminal`、`test_cancel_endpoints_and_terminal_states`、`test_model_whitelist_and_agent_mapping` |
| 独立租户 `/v1` 边界，不复用员工 Cookie 或管理令牌 | `test_auth_rejects_cookie_management_and_invalid_tokens` |
| `agentId/sessionKey/sessionId` 隔离映射、`user:` 保留、用户绑定 | `test_session_mapping_continuation_and_isolation`、`test_user_namespace_is_reserved_and_sessions_are_user_bound` |
| MCP/Skill/插件白名单、风险分类、审批、超时、取消、撤销、审计 | `test_openclaw_connector.py` 全部 15 个测试 |
| `readOnlyHint=true` 不能绕过写类审批和未知拒绝 | `test_read_only_hint_cannot_bypass_write_or_unknown_classification` |
| 多实例启动恢复不误伤活跃 turn/call；租约过期才恢复；心跳续租 | `test_second_facade_startup_does_not_recover_another_instances_live_turn`、`test_expired_lease_recovery_and_live_heartbeat`、`test_second_registry_startup_does_not_recover_another_instances_live_call`、`test_lease_expiry_and_heartbeat_drive_recovery` |
| AgentHarnessV2 合同边界、`supported`/`target_only` 分离、字段交付状态 | `test_harness_contract_and_default_executor_fail_closed`、`test_harness_manifest_separates_supported_from_target_only` |
| 413 错误码、24 小时检索 TTL、binding-only 继续 | `test_request_size_retention_and_binding_only_continuation`、`test_validation_and_upstream_error_envelopes` |
| 默认检索、现有 Harness 和其他功能不回退 | 4.2 回归套件 |
| 不存储提示词、原始用户键或明文凭据 | `test_audit_and_persistence_do_not_store_prompts_or_credentials` |
| 并发幂等请求返回执行中 | `test_idempotency_concurrent_request_is_reported_in_progress` |
| 工具参数、声明和输出边界 | `test_argument_and_declaration_bounds`、`test_output_bounds_and_executor_failures` |
| 恢复未完成调用 | `test_recovery_marks_running_calls_as_interrupted` |

## 5. 已知限制与剩余缺口

以下都是真实存在、尚未完成的能力，不得当作已完成：

1. SSE 是“执行完成后按事件回放”，不是逐 token 实时转发；`event.*` 目标合同能力全部未实现，低延迟流式需要原生执行器接入事件队列。
2. 活动运行和取消信号保存在进程内存中。实例租约只解决启动恢复误伤，不等于多 worker 取消已实现；多 worker 部署仍需要共享存储或消息总线。
3. 没有绑定真实模型执行器，也没有真实或受控 Harness 适配器联调；默认返回 `503 MODEL_UNAVAILABLE`。
4. 没有真实 MCP 传输、网络访问和客户工具服务器；连接器只验证合成安全边界。
5. 没有 Gateway WebSocket RPC、渠道、节点、设备、插件安装、配置写入、Skill 自动安装和 embeddings。
6. 客户 OpenClaw 版本、渠道清单、脱敏样例和基线对比仍未提供，合同状态只能是候选。
7. `store` 字段被接受，但 24 小时只是 `previous_response_id` 的检索 TTL，不是删除策略。`openclaw_turns`、`openclaw_responses`、SSE 事件、`openclaw_audit`、`openclaw_connector_audit` 和 `openclaw_connector_calls` 都没有清理任务，正文和审计会长期保留。
8. 没有租户级速率限制、并发限制或用量配额；令牌持有者可以持续写入会话、turn 和审计。
9. 已存在会话不会每轮复核助手是否已停用、删除或权限变更；会话创建时校验一次，后续继续直接使用既有绑定。
10. 超时和取消依赖执行器配合取消；非合作执行器吞掉取消时，`504`/`499` 仍可能被拖住。
11. MCP 服务器自报的 `readOnlyHint`/`destructiveHint` 是需要受信的输入；本切片保证写类和未知工具不能仅凭 hint 放行，但被攻陷或配置错误的 MCP 服务器仍可能提供恶意工具声明。
12. 合成审批允许申请者自己批准，没有独立审批人或审批策略；真实产品需要补充。
13. `parallel_tool_calls` 被接受，但执行器顺序执行；不支持真正的并行工具调用。
14. 会话创建在单进程下处理了唯一键竞争，但极端崩溃场景可能留下未绑定的产品会话；需要后续清理任务。
15. 请求体超过 1 MiB 时由现有中间件返回 `413 UPLOAD_TOO_LARGE`，不是 OpenClaw 风格错误码；如果客户依赖统一错误格式，需要后续适配。
16. `user` 不是独立认证凭据：未携带 `user` 的继续请求按租户服务主体作用域处理，不比对会话已有绑定；显式会话 key 和 `previous_response_id` 本身是持有者能力凭据。

## 6. 冲突与集成说明

- `server/app/main.py` 和 `docs/REQUIREMENTS_ALIGNMENT.md` 没有修改。原因是其他工作树（`oc-t2-auto-compaction-usage`、`oc-t9-workflow-artifacts`）正在修改这些文件，按任务要求优先只新增/修改 OpenClaw 适配专属文件并报告冲突。
- 新增 sidecar 工厂需要部署方显式挂载；本切片不改变现有应用启动路径，因此不会自动影响员工入口。
- 主工作区中 `docs/design/OPENCLAW_COMPATIBILITY_AND_MIGRATION.md` 和 `docs/evidence/OPENCLAW_COMPATIBILITY_2026-11-12.md` 目前是未跟踪文件；本工作树没有修改或复制它们，避免跨工作树路径冲突，并以本文件提供同主题实现证据。
- 任务板未修改，最终状态由父级/调度方确认。

## 7. 结论

本次返工已经修复独立审核的 F1–F5 阻断项，并按 F6 修正了文档边界：`readOnlyHint` 不能再让写类/未知工具自动放行；`user:` 成为服务端保留命名空间且显式会话按键/按编号继续时校验用户绑定；多实例启动恢复改为实例所有权加租约心跳，只恢复过期或缺失租约的 turn/call；HarnessV2 合同拆分 `supported` 与 `target_only` 并逐项标注 turn 字段交付状态；证据中的 400/413、同租户隔离、24 小时保留期和 binding-only 继续语义已按实现更正。新增回归测试全部通过，相关回归套件通过，`py_compile` 通过。

它仍然是一个可审计的第一阶段兼容切片，不是对客户 OpenClaw 的完整替换，也不代表版本级兼容已经完成。后续应在客户确认版本和样例后，再推进真实 Harness 执行器、真实 MCP 传输、实时事件流、多 worker 取消和限流/清理任务。
