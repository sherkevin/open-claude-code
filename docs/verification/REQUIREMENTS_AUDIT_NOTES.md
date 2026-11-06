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
| MOD-004 | 已实现 | `server/app/model_recommendation.py`、`main.py`；`client/src/NewSession.tsx`、`ChatPage.tsx` | `server/tests/test_model_recommendation.py`；`client/src/integration.test.ts`；远端记录中的 provider router 结果 | 推荐按钮只在空闲会话应用结果，活动运行受保护；远端曾报告 `router.mode=provider`，本轮不重复真实模型调用。 |
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
| CTX-004 | 已实现 | `server/app/runtime.py`、`service.py`、`models.py`；`client/src/RunMetrics.tsx`、`runMetrics.test.tsx` | `server/tests/test_sdk_adapter.py`、`test_api.py`、`test_context.py` | 运行保存 Token、耗时、模型和费用可靠性标记；供应商未提供的真实费用保持未知，不能用估算宣传实际账单。 |

## 3. 设计中能力与证据边界

- **多模态知识图谱：设计中。** `docs/design/RAG_MULTIMODAL_GRAPH.md` 只有方案和阶段验收，不代表生产实现。
- **Sub-agent / Multi-agent：设计中。** `docs/design/SUBAGENT_ORCHESTRATION.md` 只有受控 fan-out 方案；当前没有生产调度器、子任务持久化、审批级联和负载验收。
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

当前开发树的 27 个编号需求中，**22 项已实现、4 项部分实现、1 项设计中**。服务端 322 项测试、客户端 53 项测试、TypeScript 检查和独立目录生产构建均已通过。部分实现项的缺口集中在默认界面品牌最终决策、持续诱导攻击集、真实客户 MCP/知识库/业务基线、混元 4 与 Kimi 3.1 PRO 的 TokenHub 开通；RAG-005 多模态能力和生产 Sub-agent 仍是设计中。

本轮没有重启 systemd 服务、没有读取或修改生产数据与凭据、没有调用真实客户 MCP 或真实模型；0.4.2 发布归档保持冻结。
