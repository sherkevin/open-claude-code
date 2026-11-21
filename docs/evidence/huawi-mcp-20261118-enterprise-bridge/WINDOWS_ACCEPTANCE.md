# Windows 实机验收状态与步骤

## 1. 当前状态

用户在当前工程师会话明确要求“先不做 Windows 匹配，回到主线”。因此本轮没有连接 Shervin Windows 电脑，没有启动远程 Windows 授权流程，也没有操作客户 Windows Server 2019 或任何别的 Windows 桌面。

本轮实际完成的是 Windows 候选包构建和静态检查，不是 Windows 实机验收：

- 包路径：`/media/data3/dengkw/releases/huawi-connector-1.3.0-3d0d06c/kaiwu-connector-1.3.0-windows-amd64.zip`
- SHA-256：`705517ecde9d5ac72bbb30b24c523b607e108203681d79ba16ff69094ea8f5c2`
- 源码提交：`3d0d06cd87268ce61fc2b7c33b18a679a2d12d66`
- 包内文件：`install.ps1`、`uninstall.ps1`、`windows-smoke.ps1`、Windows amd64 连接器、官方 `frpc.exe`、配置模板、文档、SBOM、漏洞扫描、许可证和构建证据。
- 校验：ZIP 完整性、包内 `SHA256SUMS`、PE32+ x86-64 格式和包内版本信息通过。

这些结果只证明包能生成且结构正确，不证明 Windows 服务能安装、DPAPI 能读取、ACL 能生效、重启后能恢复或升级能保留配置。

## 2. 代码中已经存在、但未做实机验证的能力

- Windows 机器级 DPAPI，用于 `identity.json`、`device_key.json`、`secrets.json` 和 `auth_profiles.json`；
- 严格数据目录 ACL，`icacls` 或 ACL 校验失败时中止；
- 升级前备份到受保护数据目录；
- 旧明文 JSON 首次读取时的原子迁移；
- Windows 服务重启和 frpc 子进程守护；
- Windows `frpc.toml` 使用官方 `.Envs` 占位符，不持久化 relay/STCP 密钥；
- `windows-smoke.ps1` 和安装脚本。

Go 的 Windows 专用源码交叉编译通过，但不能替代目标机运行。

## 3. 目标环境

- 客户目标是 Windows Server 2019 Datacenter 64 位，有图形桌面和管理员权限。
- 需要一个合成或获授权的本地 MCP 端点；开发证据不得使用客户真实秘密。
- 使用本报告开头的固定 ZIP 和 `SHA256SUMS`。
- 如果以后把 Shervin Windows 11 作为补充环境，必须单独标注系统版本；它不能替代 Windows Server 2019 客户目标验收。

恢复 Windows 测试时，设备操作必须先只读核对主机名、操作系统、登录用户和设备标识；进程记录必须同时保存设备 ID 与 PID，搜索会话必须保存设备 ID 与会话 ID。

## 4. Windows Server 2019 实机步骤

1. 在 Windows 上核对 ZIP SHA-256，解压并以管理员身份运行 `install.ps1`。
2. 确认 `KaiwuConnector` 服务为 `Running`，`http://127.0.0.1:17872/` 可访问。
3. 检查四个敏感文件以 `KAIWU-DPAPI-V1` 开头，且不包含测试 Bearer 明文。
4. 检查数据目录 ACL：只允许 `S-1-5-18`（SYSTEM）和 `S-1-5-32-544`（Administrators）；继承规则应关闭。
5. 导入合成 MCP 配置，批准目标工具，通过本机认证代理执行 initialize、tools/list 和 tools/call。
6. 启动连接器，检查隧道和 Edge；停止 relay 后应得到有界 503 或等价失败，而不是静默成功。
7. 重启 Windows 服务，确认配置和解密能力保留，日志和 UI 不出现明文秘密。
8. 重启整台 Windows，重复服务和数据面检查。
9. 覆盖安装同一候选包，确认升级前备份存在且配置保留。
10. 卸载或撤销测试设备，确认本机访问被拒绝，云端会话不能继续。

## 5. 应保存的证据

- Windows 名称、版本、主机名和时区；
- 候选包版本与 SHA-256；
- 服务和整机重启前后的状态；
- 脱敏后的 ACL 输出；
- DPAPI 文件前缀和明文秘密扫描结果，禁止粘贴真实秘密；
- initialize、tools/list、tools/call 的固定合成结果；
- relay 停止和恢复时间；
- 升级备份路径和配置保留结果；
- 卸载或撤销结果。

在以上步骤真正执行前，T11 必须保持“未执行”，不能因为 ZIP 构建成功或 PowerShell 脚本存在而改成通过。
