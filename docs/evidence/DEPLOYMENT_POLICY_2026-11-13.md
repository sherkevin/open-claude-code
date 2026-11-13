# 受控试用 DeploymentPolicy 实现证据

日期：2026-11-13

环境：学校云开发工作树
`/media/data3/dengkw/projects/open-claude-code`

基线：Git `38c0a04`，工作树包含其他在途任务修改；本任务没有提交、部署、重启共享服务，
没有启动公共监听端口，也没有使用客户数据、凭据或付费模型。

## 第三轮独立审核（当前状态）

2026-11-13，全新独立会话
`ST-01a1d174-02a7-77a2-9cfa-9da79946dfce` 完成第三轮只读审核，结论 **PASS，阻断数 0，
C1-C5 全部通过**。独立探针确认 F4-R2 已关闭：Workspace/SDK 对 13 类非法工具名共 26 次
调用均在读取 `runtime_config`、数据库或调用 `Service.authorize_tool` 之前返回
`403 FEATURE_DISABLED`；合法但配置过期的工具仍返回 `403 CONFIG_CHANGED`。

独立串行重跑指定后端 `270 passed`、前端 `22 files / 147 tests passed`，TypeScript 和
生产构建退出码均为 `0`，独立探针 `failures=0`。完整报告：
`.runtime/evidence/oc_t18_independent_review_r3_20261113.md`。

该 PASS 只覆盖 OC-T18 当前学校云工作树，不代表全量回归、浏览器、容量长稳、真实模型或
客户数据、v4 冻结包或华微可试用。项目已随后启动 OC-T13 最终学校云验收。

## 返修 2（第三轮独立审核前的实现上下文）

第二轮独立只读审核确认 F1、F2、F3、F5 和请求级 C5 通过，仅发现 F4-R2 一个阻断：
配置在运行期间过期后，`WorkspaceRuntime.check_tool` 和继承该入口的 `SDKRuntime`
先读取 `runtime_config` 并比较指纹，导致非法工具名返回 `403 CONFIG_CHANGED`，
而不是先由部署策略返回 `403 FEATURE_DISABLED`。

返修 2 在 `server/app/runtime.py:129` 的 `WorkspaceRuntime.check_tool` 首行调用
共享策略 `self.service.policy.require_tool(tool)`。因此工具名类型、空白名、未知字符串
和试用工具白名单先在纯内存策略层完成校验；非法值在 `runtime_config`、数据库读取、
配置指纹比较、`Service.authorize_tool`、run、event、approval、evidence 和外部调用前
统一拒绝。合法工具名仍继续执行原有配置新鲜度检查，再进入
`Service.authorize_tool` 的会话、用户、助手和工具权限检查。本次没有捕获或改写
`CONFIG_CHANGED`，没有绕过配置检查，也没有放行全部工具或关闭 normal 行为。

新增真实调用路径测试位于
`server/tests/test_deployment_policy.py:1012`，覆盖配置过期时
`WorkspaceRuntime.check_tool` 与 `SDKRuntime.check_tool` 对 `None`、空字符串、
空白字符串、list、dict、整数、布尔、浮点数、普通对象、不可哈希对象和未知字符串均返回
`403 FEATURE_DISABLED`；同时证明拒绝前 `runtime_config` 调用数和数据库读取数均为零，
`Service.authorize_tool` 未执行，数据库表计数不变。配置过期时合法工具名在两个入口仍返回
`403 CONFIG_CHANGED`；配置未过期时原有的合法/非法工具测试继续通过。

验证结果：指定 11 个后端测试文件共 `270 passed`，逐文件退出码均为 `0`；前端完整测试
`22 files / 147 tests passed`，TypeScript 检查和生产构建退出码均为 `0`。独立最小探针
在配置过期后对两个入口的 10 类非法值均得到 `403 FEATURE_DISABLED`，合法工具名得到
`403 CONFIG_CHANGED`，策略拒绝期间配置读取次数为零。精确命令、日志和证据见
`.runtime/evidence/oc_t18_rework2_20261113.md`。

返修 2 阶段当时的状态是“完成但尚未独立审核”；该状态现已被上方第三轮独立审核取代。

## 返修 1（历史，已被返修 2 取代）

独立只读审核曾判定首次候选 FAIL，五项阻断为 F1-F5。返修 1 已针对五项阻断完成最小修改，
并补齐 CORS 预检、前置错误优先级、工具名类型、空检索模式以及前端请求体/路径的反向测试。

当前有效结果：

- F1：`Settings` 和 `DeploymentPolicy` 共用 `valid_deployment_mode()`，显式值不再
  `strip/lower`；`NORMAL`、`TRIAL`、`Trial`、` trial `、空字符串和未知值均启动失败，
  精确 `normal`、`trial` 仍可启动。
- F2：新增最外层 `DeploymentPolicyMiddleware`，关闭路由的 GET/POST CORS 预检在 CORS
  自动应答前返回 `403 FEATURE_DISABLED`；允许路由的合法预检和 normal 预检仍为 200。
- F3：同一外层门禁先于非法 Origin、超限 Content-Length、`BodyLimitMiddleware` 和请求体
  解析执行；允许路由的 Origin、大小、JSON 校验和 CORS 行为保持不变。
- F4：`DeploymentPolicy.require_tool_name()` 集中拒绝 `None`、空字符串、list、dict、
  数字、布尔和其他不可哈希值；Service、WorkspaceRuntime、Harness bridge、Memory 和 MCP
  入口不再泄漏宿主 `TypeError` 或误用 `HARNESS_TOOL_DENIED`。
- F5：`SearchInput.mode` 移除 `min_length=1`，trial 的空模式进入共享策略并返回
  `403 FEATURE_DISABLED`；normal 仍保持 422。
- C5：新增请求级前端测试，捕获 NewSession 和 ChatPage 使用的实际 `fetch` 路径与请求体；
  trial 不发关闭 API 请求，创建/发送 payload 的自动路由字段为 false，normal payload 保持 true。

验证结果：指定后端测试共 `269 passed`，退出码均为 `0`；前端 `22 files / 147 tests passed`，
TypeScript 检查和生产构建退出码均为 `0`。精确命令、日志和逐文件数量见
`.runtime/evidence/oc_t18_rework1_20261113.md`。

边界：返修实现和指定定向回归完成，但尚未独立审核、尚未全量回归、尚未浏览器/容量/稳定性
验收，不可发布。`scripts/trial_release_manifest.py` 的
`runtime_enforcement_verified=false`、`runtime_state="unverified"` 未改动。

下文“实现内容”和旧测试表保留为首次候选的历史记录；其中“9 passed”和“CORS 预检未验收”
等描述已被本次返修 1 取代。

## 结论边界

本任务范围内：

- 源码实现完成。
- 新增 DeploymentPolicy 定向测试通过。
- 相关后端定向回归、前端测试和生产构建通过。
- 尚未独立审核。
- 尚未运行完整后端测试集。
- 尚未完成浏览器验收、冻结包、全量容量/稳定性或发布验收。
- 因此本文件不构成“试用版可发布”或“全量验收通过”的结论。

`scripts/trial_release_manifest.py` 未改动：
`runtime_enforcement_verified=false`、`runtime_state="unverified"` 保持不变。实现和定向
测试通过不等于独立审核，也不等于发行清单已通过。

## 实现内容

### 1. 单一配置与只读策略

- 新增 `AGENT_DEPLOYMENT_MODE`，默认 `normal`，只接受 `normal`、`trial`。
  空值、未知值和错误类型启动失败。
- 新增 `AGENT_TRIAL_CAPABILITIES`。未设置时使用已审查默认集合；显式选择只接受已知
  能力名，不支持通配符、前缀或隐式开启。未知能力启动失败。
- `server/app/deployment_policy.py` 集中定义模式、能力、精确路由模板、工具白名单和
  检索模式。策略对象只读。
- `Settings.deployment_policy()` 和 `/api/v1/config` 使用同一策略源。

默认试用能力：

```text
agent.knowledge_tools
assistants.read
auth
config
conversations.read
conversations.write
health
knowledge.read
knowledge.search
messages.read
messages.send
models.read
runs.cancel
runs.read
sources.read
```

### 2. HTTP 路由精确门禁

- FastAPI 全局依赖按匹配后的 `route.path` 模板与 HTTP 方法精确判断。
- 试用模式关闭 `/docs`、`/redoc`、`/openapi.json`。
- 试用模式关闭 Starlette 自动尾斜杠重定向，避免 307 进入未审查路径。
- 无匹配路由和 404/405 方法错误统一返回
  `403 FEATURE_DISABLED`，响应不包含请求路径、内部路由表或异常细节。
- 普通模式保留原 404/405、307 和文档行为。
- 关闭路由在鉴权、请求体解析、业务处理、运行创建、审批创建、模型调用和数据库写入前
  拒绝；管理员没有豁免。

完整允许路由表见
`docs/design/TRIAL_CAPABILITY_POLICY.md` 的“精确允许路由”一节，共 32 条。

### 3. 工具注册与执行双层门禁

- `Service.authorize_tool` 第一步调用部署策略，早于 conversation/memory/file/
  Read/Write/Edit/Glob/Grep/knowledge/web/xlsx/image/MCP 特例、审批和外部调用。
- 试用 Agent 工具只允许：
  - `mcp__workspace__knowledge_search`
  - `mcp__workspace__knowledge_read`
- `workspace_tools.build_workspace_tools` 在注册期过滤文件、网页、Excel、生图、
  Memory 和业务工具。
- `harness_tools.HarnessToolBridge` 的定义列表再次过滤 Workspace、业务 MCP 和独立
  Memory 工具；执行入口在参数解析、`tool.started`、审批和外部调用前再次检查策略。
- `mcp_tools.build_mcp_tools` 在试用模式直接返回空注册表；
  `MCPBusinessTool.invoke` 在连接读取和网络调用前再次检查。
- `harness_memory.HarnessMemoryTools`、`harness_bridge.HarnessKnowledgeBridge`
  均有注册/执行检查。
- 旧 SDK 执行器在试用模式设置 `tools=[]`；历史 run 的
  `capability_snapshot`/`skill_snapshots` 不能恢复原生工具、业务 MCP 或 Skills。

### 4. 请求字段门禁

- trial 下以下字段在 provider router、创建运行或写库前返回
  `403 FEATURE_DISABLED`：
  - `ConversationCreate.auto_model_routing=true`
  - `ConversationPatch.auto_model_routing=true`
  - `SendMessage.auto_route=true`
  - 非空 `skill_ids`
  - 非空 `file_ids`
- 路由层和 `Service` 层都执行同一检查；直接调用 Service 也成立。
- 显式基础 `model_id` 和 `reasoning_level` 仍允许，不触发自动路由。

### 5. 检索模式门禁

- trial 只允许 `bm25`、`hybrid`。
- `vector`、`hipporag` 和未知模式返回 `403 FEATURE_DISABLED`。
- 人工搜索、Service 搜索和 Agent `knowledge_search` 共用
  `DeploymentPolicy.require_retrieval_mode`。
- trial 的 `search_for_run` 固定使用 `hybrid`。即使测试中挂载官方 HippoRAG 后端，
  也不会自动切换；normal 行为保持不变。

### 6. `/api/v1/config`

- normal 和 trial 都返回稳定的 `deployment_mode`、排序后的 `capabilities`、
  `allowed_retrieval_modes`。
- trial 不返回 Feishu H5 开关或 app id，不返回 provider 地址、认证环境变量、凭据、
  token 或密码哈希。
- 重复请求输出确定一致。

### 7. 前端一致性

- `client/src/types.ts` 增加能力清单和 `configAllows`。
- `NewSession.tsx` 在 trial 发送 `auto_model_routing:false`，不再硬编码自动路由。
- 实际聊天发送使用 `auto_route:false`；trial 不请求 Skills，不展示附件、Skill、
  Context、自动推荐、产物和 Memory 等关闭能力。
- 导航和页面入口按 `/api/v1/config` 能力清单收敛；normal 模式保留现有功能。

## 测试与结果

所有命令使用仓库 `.venv`，`TMPDIR` 和 pytest `--basetemp` 均位于
`.runtime/tmp`。日志位于 `.runtime/evidence/`。

| 检查 | 命令 | 结果 | 日志 |
|---|---|---|---|
| 新增策略测试 | `pytest server/tests/test_deployment_policy.py -q` | `9 passed`，exit 0 | `oc_t18_new_tests.log` |
| 配置/API/能力回归 | `pytest test_config.py test_api.py test_capabilities_04.py -q` | `57 passed`，exit 0 | `oc_t18_core_regression.log` |
| Harness/SDK/MCP 工具回归 | `pytest test_harness_tool_execution.py test_sdk_adapter.py test_workspace_tools.py test_harness_bridge.py test_harness_mcp_tools.py -q` | `138 passed`，exit 0 | `oc_t18_tool_regression.log` |
| 知识/RAG 回归 | `pytest test_knowledge_evidence.py test_hipporag_retrieval.py -q` | `54 passed`，exit 0 | `oc_t18_knowledge_regression.log` |
| 产物/审批回归 | `pytest test_artifacts.py test_high_risk_approval.py test_model_approval.py -q` | `18 passed`，exit 0 | `oc_t18_approval_regression.log` |
| 事件循环/审批竞态回归 | `pytest test_event_loop_isolation.py test_approval_race.py -q` | `12 passed`，exit 0 | `oc_t18_race_regression.log` |
| 前端测试 | `npm test -- --run` | `21 files / 144 tests passed`，exit 0 | `oc_t18_client_tests.log` |
| 前端生产构建 | `npm run build` | TypeScript 与 Vite 构建成功，exit 0 | `oc_t18_client_build.log` |

后端定向通过合计：`9 + 57 + 138 + 54 + 18 + 12 = 288` 项，没有失败。

新增测试覆盖：

1. normal 模式健康、登录、助手、会话、消息和知识检索仍可用。
2. 未知模式、空模式、错误类型、未知能力启动失败。
3. 32 条允许路由逐条可用，关闭的代表性管理/上传/MCP/Skills/Plugins/Memory/
   Context/Feishu/Benchmark/Workflow/Sub-agent/推荐/补全/生图/审批写入路由在成员和
   管理员下均为 `403 FEATURE_DISABLED`。
4. 关闭请求前后数据库表计数、run、approval、runtime 连接和 fake provider 调用数
   不变。
5. `/docs`、`/redoc`、`/openapi.json` 不可用。
6. 尾斜杠、双斜杠、`%2F`、编码字符、大小写、HEAD/PUT/PATCH/DELETE、伪造后缀和未知
   路径均不能绕过。
7. `authorize_tool` 顶层拒绝早于不存在的 conversation；Workspace/Harness/MCP/SDK
   注册与执行双层过滤；历史快照和伪造工具名不能恢复禁用工具。
8. 高级字段拒绝发生在 fake provider router 调用和写库前，计数为 0；显式模型和推理
   级别可用。
9. 人工/Service/Agent 检索模式白名单一致；挂载 HippoRAG 后端后 trial 仍为 `hybrid`。
10. `/config` 输出稳定，不出现凭据值和内部地址。

后置检查：

- 21 个本任务相关文件的无 NUL 字节和尾随空白扫描：exit 0。
- 全工作树 `git diff --check`：exit 2。原因是当前工作树中其他在途文件已存在
  CRLF/尾随空白告警；本任务没有修改或清理这些无关文件，不能把该结果说成全树通过。

## 未覆盖风险

1. 本任务没有运行完整后端测试集，因此不能证明其他 1600 项级测试全部通过。
2. 本任务没有进行独立代码审核、安全审核或对抗性复核；下一任务仍应独立检查路由
   枚举、工具桥、MCP 和历史快照路径。
3. 本任务没有进行真实浏览器验收、移动端验收、SSE 长连接稳定性、默认四槽容量、
   1/5/10/25 全矩阵、20 用户十分钟控制面或 30 分钟 soak。
4. 本任务没有验证真实授权模型线路、真实客户角色、真实业务 MCP 或供应商账单。
5. 路由白名单依赖 FastAPI 匹配后的模板。以后新增路由不会自动获得试用权限，必须
   显式加入策略和表驱动测试；这是预期的默认拒绝行为，但需要独立审核确认。
6. trial 关闭 404/405 为通用 403 是有意的不可见化处理；普通模式保持原状态码。
   CORS 预检请求仍由现有中间件处理，不执行业务处理，但本任务未做浏览器侧预检验收。
7. 早期 `git diff --check` 全树告警来自其他在途修改和混合行尾；本任务只完成了相关
   文件的字节级空白检查，没有清理无关文件。

返修 1 后，原风险第 6 条中的 CORS 预检缺口已由 F2/F3 修复；其余未覆盖风险边界继续有效，
并以 `.runtime/evidence/oc_t18_rework1_20261113.md` 为最新记录。

## 复现命令

```bash
cd /media/data3/dengkw/projects/open-claude-code
export PYTHONPATH=server
export PYTHONDONTWRITEBYTECODE=1
export TMPDIR=$PWD/.runtime/tmp

.venv/bin/python -m pytest server/tests/test_deployment_policy.py -q \
  --basetemp=$PWD/.runtime/tmp/pytest-oc-t18-new-final

.venv/bin/python -m pytest \
  server/tests/test_config.py \
  server/tests/test_api.py \
  server/tests/test_capabilities_04.py \
  server/tests/test_harness_tool_execution.py \
  server/tests/test_sdk_adapter.py \
  server/tests/test_workspace_tools.py \
  server/tests/test_harness_bridge.py \
  server/tests/test_harness_mcp_tools.py \
  server/tests/test_knowledge_evidence.py \
  server/tests/test_hipporag_retrieval.py \
  server/tests/test_artifacts.py \
  server/tests/test_high_risk_approval.py \
  server/tests/test_model_approval.py \
  server/tests/test_event_loop_isolation.py \
  server/tests/test_approval_race.py -q \
  --basetemp=$PWD/.runtime/tmp/pytest-oc-t18-combined

cd client
TMPDIR=$PWD/../.runtime/tmp npm test -- --run
TMPDIR=$PWD/../.runtime/tmp npm run build
```
