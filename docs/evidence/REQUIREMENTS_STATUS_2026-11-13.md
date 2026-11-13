# 需求与验收状态快照（2026-11-13）

**服务器：** 学校云
**项目：** open-claude-code（知行工作台）
**任务性质：** 文档与验收记录专项；只更新需求状态和差距证据，不改产品代码、不启动或重启服务、不调用真实模型、不接触客户数据或腾讯云。
**依据：** 当前工作树、`docs/REQUIREMENTS_ALIGNMENT.md`、`docs/verification/PARTIAL_REQUIREMENTS_GAP_REGISTER_2026-11-06.md`、`docs/design/HUAWI_TRIAL_RELEASE_GATE.md`、`docs/design/TRIAL_CAPABILITY_POLICY.md` 及下列证据文件。
**结论边界：** 本文件是状态校正记录，不是发布声明，也不是客户验收。除 `STB-001` 明确限定于 OC-T13 受控试用范围外，不把任何“部分实现”升级为完整实现。

## 0.1 2026-11-13 TokenHub 单一路由补充

- 当前产品策略已统一为：所有生产模型角色（聊天、Fitness 1007、Tab 1017、知识视觉/实体/关系和未来图片角色）只能经 `https://tokenhub.tencentmaas.com`；不会把厂商直连地址当作兼容线路。
- `server/app/provider_policy.py` 提供不联网的 URL 分类；`Service.validated_provider` 在运行 admission 前拒绝带凭据的非 TokenHub 外部地址，路由预检返回 `non_tokenhub_route`，视觉索引也复用同一门禁。
- 本轮新增 TokenHub 路由专项测试，覆盖 canonical TokenHub、loopback/synthetic 测试桥和真实外部地址拒绝；当前路由与模型专项回归为 `9 passed`（provider policy）及 `95 passed`（model recommendation/catalog/capability/config/visual）。
- 追加的生产模式门禁要求 `AGENT_PROVIDER_TEST_MODE` 默认关闭：无凭据但显式非 TokenHub 的目录项不可用，loopback/synthetic 不能让生产预检变为 `ready`，Fitness/Tab 在创建 HTTP 客户端前即拒绝。
- 这项修补只证明学校云代码路径和安全门禁，不证明 1007/1017、视觉模型或图片模型在 TokenHub 的真实授权、额度、延迟和语义质量；这些仍是外部输入和发布阻塞项。

## 0.2 2026-11-13 当前工作树回归补充

- 在学校云根 `.venv`、数据盘隔离临时目录下完成当前工作树完整服务端回归：`1537 passed, 17 skipped, 1 warning`，未触碰生产或共享服务。
- 路由门禁、模型目录、SDK/Harness 运行时定向回归：`124 passed, 8 skipped, 1 warning`；新增健康检查断言确认 synthetic/loopback 不会报告 `model_configured=true`。
- 前端 `22` 个测试文件、`147` 个测试和生产构建继续通过；构建仅产生现有大 chunk 警告，不影响构建退出码。
- 本轮回归只能把“代码路径未发现绕过”升级为学校云合成证据；真实 TokenHub 1007/1017/视觉授权、腾讯云独立资源和外部端口仍未提供，因此不得把当前结果写成腾讯云可访问地址或客户交付。

## 0.3 2026-11-13 腾讯云入口复核纠正

- 用户此前提供的腾讯云入口仍然存在：本机 `ssh tencent` 可连接 `119.45.112.102`；远端 `18081` 预览和健康检查均正常，公网入口为 `https://119.45.112.102/`，由 Nginx Basic Auth 保护。
- 本轮只读核验了入口、远端健康和静态客户端，没有读取密码、同步代码、修改远端文件、重启服务或切换入口。
- 远端工作树为历史干净提交 `2876c38`，与学校云当前开发工作树不同；该地址可以作为既有预览入口体验，但不能宣称包含本轮最新 TokenHub 门禁和当前所有改动。

## 0.4 2026-11-13 腾讯云最新受控预览部署

- 用户已明确批准把学校云当前代码同步到腾讯云供体验。本轮已完成项目目录同步、项目自有后端/前端服务更新和回滚记录；没有部署生产、没有重启共享 Orca 服务、没有同步数据库/附件/凭据/模型权重，也没有调用真实付费模型。
- 为保持原有 Orca 根入口不变，腾讯云只新增项目隔离路径：`https://119.45.112.102/zhixing/`。浏览器验收已看到“知行工作台”登录页；公网静态入口、子路径接口和资源均返回 `200`，`/zhixing/api/v1/health/ready` 返回 `status=ready`、`tokenhub_route=true`。
- 腾讯云源码关键摘要与学校云一致：`server/app/provider_policy.py` 为 `c39f671f8772444827d36d84c55c9e30aae8efb8243a177cd3094e1387966e4c`，`scripts/smoke_api.py` 为 `6c97eda61407667556455671d3ce47c1f0a0eaa5ac39b5e88a261fa4d25347b2`；子路径构建入口 `client/dist/index.html` 摘要为 `5f8413cba18c0cfd6344f5a464bffcb38ad004ffec3a00f31ce0b7241804f639`，构建资源均包含 `/zhixing/` 前缀。
- 腾讯云合成烟囱结果：本机直连和公网 `/zhixing/` 路径各 `5/5` 通过，覆盖登录、成员与助手范围、MCP 工具发现、会话与附件归属、用量接口；两次清理错误均为 `0`。前端当前回归为 `22` 个测试文件、`149` 项通过，默认构建和 `/zhixing/` 子路径构建均成功。
- 学校云本轮新增压测记录：默认四槽 `1/5/10/25` 矩阵 `205` 请求、`0` 错误、`0` 超时、`84` 个预期容量拒绝、`13` 个取消；容量边界 `4` 槽、`13` 接受、`25` 精确拒绝、峰值同时执行 `4`、SQLite `FULL`、干净关闭，均通过。该压测为合成运行时，不等同真实模型或客户网络容量。
- 状态边界保持：这是“最新代码可体验的受控预览”，不是最终发布或客户生产交付。真实 1007/1017/视觉授权、固定 Harness 与 Node、HippoRAG/图谱质量金标准、客户脱敏样例、OpenClaw 兼容和 v4 干净安装仍未闭合。

## 1. 结论摘要

1. **OC-T13 已通过第二轮独立只读审核。** 审核会话为 `ST-01a1d285-1577-72e0-8fe1-b8e7ce846cb2`，C1/C2/C3/C4 全部 **PASS**，总判定 **PASS**；第一轮 F-B1（矩阵/资源文字不一致）和 F-B2（门禁旧数字与缺少当前源码绑定候选）均已关闭。证据见 [`T13_INDEPENDENT_REVIEW_R2_2026-11-13.md`](T13_INDEPENDENT_REVIEW_R2_2026-11-13.md)（SHA-256 `68550574bcebb96a13c65f3b5dcea3f8d14b359bb1db23513b998c2801c5b9b3`）。
2. **当前源码对象的执行证据已形成。** 产品源码摘要为 `8134c5891edc34404dec693b03cab06bdaea7ccdd4fe4af17fbced639d8314e2`（Git 路径 261、实际文件 260）；后端 `1524 passed / 17 skipped / 0 failed`，前端 147 tests、TypeScript 和生产构建通过；试用烟囱 21/21，浏览器 trial 11/11、normal 4/4，正式运行 `model_calls=0`。
3. **并发和长稳达到 T13 范围门槛。** 干净默认四槽、1/5/10/25 的 20 行矩阵共 410 请求、0 error、0 timeout、168 个预期容量拒绝、78 完成、26 取消；600 秒、20 虚拟用户控制面 35,841 请求，1800 秒 soak 总请求 100,766。矩阵独立复算 200 个显示单元格（180 个数值单元格）差异 0，资源峰值 RSS 164,892,672 bytes、线程 113、FD 133。
4. **T13 的 staging 候选完整性已通过，但不是发布包。** `final-20261113-v4-bound` 为 336 文件，verifier 退出码 0、`acceptance_bound=true`，验收 JSON 与仓库字节一致；仍为 `release_ready=false`、`publishable=false`、`product_runtime_verified=false`。
5. **F-N5/F-N6/F-N7 保持未关闭且不阻断 T13。** 会话隔离只有单向断言，矩阵没有主动制造 SSE 断线，矩阵/容量探针缺少专属 SQLite busy 与终态字段；这些必须继续作为 v4 和生产稳定性加固项，不能用 T13 PASS 掩盖。
6. **产品仍不可发布，也不能宣称客户试用已完成。** 13 个固定 Harness runtime skip 和 4 个 `torch` skip 尚未关闭；v4 冻结包、干净安装、真实 Harness、真实授权模型小流量、客户脱敏样例、客户验收和腾讯云独立入口仍未完成。T13 PASS 只关闭学校云执行证据与 staging 完整性门禁，不是五道最终发布门的整体通过。

## 2. 已核实事实与证据

### 2.1 后端完整回归（2026-11-12 启动，2026-11-13 完成）

- 命令形态：在 `server/` 下使用根 `.venv` 解释器运行完整 `pytest`，`TMPDIR` 指向数据盘 `.runtime/tmp`。
- 结果：`1 failed, 1488 passed, 17 skipped, 1 warning in 1063.21s (0:17:43)`。
- 唯一失败：`tests/test_memory.py::test_memory_user_assistant_isolation_and_scope_guards`；断言 `client.get(.../memory/file?path=<绝对路径>).status_code == 404` 实际得到 `422`。
- 原始日志：`.runtime/evidence/pytest_full_20261112_post_fix.txt`。
- 运行目录：`.runtime/tmp/full-regression-20261113-001144/`。
- 判读：1488 项通过不能写成“全绿”，因为同一轮存在 1 个越权路径失败；17 项 skip 也不能默认视为通过。

### 2.2 memory 越权路径 422 泄露修复

- 问题性质：无权访问的会话在传入绝对路径或超长路径时，FastAPI 查询参数层先做长度校验并返回 `422`，没有走统一的资源隐藏 `404`，因此请求方能区分“会话不存在/无权”与“参数不合法”。
- 修复方向：移除查询参数层的 `max_length=240` 前置校验，让请求先经过会话授权检查；无权会话统一返回 `404`，服务层仍拒绝绝对路径、隐藏路径和 `..`。另新增 300 字符路径用例，确认超长路径不再从参数层泄露 `422`。
- 定向复测：按本轮任务简报为 **10 passed**；运行痕迹目录 `.runtime/tmp/memory-scope-fix-20261113-002953/`。
- **证据缺口：** 这 10 项通过的原始日志尚未归档到 `docs/evidence/`；当前只能引用任务简报结论和临时目录痕迹，不能像其他证据那样独立重放。
- 状态影响：`CTX-002` 的权限隔离证据得到加强，但仍不满足“共享服务采用、真实供应商记忆质量、客户数据验收”，状态保持“本地开发阶段验收通过”。

### 2.3 OC-T14 H1：发行包验证器加固

- 第二轮独立审核发现 v3 staging 包的验证器只检查封面和 `release_ready/publishable`，没有拒绝 manifest 把 `product_runtime_verified`、`product_runtime_installed`、`install_contract.scope`、`publishable_claim` 或 `acceptance_binding` 自声明为成功值。
- 整改后 `server/tests/test_trial_release_manifest.py` 为 **37 passed**；合法 v3 包仍按可信源码树摘要 `871060968c542a0b267c04e29374e7e07343d4eabaec4327dca69a286bc7eb0d` 验证为 `PASS`，同时保持 `release_ready=false`、`publishable=false`。
- 独立复核：PASS；审核人在隔离目录构造 30 个恶意 manifest 样本，30/30 均失败关闭，原因指向被篡改字段。
- 证据：`docs/evidence/TRIAL_PACKAGE_2026-11-12.md` 第 9 节、`.runtime/evidence/oc_t14_h1_independent_review_20261113.md`、`.runtime/evidence/oc_t14_h1_contracts_20261113.txt`、`.runtime/evidence/oc_t14_h1_v3_verify_20261113.json`。
- 边界：v3 是 **历史 staging 快照**；当前工作树相对 v3 已继续漂移，必须等所有并行源码修改停止后重新冻结、重建 v4、重新安装取证并再次独立审核。

### 2.4 OC-T15：HippoRAG 进程适配器路径规范化

- 根因：Python 3.10 的 `Path.absolute()` 不做词法规范化，`server/../.venv/bin/python3` 会保留 `..`；`hipporag_process.py::_absolute()` 的 fail-closed 检查因此拒绝合法解释器路径，导致 46 项进程测试在完整回归中失败。
- 修复：用 `os.path.abspath` 做词法规范化并保留 `realpath` 一致性校验；不跟随符号链接、不放宽越界/NUL/超长/非字符串拒绝；默认关闭、离线、超时/取消和进程回收语义未改。
- 验证：修复后 `test_hipporag_process.py` **58 passed**；相关定向套件合计 **268 passed**（`oc_t15_verifier_20261112.txt`）；新增 3 个路径规范化测试。
- 证据：`.runtime/evidence/OC-T15_HIPPORAG_PROCESS_2026-11-12.md`、`oc_t15_baseline_before_fix.txt`、`oc_t15_new_tests_against_prefix_logic.txt`、`oc_t15_hipporag_process_after_fix.txt`、`oc_t15_verifier_20261112.txt`。
- 独立审核：按任务简报 PASS；**证据缺口：** T15 的独立审核报告未落到 `docs/evidence/`，当前可复核的是 verifier 日志和修复前后对照。
- 状态影响：`HPR-003`/`HPR-007` 的进程恢复与路径边界证据加强；`HPR-005` 的实体/关系/图片定位质量缺口不变，官方检索质量仍未通过。

### 2.5 OC-T16：文件工具与 Sub-agent 测试合同

- 范围：只修复 `test_file_tools.py`、`test_capabilities_04.py`、`test_subagent.py` 的跨模块测试合同；不删除文件工具、不弱化安全断言、不改产品能力。
- 验证：定向 verifier **15 passed**（`oc_t16_verifier_20261112.txt`）。
- 独立审核：按任务简报 PASS；**证据缺口：** T16 的独立审核报告未落到 `docs/evidence/`。
- 状态影响：`HAR-004` 的“文件工具”测试合同缺口和 `SUB-001`/`SUB-002` 的测试导入缺陷得到修复；但真实 SDK 子会话、跨进程审批恢复、产物来源映射和生产负载仍缺，`HAR-004`、`SUB-001`、`SUB-002` 保持“部分实现”。

### 2.6 OC-T17：模型测试环境隔离

- 根因：宿主进程带有 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`，按设计覆盖 provider 设置文件，导致 5 个“未配置 provider”用例失败。
- 修复：新增 `server/tests/conftest.py`，session 级 autouse fixture 仅在测试会话内移除这三个授权变量，结束时恢复；不改 `server/app`，不 skip/xfail，不删除断言。
- 验证：模型测试 **19 passed**；加 `test_config.py` 与 monkeypatch 场景 **62 passed**；编译检查通过。
- 独立审核：PASS，报告 `.runtime/evidence/oc_t17_independent_review_20261113.md`。
- 状态影响：这是测试环境治理，不是产品能力；`MOD-003`、`MOD-004`、`SKL-002`、`PROT-001` 的真实线路、质量、延迟和成本缺口不变。

### 2.7 试用功能烟囱与默认四槽容量

- 合成功能烟囱：16/16 通过（`health_live`、`health_ready`、`login_admin`、`model_catalog`、`create_knowledge_base`、`upload_synthetic_document`、`retrieval_bm25`、`create_assistant`、`upload_attachment`、`create_conversation`、`chat_with_attachment`、`sse_stream`、`cancel_run`、`usage_endpoint`、`member_isolation`、`logout_admin`）。
- 默认四槽容量：`accepted=8, rejected=25, unexpected=0, peak_simultaneous_executions=4, synchronous_pragma=2, shutdown_ok=true`；突发放行/精确超额拒绝/取消释放/恢复均通过。
- 证据：`.runtime/evidence/trial_smoke_full_v2.json`、`.runtime/evidence/trial_capacity_full_v2.json`、`docs/evidence/TRIAL_REMEDIATION_2026-11-12.md` 第 4.3、4.4 节。
- 边界：这是合成资源、注入确定性运行时、零真实模型调用；它证明应用逻辑和槽位边界，不证明真实模型、浏览器或生产容量。

### 2.8 并发矩阵

| 运行 | 结果 | 能否作为交付门槛 |
|---|---|---|
| 2026-11-12 12:33 的 1/5/10/25 矩阵 | `passed`，410 请求，0 错误 | **不能。** 该轮把合成会话上限提高到 33，且服务端、客户端和 SQLite 同进程同事件循环；不是默认四槽配置。 |
| 2026-11-12 22:01 的 1/5/10/25 矩阵 | `failed`，410 请求、408 成功、聊天 25 并发 2 个 `ReadError`；p50 11530.661ms、p95 22536.259ms、max 25768.834ms | **不能。** 失败与 FULL 同步提交阻塞、并行发布安装噪声重叠；原始失败保留。 |
| 2026-11-12 23:32 的干净 1/5 短矩阵 | `passed`，60 请求、0 错误、0 超时、12 次预期取消、18 秒 | 只能作为干净短矩阵证据，不能替代 1/5/10/25 全量。 |
| 2026-11-13 干净默认四槽 1/5/10/25 最终矩阵 | **passed**，20 行、410 请求、0 error、0 timeout、168 个预期容量拒绝、78 完成、26 取消；RSS 164,892,672 bytes、线程 113、FD 133 | **可以。** 第二轮独立审核从原始 JSON 独立复算 200 个显示单元格差异 0，C2 PASS。 |

- 证据：`.runtime/evidence/oc_t13_final_concurrency_matrix_20261113.json`、`docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md` 第 9 节、`docs/evidence/T13_INDEPENDENT_REVIEW_R2_2026-11-13.md` 第 4 节。
- 同一 T13 还完成了 20 虚拟用户 600 秒控制面（35,841 请求）和 1800 秒 soak（总请求 100,766）。F-N6/F-N7 仍未关闭：矩阵没有主动制造 SSE 断线，也没有专属 SQLite busy/数据库终态字段；这些是非阻断加固项。

### 2.9 受控试用默认拒绝能力边界（DeploymentPolicy）

- 当前工作树结论：**已实现并通过第三轮独立只读审核，阻断数 0，C1-C5 全部通过。** 以下能力均有真实应用、服务、Workspace、SDK、Harness 和 MCP 路径的反向测试，不以隐藏前端按钮代替服务端拒绝：
  - 只接受精确 `normal`/`trial` 模式，空值、未知值和大小写/空白变体均失败关闭；
  - 关闭路由按匹配后的精确模板和 HTTP 方法拒绝，包括 CORS 预检和前置错误优先级；
  - `Service.authorize_tool` 与 Workspace/SDK/Harness/MCP 工具入口在运行、审批、数据库写入或外部调用之前拒绝；
  - 自动路由、Skill、附件、记忆、向量、图谱和 HippoRAG 等高级字段及检索模式在试用模式默认拒绝；
  - `/api/v1/config` 返回安全能力清单，统一拒绝码为 `403 FEATURE_DISABLED`，管理员不能绕过。
- F4-R2 独立复核：配置过期时，Workspace/SDK 对 13 类非法工具名共 26 次调用均在读取 `runtime_config`、数据库、配置指纹和 `Service.authorize_tool` 之前返回 `FEATURE_DISABLED`；合法但过期的工具仍返回 `CONFIG_CHANGED`。
- 验证：指定后端 270 项、前端 147 项、TypeScript、生产构建和独立探针全部退出码 0，独立探针 `failures=0`。证据见 `.runtime/evidence/oc_t18_independent_review_r3_20261113.md`。
- 仍需保留的边界：这不是全量回归、浏览器 E2E、容量长稳或冻结包验收；T13 必须在当前候选上复跑，v4 才能把运行时能力写入发行证据。

### 2.10 SQLite FULL 事件循环阻塞

- 单次 `prepare_run` 直接跑在事件循环上：10 次调用 p50=234.72ms、max=310.49ms；同期事件循环延迟 p50=758.99ms、max=761.94ms，采样器只拿到 3 个样本。
- 同样 10 次改为 `asyncio.to_thread`：每次 p50=220.18ms，事件循环延迟 p50=0.22ms、max=0.97ms，采样器拿到 449 个样本。
- 25 路并发真实 HTTP 提交：全部返回 202；提交延迟 p50=8910.30ms、p95=10468.11ms；事件循环延迟 p50=279.88ms、p95=1975.27ms、max=2120.13ms；10.53 秒内只采到 20 个样本。
- 一次性实验（已回滚，不作为交付）：只把 `prepare_run` 放到线程池后，25 个执行器同时启动，但每个事件的同步 FULL `emit` 继续阻塞事件循环，事件循环延迟最高约 20 秒，25 个提交全部 `ReadTimeout`。
- 证据：`docs/evidence/TRIAL_REMEDIATION_2026-11-12.md` 第 6 节、`.runtime/evidence/probe_prepare_run_loop.json`、`.runtime/evidence/probe_message_send_block_full.json`。
- 状态：OC-T19 的限定范围独立审核已通过，四类持久化路径完成异步隔离；全局同步数据库路径仍有残余风险。该缺口是否在试用范围内闭合，必须由 T13 的干净默认四槽、1/5/10/25 和 30 分钟 soak 结果决定，当前不得宣称 `STB-001` 完成。

### 2.11 OC-T13 最终执行与第二轮独立审核

- 执行会话：`ST-01a1d186-a675-7c31-ac2e-16a962c08459`；返修会话：`ST-01a1d277-98d5-7383-b2ce-1c4730c15739`；第二轮独立审核：`ST-01a1d285-1577-72e0-8fe1-b8e7ce846cb2`。
- 第二轮审核任务、dispatch 和 terminal：`task_5bd7be8da81c`、`ctx_307d68252766`、`term_fa4c205b-fe98-4386-b890-2807c3ed3e8a`。审核人是全新只读会话，未复用执行、返修或第一轮审核进程。
- 功能与回归：试用烟囱 21/21；浏览器 trial 11/11、normal 4/4；完整后端 1524 passed / 17 skipped / 0 failed；前端 147 tests、TypeScript 和生产构建通过；正式运行 `model_calls=0`。
- 并发与长稳：默认四槽容量 `accepted=13, rejected=25, unexpected=0, peak=4`；20 行 1/5/10/25 矩阵 410 请求、0 error、0 timeout、168 个预期容量拒绝、78 完成、26 取消；600 秒控制面 35,841 请求；1800 秒 soak 总请求 100,766、执行层 4,259。
- 候选完整性：`final-20261113-v4-bound` 336 个文件；包源码树摘要 `cf67c341094d3d2a45c49424176f77d681c2d48c52c12b9c38797251578792a5`；manifest SHA-256 `6a7db89ac57ad7de671e799649a12b513680eed8c6da658de1501975d13c9e7a`；验收 JSON SHA-256 `430de69837bd9c9540e31bf2bda4a8a63009a5aff10b5959c3f4db1ffe5b23b0`。
- 审核结论：C1/C2/C3/C4 全部 PASS；F-B1/F-B2 关闭；F-N1 至 F-N4 关闭；F-N5 至 F-N7 未关闭但非阻断。
- 发布边界：`release_ready=false`、`publishable=false`、`product_runtime_verified=false`。T13 PASS 不触发腾讯云发布，也不能替代 OC-T14 的 v4 冻结和干净安装。

## 3. 需求状态判定

**总原则：OC-T13 第二轮独立审核通过只把“学校云试用执行证据与 staging 完整性”推进到 T13 范围通过，不把真实模型、真实 Harness、客户业务、完整需求或最终发布整体升级为“已实现”。** 原有限定条件仍然成立。

| 编号 | 既有状态 | T13 新增证据 | 当前判定 | 是否升级 |
|---|---|---|---|---|
| `CTX-002` | 本地开发阶段验收通过 | T13 登录、会话、SSE、取消、撤权、成员隔离和上下文统计烟囱通过 | 仍为“本地开发阶段验收通过”；真实供应商记忆质量与客户数据未验收 | 否 |
| `HAR-004` | 部分实现 | T13 在 Harness 默认路径完成 21 项烟囱和 1524 项全量回归；13 个固定 runtime 用例仍 skip | 仍为“部分实现”；真实 Harness 资源、真实供应商等价和完整发布验收未完成 | 否 |
| `SUB-001` / `SUB-002` | 部分实现（本地合成证据） | T13 烟囱和回归没有回归该切片 | 仍为“部分实现”；真实 SDK 子会话、跨进程恢复、产物来源和生产负载仍缺 | 否 |
| `HPR-003` / `HPR-007` | 部分实现 | T13 验证默认试用路由与回退没有回归既有合同 | 仍为“部分实现”；真实多租户、负载、运营恢复、质量与许可审查未完成 | 否 |
| `MOD-003` / `MOD-004` / `SKL-002` / `PROT-001` | 部分实现 | T13 正式运行 `model_calls=0`；未用真实模型代替烟囱 | 状态不变；真实线路、质量、延迟、成本和客户验收仍缺 | 否 |
| `STB-001` | 部分实现 | 默认四槽、1/5/10/25、20 用户 600 秒、1800 秒 soak、浏览器和完整回归通过 | **T13 受控试用范围通过**；完整生产稳定性、真实 Harness/模型和多进程容量仍为部分实现 | 是（仅 T13 范围） |
| `APR-001` / `APR-002` / `APR-004` | 部分实现 | OC-T18 部署级默认拒绝 PASS；T13 烟囱验证反向边界未回归 | 仍为“部分实现”；模型代审、双人审批真实端点和客户风险矩阵未验收 | 否 |
| `SAFE-001` / `CACHE-001` | 待核验 | T13 合成隔离与清理 PASS；未使用真实敏感数据和真实缓存收益 | 状态不变 | 否 |
| `DOC-002` | 持续维护 | 本文件、发布门禁、需求对齐、差距清单和颜色分组同步 T13 最终审核结果 | 仍为持续维护；v4 和客户阶段必须继续更新 | 否 |

> 说明：`OC-T15`、`OC-T16`、`OC-T17` 是整改任务编号，不是需求编号；它们的 PASS 不能被当作对应需求的完成声明。

## 4. 试用门禁状态（对应 `docs/design/HUAWI_TRIAL_RELEASE_GATE.md`）

| 门禁 | 当前判定 | 已有证据 | 仍缺什么 | 是否阻塞试用发布 |
|---|---|---|---|---|
| 1. 功能烟囱 | **T13 范围通过** | 第二轮独立审核 C1 PASS；烟囱 21/21；浏览器 trial 11/11、normal 4/4；`model_calls=0` | v4 冻结包干净安装后复验同一启用集合 | 否（T13 范围）；v4 待复验 |
| 2. 稳定性 | **T13 范围通过** | 后端 1524 passed / 17 skipped / 0 failed；前端 147 tests、TypeScript、生产构建和 Chromium 通过 | 13 个固定 Harness runtime skip、4 个 `torch` skip；真实 Harness、纯产品进程内存和多进程容量 | 否（T13 范围）；v4 待补齐 |
| 3. 并发压测 | **T13 范围通过** | 默认四槽、20 行 1/5/10/25 矩阵、600 秒控制面、1800 秒 soak 全部通过；C2 独立复算 0 差异 | F-N6/F-N7 的断线重放与专属 SQLite 字段仍是非阻断加固项；真实供应商限流和跨网未测 | 否（T13 范围） |
| 4. 安全与隔离 | **T13 切片通过，最终发布门未通过** | T13 烟囱与浏览器验证登录、成员隔离、撤权、默认拒绝、取消和证据访问；DeploymentPolicy 当前工作树独立审核 PASS | v4 冻结版本尚未重跑能力边界；日志脱敏、客户风险矩阵、真实角色、客户 MCP 和业务写权限未验收 | 是 |
| 5. 交付记录 | **staging 完整性通过；发布包未通过** | 第二轮独立审核 C4 PASS；v4-bound verifier 退出码 0、336 文件、验收绑定和回滚可审计 | 产品运行时未安装；v4 未冻结/干净安装；固定 Harness/Node/依赖、真实模型和最终发布批准未完成 | 是 |

**发布结论：当前不可发布，也不可称为“华微可试用版”。** T13 学校云试用执行与 staging 完整性已通过，但第 4、5 道最终发布门和 v4/真实资源/客户证据仍未关闭，因此腾讯云发布条件仍未触发。

## 5. 未解决的证据缺口

1. **v4 冻结包和干净安装未完成。** 当前只有 T13 staging 候选，`product_runtime_verified=false`；必须在停止产品代码写入后重新冻结，并在干净目录安装。
2. **固定真实 Harness/Node/依赖未验收。** 13 个固定 Harness runtime 用例仍 skip；T13 验证的是现有默认执行路径，不替代产品发行包中的真实运行时握手。
3. **真实授权模型小流量未做。** T13 全部正式运行 `model_calls=0`；真实聊天、Tab、视觉理解、生图、供应商限流、首字延迟和费用对账仍缺。
4. **客户业务验收未做。** 未使用华微真实客户数据、凭据、客户 MCP、真实角色或 OpenClaw 版本级请求；不能用合成 21/21 替代客户正确率和权限验收。
5. **v4 上尚未复跑部署级默认拒绝。** OC-T18 当前工作树 PASS，T13 烟囱也验证了反向边界，但发行 manifest 的 `runtime_enforcement_verified` 必须在 v4 干净安装后重新取证。
6. **F-N5 会话隔离双向证明未关闭。** 当前只有 B 标记不进入 A 的单向断言，不反向证明 A 标记不会进入 B；非阻断，但进入生产前应补齐。
7. **F-N6/F-N7 专属并发字段未关闭。** 1/5/10/25 矩阵未主动制造 SSE 断线，矩阵/容量 JSON 缺少专属 SQLite busy 和数据库终态字段；重放与 0 锁等待由控制面、soak 和合同测试承担。
8. **多进程和真实网络容量未测。** 20 用户控制面和 30 分钟 soak 是单进程同机回环；反向代理、TLS、跨网、多 worker、真实供应商限流未覆盖。
9. **多模态图谱与 HippoRAG 质量未冻结。** `MMG-001`、`MMG-002`、`HPR-005` 的实体、关系、图片定位、融合和独立人工金标准仍缺。
10. **模型线路和 Tab 精确证据仍缺。** 1007 fitness、1017 tab、混元 4、Kimi 3.1 PRO 的真实授权线路与质量未纳入 T13。
11. **腾讯云隔离入口未部署。** 学校云 T13 通过不等于最终发布批准；仍需 v4、真实资源、客户证据和回滚记录。

## 6. 仍需外部输入

- **固定真实 Harness、Node 与依赖资源**：用于 OC-T14 的 v4 干净安装和真实握手，不以当前 13 项 skip 代替。
- **精确 1007 / 1017 授权线路**的目录映射、协议、可用性和配置指纹证据；没有这些证据时 `MOD-004`、`MOD-007`、`SKL-002` 不能宣称已路由或已补全。
- **混元 4、Kimi 3.1 PRO 的 TokenHub 开通状态、额度和上游配置**；没有时只能显示不可用。
- **真实授权模型的小流量许可与预算**：单用户单任务，再最多两个并发；用于真实聊天、Tab 和视觉/生图边界验证。
- **华微客户侧输入**：试用用户/管理员名单和网络范围、OpenClaw 版本与入口类型、脱敏任务与标准答案、客户 MCP/工具白名单/权限边界；这些不阻塞 T13，但阻塞客户业务验收。
- **业务字段分类与保留规则**：`SAFE-001` 的输出脱敏和日志策略需要业务确认。
- **腾讯云发布的最终人工批准**：T13 staging 通过不等于 v4 发布包和客户试用通过；条件批准仍需在 v4、真实资源和回滚记录齐全后确认。

## 7. 当前不可宣称的事项

在证据补齐前，以下说法仍不成立：

- “T13 PASS 等于整个产品可发布或客户可试用”——T13 只覆盖学校云执行证据和 staging 完整性。
- “v4 冻结包已经安装并验证”——当前 `product_runtime_verified=false`，v4 仍未构建和干净安装。
- “真实 Harness、真实模型和真实供应商容量已经通过”——13 个 runtime skip、4 个 `torch` skip，正式运行 `model_calls=0`。
- “未启用能力已在最终发布包中封禁”——当前工作树和 T13 烟囱有反向边界证据，但 v4 尚未重跑。
- “1007 已路由”“Tab 补全质量已验收”“生图可用”“HippoRAG 质量通过”——对应真实资源或质量证据仍缺。
- “客户验收已通过”“OpenClaw 已替换”“腾讯云试用已就绪”——客户样例、真实角色和最终发布动作均未完成。

## 8. 证据索引

| 证据 | 证明什么 | 不能证明什么 |
|---|---|---|
| `docs/evidence/T13_INDEPENDENT_REVIEW_R2_2026-11-13.md` | T13 第二轮独立审核 C1-C4 全部 PASS、F-B1/F-B2 关闭、F-N5..N7 保留；原报告与 `.runtime` 版本 SHA-256 相同 | 不证明 v4、真实 Harness/模型、客户验收或发布 |
| `docs/evidence/TRIAL_FINAL_ACCEPTANCE_2026-11-13.md` / `.json` | 学校云 T13 最终执行证据、21/21 烟囱、1524/17/0、浏览器、矩阵、控制面、soak 和源码计数 | 不代表产品安装、可发布或客户使用 |
| `.runtime/evidence/oc_t13_final_concurrency_matrix_20261113.json` | 20 行 1/5/10/25 原始矩阵；410 请求、0 error、0 timeout、资源峰值 | 不证明断线重放或真实供应商容量 |
| `.runtime/evidence/oc_t13_control_20u_600s_20261113.json`、`oc_t13_final_soak_1800s_20261113.json` | 600 秒控制面 35,841 请求；1800 秒 soak 100,766 请求；SSE 重连和数据库字段 | 不证明多进程、跨网或生产容量 |
| `docs/evidence/T13_RELEASE_ACCEPTANCE_20261113.json` | 当前 staging 候选与执行证据的审计绑定，`acceptance.bound=true` | 不是客户验收，不是 T13 之外的发布许可 |
| `.runtime/trial-release-staging/final-20261113-v4-bound/manifest.json` | 336 文件候选的源码树、验收绑定和回滚边界 | `release_ready=false`、`publishable=false`，不是最终发布包 |
| `.runtime/evidence/OC-T15_HIPPORAG_PROCESS_2026-11-12.md`、`oc_t15_verifier_20261112.txt` | HippoRAG 进程路径规范化修复；268 定向通过 | 不证明 HPR-005 检索/实体/图片质量 |
| `.runtime/evidence/oc_t16_verifier_20261112.txt` | 文件工具/Sub-agent 测试合同 15 passed | 不证明真实子会话、跨进程恢复、生产负载 |
| `.runtime/evidence/OC-T17_MODEL_TEST_ISOLATION_2026-11-12.md`、`oc_t17_independent_review_20261113.md` | provider 测试隔离 19/62 passed；独立审核 PASS | 不证明任何真实模型能力 |
| `docs/evidence/TRIAL_PACKAGE_2026-11-12.md`、`.runtime/evidence/oc_t14_h1_independent_review_20261113.md` | v3 staging 完整性、H1 验证器加固、37 项合同测试、30/30 恶意 manifest 拒绝 | 不证明产品运行时、可发布、客户试用 |
| `.runtime/evidence/trial_smoke_full_v2.json`、`trial_capacity_full_v2.json` | 合成 16/16、默认四槽容量 | 不证明真实模型、浏览器、生产容量 |
| `docs/evidence/CONCURRENCY_LOAD_MATRIX_2026-11-12.md`、`.runtime/evidence/trial_concurrency_full.json`、`trial_concurrency_short_clean.json` | 历史并发矩阵的通过与失败原始记录 | 不能替代 T13 最终默认四槽矩阵 |
| `.runtime/evidence/oc_t18_independent_review_r3_20261113.md` | DeploymentPolicy F1-F5、C5、F4-R2 独立 PASS；指定后端 270、前端 147、构建和探针通过 | 不证明全量回归、浏览器、容量长稳、真实模型或 v4 包 |
| `.runtime/evidence/deployment_policy_audit_20261113.md` | 整改前的 DeploymentPolicy 缺口审计 | 历史缺口记录，不是当前实现状态 |
| `docs/evidence/SQLITE_EVENT_LOOP_ISOLATION_2026-11-13.md` | OC-T19 限定四类持久化路径的整改和独立审核 | 不代表全局同步数据库路径迁移完成，也不替代并发矩阵 |
| `docs/evidence/TRIAL_REMEDIATION_2026-11-12.md` 第 6 节 | SQLite FULL 阻塞诊断与回滚实验 | 不证明修复完成 |
| `docs/evidence/TRIAL_RELEASE_INTERIM_2026-11-12.md` | 阶段性事实快照 | 不是验收通过或发布声明 |

## 9. 与四份需求/门禁文档的对应关系

- `docs/REQUIREMENTS_ALIGNMENT.md`：新增第 17 节“2026-11-13 验收状态校正”，并在第 16 节试用门禁表补充本轮事实；不修改历史实现叙述。
- `docs/verification/PARTIAL_REQUIREMENTS_GAP_REGISTER_2026-11-06.md`：新增“2026-11-13 复核增量”和“当前交付阻断项”两节；不改变原 44 项 21/23 的历史统计。
- `docs/design/HUAWI_TRIAL_RELEASE_GATE.md`：登记 T13 第二轮独立审核 PASS、staging 完整性、v4 待办和五道最终发布门。
- `docs/design/TRIAL_CAPABILITY_POLICY.md`：运行时实现和当前工作树独立审核状态以本文件第 2.9 节和 `.runtime/evidence/oc_t18_independent_review_r3_20261113.md` 为准；冻结包状态仍待 T13/T14 重新取证。

## 10. 本次文档专项的边界

- 未修改产品代码、测试、配置、共享 board、README 或部署状态。
- 未启动或重启服务，未调用真实模型，未读取凭据，未使用真实客户数据，未接触腾讯云。
- 未把任何历史测试数量当作当前候选的通过证据；未把合成证据升级为客户验收；未把设计文档当作已上线能力。
- 所有临时目录、缓存和运行痕迹位于 `/media/data3/dengkw/projects/open-claude-code/.runtime/` 下，`TMPDIR=.runtime/tmp`，`PYTHONDONTWRITEBYTECODE=1`。

## 11. 腾讯云受控预览最新复核

- 公网入口：`https://119.45.112.102/zhixing/`；项目后端、前端和演示 MCP 服务均为项目自有单元并保持运行。
- 固定 Harness/Node 运行环境已在腾讯云候选目录完成校验；真实模型公网烟囪全部通过，随后公网合成烟囪 `5/5` 通过，清理错误 `0`。
- 限流处理已核对：`429` 转为不暴露上游名称的“模型服务请求过于频繁或已达到用量限制，请稍后重试”，不做自动盲重试。应用当前仅有四槽本机执行上限，没有已核验的供应商每分钟请求数/每分钟 Token 配额，故仍不能宣称企业并发已通过。
- 健康接口公共响应不再包含内部路由名称；这项改动已在学校云测试后同步到腾讯云并重启项目自有后端。
- 1007 线路复核保持诚实边界：在隔离备份中登记精确 1007 后，预检虽显示可选，但真实 Harness 业务烟囪首个模型回合失败；配置已回滚，不能把预检通过或目录登记当作 1007 可用证据。当前 1007 仍需用户提供可调用端点/映射后再验收。

## 12. 知识库与多模态样例差异（2026-11-13）

本轮对“腾讯云只剩两个知识库”的反馈完成了数据核查。学校云运行时数据库仍有 5 个主知识库、35 份文档；其中包含图片索引、页面预览、实体关系图谱和结构化工业质量样例。腾讯云受控预览仅有 2 个合成知识库、2 份文档。差异原因是代码同步脚本按边界排除了 `.runtime` 数据库和附件，而不是删除或覆盖学校云知识库。

该差异不改变需求完成度判断：学校云仍是产品数据和验收基准，腾讯云不能作为知识库展示或验收依据。将公开/合成样例导入腾讯云需要单独的用户授权、版本化导出包、权限复核、图片与图谱完整性验证和可回滚记录；Better AI 内部资料、真实客户数据、凭据和模型权重不得随意迁移。

## 13. 用户变更：工作流下线与 Tab 补全修复（2026-11-13）

- 用户明确要求当前产品下线“工作流”功能。执行口径为：客户端不再显示工作流入口，服务端工作流接口 fail-closed 禁用并返回可审计的功能关闭结果；历史工作流数据库表和代码暂不物理删除，以避免旧数据迁移和无关能力回归。Excel、图片生成、知识库、子任务、审批和聊天主链路不随工作流下线而删除。
- 用户同时反馈 Tab 补全按键无反应。修复门槛是：浏览器按 Tab 必须实际调用受授权的 1017 补全线路；成功时补全文本必须保留原输入前缀，失败时必须在界面给出明确的品牌中立提示，禁止写死短语、静默吞错或调用其他未授权模型。
- 本节是当前版本的需求变更记录；旧章节中关于工作流“部分实现”的历史描述保留用于追溯，不得当作当前可交付入口。
