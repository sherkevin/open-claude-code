# 独立审核报告：能力合同首个已提交切片（f3f8807）

- 审核角色：独立 Claude Code 审核（非实现 owner、非看板节点）。
- 真实 Claude Code 会话 ST：`4d879d22-cc6a-4e38-b87b-7abfc94dc2b1`（取自 `CLAUDE_CODE_SESSION_ID`，非 term/dispatch 伪造）。
- 审核结论口径：这是**首个已提交切片的早期审核**，不是 OC-T24/OC-T25 整体验收。本报告**不**宣称节点/产品完成、可发布或已验收。
- 审核时间：2026-11-14（Asia/Shanghai），只读。

## 0. 冻结来源与完整性

| 项 | 值 |
|---|---|
| 审核树 | `/media/data3/dengkw/projects/open-claude-code-capability-contract-review` |
| 工作区状态 | `git status --porcelain` 为空（干净，detached HEAD） |
| HEAD（短） | `f3f8807` |
| HEAD（完整 SHA） | `f3f88077db916791587b369470f10f8c0c466778` |
| 对比基线 | `f4d4842f596afea7af877fd31019bc3f74fac946`（`Merge independent acceptance closeout`） |
| 提交差异 SHA-256（`git diff f4d4842 HEAD`） | `6aa670afaa5f0b2931adf7a68a8b88bec98ccea9cf36875582c0a4805c90eb2c` |

被审文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| server/app/capability_policy.py | `5c6fe27be2e7ae67d110264b74d37df14aad239c260695ef225052b9f52980a0` |
| server/app/capability_execution.py | `b37bea65bf8c23908c4c0fbe18a2e453ad0732059db1b5ce7abd6fdfdef916f9` |
| server/app/capability_usage.py | `e55a9b4df5ea88f7a7418d0c0acb38ea09e8ff7e2376fa52f2d9df48c0e370ee` |
| server/app/capability_receipts.py | `6fa6795ec37d69e75d1ed2aa918ea19f25092b1218749a37f938576f0e4575b5` |
| server/app/capability_credentials.py | `895609f0583c47e6e1277092bd66509ad835999305f098158b97bdd66b7efc46` |
| server/app/capability_settings.py | `a48c4ddead0e86a89b7a46ca86a6b7567b20b9593cc8afa31c4c3fc95ce43d53` |
| server/app/config.py | `61aa66535e712ddfc858840677a562b26d7dc4e956b0f91ca8edd7994bec143c` |
| server/app/db.py | `7fc6e9b5e7d6a8c1cfeceeac90929959b18f7269f4f7961a0531c00681e3bb01` |
| server/app/models.py | `419e9d814f0bad14d03c612e32373b870ee674061dfb4c82c1d30adb3f1a08a0` |
| server/app/service.py | `8cf817d896ebbf4d12e90b60f6d13f0b0664d1b75cf97bce298dbf8177ff3688` |
| server/app/web_tools.py | `ddecf8f71f91ad6faa35f8a1645d2f949d7b80884650378f41d3b2c8812b8bc3` |
| server/tests/test_capability_settings.py | `894626e926d68624b8d24cc9347cdd7a45a2301053c163700601c1c783ea0f3a` |
| server/tests/test_web_tools.py | `f7c08f4d329b61e23f4e45c36228c6368963979a38a490ed48ced8cacfd42272` |

> 注：`capability_usage.py`、`models.py` 与两份既有 in-flight advisory 中记录的字节完全一致（`e55a9b4…`、`419e9d8…`），说明 advisory 审过的正是本切片这两个文件；其余文件已被本切片扩展。

---

## 1. Findings（按严重度排序）

### F1 — 中高危（已复现）：fetch 的域名 allowlist 只约束请求 URL，不约束重定向后的 final URL

**位置**
- `server/app/web_tools.py:460` `read_for_run` → `:489-491` 调用执行器并直接取 `route.value` 的 `final_url`。
- `server/app/capability_execution.py:138-147` `_check_target` 只对 `arguments["url"]` 与 `target_url`（均为**重定向前**原始 URL）做 `domain_allows`。
- `server/app/web_tools.py:222` `_fetch_public` 内部最多跟随 5 次重定向，返回**重定向后**的 `url`；该 `final_url` 回到 `read_for_run` 后**再无任何域名校验**就被当作 evidence 的 `original_url` 登记并返回。

**影响**：当管理员把 fetch 域名策略设为 `allowlist`（如仅 `example.com`）时，一个在 allowlist 内的公开主机只要 302 跳转到任意其他公开主机，其内容仍会被读取、登记为来源并返回给模型。域名 allowlist 的“只返回允许域名内容”语义被绕过。**不是 SSRF**：`public_url`/`PublicNetworkBackend` 仍会拦截私有/元数据地址，所以无法借此访问内网；这是 allowlist 策略绕过。

**复现（外部工件，独立进程、无网络、无真实 provider）**
- 脚本：`/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/contract-review/probe/probe_redirect_domain.py`（SHA-256 `4c30386b1fb286919edce9d5616af83005fba61dd81a875f22f0703c7726d0c0`）
- 日志：`.../contract-review/logs/probe_redirect.log`（SHA-256 `5a46cb6e12ed7287976dd76007bcdeddfa71c895035110c306b12b587fd02157`）
- 关键输出：
  ```
  RESULT_URL= https://evil.example.org/exfiltrated
  EVIDENCE_REGISTERED= 1
  ALLOWLIST= {'mode': 'allowlist', 'hosts': ['example.com']}
  FINAL_HOST= evil.example.org
  ALLOWLIST_BYPASSED= True
  ```
- 复现方法：构造 run，把 fetch 域名设为 `allowlist=["example.com"]`；将 `app.web_tools.fetch_public` 打桩为返回 final_url=`https://evil.example.org/...`（忠实模拟 `_fetch_public` 跟随重定向后的返回值），调用 `WebTools.read_for_run(..., "https://example.com/redirector")`，即成功返回并登记来源，未再校验 final host。

**建议修复**（root 已转原 writer，并扩充分级标准）：修复标准为**逐跳发出前校验**——`_fetch_public`/`fetch_public_headers` 的**每一次重定向 hop 都必须在发送该 hop 请求之前**用同一份有效 domain grant 做 `domain_allows` 校验（并复用 `recheck`），而不是只对最终 `final_url` 事后拒绝；最终登记 evidence 时**再次验证**；`allowed -> denied -> allowed` 的中途链路也**不得发生任何外连**。因此不能只在 `read_for_run` 拿到 `final_url` 后补一次检查（那样非允许主机的请求已经发出）。实现上需由执行器/`ReadContext` 把“当前有效域策略”作为逐跳判定回调传入 `fetch_public`（`RouteResult.value` 目前是 provider 自定义 tuple，需按 capability 明确 final URL 与逐跳校验语义）。现有两份测试模块均未覆盖重定向越界（含 allowed→denied→allowed 中途越界）场景。

> 说明：这是**新功能内的缺陷**，不是旧 search/fetch 行为的回归（旧代码没有域名策略）。

---

### F2 — 中危（条件性部署假设，非当前可利用）：`secure_transport` 只要 `AGENT_TRUST_PROXY_HEADERS=true` 就采信任意对端提供的 `X-Forwarded-Proto`

**位置**：`server/app/capability_credentials.py:37-58`，尤其 `:49-52` 直接读取 `request.headers["x-forwarded-proto"]` 并接受 `https`，**不校验 socket 对端是否来自受信代理**；`:47` 又直接采信 `request.url.scheme`（ASGI 服务器若开启 proxy-headers，该值本身可能已被请求头改写）。

**精确部署假设**：该函数只有在“应用无法被直连、且前置代理会覆盖/剥离客户端自带的 `X-Forwarded-Proto`”时才安全。仅设置布尔开关并不构成对代理解边界的认证。`local_dev` 分支已正确要求“真实 socket peer 与 `url.hostname` 同时为 loopback”，这一支是稳的。

**当前可利用性**：本切片**未接线**任何 credential HTTP 路由（任务描述也确认紫色仍在实现 HTTP credential/diagnostics 路由），`secure_transport` 目前是死代码，因此**不宣称存在当前已部署利用**。但合同第 5 节要求它作为凭据写入的传输证明，接线前必须补“受信代理/直连不可达”的真实边界，否则远程 HTTP 伪造转发头即可通过。

> 补充（root 告知）：紫色后续**未提交**改动已改为“受限真实 peer 中间件”。该改动不在本冻结提交 `f3f8807` 内，故本报告只按冻结字节记录此条件性缺陷；复评时需在包含该中间件的稳定候选上重新确认。

**建议**：接线时依赖中间件已校验的 scheme，并显式限制受信代理网段/直连不可达；不要把裸客户端转发头当作 TLS 证据；生产/公网凭据流程在无法确认传输真实性时 fail-closed。

---

### F3 — 中低（功能性/接线缺口，fail-closed）：`egress=approval` 与人工外发审批路径在本切片内不可达

**事实**
- `capability_receipts.issue_approval_receipt` **无生产调用方**（仅测试调用）；`app/` 内除 `capability_execution.py` 的 claim/verify 外无发放者。
- `server/app/workspace_tools.py:103,107` 调用 `web.search_for_run(...)`/`web.read_for_run(...)` 时**不传 `call_id`**；执行器 `capability_execution.py:67` 的 `new_call_id()` 会生成随机 id。由于回执以 `(run_id, tool_call_id, tool_name, arguments_digest)` 为唯一键，真实审批链（按主机 tool-call id 建回执）无法与该随机 id 对齐。

**影响**：任何把某能力 egress 配成 `approval` 的管理员都会得到 `APPROVAL_REQUIRED`，功能不可用——**方向是 fail-closed（拒绝而非放行）**，不是绕过。这属于橙色 SDK/Harness 审批桥尚未接入的预期未完成项，不是“已完成”。接线时必须在调用栈把真实 host tool-call id 作为 `RunContext.tool_call_id`（或经 nonce 票据映射）传入。

**另**：`capability_policy.authorize_source_access`、`capability_credentials.secure_transport` 均已定义但**未接线**；`capability_diagnostics` 模块与 credential/diagnostics HTTP 路由在本切片缺失（与任务描述的“紫色仍在实现”一致）。

---

### F4 — 低（潜在，当前为死代码）：`release_unsent` 回滚“当天”计数而非预留当日

**位置**：`server/app/capability_usage.py:77-92`。`release_unsent` 用 `day_key()`（当前 UTC 日）去递减 `capability_day_counters`，但预留可能发生在跨 UTC 午夜的前一天，导致回滚错日、计数漂移。

**当前状态**：`execute_for_run` 未调用 `release_unsent`（保留已发送预留是正确取舍），因此这是潜在缺陷而非现网 bug。若将来接“未发送即释放”路径，应按 `capability_attempts.started_at` 派生日键回滚。

---

### F5 — 低（配置语义缺口）：image_search 的 fallback 永远无法通过校验

**位置**：`server/app/capability_settings.py:147-154`。`normalize_route` 对 `{ocr, image_search}` 强制 `max_attempts=1`，随后又要求 `max_attempts >= 1 + len(fallbacks)`；因此 image_search 只要配置了 fallback 就必然抛 `CAPABILITY_ROUTE_INVALID`（且静默压掉管理员配置的 `max_attempts`）。合同第 3 节只对 **OCR** 规定“无重试/无回退”，未对 image_search 作此限制。属文档/实现不一致，未造成安全问题。

---

### 已核对为“正确”的关键性质（非验收声明）

以下为本切片在这些方面的**实际行为**（用源码+外部探针核对，不采信注释/测试自述）：

- **默认拒绝 / 稀疏与快照求交**（复现：`probe_default_deny.py`，SHA-256 `0b326aa7c3be84fdedafc051fd72b670c3863858926d9ad3f0ffec94a9a5dd31`，日志 `logs/probe_default_deny.log`）：
  - 旧助手 `web_search_enabled=True` → search/fetch=True、ocr/image_search=False；
  - 稀疏 v2 `search-only` → 仅 search=True，fetch/ocr/image_search=False；
  - 当前 search-only + 旧快照 enabled → 仍仅 search；
  - 旧当前 enabled + search-only 快照 → 仍仅 search。
  → 既有 advisory 的 P1“缺失能力失败打开”在本切片已被正确修复为失败关闭。
- **当前/快照求交、域求交、egress 取最严、limits 取最小、max_attempts 取最小**：`capability_policy.py:216-271` 逻辑成立。
- **每次真实 attempt 原子计额**：`capability_usage.reserve_attempt` 在 `db.transaction()`（`BEGIN IMMEDIATE` + 进程内 `RLock`）内先查后增，跨线程/跨进程串行；`test_quota_last_unit_has_exactly_one_winner` 证明“最后一单位恰好一个赢家”。
- **精确一次性回执**：`capability_receipts` 以 `(run_id, tool_call_id, tool_name, arguments_digest)` 唯一键绑定，首次 claim 置 `claimed_by_call`，同调用可 verify 不重复消费，异调用/改参/过期/重放均拒绝；不存在“最近 allowed 行”查找。
- **取消/撤销**：`CancelledError` 记 `unknown` 并保留预留；成功后 recheck 失败记 `discarded` 并抛错、不产来源。
- **有界重试/总截止**：`max_attempts≤3`、`deadline=monotonic+timeout`、`asyncio.wait_for(remaining)` 约束单次调用，退避 `min(0.15·2^i,1.0,remaining-0.05)`。
- **凭据 tombstone/轮换/脱敏**：仅为 `SecretStore` 密文、GET 只返 present/revision、delete 写墓碑使 `get` 返回 None 且不复活环境 key、revision 乐观并发、写前重读 enabled-admin。
- **成员上限**：`narrow_assistant_grants` 对 `auto_approved` 仅管理员、domains 求交、egress 取严、limits 取小；运行期再与全局求交，成员无法把 denied 改 allow。
- **旧 web/source/trial 合同**：`Service.authorize_tool` 仍先过部署边界（trial 仅放行 knowledge 工具），四能力统一委托 `CapabilityPolicy`；旧 `search_for_run/read_for_run` 返回字段（provider/requested_provider/fallback/note/citation）不变，新增域过滤为更严格方向，未见既有 search/fetch 行为回归。

---

## 2. 测试（独立执行，非复述实现结果）

环境：`/media/data3/dengkw/projects/open-claude-code/.venv/bin/python`（Python 3.10），工作目录冻结树 `server/`；`env -i` 允许列表仅 PATH/HOME/TMPDIR/XDG_CACHE_HOME/LANG/LC_ALL/PYTHONPATH/PYTHONDONTWRITEBYTECODE/PYTHONHASHSEED/AGENT_PROVIDER_TEST_MODE；`--basetemp` 与 HOME/TMPDIR/cache 全部落在 `.../contract-review/` 下；未安装依赖、未继承任何 provider 凭据/代理变量、未改 conftest 或生产文件。两个模块**分别**执行以保留真实退出码（非管道尾码）。

| 模块 | 命令 | 结果 | 退出码 | 耗时 |
|---|---|---|---|---|
| test_capability_settings.py | `python -m pytest tests/test_capability_settings.py -p no:cacheprovider --basetemp=.../tmp/bt_cap -q` | `43 passed, 1 warning` | 0 | 65.98s |
| test_web_tools.py | `python -m pytest tests/test_web_tools.py -p no:cacheprovider --basetemp=.../tmp/bt_web -q` | `35 passed` | 0 | 8.31s |

- 合计 **78 passed**；与实现报告“78 passed”一致，但这是本次独立执行的实测结果。
- 日志：`.../logs/test_capability_settings.log`（SHA-256 `6560b830468e1bc5581175313714c932420ece4a4fe9bb9f144334778cc7f55c`）、`.../logs/test_web_tools.log`（SHA-256 `9b977f7c126050c2edc2123f20f3b893151410d10c9403d76b72098decce26b8`）。
- 唯一告警为 Starlette `TestClient` 的 httpx 弃用提示，与本切片无关。
- 未运行 1600+ 全量套件（按任务要求，root 集成后自行运行）。

**对抗性复现**（外部工件，见 F1/F5 及默认拒绝验证）：`probe_redirect_domain.py`、`probe_default_deny.py` 及其日志，均在 `.../contract-review/` 内，未污染冻结树。

---

## 3. 明确未完成/未纳入本切片（不作完成声明）

以下属**预期未完成**，本次不要求、也不应被当作已实现：

- 橙色：SDK/Harness 审批上下文与一次性票据桥、OCR/图片适配器、`Sources.tsx` 与来源 UI、`workspace_tools.py` 传真实 `call_id`。
- 紫色：HTTP 凭据/诊断路由、旧 knowledge 外部 OCR 门禁接线（`knowledge.py` 本切片未改，`CapabilitySettings.knowledge_ocr_egress_allowed` 未被调用）、最小设置/助手 UI 接线。
- 证据读取侧 `EvidenceService._authorize`（橙色）仍用旧 `web_search_enabled`，尚未改用 `authorize_source_access`；历史来源的能力/域名一致性会在橙色接线后生效。

---

## 4. 建议（是否可进入后续测试）

**可以作为“首个可提交切片”进入后续联调测试**，但需满足：

1. 先修 F1（fetch 重定向 final URL 的域名校验）——这是已复现的策略绕过，建议在 writer 侧补上并加回归测试（重定向越界）。
2. 接线凭据路由时按 F2 落实“受信代理/直连不可达”的真实边界，不能只靠 `AGENT_TRUST_PROXY_HEADERS` 布尔。
3. 橙色接入审批桥时按 F3 把真实 host tool-call id 打通，否则 `egress=approval` 永远不可用（当前 fail-closed）。
4. F4/F5 作为低优先清理项随实现收口。

**不建议**据此把 OC-T24/OC-T25 或产品宣称为完成、可发布或已验收；本报告仅是首个已提交切片的独立早期审核，完整验收仍需在集成稳定候选上重做，并覆盖橙色/紫色剩余实现。

---

## 5. 最终 git 状态

审核树 `/media/data3/dengkw/projects/open-claude-code-capability-contract-review`：`git status --porcelain` 为空，detached HEAD 仍为 `f3f88077db916791587b369470f10f8c0c466778`。所有对抗性脚本、日志与测试 basetemp 均位于 `/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/contract-review/`，未修改任何产品/测试/配置/看板文件，未触碰其他工程师 worktree。
