# 产物生成与图片生成边界（2026-11-07，2026-11-12 更新）

## Excel

- Agent 工具仍在每个会话的 `artifacts/` 工作区中生成文件，运行结束后由服务端收集为可授权下载的文件，并在执行记录中产生 `artifact.created` 事件；直接调用接口则立即返回用户文件和下载路径。
- `mcp__workspace__generate_xlsx` 工具和 `POST /api/v1/artifacts/xlsx` 是结构化导出入口。工作表、行、列和单元格长度均有上限；导出的字符串会对 `= + - @` 开头内容加前缀，避免把业务文本变成 Excel 公式。
- 导出只写当前会话产物目录或当前用户文件区，不访问外部系统。工具不是自动放行，仍按现有审批策略记录；下载继续使用 `/api/v1/files/{id}/content` 的用户权限检查。
- 新增 `GET /api/v1/files/{id}/xlsx-preview`：服务端用标准库 `zipfile` 只读解析工作簿，返回工作表名、列、单元格文本、行数和截断标记，并统计公式单元格与被转义文本单元格的数量。它从不计算公式、宏或外部链接，只展示 `sharedStrings` 和单元格缓存文本，因此公式单元格显示的是文件里已有的缓存值。
- 客户端 `XlsxPreview` 提供工作表切换、按任意单元格筛选、按列排序（数字按数值、文本按自然顺序）、分页、重新读取和下载原文件。排序与筛选只作用于已取回的有界预览数据，不会改写服务器上的文件；超过 `rows` 上限时明确标注“预览已截断”，需要完整数据时下载原文件。
- 聊天内工作流运行时，`artifact` 节点生成的 Excel 会被物化为会话文件并挂到助手消息附件上；直接下载、重新打开预览、刷新对话后再次打开都可复现。

## AIGC 生图

- `mcp__workspace__generate_image` 工具、`POST /api/v1/artifacts/image` 契约和 `GET /api/v1/artifacts/image-capability` 状态入口保持不变，2026-11-12 补齐了真实适配器边界。
- 默认仍然关闭。只有同时满足下列条件，`available` 才为真：
  - `AGENT_IMAGE_GENERATION_ENABLED=true`；
  - `AGENT_IMAGE_GENERATOR_URL` 已配置，且主机在 `AGENT_MCP_ALLOWED_HOSTS` 内；
  - `AGENT_IMAGE_GENERATOR_CONTRACT` 是已评审的 `openai-images-v1`；
  - 请求发往 `{url}/images/generations`，返回 `data[0].b64_json`，图片字节是合法的 PNG/JPEG/WebP，且不超过 `AGENT_IMAGE_GENERATION_MAX_BYTES`。
- 返回 URL 或其他形状的响应一律拒绝（`IMAGE_RESPONSE_UNSUPPORTED`），避免服务端去拉取任意外部地址；解密失败、超时、超大和非法图片分别有独立的错误码。
- 每次请求先写 `image_generation_jobs` 记录（用户、会话、提示词摘要与字符数、模型、状态、错误码、文件、字节数、用量、时间）。路由不可用时记录 `unavailable` 并返回 503 `IMAGE_MODEL_UNAVAILABLE`，绝不生成占位图片、伪造凭据或把普通聊天模型当生图模型；失败请求同样留痕，`GET /api/v1/artifacts/image-jobs` 可按用户查询。
- 图片成功后会登记为普通用户文件，聊天附件、预览、缩放和下载复用现有文件权限链路；客户端预览支持 25%–400% 缩放与复位。
- 当前本机没有配置已授权的图片模型路由，因此运行时仍是“明确失败”路径；测试用本地 `ThreadingHTTPServer` 合成 1×1 PNG 证明适配器在真实 HTTP 往返下可用，不涉及真实客户数据或付费模型。

## 验收证据

- `server/tests/test_artifacts.py` 覆盖有效 Office Open XML、公式注入防护、行数限制、会话产物路径、用户文件下载和缺少图片授权时的拒绝状态；工作区工具审批路径由 `test_capabilities_04.py` 覆盖。
- `server/tests/test_workflow_chat.py` 覆盖聊天内工作流：审批前阻塞、批准后生成 Excel、下载、预览、重新打开、重复请求幂等、取消、跨助手拒绝、图片明确失败与适配器成功路径、公式安全。
- 历史回归：服务端 `399 passed`；客户端 `16 个测试文件、72 passed`，TypeScript 检查通过。
- 真实客户数据、生产部署、服务重启和未授权付费模型均未触碰。
