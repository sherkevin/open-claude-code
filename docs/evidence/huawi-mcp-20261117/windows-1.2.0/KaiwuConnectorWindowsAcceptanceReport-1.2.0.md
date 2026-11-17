# Windows 内网 MCP 连接器本地验收报告（1.2.0 诊断试用包）

- 测试日期：2026-11-17
- 测试机：SHERVIN（Windows 10 Home China，2009，Build 26200，x64）
- 测试包：`kaiwu-connector-1.2.0-windows-amd64.zip`
- 包内版本：`1.2.0`，commit `b26e131`，构建时间 `2026-11-17T12:11:01Z`
- 测试范围：仅使用任务书中的模拟 MCP（`127.0.0.1:28765/mcp`，`Bearer demo-token-123`）和模拟库存数据；未使用华微真实业务数据、真实客户 Bearer/HMAC 或生产凭据。
- 测试依据：安装包内 `docs/windows-local-test-prompt.md`，并按用户提供的 1.2.0 交付消息执行。

## 测试结论

**A 通过；B 通过；C 阻塞（缺少真实一次性接入码，云端控制面/中继/Edge 未闭环）。**

- **A（本机安装）通过**：`install.ps1` 校验包内 `SHA256SUMS` 后注册并启动 `KaiwuConnector`；服务 Running、`Automatic`、`LocalSystem`；`127.0.0.1:17872` 页面可访问；服务重启恢复；`uninstall.ps1 -Purge` 删除服务、程序目录和数据目录，卸载后无监听。
- **B（本地 MCP 链路）通过**：模拟 MCP 独立初始化；连接器导入 `mcpServers`；配置、网络、证书（skip）、协议、认证与工具目录通过；发现并可试调用 `get_inventory`；错误凭据被 401/`UPSTREAM_AUTH_FAILED` 拒绝，恢复后通过；服务重启后来源、`tool_allowlist`、动态认证档案保留，状态 `CONFIGURING`，再次诊断和试调用通过。
- **C（云端完整桥梁）阻塞**：1.2.0 诊断试用包默认不配置云端控制面和 Edge；页面第 1/5/6 步按预期失败关闭（绑定/连接/生成配置按钮禁用，提示“本机环境未配置云端控制面/中继/Edge”）。没有真实接入码、控制面、relay/visitor 和 Edge，不能从 Flint 调用。未把云端失败归因于 Windows 安装失败。

## Windows 环境

- 版本：Windows 10 Home China，版本 2009，Build 26200
- 架构：64 位（x64）
- 是否管理员：交互会话为 `SHERVIN\shers`，UAC 过滤后的令牌为 Medium，非提权；安装、服务重启和卸载均通过 UAC 提权会话执行（提权会话 `IsAdmin=True`）。
- PowerShell：5.1.26100.9818
- 其他环境观察：`Get-MpComputerStatus` 在测试前后均显示 Defender 实时保护为关闭（测试未修改该状态）；未观察到 SmartScreen 或杀毒软件拦截安装。

## 安装包

- 压缩包 SHA-256 是否匹配：**匹配**
  - 期望：`0d9b7abc3d700390fc52476b0b896b7439e9fc6630b060ff30fb0838202c51d6`
  - 实际：`0d9b7abc3d700390fc52476b0b896b7439e9fc6630b060ff30fb0838202c51d6`
- 包内版本：`1.2.0`，提交 `b26e131`，构建时间 `2026-11-17T12:11:01Z`；包含 frpc `0.61.1`、Go `1.27.4` 标准库。
- 包内 `SHA256SUMS`：**25 项全部通过，FAILED_COUNT=0**（包括 `bin/kaiwu-connector.exe`、`bin/frpc.exe`、`install.ps1`、`uninstall.ps1`、docs、compliance 和 `版本信息.txt`）。
- SBOM 备注：有两条已解释的 warning：主模块版本因 `git: object not found` 未识别；Go 标准库许可证未自动检测，构建已注入 BSD-3-Clause 并随包提供 Go LICENSE/PATENTS。不是本次 A/B 阻断项。

## 安装与服务

- 服务是否运行：**是。**
  - 服务名称 `KaiwuConnector`，状态 `Running`，启动类型 `Automatic`，运行账户 `LocalSystem`。
  - `BINARY_PATH_NAME`：`"C:\Program Files\Kaiwu\Connector\kaiwu-connector.exe" serve --data-dir "C:\ProgramData\Kaiwu\Connector" --listen 127.0.0.1:17872`。
  - `install.ps1` 先校验 `SHA256SUMS`，再使用 `New-Service` 注册；`Start-Service` 后 30 秒内进入 Running。
- 是否仅监听 `127.0.0.1:17872`：**是**，`LocalAddress=127.0.0.1`，不是 `0.0.0.0`。
- 页面：`http://127.0.0.1:17872/` 返回 HTTP 200，标题“开物 · 内网连接器”，版本显示 `1.2.0 (commit b26e131, built 2026-11-17T12:11:01Z)`。
- 数据目录 ACL：`C:\ProgramData\Kaiwu\Connector` 仅 `BUILTIN\Administrators` 和 `NT AUTHORITY\SYSTEM` 有完全控制；普通用户无法列出目录，也无法读取 `config.json`/`secrets.json`。
- 卸载是否清理：**是。** `uninstall.ps1 -Purge` 后服务不存在、`C:\Program Files\Kaiwu\Connector` 不存在、`C:\ProgramData\Kaiwu\Connector` 不存在；17872 无 LISTEN（仅少量 TIME_WAIT），机器级 `KAIWU_CLOUD_BASE`/`KAIWU_EDGE_BASE` 已清空。

## 本地 MCP

- 服务端名称和版本：`kaiwu-mock-inventory 1.0.0`
- 协议版本：`2025-06-18`
- 工具列表：`get_inventory`
- 正常诊断是否通过：**是**
  - 配置校验 `pass`；内网连通性 `pass`（可连接 `127.0.0.1:28765`）；证书校验 `skip`（本地明文 HTTP，预期）；协议握手 `pass`（`kaiwu-mock-inventory 1.0.0`，协议 `2025-06-18`）；认证与工具目录 `pass`（可用工具 1 个）。
- 工具试调用：**通过**，`调用成功: get_inventory`。1.2.0 新增本机动态认证；本次使用合成演示值（`demo-app`/`demo-channel`/`demo-user`/合成 HMAC key）配置本机认证档案，仅用于让试调用走通，**不是华微真实 HMAC 或渠道身份**。
- 错误凭据是否被拒绝：**是**
  - `Authorization: Bearer wrong-token` → 协议握手 `fail`，错误码 `UPSTREAM_AUTH_FAILED`，详情 `上游认证失败: HTTP 401`。
  - 恢复 `Bearer demo-token-123` 后重新导入并诊断，全部通过；工具试调用恢复通过。

## 重启恢复

- 服务是否恢复：**是。** 第二次干净 `Restart-Service -Name KaiwuConnector -Force` 后服务重新进入 `Running`，17872 重新监听。
- 配置是否保留：**是。** 重启后 `/api/status` 返回 `state=CONFIGURING`、`state_label=待配置`、`reason=已载入来源配置`；来源 `src_4853fc5ee3cc19cfc68f0d9e1751bf35`（`华微库存演示`）保留，`tool_allowlist=["get_inventory"]` 保留，`auth_profiles` 显示 `configured=true`。
- 再次诊断是否通过：**是。** 协议握手、认证与工具目录、`get_inventory` 试调用均通过。
- 测试观察：第一次重启（20:41）后曾出现一次 401；API 重新导入同一正确配置后恢复；第二次干净重启（20:46）通过。该现象已单独记录为“待复现观察”，不作为 A/B 失败判定；详见 `evidence/restart-401-observation.md`。
- 未执行整机重启验证；服务启动类型为 `Automatic`，本次只验证了服务级重启。

## 网络方向

- `17872` 的监听地址：`127.0.0.1:17872`。
- `28765` 的监听地址：`127.0.0.1:28765`。
- Windows 防火墙是否保持启用：**是**，Domain/Private/Public 三个配置文件均为 `Enabled=True`。
- 从其他设备访问 Windows 入站端口是否失败：**未执行第二台设备的外部入站探测（现场没有第二台设备）**。补充探测：本机对局域网地址 `172.20.128.1`、`10.81.17.189` 的 `17872`/`28765` 连接均失败，回环连接成功；未新增任何放行 `17872`/`28765` 的入站规则；未使用端口映射、SSH 反向隧道、ngrok、Cloudflare Tunnel 等暴露 Windows 端口。
- Flint 是否能够直接访问 Windows：**否（按网络方向判定）**。17872/28765 只监听回环，局域网/公网不可达；Flint 侧未执行直接调用（C 阶段未执行），不存在把 Windows 地址写入 Flint 的配置。
- Windows 是否只主动发起出站连接：未执行云端绑定，服务没有建立 TCP/443 出站连接；监听仅回环。云端 C 段完成后才建立主动出站隧道。

## 云端桥梁

- 是否执行：**否。**
- 绑定结果：**未执行，缺少真实一次性接入码。** 页面第 1 步显示“本机环境未配置云端控制面，当前诊断试用包不能绑定企业”，绑定按钮禁用。
- 连接结果：**未执行。** 页面第 5 步显示“本机环境未配置云端中继。当前诊断试用包只能完成本机安装、配置导入和连通性诊断，不能建立云端通道”，连接按钮禁用。
- 导出结果：**未执行。** 页面第 6 步显示“本机环境未配置云端 Edge，不能导出可用的平台连接”，生成配置按钮禁用；`/api/export` 返回 `CONFIG_INVALID: 缺少云端 Edge 地址`。
- 失败归类：**接入码、控制面、relay/visitor、Edge 均未提供，云端 C 段阻塞。** 1.2.0 诊断试用包默认不配置云端，页面第 1/5/6 步属于预期的失败关闭，不是 Windows 服务故障。

## 必须提交的证据

- Windows 版本、架构、权限：`windows-info.txt`、`whoami-all.txt`、`environment.txt`
- 安装包 SHA-256：`package-hash.txt`
- 包内版本和 `SHA256SUMS` 校验：`package-version.txt`、`sha256sums-check.txt`、`SHA256SUMS.txt`、`package-files.sha256.txt`
- 服务安装/启动/重启/卸载：`service-status.txt`、`service-config.txt`、`service-queryex.txt`、`service-account.txt`、`service-after-restart-clean.txt`、`status-after-restart-clean.json`、`service-after-uninstall.txt`、`install-dir-after-uninstall.txt`、`data-dir-after-uninstall.txt`、`listener-after-restart-clean.txt`、`listener-after-uninstall.txt`
- 页面截图：`screenshot-01-service-running.png` 到 `screenshot-09-diagnose-after-restart.png`
- 模拟 MCP 启动/初始化：`mock-mcp.log`、`mock-initialize.json`、`mock-tools-list.json`
- 正常导入/诊断：`import-result.json`、`diagnose.json`、`diagnose-tool-probe.json`
- 正常/错误凭据对比：`diagnose-wrong-token.json`、`diagnose-restored.json`、`screenshot-06-wrong-token.png`、`screenshot-07-restored-ok.png`
- 重启后状态/诊断：`status-after-restart-clean.json`、`diagnose-after-restart.json`、`diagnose-tool-after-restart.json`
- 监听地址/防火墙/入站探测：`listen-addresses.txt`、`firewall-profiles.txt`、`firewall-inbound-rules.txt`、`network-direction-probe.txt`、`established-outbound-443.txt`
- 数据目录 ACL：`data-dir-acl.txt`、`data-file-acl.txt`、`data-file-acl-after-import.txt`、`data-files-nonadmin.txt`
- 云端失败关闭：`ui-cloud-state.json`、`cloud-binding-not-attempted.txt`、`export-no-cloud.json`
- Defender/执行策略：`defender-status.txt`、`execution-policy.txt`、`firewall-profiles-before.txt`
- 缺陷/观察：`defect-dynamic-auth-allowlist.md`、`restart-401-observation.md`

## 发现的缺陷

1. **D01（中）重新导入 MCP 配置后动态认证工具允许列表被清空。**
   - 复现：导入配置 → 第 3 步配置动态认证并允许 `get_inventory` → 重新导入同一配置（例如从错误凭据恢复正确凭据）→ `/api/diagnose` 带 `tool_probe=get_inventory`。
   - 现象：`auth_profiles[0].configured=true`，但 `allowed_tools=[]`、`source.tool_allowlist=[]`；试调用报“试调用工具不在客户允许列表内”；重新保存第 3 步后恢复。
   - 源码依据：`ImportConfig` 替换 `a.config.Sources`（新 source 不含 `ToolAllowlist`），`authStatusLocked` 从 `source.ToolAllowlist` 取允许工具，`Diagnose` 用该列表校验试调用。
   - 影响：不影响本次 A/B 正常诊断；影响 1.2.0 新增动态认证的工具试调用和 UI 状态一致性。详见 `defect-dynamic-auth-allowlist.md`。
2. **D02（低，待复现观察）第一次服务重启后出现一次 401。**
   - 现象：在错误凭据 → 恢复正确凭据的页面操作后，第一次 `Restart-Service` 后 `/mcp` 返回 401；API 重新导入同一正确配置后恢复；第二次干净重启通过。
   - 可能原因：该次页面导入未正确写入 `Authorization` secret，或自动化填充/提交时序；需要人工浏览器复现并检查 `/api/import` 写入 `secrets.json` 的路径。详见 `restart-401-observation.md`。
3. **C 阶段阻塞（非产品缺陷）**：缺少真实接入码、控制面、relay/visitor、Edge；页面第 1/5/6 步按预期失败关闭。不能把 A/B 通过写成完整交付通过。

## 建议下一步

1. 修复 D01：重新导入时按稳定 `source_id` 保留 `ToolAllowlist`，或在允许列表清空时清除 profile/明确提示用户重新确认工具；避免“已配置”但无法试调用。
2. 用人工浏览器复现 D02：错误凭据 → 恢复 → 重启 → 诊断；检查 `/api/import` 写入 `secrets.json` 的时序和页面提交内容。
3. 提供真实一次性接入码、控制面、relay/visitor 和 Edge，再做 C 段端到端：绑定 → 连接 → 平台导入 JSON → 调用 `manual_search`/销售订单 → 停服务 503 → 恢复 → 撤销拒绝。
4. 保持 17872/28765 只绑定回环；不要把 Windows 地址写入 Flint；不要使用端口映射或反向隧道。
5. 在客户环境补一次整机重启后的服务自启验证；当前只验证了 `Restart-Service`。
6. 如果后续使用华微真实 HMAC/渠道身份，按 `docs/动态认证与试装说明.md` 配置；本次仅使用合成演示值。
