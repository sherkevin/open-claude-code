# 受控试用能力边界

日期：2026-11-13。学校云设计决定。OC-T18 已在当前学校云工作树实现运行时
默认拒绝，并由全新独立会话完成第三轮审核 PASS（阻断数 0，C1-C5 全部通过；
F4-R2 已关闭）。本页状态仍不等于完整回归、浏览器、容量长稳、v4 冻结包或试用发布通过。
独立证据见 `.runtime/evidence/oc_t18_independent_review_r3_20261113.md`。

## 目标

按用户批准，先发布已验证的部分功能，不等待全产品或 OpenClaw 完整替换。
不能仅隐藏按钮：普通成员和管理员、历史会话、模型工具请求均受同一部署边界约束。
普通模式保留现有功能，试用模式显式启用且默认拒绝未知能力，不删除原需求或代码。

## 当前实现状态

- `AGENT_DEPLOYMENT_MODE` 只接受 `normal`、`trial`，默认 `normal`。
  空值、未知值和错误类型启动失败。
- `AGENT_TRIAL_CAPABILITIES` 只接受已知能力名；未知名称启动失败，不支持通配符、
  前缀或隐式扩展。未显式设置时使用下面列出的已审查默认集合。
- `server/app/deployment_policy.py` 是能力、路由模板、工具和检索模式的唯一策略源。
  策略对象只读；`/api/v1/config` 只返回安全的模式、排序后的能力和允许检索模式。
- FastAPI 全局依赖按匹配后的 `route.path` 模板和 HTTP 方法精确判断。试用模式关闭
  `/docs`、`/redoc`、`/openapi.json` 和自动尾斜杠重定向；未匹配路径、错误方法、
  编码路径变体统一返回 `403 FEATURE_DISABLED`，不暴露内部路由是否存在。
- 服务层、Workspace 工具、Harness 工具桥、业务 MCP、独立记忆工具、旧 SDK 执行器
  和 Agent 知识工具均接入同一策略。管理员没有部署级豁免。
- 人工检索和 Agent 检索共用模式白名单。试用模式只允许 `bm25`、`hybrid`；
  `vector`、`hipporag` 和未知模式在检索或模型调用前拒绝。挂载官方 HippoRAG
  后端不会让试用会话自动切换。
- 前端从 `/api/v1/config` 读取能力清单；新建会话在试用模式发送
  `auto_model_routing:false`，聊天发送使用 `auto_route:false`，并避免主动请求
  Skills、附件、Context、自动推荐、产物、Memory 等关闭能力。

实现文件包括 `server/app/config.py`、`deployment_policy.py`、`main.py`、
`service.py`、`workspace_tools.py`、`harness_tools.py`、`harness_bridge.py`、
`harness_memory.py`、`mcp_tools.py`、`runtime.py`、`knowledge.py` 和相应前端文件。

## 精确允许路由

下表是试用模式默认能力集合对应的全部可执行 API 路由。路径按 FastAPI 匹配后的模板
比较，不是前缀匹配；方法不同的请求一律拒绝。

| 能力 | HTTP 方法与路由模板 |
|---|---|
| 健康检查 | `GET /api/v1/health/live`、`GET /api/v1/health/ready` |
| 登录与当前用户 | `POST /api/v1/auth/login`、`POST /api/v1/auth/logout`、`GET /api/v1/auth/me` |
| 只读配置与模型目录 | `GET /api/v1/config`、`GET /api/v1/models` |
| 预置助手 | `GET /api/v1/assistants`、`GET /api/v1/assistants/{item_id}` |
| 会话 | `GET /api/v1/conversations`、`POST /api/v1/conversations`、`GET /api/v1/conversations/{item_id}`、`PATCH /api/v1/conversations/{item_id}` |
| 基础消息 | `GET /api/v1/conversations/{item_id}/messages`、`POST /api/v1/conversations/{item_id}/messages` |
| 运行查看与取消 | `GET /api/v1/runs/{item_id}`、`GET /api/v1/runs/{item_id}/trace`、`GET /api/v1/runs/{item_id}/events`、`POST /api/v1/runs/{item_id}/cancel` |
| 知识库只读浏览 | `GET /api/v1/knowledge-bases`、`GET /api/v1/knowledge-bases/{kb_id}`、`GET /api/v1/knowledge-bases/{kb_id}/documents` |
| 知识检索 | `POST /api/v1/knowledge-bases/{kb_id}/search`，仅 `bm25`、`hybrid` |
| 文档原件与证据 | `GET /api/v1/knowledge-documents/{document_id}/versions`、`GET /api/v1/knowledge-documents/{document_id}/content`、`GET /api/v1/knowledge-documents/{document_id}/chunks` |
| 图片与区域预览 | `GET /api/v1/knowledge-documents/{document_id}/image`、`GET /api/v1/knowledge-documents/{document_id}/regions`、`GET /api/v1/knowledge-documents/{document_id}/thumbnail`、`GET /api/v1/knowledge-documents/{document_id}/regions/{region_id}/thumbnail` |
| 来源证据卡 | `GET /api/v1/runs/{run_id}/sources`、`GET /api/v1/sources/{source_id}` |

`/api/v1/conversations/{item_id}/messages` 在试用模式还执行字段门禁：非空
`file_ids`、非空 `skill_ids`、`auto_route=true` 均在创建运行或调用模型路由前返回
`403 FEATURE_DISABLED`。会话创建、会话修改中的 `auto_model_routing=true` 同样拒绝。

试用模式 Agent 工具白名单只有：

- `mcp__workspace__knowledge_search`
- `mcp__workspace__knowledge_read`

原生 `Read`、`Write`、`Edit`、`Glob`、`Grep`、文件工具、网页工具、Excel、生图、
Memory、业务 MCP 和未知工具都不进入注册清单，执行期仍由
`Service.authorize_tool`、Harness 工具桥和业务 MCP 入口再次拒绝。

## 首批候选启用集合

- 登录、退出、当前用户；预置助手列表和详情，只读配置。
- 会话创建、查看、重命名、归档；基础聊天、流式事件、取消和执行记录。
- 实际已配置的基础模型手动选择；不冒称精确 1007 或 1017 路由已验收。
- 公开示例知识库浏览、BM25/混合检索、证据卡、本地原件和图片查看。
- Agent 工具仅 knowledge_search、knowledge_read；仍执行当前权限、来源与版本检查。

附件上传、向量检索、图谱预览、Excel 等只有专项通过后才能加入同一清单。
服务启动前以独立初始化流程建立试用账号及公开样例，不对试用成员开放管理写入。

## 默认关闭集合

业务连接及 MCP、助手修改、用户管理、审批策略修改、Skills、插件、工作流、
Sub-agent、自动路由、Tab 补全、记忆和压缩、飞书、基准实验、生图及未验收产物。
模型不得调用网页工具、文件工具、外部 MCP、记忆、原生 Read/Write/Edit/Glob/Grep
绕过边界。工具拒绝应在任何早返回、审批创建或外部调用之前完成。

## 最小实现位置

1. 配置只有一份部署级策略。未知模式和能力值失败关闭；标准模式行为兼容。
   `/config` 公开安全的模式、能力和允许检索模式，不公开凭据。
2. FastAPI 全局依赖按 HTTP 方法和匹配后的路由模板精确白名单判断，不宽前缀放行；
   保留原有鉴权、Origin 与请求大小校验。试用模式关闭文档/OpenAPI 路由。
3. `Service.authorize_tool` 在所有特例前硬拒绝；Harness 的普通、业务和独立记忆
   工具注册一起过滤，执行仍再验。旧执行器也不得越过此边界。
4. 允许接口内部校验高级字段：会话创建/更新、prepare_run 不能开启 auto routing，
   消息不能携带非空 skill_ids 或未开放附件；在任何 provider_router 调用前拒绝。
5. 人工及工具检索共用模式门禁；试用固定已验收路径，不因官方 HippoRAG 后端存在
   自动切换。默认混合检索不变，不伪装本地算法为官方。
6. 前端导航、页面渲染、数据请求和快捷键使用同一能力清单。NewSession 不能继续
   硬编码 auto_model_routing:true；关闭 Tab 时保留正常焦点导航，不弹未配置错误。
   助手卡直接开始会话，避免详情页无条件并行请求被禁用的连接/Skills/用户 API。

拒绝码统一为 `403 FEATURE_DISABLED`，不进入业务处理、不产生运行或审批副作用。
权限检查与功能开关不同：允许的能力仍需原来的用户权限，管理员也不能绕过部署边界。

## 必须验证的反向用例

- 标准模式原回归不退化；试用未知路由/能力默认拒绝。
- 成员和管理员直接请求隐藏接口都被拒绝，数据库/模型/外部请求无副作用。
- 会话创建、更新及消息中的 auto_route/auto_model_routing/skill_ids 不可绕过。
- 历史工具授权、伪造 Harness 调用、独立记忆工具及旧执行器原生工具均被拒绝。
- 手工指定 HippoRAG/其他未开放检索模式拒绝，挂载官方后端不改变试用路径。
- 尾斜杠、路径编码、HTTP 方法变化不能绕过精确路由白名单。
- 启用集合的登录、助手、发送/回车、流式重连、取消、证据卡、原件和图片放大在
  浏览器通过；前端不后台请求已关闭接口，撤权后原件与引用仍拒绝。

## 验收口径

能力白名单只用于收敛试用范围，不代表能力效果已通过。任何启用项仍需学校云冻结包
的专项、回归、浏览器、默认四槽容量和稳定性证据。NORMAL 持久化配置的诊断压测
不能代替默认 FULL 配置的交付容量门槛。真实模型小流量与合成压测分别记录。
