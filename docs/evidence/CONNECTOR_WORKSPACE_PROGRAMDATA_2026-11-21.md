# Windows 解包目录迁移到 ProgramData（2026-11-21）

## 背景

2026 年 11 月 21 日的内部试装（1.3.15）在首次校验时找不到隧道组件 `frpc.exe`，安装在注册服务前退出。现场报告的腾讯电脑管家检测名称是 `Win64.Risktool.Frp.Zimw`；归档中的组件与 [FRP 0.61.1 官方 Windows 发布](https://github.com/fatedier/frp/releases/tag/v0.61.1) 完全一致（`d9a8cf8b…b57159d`）。

根因不是文件损坏：终端防护按路径生效，且拒绝信任 `Program Files (x86)` 根目录。安装器的解包暂存目录由 32 位 launcher 通过 `CSIDL_PROGRAM_FILES` 解析，在 64 位 Windows 上正是 `C:\Program Files (x86)\kw-<安装编号>`，落在无法被信任的根目录下。

## 改动

1. `packaging/windows/attempt-journal.c` `EXPORT(Workspace)`：暂存根从 `CSIDL_PROGRAM_FILES` 改为 `CSIDL_COMMON_APPDATA` 加产品目录，得到 `C:\ProgramData\Kaiwu\kw-<安装编号>`。
   路径校验的允许写权限由 `0` 改为 `COMMON_DATA_WRITES`：现场会为信任区预先创建 `C:\ProgramData\Kaiwu`，该目录继承的 ACL 允许标准用户创建子目录，若仍按 0 校验会直接失败并报 `workspace_unavailable`。`safe_permissions` 仍要求目录属主是 SYSTEM／Administrators／TrustedInstaller，标准用户自建的目录会被拒绝；`DELETE`、`WRITE_DAC`、`WRITE_OWNER`、`FILE_DELETE_CHILD` 始终不在允许范围内。私有 SDDL 与重解析点校验未改动。
2. `packaging/windows/installer.nsi`：解析出的暂存路径打印到安装详情页，并写入 `%ProgramData%\Kaiwu\Connector\logs\install-workspace.txt`。journal 事件格式由 C 与 Go 两处解析器逐字节校验，本次不修改该格式。
3. `packaging/windows/installer.nsi`：`MUI_ICON` 改为无条件定义。原来的 `!if /FileExists` 在缺少图标时会静默退回默认图标，且在 POSIX 主机上（NSIS 3.08 + MUI2）会让 makensis 直接崩溃；改为由 `scripts/build-windows-installer.sh` 断言图标文件存在，缺失即构建失败。
4. `packaging/windows/uninstall.ps1`：卸载时清理 `C:\ProgramData\Kaiwu\kw-<32 位十六进制>` 形式的遗留暂存目录，保留 `Connector` 数据目录与 journal 证据。
5. `packaging/windows/attempt-journal.c`：`workspace_unavailable` 的中文提示补充排查方向。
6. `scripts/build.sh` 默认版本 `1.3.18`；`scripts/build-windows-installer.sh` 在没有 `unshare` 或私有挂载命名空间不可用时回退为直接调用 makensis，并强制 `-INPUTCHARSET UTF8`；新增 `scripts/build-macos.sh`（把 Homebrew 的 GNU 工具放进 PATH 后调用原构建脚本）。
7. 新增 `scripts/tests/test-workspace-path-policy.py`，用静态断言守住上述策略（暂存根、允许掩码、可观测路径、卸载清理、版本一致性）。

## 验证

| 验证内容 | 环境 | 结果 |
| --- | --- | --- |
| Go 单元测试 `go test ./...` | macOS 15.6.1 / Go 1.27.4 | 全部通过 |
| 暂存目录策略断言 `test-workspace-path-policy.py` | macOS 15.6.1 / Python 3.13.5 | 通过 |
| `attempt-journal.c` 32 位交叉编译（含上述改动） | i686-w64-mingw32-gcc 16.2.0 → `KaiwuJournal.dll` | 通过，`-Wall -Wextra -Werror` 无告警 |
| Windows 主程序版本资源与图标 | `windows-exe-version-resource.py` → 交叉编译 | PE 内 8 个 `RT_ICON` + `RT_GROUP_ICON`，版本 1.3.18 |
| 安装包 PE 结构 | makensis 3.08（Debian bookworm 容器） | PE32 i386，版本 1.3.18，含图标组 |
| `frpc.exe` 未改动 | 包内文件哈希 | `d9a8cf8bc61bd5c8d57a2a4801316a18291f76d45dc5fd8aaf425bd91b57159d`，等于上游 FRP 0.61.1 |
| 打包完整性 | `unzip -t` / `tar tzf` | 无错误 |
| PowerShell 语法解析（8 个 `.ps1`） | pwsh 7.5.4 | 全部通过 |
| `scripts/tests/windows-missing-file-summary.ps1` | pwsh 7.5.4，合成文件 | 3 项通过 |
| `scripts/tests/uninstall-stale-workspace.ps1` | pwsh 7.5.4，合成目录 | 通过（残留删除，数据目录与无关目录保留） |
| 可复现性 | 干净检出 + 补丁重建、同工作区重复构建 | 三个产物 SHA-256 完全一致 |

本机没有 Windows 目标机：图形安装器、服务注册、17872 监听和内置示例的实机结果仍需按交付说明在目标机验证。PowerShell 侧已用 pwsh 7.5.4 完成语法解析、清单校验测试与卸载残留清理测试，但涉及 Windows 服务、注册表与终端防护的路径只能在目标机验证。

## 验收步骤

1. 在目标机保持终端防护全开。若需要预先登记信任路径，用管理员会话创建 `C:\ProgramData\Kaiwu`（普通用户会话创建的目录会被安装器判为不可信）。
2. 信任区只添加 `C:\ProgramData\Kaiwu` 和 `C:\Program Files\Kaiwu` 这两个产品目录，不关闭防护、不扩大范围。
3. 运行 `kaiwu-connector-<版本>-windows-amd64-setup.exe`，安装详情页应出现 `解包目录：C:\ProgramData\Kaiwu\kw-…`。
4. 安装完成后核对 `%ProgramData%\Kaiwu\Connector\logs\install-workspace.txt` 中的 `workspace=` 行、`Connector\logs\install-attempts\*.jsonl` 的 `extract ok`、服务状态 `Running`、`127.0.0.1:17872` 监听，以及本机页面内置示例返回数据。
5. 卸载后确认 `C:\ProgramData\Kaiwu` 下不再有 `kw-*` 目录，`Connector` 数据目录仍在。

## 不做的事

不修改 `frpc.exe` 二进制，不自动加白，不要求关闭终端防护。
