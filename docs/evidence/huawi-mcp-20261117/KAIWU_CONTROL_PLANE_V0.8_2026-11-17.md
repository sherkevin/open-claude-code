# 开物内网连接器最小控制面 v0.8

日期：2026-11-17

范围：合成控制面、设备身份、来源元数据登记、心跳、排空和撤销。

## 1. 结论

本阶段完成的是可测试的最小控制面，不是完整云端 C 段：

- `AGENT_KAIWU_CONTROL_ENABLED` 默认 `false`；关闭时管理面和设备面均失败关闭。
- 管理员接入码与连接器设备身份完全分离。
- 设备私钥只在本机生成，设备请求使用 Ed25519 签名。
- 云端只保存设备公钥、设备密钥编号、设备令牌哈希、接入码哈希和衍生秘密哈希。
- 来源登记只接受不透明编号、客户本机批准的工具、schema 摘要、目标绑定摘要和传输类型。
- 期望状态不包含客户内网 URL、主机、端口、路径或凭据；设备客户端严格拒绝未知字段。
- 心跳必须显式包含 `source_ids` 和 `last_error`，空值不能省略。
- drain 和 revoke 递增持久化 revision；revoke 后旧设备令牌立即失效。
- 本机撤销先于云端通知完成，云端不可达不会复活设备身份。

仍未完成：

- 真实 relay/visitor/Edge。
- OAuth Token Exchange、短期 `A`、一次性 `E` 和逐请求身份映射。
- grant 管理页面和映射生命周期页面。
- Windows DPAPI/CNG 设备私钥保护、代码签名和 Server 2019 实机验收。
- 真实客户 MCP、客户凭据、测试 tenant/source 和业务结果匹配。

因此当前结论只能是“最小控制面和本机诊断工具具备合成验证”，不能写成“平台端到端已闭环”。

## 2. 服务端路由

路由前缀：`/api/kaiwu`

| 方法 | 路径 | 认证 | 作用 |
| --- | --- | --- | --- |
| `GET` | `/v1/health` | 无秘密 | 返回控制面是否具备启用条件 |
| `POST` | `/v1/enrollments` | 独立管理员 Bearer | 创建一次性接入码，明文只返回一次 |
| `POST` | `/v1/enrollments/redeem` | 一次性接入码 + 设备公钥 | 绑定设备公钥并返回设备令牌 |
| `GET` | `/v1/connectors` | 独立管理员 Bearer | 查看不含秘密的连接器状态 |
| `GET` | `/v1/connectors/{id}/desired-state` | 设备令牌 + Ed25519 签名 | 返回严格期望状态 |
| `GET` | `/v1/connectors/{id}/transport-secrets` | 设备令牌 + Ed25519 签名 | 返回 `no-store` 通道材料 |
| `POST` | `/v1/sources` | 设备令牌 + Ed25519 签名 | 幂等登记来源公开元数据 |
| `PUT` | `/v1/sources/{id}/registration` | 设备令牌 + Ed25519 签名 | 同一配置幂等；变化返回冲突并要求重新审核 |
| `POST` | `/v1/connectors/{id}/heartbeat` | 设备令牌 + Ed25519 签名 | 保存完整心跳 |
| `POST` | `/v1/connectors/{id}/drain` | 独立管理员 Bearer | 排空，拒绝新通道 |
| `POST` | `/v1/connectors/{id}/resume` | 独立管理员 Bearer | 恢复来源 |
| `POST` | `/v1/connectors/{id}/revoke` | 独立管理员 Bearer | 管理员撤销 |
| `POST` | `/v1/connectors/{id}/self-revoke` | 设备令牌 + Ed25519 签名 | 本机紧急撤销后的云端通知 |

## 3. 设备签名

每个设备请求都带：

- `Authorization: Bearer <device_token>`
- `X-Kaiwu-Device-Key: <sha256(raw_ed25519_public_key)>`
- `X-Kaiwu-Timestamp: <unix_seconds>`
- `X-Kaiwu-Nonce: <24 random bytes base64url>`
- `X-Kaiwu-Signature: <Ed25519(base64url)>`

签名原文为：

```text
<timestamp>\n
<nonce>\n
<HTTP_METHOD>\n
<EXACT_PATH>\n
<SHA256(request_body)>\n
```

时间窗为正负 60 秒。服务端在同一事务语义下持久化 nonce，重复 nonce、错误公钥、路径或正文被替换、时间过期均拒绝。

## 4. 数据与秘密

数据库新增表：

- `kaiwu_enrollments`
- `kaiwu_connectors`
- `kaiwu_sources`
- `kaiwu_device_nonces`
- `kaiwu_heartbeats`
- `kaiwu_control_audit`

接入码和设备令牌只保存 SHA-256 哈希。试验阶段 relay 和 STCP 材料由独立管理员令牌做域分离 HMAC 派生，数据库只保存派生结果的哈希；管理员令牌不进入响应、日志或审计。该派生方案只用于受控试验，正式版必须接入 KMS/密钥管理系统并支持轮换。

## 5. 配置

```text
AGENT_KAIWU_CONTROL_ENABLED=false
AGENT_KAIWU_CONTROL_ADMIN_TOKEN_FILE=/path/to/dedicated-admin-token
AGENT_KAIWU_CONTROL_RELAY_ADDRESS=relay.example.com
AGENT_KAIWU_CONTROL_RELAY_PORT=7443
AGENT_KAIWU_CONTROL_RELAY_PROTOCOL=wss
AGENT_KAIWU_CONTROL_RELAY_TLS_ENABLED=true
AGENT_KAIWU_CONTROL_RELAY_TLS_SERVER_NAME=relay.example.com
AGENT_KAIWU_CONTROL_EDGE_USER=kaiwu-edge
AGENT_KAIWU_CONTROL_EDGE_PUBLIC_KEY=<Edge public key only>
```

启用条件不满足、令牌文件不可读或 relay 地址/端口缺失时，控制面返回 `503 CONTROL_PLANE_DISABLED`，不会降级到匿名或共享令牌模式。

## 6. 合成测试

服务端：

```bash
cd server
../.venv/bin/python -m pytest -q tests/test_kaiwu_control.py
```

覆盖：

- 默认关闭。
- 一次性接入码和重复使用拒绝。
- 管理员/设备凭据平面隔离。
- 正确、错误、过期和重放设备签名。
- 来源元数据登记、未知危险字段拒绝、来源冲突。
- `desired-state` 不返回内网目标。
- `transport-secrets` 使用 `no-store` 且不返回设备令牌或 `Authorization`。
- 心跳空数组、空字符串必填和缺字段拒绝。
- drain、resume、revoke 和撤销后旧令牌失效。
- 审计不包含管理员令牌、设备令牌或 relay 令牌。

连接器：

```bash
cd connector
PATH=/media/data3/dengkw/toolchains/go1.27.4/bin:$PATH go test ./...
PATH=/media/data3/dengkw/toolchains/go1.27.4/bin:$PATH go test -race ./...
PATH=/media/data3/dengkw/toolchains/go1.27.4/bin:$PATH go vet ./...
```

覆盖：

- Ed25519 设备密钥持久化、文件权限和签名验证。
- 请求签名头、精确路径和正文。
- 严格拒绝云端下发 `local_host/local_port`。
- 空心跳字段显式序列化。
- 来源登记不包含客户 URL、主机、端口或凭据。
- 本地优先撤销、撤销墓碑和云端故障下不复活。
- `draining=true` 时不启动新通道。
- 未获云端批准的本地来源不进入 frpc。

## 7. 验收边界

本文件中的“已实现”只指代码存在且合成测试通过。以下证据必须另行补齐后才能升级：

1. relay/visitor/Edge 合成链路。
2. Windows Server 2019 安装、服务、整机重启、安全软件、升级和卸载。
3. DPAPI/CNG 私钥保护和专用服务身份。
4. 客户测试 MCP 的固定查询和预期业务结果。
5. 恶意软件扫描、秘密扫描、独立重建和正式发布签名。
