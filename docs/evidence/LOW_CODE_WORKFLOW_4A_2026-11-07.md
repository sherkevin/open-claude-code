# 低代码工作流优先级 4A 证据（2026-11-07）

## 范围

本切片只实现工作流领域契约和本地合成回放，不实现客户可见的低代码画布，不调用模型、模型上下文协议连接、客户系统、真实数据或产物写入器。

## 已实现

- `server/app/workflows.py` 提供 Workflow、版本、节点、连线和运行记录接口。
- 草稿修改会生成新版本；发布版本固定图哈希；回滚会生成新的未发布草稿，不直接改变线上版本。
- 节点类型限制为 `agent`、`skill`、`knowledge_search`、`mcp_read`、`condition`、`approval` 和 `artifact`。
- 服务端拒绝重复节点、无效连线、环、孤立节点、超大配置、敏感字段和非 `synthetic` 执行模式。
- 合成运行固定已发布版本，记录 `graph_hash` 和当前助手/工作流/用户权限快照摘要，返回节点顺序和安全状态；每个节点明确 `external_call=false`。

## 验证

```text
PYTHONPATH="/media/data3/dengkw/projects/open-claude-code:/tmp/open-claude-pylib" \
pytest -q server/tests/test_workflows.py
4 passed, 1 warning
```

覆盖发布门槛、版本固定、回滚、并发版本冲突、环检测、敏感字段拒绝和外部执行拒绝。

## 未完成与边界

- 尚无客户端工作流画布、节点配置抽屉、拖拽连线或运行进度图。
- 尚未把工作流绑定加入助手配置和真实会话运行时。
- 尚未实现真实 Agent、MCP、知识检索或产物节点执行。
- 不能据此宣称客户低代码平台或生产工作流已经交付。
