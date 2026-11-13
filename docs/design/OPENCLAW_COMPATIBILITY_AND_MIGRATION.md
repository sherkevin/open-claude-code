# OpenClaw 兼容替换与华微世纪最小迁移方案

状态：设计与接口盘点（资料截至 2026-11-12；客户现网版本尚未核验）

项目：open-claude-code（知行工作台）

服务器：学校云

## 1. 结论先行

华微世纪现在使用的是 **OpenClaw**（不是 Open Cloud，也不是 OpenClaude）。我们不应让华微重写前端、品牌入口、业务系统或现有本地 MCP。最省客户工作、同时保留控制权的方案是：

1. 若目标是最快试用，先提供一个 OpenClaw 可配置的模型提供商入口，兼容 OpenAI Chat Completions 和 OpenResponses；这一步只替换模型调用，不宣称已经替换 OpenClaw 的上下文、记忆、压缩和工具编排。
2. 若目标是用户说的“我们的逻辑替换 OpenClaw Agent”，正式路线是保留 OpenClaw 作为渠道、身份和边缘策略入口，安装一个锁定版本、受信任的 `AgentHarnessV2` 适配插件，把 turn、工具、审批、取消、继续会话和流式事件交给知行工作台。这样客户仍只改一个插件/运行时入口；但该官方 Harness SDK 是实验性、版本耦合的接口，必须单独验收。
3. 华微侧只改一次 `base_url`/provider 或安装一次受信插件，再换一次租户访问令牌；原来的消息页面、模型选择、会话标识和业务系统尽量不动。
4. 如果他们的实际前端不是 HTTP，而是直接连 OpenClaw Gateway WebSocket，再提供一个只实现聊天所需最小 RPC 的适配层，不复制完整控制面、节点面或管理面。
5. 华微内网 MCP 不公网暴露。优先使用双方已有的私网互通；如果没有私网通道，只在华微侧运行一个轻量出站连接器，由它主动连接学校云中继，再把请求转发到本地 MCP。连接器只需要配置一次，不要求华微逐个改造业务系统。
6. OpenClaw 的插件、Skill、模型和会话历史不直接照搬为高权限能力。它们通过兼容层映射到知行工作台的租户、用户、助手、会话、工具白名单、审批和审计模型；未知能力默认拒绝。

因此要区分三个层次：模型 provider 兼容是低改动过渡；Harness 适配才是本项目“替换 Agent 执行层”的优先正式方案；完全卸载 OpenClaw Gateway 并重接所有渠道是第三种高成本方案，不能承诺只改一个地址。当前仍是开发设计，尚未对客户 OpenClaw 实例做真实连通或生产切换。

## 2. 需求来源与边界

### 2.1 已确认的华微需求

- 华微有自己的智能体入口、品牌、业务系统、角色和权限，不希望为了试用迁移这些资产。
- 业务工具和数据在华微侧，智能体能力需要覆盖模型、角色、知识库与检索、业务工具与 MCP、权限、安全、成本性能、可观测和评测。
- 现有 OpenClaw 的开放性带来插件安装、越权和 Token/工具控制问题；目标是让底层能力对我们透明、可调优、可审计，并由知行工作台的策略控制。
- 会议逐字稿中提到：只要告知 MCP 接入方式，现有框架可以调用并在本地形成 Skill；因此迁移应优先兼容其接入合同，而不是让客户重建每个 Skill。

来源：

- [华微世纪会议全文复核记录](../evidence/FEISHU_HUAWI_MEETING_FULL_TRANSCRIPT_2026-11-12.md)
- 原始飞书文字记录：<https://ucnik4pxez5h.feishu.cn/docx/MhqfdzOM0olYgxxs2j7cB6nGnfb>
- 智能纪要：<https://ucnik4pxez5h.feishu.cn/docx/PmWSdaUg1owpXmxvqIVcEMsanTf>

### 2.2 本轮硬边界

- 只在学校云做开发和合成/公开资料验收，不部署生产、不重启共享服务、不读取真实客户数据、不索取或保存客户凭据。
- 不把 OpenClaw 官方文档的能力描述当成华微实例已经启用的事实；必须取得华微实际版本和入口证据后才能做兼容承诺。
- 不把“兼容接口”误写成“已经替换完成”。兼容层、客户试用、客户验收和生产切换是四个不同状态。
- 任何插件安装、远程工具、模型切换或跨网调用都经过知行工作台的租户权限、工具白名单、审批和审计；未知字段和未知能力默认拒绝。

## 3. OpenClaw 官方接口盘点

以下为截至 2026-11-12 查阅的 OpenClaw 官方文档接口。官方 release 页面当时显示最新版本为 `v2026.11.3`，但这不是华微现网版本；接口名称和行为必须在客户提供的具体版本上再做一次回归，因为 Gateway WebSocket、插件 SDK 和 Harness 都是版本化协议。

OpenClaw 的官方架构是一个长驻 Gateway 持有消息渠道、provider 连接、会话和路由；控制面客户端和节点通过 WebSocket 连接，默认监听回环地址，同一端口同时承载 WebSocket 和 HTTP。这个事实决定了“直接冒充整个 Gateway”会牵涉渠道、设备配对、节点能力、管理 RPC 和事件恢复，不能作为最小迁移方案。

官方依据：[Gateway architecture](https://github.com/openclaw/openclaw/blob/main/docs/concepts/architecture.md)、[Gateway external apps](https://github.com/openclaw/openclaw/blob/main/docs/gateway/external-apps.md)、[OpenClaw releases](https://github.com/openclaw/openclaw/releases)。

### 3.1 OpenAI Chat Completions HTTP

OpenClaw Gateway 可以在同一 Gateway 端口提供一个默认关闭的 OpenAI 兼容面：

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/v1/chat/completions` | 消息、流式输出、函数工具调用 |
| `GET` | `/v1/models` | 返回 OpenClaw 的 Agent 目标，而不是全部后端供应商模型 |
| `GET` | `/v1/models/{id}` | 查询一个 Agent 目标 |
| `POST` | `/v1/embeddings` | 在已配置的 embedding provider 内生成向量 |

主要兼容点：

- `Authorization: Bearer ...` 或受信代理身份；共享 Bearer 可能对应较宽的 operator 权限，不能原样复制到知行工作台。
- `messages`、`tools`、`tool_choice`、`role: tool`、`tool_call_id` 和 SSE `data: ...`/`data: [DONE]`。
- 用 `user` 字段稳定路由一个会话；同一个会话线程重复使用同一值即可继续历史。
- `x-openclaw-model`、`x-openclaw-agent-id`、`x-openclaw-session-key` 和 `x-openclaw-message-channel` 是 OpenClaw 专用兼容头。显式 session key 还受权限和保留命名空间校验。
- 请求体读取、大小、图片数量和累计图片字节有边界；断开 HTTP 请求会取消活动运行的下载和执行。
- 错误会区分认证失败、模型不存在、超时、限流、额度不足和服务失败；知行工作台必须保留公开错误码和内部审计原因的分离。

官方依据：[OpenAI Chat Completions API](https://docs.openclaw.ai/gateway/openai-http-api)。

### 3.2 OpenResponses HTTP

OpenClaw 另有默认关闭的 `POST /v1/responses`：

- 请求使用 `input`、`instructions`、`tools`、`tool_choice`、`stream`、`max_output_tokens`、`user` 和 `previous_response_id` 等字段。
- `model: "openclaw"`、`openclaw/default` 或 `openclaw/<agentId>` 表示 Agent 目标；不是直接暴露全部后端模型。
- `x-openclaw-agent-id` 选择 Agent，`x-openclaw-model` 覆盖后端模型，`x-openclaw-session-key` 做显式会话路由，均必须经过知行工作台的映射和授权。
- `previous_response_id` 只能在同一认证主体、Agent 和会话作用域内继续；未知、过期或越权的标识必须返回明确错误，不能静默新建会话。
- 函数工具后续回传需要保留调用编号和 `function_call_output` 语义，否则 OpenClaw 客户端会把工具结果当普通文本。

官方依据：[OpenResponses API](https://docs.openclaw.ai/gateway/openresponses-http-api)。

### 3.3a Agent Harness：真正替换 Agent Loop 的窄接口

OpenClaw 官方把 Agent Harness 定义为“一次准备好的 Agent turn 的低层执行器”，明确说明它不是模型 provider、不是 channel、也不是 tool registry；普通 HTTP/WebSocket 模型 API 应使用 provider plugin，而只有拥有原生会话、压缩、工具事件或恢复语义的运行时才使用 Harness。该接口目前是实验性的，只建议 bundled/trusted native plugin 使用。

对本项目的含义：

- **过渡方案**：实现 `models.providers.zhixing`，只替换模型调用，验证协议和效果；客户改动最少，但 OpenClaw 仍掌握上下文、记忆、压缩和工具循环。
- **正式替换方案**：实现锁定 OpenClaw 版本的 `AgentHarnessV2` 插件，把知行工作台的会话、上下文压缩、记忆、RAG、工具策略、审批和事件作为外部执行器；OpenClaw 只负责渠道、身份、入口和必要的宿主策略。
- **完整卸载方案**：知行工作台重新实现每个客户渠道、设备配对、消息路由和节点能力，工程量最大，不属于“少让华微配置”的首选。

官方依据：[Agent harness plugins](https://github.com/openclaw/openclaw/blob/main/docs/plugins/sdk-agent-harness.md)。

### 3.3 Gateway WebSocket 控制面

如果华微入口直接使用 OpenClaw Gateway，而不是 OpenAI 兼容 HTTP，则需要面对版本化 WebSocket 协议：

1. Gateway 先发 `connect.challenge`，包含 `nonce` 和时间戳。
2. 客户端发送 `connect`，声明协议版本、客户端、角色、权限 scope、能力、认证和可选 Ed25519 设备签名。
3. 服务端返回 `hello-ok`，其中有协议版本、方法/事件能力、快照、有效授权 scope 和传输策略。
4. 后续通过 JSON-RPC 请求和事件完成聊天、会话、审批、节点和运行控制。

官方角色和最小 scope：

- `operator.read`：历史、会话列表、订阅和只读事件。
- `operator.write`：发送消息和普通会话修改。
- `operator.approvals`：查看和处理执行/插件审批。
- `operator.questions`、`operator.pairing`、`operator.admin` 只在确实需要时授予。

Gateway 是 OpenClaw 的单一控制面和节点传输，不应为了兼容聊天而实现整个控制面。知行工作台应先实现有限的聊天子集，并在 `hello-ok.features.methods` 中只声明真实支持的方法。

官方依据：[Gateway protocol](https://docs.openclaw.ai/gateway/protocol)、[Handshake](https://docs.openclaw.ai/gateway/protocol/handshake)、[Building a Gateway client](https://docs.openclaw.ai/gateway/clients)。

### 3.4 最小会话 RPC 子集

OpenClaw 的会话控制文档将发送、历史、运行生命周期和维护拆成一组 RPC。迁移适配器首版只考虑下列方法：

| OpenClaw 方法 | 知行工作台目标 |
|---|---|
| `chat.send` | `POST /api/v1/conversations/{id}/messages`，保留 `idempotencyKey` 和会话映射 |
| `chat.history` | `GET /api/v1/conversations/{id}/messages`，只返回当前主体有权看到的消息 |
| `chat.abort` | `POST /api/v1/runs/{id}/cancel`，向下游传播取消但不自动重放写操作 |
| `sessions.list/get` | 会话列表/详情接口，过滤到当前租户和主体 |
| `sessions.send` | 已存在会话的消息发送，走相同审批和幂等路径 |
| `sessions.abort` | 取消活动或排队运行，清理可取消的队列项 |
| `agent` | 创建异步运行，返回 `{runId, acceptedAt}` |
| `agent.wait` | 查询运行终态，超时只结束等待，不取消底层运行 |
| 生命周期事件 | 映射到知行工作台 SSE 的 `run.stage`、`tool.*`、`approval.*`、`message.*` 和 `run.completed` |

`config.*`、`exec.approvals.*`、`wizard.*`、`update.*`、节点设备控制、插件安装和管理员 RPC 不进入首版兼容面；如果未来有客户确实需要，必须单独建需求、权限和验收项。

官方依据：[Session control RPC](https://docs.openclaw.ai/gateway/protocol/rpc-session-control)、[Agent loop](https://docs.openclaw.ai/agent-loop)。

### 3.5 MCP、Skill、插件和 OpenClaw 作为 MCP Server

OpenClaw 官方说明它既可以是 MCP client，也可以是 MCP server：

- `mcp.servers` 中登记 Streamable HTTP、SSE 或 stdio 服务器；工具仍经过 OpenClaw 的 tool profile 和 tool policy，连接服务器不会绕过策略。
- `openclaw mcp serve` 启动 stdio MCP server，再通过 WebSocket 连接本地或远程 Gateway，把已路由的频道会话暴露为 MCP 对话工具。
- Skill/插件可以注册工具、频道、应用和 MCP server；插件目录和安装扫描不是知行工作台的可信授权来源。

知行工作台的映射原则：

- 客户现有 MCP server 保留在华微网络；知行工作台只消费经过白名单的工具描述和结构化证据。
- 不把 OpenClaw Skill 文本直接当成高权限系统提示词。先解析为工具声明、输入输出 schema、数据范围、风险等级和版本，再进入审批策略。
- 任何“自动安装插件”“自动写配置”“自动发送消息”的能力默认 `deny_by_default`；低风险只读可在明确策略下代审，高风险一律人工/双人审批。

官方依据：[Why OpenClaw](https://docs.openclaw.ai/start/why-openclaw)、[Connect MCP servers](https://github.com/openclaw/openclaw/blob/main/docs/tools/mcp.md)、[Run OpenClaw as an MCP server](https://docs.openclaw.ai/cli/mcp/serve)。

### 3.6 OpenClaw MCP Relay 与跨网启示

OpenClaw 自己的 `mcp-relay` 插件采用“Gateway 主动出站 WebSocket 到中继，远程 MCP 客户端访问 HTTPS MCP 端点”的模式；客户不需要把 Gateway 端口暴露到公网，并通过登录/一次性配对、授权范围和撤销管理接入。

这证明出站连接器/中继是 OpenClaw 生态已经采用的集成形态，但 **中继外层协议是 OpenClaw 特定实现，不是 MCP 标准**。我们的连接器应保留 MCP 的工具发现、JSON-RPC、超时、取消和错误语义，外层只使用租户绑定的短期连接授权、双向 TLS 或等效加密、心跳、重连、撤销和完整审计。

官方依据：[OpenClaw MCP Relay](https://docs.openclaw.ai/plugins/mcp-relay)。

## 4. 知行工作台替换架构

```text
华微原有前端 / 第三方入口 / 角色权限
          |
          | 只改一次 base_url + 租户令牌
          v
知行 OpenClaw Compatibility Gateway（学校云）
  |- /v1/chat/completions
  |- /v1/responses
  |- /v1/models /v1/embeddings
  |- 可选：最小 Gateway WS RPC
  |- 会话映射、幂等、SSE、取消、错误映射
  |- 自动模型路由、知识/RAG、审批、安全、审计
          |
          | 私网直连；若不可行则出站连接器 + 中继
          v
华微内网 Connector
          |
          v
华微本地 MCP / 业务系统 / 原有数据权限
```

### 4.1 兼容网关输入合同（草案）

```text
POST /v1/chat/completions
Authorization: Bearer <tenant-scoped-token>
Content-Type: application/json

{
  "model": "openclaw/default",       // 兼容别名，不直接决定供应商
  "user": "conv:<customer-thread>",  // 稳定会话键，映射到本产品会话
  "messages": [...],
  "tools": [...],
  "tool_choice": "auto",
  "stream": true,
  "max_tokens": 512
}
```

响应保持 OpenAI 兼容的 `choices`、`finish_reason`、工具调用和 `usage` 形状；内部同时写入租户、主体、助手、会话、请求幂等键、路由决策、工具调用摘要、审批结果、证据引用和取消原因。原始客户业务内容不得写入公开日志。

```text
POST /v1/responses
Authorization: Bearer <tenant-scoped-token>

{
  "model": "openclaw/default",
  "user": "conv:<customer-thread>",
  "input": "...",
  "tools": [...],
  "stream": true
}
```

### 4.2 兼容头映射

| OpenClaw 输入 | 知行工作台处理 |
|---|---|
| `model=openclaw/default` | 映射到该租户允许的助手/默认模型；不让客户任意指定供应商模型 |
| `openclaw/<agentId>` | 映射到租户内已发布助手别名；不存在或未授权返回 404/403 |
| `user` | 规范化为租户内会话键；禁止跨租户复用 |
| `x-openclaw-session-key` | 仅接受应用自有非保留命名空间；不开放管理员/隐身会话语义 |
| `x-openclaw-model` | 首版忽略或仅允许白名单别名；禁止通过兼容头越过自动路由和授权 |
| `x-openclaw-agent-id` | 只作兼容别名，最终仍以租户助手权限为准 |
| `x-openclaw-message-channel` | 作为审计和策略上下文，不直接获得频道发送权限 |
| `previous_response_id` | 绑定认证主体、租户、助手和会话；解析失败不静默新建会话 |

### 4.3 工具和证据映射

- OpenClaw function tool 名称 -> 知行工作台工具注册表中的规范工具 ID。
- MCP 工具 schema -> 结构化参数、数据范围、风险等级、超时和审批策略。
- MCP 结果 -> 脱敏后的结构化结果 + 证据卡；聊天引用只能链接到当前授权的文档、记录或远程 HTTP(S) 来源。
- `tool_calls`/`function_call_output` -> 内部 `tool.started`、`tool.completed`、审批事件和下一轮模型输入。
- 业务写操作 -> 默认暂停；模型代审只适用于低风险只读，未知/高风险转人工；涉及金额、批量数据、外发消息或删除一律不自动放行。

## 5. 让华微只做最少工作

### 5.1 推荐的客户步骤

1. 华微研发只需提供 OpenClaw 的精确版本、实际使用的入口类型（HTTP 或 Gateway WebSocket）和一个脱敏配置片段；不提供生产凭据。
2. 我们给出一份兼容配置，将原 Gateway/API 的 `base_url` 改为知行兼容网关地址，并换成一个租户范围令牌。
3. 如果华微 MCP 只能在内网访问，华微侧安装或运行一个轻量连接器，填写学校云中继地址、租户连接码和本地 MCP 地址；不改业务系统，不开放入站端口。
4. 保留原来的前端、频道、品牌和角色。华微只在合成/脱敏任务上验证消息、会话继续、工具调用、取消、权限拒绝和引用。
5. 若验收失败，撤销令牌并把 `base_url` 切回原 OpenClaw Gateway，即可回滚；不要求删除原配置。

### 5.2 不让客户做的事

- 不要求客户重写前端或把所有 Skill 手工改成新格式。
- 不要求客户把本地 MCP 公网暴露。
- 不要求客户把全量业务数据复制到学校云。
- 不要求客户提供主账号、模型供应商或业务系统凭据。
- 不让客户把 OpenClaw 管理员 Bearer 当作知行工作台普通用户令牌。

## 6. 迁移阶段、回滚和验收

### 阶段 A：学校云离线兼容夹具

- 用合成 OpenClaw Chat/Responses 请求验证解析、SSE、工具调用、幂等、会话保持、错误码、超时和取消。
- 同一套请求通过最简单 Agent 与增强 Agent 各跑一次，记录输入/输出 Token、时间、上下文和证据，不以估算值冒充真实供应商账单。
- 连接器使用只读合成 MCP，验证工具发现、schema 白名单、超时、取消、断线重连、撤销和审计。

### 阶段 B：客户侧无生产联调

- 华微提供版本和入口证据；我们生成对应的兼容配置和一条可回滚的试用令牌。
- 先只读工具、单租户、单测试用户、有限数据范围；写操作全部关闭。
- 检查 OpenClaw 原请求在不改业务前端的情况下能否得到正确 SSE/工具/会话行为。

### 阶段 C：客户试用验收

至少三类脱敏任务：普通问答、需要 MCP 的业务查询、权限拒绝任务。每项都记录请求、路由、工具、证据、Token、延迟、取消/错误和最终结果。没有客户样例时，状态只能是 `待客户样例`，不能写成完成。

### 阶段 D：生产切换（本轮不执行）

- 只有学校云验收、用户单独批准、客户书面确认回滚窗口后才允许。
- 先双写/旁路比较，再切换单个租户或单个入口；保留原 OpenClaw 回退地址。
- 任何生产部署、真实客户调用、凭据配置和服务重启另立审批记录。

### 6.1 可自动验收的兼容门槛

1. 原 OpenClaw Chat Completions 请求无需修改消息结构即可获得合法非流式和 SSE 响应。
2. OpenResponses `input`、工具调用和 `function_call_output` 可完成一轮闭环。
3. 相同 `user`/会话键连续两次请求落到同一知行会话；不同租户或不同主体不能互读。
4. 相同幂等键重试不产生第二条用户消息或第二次业务副作用。
5. 客户断开连接、显式 abort、超时和连接器断线都能进入明确终态；不自动重放写操作。
6. `/v1/models` 只返回租户允许的兼容别名，不能借此枚举供应商或管理员模型。
7. MCP 只读合成工具可发现、调用、取消、撤销；审计记录包含租户、主体、会话、工具、参数摘要、结果状态和策略版本，不含秘密和全文。
8. 高风险、未知工具、越权数据范围、插件安装和配置写入均默认拒绝或转人工。
9. 回滚只需恢复旧 `base_url`/令牌，兼容层撤销后不再接受旧令牌。

### 6.2 两条适配路线的通过条件

| 路线 | 能证明什么 | 不能证明什么 |
|---|---|---|
| Provider 适配 | OpenClaw 能把原有消息、工具 schema 和历史交给知行模型并拿回合法结果；客户改动最少 | 不能证明知行工作台已经接管 OpenClaw 的上下文、记忆、压缩、工具循环或权限策略 |
| AgentHarnessV2 适配 | OpenClaw 仍可保留渠道/身份入口，但 turn、上下文、记忆、工具、审批、取消、恢复和事件由知行负责 | 不能自动证明所有 OpenClaw 插件、渠道、节点和管理 RPC 兼容；必须锁版本并按实际客户启用面验收 |

实现上不能把现有员工 Cookie API 直接填写为 OpenClaw provider 的 `baseUrl`。应新增独立 `/v1` facade 或受控 sidecar，使用租户服务令牌或双向认证，并把 OpenClaw 的 `agentId/sessionKey/sessionId` 映射到知行的用户、助手、会话和原生执行器；不要把 OpenClaw Gateway 管理令牌透传给知行。

## 7. 当前缺口与外部输入

当前可以在学校云继续完成：兼容请求解析夹具、OpenClaw 别名映射、会话/幂等/取消合同、最小 SSE 适配、连接器协议草案、审计字段和安全测试；不需要客户数据或生产凭据。

仍需华微提供但不阻塞本轮设计：

- 精确 OpenClaw 版本和启用的入口（Chat Completions、Responses、Gateway WebSocket、MCP Relay、CLI 或组合）。
- 脱敏的客户端配置片段或一次请求/响应样例，以确认真正使用的字段、SSE 事件和工具调用形状。
- 允许接入的 MCP 工具清单、用户/角色到数据范围的映射和连接器所在网络出口。
- 一个合成或脱敏测试任务及原 OpenClaw 的基线结果；没有这些只能做协议兼容，不能宣称客户效果等价。

不要求提供：生产账号、主账号密码、客户原始数据、模型供应商密钥或任何无法撤销的长期凭据。

## 8. 设计状态

- **已完成**：官方接口盘点、客户最小迁移方案、HTTP/WS/MCP 映射草案、回滚边界和学校云离线验收清单。
- **未完成**：知行工作台公开兼容网关实现、最小 Gateway WS 适配器、出站连接器真实协议、华微版本级回归和客户效果验收。
- **不能宣称**：已经替换 OpenClaw、已经接入华微内网 MCP、已经完成生产切换或已经证明 Token/成本优于原方案。
