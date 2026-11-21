# 连接器 → 云端控制面接口契约

以 `kaiwu-connector 1.0.1`（commit `89c38b2`）的 `internal/cloud/client.go` 为准。连接器只使用设备身份调用这些接口，不接触平台调用凭据 A 或客户业务凭据 B。

## 0. 通用约定

- Base URL：机器环境变量 `KAIWU_CLOUD_BASE`，当前默认 `https://flintw.com/projects/kaiwu/api`。连接器直接在这个 base 后拼 `/v1/...`。
- 认证：除 `redeem` 外，所有请求带 `Authorization: Bearer <device_token>`。
- 请求/响应：`Content-Type: application/json`，响应 JSON。
- 错误格式（二选一，连接器两种都兼容）：
  ```json
  {"error":{"code":"SOURCE_CONFIG_CONFLICT","message":"...","trace_id":"...","retryable":false}}
  ```
  或
  ```json
  {"code":"SOURCE_CONFIG_CONFLICT","message":"...","trace_id":"...","retryable":false}
  ```
- 超时：连接器 HTTP 客户端 20 秒；`desired-state`/`transport-secrets` 在 `Connect` 的 90 秒上下文内。
- 连接器不自动重放 `tools/call`；网络失败按明确失败或结果不确定处理。

## 1. `POST /v1/enrollments/redeem`

用途：一次性接入码兑换设备身份。**不需要 device token。**

请求：

```json
{
  "code": "<one-time-code>",
  "device_public_key": "<optional>"
}
```

响应 `Identity`：

```json
{
  "connector_id": "conn_01",
  "tenant_id": "tenant01",
  "tenant_name": "华微",
  "device_token": "<high-entropy-device-token>",
  "relay": {
    "address": "relay-tenant01.example.com",
    "port": 7443,
    "user": "tenant01",
    "token": "<relay-token>",
    "protocol": "tcp",
    "tls": {
      "enabled": true,
      "server_name": "relay-tenant01.example.com",
      "trusted_ca_file": "C:\\ProgramData\\Kaiwu\\Connector\\relay-ca.crt",
      "cert_file": "C:\\ProgramData\\Kaiwu\\Connector\\device.crt",
      "key_file": "C:\\ProgramData\\Kaiwu\\Connector\\device.key"
    }
  }
}
```

要求：

- `device_token` 只代表设备，不能读取其他租户来源；不要与 A/B 混用。
- 接入码单次使用、过期返回明确错误。
- `relay` 可以是占位，后续 `desired-state` 会覆盖；但连接器会先保存它。
- 如果返回的 `connector_id` 或 `device_token` 为空，连接器会报“云端未返回有效的设备身份”。

## 2. `GET /v1/connectors/{connector_id}/desired-state`

用途：拉取期望状态。认证：device token。

响应 `DesiredState`：

```json
{
  "revision": 3,
  "relay": {
    "address": "relay-tenant01.example.com",
    "port": 7443,
    "user": "tenant01",
    "token": "<relay-token>",
    "protocol": "tcp",
    "tls": {
      "enabled": true,
      "server_name": "relay-tenant01.example.com",
      "trusted_ca_file": "C:\\ProgramData\\Kaiwu\\Connector\\relay-ca.crt",
      "cert_file": "C:\\ProgramData\\Kaiwu\\Connector\\device.crt",
      "key_file": "C:\\ProgramData\\Kaiwu\\Connector\\device.key"
    }
  },
  "sources": [
    {
      "source_id": "928b0c97",
      "display_name": "华微库存演示",
      "local_host": "10.0.0.8",
      "local_port": 8000,
      "stcp_secret": "<source-stcp-secret>",
      "allow_users": ["tenant01"]
    }
  ],
  "revoked": false,
  "draining": false
}
```

要求：

- `sources[].source_id` 必须与连接器本地导入的 source_id 一致（见第 6 节），否则 frpc 代理无法匹配。
- `stcp_secret` 用于 frpc STCP；每个 source 独立。
- `local_host`/`local_port` 是连接器要转发的内网目标；云端不能静默把它替换成另一个地址。
- `revoked=true` 时连接器进入 REVOKED，不再自动重连。
- `draining=true` 时连接器应停止接收新调用（当前 1.0.1 只读取字段，排空行为由后续版本补齐）。

## 3. `GET /v1/connectors/{connector_id}/transport-secrets`

用途：只返回通道材料。认证：device token。

响应与 `DesiredState` 同形；连接器会把 `sources` 按 `source_id` 合并，更新 `stcp_secret` 和 `allow_users`。

要求：

- 不返回 A 或 B。
- 响应头建议 `Cache-Control: no-store`。
- 如果这个接口失败，连接器会继续用 `desired-state` 里的材料（`Connect` 的实现是 best-effort）。

## 4. `POST /v1/connectors/{connector_id}/heartbeat`

用途：上报运行状态。认证：device token。连接器每 30 秒发一次。

请求 `Heartbeat`：

```json
{
  "state": "READY",
  "version": "1.0.1 (commit 89c38b2, built 2026-11-17T05:51:52Z)",
  "observed_at": "2026-11-17T06:00:00Z",
  "source_ids": ["928b0c97"],
  "last_error": ""
}
```

状态值包括：`UNPAIRED`、`CONFIGURING`、`CONNECTING`、`READY`、`DEGRADED_UPSTREAM`、`DEGRADED_RELAY`、`PAUSED`、`DRAINING`、`REVOKED`。响应 2xx 即可。

## 5. `POST /v1/connectors/{connector_id}/drain|resume|revoke`

- `POST /v1/connectors/{id}/drain`：请求排空；空 body；2xx。
- `POST /v1/connectors/{id}/resume`：请求恢复；空 body；2xx。
- `POST /v1/connectors/{id}/revoke`：撤销设备；空 body；2xx。连接器随后会删除本地 `identity.json`，进入 REVOKED。

## 6. Source 注册（当前连接器未调用，必须补）

开发文档建议：

- `POST /v1/sources`：创建设备绑定的 source 草稿，返回 `source_id`。
- `PUT /v1/sources/{id}/registration`：登记 source 元数据（transport、upstream metadata、tool allowlist）。
- `POST /v1/sources/{id}/grants`：签发 A，绑定范围和到期时间；一次性返回 A 和完整平台 JSON。
- `POST /v1/sources/{id}/platform-config`：导出含 A 占位符的模板。

**当前 1.0.1 连接器没有调用这些接口**：`ImportConfig` 只在本地保存 source，`Connect` 只调用 `desired-state`/`transport-secrets`/`heartbeat`。这是 C 段最大的契约缺口。

连接器本地 `source_id` 的生成方式：显示名的稳定哈希（例如“华微库存演示”→`928b0c97`）。请云端先决策：

- **方案 A（推荐）**：连接器导入后调用 `POST /v1/sources`，把本地 `source_id`、显示名、transport、工具 allowlist 注册给云端；云端接受。
- **方案 B**：云端分配 `source_id`，连接器改为使用云端返回的 ID（需要改客户端）。

在方案确定前，`desired-state` 里的 `source_id` 无法与本地 source 匹配，frpc 会因为没有 STCP secret 而无法建立代理。

## 7. Relay / visitor / STCP 语义

- 连接器用 `relay.address/port/user/token/tls` 生成 `frpc.toml`，每个 source 一个 STCP proxy：
  - `name = source_id`
  - `type = stcp`
  - `secretKey = stcp_secret`
  - `localIP/localPort = source.local_host/local_port`
  - `allowUsers = allow_users`
- 云端需要为每个 source 建立 visitor，供 Edge 访问；visitor 只监听 Edge 可达的 loopback/私有地址。
- 连接器始终主动出站；云端不主动进客户内网。

## 8. Edge / grant 语义

- Edge URL 形如：`https://<edge>/sources/<source_id>/mcp`。
- 平台调用凭据 A 由云端签发；连接器导出时只放 `Bearer <EDGE_ACCESS_TOKEN>` 占位符，不接触 A。
- Edge 需要：
  - 验证 A 与 tenant/source/grant 绑定；
  - 校验 `tools/call` 的工具名在 source/grant 允许列表交集内；
  - 去掉入口 A，按 source 配置附加客户上游 B；
  - 保留 JSON/SSE、状态码和会话头；
  - 源不可达返回 `503 SOURCE_UNAVAILABLE`；上游认证失败返回 `502 UPSTREAM_AUTH_FAILED`；超时返回 `504 TOOL_TIMEOUT`；越权返回 `403 POLICY_DENIED`。
- 连接器导出函数：`config.BuildPlatformConfig(sources, edgeBase, token)`；如果 `token` 为空，用 `Bearer <EDGE_ACCESS_TOKEN>`。

## 9. 错误映射 / 可观测性

- 入口 A 无效：401；来源/方法越权：403；限流：413/429；通道不可达：503；上游 B 失败：502；超时：504。
- 响应头/日志包含 `trace_id`，不记录 A/B、请求头、完整正文。
- 控制面错误使用第 0 节格式；不要把所有 MCP 错误改写成控制面错误。

## 10. 联调顺序

1. 云端提供测试 base URL、一次性接入码、relay/visitor。
2. 连接器 `redeem` → 保存 `identity.json`。
3. 连接器导入一个 HTTP MCP source → 本地诊断通过。
4. 云端注册 source（方案 A/B）→ `desired-state` 返回匹配的 `source_id` 和 STCP secret。
5. 连接器 `Connect` → frpc 出站 → 云端 visitor 建立。
6. 云端签发 A → 平台导入 JSON → 调用 `manual_search` 和一条销售订单查询。
7. 停连接器 → 503；恢复 → 可用；撤销 → 拒绝。
