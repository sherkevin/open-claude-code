# 低代码工作流优先级 4C 证据（2026-11-07）

## 范围

本切片提供财务申请和财务报销的合成模板，帮助客户从真实业务形态开始搭建流程。模板只使用虚构字段、金额和角色，不代表任何客户的财务规则。

## 已实现

- 服务端提供 `GET /api/v1/workflow-templates` 模板目录。
- 服务端提供 `POST /api/v1/assistants/{assistant_id}/workflows/from-template`，复制模板为新的未发布工作流草稿。
- `finance-application-v1` 包含填报、政策检索、金额条件、部门负责人审批和 Excel 产物节点。
- `finance-reimbursement-v1` 包含报销填报、费用政策、合规检查、双人审批和 Excel 产物节点。
- 所有节点配置使用 `mode=synthetic`，模板响应和复制结果带 `synthetic=true` 证据。
- 客户端能力池直接展示模板，并要求人工复制后再编辑、保存和发布。
- 合成运行可选绑定到同一助手的用户会话；服务端拒绝跨助手会话，并持久化版本、会话、权限摘要和无凭据权限快照。

## 验证

```text
PYTHONPATH="/media/data3/dengkw/projects/open-claude-code:/tmp/open-claude-pylib" \
pytest -q server/tests/test_workflows.py
7 passed, 1 warning

cd client
npm test -- --run
16 个测试文件、72 个测试通过
npx tsc -b
通过
```

## 未完成与边界

- 模板目前只执行声明字段的本地合成校验并返回结构化 Excel 预览，不写入财务系统，也不生成面向客户的正式 Excel 文件。
- 会话绑定和权限快照仅证明本地合成边界，不能证明真实助手运行时已经接入工作流。
- 客户字段、审批角色、金额边界和数据保留规则仍需客户样例与批准。
- 不使用真实客户财务资料，不连接生产系统，不调用付费模型。
