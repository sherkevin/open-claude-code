# Sub-agent 优先级 1 本地合成证据

**日期：** 2026-11-06（Asia/Shanghai）  
**服务器：** 学校云  
**项目：** open-claude-code（知行工作台）  
**结论：** `SUB-001`、`SUB-002` 从“设计中”调整为“部分实现（本地合成/只读证据）”，不构成生产交付或真实客户验收。

## 已验证范围

- `server/app/subagents.py` 提供应用自有的有界 fan-out：最多 4 个 child、深度 1、子任务输入最多 2,000 字符、结果有界、并发信号量低于活动会话上限。
- `server/app/db.py` 增加 `subagent_children` 持久化表及 parent/root/status 索引，记录父子标识、状态、停止原因、任务摘要、能力快照、独立 workspace/memory 标识、用量占位和生命周期时间。
- 子任务执行器固定为本地合成执行器，不读取模型、网络、MCP、凭据、父历史或父记忆；能力快照仅允许 `Read`、`Glob`、`Grep`，并明确禁止写入。
- 父任务事件包含 `subagent.plan.created`、`subagent.started`、`subagent.progress`、`subagent.completed`、`subagent.failed`、`subagent.cancelled`、`subagent.plan.completed`；事件只含 child ID、阶段、进度、停止原因或结构化计数摘要，不含任务原文、思维链、私有提示词或凭据。
- 受保护接口为 `POST/GET /api/v1/runs/{run_id}/subagents` 和取消接口；只接受 `mode=synthetic`，并按父任务用户权限检查。原生 Agent/Task 未启用。
- 父取消级联未完成 child；已完成 child 不被改写；单个 child 失败不取消兄弟；恢复把 queued/running/cancelling 置为 `interrupted`，不自动重放。

## 验证命令

在仓库根目录执行：

```bash
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests/test_subagents.py
PYTHONPATH=/tmp/open-claude-pylib:server python3 -m pytest -q server/tests/test_api.py
python3 -m py_compile server/app/subagents.py server/app/db.py server/app/models.py server/app/main.py server/app/runtime.py server/app/service.py
```

结果：子任务专项 **4 passed**；现有 API 回归 **11 passed**；修改后的 Python 文件编译通过。优先级 1 修复后的全量服务端回归为 **330 passed, 1 warning**（第三方 Starlette/httpx 弃用警告）；客户端回归为 **11 个测试文件、57 passed**，`npx tsc -b` 通过。警告不来自项目源码。

## 尚未证明的内容

- 没有真实模型、付费模型、客户 MCP、客户文档、客户助手或生产凭据调用。
- 没有真实 SDK 子会话、持久预算/费用、跨进程租约、分布式恢复、真实审批映射、客户端完整子任务进度页或生产压力测试。
- 模型代审、高风险管理员审批和双人审批不在本优先级内，未实现也未宣称完成。
- 没有生产部署、服务重启或线上构建；任何线上构建和生产变更仍需用户单独批准。
