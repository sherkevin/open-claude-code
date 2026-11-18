# 华微 MCP 桥接器：Windows 实机测试与安装包交付任务

- 生效时间：2026-11-18 19:39（北京时间）。
- 指定执行会话：import:codex:01a1d40f-0e5e-7382-8865-79bbb09e28c2。
- 继续使用现有 DeepSeek 工程会话；不新建重复任务，不切换全局模型或 provider。
- 工作树：/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118。
- 分支：feat/huawi-enterprise-bridge-20261118。

## 1. 最新用户要求及优先级

用户最新明确要求：
“他做测试时需要在 Windows 上进行，跟华为的保持一致。他最后交付的应该是一个 Windows 安装包才对，工程师可以访问 Shevin 的 windows 机器，这个可以当做测试机。”

本要求覆盖旧任务、旧队列和 WINDOWS_ACCEPTANCE.md 中“暂停 Windows 远程匹配/登录/测试”的安排。Windows 测试现已获用户授权，不再按旧暂停要求跳过。旧 run 73 的收尾任务与本任务合并执行一次；保留主目标、既有实现与验收成果。

先收尾当前正在进行的必要构建，随即转入 Windows 实机安装与验证。已经通过且源码未变的 Linux 测试、双份可复现构建不再重复。只有具体修复影响到对应功能时补测。不要只完成文档后结束。

## 2. 已确认的测试环境

Desktop Commander 已确认两台设备在线，所有远程命令必须明确指定 deviceId：

- Windows 测试机显示名 Shervin（用户称 Shevin）。
  deviceId：d5edb697-e33e-4e6d-9150-5b9c278b6524。
  主机名 SHERVIN；Windows 11，64 位，build 26200。
  架构师已通过 Get-CimInstance 做只读核验；当前远程 PowerShell 进程 Elevated=false。
- VIP 开发机 viplabserver12。
  deviceId：9671a4f4-f6a8-459b-b4ce-4bfd87581461。

客户系统是 Windows Server 2019 Datacenter x64（此前资料为 build 17763.9020）。Shervin 的实测可以推进 Windows 验收，但报告必须写真实系统版本，不能将 Windows 11 实测写成 Server 2019 已通过。确保实现和安装器保持 Server 2019 兼容，未实测的版本边界单列。

沿用已具备的 Desktop Commander 访问能力，不重新反复发起 OAuth 登录。若你所在工程会话的工具或 CLI 缺少现有设备通道，只报告准确的工具/登录状态及所需最小操作，不假装已连接，也不把全部开发重新阻塞在认证上。

## 3. 本轮交付形态

最终客户交付物为 Windows x64 安装包，客户可双击安装，优先复用已有安装打包工具，提供 setup.exe 或 .msi。

如果当前只有 ZIP、EXE 主程序和 install.ps1，应把它视为工程候选包：可以先用它开展实机验证，但最后需要补齐可双击的安装器。ZIP 可以作为附加便携包，不能用“Linux 上交叉编译成功”代替 Windows 安装验收。

安装器复用现有 Go 程序、官方未修改的 frpc 和现有安装/卸载逻辑；不要另造服务框架或 FRP 实现。尽量复用已装好的成熟开源打包器。最终客户电脑不需要安装 Go、Python、Node 或编译工具。

安装器应完成文件落盘、Windows 服务注册/启动、必要数据目录保护，并给出本机配置页面入口；配置界面里由客户填写其内部 MCP 地址及访问配置。真实内部密钥不进入我方导出的 MCP JSON 或日志。双击安装过程中允许正常 UAC 提权。

## 4. 按顺序执行

1. 先只读检查 Shervin：OS、当前权限、磁盘余量、相关 Kaiwu 服务/进程、17872 端口和已存在的安装/数据目录。保留已有配置，选明确的专用测试目录。
2. 选择当前已提交源码对应的最新候选包，不回退到旧基线。记录源码提交、版本、构建时间和 SHA-256。将包传到 Shervin，Windows 上用 Get-FileHash 再核对；不把二进制 Base64 或凭据刷入聊天日志。
3. 使用正常管理员提权运行安装。当前远程进程不是管理员，不能将失败的服务注册记作成功，也不绕过 UAC。若需要用户点击“是”，先把安装包和命令准备好，再只说明这一项实际操作。
4. 在 Windows 实机验证服务启动、本机配置页、官方 frpc 启动和日志。用固定合成 MCP 数据完成配置、工具发现和查询；开发测试无需华微真实 Bearer/HMAC key。
5. 让 Windows 连接器主动连接 VIP 上本项目专属 relay/Edge，验证经公网入口/标准 MCP JSON 的查询链路，记录真实传输方式；不要把两端同机回环测试算成 Windows 端到端通过。继续补实际开物模型调用工具的对话证据。
6. Windows 上验证 DPAPI 持久化、目录 ACL、服务重启后配置恢复、隧道断线后恢复、覆盖升级及卸载/撤销。优先跑已有 windows-smoke.ps1；检查脚本包含在最终包中且测试有实际断言。
7. 整机重启恢复单列记录。不要突然重启用户正在使用且有未保存工作的机器；先完成可安全执行的安装和服务重启测试，再安排可安全进行的整机重启。未执行的整机重启明确写待执行。
8. 只修复实测发现的具体问题，修复交由本工程会话执行。更改了程序或安装脚本后重建，并针对受影响项目复测；最终安装包须对应被验证的版本与源码。

VIP 上的项目、构建、缓存、日志及临时文件一律在 /media/data3/dengkw/，不得写满根分区，不改 HOME/CODEX_HOME。Windows 机器使用其本地有空间的专用测试目录，不套用 Linux 路径。

继续采用企业统一入口、官方 frps/frpc、专属 relay 与 STCP，不扩展为新的数据库连接器、员工身份系统或通用内网代理。现有公网证书、TLS/WSS 和真实平台对话的未验收项目仍需如实收尾。

## 5. 证据、报告和首次回报

在 docs/evidence/huawi-mcp-20261118-enterprise-bridge/ 更新：

- WINDOWS_ACCEPTANCE.md：将旧暂停说明改为最新授权及真实执行状态；记录设备、实际 Windows 版本、权限、安装包哈希、每项操作/结果和脱敏日志路径。
- ACCEPTANCE_MATRIX.md：Windows 11 实测、Server 2019 兼容性、整机重启、真实平台对话分别标注，未执行不写通过。
- RELEASE_REPORT.md：最终 Windows 安装器位置、大小、版本、源码提交、SHA-256、安装/升级/卸载说明与已知限制。

本指令是文档与优先级更新；不因仅更新任务文档反复重建同一程序。保留已完成的新提交和新包，以你收到本任务时的最新状态为准。

收到后先用一次简短回报说明：已读取本最新指令、当前源码/包版本、是否能够操作 Shervin、当前正在执行哪一步。之后以新增实测结果回报，避免只反复声明“继续收尾”。

## 6. 架构师只读预检补充（安装前必须采纳）

Shervin 上已有运行中的 KaiwuConnector 服务，StartMode=Auto，PID=42248；127.0.0.1:17872 已由该进程监听。实际程序位于 C:\Program Files\Kaiwu\Connector\kaiwu-connector.exe，同目录有 frpc.exe。不要按“全新空机器”直接覆盖。

已装连接器二进制 SHA-256：
df725b2f570f4c926779d5a7572cbfad33957c27bc79c7a4ae2b1c3b651e53bf

当前发布目录 /media/data3/dengkw/releases/huawi-connector-1.3.0-8c889fe/ 中：
- Windows ZIP：kaiwu-connector-1.3.0-windows-amd64.zip，9,488,573 字节。
- ZIP SHA-256：e9cc49f1dfffeb68ba5061a47648f30ef74df1fd1d332fbb3ab86a784ff1f6ae。
- ZIP 内 bin/kaiwu-connector.exe SHA-256：15490d71f19d01729ae449824809cd309e83c2651e4681f598378d87736971eb。

上述两份二进制不同，不能把已有服务的运行状态当成本次候选包的实测结果。已有程序版本号尚未取到，不臆测版本号。请定位现有数据目录和配置，脱敏记录并备份，再做受控升级。Windows C、D 盘均有空间，D 盘预检约有 301 GiB 可用，可考虑放测试包和证据。

只读审查发现现有安装脚本会停用并删除旧服务再继续安装。升级时需先保存旧程序、配置及可执行的恢复步骤；测试安装失败后的服务恢复，不能仅证明新安装成功。默认卸载保留数据；-Purge 会删除数据和备份，只对确认归属的本次测试数据使用。

该预检未安装、停止、升级或卸载任何 Windows 程序；执行实机测试仍由指定工程师继续。
