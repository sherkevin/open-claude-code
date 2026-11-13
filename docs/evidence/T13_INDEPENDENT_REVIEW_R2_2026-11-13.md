# OC-T13 第二轮独立审核报告

**总判定：PASS（仅限 OC-T13 的 C1-C4 执行证据与候选包审核；不代表产品可发布，也不代表客户验收）**

## 1. 审核身份与边界

| 项目 | 记录 |
|---|---|
| 审核角色 | OC-T13 第二轮独立审核人 |
| 服务器 | 学校云 |
| 审核终端 | `term_fa4c205b-fe98-4386-b890-2807c3ed3e8a` |
| Task ID | `task_5bd7be8da81c` |
| Dispatch ID | `ctx_307d68252766` |
| 审核窗口 | 2026-11-13 09:28 至审核完成（Asia/Shanghai） |
| Git HEAD | `38c0a04478da94ae83e507ae4183476995c1fc79` |
| 源码摘要 | `8134c5891edc34404dec693b03cab06bdaea7ccdd4fe4af17fbced639d8314e2` |
| 临时证据目录 | `.runtime/review-t13-r2-20261113T092845-9wPeqA/` |

本次是独立只读审核，没有复用执行者 `ST-01a1d186-a675-7c31-ac2e-16a962c08459`、第一轮审核人
`ST-01a1d25e-a66a-7621-b9ab-b49c265f08f0` 或返修人
`ST-01a1d277-98d5-7383-b2ce-1c4730c15739` 的进程。除写入本报告和审核临时目录外，没有修改产品源码、
测试、配置、证据原文件、项目看板、共享服务或部署状态；没有 commit、push、重启共享服务、访问腾讯云、
真实客户数据、客户凭据或未授权付费模型。

## 2. 源码完整性

独立执行：

```bash
paths=$(git ls-files -co --exclude-standard -- server/app server/tests client/src scripts | sort -u)
exists=$(printf '%s\n' "$paths" | while IFS= read -r f; do test -f "$f" && printf '%s\n' "$f"; done)
printf '%s\n' "$exists" | while IFS= read -r f; do sha256sum "$f"; done | sha256sum
```

实测：

- Git 路径条目 `261` 条，实际存在并参与摘要的文件 `260` 个；
- 缺失路径只有 `server/tests/test_subagents.py`，它是索引中的删除项；
- `server/tests/test_subagent.py` 存在，并保留了旧文件的 8 个测试函数名称，同时增加了工作区、内存作用域和路径逃逸断言；
- 260 个文件重算摘要为
  `8134c5891edc34404dec693b03cab06bdaea7ccdd4fe4af17fbced639d8314e2`，与
  `.runtime/evidence/oc_t13_final_source_tree_summary_20261113.txt:11` 一致；
- 摘要记录中的 9 个关键文件 SHA-256 全部与当前文件一致，相关记录见
  `.runtime/evidence/oc_t13_final_source_tree_summary_20261113.txt:13-21`。

因此，返修没有造成 `server/app`、`server/tests`、`client/src` 或 `scripts` 产品源码漂移。
最终报告 `docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md:17-18` 和机器报告
`docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.json:25-30` 对 `261` 条路径、
`260` 个实际文件的计数和删除原因说明是正确的。

## 3. C1 功能烟囱

**结论：PASS。**

独立读取原始结果并计数：

- `.runtime/evidence/oc_t13_final_trial_smoke_20261113.json:252-254`：
  `passed=21`、`failed=0`、`total=21`；
- 同文件 `:226` 和 `:231-236`：运行时 `model_calls=0`；
- 21 个检查全部为 `status=passed`，名称覆盖健康、鉴权、模型目录、合成知识库和 BM25、附件哈希、
  会话和 SSE、取消复用、用量、成员隔离、trial 部署策略及反向边界。

浏览器原始 JSON：

- `.runtime/evidence/oc_t13_final_browser_acceptance_20261113.json:286-291`：
  `status=passed`、`modes=2`、`failed_modes=[]`、`model_calls=0`；
- trial 的 11 个检查全部通过，normal 的 4 个检查全部通过；
- trial `ERR_ABORTED` 3 条，normal 0 条；两者都只有登录前 `/api/v1/auth/me` 的预期 401；
  `page_errors=[]`、`issues=[]`、`node_exit_code=0`；
- trial 生产页实测 `dcl=158`、`load=159`，见同文件 `:115-116`。

第一轮审核已独立复跑 C1 并判定 PASS，见
`.runtime/evidence/oc_t13_independent_review_20261113.md:62`。本次复核确认源码摘要没有变化，
返修没有改变该结论。

## 4. C2 并发矩阵、资源与长稳

**结论：PASS。**

### 4.1 20 行表格独立逐单元格复算

我没有采用返修一致性 JSON 的结论，而是独立解析
`.runtime/evidence/oc_t13_final_concurrency_matrix_20261113.json` 的 `matrix` 数组，
按最终报告第 9 节同一格式重建预期值，再与
`docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md:196-215` 的 20 行逐项比较。

- 比较了 200 个显示单元格（20 行 × 10 列，其中包含 20 个场景名称）；
- 其中数值单元格 180 个；
- **差异 0 个**；
- 场景顺序、并发、请求数、完成数、容量拒绝、p50、p95、p99、SSE 首字节 p95 和吞吐全部一致。

返修一致性 JSON 所说的“200 个数据单元格”实际包含每行第一个场景字符串；这不影响
“200 个显示单元格 0 错误”的结论，但报告中应理解为 200 个显示单元格、180 个数值单元格。

### 4.2 汇总和资源

从原始 `matrix` 独立求和：

- 20 行，请求 `410`，`ok` 字段合计 `410`；
- `error_count=0`、`timeouts=0`、`transport_failures=0`；
- `capacity_rejected=168`，且全部容量码为 `EXECUTION_CAPACITY`；
- `completed=78`；
- `cancellations=26`；
- `sse_reconnects=0`、`stale_connection_retries=0`、`unknown_transport_retries=0`；
- 最大 RSS `164,892,672` bytes；
- 最大线程数 `113`；
- 最大文件描述符数 `133`；
- 最大活动执行槽 `4`。

这些数值与最终报告
`docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md:217-219` 一致。

### 4.3 null/0 区分与 SSE 边界

- 原始矩阵有 16 行 `first_byte_ms=null`，这些行在 Markdown 中使用 `-`；
- 4 行 SSE 有非空首字节值，Markdown 使用实际数值；
- retrieval 和 attachment 的 `completed=0` 共 8 行，Markdown 写为 `0`，没有写成 `-`；
- 原始矩阵没有主动制造 SSE 断线，`sse_reconnects=0` 只代表本次矩阵没有重连；
- 最终报告 `docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md:221` 明确说明不能把该 0
  当成已执行断线重放，重放证据必须由 600 秒控制面、1800 秒 soak 和 SSE 合同测试承担；
- 我独立检查矩阵 JSON 字段，确认没有 `duplicate_event_ids`、`cross_run_events`、
  `terminal_count_in_trace`、`nonterminal_runs`、`terminal_event_anomalies`、
  `duplicate_terminal_runs` 或 `sqlite_busy_locked` 字段，报告没有虚构这些字段。

因此，F-N6 和 F-N7 仍被诚实保留为未关闭的非阻断加固项。

### 4.4 600 秒控制面和 1800 秒 soak

独立读取原始 JSON：

| 检查 | 原始结果 |
|---|---|
| 600 秒控制面 | 请求 `35,841`，成功 `35,841`，成功率 `100%`；p50/p95/p99 为 `124.470/274.034/419.579 ms`；`sqlite_busy_locked=0`；数据库 24 runs、2,529 events；非终态、终态异常、重复终态均为空 |
| 1800 秒 soak | 总请求 `100,766`，成功 `100,766`，成功率 `100%`；执行层请求 `4,259`；执行层容量拒绝 `1,054`；总请求 p50/p95/p99 为 `94.335/238.142/369.666 ms`；数据库 4,280 runs、39,239 events；非终态、终态异常、重复终态均为空 |

控制面见 `.runtime/evidence/oc_t13_control_20u_600s_20261113.json:106`、
`:225-233` 和 `:251`；soak 见
`.runtime/evidence/oc_t13_final_soak_1800s_20261113.json:106`、`:198-204`、
`:250` 和 `:276`。最终报告 `docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md:229-240`
和 `:248-262` 对两个运行的口径区分正确，没有把 100,766 写成 600 秒控制面请求数。

两个运行的 SSE streams 还独立支持重连和 `Last-Event-ID` 重放：

- 控制面 4 条流分别为 573、572、572、572 个事件，每条 38 次重连和 38 次
  `Last-Event-ID`，重复事件和跨运行事件均为 0；
- soak 2 条流分别为 1,706、1,705 个事件，每条 87 次重连和 87 次
  `Last-Event-ID`，重复事件和跨运行事件均为 0。

## 5. C3 合成数据与清理

**结论：PASS。**

五份正式压力测试 JSON 的字段和目录状态如下：

| 原始文件 | `synthetic` | `data_dir_retained` | 目录当前存在 |
|---|---:|---:|---:|
| `oc_t13_final_trial_smoke_20261113.json` | true | false | 否 |
| `oc_t13_final_trial_capacity_20261113.json` | true | false | 否 |
| `oc_t13_final_concurrency_matrix_20261113.json` | true | false | 否 |
| `oc_t13_control_20u_600s_20261113.json` | true | false | 否 |
| `oc_t13_final_soak_1800s_20261113.json` | true | false | 否 |

五份 JSON 的 `data_dir` 指向的临时目录均已不存在。对最终 Markdown、最终 JSON、候选验收 JSON
和五份压力测试 JSON 扫描 `password`、`passwd`、`secret`、`api_key`、`authorization`、
`bearer`、`access_token`、`refresh_token`、`sk-` 等敏感模式，未发现命中；没有发现真实客户数据、
密码或令牌。返修没有破坏第一轮 C3 的结论。

## 6. C4 门禁、候选包和回滚

**结论：PASS。**

### 6.1 门禁文字

门禁文档 `docs/design/HUAWI_TRIAL_RELEASE_GATE.md` 已同步以下事实：

- 试用烟囱 `21/21`；
- 完整后端 `1524 passed / 17 skipped / 0 failed`；
- 600 秒控制面 `35,841`；
- 1800 秒 soak 总请求 `100,766`，执行层 `4,259`；
- 第一轮审核结论为 **FAIL**，其中 C1/C3 PASS、C2/C4 failed，返修 1 等待第二轮独立复核；
- 当前状态明确不是 T13 PASS、不是发布通过，`release_ready=false`、`publishable=false`；
- 13 个固定 Harness runtime skip 和 4 个 `torch` skip 仍作为缺口保留。

相关行：`docs/design/HUAWI_TRIAL_RELEASE_GATE.md:3`、`:32-34`、`:42-46`、
`:54-67` 和 `:71-83`。我没有发现把返修写成 T13 PASS 或可发布的越界声明。

### 6.2 verifier 原始输出

独立执行的命令、原始输出和退出码：

```text
$ python3 scripts/trial_release_manifest.py verify \
    --package .runtime/trial-release-staging/final-20261113-v4-bound \
    --expected-source-tree-sha256 cf67c341094d3d2a45c49424176f77d681c2d48c52c12b9c38797251578792a5
exit_code=0
{
  "acceptance_bound": true,
  "claim_scope": "integrity_verified_staging",
  "expected_source_tree_sha256": "cf67c341094d3d2a45c49424176f77d681c2d48c52c12b9c38797251578792a5",
  "file_count": 336,
  "package": "/media/data3/dengkw/projects/open-claude-code/.runtime/trial-release-staging/final-20261113-v4-bound",
  "publishable": false,
  "reasons": [],
  "release_ready": false,
  "release_stage": "staging",
  "schema": "trial-release-verification/v1",
  "source_tree_sha256": "cf67c341094d3d2a45c49424176f77d681c2d48c52c12b9c38797251578792a5",
  "status": "PASS"
}
```

该输出满足任务要求：退出码 0、`acceptance_bound=true`、`release_ready=false`、
`publishable=false`、`reasons=[]`、336 个包文件。

### 6.3 哈希与字节绑定

独立计算：

| 对象 | SHA-256 |
|---|---|
| 产品源码树（260 个实际文件） | `8134c5891edc34404dec693b03cab06bdaea7ccdd4fe4af17fbced639d8314e2` |
| 最终验收 Markdown | `402622e8857ac3e9773f4734f7255a2fd8c76c8e78227eb6b06df49057dba526` |
| 最终验收 JSON | `b4946db847ae89f1e2aec17778f58029dc63da7801cdd22de5b20a844cd93c4f` |
| 候选包源码树 | `cf67c341094d3d2a45c49424176f77d681c2d48c52c12b9c38797251578792a5` |
| `final-20261113-v4-bound/manifest.json` | `6a7db89ac57ad7de671e799649a12b513680eed8c6da658de1501975d13c9e7a` |
| `docs/evidence/T13_RELEASE_ACCEPTANCE_20261113.json` | `430de69837bd9c9540e31bf2bda4a8a63009a5aff10b5959c3f4db1ffe5b23b0` |

候选包内：

```text
evidence/T13_RELEASE_ACCEPTANCE_20261113.json
```

与仓库中的 `docs/evidence/T13_RELEASE_ACCEPTANCE_20261113.json` 字节完全一致。独立运行
`cmp -s` 的退出码为 0，两个文件的 SHA-256 都是
`430de69837bd9c9540e31bf2bda4a8a63009a5aff10b5959c3f4db1ffe5b23b0`。

候选 `manifest.json` 的 `source_tree_sha256` 和候选验收 JSON 的
`source_tree_sha256` 都是 `cf67c341...`，绑定关系一致；manifest 的
`acceptance.bound` 为 `true`，`release_ready=false`、`publishable=false`，见
`.runtime/trial-release-staging/final-20261113-v4-bound/manifest.json:3`、
`:8` 和 `:2556-2573`。

### 6.4 当前候选不是 v3

- 候选版本为 `0.4.2-rework1`，标签为 `0.4.2-rework1-staging`；
- HEAD 为 `38c0a04478da94ae83e507ae4183476995c1fc79`；
- 候选构建记录见 `.runtime/evidence/oc_t13_rework1_candidate_bound_build_20261113.json`；
- 候选包含当前返修验收 JSON，manifest 的 `staging_notice` 明确 `product_runtime_verified=false`；
- 与当前仓库的包文件比对：330 个同路径文件字节一致；唯一的差异是候选包内的构建时门禁文档，
  外部当前门禁文档在构建后追加了候选锚定；另有 5 个生成或包专属文件不在仓库路径中，这符合构建产物和
  staging 封面设计，不是源码漂移。

### 6.5 回滚和自引用

回滚说明存在：

- 候选没有安装到产品运行目录；
- 没有启动或替换共享服务；
- 没有启用独立产品端口或切换入口；
- 没有迁移数据；
- 失败时停止候选回环进程并删除候选目录，现有学校云服务不受影响。

见 `docs/design/HUAWI_TRIAL_RELEASE_GATE.md:65` 和
`.runtime/trial-release-staging/final-20261113-v4-bound/manifest.json:32`、
`:2477-2480`、`:2570-2573`。

门禁自引用处理是可审计的：包内保留构建时的门禁文档，构建后由包外当前门禁文档记录候选路径、
候选源码树摘要、manifest 摘要和验收绑定；没有回写包内 manifest 来制造固定点。该限制在
`docs/design/HUAWI_TRIAL_RELEASE_GATE.md:67` 和返修报告
`.runtime/evidence/oc_t13_rework1_20261113.md:88` 中都有明确说明。外部锚定与包内 manifest 的
实测摘要一致，因此没有发现伪造闭环。

## 7. 第一轮阻断项和非阻断项

| 项目 | 本轮结论 | 依据 |
|---|---|---|
| F-B1：最终矩阵与资源峰值 | **已关闭** | 20 行 200 个显示单元格独立比较 0 错误；资源峰值为 RSS `164,892,672`、线程 `113`、FD `133`；汇总为 410/0/0/168/78/26 |
| F-B2：门禁和当前候选包 | **已关闭** | 门禁已同步真实计数；当前 v4-bound staging verifier 退出码 0；验收绑定和回滚均存在 |
| F-N1：261 路径 / 260 文件 | **已关闭** | 独立重算 261/260，缺失旧 `test_subagents.py`；新 `test_subagent.py` 保留 8 个测试名称并增加断言 |
| F-N2：烟囱 20/21 文字 | **已关闭** | 原始 smoke 和最终 Markdown/JSON 均为 21/21 |
| F-N3：浏览器错误与 DCL/load | **已关闭** | 原始 JSON 为 trial `ERR_ABORTED=3`、normal `0`，DCL/load 为 `158/159` |
| F-N4：控制面和 soak 口径 | **已关闭** | 600 秒为 `35,841`；1800 秒总请求为 `100,766`、执行层 `4,259` |
| F-N5：会话隔离双向证明 | **未关闭，非阻断** | `scripts/trial_smoke.py:1004-1039` 仍只验证 B 标记不进入 A，未建立 A 标记反向进入 B 的断言 |
| F-N6：矩阵 SSE 重放字段 | **未关闭，非阻断** | 矩阵不主动制造断线，`sse_reconnects=0`；重放由控制面、soak 和 SSE 合同测试承担 |
| F-N7：矩阵/容量 SQLite 专属字段 | **未关闭，非阻断** | 矩阵和容量 JSON 仍没有专属 busy/locked 和数据库终态字段；控制面/soak 的对应字段为 0 |

这些非阻断项没有被本轮记录掩盖，也没有被写成已关闭。它们不改变本轮 C1-C4 的通过判定，但不能被
当作后续生产稳定性或完整隔离能力的证明。

### 7.1 其他非阻断观察

- **R2-O1：最终报告的工作树计数是执行时快照。**
  `docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md:15` 记录执行时
  `65 tracked / 206 untracked`；返修新增未跟踪的验收 JSON 后，当前 `git status` 为
  `65 tracked / 207 untracked`（共 272 条），候选 manifest 也记录了同样的 272 条
  （`manifest.json:2859-2860`）。这不是产品源码漂移：260 文件摘要仍是 `8134c589...`，
  候选 manifest 也已记录构建时工作树。建议后续只把该字段标注为“T13 执行时快照”，避免读者误认为
  返修后仍是 206。该观察不构成 C4 阻断。
- **R2-O2：“200 个数据单元格”是显示单元格口径。**
  其中 20 个是场景标签，180 个才是数值单元格；两种口径的独立比较结果都是 0 错误。

## 8. 独立计数总表

| 核对项 | 独立实测 | 结果 |
|---|---:|---|
| 源码 Git 路径 | 261 | 与报告一致 |
| 实际参与摘要文件 | 260 | 与报告一致，缺 `test_subagents.py` |
| 试用烟囱 | 21/21 | 与原始 JSON、Markdown、JSON 一致 |
| 完整后端 JUnit | 1,541 cases；1,524 passed；17 skipped；0 failed；0 errors | 一致 |
| 浏览器 trial | 11/11 | 一致 |
| 浏览器 normal | 4/4 | 一致 |
| trial `ERR_ABORTED` | 3 | 一致 |
| normal `ERR_ABORTED` | 0 | 一致 |
| trial DCL/load | 158 ms / 159 ms | 一致 |
| 600 秒控制面 | 35,841 请求 | 一致 |
| 1800 秒 soak 总请求 | 100,766 | 一致 |
| 1800 秒 soak 执行层 | 4,259 请求 | 一致 |
| 矩阵数值单元格 | 180 | 200 个显示单元格中的 180 个数值单元格均正确 |
| 矩阵资源峰值 | RSS 164,892,672 bytes；线程 113；FD 133 | 与原始 JSON 一致 |

审核方法说明：本轮独立重跑了候选 verifier，独立解析原始矩阵、五份压力 JSON、浏览器 JSON 和
JUnit，并核对文件哈希；没有重新执行 20 分钟完整后端、浏览器、600 秒控制面或 1800 秒 soak。
这样做的依据是任务指定的第一轮 C1 已独立复跑、返修未改产品源码且当前摘要仍完全一致；已有原始运行
证据保持未变。该限制不影响本轮对记录、计数和候选绑定的判定，但仍属于审核范围的残余风险。

## 9. C1-C4 结论

| 验收项 | 结论 | 说明 |
|---|---|---|
| C1 功能烟囱 | **PASS** | 原始 smoke 21/21、`model_calls=0`；浏览器 trial 11/11、normal 4/4；第一轮 C1 复跑成立且源码未漂移 |
| C2 并发矩阵、资源和长稳 | **PASS** | 20 行表格独立逐单元格 0 错误；资源、请求、错误、容量拒绝、完成和取消数均与原始 JSON 一致；空值/零值和 SSE 边界表述正确 |
| C3 合成数据与清理 | **PASS** | 五份正式压力 JSON 均为 `synthetic=true`、`data_dir_retained=false`，数据目录不存在；未发现客户数据、密码或令牌 |
| C4 门禁、候选包和回滚 | **PASS** | 门禁数字和第一轮 FAIL 状态正确；v4-bound candidate verifier 退出码 0；验收 JSON 字节绑定、336 文件、manifest 摘要、回滚和自引用说明均可审计 |

**总判定：PASS。** 没有发现仍会使 C1-C4 不能签署的阻断项。

## 10. 仍需保留的发布边界

本 PASS 只关闭 OC-T13 第二轮独立审核中的 C1-C4 记录与候选完整性检查，不改变以下边界：

1. `release_ready=false`、`publishable=false`、`product_runtime_verified=false` 仍然成立；
   本候选只是完整性可核对的 staging，不是可发布包。
2. 13 个固定 Harness runtime 测试和 4 个 `torch` 测试仍为 skip；真实 Harness 和真实嵌入模型
   尚未在本候选上验收。
3. 正式运行全部 `model_calls=0`；真实授权模型质量、首字延迟、供应商限流、Token 和费用未测。
4. 没有使用真实客户数据、客户凭据或客户 MCP；客户角色、业务写操作和业务正确率未测。
5. 当前并发和长稳是单进程同机回环；多进程、多 worker、反向代理、TLS、跨网和真实生产容量未测；
   RSS 仍包含压测器和同进程 SQLite 开销，不能当作纯产品进程泄漏结论。
6. F-N5、F-N6、F-N7 仍未关闭；它们是非阻断加固项，不应被本轮 PASS 掩盖。
7. 本审核没有安装 v4、没有执行干净目录安装、没有切换入口、没有迁移数据、没有访问腾讯云，也没有
   取得客户验收或用户书面批准。后续仍应按门禁顺序完成 v4 冻结、干净安装、真实资源专项和最终发布授权。

## 11. 最终意见

第一轮的两个阻断项 F-B1 和 F-B2 已在本轮独立复核中关闭。最终报告、机器报告、原始矩阵、
控制面、soak、浏览器、完整后端、合成数据清理和当前 staging 候选之间的关键计数和字节绑定均可复算，
没有发现新的产品执行结论被旧数字或历史 v3 掩盖。因此 C1/C2/C3/C4 均为 **PASS**，
本轮总判定为 **PASS**，但仅限上述审核范围，不构成发布、客户验收或腾讯云上线许可。
