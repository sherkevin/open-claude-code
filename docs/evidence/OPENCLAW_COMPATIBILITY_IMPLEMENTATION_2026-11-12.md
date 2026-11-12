# OpenClaw 兼容切片实现证据（OC-T12，2026-11-12）

## 1. 证据范围与边界

本记录对应任务 `OC-T12“OpenClaw 兼容入口与最小迁移适配”`，只证明在隔离工作树中完成了**第一阶段、合成夹具驱动的兼容切片**，不证明已经和客户真实 OpenClaw 实例完成版本级联调。

- 工作位置：学校云，隔离工作树 `.claude/worktrees/oc-t12-openclaw-compat`，分支 `worktree-oc-t12-openclaw-compat`。
- 没有部署生产、没有重启共享服务、没有读取真实客户数据、没有使用凭据、没有调用付费模型。
- 所有测试使用合成租户、合成用户、合成执行器和合成 MCP 工具声明，不包含任何真实模型结果或客户样例。
- 没有修改 `/media/data3/dengkw/projects/project-board` 共享代码，也没有把任务板改为完成。
- 设计输入来自主工作区中未跟踪的 `docs/design/OPENCLAW_COMPATIBILITY_AND_MIGRATION.md` 与 `docs/evidence/OPENCLAW_COMPATIBILITY_2026-11-12.md`。为避免和主工作区未跟踪文件发生路径冲突，本工作树不复制这两个文件，改为新增本同主题实现证据。

## 2. 变更文件

全部为新增文件，没有修改现有业务代码：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `server/app/openclaw_compat.py` | 1751 | 独立、租户范围的 `/v1` 兼容 facade、认证、会话映射、幂等、取消/超时、审计、AgentHarnessV2 合同清单 |
| `server/app/openclaw_connector.py` | 736 | 标准 MCP 形状的合成工具安全边界：白名单、风险分类、审批、超时、取消、撤销、审计 |
| `server/tests/test_openclaw_compat.py` | 762 | facade 合同测试 13 个 |
| `server/tests/test_openclaw_connector.py` | 460 | 连接器安全边界测试 12 个 |
| `docs/design/OPENCLAW_AGENT_HARNESS_V2_CONTRACT.md` | 121 | 锁定候选版本的 AgentHarnessV2 合同与当前不支持面 |
| `docs/evidence/OPENCLAW_COMPATIBILITY_IMPLEMENTATION_2026-11-12.md` | 本文件 | 实现与测试的真实证据 |

关键文件校验和（2026-11-12 最终测试后）：

```
607f9172f6b811aa24a065ef0a54b4d5  server/app/openclaw_compat.py
c15efaa7879036fd3c59195581545b3a  server/app/openclaw_connector.py
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

兼容版本常量：`COMPAT_VERSION = "openclaw-compat/2026-11-12.1"`。

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
- 支持 `previous_response_id` 会话继续，24 小时过期；跨租户、跨主体、跨助手一律 `404`，不区分“不存在”和“无权”。
- 模型白名单来自租户/令牌范围，支持模型别名和 `x-openclaw-agent-id` 助手映射，冲突返回 `400 MODEL_OVERRIDE_CONFLICT`。
- 未知参数、图片/音频输入、重复工具、非法 `tool_choice`、超长输入和超大请求体都返回明确的 `400`，不静默降级。
- 上游执行器错误映射为稳定错误码，不泄露内部异常或凭据。

### 3.3 会话隔离映射

- 映射键：`(tenant_id, principal_id, agent_id, session_key)`。
- `principal_id` 使用稳定的 `tenant:<tenant_id>:user:<user_id>`，令牌轮换不会丢失既有会话。
- `sessionId` 由服务端签发，必须已存在且属于同一租户、同一主体、同一助手；不匹配返回 `404`。
- `sessionKey` 显式命名空间保留前缀（`admin`、`system`、`internal`、`operator` 等）拒绝；`user` 字段派生为哈希后的会话键，数据库不保存原始值。
- 每个外部会话绑定一个真实产品 `conversations` 记录，标题为 `OpenClaw · <agent> · <哈希>`，便于后续原生 Harness 衔接。
- 同租户不同用户、不同租户、不同助手之间互相不可见，测试覆盖了跨租户和跨助手的读取、继续和取消。

### 3.4 MCP / Skill / 插件合成安全边界

`server/app/openclaw_connector.py` 只接受标准 MCP `tools/list` 形状的声明，并实现：

- 每个租户主体显式登记连接器和工具白名单；未登记返回 `404 TOOL_NOT_FOUND`。
- 低风险只读工具（`readOnlyHint` 且名称不含高风险语义）自动放行。
- 写类工具（write/create/update/upload/send 等）归为 `medium`，必须审批；审批绑定工具名和参数摘要，单次使用，默认 5 分钟过期。
- 破坏性、管理、执行、安装、配置、支付、撤销等语义，以及无法分类的未知工具，一律 `high` 并默认拒绝，即使显式加入白名单也不放行。
- 工具名不能跨连接器重复暴露，重复返回 `409 TOOL_NAME_CONFLICT`。
- 参数必须是 JSON 对象，大小、深度、字符串预算、必填字段、额外字段和基础类型都有上限；摘要使用排序后的规范 JSON。
- 执行超时返回 `504 TOOL_TIMEOUT`，取消返回 `499 TOOL_CANCELLED`，默认执行器不可用时返回 `503 TOOL_UNAVAILABLE`。
- 连接器和单个工具都可撤销；撤销后立即不可发现、不可调用。
- 审计只记录租户、主体、连接器、工具、动作、结果、参数摘要、输出摘要和长度，不记录原始参数或原始输出。
- 默认执行器为失败关闭的 `UnavailableToolExecutor`，不会意外连接真实 MCP 服务器。
- 本阶段**没有**真实 MCP 传输、网络访问、插件安装、自动更新或配置写入接口。

### 3.5 AgentHarnessV2 候选合同

`docs/design/OPENCLAW_AGENT_HARNESS_V2_CONTRACT.md` 和代码清单 `harness_contract_manifest()` 锁定：

- 合同版本：`zhixing-openclaw-agent-harness-v2/2026-11-12.1`
- 候选 OpenClaw 版本：`v2026.11.3`
- 锁定状态：`candidate_pending_customer_version_confirmation`

合同列出受支持 turn 字段、事件类别、取消/超时/幂等规则、身份与会话规则、工具边界，以及明确不支持面：Gateway WebSocket RPC、渠道发送、节点控制、设备配对、插件安装/自动更新/配置写入、Skill 自动安装、提供商模型透传、embeddings、图片/音频输入、Gateway 历史回放。

客户确认真实版本前，状态不得改为 `locked` 或 `verified`。本阶段没有产生任何真实模型结果。

### 3.6 默认失败关闭

- `OpenClawFacade` 默认注入 `UnavailableExecutor`，未显式绑定经批准执行器时返回 `503 MODEL_UNAVAILABLE`。
- 连接器默认注入 `UnavailableToolExecutor`，返回 `503 TOOL_UNAVAILABLE`。
- 因此即使误挂载 sidecar，也不会自动调用付费模型或真实工具服务器。

## 4. 测试命令与真实结果

运行环境：Python 3.10 虚拟环境 `/media/data3/dengkw/projects/open-claude-code/.venv/bin/python`，工作目录 `server/`。

### 4.1 定向编译与两个新增测试套件（最终代码）

```bash
cd /media/data3/dengkw/projects/open-claude-code/.claude/worktrees/oc-t12-openclaw-compat/server
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m py_compile \
  app/openclaw_compat.py app/openclaw_connector.py \
  tests/test_openclaw_compat.py tests/test_openclaw_connector.py
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  tests/test_openclaw_compat.py tests/test_openclaw_connector.py -q
```

真实结果：

```
25 passed, 1 warning in 139.07s (0:02:19)
```

`py_compile` 无输出并以退出码 0 通过。唯一警告来自现有依赖 `fastapi/testclient.py` 的 Starlette 弃用提示，与本任务代码无关。

### 4.2 相关回归（最终代码）

```bash
cd /media/data3/dengkw/projects/open-claude-code/.claude/worktrees/oc-t12-openclaw-compat/server
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
  tests/test_openai_compat.py tests/test_config.py \
  tests/test_session_isolation_042.py tests/test_api.py -q
```

真实结果：

```
33 passed, 1 warning in 122.84s (0:02:03)
```

这些回归覆盖现有内部请求归一化、配置、会话隔离和公开 API，确认新增文件没有改变默认检索、现有 Harness 主线或其他产品功能的行为。

### 4.3 测试覆盖矩阵

| 验收要求 | 对应测试 |
| --- | --- |
| 非流式、SSE、工具往返、会话继续、幂等、错误、取消、模型白名单 | `test_chat_non_stream_and_sse`、`test_responses_non_stream_previous_and_sse`、`test_tool_call_round_trip_chat_and_responses`、`test_idempotency_replay_conflict_and_stream_replay`、`test_validation_and_upstream_error_envelopes`、`test_timeout_and_cancellation_are_terminal`、`test_cancel_endpoints_and_terminal_states`、`test_model_whitelist_and_agent_mapping` |
| 独立租户 `/v1` 边界，不复用员工 Cookie 或管理令牌 | `test_auth_rejects_cookie_management_and_invalid_tokens` |
| `agentId/sessionKey/sessionId` 隔离映射 | `test_session_mapping_continuation_and_isolation`、`test_model_whitelist_and_agent_mapping` |
| MCP/Skill/插件白名单、超时、取消、撤销、审计、未知高风险默认拒绝 | `test_openclaw_connector.py` 全部 12 个测试 |
| AgentHarnessV2 锁定合同与不支持面 | `test_harness_contract_and_default_executor_fail_closed` |
| 默认检索、现有 Harness 和其他功能不回退 | 4.2 回归套件 |
| 不存储提示词、原始用户键或明文凭据 | `test_audit_and_persistence_do_not_store_prompts_or_credentials` |
| 并发幂等请求返回执行中 | `test_idempotency_concurrent_request_is_reported_in_progress` |
| 工具参数、声明和输出边界 | `test_argument_and_declaration_bounds`、`test_output_bounds_and_executor_failures` |
| 恢复未完成调用 | `test_recovery_marks_running_calls_as_interrupted` |

## 5. 已知限制与剩余缺口

以下都是真实存在、尚未完成的能力，不得当作已完成：

1. SSE 是“执行完成后按事件回放”，不是逐 token 实时转发；低延迟流式需要原生执行器接入事件队列。
2. 活动运行和取消信号保存在进程内存中，只适用于单进程；多 worker 部署需要共享存储或消息总线。
3. 没有绑定真实模型执行器，也没有真实或受控 Harness 适配器联调；默认返回 `503 MODEL_UNAVAILABLE`。
4. 没有真实 MCP 传输、网络访问和客户工具服务器；连接器只验证合成安全边界。
5. 没有 Gateway WebSocket RPC、渠道、节点、设备、插件安装、配置写入、Skill 自动安装和 embeddings。
6. 客户 OpenClaw 版本、渠道清单、脱敏样例和基线对比仍未提供，合同状态只能是候选。
7. `store` 字段被接受但保留期固定为 24 小时；审计保留期和清理策略尚未实现。
8. 请求体超过 1 MiB 时由现有中间件返回 `413 UPLOAD_TOO_LARGE`，不是 OpenClaw 风格错误码；如果客户依赖统一错误格式，需要后续适配。
9. 会话创建在单进程下处理了唯一键竞争，但极端崩溃场景可能留下未绑定的产品会话；需要后续清理任务。
10. `parallel_tool_calls` 被接受，但执行器顺序执行；不支持真正的并行工具调用。

## 6. 冲突与集成说明

- `server/app/main.py` 和 `docs/REQUIREMENTS_ALIGNMENT.md` 没有修改。原因是其他工作树（`oc-t2-auto-compaction-usage`、`oc-t9-workflow-artifacts`）正在修改这些文件，按任务要求优先只新增 OpenClaw 适配专属文件并报告冲突。
- 新增 sidecar 工厂需要部署方显式挂载；本切片不改变现有应用启动路径，因此不会自动影响员工入口。
- 主工作区中 `docs/design/OPENCLAW_COMPATIBILITY_AND_MIGRATION.md` 和 `docs/evidence/OPENCLAW_COMPATIBILITY_2026-11-12.md` 目前是未跟踪文件；本工作树没有修改或复制它们，避免跨工作树路径冲突，并以本文件提供同主题实现证据。
- 任务板未修改，最终状态由父级/调度方确认。

## 7. 结论

本切片已经实现并验证了：独立租户 `/v1` facade、Chat/Responses 合成合同、会话隔离映射、工具安全边界、AgentHarnessV2 候选合同和默认失败关闭。它是一个可审计的第一阶段兼容切片，不是对客户 OpenClaw 的完整替换，也不代表版本级兼容已经完成。后续应在客户确认版本和样例后，再推进真实 Harness 执行器、真实 MCP 传输、实时事件流和多 worker 验收。
