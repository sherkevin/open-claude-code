# Windows Server 2019 远程 CMD 协助通道验收（2026-11-24）

## 结论

通过。连接器在 Windows Server 2019 上完成了「客户只执行一次建联命令，后续命令自动拉取、执行、回传」的完整链路：

- 在客户端本机执行 `kaiwu-connector assist` 后，连接器主动轮询开物的协助会话；
- 工程师下发的命令由连接器在本机 `cmd.exe` 执行，输出、退出码、耗时、截断标记自动回报；
- 超时命令被整棵进程树终止，并回报 `command_timeout`；
- 全程不需要开物侧主动连入客户内网。

## 环境

| 项 | 值 |
| --- | --- |
| 虚拟化 | QEMU 2 vCPU（TCG，无 KVM），4 GB 内存，UEFI |
| 客户机 | Windows Server 2019 Datacenter 10.0.17763.9020 |
| 系统盘 | 冻结基线 `server2019-datacenter-baseline.qcow2` 的临时覆盖层 |
| 构建工具链 | Go 1.27.4，`GOOS=windows GOARCH=amd64` |
| 代码基线 | 分支 `feat/kaiwu-mcp-check`，远程 CMD 协助改动（与本证据同一次提交） |

## 方法

`assistprobe.exe` 在客户机内启动一个仅回环（`127.0.0.1`）的假控制面，直接驱动真实的连接器 agent：

1. `Bind` 绑定临时设备；
2. `OpenAssist` 开启协助会话；
3. agent 按 3 秒间隔轮询 `/assist/poll`；
4. 第一道作业：`echo assist-windows-ok`，超时 10 秒；
5. 第二道作业：`ping -n 10 127.0.0.1 >NUL`，超时 1 秒，验证超时终止；
6. 两道作业的结果都必须自动 POST 回 `/assist/result`。

客户机控制台与 COM1 串口同时留证。

## 结果

```
KAIWU_ASSIST_CMD_PROBE_BEGIN
step bind at +0s
step open-assist at +331ms
step first-result at +2.029s
ok: command output was captured and reported automatically
step second-result at +6.574s
ok: command timeout stopped the process and reported command_timeout
PASS: Windows command execution and automatic report
KAIWU_ASSIST_CMD_PROBE_EXIT 0
```

首次运行时探针自带 40 秒总等待预算，在冷启动的 TCG 客户机上等待超时（`FAIL: timed out waiting for command result`）。把探针预算放宽到 300/480 秒后，同一台客户机 6.6 秒内完成全部步骤。该失败属于探针自身的等待预算，不是连接器或服务端行为；连接器侧的作业超时由服务端 `timeout_seconds`（5–120 秒，默认 60 秒）独立控制。

## 产物

| 文件 | 大小 | SHA256 |
| --- | --- | --- |
| `kaiwu-connector.exe`（1.5.0 候选） | 11,625,472 | `e465d16598a69752a479d49c3d13c56fb49dc0a77953e12b2f148a240aa3b862` |
| `assistprobe2.exe`（验收探针） | 15,431,680 | `1b67a55fa091fb11de613d3378daf5c90b197e6b9c7b1ad0c7a4b6bf50cb3d9b` |
| `tools2.iso`（含 PROBE.EXE 与 RUN.CMD） | 15,804,416 | `79e18692586fae5faa0ee5409129c03c80aef4f79094c0d25ca8d8021781c6b1` |
| `guest-serial.txt`（COM1 串口记录） | — | `1eead45cd06ecec8ccaf28050d2e57b0aac2a36332e959b13e487f53fec330b8` |

产物目录：`/media/data3/dengkw/acceptance/kaiwu-150-win2019/`。
截图：`42-probe-pass.png`（PASS 控制台）、`17-password-typed.png`、`18-login-after.png`。

## 未覆盖

- 尚未对真实开物控制面做端到端联调；本验收使用回环假控制面驱动同一份 agent 代码。
- 尚未在赵鹏的 Windows 11 / Windows Server 2019 实机验证。
- 服务端命令接口只在本仓库测试套件中验证（`server/tests/test_kaiwu_assist.py`），尚未部署。
