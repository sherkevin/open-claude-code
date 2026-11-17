# 华微内网 MCP 接入 · 云端工程师交接说明

- 日期：2026-11-17
- 交接人：邓凯文
- 目的：让云端同事了解当前进度、连接器已经实现的云端契约，以及 C 段需要补齐的内容。
- 重要：本文件夹里的材料是“连接器视角”的事实，不是云端设计稿；请以 `01_云端接口契约.md` 为准实现服务端。

## 1. 一句话目标

在华微内网部署一个连接器，由它主动出站连到开物云端；开物 Agent 平台通过云端 Edge 调用华微现有的 MCP，读取他们允许的数据。华微不需要开公网入站。

```text
华微内网：MCP Server ← 连接器（主动出站长连接）→ 开物云端：relay/visitor/Edge/Agent 平台
```

## 2. 当前进度

- **Windows 连接器 1.0.1：本地 A/B 已通过。** 安装服务、导入 HTTP MCP、诊断、工具发现、错误凭据拒绝、服务重启恢复、卸载清理都验证过。见 `03_KaiwuConnectorWindowsAcceptanceReport-1.0.1.md`。
- **云端 C 段：未闭环。** 连接器目前只能做本地导入和诊断；平台还不能通过 Edge 调用华微 MCP。
- **华微环境：** 他们的 MCP 在 OpenClaw Linux 服务器的“MCP 身份代理”后面，代理只监听 `127.0.0.1`；令牌按可信消息上下文签发；已确认三个只读工具，但没有线上地址、服务令牌或完整 schema。见 `02_华微灵犀智能体接口接入技术说明.docx`。

## 3. 云端需要补齐的 C 段

1. **接入码兑换**：`POST /v1/enrollments/redeem`，返回设备身份和 relay 配置。
2. **设备身份与 relay**：`connector_id`、`device_token`、relay 的 address/port/user/token/TLS 材料。
3. **Source 注册**：连接器目前只在本地导入 source，没有调用云端注册接口；这是 C 段最大的契约缺口。需要确定 `source_id` 的归属（见第 5 节）。
4. **期望状态 / 通道材料**：`GET /v1/connectors/{id}/desired-state` 和 `/transport-secrets`，返回 relay、sources（`source_id`、`local_host`、`local_port`、`stcp_secret`、`allow_users`）、`revoked`、`draining`。
5. **Grant / 平台 JSON**：签发平台调用凭据 A，生成 `mcpServers` JSON，Edge URL 为 `<edge>/sources/<source_id>/mcp`。
6. **Edge 路由**：验证 A、按 source 路由到 visitor、把 A 换成客户上游凭据 B、保留 JSON/SSE、实现 401/403/503/504 语义。
7. **心跳 / 排空 / 撤销**：记录设备状态，支持 drain/resume/revoke。

## 4. 连接器实际调用的接口

以 `01_云端接口契约.md` 为准。Base URL 来自机器环境变量 `KAIWU_CLOUD_BASE`，当前默认：

```text
https://flintw.com/projects/kaiwu/api
```

请确认正式地址是复数 `projects` 还是单数 `project`；连接器会直接在这个 base 后面拼 `/v1/...`。

## 5. Source ID 归属（请云端先决策）

连接器在本地导入时，会按显示名生成一个稳定的 `source_id`（例如 `928b0c97`）。云端 `desired-state` 必须返回**相同的 source_id** 和对应的 STCP secret，否则连接器生成的 frpc 代理无法匹配。

两种方案：

- **方案 A（推荐，改动小）**：连接器导入后调用 `POST /v1/sources` 注册本地 `source_id`、显示名、传输方式、工具 allowlist；云端接受并存储。
- **方案 B**：云端分配 `source_id`；连接器改为使用云端返回的 ID（需要改客户端）。

请云端确认选哪种，并把接口字段告诉连接器侧。

## 6. 华微环境对 C 的影响

- 身份代理只监听 `127.0.0.1`：连接器要么装在 OpenClaw 同一台 Linux 服务器上，要么让代理监听内网地址 + TLS。否则 Windows 连接器无法访问。
- 代理令牌是可信上下文签发的：连接器需要独立的服务身份（固定服务令牌 / mTLS / 令牌交换）。不能把聊天 Bearer 直接复制给连接器。
- 这两条不解决，C 段即使做好，连接器也无法访问华微 MCP。

## 7. 需要云端同事回复/交付

1. 正式控制面 base URL 和一个测试接入码。
2. 按 `01_云端接口契约.md` 实现的最小控制面。
3. relay/visitor/Edge 测试环境（域名、端口、TLS 材料）。
4. `source_id` 归属决策（方案 A 或 B）。
5. grant/token A 的签发方式和一份 `mcpServers` JSON 样例。
6. 一个测试 tenant/source，用于端到端联调。

## 8. 文件夹内容

- `00_云端工程师交接说明.md`（本文件）
- `01_云端接口契约.md`（连接器实际调用的接口和 JSON 示例）
- `02_华微灵犀智能体接口接入技术说明.docx`（客户提供的核实清单）
- `03_KaiwuConnectorWindowsAcceptanceReport-1.0.1.md`（本地 A/B 验收报告）
- `04_test-summary-1.0.1.json`（测试摘要）
- `05_mcp-platform-export-1.0.1.json`（连接器导出的平台 JSON 样例，token 仍是占位符）
- `06_kaiwu-connector-1.0.1-windows-amd64.zip`（Windows 安装包，可选）

## 9. C 段验收标准

1. 连接器用真实接入码兑换设备身份，服务重启后身份保留。
2. 连接器拿到 relay/visitor 参数，frpc 主动出站并保持连接。
3. 平台导入 `mcpServers` JSON，能调用 `manual_search` 和一条销售订单/客访日志查询。
4. 停掉连接器，平台在 5 秒内得到 `503 SOURCE_UNAVAILABLE`；恢复后调用恢复。
5. 撤销连接器后，平台在新窗口内被拒绝；在途流按约定终止。
6. 平台导出配置只包含 Edge 地址和凭据 A，不包含华微内网地址或上游 B。
