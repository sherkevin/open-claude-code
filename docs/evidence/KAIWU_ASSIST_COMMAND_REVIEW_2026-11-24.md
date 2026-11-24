# 远程 CMD 协助：合并前代码审查与功能审查（2026-11-24）

## 范围

- 合并 `feat/kaiwu-mcp-check`（远程 CMD 协助）到 `main`。
- 审查对象：该分支相对 `main` 的全部改动（31 个文件，+1920/−58），含合并 `main` 的 attended support portal 提交。
- 审查方式：逐文件读 diff + 依赖方向检查 + 测试与端到端证据核对。

## 结论

**可以合并。** 未发现阻断性缺陷；合并过程中发现并修复 1 个真实缺陷（见下）。剩余风险都是已知、已登记的产品边界，不影响试用交付。

## 合并冲突

| 文件 | 冲突 | 处理 |
| --- | --- | --- |
| `connector/internal/web/onboarding.go` | 本分支新增 `.assist-console` / `.assist-command` 样式，`main` 新增 `.terminal-help` 样式，两段 CSS 都落在同一位置 | 两段都保留，检查无重复选择器；HTML 片段同样两边都保留（远程协助入口 + 命令面板） |

## 合并中修复的缺陷

**`runAssistJob` 用了未合并目标的 offer**（`connector/internal/agent/assist.go`）

- 现象：`go test ./...` 失败于 `TestDeviceOpensAssistBeforeConfirmingMCP`，discover 作业回报 `upstream_failed`、HTTP 状态 0。
- 原因：作业执行读取 `session.offer`（会话开启时的原始 offer，地址为空），而不是 `session.localOffer()`（合并了本机已确认目标）。旧的协议里服务端在地址确认前不发作业，所以这个问题被掩盖；新协议允许命令作业提前下发，问题才暴露。
- 修复：`runAssistJob` 改为在 `a.mu.RLock()` 下取 `session.localOffer()`；同时只允许 `command` 作业在没有确认目标时执行，其他动作收到即视为协议违规、结束会话。
- 覆盖：测试新增 `targetReady` 握手，明确断言「无 MCP 目标时设备也必须接收命令作业」；`go test ./...`、`go test -race ./internal/agent/ ./internal/web/` 通过。

## 逐项审查

### 服务端 `server/app/kaiwu_assist.py`

| 检查点 | 结果 |
| --- | --- |
| 命令长度 | `command` 上限 4000 字符；`\x00` 与纯空白被拒 |
| 超时 | `timeout_seconds` 5–120 秒，`strict=True`（拒绝 `"60"` 字符串）；运行租约 `max(RUN_TTL, timeout+30)` |
| 输出上限 | 65536 字符 + 131072 字节双限制 |
| 结果校验 | 命令作业只接受 `command_completed`（必须带 `exit_code`）/ `command_failed` / `command_timeout` / `canceled`；携带 MCP 字段即 422 |
| 作业排队 | 命令作业不得携带 `bearer_token` / `dynamic_auth` / `tools`；非命令作业不得携带 `command` |
| 目标门控 | 轮询时非命令作业在 `url` 为空时不下发；命令作业不受此限制 |
| 并发 | 同一会话同时只允许一个 queued/running 作业（部分唯一索引 `kaiwu_assist_job_live`） |
| 授权 | 只有会话所有者（认领后的工程师）能排队；`_require_recent_consent` 仍对命令生效 |
| 审计 | 命令入队与结果都写 `kaiwu_diagnostic_events` |
| 兼容旧连接器 | `accept_jobs` 默认 `True`；旧连接器在地址确认前不发 `accept_jobs=true`，因此不会收到命令，属安全降级 |

### 数据库 `server/app/db.py`

- `action` 的 CHECK 约束扩到 `('discover','connect','command')`。SQLite 不能原地放宽 CHECK，因此做单事务表重建：`CREATE ... _new` → `INSERT ... SELECT`（`command` 置 NULL、`timeout_seconds` 置 60）→ `DROP` → `RENAME` → 重建 `kaiwu_assist_jobs_session` 与 `kaiwu_assist_job_live` 两个索引。
- 全部语句走 `conn.execute` 且在已打开的 `BEGIN IMMEDIATE` 事务内，不用 `executescript`（后者会先提交，可能留下半成品表）。
- 已在生产库核对：`command`、`timeout_seconds` 两列存在，两个索引存在。

### 连接器命令执行 `connector/internal/agent/assist_command*.go`

- Windows：`cmd.exe /d /s /c "chcp 65001 >NUL & <command>"`，`CREATE_NO_WINDOW`；输出按 CP936 解码（`MultiByteToWideChar(936,...)`），非 Windows 走 `sh -c`。
- 超时：`exec.CommandContext` + 自定义 `Cancel` 调 `taskkill /T /F` 终止整棵进程树，`WaitDelay = 5s` 防止管道未关闭导致挂死。
- 输出缓冲固定 64 KiB，`Write` 始终返回 `len(data)`，子进程不会因管道写满而阻塞。
- 结果状态：`succeeded` / `failed` / `timeout` / `canceled`，与页面显示分支一一对应（无死分支）。

### 客户端与页面

- 工程师端 `KaiwuAssistDialog.tsx`：命令输入、30/60/120 秒超时选择、最近 20 条作业历史；轮询与既有会话刷新共用同一节奏。
- 客户机页面 `onboarding.go`：命令面板展示命令内容、状态与输出（折叠），停止协助时清空；`renderAssist()` 在 `local` 会话下才显示，避免把服务器状态误当本机状态。

### 安全边界

- 连接器只监听回环 `127.0.0.1:17872`；出站 HTTPS/WSS 到 `flintw.com`。
- 命令只在客户已开启、工程师已认领、且客户侧近期有轮询（`_require_recent_consent`）的会话中执行。
- 设备签名（Ed25519）+ 设备令牌；bridge API 仅回环、需 bridge secret 文件与 `X-Kaiwu-Operator`。
- 命令文本以明文存于 `kaiwu_assist_jobs.command`（MCP 凭据是密封存储的）。可见范围是协助作用域（试用租户或指定设备）内的操作员：会话列表/详情接口不带 owner 过滤，同一作用域的其他操作员能看到最新作业的命令文本；**下发**命令才要求会话所有者（`_session(..., owner=user["id"])`）。试用期内可接受，正式多客户上线前应改为密封存储或按所有者收窄读权限。

## 功能审查（真实链路）

| 场景 | 结果 |
| --- | --- |
| 回环 E2E（`scripts/kaiwu_assist_command_e2e.py`） | PASS：`kaiwu-assist-e2e-ok` 4 ms；`sleep 30` 5,002 ms 超时终止 |
| 生产控制面 `https://flintw.com` | PASS：会话 `asst_e03823ade7aa485c91694afe6369a427`；`echo kaiwu-prod-e2e-ok` 2 ms；`sleep 30` 5,001 ms `command_timeout`；停止后设备回收 |
| 客户实机（赵鹏，Windows 11） | PASS：`hostname & ver` → `LAPTOP-S5UA26C7` / `Microsoft Windows [版本 10.0.26200.9974]`，179 ms；`dir D:\` 81 ms |
| Windows Server 2019 探针 | PASS（`docs/evidence/KAIWU_ASSIST_COMMAND_WIN2019_2026-11-24.md`） |
| 生产 UI | 已发布并公网可见（`index-B80SXrXT.js`、`ConnectionsPage-CkAxoZf6.js`） |

## 遗留风险（不阻断）

1. 安装包未做代码签名，Windows 显示「未知发布者」。
2. 命令文本明文入库；如需合规留存，应改为密封存储并加保留期。
3. 命令在 `cmd.exe` 中执行，属于高权限操作；生产环境应配合设备白名单与操作审计（审计事件已有）。
4. 飞书交付走用户身份（`im:message:send_as_user`），应用仍缺群聊读取权限。
