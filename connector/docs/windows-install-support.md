# 开物连接器 Windows 安装支持收集

安装窗口显示失败后，记下窗口中的安装编号。对接人只需把 `collect-install-support.ps1` 交给本机操作人。脚本可在连接器尚未安装完成、服务尚未启动或设备尚未绑定时运行，使用 Windows PowerShell 5.1 自带的命令和接口，离线采集并把报告输出到控制台。

在脚本所在目录运行以下命令。把示例值替换成窗口中的 32 位小写十六进制安装编号。

```powershell
powershell.exe -NoProfile -NonInteractive -File .\collect-install-support.ps1 -AttemptId 0123456789abcdef0123456789abcdef
```

需要报告文件时，在 Windows PowerShell 中运行下列命令。报告文件由命令中的 `Set-Content` 保存，收集器只读取资料并输出报告。

```powershell
powershell.exe -NoProfile -NonInteractive -File .\collect-install-support.ps1 -AttemptId 0123456789abcdef0123456789abcdef |
  Set-Content -LiteralPath .\kaiwu-install-support.json -Encoding Ascii
```

只需转交生成的报告。报告包含输入的安装编号、白名单内的产品文件标识和检查结果、有上限的事件序号、已校验的安装阶段和事件码，以及本机查询状态。工具读取的文件范围限于安装器在 Windows 公共应用数据目录保存的安装包检查文件和指定编号的事件日志。

## 如何读报告

| 字段或状态 | 含义 |
| --- | --- |
| `package_checkpoints` | 安装包检查。安装器只保留最近一次成功保存的检查文件。 |
| `available` | 检查文件中的编号与输入一致，所列检查行通过格式校验。安装可能在首项失败时停止，因而记录可以只有一行。 |
| `different_attempt` | 检查文件属于另一次安装。报告省略其编号和检查行。 |
| `install_events` | 输入编号对应的安装阶段事件。每条记录包含序号、阶段、结果和代码。 |
| `complete` | 事件从起始记录连续到结束记录。安装结果看结束记录的 `outcome`，阶段细节看各条事件的 `code`。 |
| `incomplete` | 检查文件出现截断迹象，或事件日志缺少完整结束记录。事件可以保留已校验的连续开头。 |
| `invalid`、`size_limit`、`unsafe_path` | 资料格式不符、源文件超过 4096 字节，或资料路径未通过安全检查。格式异常的事件日志只保留异常之前已校验的连续记录。 |
| `missing`、`unavailable` | 资料不存在，或当前无法读取。若因受保护目录权限无法读取，可由有权限的本机操作人复跑。 |
| `current_local_status` | 查询当下的 `KaiwuConnector` 服务和本机端口 `17872` 的传输控制协议监听状态。服务查询可能返回 `running`、`stopped`、`not_found` 等，端口返回 `listening` 或 `not_listening`。查询无法执行时返回 `unavailable`。 |

服务状态和端口监听只描述采集时的观察。监听范围是本机任一地址，端口所属进程、页面健康和安装失败根因需要独立证据。正式检查文件没有预期行数，若内容恰好截断在完整行末，工具只能报告剩余的通过校验的行。早期安装失败、记录被覆盖或清理等情况可能留下很少资料，报告按实际读取结果显示状态。

读取前会检查目录和文件的链接属性。属性检查与文件打开分两步执行，收集依赖安装器保护资料目录，无法防止有写权限的进程恰在两步之间替换路径。

有效编号生成报告时退出码为 `0`，包括无资料或无法查询的情况。编号不合法时输出固定错误并以 `2` 退出。

## 离线样本复跑

从研发树根目录运行测试。测试在脚本目录下创建并清理临时模拟记录，服务与监听查询使用模拟输入。

```powershell
powershell.exe -NoProfile -NonInteractive -File .\connector\scripts\test-collect-install-support.ps1
```

在已有 PowerShell 7 的开发机上也可执行同一套样本。

```sh
pwsh -NoProfile -NonInteractive -File connector/scripts/test-collect-install-support.ps1
```
