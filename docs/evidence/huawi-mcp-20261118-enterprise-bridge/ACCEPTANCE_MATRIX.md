# 华微 MCP 企业统一入口验收矩阵

- 日期：2026-11-18
- 代码基线：`476ff14f9a4663512925e5b14ba29a5729effdce`
- 本轮固定源码提交：`3d0d06cd87268ce61fc2b7c33b18a679a2d12d66`
- 工作树：`/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118`
- 分支：`feat/huawi-enterprise-bridge-20261118`

## 1. 证据等级

本矩阵明确区分以下五件事，任何一种通过都不能替代另一种：

1. 代码已实现；
2. 自动化测试和本机合成环境已通过；
3. 实际开物网页和真实模型工具循环已通过；
4. Windows Server 2019 实机已通过；
5. 真实公网、腾讯云生产部署和华微真实业务已通过。

本轮已经完成第 1 至 3 项中可执行的部分。Windows 远程实机按用户当前“不做 Windows 匹配、回到主线”的指示未执行；真实公网和客户业务也没有执行。不得把回环 HTTPS/WSS、合成订单或 Windows 交叉编译写成上述未执行项已通过。

## 2. 本轮证据

| 证据 | 文件 | 运行目录或边界 |
| --- | --- | --- |
| 本机 TCP 合成链路 | `e2e-synthetic-latest.json` | `.runtime/huawi-enterprise-e2e-20261118T122456Z`；15 项通过；21.939 秒 |
| 本机 HTTPS/WSS 回环链路 | `e2e-tls-wss-latest.json` | `.runtime/huawi-enterprise-e2e-20261118T122524Z`；8 项通过；15.501 秒 |
| 实际开物网页对话 | `e2e-chat-browser-latest.json` | `.runtime/huawi-enterprise-e2e-20261118T122545Z`；5 项通过；45.969 秒 |

三份证据都记录 `source_commit=3d0d06cd87268ce61fc2b7c33b18a679a2d12d66` 和 `source_dirty=false`。任务派发前已有的 14 项合成检查没有删除；对固定源码重跑后，当前合成证据为 15 项，全部通过。

实际网页对话的关键证据：

- 使用隔离的管理员账号登录实际开物产品网页；
- 导入标准 `mcpServers` JSON，发现且只发现 `lookup_order`；
- 用户消息触发恰好一次工具调用，参数为 `{"sku":"SKU-1001"}`；
- 工具调用成功耗时 771 毫秒，开始和完成事件分别为 22、26；
- 最终回答包含固定合成结果 `SYNTHETIC-ORDER-42`；
- 网页模型显示为 `dano-v2.1`，上游标识为 `tencent-cloud/deepseek-v4.1-flash-1017[1m]`；
- 模型请求通过学校云回环测试适配器 `127.0.0.1:3425`，`provider_test_mode=true`、`production_route=false`，不是腾讯云生产模型路由；
- 截图位于运行目录的 `browser-screenshots/03-web-chat-answer.png`、`04-web-chat-tool-trace.png`、`05-web-chat-tool-events.png`。

## 3. T01—T13 验收结果

| 编号 | 验证项 | 状态 | 证据与边界 |
| --- | --- | --- | --- |
| T01 | 标准 JSON 导入 | **通过，实际网页** | 实际开物网页完成导入并保存。导入内容只包含我方 Edge 地址和企业集成凭据 K，不包含客户 Bearer、HMAC 密钥、channel 或 userid。证据见 `e2e-chat-browser-latest.json` 和导入截图。 |
| T02 | MCP 生命周期 | **通过，合成环境** | 完成 `initialize`、初始化通知、`tools/list` 和 `tools/call`，返回 `SYNTHETIC-ORDER-42`。TCP 与 HTTPS/WSS 两条链路均通过，见两份端到端证据。 |
| T03 | 实际完整隧道 | **部分通过，本机回环** | 使用官方 `frps`/`frpc` 0.61.1、云端 visitor、连接器核心、Edge 和 Nginx 完成 TCP 与 HTTPS/WSS 链路。Nginx 记录到两个 WebSocket `101`；错误 CA 被拒绝。真实公网 DNS、正式证书和腾讯云部署未执行。 |
| T04 | 华微签名 | **通过，离线向量** | 固定向量、短协议有效密钥、错误密钥、过期时间、未来时间和伪造身份头均有自动化测试。客户服务器实际时钟窗口未验证。 |
| T05 | SSE | **通过，自动化与回环** | 自动化测试确认上游未关闭时即可收到首事件，多行 `data` 只产生一次有效载荷。本轮合成链路首事件为 0.149 秒。长时间、高并发和生产长连接容量未验证。 |
| T06 | 会话隔离 | **通过，合成环境** | 覆盖不同企业集成凭据、跨来源拒绝、Edge 会话绑定和两个并发集成不串结果。 |
| T07 | 授权与撤销 | **通过，合成环境** | 错误、过期、撤销 K、跨来源访问、设备撤销和凭据轮换均有覆盖。撤销后 0.502 秒停止专属实例，Edge 返回 401，frps/visitor 端口关闭，visitor 路由释放。 |
| T08 | 来源和工具范围 | **通过，合成环境** | `tools/list` 只返回允许工具，未批准工具调用返回 403；空范围默认拒绝；重复导入保留工具批准列表，D01 修复未回退。 |
| T09 | 断线恢复 | **部分通过** | 正常停机后 Edge 返回 503 用时 0.183 秒；强杀专属 frps 后不可用 0.101 秒，2.427 秒恢复；relay 重启后 initialize 返回 200。模拟网络分区未执行。 |
| T10 | 凭据与日志 | **部分通过** | 本机合成链路和实际网页对话的日志、控制数据库、浏览器报告、截图元数据及 Windows 风格 `frpc.toml` 完成秘密扫描，未命中 K、客户 Bearer、HMAC 密钥、R 或 T。Windows 实机 ACL 和事件日志未验证。 |
| T11 | Windows | **未执行** | 已生成 Windows amd64 候选包，但按用户当前指示没有连接 Shervin Windows 电脑，也没有执行客户 Windows Server 2019 的图形安装、DPAPI、ACL、服务重启、整机重启、升级和卸载。交叉编译和单元测试不能替代实机结果。 |
| T12 | 实际开物对话 | **通过，合成业务数据和回环模型路由** | 已登录实际开物网页，导入本轮 JSON，发现并绑定工具，由真实模型发起消息并实际调用 `lookup_order`，最终回答包含固定合成结果。限定：数据是合成数据，模型通过学校云回环测试适配器，不是腾讯云生产路由，也不是客户业务验收。 |
| T13 | 客户业务 | **未执行** | 没有使用华微真实业务 MCP、客户 Bearer、HMAC 密钥、channel、userid 或真实业务数据；需客户本机录入授权参数后另行验收。 |

## 4. 候选包

- 版本：`1.3.0`
- 源码提交：`3d0d06cd87268ce61fc2b7c33b18a679a2d12d66`
- 发布目录：`/media/data3/dengkw/releases/huawi-connector-1.3.0-3d0d06c`
- Linux 包 SHA-256：`f19b31b959f8cfd38a3234bc0a519c2a8acf1877397f24b80657b6a6480596cd`
- Windows 包 SHA-256：`705517ecde9d5ac72bbb30b24c523b607e108203681d79ba16ff69094ea8f5c2`

两次独立 `git archive` 构建产生的 Linux 包、Windows 包和外层 `SHA256SUMS` 逐字节一致。包内哈希、ZIP 完整性、TAR 读取、二进制格式、SBOM 和漏洞扫描均通过。

## 5. 仍需执行的最小事项

1. **我方部署**：选择公网域名和证书，部署单个 Edge worker、固定版本 frps/visitor 和 Nginx HTTPS/WSS 反向代理；然后创建企业集成并生成正式 JSON。
2. **Windows 实机**：在客户 Windows Server 2019 上安装候选包，核对 DPAPI/ACL、服务重启、整机重启、升级和卸载。Shervin Windows 11 只能作为补充测试环境，不能替代客户目标系统。
3. **客户业务**：客户本机填写 MCP 地址、Bearer、HMAC 密钥、appid、channel、userid 和允许工具后，用实际开物对话查询真实业务数据并单独记录结果。

在以上三项完成前，正确结论是：代码、本机合成链路、本机 HTTPS/WSS 回环和实际开物网页合成对话已通过；真实公网、Windows Server 2019 实机和客户业务未通过验收。
