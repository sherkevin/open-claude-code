# 华微 MCP 企业统一入口交付说明

## 1. 交付目的

华微的业务 MCP 在客户内网 Windows Server 2019 上，只能主动访问公网。开物平台要在公网提供企业统一入口，让员工通过真实网页对话调用这个内网 MCP，查询客户授权开放的只读业务数据。

本期交付物由三部分组成：

1. 客户 Windows 本机连接器，负责连接我方中继、调用客户内网 MCP，并在本机附加客户 Bearer 和华微 HMAC 身份头；
2. 我方云端企业 Edge、专属官方 frps、visitor 和 HTTPS/WSS 反向代理，负责公网入口、企业集成凭据 K 校验、来源绑定和撤销；
3. 标准 MCP JSON，包含我方 Edge 地址和企业集成 Bearer K，可直接导入开物网页。

客户内部 Bearer、HMAC 密钥、appid、channel、userid 和真实业务数据只留在客户本机或客户系统，不进入开物 JSON、普通日志或公开证据。

## 2. 当前已交付

| 对象 | 位置或标识 | 状态 |
| --- | --- | --- |
| 固定源码 | `3d0d06cd87268ce61fc2b7c33b18a679a2d12d66` | 已提交 |
| Linux 候选包 | `/media/data3/dengkw/releases/huawi-connector-1.3.0-3d0d06c/kaiwu-connector-1.3.0-linux-amd64.tar.gz` | 已构建、校验 |
| Windows 候选包 | `/media/data3/dengkw/releases/huawi-connector-1.3.0-3d0d06c/kaiwu-connector-1.3.0-windows-amd64.zip` | 已构建、格式校验；未实机验收 |
| 发布校验 | `/media/data3/dengkw/releases/huawi-connector-1.3.0-3d0d06c/SHA256SUMS` | 已生成、通过 |
| 验收矩阵 | `ACCEPTANCE_MATRIX.md` | 已更新 |
| 测试报告 | `TEST_REPORT.md` | 已更新 |
| 发布报告 | `RELEASE_REPORT.md` | 已更新 |
| 云端部署步骤 | `CLOUD_DEPLOYMENT.md` | 已更新 |
| 回滚步骤 | `ROLLBACK.md` | 已存在并保留 |

候选包 SHA-256：

- Linux：`f19b31b959f8cfd38a3234bc0a519c2a8acf1877397f24b80657b6a6480596cd`
- Windows：`705517ecde9d5ac72bbb30b24c523b607e108203681d79ba16ff69094ea8f5c2`

## 3. 已经验证到什么程度

### 已通过

- 连接器 Go 全量测试和 `go vet`；
- Kaiwu/import/resource Python 测试 `45 passed`；
- 本机 TCP 合成链路 15 项；
- 本机 HTTPS/WSS 回环链路 8 项，包括临时 CA、错误 CA 拒绝和 Nginx WebSocket `101`；
- 实际开物产品网页登录、JSON 导入、工具发现、模型消息和 `lookup_order` 工具调用；
- 固定合成 MCP 返回 `SYNTHETIC-ORDER-42`；
- 错误 K、跨来源、未批准工具、错误 relay/STCP 凭据、正常停机、强杀 frps、relay 重启和撤销；
- 候选包双构建逐字节一致、包内/外层哈希、ZIP/TAR、二进制格式、SBOM 和漏洞扫描。

### 未通过或未执行

- Windows Server 2019 实机安装、DPAPI、ACL、服务重启、整机重启、升级和卸载；
- 真实公网 DNS、正式证书、腾讯云 Edge、Nginx、专属 frps 和 visitor 部署；
- 腾讯云生产模型路由；
- 华微真实业务 MCP、客户秘密和真实数据验收。

## 4. 我方最小部署动作

1. 在受控服务器部署当前源码提交 `3d0d06c` 的控制面和 Edge，只启动一个 Edge worker。
2. 安装官方 frps/frpc 0.61.1，核验二进制哈希；为每个连接器准备专属 frps 实例和 loopback visitor。
3. 配置管理员根密钥、控制面环境、Edge 公开路径和私有的 frps bind 端口。
4. 配置公网域名与正式证书；用 Nginx 将 `/~!frp` 以 WebSocket/WSS 转到 frps private bind，将 `/projects/kaiwu/mcp/` 以 HTTPS 转到 Edge。
5. 创建企业集成，签发一次性 K 和标准 MCP JSON；只把 JSON 和 K 交给获授权的开物管理流程。
6. 从公网执行导入、initialize、tools/list、允许工具调用、错误 K、跨来源、撤销和断线检查，并记录正式环境证据。

具体命令和配置字段见 `CLOUD_DEPLOYMENT.md`。不要在没有备份和值班确认的情况下重启共享服务或修改现网域名。

## 5. 客户 Windows 本机未来只需填写

安装 Windows 候选包后，由客户管理员在本机回环页面填写：

1. 已获授权的业务 MCP 地址，例如 `https://svr39.bjhwsj.com/SysDevCore/mcp/sales_test`，最终以客户本机实际地址为准；
2. 客户 MCP 固定 Bearer；
3. 华微 HMAC-SHA256 密钥；
4. `appid`，按客户现有协议填写；当前协议通常为 `MCP`，以客户确认为准；
5. `channel`；
6. `userid`；
7. 允许工具列表，例如只允许业务只读工具。

客户还需要在我方提供的控制面完成一次性接入码绑定，并在本机检查连接、工具发现和认证代理结果。客户不需要理解或保存云端 R/T，也不需要把客户 Bearer、HMAC 密钥或渠道身份发给我方。

## 6. 开物导入和验收流程

1. 我方从管理员接口创建企业集成，取得一次性 K 和 `mcpServers` JSON；
2. 在开物网页导入 JSON，确认只出现我方 Edge 地址和 K；
3. 发现工具，只批准客户允许的工具；
4. 发起真实模型消息，要求实际调用工具而不是猜测；
5. 核对工具调用事件、参数和合成结果；
6. 再执行错误 K、跨来源、未批准工具、relay 停止、恢复和撤销检查；
7. 在正式公网和客户 Windows 完成前，不把本机回环结果写成客户业务验收。

本轮实际网页证据为 `e2e-chat-browser-latest.json`。其中的模型通过学校云回环测试适配器，不是腾讯云生产路由，MCP 返回的是固定合成订单。

## 7. 发布包、源码和证据对应

| 对象 | 标识 |
| --- | --- |
| 基线 | `476ff14f9a4663512925e5b14ba29a5729effdce` |
| 本轮固定源码 | `3d0d06cd87268ce61fc2b7c33b18a679a2d12d66` |
| 源码短提交 | `3d0d06c` |
| 固定构建时间 | `2026-11-18T12:24:52Z` |
| Linux 包 | `f19b31b959f8cfd38a3234bc0a519c2a8acf1877397f24b80657b6a6480596cd` |
| Windows 包 | `705517ecde9d5ac72bbb30b24c523b607e108203681d79ba16ff69094ea8f5c2` |

本轮所有代码证据都记录 `source_commit=3d0d06c` 和 `source_dirty=false`。候选包从同一提交的两次独立 `git archive` 构建，包内版本信息和 SBOM 也记录该提交。

## 8. 回滚和停止服务

先撤销 K 或连接器，使新请求失败关闭，再停止专属实例；不要通过开放 visitor、放宽认证或复用撤销凭据恢复服务。完整步骤见 `ROLLBACK.md`。Windows 回退时必须保留数据目录和最近备份，不能把 DPAPI 文件降级成明文。

## 9. 当前结论

当前成果已经可以作为受控试用候选：代码、本机合成链路、本机 HTTPS/WSS 回环、实际开物网页合成对话和候选包一致性均有证据。它还不能作为 Windows Server 2019、真实公网或华微真实业务已验收的证明；剩余最小动作分别由我方部署、Windows 实机和客户本机填写参数承担。
