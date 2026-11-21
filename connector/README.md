# 开物内网连接器（Kaiwu Intranet Connector）

部署在客户内网、把内网 MCP 服务安全接入开物平台的连接器。只做出站连接，不需要客户开放任何入站端口。

面向客户现场的实施与运维文档见 [`docs/交付与运维说明.md`](docs/交付与运维说明.md)。本文件面向开发和构建。

> 本目录随 `1.3.14` 内部验收候选构建。一个 Windows 连接器对应一个专属原版 frps 实例和一组云端 visitor 路由；开物侧使用云端管理员一次性签发的企业集成凭据 K 和标准 MCP JSON。安装包预配置我方已验证的公网控制面和 MCP Edge；首次使用在本机页面依次检查内置示例、绑定企业并验证公网，再填写服务名称、MCP 地址和认证参数，读取并勾选允许的工具，点击“保存并连接”。原始 MCP JSON 导入在高级选项中。

## 架构

```
本机界面（127.0.0.1:17872）
        │
   agent 编排层 ── cloud 控制面客户端（绑定/期望状态/中继参数/心跳/撤销）
        │        ── 普通表单/单来源保存（高级模式仍兼容 mcpServers 导入）
        │        ── diag 分层诊断（网络 → TLS → MCP 握手 → 工具列表）
        │        ── frpc 配置生成 + supervisor 进程守护
        │        ── 每来源回环认证代理（固定目标、工具白名单、Bearer、HMAC）
        ▼
   frpc（固定 0.61.1）── STCP + 专属 frps ──▶ 云端 visitor
        ▲                                      │
        └──── 客户内网认证代理 ◀── MCP 请求 ── MCP Edge
                       │
                       ▼
                 客户内网 MCP
```

凭据分离是本项目的硬约束：

- 设备身份只用于控制面，不能读取企业集成凭据 K 或客户 MCP 凭据 B；
- 企业集成凭据 K 不能当作客户 MCP 凭据 B 使用；
- 客户 MCP 凭据 B 和动态签名密钥 S 只保存在本机，只由回环认证代理使用；
- Edge 和中继只能到达逐来源代理的 STCP 入口，不能改写固定上游目标。
- 企业集成凭据 K 只由开物调用我方 Edge；K 不转发到客户业务 MCP，B／S 不进入开物 JSON。

## 目录

| 路径 | 说明 |
| --- | --- |
| `cmd/connector` | 命令行入口：`serve` / `import` / `export` / `diagnose` / `version` |
| `internal/config` | 宿主 MCP 配置解析、内部来源模型、平台配置生成 |
| `internal/state` | 连接器状态机与中文状态标签 |
| `internal/frpc` | frpc TOML 生成；R／T 使用官方环境模板，不写入磁盘 |
| `internal/supervisor` | frpc 进程守护，指数退避 1s→30s 带抖动 |
| `internal/mcp` | 最小 MCP 客户端（initialize / tools/list / tools/call，SSE 解析） |
| `internal/diag` | 分层诊断与错误码 |
| `internal/cloud` | 控制面 API 客户端 |
| `internal/agent` | 运行主体，串联以上模块 |
| `internal/authproxy` | 回环认证代理、固定上游重建、工具交集、会话映射、增量 SSE 和响应过滤 |
| `internal/huaweiauth` | 华微渠道身份与 HMAC-SHA256 动态签名 |
| `internal/web` | 仅回环的本机界面与 API，会话令牌 + Host/Origin 校验 |
| `internal/secretfile` | Windows 机器级 DPAPI、秘密文件原子写入和旧明文迁移 |
| `packaging/` | 安装包素材：systemd 单元、安装卸载脚本、地址模板、frpc 二进制 |
| `scripts/build.sh` | 生成 Linux/Windows 交付包 |

## 构建

需要固定版本 Go `1.27.4`、Python 3、`zip`、NSIS 3.08、`unshare`、`cyclonedx-gomod v1.12.0`、`govulncheck v1.9.0`，以及 `packaging/bin/` 下的 frpc 二进制（`frpc-linux-amd64`、`frpc-windows-amd64.exe`）。第三方 frpc 获取和哈希要求见 `packaging/bin/README.md`。NSIS 通过 `MAKENSIS` 指定 `makensis` 路径，通过 `NSISDIR` 指定其数据目录；构建脚本会在私有挂载命名空间中把 NSIS 硬编码的 `/tmp` 临时文件映射到数据盘，避免根分区写满导致 SIGBUS。

如果工具不在 `PATH` 中，可显式指定：

```bash
CYCLONEDX_GOMOD=/path/to/cyclonedx-gomod \
GOVULNCHECK=/path/to/govulncheck \
bash scripts/build.sh
```

```bash
cd connector
go test ./...
bash scripts/build.sh            # 默认版本取自构建脚本，可用 VERSION=... 覆盖
```

产物在 `dist/`：

- `kaiwu-connector-<版本>-linux-amd64.tar.gz`
- `kaiwu-connector-<版本>-windows-amd64.zip`
- `kaiwu-connector-<版本>-windows-amd64-setup.exe`（客户可双击安装）

构建使用 `CGO_ENABLED=0` 交叉编译、`-trimpath`，并通过 ldflags 注入版本、提交号、构建时间。Windows 安装器使用 NSIS 3.08，复用同一套 Go 程序、官方未修改的 frpc 和服务注册脚本。构建会执行 `govulncheck`，发现已知漏洞时失败。包内生成 `SHA256SUMS`、CycloneDX 1.5 JSON SBOM、SBOM 工具警告与说明、漏洞扫描结果、Go 模块清单、二进制构建信息、构建日志、frp 许可证和 Go 标准库许可证；外层安装包、ZIP 和 TAR 的哈希写入 `dist/SHA256SUMS`。SBOM 和漏洞扫描是构建证据，不代表包内二进制已完成 Windows 实机、恶意软件或秘密扫描。

## 本地运行

```bash
go run ./cmd/connector serve --data-dir /tmp/kaiwu-connector --listen 127.0.0.1:17872
# 打开 http://127.0.0.1:17872/
```

常用参数与环境变量：

| 参数 | 环境变量 | 默认值 |
| --- | --- | --- |
| `--data-dir` | `KAIWU_CONNECTOR_DATA` | Linux `/var/lib/kaiwu-connector`；Windows `%ProgramData%\Kaiwu\Connector` |
| `--listen` | — | `127.0.0.1:17872` |
| `--cloud` | `KAIWU_CLOUD_BASE` | 无（当前预配置 `https://flintw.com/api/kaiwu`，以交付提供为准） |
| `--edge` | `KAIWU_EDGE_BASE` | 无（正式环境 `https://flintw.com/projects/kaiwu/mcp`，以交付提供为准） |
| `--frpc` | — | 与主程序同目录，或 `<数据目录>/bin/frpc` |

命令行：

```bash
connector import --file mcp.json          # 只校验并打印导入报告
connector export --data-dir <目录>        # 打印不含凭据的 Edge 地址预览
connector diagnose --data-dir <目录>      # 执行一次分层诊断，JSON 输出
connector version
```

## 控制面接口

连接器按以下接口与云端交互。设备请求同时携带 `Authorization: Bearer <设备令牌>`，并用本机 Ed25519 私钥签名 HTTP 方法、精确路径、正文摘要、时间戳和一次性 nonce：

- `POST /v1/enrollments/redeem`：一次性接入码兑换设备身份
- `POST /v1/sources`：登记不透明来源元数据、客户本机批准的工具，以及从真实 `tools/list` 规范化计算出的工具 schema 摘要
- `GET  /v1/connectors/{id}/desired-state`：期望状态、来源、中继参数
- `GET  /v1/connectors/{id}/transport-secrets`：通道密钥
- `GET  /v1/connectors/{id}/data-plane`：管理员读取专属 frps 与 visitor 配置材料
- `POST /v1/connectors/{id}/heartbeat`：30 秒心跳
- `POST /v1/connectors/{id}/self-revoke`：本机已撤销后通知云端

管理员的企业集成、drain、resume、revoke 和数据面材料接口使用独立管理员凭据，连接器设备令牌不能调用。

- `POST /v1/integrations`：创建、绑定来源并一次性返回 K 与完整开物 JSON
- `GET  /v1/integrations` / `GET  /v1/integrations/{id}`：只返回非秘密状态
- `POST /v1/integrations/{id}/rotate`：原子轮换 K 并重新签发 JSON
- `POST /v1/integrations/{id}/revoke`：立即撤销企业集成

## 平台导入契约

连接器本机“导出”只生成不含授权信息的 Edge 地址预览：

```json
{"mcpServers":{"名称":{"type":"http","url":"https://<edge>/sources/<id>/mcp"}}}
```

正式开物 JSON 由云端管理员创建企业集成时一次性生成：

```json
{"mcpServers":{"华微业务查询":{"type":"http","url":"https://<edge>/sources/<source_id>/mcp","headers":{"Authorization":"Bearer <企业集成凭据K>"}}}}
```

K 绑定企业、连接器和允许来源，支持过期、轮换和撤销；服务端只保存不可逆验证值。K 不是客户 Bearer，不转发到客户业务 MCP。设备令牌、客户 B／S 和隧道 R／T 均不得进入开物 JSON。

## Windows 秘密保护

设备身份、设备私钥、客户 Bearer、HMAC 密钥、认证配置和通道材料通过 Windows 机器级 DPAPI 加密后原子写入。安装脚本强制只允许 `SYSTEM` 和 `Administrators` 访问数据目录；ACL 设置或校验失败会中止安装。旧明文 JSON 在服务启动读取时原子迁移，迁移失败不会覆盖原文件。

## frpc 许可

随包分发的 frpc 为 0.61.1，来自 [fatedier/frp](https://github.com/fatedier/frp)，遵循 Apache License 2.0。分发时保留 `版本信息.txt` 中的声明。

构建产物还包含固定 Go 1.27.4 工具链的 `go-LICENSE.txt` 和 `go-PATENTS.txt`，用于说明静态链接进二进制的 Go 标准库许可。
