# Windows 安装器关键依赖失败与恢复核验

## 任务边界

本任务只修复 Windows 安装事务在关键依赖缺失、目标文件丢失、备份缺失或损坏时的失败和恢复判定，不解决也不绕过安全软件对 `frpc.exe` 的检测。外部备份脚本由原负责人维护，本任务没有修改它。

- 隔离工作树：`/media/data3/dengkw/projects/open-claude-code-huawi-installer-transaction-20261119`
- 分支：`fix/installer-dependency-failure-20261119`
- 冻结基线：`b54e54a18f63b1b8e19f7db374ca4633b5b05ddf`
- 本轮修改：`connector/packaging/windows/install.ps1`
- 保留的前轮关联修改：`connector/packaging/windows/installer.nsi`
- 新增故障注入脚本：`connector/scripts/installer-fault-injection.ps1`
- 没有修改版本号，没有提交、打包、部署或操作 Windows、UAC、Shervin、云端和客户设备。

## run5 三项 P1 的实际修正

### 1. 产品内嵌 restore.ps1 按清单恢复

产品安装器在备份目录生成的 `restore.ps1` 已不再用通配符复制现存 EXE，也不再直接复制元数据文件。

恢复入口现在执行以下顺序：

1. 计算并核对内嵌的备份清单 SHA-256。
2. 校验清单版本、条目数量、重复项、类别、相对路径、备份相对路径和每一项 SHA-256。
3. 校验每个备份文件存在且摘要匹配。
4. 只把清单中 `program` 和 `data` 类别恢复到安装目录或数据目录。`metadata` 类别只用于服务、环境变量和说明，不会写入业务数据目录。
5. 全部目标文件摘要通过后，才恢复机器级环境变量。
6. 服务快照存在时，才重建旧服务；旧服务安装前运行，才启动它。
7. 启动后复核稳定程序和配置；`state.json` 是服务启动会合法改写的运行态文件，启动后不参与逐字节比较。

核验失败时不会创建或启动旧服务，也不会输出“已恢复安装前版本”。清单自身摘要被嵌入生成的 `restore.ps1`，恢复脚本还会独立重新验证每个备份文件。

产品内嵌恢复脚本不是外部 `reviewed-backup-138.ps1`，本任务未接管外部备份脚本。

### 2. state.json 正常变化不误判

自动恢复仍然在启动旧服务前执行完整清单目标摘要校验。启动旧服务后改用稳定状态核验：

- 复核关键程序 `kaiwu-connector.exe`、`frpc.exe`。
- 复核除 `state.json` 外的 `program`、`data` 条目。
- 复核旧服务名称、程序路径、运行状态和旧版本信息。

因此旧服务加载身份后把 `state.json` 从旧状态改写为 `CONFIGURING`，不会被误判为恢复破坏，也不会触发额外的强制停止。测试脚本中的 `Start-Service` 替身会实际改写 `state.json`，用于覆盖该行为。

### 3. 受控停止，不使用 Stop-Service -Force

安装器停止和删除服务时现在统一执行：

- 先读取目标服务的 `DependentServices`；存在依赖服务时直接失败，不调用停止，也不删除目标服务。
- 只对目标服务调用 `Stop-Service`，不使用 `-Force`。
- 使用 `-ErrorAction Stop`，停止失败不得继续删除服务。
- 停止后轮询确认状态为 `Stopped`，再调用 `sc.exe delete`。
- 删除后等待服务记录消失。

产品内嵌 `restore.ps1` 的当前服务替换和失败补偿也使用同一套受控停止规则，不连带停止其他服务。安装器仍在 `sc.exe` 删除失败时报告恢复未确认，不会把删除失败当成成功。

## 可执行故障注入脚本

脚本：

`/media/data3/dengkw/projects/open-claude-code-huawi-installer-transaction-20261119/connector/scripts/installer-fault-injection.ps1`

运行方式（必须在已提升权限的 Windows PowerShell 5.1 中运行，因为被测 `install.ps1` 保留 `#Requires -RunAsAdministrator`）：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  C:\path\to\connector\scripts\installer-fault-injection.ps1 `
  -OutputPath C:\path\to\installer-fault-injection-result.json
```

脚本把实际 `install.ps1` 原文件复制到隔离源目录后调用，避免改写仓库中的安装器；同时把 `ProgramFiles`、`ProgramData` 和 `TEMP` 指向临时目录，并用函数替身覆盖服务、ACL、HTTP、`sc.exe`、文件复制和机器级环境变量读取/写入。服务、注册表、ACL 和生产目录不会发生真实操作。环境变量替身只是平台调用接缝，不跳过任何清单、摘要、路径或服务安全检查。脚本输出机器可读 JSON，任一用例失败时退出非零。

故障注入脚本故意放在 `connector/scripts/`，不放在 `connector/packaging/windows/`。构建脚本会把 `packaging/windows/*.ps1` 复制进 Windows 客户 payload；测试脚本放在 `scripts/` 可以调用真实安装器，同时不会被客户包分发。

### 用例清单与实际状态

本机没有现成 PowerShell 运行时，无法执行 Windows PowerShell 5.1 故障注入，因此下列结果全部是 `not_run`，不是实机通过结果。没有下载、安装或启动新的运行时。

| 编号 | 场景 | 实际状态 | 脚本验收点 |
| --- | --- | --- | --- |
| A | 首次校验前源 `frpc.exe` 缺失，且已有运行中的业务服务和配置 | not_run | `PRECHECK_FAILED`；旧服务保持运行；停止和删除次数为 0；业务配置和旧程序摘要不变 |
| B | 预校验后复制时源 `frpc.exe` 消失 | not_run | 失败后通过清单恢复旧程序、配置和运行中的旧服务 |
| C | 目标 `frpc.exe` 在复制后消失，服务和 HTTP 替身仍返回正常 | not_run | 最终目标摘要检查失败，不得返回 `INSTALLED` |
| D1 | 破坏自动恢复所用的备份清单 | not_run | `FAILED_RECOVERY_UNCONFIRMED`，不得启动混合内容旧服务 |
| D2 | 破坏实际生成的 `restore.ps1` 所示备份 | not_run | 生成恢复入口拒绝清单，停止和删除调用均为 0 |
| E | 首装复制第一个 EXE 后失败 | not_run | 只删除本轮新增受管文件；预存无关文件保留 |
| F | 正常恢复时 `Start-Service` 改写 `state.json` | not_run | 恢复仍为 `FAILED_RESTORED`，服务保持运行，不追加误停 |
| G | 目标服务存在依赖服务 | not_run | 其他及目标 `Stop-Service` 调用为 0，目标服务不删除 |

上述脚本已经包含对应用例和故障注入钩子，但“文件存在”不等于“Windows 实机执行通过”。必须在隔离 Windows 环境运行后才能把状态改为 passed。

## 已执行检查

| 检查 | 状态 |
| --- | --- |
| `git diff --check` | 通过 |
| `install.ps1` UTF-8 BOM | 通过 |
| 故障注入脚本 UTF-8 BOM | 通过 |
| 工作树基线为约定冻结提交 | 通过 |
| 测试脚本未放入客户 payload 的 `packaging/windows` 目录 | 通过 |
| 安装器内不再出现 `Stop-Service -Force` | 通过（静态文本检查） |
| 机器级环境变量调用保留平台替身接缝，测试脚本不调用真实 Machine API | 通过（静态契约检查） |
| 内嵌恢复脚本包含清单摘要和完整条目校验 | 通过（代码审查和静态契约检查） |
| 自动恢复启动后跳过 `state.json` 全量比较 | 通过（代码审查和静态契约检查） |
| PowerShell 运行时、Windows 服务、ACL、UAC 实机验收 | 未执行 |
| 故障注入 A-G | 未执行，无现成 PowerShell 运行时 |
| 安全软件误报、清除或绕过行为 | 未执行，本任务也不声称解决 |
| 版本修改、构建、打包、发布、提交 | 未执行 |

静态检查不能替代 Windows、PowerShell 5.1、服务、ACL 或安全软件实机验收。

## 文件摘要

以下摘要在本报告冻结时计算；如果后续有文件变更，应重新计算，不应把它们当作有效验收凭据。

- `connector/packaging/windows/install.ps1`: `92748102c6603880b10ef8b794707c5efd8d4c5d2271e029b8b351e219454c31`
- `connector/packaging/windows/installer.nsi`: `3366fced3969ee61392741d728d69f5767db4a6a56fb7afb9b754fe48646ef5e`
- `connector/scripts/installer-fault-injection.ps1`: `67867d249ded6e15fcba8c80e340ce774a58417e323e77491c0a81a092e2e190`

## 未执行的范围

- Windows、Server 2019、UAC、服务、ACL、实际安装和实际 HTTP 服务操作。
- 真实服务、注册表或 ProgramFiles 操作。
- Shervin、客户设备、云端部署和群消息。
- 外部备份脚本修改。
- 版本修改、构建、打包、发布和提交。

本任务只交付可读代码差异、可执行测试脚本和未运行的测试清单，等待总控复审。
