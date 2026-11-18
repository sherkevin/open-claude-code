# 华微 MCP 企业统一入口工程进度

## 1. 工作区与基线

- 工作树：`/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118`
- 分支：`feat/huawi-enterprise-bridge-20261118`
- 基线：`476ff14f9a4663512925e5b14ba29a5729effdce`
- 本轮固定源码提交：`3d0d06cd87268ce61fc2b7c33b18a679a2d12d66`
- 共享主工作树没有切换分支、重置、清理或覆盖用户文件。
- 所有工程、构建缓存、运行日志、临时文件和安装包都位于 `/media/data3/dengkw/`。

## 2. 已完成实现

- 企业集成凭据 K 的创建、列表、检查、过期、轮换、撤销和连接器/来源绑定；
- 一次性签发 K 和可直接导入开物的标准 `mcpServers` JSON；
- 公网 MCP Edge 的 K 校验、来源授权、会话绑定、增量 SSE 和撤销重检；
- 每个连接器一个专属官方 `frps`、云端 visitor、数据库来源路由和撤销感知守护；
- 复用控制面根派生的 relay token R 和逐来源 STCP token T，不把明文写入普通数据库字段；
- 本机 authproxy 的增量 SSE 和多行 `data` 单次输出修复；
- 华微 HMAC 固定离线向量、错误 key/时钟/身份头测试，并移除无客户协议依据的 16 字节密钥下限；
- 官方 FRP 0.61.1 的 WSS/TLS 配置，包括服务端和客户端两个独立可信 CA 路径；
- Windows 机器级 DPAPI、严格数据目录 ACL、升级备份、旧明文迁移和服务重启代码；
- 部署模板、Nginx HTTPS/WSS 示例、回滚说明和客户本机填写说明；
- 更新旧 `AT_user`/OAuth Token Exchange 前置范围，防止解除本轮企业统一入口。

## 3. 本轮新增提交

| 提交 | 内容 |
| --- | --- |
| `99fca7a` | 增加 WSS/TLS 配置、浏览器验收脚本和部署模板 |
| `f9d4e07` | 将端到端证据绑定到源码提交和源码脏状态 |
| `3d0d06c` | 将源码脏检查限定在连接器、服务端、脚本和部署代码 |

本轮固定源码是 `3d0d06c`。后续文档提交只修改公开证据和交付说明，不再修改连接器、服务端、脚本或部署代码。

## 4. 已执行验证

### 本机 TCP 合成链路

- 证据：`e2e-synthetic-latest.json`
- 运行目录：`.runtime/huawi-enterprise-e2e-20261118T122456Z`
- 15 项通过，耗时 21.939 秒。
- 官方 FRP 0.61.1、visitor、连接器、Edge、控制面和合成 MCP 全部参与。
- 导入只发现 `lookup_order`，完整协议返回 `SYNTHETIC-ORDER-42`。
- 错误 K、跨来源、未批准工具、错误 relay/STCP 凭据均失败关闭。
- SSE 首事件 0.149 秒；frps 强杀后 2.427 秒恢复；正常停机后 503；撤销后 0.502 秒停止专属实例。

### 本机 HTTPS/WSS 回环

- 证据：`e2e-tls-wss-latest.json`
- 运行目录：`.runtime/huawi-enterprise-e2e-20261118T122524Z`
- 8 项通过，耗时 15.501 秒。
- 使用临时 CA、Nginx 1.18、官方 FRP 0.61.1、WSS、Edge 和合成 MCP。
- HTTPS 导入和完整 MCP 生命周期通过，返回 `SYNTHETIC-ORDER-42`。
- 错误 CA、错误 K、未知来源、未批准工具均被拒绝。
- Nginx 记录到两个 WebSocket `101`。

### 实际开物网页对话

- 证据：`e2e-chat-browser-latest.json`
- 运行目录：`.runtime/huawi-enterprise-e2e-20261118T122545Z`
- 5 项通过，耗时 45.969 秒。
- 实际开物网页完成隔离登录、JSON 导入、工具发现和绑定。
- 真实模型消息恰好触发一次 `lookup_order` 调用，参数 `{"sku":"SKU-1001"}`，成功耗时 771 毫秒。
- 最终回答包含 `SYNTHETIC-ORDER-42`。
- 网页模型为 `dano-v2.1`，但通过学校云回环测试适配器 `127.0.0.1:3425`，不是腾讯云生产模型路由。
- 截图保存在运行目录 `browser-screenshots/`。

### 自动化与候选包

- Go 全量测试、`go vet`、Python `45 passed`、`gofmt`、脚本语法检查、Nginx 模板语法检查均通过。
- 候选包版本 `1.3.0`，源码 `3d0d06c`，固定构建时间 `2026-11-18T12:24:52Z`。
- Linux 包 SHA-256：`f19b31b959f8cfd38a3234bc0a519c2a8acf1877397f24b80657b6a6480596cd`
- Windows 包 SHA-256：`705517ecde9d5ac72bbb30b24c523b607e108203681d79ba16ff69094ea8f5c2`
- 两次独立 `git archive` 构建逐字节一致；包内哈希、ZIP/TAR 读取、二进制格式、SBOM 和漏洞扫描通过。

## 5. 明确未完成

- Windows Server 2019 实机安装、DPAPI、ACL、服务重启、整机重启、升级和卸载。用户当前要求先不做 Windows 匹配，因此没有启动远程授权或设备操作。
- 模拟网络分区和长时间高并发容量测试。
- 真实公网 DNS、正式证书、腾讯云 Edge、Nginx、专属 frps 和 visitor 部署。
- 腾讯云生产模型路由。
- 华微真实业务 MCP、客户凭据和真实业务数据。

最终验收口径见 `ACCEPTANCE_MATRIX.md`，测试细节见 `TEST_REPORT.md`，部署动作见 `CLOUD_DEPLOYMENT.md` 和 `DELIVERY.md`。
