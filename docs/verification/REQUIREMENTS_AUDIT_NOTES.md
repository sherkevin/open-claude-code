# Better AI 工业 Agent 逐项需求审计（第一版）

**日期：** 2026-11-06（Asia/Shanghai）  
**服务器：** 腾讯云  
**项目目录：** `/data/dengkw/workspace/open-claude-code`  
**依据：** `docs/REQUIREMENTS_ALIGNMENT.md` v0.1、`docs/API_CONTRACT.md`、`docs/CONFIGURABLE_WORKSPACE.md`、`README.md`、当前源码和自动化测试。  
**审计范围：** 27 个功能需求 ID；不把设计文档、历史记录或远端口头结果单独当作“已实现”证据。

## 1. 审计计划与证据口径

本轮按以下顺序执行：

1. 读取远程交接、需求对齐、工程记录、接口契约、工作空间说明和 README，建立需求 ID 清单。
2. 对每个 ID 查找服务端路由/领域实现、客户端入口、自动化测试和已记录的远端实测；状态沿用“已实现 / 部分实现 / 设计中 / 待确认 / 未实现”五种定义。
3. 保留产品已经约定的 provider 指纹变化保护：旧会话线路或凭据变化返回 `409 MODEL_SELECTION_REQUIRED`，只修正测试 fixture 使其在明确重新选择后重新固定指纹。
4. 重新执行服务端完整测试与 `compileall`，客户端测试、TypeScript 检查和独立输出目录构建；不重启 systemd，不执行真实模型或客户 MCP 调用，不覆盖在线 `client/dist`。
5. 将实际命令、数量、失败原因和未决证据补入本文件，并同步 `docs/ENGINEER_TODO.md` 与 `TODO.md`；生产部署另列为待批准事项。

### 环境与历史边界

- `docs/HANDOFF_REMOTE_2026-11-06.md` 记录同步后的开发树基线为 server 322 passed、client 53 passed、生产构建成功；本轮将重新执行，不能只引用该记录。
- 2026-11-05 发布树的旧基线曾为 server 316 passed、3 failed；失败集中在 `test_model_catalog.py` 的 provider 指纹 fixture。当前开发树已经加入后续需求文件和代码，不能把两套结果混成一个版本结论。
- 0.4.2 正式归档保持冻结；当前工作树已超出 0.4.2，独立构建输出必须放到 `/home/ubuntu/client-dist-build-<时间戳>` 或其他临时目录。
- 真实客户 MCP 地址、认证资料、业务样本、TokenHub 新模型开通和生产负载尚未提供；涉及这些内容的需求最多标为“部分实现”或“待确认”。

## 2. 逐项审计表

| ID | 状态 | 实现位置 | 可重复验证命令 / 测试 | 当前观测与剩余缺口 |
|---|---|---|---|---|
| ID-001 | 部分实现 | `server/app/config.py`（`AGENT_BRAND_NAME`）、`server/app/identity.py`、`server/app/main.py`、`client/src/App.tsx` | `server/tests/test_identity_classifier.py`、`server/tests/test_identity_metadata.py`；`GET /api/v1/config` | Dano-v2.1 公开身份和可配置品牌已存在；默认品牌仍为“知行工作台”，是否统一改为 Better AI 需产品确认。 |
| ID-002 | 部分实现 | `server/app/identity.py`、`server/app/service.py`、`server/app/runtime.py`、`server/app/evidence.py` | `server/tests/test_identity_classifier.py`、`test_identity_metadata.py`、`test_knowledge_evidence.py` | 纯身份问法走公开元数据且不调用模型，公共轨迹不暴露私有路由；持续诱导、编码转换、混合业务问法仍需持续攻击集，不能宣称对任意模型生成文本绝对保密。 |
| ID-003 | 部分实现 | `server/app/evidence.py`、`server/app/knowledge.py`、`server/app/web_tools.py`、`server/app/runtime.py`、`client/src/Sources.tsx` | `server/tests/test_knowledge_evidence.py`、`test_runtime_sources_04.py`、`test_web_tools.py`；`client/src/workspace.test.tsx` | 数据、知识、网页来源均有登记和脚注；真实客户 MCP、知识库与网页任务及业务正确性尚未验收。 |
| SES-001 | 已实现 | `client/src/AssistantDetailPage.tsx`、`AssistantConfiguration.tsx`；`server/app/main.py`、`service.py` | `client/src/assistantConfiguration.test.tsx`；`server/tests/test_resources_04.py`；保存后刷新接口读回 | 助手详情可配置连接、知识库、Skills、网页搜索、默认模型和推理级别，服务端保存并返回修订版本。 |
| SES-002 | 已实现 | `server/app/runtime.py`、`service.py`、`memory.py`；`client/src/ChatPage.tsx`、`SessionsPage.tsx` | `server/tests/test_session_isolation_042.py`、`test_memory.py`；`server/tests/test_api.py` | 同助手多会话拥有独立工作区、记忆、审批、取消和 SDK 会话；并发/取消场景有自动化覆盖。 |
| SES-003 | 已实现 | `server/app/main.py` 会话路由、`service.py`；`client/src/SessionsPage.tsx`、`App.tsx` | `server/tests/test_api.py`；`client/src/integration.test.ts`、`workspace.test.tsx` | 新建、重命名、分类、标签、搜索、归档和恢复字段及界面已实现；生产浏览器过程证据部分位于缺失私有目录，需独立复测才可复核交互细节。 |
| SES-004 | 已实现 | `server/app/service.py`（活动运行与幂等请求）、`runtime.py`；`client/src/ChatPage.tsx` | `server/tests/test_api.py`、`test_session_isolation_042.py`、`test_model_catalog.py` | 同会话重复提交返回 `409 CONVERSATION_BUSY` 或复用同一请求结果，不会把不同会话误判为同一助手。 |
| MOD-001 | 已实现 | `server/app/model_catalog.py`、`main.py`；`client/src/ModelSelector.tsx`、`NewSession.tsx`、`models.ts` | `server/tests/test_model_catalog.py`、`test_model_recommendation.py`；`client/src/models.test.tsx` | 公开名称、可用状态、不可用原因、切换限制和推理档位由目录/API/UI 一致呈现。 |
| MOD-002 | 已实现 | `server/app/model_catalog.py`、`service.py`、`runtime.py` | `server/tests/test_model_catalog.py::test_private_catalog_defaults_validation_and_effort_mapping`、`test_sdk_receives_selection_and_all_model_aliases`（修正 fixture 后复跑） | Dano `light/high/max` 映射到上游 `low/high/max`；SDK options 需在 provider 环境重新 pin 后验证。 |
| MOD-003 | 部分实现 | `server/app/model_catalog.py`、`server/model-catalog.example.json`、`client/src/ModelSelector.tsx` | `server/tests/test_model_catalog.py`、`test_model_recommendation.py`；`GET /api/v1/models` | `hy4` 与 `kimi-k3.1-pro` 已进入目录/UI，未配置时显示不可用；TokenHub 实际开通、额度和真实任务仍待管理员资料与客户确认。 |
| MOD-004 | 部分实现 | `server/app/model_recommendation.py`、`main.py`、`service.py`；`client/src/NewSession.tsx`、`ChatPage.tsx` | `server/tests/test_model_recommendation.py`；`client/src/integration.test.ts`；远端记录中的 provider router 结果 | 已增加任务提交时的有界本地自动路由、回退、活动运行保护和请求级手动覆盖；成本/延迟实测、完整路由报告和真实模型回归仍未完成。远端曾报告 `router.mode=provider`，本轮不重复真实模型调用。 |
| MOD-005 | 已实现 | `server/app/model_recommendation.py`（摘要、候选白名单、输出/预算边界） | `server/tests/test_model_recommendation.py::test_bounded_recommendation_is_advisory_and_filters_unavailable`、`test_recommendation_requires_short_summary` | 推荐输入只包含有界任务摘要和公开候选，不读取完整历史、附件、MCP 数据或凭据；仍需在回归中保留边界断言。 |
| MOD-006 | 已实现 | `server/app/model_recommendation.py`（provider 失败回退本地评分） | `server/tests/test_model_recommendation.py`（provider 失败、非法结果和不可用候选分支） | 路由超时、错误或非法 JSON 时返回有界本地评分或 `NO_AVAILABLE_MODELS`；不因 provider 失败伪装成已调用模型。 |
| MCP-001 | 已实现 | `server/app/mcp_import.py`、`main.py`、`service.py`；`client/src/ConnectionsPage.tsx`、`McpImportDialog.tsx` | `server/tests/test_api.py`、`test_mcp_import.py`、`test_mcp_transport.py`；客户端集成测试 | HTTP MCP 新增、导入、测试、发现和编辑已实现；凭据只在服务端保存，当前工作树未做真实客户地址验收。 |
| MCP-002 | 已实现 | `server/app/service.py::authorize_tool`、`capabilities.py`、`runtime.py`；`client/src/AssistantConfiguration.tsx` | `server/tests/test_api.py`、`test_capabilities_04.py`、`test_knowledge_evidence.py` | 助手绑定允许工具和自动批准工具，写操作进入审批；每次工具调用重新检查连接、助手、会话和当前权限。 |
| SKL-001 | 已实现 | `server/app/skills.py`、`main.py`、`service.py`；`client/src/SkillsPage.tsx`、`SkillPicker.tsx`、`ChatPage.tsx` | `server/tests/test_resources_04.py`、`test_api.py`；`client/src/integration.test.ts`、`workspace.test.tsx` | Skill 创建、编辑、启停、版本和绑定已实现；消息快照记录 Skill 版本，`@Skill` 通过有界选择器发送。 |
| SKL-002 | 已实现 | `client/src/SkillPicker.tsx`、`ChatPage.tsx`、`integration.ts` | `client/src/workspace.test.tsx`、`integration.test.ts` | Tab 意图补全是本地有界建议，不在输入阶段发请求；当前覆盖常见工业任务动词，不能宣称任意自然语言都能补全。 |
| WEB-001 | 已实现 | `server/app/web_tools.py`、`capabilities.py`、`evidence.py`；`client/src/AssistantConfiguration.tsx`、`Sources.tsx` | `server/tests/test_web_tools.py`、`test_knowledge_evidence.py`；`client/src/workspace.test.tsx` | 助手可启用公开网页搜索，网页摘要和正文分别登记来源并做公共 DNS/重定向/权限检查；网络可用性和客户实际网页任务仍是部署环境边界。 |
| RAG-001 | 已实现 | `server/app/knowledge.py`、`service.py`、`runtime.py` | `server/tests/test_knowledge_evidence.py`、`test_capabilities_04.py`、`test_session_isolation_042.py` | 助手显式绑定知识库，运行快照固定可见文档版本；新版本在下一轮读取，运行中不悄悄换版本。 |
| RAG-002 | 已实现 | `server/app/knowledge.py`（词法、向量、RRF、结果/上下文上限） | `server/tests/test_knowledge_evidence.py`；`docs/KNOWLEDGE_AND_SOURCES.md` | BM25/词法和本地向量混合排序、数量及文本上限已实现；未配置向量模型时明确落到 `local-lexical-v1`，不冒充语义检索。 |
| RAG-003 | 已实现 | `server/app/knowledge.py`、`evidence.py`、`service.py` | `server/tests/test_knowledge_evidence.py`（版本、撤权、删除、跨运行来源）；`test_runtime_sources_04.py` | 来源保存文件、版本、页码/段落、哈希等定位，打开和最终引用再次检查权限；完整客户数据权限矩阵仍待真实资料。 |
| RAG-004 | 已实现 | `server/app/evidence.py`、`runtime.py`；`client/src/Sources.tsx`、`display.ts`、`workspace.test.tsx` | `server/tests/test_knowledge_evidence.py`、`test_runtime_sources_04.py`；`client/src/workspace.test.tsx`、`stream.test.ts` | 仅已登记 source ID 能转成编号脚注和可点击来源，伪造/未登记内部链接保持无效；历史消息不会因撤权被重写。 |
| RAG-005 | 设计中 | `docs/design/RAG_MULTIMODAL_GRAPH.md`（方案）；当前无生产图谱模块 | 设计文档内的接口/阶段验收；当前回归不计为实现测试 | 图片、表格、图形、OCR、视觉索引和多模态图谱尚未上线，必须完成布局解析、版本化图谱、权限和真实样本验收后再改状态。 |
| CTX-001 | 已实现 | `server/app/context.py`、`runtime.py`、`service.py` | `server/tests/test_context.py`、`test_sdk_adapter.py`、`test_api.py` | 上下文统计和 `/compact` 使用原生 SDK，会持久化压缩边界事件；没有第二套应用摘要器。 |
| CTX-002 | 已实现 | `server/app/memory.py`、`service.py`、`runtime.py`；`client/src/MemoryPage.tsx` | `server/tests/test_memory.py`、`test_session_isolation_042.py`、`test_api.py` | 记忆按用户/会话隔离，编辑带 revision，清空不删除聊天历史；跨租户、跨助手真实生产资料仍需客户环境验收。 |
| CTX-003 | 已实现 | `server/app/runtime.py`、`main.py`；`client/src/stream.ts`、`ChatPage.tsx`、`LiveActivity` | `server/tests/test_api.py`、`test_context.py`、`test_session_isolation_042.py`；`client/src/stream.test.ts` | SSE 重连、事件重放、审批、工具调用、压缩、取消和完成状态均有事件与界面；不显示私有思维链。 |
| CTX-004 | 已实现 | `server/app/runtime.py`、`service.py`、`models.py`；`client/src/ChatPage.tsx`（`RunMetrics`）、`runMetrics.test.tsx` | `server/tests/test_sdk_adapter.py`、`test_api.py`、`test_context.py` | 运行保存 Token、耗时、模型和费用可靠性标记；供应商未提供的真实费用保持未知，不能用估算宣传实际账单。 |

## 3. 设计中能力与证据边界

- **多模态知识图谱：设计中。** `docs/design/RAG_MULTIMODAL_GRAPH.md` 只有方案和阶段验收，不代表生产实现。
- **Sub-agent / Multi-agent：部分实现（仅本地合成）。** 早期审计时只有 `docs/design/SUBAGENT_ORCHESTRATION.md` 方案；本轮已增加受保护的有界合成调度、子任务持久化、隔离能力快照、父事件、失败隔离、取消级联和恢复中断测试，并补充高风险子任务继承父级双人审批、无策略默认拒绝和父取消失效测试；仍没有生产调度器、真实 SDK 会话、跨进程审批恢复和负载验收。
- **真实客户验收：待确认。** 尚缺华微世纪 MCP 地址、认证方式、工具清单、业务样本、标准答案和原方案基线，因此不能宣称业务正确率、节省比例、生产容量或 TokenHub 新模型已开通。
- **生产部署：待批准。** 本轮只改源码/文档并做离线或本机回归；systemd 服务、`.runtime/data/`、`.runtime/backups/`、私有配置和在线 `client/dist` 不在审计写入范围。

## 4. 回归执行记录（2026-11-06）

### 服务端

```bash
cd /data/dengkw/workspace/open-claude-code/server
../.venv/bin/python -m pytest -q --basetemp=/tmp/zhixing-server-regression-20261106 -p no:cacheprovider
../.venv/bin/python -m compileall app
```

- `pytest`：**322 passed, 1 warning in 40.16s，退出码 0**。
- 警告来自第三方 `fastapi/testclient.py` 的 `StarletteDeprecationWarning`，不是项目源码警告。
- `compileall app`：**退出码 0**。
- 完整证据：[SERVER_REGRESSION_2026-11-06.md](../evidence/SERVER_REGRESSION_2026-11-06.md)。
- 三个原失败参数已通过测试 fixture 修正：provider 环境替换后显式重新选择同一公共模型以重新固定指纹；产品 `409 MODEL_SELECTION_REQUIRED` 防漂移行为保持不变。

### 客户端

```bash
cd /data/dengkw/workspace/open-claude-code/client
npm ci --registry=https://mirrors.tencentyun.com/npm/ --no-audit --no-fund
npm test
npx tsc -b
npx vite build --outDir /home/ubuntu/client-dist-build-20261106-1348 --emptyOutDir
```

- `npm ci`：**退出码 0**，新增 197 个包；`package-lock.json` SHA-256 未变化。
- `npm test`：**10 个测试文件、53 个测试通过，退出码 0**。
- `npx tsc -b`：**退出码 0**。
- Vite 独立目录构建：**退出码 0**，1,855 个模块转换完成；没有写入在线 `client/dist`。
- 生产健康检查：`{"status":"ready","model_configured":true}`；三个 systemd 服务均为 `active`，本轮没有重启或部署。
- 完整证据：[CLIENT_REGRESSION_2026-11-06.md](../evidence/CLIENT_REGRESSION_2026-11-06.md)。

## 5. 审计结论（已完成本轮回归）

原远端开发树的 27 个编号需求中，**22 项已实现、4 项部分实现、1 项设计中**。远端历史证据是服务端 322 项测试、客户端 53 项测试、TypeScript 检查和独立目录生产构建均已通过；本轮学校云阶段性复核曾记录服务端 326 项、客户端 57 项，优先级 3 收口后的全量服务端为 340 项；继续补齐协议、子任务级联、模型标识边界和审批竞态保护后，最新本机全量服务端为 358 项、客户端为 61 项。部分实现项的缺口集中在默认界面品牌最终决策、持续诱导攻击集、真实客户 MCP/知识库/业务基线、混元 4 与 Kimi 3.1 PRO 的 TokenHub 开通；RAG-005 多模态能力和生产 Sub-agent 仍是设计中。

本轮没有重启 systemd 服务、没有读取或修改生产数据与凭据、没有调用真实客户 MCP 或真实模型；0.4.2 发布归档保持冻结。

---

## 6. v0.2 新增 17 项需求的接手审计（学校云复核）

本节补齐 `docs/REQUIREMENTS_ALIGNMENT.md` v0.2 新增的 17 个编号。状态只依据当前源码、已有测试文件和设计文档；学校云副本缺少 Python/Node 依赖，本轮没有把历史远端回归数字冒充本机重跑结果。对应环境记录见 [REGRESSION_RECHECK_2026-11-06](../evidence/REGRESSION_RECHECK_2026-11-06.md)。

| ID | 状态 | 服务端代码位置 | 客户端入口 | 测试/验证命令 | 当前观测与缺口 |
|---|---|---|---|---|---|
| PROT-001 | 部分实现（本地合成适配器） | `server/app/openai_compat.py`、`server/app/model_catalog.py`、`server/app/runtime.py`、`server/app/models.py` | `client/src/ModelSelector.tsx`、`client/src/types.ts` | `server/tests/test_openai_compat.py`（10 项）；`test_model_catalog.py`、`test_sdk_adapter.py`；`client/src/models.test.tsx`；远端历史回归记录 | 新增无网络、注入传输的 Responses/Chat Completions 规范化、流式/非流式 envelope、函数工具调用、输入边界、模型白名单和统一错误映射；上游响应诊断不泄露。真实兼容矩阵、认证/限流/超时现场和真实上游用量仍未验收。 |
| PROT-002 | 已实现 | `server/app/mcp_import.py`、`server/app/runtime.py`、`server/app/main.py` | `client/src/McpImportDialog.tsx`、`ConnectionsPage.tsx` | `server/tests/test_mcp_import.py`、`test_mcp_transport.py`、`test_api.py`；客户端集成测试 | 两种配置格式解析、Streamable HTTP 连接、工具发现和“不自动绑定/不自动免审批”边界已有实现与测试；仍缺合成工业 MCP 的完整业务回归套件。 |
| PROT-003 | 部分实现 | `fixtures/synthetic-industrial-v1/`、`examples/synthetic_industrial_mcp.py`、`scripts/synthetic_benchmark.py` | 当前无生产页面入口；回环 HTTP MCP 可独立启动 | `python3 scripts/synthetic_benchmark.py plan/run/verify/clean`；`examples/test_synthetic_benchmark.py` 5 项 | 阶段 0 已固定版本、seed、文档/表格/图片、标准答案、只读 MCP 工具契约、来源标记和哈希校验；尚未接入产品端到端 MCP 回归和真实模型。 |
| DOC-001 | 部分实现 | `server/app/main.py` 上传路由、`server/app/knowledge.py`、`server/app/runtime.py` 附件解析 | `client/src/ChatPage.tsx`、`client/src/KnowledgePage.tsx`、`FilePreview.tsx` | `server/tests/test_api.py`、`test_knowledge_evidence.py`；`client/src/workspace.test.tsx` | 已支持文本、DOCX、XLSX、PDF 和常见图片上传；XLSX 单元格解析、本地 OCR 缺失状态和失败后重试已有基础，仍缺统一异步进度及聊天图片视觉解析。 |
| RAG-006 | 部分实现 | `server/app/knowledge.py`、`service.py`、`runtime.py` | `client/src/KnowledgePage.tsx`、`Sources.tsx` | `server/tests/test_knowledge_evidence.py`、`test_capabilities_04.py` | 上传后同步生成版本、页/表格片段、词法/本地向量索引、来源登记和当前版本快照；异步摄取、失败重试队列和完整多模态流水线仍待补齐。 |
| RAG-007 | 部分实现 | `server/app/knowledge.py`（版本化图谱表、抽取和查询）及 `docs/design/RAG_MULTIMODAL_GRAPH.md` | `client/src/KnowledgePage.tsx`（图谱画布） | `server/tests/test_knowledge_evidence.py::test_knowledge_graph_is_versioned_and_source_linked`；`python3 -m py_compile` | 已有文本/表格/OCR 片段共现节点、关系和来源指针，并只展示知识库当前 ready 版本；尚缺语义实体/主张、图片区域模型、增量任务和更细粒度权限复核。 |
| RAG-008 | 部分实现 | `server/app/knowledge.py` 图谱 API | `client/src/KnowledgePage.tsx` SVG 图谱画布 | `server/tests/test_knowledge_evidence.py`；客户端 TypeScript/回归需在依赖恢复后执行 | 已提供权限保护的图谱查询、版本/页码来源和最小 SVG 可视化；RAG 链路指标、证据区域交互、性能边界和完整客户端回归仍待补齐。 |
| APR-001 | 部分实现 | `server/app/security.py::tool_risk_profile`、`server/app/service.py::authorize_tool`、`server/app/runtime.py` | `client/src/AssistantConfiguration.tsx`、`client/src/ChatPage.tsx`（风险审批卡片） | `server/tests/test_capabilities_04.py`、`test_api.py`；`client/src/workspace.test.tsx` | 已有低风险只读/中风险写入/高风险外部或未知工具的统一保守分类；授权与参数仍由服务端每次调用复核，未知工具默认拒绝。完整连接工具语义白名单和模型代审仍未完成。 |
| APR-002 | 部分实现（本地合成） | `server/app/model_approval.py`、`server/app/db.py`、`server/app/runtime.py`、`server/app/main.py` | 管理员专用策略接口及助手能力配置内“审批策略”面板 | `server/tests/test_model_approval.py`：8 项通过；`client/src/assistantConfiguration.test.tsx` 覆盖面板可见性 | 仅本地合成 reviewer；策略默认禁用、仅允许 `Read`/`Glob`/`Grep`，服务端要求授权函数显式返回 `True`，并前后复核授权/策略版本/参数摘要，异常/超时/非法/不确定结果拒绝，高风险转人工。真实模型质量、成本、延迟、客户策略和持续攻击集仍缺，不能把本地合成证据算作生产完成。 |
| APR-003 | 部分实现 | `server/app/runtime.py` 审批生命周期、`service.py` 审批投影、`db.py` 审计迁移、`main.py` 决策接口 | `client/src/ChatPage.tsx`（风险/影响/不可变参数审批卡片） | `server/tests/test_api.py`、`test_session_isolation_042.py`；客户端集成测试 | 参数保持服务端不可变快照并以摘要校验，页面展示风险与影响；`decided_at`、`decided_by`、`decision_source` 记录用户、超时、取消、中断、重启和运行结束。管理员/双人审批、完整重启回放矩阵仍待补齐。 |
| APR-004 | 部分实现（本地合成） | `server/app/high_risk_approval.py`、`server/app/db.py`、`server/app/runtime.py`、`server/app/main.py`、`server/app/subagents.py` | 管理员白名单过滤队列、助手能力配置内“双人审批”策略面板、全局待审批页和运行内双人投票卡；未配置策略时顶层任务仍兼容旧的单人用户确认路径 | `server/tests/test_high_risk_approval.py`：6 项通过；`server/tests/test_subagents.py`：8 项通过；`client/src/assistantConfiguration.test.tsx` 覆盖策略面板可见性 | 启用策略后，高风险工具使用管理员/工具白名单快照和两名不同管理员投票；启用高风险工具时白名单至少两名管理员；最终投票和子任务唤醒前复核策略快照/取消状态；本地合成子任务继承父策略、无策略默认拒绝，父取消使等待中的子审批过期；任一拒绝、取消、过期或重启失效；申请人只能拒绝不能批准自己的请求。真实子会话、生产角色和客户风险矩阵仍缺，不能算生产交付。 |
| SUB-001 | 部分实现（本地合成） | `server/app/subagents.py`、`server/app/db.py`；原生 `Agent`/`Task` 仍禁用 | 当前提供受保护的子任务 API，尚无完整客户端计划页 | `server/tests/test_subagents.py`；本地合成执行器和 SQLite 生命周期测试 | 已有 child 持久化、父子状态、上限/深度校验、并发执行、失败隔离、取消和恢复中断；真实模型/生产调度、持久预算和跨进程租约仍缺。 |
| SUB-002 | 部分实现（本地合成） | `server/app/subagents.py`、`server/app/high_risk_approval.py`、`server/app/runtime.py`、`server/app/main.py` | 当前只有安全父事件接口，尚无完整子任务审批/可视化界面 | `server/tests/test_subagents.py`：8 项通过，含双人继承、默认拒绝、父取消过期和最终投票策略复核 | 子任务有独立 workspace/memory 标识、只读能力快照，禁止写入/网络/MCP/父历史；高风险子任务已关联父级双人审批并在父取消或父策略变更时拒绝。真实 SDK 会话、跨进程审批恢复、产物来源和生产负载仍缺。 |
| UX-001 | 部分实现 | `server/app/runtime.py` 运行阶段和事件、`service.py` 运行投影 | `client/src/ChatPage.tsx`（`LiveActivity`、`EventRow`） | `server/tests/test_api.py`、`test_context.py`；`client/src/stream.test.ts`、`workspace.test.tsx` | 已展示安全阶段、工具、审批、压缩和最近步骤，不显示私有思维链；计划、子任务、模型代审和双人投票事件已统一接入本地安全进度流，图谱检索和真实子会话仍待补齐。 |
| BENCH-001 | 部分实现 | `fixtures/synthetic-industrial-v1/`、`scripts/synthetic_benchmark.py` | 当前无对标页面或产品助手初始化入口 | `python3 -m unittest discover -s examples -p 'test_synthetic_benchmark.py' -q`：5 项通过 | 阶段 0 已提供固定数据集、标准答案、工具契约、seed、版本哈希、清理策略和可重复 runner；尚未创建真实产品基准助手或跨平台执行矩阵。 |
| BENCH-002 | 部分实现 | `scripts/synthetic_benchmark.py` 报告聚合与原始证据结构 | 当前无指标报告页面 | `run` + `verify`；证据见 `docs/evidence/SYNTHETIC_BENCHMARK_STAGE0_2026-11-06.md` | 阶段 0 记录任务正确性、来源覆盖率、本地耗时、记忆和原始工具结果；Token、费用、原生上下文、视觉质量和跨平台对标保持 `not_measured`，不可据此宣称性能优势。 |
| BENCH-003 | 部分实现 | 当前仍无基准报告 API；页面接受经过校验的合成 JSON 快照 | `client/src/BenchmarkPage.tsx` 提供管理员可见的百分比柱状图、详细指标卡、待测状态和 JSON 导出 | `client/src/benchmark.test.tsx`：4 项通过；`npx tsc -b` | 仅展示 `synthetic-industrial-v1` 阶段 0 快照；客户现有助手、Token、上下文、费用、多模态质量等缺失指标保持待测。仍需服务端报告读取、真实同任务基线和完整指标图表。 |

### 6.1 汇总

按 v0.2 对齐表和本节逐项状态，初始审计口径为 22 项已实现、17 项部分实现、3 项设计中、2 项 TODO；本轮优先级 1 已将 SUB-001/SUB-002 从设计中推进为“部分实现（仅本地合成证据）”，优先级 2 将 APR-002、优先级 3 将 APR-004 从 TODO 推进为“部分实现（仅本地合成证据）”，最新状态为：**22 项已实现、21 项部分实现、1 项设计中、0 项 TODO**。其中 MOD-004 已有最小自动路由闭环但仍缺真实模型成本/延迟证据；PROT-003、BENCH-001、BENCH-002 和 BENCH-003 已完成阶段 0 最小闭环；RAG-007/008 已有最小图谱 API 与画布，本地合成高风险子任务审批级联也已补证，但完整产品接入、真实模型指标、跨平台对标、语义图谱、真实模型代审、真实子会话和生产 Sub-agent 仍未完成。本节以可核对的 44 行状态为准，并保留设计中与 TODO 的区别。

## 7. v0.3 部分实现差距登记（学校云）

用户已批准进入下一阶段，但本轮不把新增模块或页面当作完成证据。初始 17 项部分实现（`ID-001`、`ID-002`、`ID-003`、`MOD-003`、`MOD-004`、`PROT-001`、`PROT-003`、`DOC-001`、`RAG-006`、`RAG-007`、`RAG-008`、`APR-001`、`APR-003`、`UX-001`、`BENCH-001`、`BENCH-002`、`BENCH-003`）以及本轮新增为部分实现的 `SUB-001`、`SUB-002`、`APR-002`、`APR-004`，逐项剩余缺口、验收标准、交付阻塞性、无真实客户数据可验证范围和外部输入，统一登记在 [PARTIAL_REQUIREMENTS_GAP_REGISTER_2026-11-06](../verification/PARTIAL_REQUIREMENTS_GAP_REGISTER_2026-11-06.md)。

该登记明确：合成数据和本地模拟可推进协议、上传、RAG、图谱、审批生命周期、执行进度、指标结构、模型代审安全边界和高风险双人投票；客户样例、TokenHub 开通、真实模型成本/延迟/质量、客户现有助手基线、客户角色矩阵和部署环境容量仍是外部证据。SUB-001/SUB-002 当前只完成本地合成、有界、只读编排，其中高风险子任务已验证父策略继承、无策略默认拒绝和父取消失效；APR-002 只完成本地合成 reviewer 和拒绝优先策略，APR-004 只完成本地合成管理员/双人审批及子任务级联，不能宣称真实模型或生产能力。最新 21 项状态保持“部分实现”，不会因为设计文档、静态页面或合成快照升级为“已实现”。下一轮继续按一个优先级一个优先级推进，并在每次实现后补测试和回归证据。

## 8. 优先级 1 实现复核（学校云，2026-11-06）

- `SUB-001`、`SUB-002` 已从“设计中”调整为“部分实现（本地合成证据）”；实现位于 `server/app/subagents.py`，持久化表和索引位于 `server/app/db.py`，受保护接口位于 `server/app/main.py`。
- 本地合成执行器固定为只读：禁止写入、网络、MCP、父历史和父记忆；每个 child 使用独立 workspace/memory 标识，父事件只发 child ID、阶段、进度、停止原因和结构化计数摘要。
- 测试 `server/tests/test_subagents.py` 覆盖隔离、最大子任务数、失败隔离、父取消级联、完成子任务不变、恢复中断不重放、事件游标稳定性和 HTTP 只允许 `synthetic`。验证命令和限制见 [SUBAGENT_LOCAL_PRIORITY1_2026-11-06](../evidence/SUBAGENT_LOCAL_PRIORITY1_2026-11-06.md)。
- 未实现内容：真实模型/客户 MCP、持久预算、跨进程租约、真实 SDK 会话、跨进程审批恢复、产物来源映射、生产负载和部署；子任务高风险审批级联仅在本地合成路径验证。优先级 1/3 均未触碰生产服务或真实客户数据。模型代审在优先级 2 另有独立复核，仍仅本地合成。

## 9. 优先级 2 模型代审复核（学校云，2026-11-06）

- `APR-002` 已从 TODO 调整为“部分实现（本地合成证据）”；实现位于 `server/app/model_approval.py`，策略表和审批审计列位于 `server/app/db.py`，管理员接口位于 `server/app/main.py`，运行时接入位于 `server/app/runtime.py`。
- 默认禁用、默认拒绝、最小权限：启用和修改只允许启用的 `admin` 角色，模型标识限定为本地合成范围，只允许 `Read`、`Glob`、`Grep` 低风险工具，并同时检查数据范围、操作范围和金额上限。
- reviewer 只接收最小化摘要和参数摘要，不接收原始提示词、私有历史或凭据；服务端在评估前和放行前复核授权、策略版本/摘要和参数 digest。异常、超时、非法/多余字段、低置信度和不确定结果拒绝；中高风险工具不进入模型判定而转人工。
- `server/tests/test_model_approval.py` 当前 8 项通过；服务端授权函数必须显式返回 `True`，否则模型代审安全拒绝。完整证据和边界见 [MODEL_APPROVAL_PRIORITY2_2026-11-06](../evidence/MODEL_APPROVAL_PRIORITY2_2026-11-06.md)。未实现真实模型质量/成本/延迟、客户策略和持续提示注入攻击集；APR-004 已在下一优先级另行补充本地合成证据。

## 10. 优先级 3 高风险管理员/双人审批复核（学校云，2026-11-06）

- `APR-004` 已从 TODO 调整为“部分实现（本地合成证据）”；实现位于 `server/app/high_risk_approval.py`、`server/app/subagents.py`，策略/投票表和审批快照字段位于 `server/app/db.py`，策略及投票接口位于 `server/app/main.py`，运行时分流位于 `server/app/runtime.py`。
- 启用助手策略后，外部 MCP、删除/批量/凭据/发布等高风险标记进入双人路径；工具白名单和启用管理员白名单在创建审批时快照化，必须两名不同管理员允许，任一拒绝立即终止，申请人只能拒绝不能批准自己的请求。
- 投票、策略版本/摘要、参数摘要、决策者、来源和时间均留在服务端；取消、过期、策略变更和重启使旧审批失效且不自动重放。管理员待审批队列会过滤已过期或策略快照失效的请求。审批事件不含凭据、私有提示词或原始参数。
- `server/tests/test_high_risk_approval.py` 当前 6 项通过；`server/tests/test_subagents.py` 当前 8 项通过，级联证据见 [SUBAGENT_APPROVAL_CASCADE_PRIORITY3_2026-11-07](../evidence/SUBAGENT_APPROVAL_CASCADE_PRIORITY3_2026-11-07.md)。最终投票后的策略/取消竞态也已在本地合成路径复核。完整双人证据见 [HIGH_RISK_DUAL_APPROVAL_PRIORITY3_2026-11-06](../evidence/HIGH_RISK_DUAL_APPROVAL_PRIORITY3_2026-11-06.md)。管理员策略面板、全局待审批页和运行内双人卡已有客户端本地验证；仍缺真实子会话、生产角色审查和客户风险矩阵。
