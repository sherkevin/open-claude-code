# OC-T13 华微试用版最终学校云功能、回归与容量验收

**日期：** 2026-11-13
**服务器：** 学校云
**项目：** open-claude-code（知行工作台）
**执行范围：** 当前工作树上的完整后端回归、前端质量门禁、合成功能烟囱、真实 Chromium 关键路径、默认四执行槽、1/5/10/25 并发矩阵、20 虚拟用户十分钟控制面、30 分钟 soak
**最终结论：** 本轮 T13 执行范围内的功能、回归、浏览器、容量和长稳检查通过，退出码均为 0；但这不是发布声明，不能据此称为“可发布”或“华微可试用版”。第一轮独立审核结论为 **FAIL**：C1/C3 通过，C2/C4 因验收记录和当前候选暂存包未同步而失败。返修 1 已按最终原始 JSON 修正本记录，并建立当前源码绑定的 staging 候选；仍需第二轮独立审核，不能写成 T13 PASS。v4 干净安装和最终冻结发行包、固定真实 Harness 运行时、真实授权模型小流量、客户数据和腾讯云入口仍未完成。

## 1. 工作树与环境

| 项目 | 值 |
|---|---|
| Git HEAD | `38c0a04478da94ae83e507ae4183476995c1fc79` |
| 分支 | `main` |
| 工作树状态 | dirty；65 个 tracked 文件有改动，206 个 untracked 条目 |
| 源码树摘要 | `8134c5891edc34404dec693b03cab06bdaea7ccdd4fe4af17fbced639d8314e2` |
| 摘要范围 | 对 `server/app`、`server/tests`、`client/src`、`scripts` 下全部 tracked 与 untracked 文件排序后逐文件 SHA-256，再对结果做 SHA-256；Git 列出 261 条路径，其中已索引删除的 `server/tests/test_subagents.py` 不存在，实际进入摘要的是 260 个文件 |
| 摘要计数口径 | `source_tree_path_count=261` 是 Git 路径条目数；`source_tree_file_count=260` 是实际存在并参与哈希的文件数。旧 `test_subagents.py` 已由新的 `server/tests/test_subagent.py` 取代/整合，不属于意外删除，也不为迎合计数恢复旧文件 |
| 摘要记录 | `.runtime/evidence/oc_t13_final_source_tree_summary_20261113.txt` |
| Python | `.venv`，Python 3.10.12 |
| 数据库 | 临时合成 SQLite；`PRAGMA synchronous=FULL`，数值 2；WAL |
| 外部资源 | 未使用真实模型、客户数据、客户凭据、客户 MCP、公网端口或腾讯云 |

工作树在本任务开始前已经包含大量在途改动。本轮没有执行 `git reset --hard`、`git checkout --`、`git clean`、`git stash`、commit，也没有清理或回退其他人的改动。没有修改任务板、颜色分组表、共享看板或生产部署。

最终复跑窗口为 2026-11-13 07:00 至 08:40（Asia/Shanghai）。20 虚拟用户十分钟控制面在 06:20 至 06:31 完成。所有临时目录、pytest basetemp、缓存和日志都位于 `/media/data3/dengkw/projects/open-claude-code/.runtime` 下；后端命令统一清除了宿主 `ANTHROPIC_*` 等 provider 环境变量。

## 2. 本轮修改与原因

| 文件 | 修改原因 |
|---|---|
| `server/app/runtime.py` | 修复真实产品缺陷：执行运行在 runtime 已建立但尚未进入 `execute` 时被取消，旧 runtime 会以 `stopped` 状态被下一次运行复用，导致下一条消息错误变成 `interrupted`。现在启动阶段取消会关闭并丢弃该 runtime，下一条运行重新建立 runtime。 |
| `server/tests/test_api.py` | 新增 `test_cancel_during_startup_does_not_poison_the_next_run`，先取消启动中的运行，再提交下一条消息并断言其成功且只执行一次。 |
| `scripts/trial_smoke.py` | 扩展试用烟囱到 21 项；加入 trial 模式反向路由、字段和检索模式检查；为隔离浏览器实例支持显式 origins 和审批时长。 |
| `scripts/concurrency_load.py` | 把部署执行槽改为显式输入 `--max-active-sessions`，默认严格为 4，不再从并发档位推导；聊天、SSE、取消将超额结果单独分类为预期 `EXECUTION_CAPACITY`，并检查 SSE 唯一终态。 |
| `scripts/trial_capacity.py` | 默认配置固定为 `sessions=4`、`bursts=8,25`、SQLite `FULL`，保留精确四槽、取消释放、恢复和干净关停检查。 |
| `scripts/trial_soak.py` | 新增 20 虚拟用户控制面与长稳工具；每个虚拟用户使用独立合成账号；覆盖健康、登录态、会话列表、检索、状态轮询、SSE、提交、取消、`Last-Event-ID` 重放、唯一终态、资源、关停和长稳后复测。 |
| `scripts/trial_browser_acceptance.py` | 新增隔离回环实例上的真实 Chromium 验收编排；只访问本次临时服务和生产构建静态成品。 |
| `scripts/browser_acceptance.mjs` | 新增 Playwright 浏览器动作、截图、控制台/页面/网络错误采集、回车/发送实际到服务端断言、流式 DOM 增长检查、证据卡、图片灯箱、审批和 Excel 下载。 |
| `server/tests/test_concurrency_load.py` | 新增压测工具合同测试，包括分位数、SSE 重放、容量分类、烟囱项数、1/5 矩阵分层和短长稳探针。 |

修改没有放宽 SQLite `FULL`、安全默认拒绝、高风险审批、会话隔离、证据权限或部署槽数。没有修改 `scripts/trial_release_manifest.py` 的可发布标记，没有修改需求状态文档，也没有把任何测试改成 skip/xfail。

## 3. 命令、退出码与原始证据

| 检查 | 命令摘要 | 结果 | 退出码 | 原始日志/报告 |
|---|---|---|---:|---|
| 定向回归 | 根 `.venv` 串行运行 `test_concurrency_load.py` | 8 passed，29.34s | 0 | `.runtime/evidence/oc_t13_final_targeted2_20261113.log/.exit` |
| 功能烟囱 | `scripts/trial_smoke.py --report ... --timeout 90` | 21/21 passed，failed=0，model_calls=0 | 0 | `.runtime/evidence/oc_t13_final_trial_smoke_20261113.json/.log/.exit` |
| 默认四槽容量 | `scripts/trial_capacity.py --json-out ... --timeout 90`，不传 sessions/bursts | accepted=13，rejected=25，unexpected=0，peak=4 | 0 | `.runtime/evidence/oc_t13_final_trial_capacity_20261113.json/.log/.exit` |
| 1/5/10/25 矩阵 | `concurrency_load.py --levels 1,5,10,25 --iterations 2 --scenarios chat,sse,retrieval,attachment,cancel --max-active-sessions 4 --execution-hold-ms 350` | 410 requests，0 errors，0 timeouts，168 个预期容量拒绝 | 0 | `.runtime/evidence/oc_t13_final_concurrency_matrix_20261113.json/.md/.log/.exit` |
| 前端 Vitest | `npm test -- --run` | 22 files / 147 tests passed | 0 | `.runtime/evidence/oc_t13_final_client_test_20261113.log/.exit` |
| 前端 TypeScript | `./node_modules/.bin/tsc --noEmit --pretty false` | 无错误输出 | 0 | `.runtime/evidence/oc_t13_final_client_tsc_20261113.log/.exit` |
| 前端生产构建 | `npm run build` | Vite 6.1.0 构建成功，仅大 chunk 警告 | 0 | `.runtime/evidence/oc_t13_final_client_build_20261113.log/.exit` |
| 完整后端回归 | 根 `.venv`，清空宿主 provider 环境，串行运行全部 `server/tests` | 1524 passed，17 skipped，0 failed，1180.42s | 0 | `.runtime/evidence/oc_t13_final_full_backend2_20261113.log/.exit/.junit.xml` |
| 真实 Chromium | `trial_browser_acceptance.py`，隔离回环端口和生产构建 | trial 11/11、normal 4/4，failed_modes=[]，model_calls=0 | 0 | `.runtime/evidence/oc_t13_final_browser_acceptance_20261113.json/.log/.exit` |
| 20 用户十分钟 | `trial_soak.py --profile control --duration 600 --virtual-users 20 --streams 4 --pace 1.0` | 35841 requests，100%，p95=274.034ms，p99=419.579ms | 0 | `.runtime/evidence/oc_t13_control_20u_600s_20261113.json/.md/.log/.exit` |
| 30 分钟 soak | `trial_soak.py --profile soak --duration 1800 --virtual-users 20 --streams 2 --pace 1.0` | 100766 requests，100%，p95=238.142ms，p99=369.666ms | 0 | `.runtime/evidence/oc_t13_final_soak_1800s_20261113.json/.md/.log/.exit` |

### 3.1 完整后端命令

```bash
cd /media/data3/dengkw/projects/open-claude-code
TMPBASE="$PWD/.runtime/tmp/oc-t13-final-backend2-1794528860"
mkdir -p "$TMPBASE"
env -i HOME="$HOME" PATH="$PATH" \
  TMPDIR="$TMPBASE" PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest \
  -p no:cacheprovider \
  --basetemp="$TMPBASE/pytest" \
  --junitxml="$TMPBASE/junit.xml" \
  -ra server/tests
```

开始时间：2026-11-13 08:14:20；结束时间：2026-11-13 08:34:05。JUnit 记录 1541 个测试用例，其中 17 个 skipped、0 failures、0 errors。日志只有 9 条 warning，主要是 FastAPI `TestClient` 的 Starlette 弃用提示和 JUnit `xunit2` 的 `record_property` 兼容提示，没有产品失败。

## 4. 17 个 skip 的逐项说明

| 数量 | 测试位置 | skip 条件 | 为什么本轮允许 | 后续归属 |
|---:|---|---|---|---|
| 1 | `test_harness_context.py:350` | 未安装显式固定的 Harness runtime | 这是真实上游运行时集成契约，不是本轮注入合成运行时能替代的产品逻辑；当前学校云候选未安装该运行时，不能伪造通过 | v4 固定运行时和 T14 干净安装验收 |
| 3 | `test_harness_context.py:434` | 未安装显式固定的 Harness runtime | 同上，三组恢复/路由/零额外模型调用场景需要真实固定 Harness | v4 和 T14 |
| 1 | `test_harness_memory_runtime.py:270` | 未安装显式固定的 Harness runtime | 真实 Node 记忆审批和恢复需要固定运行时；本轮合成记忆路径由其他非 skip 测试覆盖 | v4 和 T14 |
| 4 | `test_harness_real_mcp.py` | 未安装显式固定的 Harness runtime | 真实 MCP 握手、撤权、超时和审批不能由合成 fixture 冒充；合成 MCP 合同测试已运行 | v4 和客户 MCP 联调 |
| 4 | `test_harness_real_runtime.py` | 未安装显式固定的 Harness runtime | 真实文本/图片载荷、工具审批、取消和压缩需要固定运行时 | v4 和 T14 |
| 4 | `test_hipporag_public_models.py:373` | 根 `.venv` 没有 `torch` | 这是可选模型质量诊断，不属于华微试用启用的 BM25/hybrid 合成协议路径；测试明确 skip 而不是伪造模型质量通过 | 质量评估环境或获授权模型专项 |

合计 13 个固定 Harness runtime skip、4 个 `torch` skip。它们是显式环境合同，不是把失败改成 skip；也没有使用 `xfail` 或隐藏错误。其存在意味着本轮不能宣称“真实 Harness 和真实嵌入模型已验收”，必须由 v4/T14 和后续真实资源专项关闭。

## 5. 功能烟囱与部署级默认拒绝

最终烟囱为 21/21：

- `health_live`、`health_ready`、管理员登录、模型目录；
- 合成知识库、Markdown 上传、BM25 检索；
- 合成助手、附件上传和哈希校验、会话创建；
- 带附件聊天、SSE 首字节和终态、取消并复用会话、用量接口；
- 私有知识库和搜索的成员隔离、退出登录；
- 两个会话之间的消息标记和运行标识不串线；
- trial 部署能力清单、trial 允许的读取与聊天、BM25/hybrid；
- trial 反向边界。

报告记录 `model_calls=0`、`synthetic=true`、SQLite `FULL` 验收模式。反向边界实际拒绝 9 类关闭路由或检索模式变体、3 类关闭消息字段和 1 类关闭会话字段，持久表副作用为 0。也就是说，前端隐藏不是唯一防线，服务端 DeploymentPolicy 会在 API、工具、审批和执行入口拒绝关闭能力。

本烟囱证明的是合成 HTTP/SSE 协议和应用权限逻辑，不是真实模型质量、真实客户 MCP、真实文档解析质量或客户业务正确率。

## 6. 前端测试、类型检查和生产构建

| 检查 | 精确结果 |
|---|---|
| Vitest | 22 test files passed，147 tests passed，0 failed |
| TypeScript | `tsc --noEmit` 退出码 0，无输出 |
| Vite build | `3116 modules transformed`，生产静态成品生成成功 |
| 产物 | `index-DTEQHx1m.js` 620.29 kB（gzip 198.45 kB）；`FlowGramWorkflowCanvas-BpG203z0.js` 782.14 kB（gzip 238.99 kB） |
| 警告 | 两个 chunk 大于 500 kB；这是体积优化项，不阻塞功能门禁 |

浏览器验收直接加载本次 `client/dist` 生产成品，页面实际引用 `/assets/index-DTEQHx1m.js`，因此不是只跑开发服务器。

## 7. 真实 Chromium 关键路径

本轮在随机回环端口启动独立服务，并用 Chromium 访问独立 `vite preview` 实例。没有重启共享服务，没有开放公网端口，没有访问当前共享产品服务。

### 7.1 trial 模式 11/11

| 检查 | 关键实测 |
|---|---|
| 登录和预置助手 | 预置助手卡片 1 个 |
| 预置助手新建会话 | 成功 |
| 输入框回车提交 | 服务端实际收到 run `4e8f58bed5084e4d89f9b6b91772f51f`，状态 succeeded |
| 发送按钮提交 | 服务端实际收到 run `1aca68b251ee4805be67dceabe9273dc` |
| 流式回答 | MutationObserver 记录文本样本 `[15,15,8,8,8,13]`，证明 DOM 增量增长 |
| 取消 | run `ec51eda312a44680bacf2ae340741d9c` 最终为 cancelled |
| 知识证据卡 | 1 张来源卡，片段为合成 `P-101` 维护周期 |
| BM25/hybrid 和文档访问 | 两种模式均有命中，文档访问 HTTP 200 |
| 图片灯箱 | 96x64 合成图片实际打开 |
| 生产静态成品 | 实际加载生产 JS；原始 JSON 实测 trial DCL=158ms、load=159ms |
| 390x844 窄屏提交 | 无横向溢出，run `e8fb8d381d714153a2f1b304429e6cf7` 成功 |

trial 模式没有开放审批、Excel 和上传，因此本轮没有把这些能力写成 trial 已开放。

### 7.2 normal 合成实例 4/4

normal 独立实例只用于验证产品普通模式仍能工作：

- 登录并开始会话；
- 人类审批实际完成，run `1d6d07d00dec4e79883366731ead5663` 成功；
- Excel 产物实际下载，文件名 `browser-acceptance.xlsx`，1658 bytes，保存为 `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-normal/browser-acceptance-downloaded.xlsx`；
- 审批队列在决策后为空。

这些结果证明普通模式没有因 trial 默认拒绝而退化，不证明 trial 已开放审批或 Excel。

### 7.3 浏览器错误判读

两种模式各记录 1 条控制台 401：页面启动时在登录前请求 `/api/v1/auth/me`，这是未登录探测，不是登录失败。原始 JSON 记录的 `net::ERR_ABORTED` 是 trial 3 条、normal 0 条；trial 的 3 条均为 SSE 请求，发生在取消、页面切换或测试清理关闭连接时。`page_errors=[]`、`issues=[]`、`node_exit_code=0`，没有观察到未解释的脚本异常或 5xx。

### 7.4 截图索引

| 模式 | 文件 | 场景 |
|---|---|---|
| trial | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-trial/trial-00-login.png` | 登录页 |
| trial | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-trial/trial-01-workspace-desktop.png` | 桌面工作台和预置助手 |
| trial | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-trial/trial-02-enter-submission.png` | 回车提交后消息和回复 |
| trial | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-trial/trial-03-cancel.png` | 取消后终态 |
| trial | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-trial/trial-04-evidence-card.png` | 知识检索证据卡 |
| trial | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-trial/trial-05-image-lightbox.png` | 图片灯箱 |
| trial | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-trial/trial-06-narrow-chat.png` | 390px 窄屏聊天 |
| normal | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-normal/normal-00-login.png` | normal 登录页 |
| normal | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-normal/normal-01-approval.png` | 人类审批 |
| normal | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-normal/normal-02-excel-preview.png` | Excel 产物预览 |
| normal | `.runtime/evidence/oc_t13_browser_final2_20261113/screenshots-normal/normal-03-approval-queue.png` | 审批队列 |

浏览器脚本自产出的 `browser-trial.json`、`browser-normal.json`、stdout/stderr 和页面诊断日志与截图位于同一目录；stdout/stderr 均为 0 字节。

## 8. 默认四执行槽容量

最终容量探针没有传 `--sessions` 或 `--bursts`，直接验证部署默认值：

| 阶段 | 结果 |
|---|---|
| 8 个同时提交 | accepted=4，rejected=4，unexpected=0，峰值执行=4 |
| 取消并替换 | 取消后 active 3，替换运行被接受，槽位复用成功 |
| 25 个同时提交 | accepted=4，rejected=21，unexpected=0，峰值执行=4 |
| 释放后恢复 | 4 个提交全部成功 |
| 总计 | accepted=13，rejected=25，unexpected=0 |
| SQLite | `synchronous=FULL`，`PRAGMA synchronous=2`，WAL |
| 关停 | `active_drained=true`、`residual_actors=0`、`residual_retiring=0`、`server_task_done=true` |

所有超额运行都以明确 `EXECUTION_CAPACITY` 终态失败，没有 5xx、静默排队或超过四槽。默认槽数没有被提高。

## 9. 1/5/10/25 并发矩阵

矩阵固定显式部署槽 `--max-active-sessions 4`，没有把 `max(levels)+8` 作为配置。服务端、压测客户端与 SQLite 在同一 Python 进程和事件循环中，因此这是单进程回环应用基线，不是生产容量上限。

| 场景 | 并发 | 请求 | 完成 | 预期容量拒绝 | p50 ms | p95 ms | p99 ms | SSE 首字节 p95 ms | 吞吐 req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| chat | 1 | 2 | 2 | 0 | 918.909 | 1011.557 | 1019.792 | - | 1.07 |
| chat | 5 | 10 | 8 | 2 | 2895.635 | 4150.660 | 4188.071 | - | 1.41 |
| chat | 10 | 20 | 8 | 12 | 1815.256 | 3863.655 | 3908.419 | - | 4.04 |
| chat | 25 | 50 | 8 | 42 | 2713.825 | 6099.470 | 6341.513 | - | 6.75 |
| sse | 1 | 2 | 2 | 0 | 618.108 | 660.072 | 663.802 | 141.011 | 1.61 |
| sse | 5 | 10 | 8 | 2 | 1004.178 | 1609.186 | 1614.920 | 756.553 | 3.81 |
| sse | 10 | 20 | 8 | 12 | 1367.553 | 2767.040 | 2781.170 | 1858.665 | 5.33 |
| sse | 25 | 50 | 8 | 42 | 2823.604 | 5786.631 | 6189.592 | 4562.417 | 7.17 |
| retrieval | 1 | 2 | 0 | 0 | 16.845 | 19.387 | 19.613 | - | 56.66 |
| retrieval | 5 | 10 | 0 | 0 | 68.385 | 107.540 | 110.688 | - | 60.45 |
| retrieval | 10 | 20 | 0 | 0 | 111.724 | 214.234 | 230.581 | - | 67.22 |
| retrieval | 25 | 50 | 0 | 0 | 322.577 | 519.901 | 577.390 | - | 61.33 |
| attachment | 1 | 2 | 0 | 0 | 84.809 | 136.053 | 140.608 | - | 11.70 |
| attachment | 5 | 10 | 0 | 0 | 130.496 | 159.079 | 163.352 | - | 35.78 |
| attachment | 10 | 20 | 0 | 0 | 258.209 | 369.877 | 375.779 | - | 34.66 |
| attachment | 25 | 50 | 0 | 0 | 425.303 | 728.072 | 748.554 | - | 47.76 |
| cancel | 1 | 2 | 2 | 0 | 382.733 | 393.016 | 393.930 | - | 1.88 |
| cancel | 5 | 10 | 8 | 2 | 365.707 | 704.360 | 892.373 | - | 3.66 |
| cancel | 10 | 20 | 8 | 12 | 446.892 | 1747.480 | 1825.364 | - | 5.49 |
| cancel | 25 | 50 | 8 | 42 | 1091.561 | 4437.293 | 4572.967 | - | 6.75 |

总计 20 行、410 个请求、410 个预期结果、0 未预期错误、0 超时、0 传输失败、26 次实际取消、168 次预期 `EXECUTION_CAPACITY` 拒绝、78 个完成执行、`model_calls=0`。执行层和控制面/只读层分开计数，没有用总成功率掩盖容量拒绝。

资源峰值来自同进程采样：RSS 峰值 `164,892,672` bytes（约 `157.254` MiB），线程峰值 113，文件描述符峰值 133，活动执行槽峰值 4。SQLite 全程 `sync=2`。

矩阵本身通过：20 行、410 个请求均得到预期分类，0 error、0 timeout、0 transport failure；168 个请求按设计返回 `EXECUTION_CAPACITY`，78 个完成，26 个取消，`model_calls=0`。矩阵没有主动制造 SSE 断线，所以 `sse_reconnects=0` 只表示本次矩阵未触发重连，不能冒充已执行断线重放。断线重放证据来自 600 秒控制面、1800 秒 soak 和 SSE 合同测试；矩阵 JSON 没有单独保存 `duplicate_event_ids`、`cross_run_events` 或 `terminal_count_in_trace`，本记录不为矩阵虚构这些字段。

## 10. 20 虚拟用户十分钟控制面与 SSE

运行参数：20 用户、600 秒、4 条活动 SSE、控制面每秒一次循环、默认四槽、SQLite FULL。

| 指标 | 结果 |
|---|---|
| 总请求 | 35,841 |
| 成功 | 35,841，100% |
| p50 / p95 / p99 | 124.470 / 274.034 / 419.579 ms |
| HTTP 5xx | 0 |
| SQLite busy/locked | 0 |
| 活动执行槽峰值 / 结束 | 4 / 0 |
| 关停残留 actor | 0 |
| RSS 起始 / 峰值 / 结束 | 486,064,128 / 640,466,944 / 640,466,944 bytes |
| 线程起始 / 峰值 / 结束 | 88 / 113 / 112 |
| FD 起始 / 峰值 / 结束 | 31 / 61 / 11 |

4 条 SSE 流分别记录 573、572、572、572 个事件；每条流有 38 次断线重连和 38 次 `Last-Event-ID`，重复事件 0、跨运行事件 0、客户端终态 1、持久 trace 终态 1。控制接口成功率 100%，p95 和 p99 均低于 1 秒和 2 秒门槛。

## 11. 30 分钟 soak

运行参数：20 虚拟用户、1800 秒、2 条持续 SSE、执行层每秒节奏、默认四槽、SQLite FULL、合成模型。

| 指标 | 结果 |
|---|---|
| 1800 秒 soak 总请求 | 100,766 |
| 1800 秒 soak 总成功 | 100,766，100% |
| 1800 秒 soak 总请求 p50 / p95 / p99 | 94.335 / 238.142 / 369.666 ms |
| HTTP 5xx | 0 |
| SQLite busy/locked | 0 |
| 执行层请求 | 4,259，全部为预期成功或明确容量拒绝 |
| 执行层 `EXECUTION_CAPACITY` | 1,054 |
| 实际执行数 / 连接数 / 模型调用 | 3,226 / 1,830 / 0 |
| HTTP 状态 | 总请求 200 共 100,766；执行层另有 200 共 827、202 共 3,432 |
| 活动槽峰值 / 采样结束 / 关停后 | 4 / 1 / 0 |
| RSS 起始 / 峰值 / 结束 | 432,771,072 / 608,305,152 / 607,440,896 bytes |
| 线程起始 / 峰值 / 结束 | 84 / 104 / 103 |
| FD 起始 / 峰值 / 结束 | 28 / 73 / 11 |

两条 SSE 流分别收到 1,706 和 1,705 个事件，各有 87 次断线重连和 87 次 `Last-Event-ID`；重复事件 0、跨运行事件 0、客户端终态 1、持久 trace 终态 1。数据库共有 4,280 runs、39,239 events，非终态运行 0，终态事件异常 0，重复终态 0。

长稳后复测全部成功：健康 200、检索返回 2 条、提交 run `505745b7ee684c8da578cb9d137bd617` 为 succeeded、取消 run `d1be08e3f9d145699f771f770d08926c` 为 cancelled。

外部 `/proc` 采样记录在 `.runtime/evidence/oc_t13_final_soak_1800s_rss_trace_20261113.tsv`。排除前五分钟导入和服务预热后，每五分钟 RSS 增量依次约为 9.18、9.60、9.18、8.91、8.39 MiB，没有加速趋势。线程和 FD 在负载期稳定，结束时 FD 回落到 11，关停后 actor 为 0。

必须保留的资源判读边界：本轮长稳工具与服务、压测客户端和 SQLite 位于同一 Python 进程，且 100,766 个请求的明细记录保存在压测器 `Metrics.records` 中，因此 RSS 包含压测工具自身开销，不能当作纯服务端泄漏结论。独立 100,766 条同结构记录的内存探针约占 37.0 MiB（`.runtime/evidence/oc_t13_metrics_record_memory_probe_20261113.json`）；服务端、客户端缓冲和 Python 分配器保留区也必须计入混合进程。当前证据没有看到资源加速耗尽或功能退化，但“纯服务进程长时间无泄漏”仍需 v4 分进程或独立进程探针验证。

## 12. 失败项、非门禁诊断与残余风险

原始执行门禁没有失败项：所有表中命令退出码为 0，完整后端 0 failed，浏览器 `failed_modes=[]`，容量 `unexpected=0`，矩阵 0 error/0 timeout，两轮长稳 `issues=[]`。交付记录的第一轮独立审核仍为 C2/C4 FAIL；返修 1 已修正矩阵、资源峰值、文字漂移和当前候选绑定，但必须等第二轮独立审核，不能把“原始命令全通过”改写成“T13 PASS”。

另有一次 90 秒 `tracemalloc` 诊断，因低于长稳最短 1800 秒门槛且 tracemalloc 明显放大延迟而返回失败；它只用于内存归因探索，不是产品门禁运行，也没有被当作通过证据。原始诊断在 `.runtime/evidence/oc_t13_soak_tracemalloc_diagnostic_20261113.json/.log`，必须按非门禁实验判读。

仍未覆盖且不能隐藏的边界：

1. 真实授权模型推理质量、真实首字延迟、供应商限流、Token 吞吐和费用未测，所有正式运行 `model_calls=0`。
2. 真实客户数据、客户凭据、客户 MCP、业务写操作、客户角色矩阵和业务正确率未测。
3. 当前并发与长稳是单进程同机回环；多进程、多 worker、反向代理、TLS、跨网和真实生产容量未测。
4. 13 个固定 Harness runtime 测试和 4 个 `torch` 测试仍 skip；真实 Harness 与真实嵌入模型尚未在本候选上验收。
5. 浏览器错误采集中的登录前 401 和受控导航 `ERR_ABORTED` 已解释，但没有真人视觉签收。
6. 纯产品进程内存基线仍缺；RSS 混合了压测器记录和客户端对象。
7. 当前源码绑定的 v4 staging 候选、逐文件 manifest 和审计验收 JSON 已建立；v4 最终冻结、干净目录安装、固定 Node/Harness 运行和安装级回滚验证尚未完成。
8. 最终候选独立审核、真实客户试用批准和腾讯云独立入口尚未完成。

## 13. 发布判断

本轮证据足以确认：当前学校云工作树在合成数据、合成模型、真实 HTTP/SSE 回环、默认四槽、完整后端、前端构建和真实 Chromium 路径上达到了 OC-T13 的执行验收要求。

本轮证据不足以确认：真实业务质量、真实模型线路、客户生产接入、纯服务进程长期内存稳定性、v4 冻结包可安装性和正式发布安全性。

因此，本报告不宣布“可发布”，不修改任务板，也不触发腾讯云发布。下一步应在停止产品代码修改后冻结源码并重建 v4，在干净目录安装固定 Harness/Node 运行时，复跑试用能力清单、DeploymentPolicy、后端 skip 项、分进程容量与长稳，再由全新独立会话审核最终候选。
