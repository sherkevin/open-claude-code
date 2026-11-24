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

## 真实控制面端到端联调

探针之外，另用真实服务端进程（uvicorn 加载 `server/app/main.py`）加编译后的连接器二进制做了一次端到端：

| 步骤 | 结果 |
| --- | --- |
| 管理员登录 | 通过（`assist-admin`，角色 admin） |
| 设备注册 | 通过（签发注册码，连接器绑定成功） |
| 客户侧开启协助 | 通过（`asst_961f87f9b1754158aa1ef51bdfe3d0d0`） |
| 工程师认领会话 | 通过（设备在线且可见） |
| 命令 1 `echo kaiwu-assist-e2e-ok` | `succeeded` / `command_completed`，退出码 0，输出 `kaiwu-assist-e2e-ok`，耗时 2 ms |
| 命令 2 `sleep 30`（超时 5 秒） | `failed` / `command_timeout`，5,002 ms 后被终止 |
| 客户侧停止协助 | 会话状态 `stopped` |

报告：`/media/data3/dengkw/acceptance/kaiwu-assist-real-e2e/report.json`。
脚本：`scripts/kaiwu_assist_command_e2e.py`，可用 `CONNECTOR_BINARY=<路径> python3 scripts/kaiwu_assist_command_e2e.py` 复跑。

该联调在生产控制面同款代码（直接协助模式）上执行，但服务运行在本机回环，尚未部署到 `flintw.com` 生产实例。

## 生产控制面端到端（flintw.com）

2026-11-24 把提交 `13b2aad` 发布到生产：控制面 `kaiwu-control.service`（:18093，发布目录 `releases/assist-cmd-13b2aad35a0a.Ou0NLt0i/server`）与工作台 `kaiwu.service`（:18092，发布目录 `.releases/20261125-hk-assist-cmd-ui-13b2aad.JoGXts1T`，含远程 CMD 控制台）。两个服务均为 `active`，健康检查通过。

随后用 Linux 连接器连真实 `https://flintw.com` 控制面，完成一次完整会话：

| 步骤 | 结果 |
| --- | --- |
| 设备注册 + 连接器绑定 | 通过（`conn_7c3f92b9ea9f40bca658cc7f991e0f5a`，租户 `tenant-kaiwu-assist-selftest-20261123`） |
| 客户侧开启协助（`POST /api/assist/start`） | 通过（`asst_e03823ade7aa485c91694afe6369a427`，30 分钟有效期） |
| 工程师认领会话 | 通过（`active`） |
| 命令 1 `echo kaiwu-prod-e2e-ok` | `succeeded` / `command_completed`，退出码 0，输出 `kaiwu-prod-e2e-ok`，耗时 2 ms |
| 命令 2 `sleep 30`（超时 5 秒） | `failed` / `command_timeout`，5,001 ms 后被终止 |
| 客户侧停止协助 | 会话状态 `stopped` |
| 临时设备回收 | `revoked: true` |

排查记录：首次 `POST /api/assist/start` 返回 `OPERATION_FAILED`，原因是连接器启动时没带 `--native-relay`（`canAssistLocked` 要求原生中继），补上该参数后会话立即建立；这不是服务端缺陷。

生产 UI 产物：

| 文件 | SHA256 |
| --- | --- |
| `index.html` | `d922fdca323f6c435f046f92c572ea85d2ced1b0e29d81ca5037d7729433a815` |
| `assets/index-B80SXrXT.js` | `f6f109cfbf6ae5cae4162d8c00b666054747dfe9597b1c366a41a058c37e948b` |
| `assets/ConnectionsPage-CkAxoZf6.js`（远程 CMD 控制台） | `bfde585b8b57adde43aa5ed72deefac4849f12ff3e081c98b047db1c100f27f9` |

公网 `https://flintw.com/project/kaiwu/` 已返回新 `index-B80SXrXT.js`，控制台所在 chunk 可通过公网取到。发布脚本：`/media/data3/dengkw/acceptance/kaiwu-150-ui-20261124/deploy-ui.sh`（新建发布目录 + 清单校验 + 失败自动回滚）。UI 包由生产同源的 `4f9627b` 客户端加上本提交的两个文件构建，产物分片集合与上一版完全一致（29 个分片，无新增/丢失）。

## 交付物

| 文件 | 大小 | SHA256 |
| --- | --- | --- |
| `kaiwu-connector-1.5.0-native-windows-amd64-setup.exe` | 3.8 MB | `018ad88b8817f7b3747d806b7e64b738a84a010393ecab639607664f758d0764` |
| `kaiwu-connector-1.5.0-native-windows-amd64.zip` | 4.5 MB | `d78fd77a6c1fc7e1dac542d88fe7ff0f2fab2b4b6d0b71b59733743d7af511d8` |

构建信息：`version=1.5.0`、`commit=13b2aad`、`transport=native-wss`；`govulncheck` 无漏洞。安装包未签名（本机没有可用代码签名证书）。

交付记录：2026-11-24 11:01（东八区）以邓凯文的飞书身份私聊发送给赵鹏（`ou_d0c47fb5f4a601c16286033f78c81005`，会话 `oc_38af83bdc03aba63b782135b7924ad6d`），文字消息 `om_x100b7f7b91e44880c30560116eb3f5b`，安装包 `om_x100b7f7b917204b8dfba97191c77972`。交付目录：`/media/data3/dengkw/acceptance/kaiwu-150-delivery-20261124/`。

## 客户实机（赵鹏，Windows 11）

赵鹏安装 1.5.0 后在本机配置页点击「开始协助」，生产控制面随即出现会话 `asst_00e30dceade74dce8c5808612a5f6e67`（设备 `conn_7b7eb2d128243d8a3ec59eada396500`，租户 `tenant-huawi-win11-pilot`）。工程师认领后下发只读命令：

| 项 | 结果 |
| --- | --- |
| 命令 | `hostname & ver` |
| 状态 | `succeeded` / `command_completed`，退出码 0，耗时 179 ms |
| 回传输出 | `LAPTOP-S5UA26C7`；`Microsoft Windows [版本 10.0.26200.9974]` |

即客户电脑上的连接器自动拉取命令、在 `cmd.exe` 执行并把输出回传，工程师无需连入客户内网。会话由客户侧控制，验证后保持开启，未代为停止。

## 未覆盖

- 安装包未做代码签名，Windows 会显示未知发布者提示。
- 飞书私聊发送使用用户身份（`im:message:send_as_user`）；应用 `cli_aa5e2ae5a638dd0d` 仍缺 `im:chat:readonly` 等群聊权限，机器人身份只能进群后发消息。


## 会话保活（2026-11-24 晚）

问题：客户每次点击「开始协助」后约 30 分钟会话即失效，必须重新点击，来回反复。

原因有两层：

1. 服务端会话有效期在创建时固定，轮询只更新 `last_poll_at`，不延长 `expires_at`。
2. 连接器自身用 `assistLifetime`（30 分钟）作为会话 context 的截止时间，到点主动停轮询。

修复：

| 侧 | 提交 | 内容 |
| --- | --- | --- |
| 服务端 | `c3fc75b` | `/assist/poll` 每次轮询把 `expires_at` 顺延 `SESSION_TTL`（30 分钟，滑动租约）；停止轮询 30 分钟后自然过期 |
| 连接器 | `a7ec767` | 会话 context 改为 24 小时本地安全上限；轮询失败按 3 秒→30 秒指数退避重试；结果上报最多 5 次重试；成功轮询同步顺延本地 `ExpiresAt`；确认 MCP 时比较身份（会话/状态/名称/地址）而不是会滑动的有效期 |

生产发布（均校验运行目录内 `kaiwu_assist.py` 含滑动续期语句）：

| 服务 | 目录 |
| --- | --- |
| `kaiwu-control.service`（连接器 `/api/kaiwu/` 入口，:18093） | `/data/dengkw/workspace/kaiwu-enterprise-bridge-20261119/releases/assist-keepalive-c3fc75b.EcUcwp70/server` |
| `kaiwu.service`（工作台，:18092） | `/data/dengkw/workspace/huawi-preview-20261115/.releases/20261125-hk-assist-keepalive-c3fc75b.PteiCHdb` |

健康检查 `https://flintw.com/api/kaiwu/v1/health` 返回 `{"status":"enabled","version":"control-plane-1"}`。

测试：`server/tests/test_kaiwu_assist.py::test_polling_renews_the_session_lease`（服务端续期与无轮询过期）、`connector/internal/agent/assist_test.go::TestAssistOutlivesTransientPollFailure`（首次轮询 502 后会话仍存续并顺延）；Go 全量测试通过，`govulncheck` 0 个可调用漏洞。

连接器 1.5.1 交付包（提交 `a7ec767`，`native-wss`）：

| 文件 | 大小 | SHA256 |
| --- | --- | --- |
| `kaiwu-connector-1.5.1-native-windows-amd64-setup.exe` | 3,966,436 | `2195d0a178e4e378cdf494748821d2d3ec313e661bbef219a4ce3a8749907794` |
| `kaiwu-connector-1.5.1-native-windows-amd64.zip` | 4,693,777 | `760dd3ba2ed85bfae7043327ebc35ccb88ee2f7fcf46be2b59a05cb5ff2c0fe5` |

交付目录：`/media/data3/dengkw/acceptance/kaiwu-keepalive-20261124/delivery/`（含 `交付说明.md`）。

交付记录：2026-11-24 15:51（东八区）以邓凯文的飞书身份私聊发送给赵鹏（`ou_d0c47fb5f4a601c16286033f78c81005`）：文字说明 `om_x100b7f6051e4b880dc0be3432be05b6`，安装包 `om_x100b7f606d93acacde0042c1cb1fc43`。

客户实机复核（赵鹏 Windows 11，2026-11-24）：17:00 前完成 1.5.1 覆盖安装，心跳显示 `1.5.1 (commit a7ec767, built 2026-11-24T07:33:45Z)`、状态 `READY`；16:34:35（东八区）点击「开始协助」，会话 `asst_4b1cc125f4d54caba3b2089912270021` 建立。17:07:00 / 17:07:10 / 17:07:20 三次取样（已越过旧版 30 分钟截止点 17:04:35）：状态均为 `active`，`last_poll_at` 始终在取样前 1 秒内，`expires_at` 每次顺延到 `last_poll_at + 30 分钟`（17:36:59 → 17:37:08 → 17:37:20）。保活已在客户实机成立。

未覆盖：更长时间（小时级）仍靠同一机制，未再单独实测；安装包未签名。
