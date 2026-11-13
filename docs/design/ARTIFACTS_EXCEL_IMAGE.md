# 产物生成与图片生成边界（2026-11-07）

## Excel

- Agent 工具仍在每个会话的 `artifacts/` 工作区中生成文件，运行结束后由服务端收集为可授权下载的文件，并在执行记录中产生 `artifact.created` 事件；直接调用接口则立即返回用户文件和下载路径。
- 新增 `mcp__workspace__generate_xlsx` 工具和 `POST /api/v1/artifacts/xlsx` 结构化导出入口。工作表、行、列和单元格长度均有上限；导出的字符串会对 `= + - @` 开头内容加前缀，避免把业务文本变成 Excel 公式。
- 导出只写当前会话产物目录或当前用户文件区，不访问外部系统。工具不是自动放行，仍按现有审批策略记录；下载继续使用 `/api/v1/files/{id}/content` 的用户权限检查。

## AIGC 生图

- 新增 `mcp__workspace__generate_image` 工具、`POST /api/v1/artifacts/image` 契约以及 `GET /api/v1/artifacts/image-capability` 状态入口。
- 当前本地模型目录没有声明 `image_generation` 的已授权模型，且没有经过验收的 TokenHub 图片适配器，因此状态固定为 `pending_authorization`，请求返回 `IMAGE_MODEL_UNAVAILABLE`；不会把普通聊天模型当成生图模型，也不会生成占位图片或伪造凭据。
- 后续启用需要管理员提供独立图片模型目录项、已批准的适配器请求与响应格式、额度边界和验收样例；在此之前功能保持默认拒绝。

## 验收证据

- `server/tests/test_artifacts.py` 覆盖有效 Office Open XML、公式注入防护、行数限制、会话产物路径、用户文件下载和缺少图片授权时的拒绝状态（4 项通过）；工作区工具审批路径由 `test_capabilities_04.py` 覆盖。
- 本轮服务端全量回归 `399 passed`；客户端回归 `16 个测试文件、72 passed`，TypeScript 检查通过。
- 真实客户数据、生产部署、服务重启和未授权付费模型均未触碰。
