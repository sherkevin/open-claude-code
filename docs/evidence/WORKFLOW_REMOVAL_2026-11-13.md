# 工作流功能下线证据（2026-11-13）

## 决策

用户要求从当前知行工作台版本移除低代码工作流功能，优先保证聊天、知识库、模型补全、业务连接、审批和产物主链路稳定。本次变更只影响产品可见范围和默认服务入口，不涉及生产部署或共享服务重启。

## 实施范围

- `client/src/AssistantDetailPage.tsx` 不再渲染“工作流”页签，不再导入 `WorkflowPage` 或懒加载工作流画布；能力配置和会话页签保持原有导航与未保存保护。
- `server/app/config.py` 新增 `workflows_enabled`，默认读取 `AGENT_WORKFLOWS_ENABLED=false`。
- `server/app/main.py` 将开关传给工作流路由。
- `server/app/workflows.py` 在开关关闭时保留历史 URL 形状，但经认证请求统一返回 HTTP 410、错误码 `FEATURE_DISABLED`；数据库表和实现保留，避免物理删表破坏历史迁移。
- `.env.example`、`server/.env.example` 和 `docs/API_CONTRACT.md` 明确默认下线及迁移兼容边界。

## 验证

- `.venv/bin/pytest -q server/tests/test_workflow_disabled.py server/tests/test_workflows.py -p no:cacheprovider`：30 项通过，1 个第三方依赖弃用警告。
- `server/tests/test_workflow_disabled.py` 覆盖模板、助手工作流、草稿、发布、运行、运行查询和审批决定等代表性路径，确认默认配置不会创建或执行工作流。
- `server/tests/test_api.py` 的旧工作流契约夹具显式开启 `workflows_enabled=True`，证明迁移兼容实现仍可被隔离回归，不代表客户默认启用。

## 未做事项

未删除 `server/app/workflows.py`、工作流数据库表或历史客户端测试夹具；未来若需恢复或归档，必须单独批准并重新评估需求、权限和数据迁移。未修改共享看板代码、生产服务或腾讯云数据。
