# 2026-11-26｜E2.2 Codex App Server 产品宿主适配切片（默认关闭，未执行测试）

工作树 `/media/data3/dengkw/projects/kaiwu-codex-host-20261126`，基线 HEAD `c90b204`。本轮只新增默认关闭的 Codex App Server 产品宿主模块、产品 API、synthetic stdio peer 测试与复验探针；没有启动真实 Codex 二进制、没有模型 ID、没有外网模型请求、没有真实登录态/Codex token/配置、没有终端或业务工具、没有审批放行。**本轮代码和测试由实现工程师写入，但实现环境只有受控文件工具，不能运行 shell，因此以下测试与探针均未执行；表中的预期结果不是已通过证据。**

## 1. 复用与边界

- 只复用既有固定 0.165.0 原生 JSON-RPC stdio 与 schema 基线，见 [协议基线](KAIWU_CODEX_APPSERVER_SCHEMA_2026-11-26.md) 和 [无外网 stdio 元数据](KAIWU_CODEX_APPSERVER_STDIO_2026-11-26.md)。
- 没有重写 Codex，没有新增通用代理框架。宿主只做 owner 绑定、线程/轮次生命周期、stdout 事件身份校验、取消/中断和恢复准入。
- 不新增第三方依赖；宿主只用 Python 标准库（`subprocess`、`queue`、`fcntl`、`hashlib`、`json`）。
- 真实 provider turn 明确不在本切片内。本宿主不会自行选择模型、发送外网请求或读取登录态。

## 2. 新增入口

| 入口 | 位置 | 说明 |
|---|---|---|
| 宿主模块 | `server/app/codex_appserver.py` | `CodexHost` / `CodexHostConfig`；进程级短期 app-server，按操作启动关闭 |
| 产品 API | `server/app/conversation_codex.py` | `/api/v1/conversations/{id}/codex` GET/POST thread/turn/interrupt/reconcile |
| 配置开关 | `server/app/config.py` | `AGENT_CODEX_APPSERVER_*`，默认 `enabled=false` |
| 装配 | `server/app/main.py` | 仅非 trial 且开关开启时挂载；`create_app(..., codex_host_factory=None)` 测试注入口 |
| synthetic peer | `scripts/oss/codex_appserver_fake_peer.py` | 无网络、无模型、无工具的确定性 JSON-RPC peer |
| 独立探针 | `scripts/oss/codex_appserver_host_probe.py` | 8 项 synthetic 生命周期检查；`--binary` 只校验摘要，不执行 |
| 测试 | `server/tests/test_codex_appserver_host.py`、`server/tests/test_conversation_codex.py` | 宿主 14 项、产品 API 6 项，共 20 项 |

## 3. 已实现的安全与生命周期边界

1. **owner 绑定**：状态文件路径由 `sha256(user_id\0conversation_id)` 生成；文件内再次校验 `user_id`、`conversation_id`、工作区字符串和 `thread_id`。产品 API 先走既有 `service.require("conversations", cid, user)`，其他用户得到 404。
2. **工作区隔离**：每个 owner/conversation 使用 `data_dir/codex-appserver/workspaces/<owner-hash>/<conversation-hash>`，目录 0700、逐级校验非符号链接和属主；模型/用户不能提供路径。
3. **进程环境隔离**：子进程 `env` 由宿主白名单重建，只含 `PATH`、隔离 `HOME`、`CODEX_HOME`、`TMPDIR`、`XDG_*`、`LANG/LC_ALL`、`NO_COLOR`；不继承产品服务环境、代理或任何 provider/Codex 凭据。
4. **固定执行档位**：`thread/start` 使用 `sandbox="read-only"`、`approvalPolicy="never"`、`ephemeral=false`；`thread/resume` 重复同样档位；不发送 `model` 字段。
5. **审批与未知请求**：所有 server request（3 类已知审批和未知方法）都回复 JSON-RPC `-32601` error，不返回 `result`、不放行任何能力；未知或跨线程通知不进入回执。
6. **线程/轮次身份**：`thread/start`、`thread/resume`、`thread/read`、`turn/start`、`turn/interrupt` 的响应都校验精确 `threadId`/`turnId`；delta 和完成通知必须精确匹配当前 `turnId`，出现跨线程通知立即关闭会话并保留不确定标记。
7. **幂等与不重放**：同一 `client_request_id` + 相同正文返回已持久化回执，不再次 `turn/start`；同 id 不同正文拒绝。跨进程用 per-binding `flock` 串行，第二个进程得到 busy。
8. **取消/中断**：只有当前产品进程持有该 turn 的 app-server 会话时，`interrupt` 才会向精确 `threadId/turnId` 发送 `turn/interrupt`；重启后活动标记保留，不能从新进程可靠取消，必须显式 `reconcile`。
9. **恢复边界**：`reconcile` 先 `thread/resume`，再 `thread/read(includeTurns=true)`；已终态则清除，未终态则发送精确 interrupt 并在有界时间内轮询终态；始终不重放原始输入。崩溃、超时或启动回执不明确时保留 `active_turn_id` 或 `uncertain_request_id`，后续 turn 被拒绝。
10. **预算**：输入字符数、输出字符数、回执持久化长度、通知条数、响应/轮次/恢复超时、单轮锁均有上限；没有模型费用硬预算。

## 4. 未执行测试矩阵（预期结果）

| # | 用例 | 预期 |
|---|---|---|
| 1 | `test_disabled_host_does_nothing` | 默认关闭不建目录、不启动进程 |
| 2 | `test_thread_start_is_owner_scoped_and_read_only` | 线程 cwd 在宿主私有工作区；`read-only`/`never`；另一 owner 看不到绑定；`HOME`/`CODEX_HOME` 隔离；无 provider/代理变量 |
| 3 | `test_turn_receipt_identity_and_idempotent_replay` | 回执线程/轮次精确；相同请求重放不新增 `turn/start`；同 id 不同正文拒绝；第二轮先 `thread/resume`；有界近期请求历史仍可重放旧请求 |
| 4 | `test_server_requests_are_always_rejected` | 已知审批与未知 server request 都收到 `-32601` error，无 `result`，回执计数增加 |
| 5 | `test_mismatched_notification_fails_closed` | 跨线程 delta 使 turn 失败并保留 recovery 标记；后续 turn/prepare 被拒绝 |
| 6 | `test_turn_timeout_leaves_uncertain_state` | 无完成通知时超时，活动标记持久保留 |
| 7 | `test_crash_then_reconcile_terminal_turn` | 进程崩溃后显式 reconcile 读取终态并清除，不重放 |
| 8 | `test_reconcile_in_progress_interrupts_exact_turn` | 未终态 turn 用精确 interrupt 收敛为 `interrupted` |
| 9 | `test_restart_resumes_the_same_thread` | 新宿主实例只发 `thread/resume`，不再新建线程 |
| 10 | `test_pinned_digest_rejects_before_launch` | 摘要不匹配在启动前拒绝，无 peer 日志；正确摘要可启动 |
| 11 | `test_owner_binding_tamper_fails_closed` | 状态文件 owner 被篡改时 fail closed |
| 12 | `test_input_and_request_budgets_are_enforced` | 超长输入、非法请求 id、空白输入在任何进程启动前拒绝 |
| 13 | `test_turn_fencing_and_owned_interrupt` | 持锁时 busy；运行中的精确 turn 可被 interrupt，回执为 `interrupted` |
| 14 | `test_config_requires_absolute_pinned_launch` | 非绝对可执行文件、非法摘要、相对 data_dir 拒绝 |
| 15 | `test_codex_disabled_by_default` | API 404，`/config` 无开关 |
| 16 | `test_trial_mode_does_not_expose_codex_router` | trial 下 403 `FEATURE_DISABLED`，host factory 不被调用 |
| 17 | `test_owner_thread_turn_and_replay` | 正常流程、回执、重放、409、422；`/config` 暴露开关 |
| 18 | `test_other_owner_cannot_read_or_use_the_binding` | 非本人 GET/POST 全 404 |
| 19 | `test_busy_conversation_and_active_run_are_refused` | 持锁时 409 `CODEX_BUSY`；归档/活动 run 409 |
| 20 | `test_feature_errors_are_not_leaked` | 未 prepare 的 turn 返回 409，错误正文不含内部路径/二进制名 |

## 5. 总控复验命令

在 data3 建临时目录后执行（本工作树只读约束不影响总控）：

```bash
cd /media/data3/dengkw/projects/kaiwu-codex-host-20261126
mkdir -p /media/data3/dengkw/acceptance/kaiwu-codex-host-20261126/tmp
cd server
# 语法体检（只编译，不执行模块）
../.venv/bin/python -m compileall -q app/codex_appserver.py app/conversation_codex.py
TMPDIR=/media/data3/dengkw/acceptance/kaiwu-codex-host-20261126/tmp \
  ../.venv/bin/python -m pytest -q \
  tests/test_codex_appserver_host.py tests/test_conversation_codex.py
```

预期：`20 passed`，退出码 0。若本机 `.venv` 在别处，按仓库既有约定替换解释器；不要安装新依赖。

独立 synthetic 探针：

```bash
cd /media/data3/dengkw/projects/kaiwu-codex-host-20261126
.venv/bin/python scripts/oss/codex_appserver_host_probe.py \
  --root /media/data3/dengkw/acceptance/kaiwu-codex-host-20261126
```

预期：JSON `"ok": true`、`"passed": 8`，`run_dir` 下保留 synthetic peer 日志（仅合成数据）。

可选地复核既有固定二进制摘要（不执行 app-server、不发模型请求）：

```bash
.venv/bin/python scripts/oss/codex_appserver_host_probe.py \
  --root /media/data3/dengkw/acceptance/kaiwu-codex-host-20261126 \
  --binary /media/data3/dengkw/.private-dev/bin/codex-bin
```

预期：`"binary": {"sha256": "c06a67fd...962d5", "metadata_only": true}`。真实隔离 stdio 元数据仍用既有
`scripts/oss/codex_appserver_stdio_probe.py` 4/4 证据；本切片没有重新运行它。

真实模型 turn 不属于本轮复验；总控必须使用明确授权的模型/内网或隔离 provider 另行验收，不能用本文件的 synthetic 结果替代。

## 6. 剩余限制

- **真实 turn 未验证**：`turn/start` 的 delta、`turn/completed` 和 `thread/resume` 真实字段/时序只依据固定 schema 与 fake peer；真实 provider turn 必须由总控另验。
- **审批只做拒绝**：3 类已知审批请求当前也返回 JSON-RPC error，未实现 schema 中的显式 `decline` 响应映射。安全但可能导致真实需要审批的 turn 直接失败；接入宿主既有审批前不得改为放行。
- **跨进程取消不支持**：活动 turn 只归属当前进程；重启后必须 `reconcile`，不能从另一进程可靠 interrupt。
- **无硬预算/费用账本**：只有字符、时间、通知条数上限；没有 token、费用或 provider 账单核对。
- **无产品消息持久化/UI**：回执保存在宿主私有状态文件，不写入主 `messages`/`runs`，也不生成工作台消息；与现有运行历史的合并需要总控决定。
- **无分布式协调**：仅本机 `flock` + 私有目录，不是多节点租约。
- **二进制来源/许可待核**：openai/codex 上游声明 Apache-2.0；本地 0.165.0 候选的官方发行资产、签名、传递依赖和 NOTICE 尚未核验，不能当作正式供应链认证。
- **摘要只覆盖 command[0]**：固定摘要校验的是可执行文件（解析部署符号链接后）的内容，不覆盖固定参数 `app-server --stdio`。
- **通知字段假设**：`threadId`、`turnId`、`delta` 字段名按当前 schema 解读；缺失身份信息时 fail closed，真实兼容性需总控复核。
- **同步长请求**：`POST /turn` 在 HTTP 请求内等待完成，受 `AGENT_CODEX_APPSERVER_TURN_TIMEOUT_SECONDS` 限制；不是异步作业/队列模型。

## 7. 公共接线冲突点（供总控合并时核对）

| 文件 | 改动 | 冲突风险 |
|---|---|---|
| `server/app/config.py` | 新增 7 个 `codex_appserver_*` 字段和 `__post_init__` 校验 | 与并发修改 Settings 的工程师冲突；新字段插在 Goal 配置后、`harness_max_output_tokens` 前 |
| `server/app/main.py` | `create_app` 增加 `codex_host_factory=None`；非 trial 下按开关挂载 router；`/config` 增加 `codex_appserver` 键 | 与并发修改 router 列表/`/config` 的改动冲突；挂载点位于 Goal execution 之后、workflows 之前 |
| `server/app/db.py` | 未改动 | 无迁移冲突；宿主状态在 `data_dir/codex-appserver/` 私有文件，不新增表 |
| `server/pyproject.toml` | 未改动 | 无新依赖/SBOM 变更 |
| 既有测试 | 未改动 | 仅新增两个测试文件；依赖 `test_api` 的既有夹具约定 |
| API 路径 | 新增 `/api/v1/conversations/{id}/codex` | 未来若产品化 Codex 路由使用同一路径，需要统一契约；当前只读/显式 turn，不写主消息 |
| `app.state.codex_host` | 新增属性 | 仅开关开启时存在；无既有读取方 |

## 8. 结论

本切片交付了默认可关闭、owner 绑定、可测试的 Codex App Server 产品宿主适配代码，覆盖线程准备/恢复、实际消息轮次回执、精确取消、审批拒绝和不确定状态恢复边界。**没有执行测试，没有真实二进制 turn，没有生产接线或上线**；现有 4/4 仅是本地无外网元数据证据，不能当作真实 provider 或产品验收。下一步由总控按第 5 节复验，并另行授权真实模型 turn。
