# 模型代审优先级 2 证据（2026-11-06）

**服务器：** 学校云  
**项目：** open-claude-code（知行工作台）  
**范围：** 仅本地合成 reviewer、合成运行时和合成审计；没有生产部署、服务重启、真实客户数据、凭据、MCP 网络调用或付费模型调用。

## 已实现的本地闭环

- 管理员专用接口 `GET/PUT /api/v1/assistants/{id}/model-approval`；策略默认禁用，普通成员不能读取或修改。
- 管理员在助手能力配置的“审批策略”面板可读取和保存模型代审策略；列表输入会去重并过滤通配符，服务端仍执行最终工具/范围校验。该面板不替代真实模型验收。
- 默认拒绝和最小权限：只允许 `Read`、`Glob`、`Grep` 低风险工具；必须同时命中工具、数据范围、操作范围和非负金额上限。
- 运行时在安全只读自动路径前强制进入模型策略路径；高风险、未知和中风险工具不由模型决定，继续转人工审批。
- reviewer 只接收最小化摘要、`synthetic-*` 命名空间内的公开合成模型标识、策略版本和参数摘要；原始提示词、私有历史、凭据和完整参数不发送给 reviewer。服务端拒绝任何非合成模型标识，避免配置路径误启用付费或未授权模型。
- 服务端在评估前和放行前复核授权、参数摘要、策略版本/摘要和边界；策略修改或权限撤销会使旧 allow 失效。
- 审计保存不可变参数快照及摘要、模型标识、策略标识/版本/摘要、工具、操作、数据范围、金额、理由、结果、决策来源和时间；事件只发安全的 `approval.model_decided`。
- reviewer 异常、超时、非法或多余字段、低置信度和不确定结果均安全拒绝；服务端授权函数必须返回显式 `True` 才能进入 reviewer 或执行，任何非明确授权结果均拒绝；默认 reviewer 不执行任何模型或网络调用。

## 验证命令与结果

```text
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests/test_model_approval.py
8 passed, 1 third-party Starlette/httpx deprecation warning

PYTHONPATH=/tmp/open-claude-pylib:server python3 -m py_compile server/app/*.py
pass

git diff --check
pass

PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests
336 passed, 1 third-party Starlette/httpx deprecation warning
```

专项测试覆盖：合成 allow 和审计、数据/操作越界的默认拒绝、高风险转人工、管理员权限、最小化 reviewer 输入、异常/不确定结果拒绝、策略变更后的陈旧 allow 拒绝，以及模型事件投影。

## 证据边界

APR-002 现在是“部分实现（本地合成）”，不是生产可交付或真实模型质量验收。仍缺真实获批模型的准确率、拒绝率、延迟、成本、提示注入鲁棒性和客户策略样例；未授权的付费模型、凭据和真实客户资源不会被伪造或调用。APR-004 管理员/双人审批属于随后优先级 3，本文件仅记录优先级 2 截止时点。

## 后续收口补充（2026-11-07）

- 新增非 `synthetic-*` 模型标识拒绝、服务端授权非明确允许拒绝测试，专项测试现为 8 项；策略仍默认禁用和拒绝。
- 学校云最新全量回归为服务端 358 项、客户端 12 个测试文件/61 项，TypeScript 检查通过；这些结果仍只证明本地合成边界，不改变本文件的生产证据结论。
