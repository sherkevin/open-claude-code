# OpenClaw 兼容替换调研证据（2026-11-12）

## 1. 证据范围

本记录只证明 OpenClaw 官方接口盘点、替换边界和迁移设计已完成，不证明知行工作台已经接入华微世纪的 OpenClaw 实例。调研使用官方公开文档和公开仓库；没有读取客户配置、真实业务数据、凭据或生产服务，也没有重启共享服务。

客户版本和实际入口尚未提供，因此版本级兼容性、客户渠道清单和客户工具策略仍是外部输入。

## 2. 官方接口核对

- 官方架构以一个长期运行的 Gateway 统一承载渠道、提供商、会话、路由和状态；默认监听本机回环地址，HTTP 与 WebSocket 共用 Gateway 端口。
- OpenAI 兼容接口包括 `POST /v1/chat/completions`、`GET /v1/models`、`GET /v1/models/{id}` 和 `POST /v1/embeddings`；OpenResponses 使用单独的 `POST /v1/responses`，默认需要显式启用。
- Gateway WebSocket 使用 `connect.challenge`、`connect`、`hello-ok` 握手；最小外部会话闭环需要 `chat.send`、`chat.history`、`chat.abort`、`sessions.*`、`agent` 和 `agent.wait`，并保留幂等键和生命周期事件。
- OpenClaw 官方 Agent Harness 文档把 Harness 定义为单轮低层执行器，而不是渠道、提供商或工具注册中心；该扩展面仍是 experimental，要求锁定版本并使用受信插件。
- MCP 支持标准的 Streamable HTTP、SSE 和 stdio；工具策略、权限和审批不能被兼容层绕过。MCP Relay 采用出站连接和配对/撤销，不要求把客户 Gateway 暴露到公网。

## 3. 当前产品事实

- `server/app/openai_compat.py` 是内部请求归一化模块，不是公开的 OpenClaw `/v1` 网关，也没有客户可用的租户令牌入口。
- 当前没有 OpenClaw 版本级 WebSocket 适配器，也没有把客户 Skill、插件和本地 MCP 自动映射到知行权限策略的生产连接器。
- 因此 `OC-T12` 在任务板登记为 `ready`，未取得 Cloud Code 执行回执，不写入虚假 `ST ID`，不标记为进行中或完成。

## 4. 目标与验收缺口

正式目标不是把内部归一化模块改名，而是：

1. 先提供隔离的 OpenAI Chat/Responses 兼容 facade，完成合成非流式、SSE、工具往返、会话继续、幂等、错误、取消和模型白名单合同。
2. 再提供锁定 OpenClaw 版本的受信 `AgentHarnessV2` 适配，把上下文、记忆、压缩、工具审批和安全事件交给知行；OpenClaw 保留渠道、身份和策略边缘。
3. 通过私网或单一出站连接器接入客户 MCP，保留工具白名单、最小权限、撤销、超时、取消、租户隔离和审计。
4. 使用旧 OpenClaw 地址作为一键回滚，不删除客户旧配置、不迁移真实会话、不把 Gateway 管理令牌转发给知行。

仍缺的证据：公开 `/v1` facade、版本级 WebSocket 合同、受信 Harness 插件、连接器断线/撤销回归、客户脱敏请求样例和基线对比。所有缺口已登记到 `docs/REQUIREMENTS_ALIGNMENT.md` 的 `OCLAW-001` 至 `OCLAW-005`。

## 5. 官方来源

- https://docs.openclaw.ai/start/why-openclaw
- https://docs.openclaw.ai/gateway/openai-http-api
- https://docs.openclaw.ai/gateway/openresponses-http-api
- https://docs.openclaw.ai/gateway/protocol
- https://docs.openclaw.ai/gateway/protocol/handshake
- https://docs.openclaw.ai/gateway/protocol/rpc-session-control
- https://docs.openclaw.ai/gateway/clients
- https://docs.openclaw.ai/plugins/mcp-relay
- https://docs.openclaw.ai/cli/mcp/serve
- https://github.com/openclaw/openclaw/blob/main/docs/concepts/architecture.md
- https://github.com/openclaw/openclaw/blob/main/docs/gateway/external-apps.md
- https://github.com/openclaw/openclaw/blob/main/docs/plugins/sdk-agent-harness.md
- https://github.com/openclaw/openclaw/blob/main/docs/tools/mcp.md
- https://github.com/openclaw/openclaw/releases

## 6. 边界声明

本记录不包含生产部署、服务重启、客户数据、客户凭据、付费模型调用或对外发布。它不能替代版本级联调和客户验收。
