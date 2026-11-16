# 开物内网连接器（Kaiwu Intranet Connector）

部署在客户内网、把内网 MCP 服务安全接入开物平台的连接器。只做出站连接，不需要客户开放任何入站端口。

面向客户现场的实施与运维文档见 [`docs/交付与运维说明.md`](docs/交付与运维说明.md)。本文件面向开发和构建。

## 架构

```
本机界面（127.0.0.1:17872）
        │
   agent 编排层 ── cloud 控制面客户端（绑定/期望状态/中继参数/心跳/撤销）
        │        ── config 导入导出（mcpServers → 内部来源 → 平台配置）
        │        ── diag 分层诊断（网络 → TLS → MCP 握手 → 工具列表）
        │        ── frpc 配置生成 + supervisor 进程守护
        ▼
   frpc（固定 0.61.1）── STCP 隧道 ──▶ 云端 MCP Edge ──▶ 客户内网 MCP
```

凭据分离是本项目的硬约束：

- 设备身份只用于控制面，不能读取平台授权 A 或客户 MCP 凭据 B；
- 平台授权 A 不能当作客户 MCP 凭据 B 使用；
- 客户 MCP 凭据 B 只保存在本机并只由 Edge 使用，不写入导出配置。

## 目录

| 路径 | 说明 |
| --- | --- |
| `cmd/connector` | 命令行入口：`serve` / `import` / `export` / `diagnose` / `version` |
| `internal/config` | 宿主 MCP 配置解析、内部来源模型、平台配置生成 |
| `internal/state` | 连接器状态机与中文状态标签 |
| `internal/frpc` | frpc TOML 生成与 0600 原子写入 |
| `internal/supervisor` | frpc 进程守护，指数退避 1s→30s 带抖动 |
| `internal/mcp` | 最小 MCP 客户端（initialize / tools/list / tools/call，SSE 解析） |
| `internal/diag` | 分层诊断与错误码 |
| `internal/cloud` | 控制面 API 客户端 |
| `internal/agent` | 运行主体，串联以上模块 |
| `internal/web` | 仅回环的本机界面与 API，会话令牌 + Host/Origin 校验 |
| `packaging/` | 安装包素材：systemd 单元、安装卸载脚本、地址模板、frpc 二进制 |
| `scripts/build.sh` | 生成 Linux/Windows 交付包 |

## 构建

需要 Go 1.22+、`zip`，以及 `packaging/bin/` 下的 frpc 二进制（`frpc-linux-amd64`、`frpc-windows-amd64.exe`）。这两个第三方二进制不纳入 git，获取方式见 `packaging/bin/README.md`。

```bash
cd connector
go test ./...
bash scripts/build.sh            # 版本默认 1.0.0，可用 VERSION=1.0.1 覆盖
```

产物在 `dist/`：

- `kaiwu-connector-<版本>-linux-amd64.tar.gz`
- `kaiwu-connector-<版本>-windows-amd64.zip`

构建使用 `CGO_ENABLED=0` 交叉编译，`-trimpath`，并通过 ldflags 注入版本、提交号、构建时间。

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
| `--cloud` | `KAIWU_CLOUD_BASE` | 无（正式环境 `https://flintw.com/projects/kaiwu/api`，以交付提供为准） |
| `--edge` | `KAIWU_EDGE_BASE` | 无（正式环境 `https://flintw.com/projects/kaiwu/mcp`，以交付提供为准） |
| `--frpc` | — | 与主程序同目录，或 `<数据目录>/bin/frpc` |

命令行：

```bash
connector import --file mcp.json          # 只校验并打印导入报告
connector export --data-dir <目录>        # 打印平台可导入配置
connector diagnose --data-dir <目录>      # 执行一次分层诊断，JSON 输出
connector version
```

## 控制面接口

连接器按以下接口与云端交互（均为 `Authorization: Bearer <设备令牌>`）：

- `POST /v1/enrollments/redeem`：一次性接入码兑换设备身份
- `GET  /v1/connectors/{id}/desired-state`：期望状态、来源、中继参数
- `GET  /v1/connectors/{id}/transport-secrets`：通道密钥
- `POST /v1/connectors/{id}/heartbeat`：30 秒心跳
- `POST /v1/connectors/{id}/drain|resume|revoke`：优雅停止、恢复、撤销

## 平台导入契约

平台只接受这一种结构，且 `type` 只支持 `http` / `streamable-http`、请求头只支持 `Authorization`：

```json
{"mcpServers":{"名称":{"type":"http","url":"https://<edge>/sources/<id>/mcp","headers":{"Authorization":"Bearer <token>"}}}}
```

## frpc 许可

随包分发的 frpc 为 0.61.1，来自 [fatedier/frp](https://github.com/fatedier/frp)，遵循 Apache License 2.0。分发时保留 `版本信息.txt` 中的声明。
