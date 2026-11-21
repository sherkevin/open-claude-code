# Windows 内网 MCP 连接器本地验收报告（1.0.1 修复版）

- 测试日期：2026-11-17
- 测试机：SHERVIN（Windows 10 Home China，2009，Build 26200，x64）
- 测试包：`kaiwu-connector-1.0.1-windows-amd64.zip`
- 包内版本：`1.0.1`，commit `89c38b2`，构建时间 `2026-11-17T05:51:52Z`
- 测试范围：仅使用任务书中的模拟 MCP（`127.0.0.1:28765/mcp`，`Bearer demo-token-123`）和模拟库存数据；未使用客户真实数据、真实凭据或生产系统。
- 说明：本报告是 1.0.0 验收失败后的修复版复测。1.0.0 的 A 未通过、B 仅控制台模式通过、C 阻塞；1.0.1 已修复 A/B 的本地阻断项，C 仍因缺少真实接入码和云端闭环而阻塞。

## 测试结论

**A 通过；B 通过；C 阻塞（缺少真实一次性接入码，云端控制面/中继未闭环）。**

- **A（本机安装）通过**：安装脚本成功注册并启动 `KaiwuConnector` 服务；服务 Running、`Automatic`、`LocalSystem`；`127.0.0.1:17872` 页面可访问；`Restart-Service` 后恢复 Running；`uninstall.ps1 -Purge` 删除服务、程序目录和数据目录，卸载后无监听。
- **B（本地 MCP 链路）通过**：模拟 MCP 独立初始化成功；连接器导入 `mcpServers` 配置成功；配置、网络、协议、认证检查通过；发现并调用 `get_inventory`；错误凭据被 401/`UPSTREAM_AUTH_FAILED` 拒绝，恢复正确凭据后重新通过；服务重启后来源配置保留、状态仍为 `CONFIGURING`、再次诊断通过。
- **C（云端完整桥梁）阻塞**：没有真实一次性接入码；云端控制面的接入码兑换、期望状态、通道密钥和心跳接口尚未闭环。未把云端失败归因于 Windows 安装或本机 MCP。

## Windows 环境

- 版本：Windows 10 Home China，版本 2009，Build 26200
- 架构：64 位（x64）
- 是否管理员：交互会话为 `SHERVIN\shers`，UAC 过滤后的令牌为 Medium，非提权；安装、服务重启和卸载均通过 UAC 提权会话执行（提权会话 `IsAdmin=True`）。
- PowerShell：5.1.26100.9818
- 其他环境观察：`Get-MpComputerStatus` 在测试前后均显示 Defender 实时保护为关闭（测试未修改该状态）；未观察到 SmartScreen 或杀毒软件拦截安装。

## 安装包

- 压缩包 SHA-256：`9305E142580CE3908B1B5C334F6F3FEA160F0767BE23702AAAB99C2C46FE5E1F`
- 包内版本：`1.0.1`，提交 `89c38b2`，frpc `0.61.1`
- 与 1.0.0 的关系：1.0.0 包 `2b02ca5b…99779` 的 A/B 阻断项已在本包修复；1.0.0 的失败报告和证据仍保留，便于追溯。

## 安装与服务

- 服务是否运行：**是。**
  - 安装脚本使用 `New-Service` 注册 `KaiwuConnector`，避免 `sc.exe` 在 Windows PowerShell 5.1 下拆分带空格的 `binPath`。
  - 服务状态 `Running`，启动类型 `Automatic`，运行账户 `LocalSystem`。
  - `BINARY_PATH_NAME`：`"C:\Program Files\Kaiwu\Connector\kaiwu-connector.exe" serve --data-dir "C:\ProgramData\Kaiwu\Connector" --listen 127.0.0.1:17872`。
  - 服务入口使用 `golang.org/x/sys/windows/svc` 的 `svc.Run`/`StartServiceCtrlDispatcher`，并向 SCM 报告 `SERVICE_RUNNING`；`Restart-Service` 可正常停止和重新启动。
- 是否监听 `127.0.0.1:17872`：**是**，`LocalAddress` 为 `127.0.0.1`，不是 `0.0.0.0`。
- 页面：`http://127.0.0.1:17872/` 返回 HTTP 200，标题“开物 · 内网连接器”。
- 环境变量：安装脚本成功写入机器级 `KAIWU_CLOUD_BASE=https://flintw.com/projects/kaiwu/api` 和 `KAIWU_EDGE_BASE=https://flintw.com/projects/kaiwu/mcp`；不传 `--edge` 也能导出平台配置。
- 卸载是否清理：**是。** `uninstall.ps1 -Purge` 删除服务、`C:\Program Files\Kaiwu\Connector` 和 `C:\ProgramData\Kaiwu\Connector`；卸载后服务不存在，数据目录不存在，17872 无监听（仅少量 `TIME_WAIT`，无进程监听）。

## 本地 MCP

- 服务端名称和版本：`kaiwu-mock-inventory 1.0.0`
- 协议版本：`2025-06-18`
- 工具列表：`get_inventory`
- 正常诊断是否通过：**是**
  - 配置校验：`pass`
  - 内网连通性：`pass`（可连接 `127.0.0.1:28765`）
  - 证书校验：`skip`（本地明文 HTTP，符合任务书预期）
  - 协议握手：`pass`（`kaiwu-mock-inventory 1.0.0`，协议 `2025-06-18`）
  - 认证与工具目录：`pass`（可用工具 1 个）
  - 测试查询：`pass`（调用成功：`get_inventory`）
- 错误凭据是否被拒绝：**是**
  - `Authorization: Bearer wrong-token` → 协议握手 `fail`，错误码 `UPSTREAM_AUTH_FAILED`，详情 `上游认证失败: HTTP 401`。
  - 恢复 `Bearer demo-token-123` 后重新导入并诊断，全部通过。

## 重启恢复

- 服务是否恢复：**是。** `Restart-Service -Name KaiwuConnector -Force` 后服务重新进入 `Running`，17872 重新监听。
- 配置是否保留：**是。** 重启后 `/api/status` 返回 `state=CONFIGURING`、`state_label=待配置`、`reason=已载入来源配置`，来源 `928b0c97`（`华微库存演示`）保留。
- 再次诊断是否通过：**是。** 协议握手、认证与工具目录、`get_inventory` 调用均通过。
- 未执行整机重启验证；服务启动类型为 `Automatic`，但本次只验证了服务级重启，未做断电/重启整机测试。

## 网络方向

- `17872` 的监听地址：`127.0.0.1:17872`。
- `28765` 的监听地址：`127.0.0.1:28765`。
- Windows 防火墙是否保持启用：**是**，Domain/Private/Public 三个配置文件均为 `Enabled=True`。
- 从其他设备访问 Windows 入站端口是否失败：**未执行第二台设备的外部入站探测（现场没有第二台设备）**。补充探测：本机对局域网地址 `172.20.128.1` 和 `10.81.17.189` 的 `17872`/`28765` 连接均失败，回环连接成功；未新增任何放行 `17872`/`28765` 的入站规则；没有使用端口映射、SSH 反向隧道、ngrok、Cloudflare Tunnel 等暴露 Windows 端口。
- Flint 配置中是否只包含云端 Edge 地址：云端阶段未执行；导出的 JSON 指向 `https://flintw.com/projects/kaiwu/mcp/sources/928b0c97/mcp`，凭据为 `Bearer <EDGE_ACCESS_TOKEN>` 占位符，不含 Windows 地址、`127.0.0.1:28765` 或演示凭据。
- Windows 是否只主动发起出站连接：未执行云端绑定，连接器服务没有建立 TCP/443 出站连接；本阶段无法给出真实出站隧道证据。

## 云端桥梁

- 是否执行：**否。**
- 绑定结果：**未执行，缺少真实一次性平台接入码。**
- 连接结果：**未执行。**
- 若失败，是接口、地址、中继还是其他问题：**云端控制面/中继未闭环**。任务书已注明接入码兑换、期望状态、通道密钥和心跳接口尚未闭环，默认云端地址使用复数路径 `/projects/kaiwu/api`，平台页面可能是单数 `/project/kaiwu/`。本次没有把云端 404 或未执行误报成 Windows 安装失败。

## 1.0.1 修复内容

分支：`fix/windows-service-20261117`（学校云 `open-claude-code` 仓库，未推送）

- `F01`：增加 Windows 服务入口 `service_windows.go`（`svc.Run`、`StartServiceCtrlDispatcher`、`SERVICE_RUNNING`、Stop/Shutdown），增加 `service_other.go`，`main.go` 在服务模式下优先接管；`web/server.go` 增加 `Serve`/`Shutdown`，支持优雅停止。
- `F02`：`install.ps1` 改用 `New-Service -BinaryPathName`，避免 `sc.exe` 在 PowerShell 5.1 下拆分带空格的 `binPath`。
- `F03`：`install.ps1`/`uninstall.ps1` 保存为 UTF-8 BOM；`connector.env.example` 改为 ASCII；安装脚本用 `[System.IO.File]::ReadAllText(..., UTF8)` 解析模板，避免中文注释与键值行被合并。
- `F04`：安装时把 `C:\ProgramData\Kaiwu\Connector` ACL 收紧为 `Administrators` + `SYSTEM`，非管理员无法列出目录或读取 `config.json`/`secrets.json`。
- `F05`：`uninstall.ps1 -Purge` 显式删除数据目录并在删除后校验；失败会抛错，不再静默残留。
- `F06`：未绑定但已导入来源时，重启恢复 `CONFIGURING`，不再退回 `UNPAIRED`。

## 证据文件

- 测试摘要：`test-summary-1.0.1.json`
- 正常诊断：`evidence/diagnose.json`
- 工具调用诊断：`evidence/diagnose-tool-probe.json`
- 错误凭据诊断：`evidence/diagnose-wrong-token.json`
- 恢复后诊断：`evidence/diagnose-restored.json`
- 服务重启后诊断：`evidence/diagnose-after-restart.json`
- 服务重启后状态：`evidence/status-after-restart.json`
- 导出配置：`evidence/export-with-env.json`、`evidence/export-after-restart.json`
- 截图：
  - `evidence/screenshot-01-service-running.png`：服务安装后页面
  - `evidence/screenshot-02-import-ok.png`：导入并校验通过
  - `evidence/screenshot-03-diagnose-ok.png`：正常诊断通过
  - `evidence/screenshot-04-diagnose-tool-probe.png`：`get_inventory` 调用成功
  - `evidence/screenshot-05-wrong-token.png`：错误凭据被 401 拒绝
  - `evidence/screenshot-06-restored-ok.png`：恢复正确凭据后通过
  - `evidence/screenshot-07-after-service-restart.png`：服务重启后页面
  - `evidence/screenshot-08-diagnose-after-restart.png`：服务重启后再次诊断通过
- 服务证据：`service-status.txt`、`service-config.txt`、`service-queryex.txt`、`service-account.txt`、`service-after-restart.txt`、`listener-after-restart.txt`
- 卸载证据：`service-after-uninstall.txt`、`install-dir-after-uninstall.txt`、`data-dir-after-uninstall.txt`、`listener-after-uninstall.txt`
- 权限证据：`data-dir-acl.txt`、`data-file-acl.txt`、`data-file-acl-after-import.txt`、`data-files-nonadmin.txt`
- 网络证据：`listen-addresses.txt`、`firewall-profiles.txt`、`firewall-inbound-rules.txt`、`network-direction-probe.txt`、`established-outbound-443.txt`
- 环境证据：`windows-info.txt`、`whoami-all.txt`、`package-hash.txt`、`package-files.sha256.txt`、`env-vars.txt`

## 仍需完成 / 已知限制

1. **C 云端桥梁未执行**：需要真实一次性接入码、云端控制面、relay/visitor 和 Edge；当前导出的 JSON 中的 `<EDGE_ACCESS_TOKEN>` 仍是占位符，不能直接用于 Flint。
2. **未执行整机重启**：服务为 `Automatic`，但只验证了 `Restart-Service`；建议在客户环境做一次重启整机测试。
3. **未执行第二台设备的外部入站探测**：现场没有第二台设备；已用本机对局域网地址的探测补充，两个端口均不可达，回环可达。
4. **stdio MCP 未验证**：本次只验证 HTTP MCP；如果华微现有 MCP 是 stdio，需要单独实现本地适配器并验收。
5. **未做真实工具数据核对**：本次只调用模拟 `get_inventory`，返回模拟库存文本；未接入客户真实库存/知识库。
6. **1.0.1 尚未推送远端**：修复分支在学校云仓库本地，提交 `8072688`、`1b4a022`、`89c38b2`；如需团队评审，可推送分支或开 PR。

## 建议下一步

1. 把 1.0.1 包和本报告交给华微现场，先做“安装服务 → 导入 HTTP MCP → 诊断 → 重启 → 卸载”一轮客户环境复核。
2. 由云端后端提供真实一次性接入码、source 注册、grant、relay/visitor 和 Edge 路由，完成 C。
3. 在平台导入 `mcp-platform-export.json`（真实 token 替换后）并调用一条只读工具，验证“停服务 → 503、恢复 → 可用、撤销 → 拒绝”。
4. 如果华微使用 stdio MCP，单列本地适配器任务，不要在导入时执行任意命令。
5. 在客户环境验证整机重启后的服务自启，以及 `C:\ProgramData\Kaiwu\Connector` 在服务账户下的 ACL 和 DPAPI/凭据保护。
