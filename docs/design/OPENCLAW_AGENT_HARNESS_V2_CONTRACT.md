# OpenClaw AgentHarnessV2 锁定合同（候选）

- 文档版本：2026-11-12.2（对应 `openclaw-compat/2026-11-12.2` 与 `zhixing-openclaw-agent-harness-v2/2026-11-12.2`）
- 代码常量：`server/app/openclaw_compat.py` 的 `AGENT_HARNESS_V2_CONTRACT_VERSION`
- 候选锁定版本：`v2026.11.3`
- 锁定状态：`candidate_pending_customer_version_confirmation`
- 适用项目：open-claude-code（知行工作台）OC-T12 第一阶段兼容切片

## 1. 为什么需要这份合同

OpenClaw 官方把 Agent Harness 定义为单轮低层执行器，而不是渠道、提供商或工具注册中心。知行工作台如果直接替换客户的 OpenClaw 网关，成本高且会把渠道、插件和设备控制面一起接进来。第一阶段只做两件事：

1. 在知行侧提供独立的、租户范围的 OpenAI Chat Completions 与 OpenResponses `/v1` 兼容边界；
2. 为后续受信 `AgentHarnessV2` 插件定义一份锁定版本的最小合同，先明确支持面和不支持面。

本文件描述的是**候选合同**，不证明已经和客户实际 OpenClaw 版本完成联调。候选版本取自 2026-11-12 官方发布页可见的 `v2026.11.3`；客户实例的真实版本、渠道清单、插件清单和工具策略仍是外部输入，未取得前不得宣称版本级兼容。

## 2. 锁定与变更规则

- `CANDIDATE_OPENCLAW_VERSION`、合同版本和锁定状态都写在代码常量与 `harness_contract_manifest()` 中，测试会断言这些值，防止文档和实现漂移。
- 在客户确认版本之前，状态只能是 `candidate_pending_customer_version_confirmation`，不能改成 `locked` 或 `verified`。
- 客户确认后，新增一个版本目录/适配层，不覆盖旧合同；旧合同保留用于回滚。
- 任何新增受支持的 turn 字段、事件或工具能力，必须先更新合同清单和测试，再实现；不得先实现再补文档。
- 不支持面默认拒绝，不能因为客户端传了某个字段就静默降级成“近似可用”。

## 3. Turn 合同

`required_turn_fields` 是**目标合同**，但本切片并不把全部字段都交给执行器。代码清单用 `turn_field_status` 逐项标注真实交付状态；客户端和验收方必须以该状态为准，不能把目标字段当成已交付。

| 字段 | 含义 | 当前实现状态 |
| --- | --- | --- |
| `turn_id` | 知行侧本次执行的稳定编号 | **已交付执行器**（`ExecutionContext.turn_id`，同时作为 `ExecutionRequest.request_id`） |
| `session_id` | 知行签发的会话绑定编号 | **已交付执行器**（`ExecutionContext.session_id`，并写入响应头 `X-OpenClaw-Session-Id`） |
| `agent_id` | 外部助手标识，来自模型别名或 `x-openclaw-agent-id` | **已交付执行器**（`ExecutionContext.agent_id` 与 `ExecutionRequest.agent_id`） |
| `messages` | 归一化后的消息序列 | **已交付执行器**（`ExecutionRequest.messages`），图片/音频等返回不支持 |
| `context_digest` | 归一化上下文摘要，用于幂等和审计 | **未交付执行器**：数据库 `request_digest` 只用于幂等冲突判定，不是传给执行器的上下文摘要 |
| `tool_policy_digest` | 工具策略摘要 | **未交付执行器**：当前直接传 `tools` 列表，工具策略摘要由后续连接器接入时补齐 |
| `event_stream_id` | 事件流编号 | **未交付执行器**：当前 SSE 为缓冲式输出，没有持久化事件流编号 |

执行器通过 `ExecutionContext` 获得 `tenant_id`、`principal_id`、`session_id`、`conversation_id`、`agent_id`、`model_alias`、`run_id`、`turn_id`。其中 `conversation_id` 是知行产品会话，供后续原生 Harness 读取上下文和记忆；第一阶段的兼容层不把历史消息重新注入执行器，避免和产品主线重复。

## 3.1 本切片实际支持的能力

代码清单 `supported` 只列本切片真实实现的能力，`capability_scope` 给出每项的真实边界：

- `turn.execute`：facade 协议边界，执行器由部署方注入；
- `turn.cancel`：仅进程内活动运行取消；
- `session.continue`：只做会话绑定，不回放历史消息；
- `tool.call`：合成 MCP 声明安全边界，没有真实 MCP 传输；
- `tool.cancel`：合成连接器的活动调用取消；
- `approval.request`：合成连接器本地审批，尚未接入 facade 或事件流。

以下能力属于**目标合同、本切片未实现**，代码清单放在 `target_only`，不得出现在 `supported`：

- `context.summary`、`memory.read`、`memory.propose_write`、`approval.resume`；
- `event.run.stage`、`event.tool`、`event.approval`、`event.message`、`event.usage`、`event.run.completed`。

## 4. 事件合同（目标未实现）

以下标准事件类别属于目标合同，当前切片**没有实现任何一类**，代码清单把它们全部放在 `target_only`：

- `event.run.stage`：执行阶段变化（未实现）；
- `event.tool`：工具调用开始、结束或取消（未实现）；
- `event.approval`：审批请求、批准、拒绝或过期（未实现）；
- `event.message`：助手文本增量（未实现）；
- `event.usage`：token 或成本用量（未实现）；
- `event.run.completed`：终态（未实现）。

终态只允许：`completed`、`cancelled`、`failed`、`interrupted`。`interrupted` 的触发条件已在 2026-11-12.2 修正为**实例租约过期**，见第 5 节；它不是任意实例启动时无条件改写所有 `running` 记录。

当前 `/v1` facade 的 SSE 是**先执行完再按事件回放**，不是逐 token 实时转发；这满足合成合同和幂等回放，但不能宣称低延迟流式，也不等于上述事件合同已实现。真正的实时事件流需要原生执行器直接把事件写入队列后再实现。

## 5. 取消、超时与幂等

- 取消：`POST /v1/runs/{run_id}/cancel` 和 `POST /v1/responses/{response_id}/cancel`；仅同一租户、同一服务主体可操作。
- 超时：执行器默认 60 秒，可通过构造参数调整；超时返回 `504 MODEL_TIMEOUT`，turn 进入 `failed`。
- 取消返回 `499 REQUEST_CANCELLED`，turn 进入 `cancelled`。
- 幂等：`Idempotency-Key` 或 `x-openclaw-idempotency-key`；请求摘要相同则回放已存响应/SSE，摘要不同返回 `409 IDEMPOTENCY_CONFLICT`，执行中返回 `409 REQUEST_IN_PROGRESS`。

### 5.1 多实例启动恢复（实例租约）

- 每个进入 `running` 的 turn 记录 `owner_instance` 和 `lease_expires_at`，执行期间由后台心跳按 `lease_seconds / 3` 的间隔续租。
- 新实例构造 facade 时只恢复 `lease_expires_at` 为 `NULL`（旧数据）或**已过期**的 `running` turn；其他实例持有有效租约的活跃 turn 保持不变，不会被误标为失败。
- 租约过期恢复会写入 `503 INTERRUPTED`、清空租约并记录 `turn.interrupted` 审计；同一幂等键的并发重放仍返回 `409 REQUEST_IN_PROGRESS`，不会把另一实例正在执行的 turn 误报为 503。
- 默认租约为 120 秒，可通过构造参数 `lease_seconds` 调整，允许范围是大于 0 且不超过 3600 秒。
- 仍存在的限制：活动运行和取消信号保存在进程内存中，取消依赖单进程事件循环；多 worker 部署需要把取消信号改为共享存储或消息总线后重新验收。租约机制解决的是“启动恢复误伤”，不等于多 worker 取消已经实现。

## 6. 身份、会话和凭据

- 下游凭据只接受 `zxoc_` 前缀的租户访问令牌，数据库只保存 SHA-256 哈希；令牌可撤销、可设过期时间。
- 员工网页 Cookie（`zhixing_session`）明确拒绝，返回 `401 EMPLOYEE_SESSION_NOT_ALLOWED`。
- OpenClaw 管理令牌、任意 Bearer、未知令牌统一返回 `401 AUTHENTICATION_FAILED`，不把管理面令牌透传给知行。
- 会话映射键：`(tenant_id, principal_id, agent_id, session_key)`；`principal_id` 是租户服务主体的稳定标识，令牌轮换不会丢失会话。
- `user:` 命名空间由服务端保留，只用于服务端派生会话键 `user:<sha256(user)[:40]>`。客户端若显式传入以 `user:` 开头（大小写不敏感）的 `x-openclaw-session-key`，一律返回 `400 RESERVED_SESSION_KEY`，因此同一租户下另一个终端用户不能靠猜测或复用派生键命中他人会话。
- 显式 `x-openclaw-session-key` 创建的会话在首次携带 `user` 时绑定该 `user` 的 SHA-256 摘要。此后同一会话通过显式会话键、`x-openclaw-session-id` 或 `previous_response_id` 继续时，如果携带不同的 `user`，一律返回 `409 SESSION_CONFLICT`；未携带 `user` 的调用不改变绑定，也不比对摘要。
- `sessionId` 必须已存在且属于同一租户/主体/助手；`previous_response_id` 只在同一作用域内有效，24 小时过期。`previous_response_id` 是**会话绑定式继续**：只把当前请求消息交给执行器，不回放历史消息；24 小时是检索有效期，不是删除任务，数据库中的会话、turn 和 response 行不会因过期自动删除。
- 跨租户、跨助手、跨主体的 `sessionId` / `previous_response_id` 一律 `404`，不区分“不存在”和“无权”，避免枚举。

## 7. 工具、Skill 和插件边界

本阶段只接受标准 MCP `tools/list` 形状的工具声明，并做合成安全边界：

- 每个租户主体显式登记连接器和工具白名单；未登记工具返回 `404 TOOL_NOT_FOUND`。
- 风险分类按固定顺序执行，`readOnlyHint` **不能**把写类或未知工具短路成低风险：
  1. `destructiveHint=true` 或名称命中破坏性/管理/执行/安装/配置/支付/撤销等高风险词元（按 snake_case、kebab-case、camelCase 分词匹配，不做子串匹配）→ `high`，默认拒绝；
  2. 名称命中写类词元（write/create/update/upsert/insert/upload/send/post/patch/put/set/append/move/rename/submit/modify/edit/save/import/sync 等）→ `medium`，必须审批；
  3. 名称命中显式只读动词白名单（read/list/get/search/query/fetch/lookup/describe/inspect/view/show/find/count/status/health/ping/retrieve）且 `readOnlyHint=true` → `low`，自动放行；
  4. 命中只读动词但未声明 `readOnlyHint=true` → `medium`，必须审批；
  5. 其余无法分类的名称，即使声明 `readOnlyHint=true` → `high`，默认拒绝。
- 因此 `write_file`、`send_email`、`create_user`、`update_ticket` 即使带 `readOnlyHint=true` 仍是 `medium` 审批；`mystery_readonly`、`thread_listener` 等未知名称即使带 `readOnlyHint=true` 仍是 `high` 拒绝；`delete_ticket` 等破坏性工具优先按 `high` 拒绝。
- 审批绑定工具名和参数摘要，单次使用，可过期；破坏性/未知工具即使显式加入白名单也不放行。
- 超时返回 `504 TOOL_TIMEOUT`，取消返回 `499 TOOL_CANCELLED`；连接器或单个工具可撤销。
- 审计只记录租户、主体、连接器、工具、动作、结果、参数摘要和输出摘要/长度，不记录原始参数或原始输出。
- 同名工具不能跨连接器重复暴露，避免风险策略被歧义绕过。
- 插件/Skill 的安装、配置写入、自动更新不在本阶段；连接器没有暴露安装接口。真实 MCP 传输、网络访问和客户工具服务器尚未接入。

## 8. 当前不支持面

代码清单 `AGENT_HARNESS_V2_CONTRACT["unsupported"]` 明确列出：

- Gateway WebSocket RPC 和控制面；
- 渠道发送、节点控制、设备配对；
- 插件安装、自动更新、配置写入、管理 RPC；
- Skill 自动安装；
- 提供商模型透传；
- embeddings；
- 图片和音频输入；
- Gateway 历史回放。

`/v1/embeddings` 返回 `501 EMBEDDINGS_UNSUPPORTED`，不伪造向量结果；图片输入返回 `400 UNSUPPORTED_CONTENT`；未知参数返回 `400 UNSUPPORTED_PARAMETER`。

## 9. 执行器绑定要求

- 默认注入 `UnavailableExecutor`，任何未显式绑定经批准执行器的部署都会返回 `503 MODEL_UNAVAILABLE`，不会意外调用付费模型。
- 正式执行器必须由部署方显式注入，并满足：租户/主体隔离、会话继续、超时、取消、幂等、错误边界、审计、用量上报。
- 执行器不得接收租户 Bearer 令牌；令牌只停留在 facade 的认证层。
- 本阶段没有绑定真实模型执行器，也没有产生任何真实模型结果；所有测试使用合成执行器。

## 10. 验收门

在声明“兼容完成”前至少需要：

1. 客户确认 OpenClaw 版本和渠道清单；
2. 客户提供脱敏请求/响应样例和基线对比；
3. 真实或受控的 Harness 适配器完成 turn/事件/审批/取消/恢复联调；
4. 真实 MCP 连接器的白名单、超时、取消、撤销、断线重连和租户隔离回归；
5. 流式事件从缓冲回放升级为可观测的实时事件；
6. 多 worker 部署下的幂等和取消共享状态验收；
7. 回滚演练：保留旧 OpenClaw 地址，删除/停用 sidecar 即可回退，不迁移真实会话。

在上述门通过前，本文件只代表候选合同和已实现的合成切片。
