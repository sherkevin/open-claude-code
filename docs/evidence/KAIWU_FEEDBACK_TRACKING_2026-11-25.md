# 2026-11-25｜问题反馈的提交回执与本人处理状态

## 范围、复用与边界

主线初始基线 fb2ffe0。与另一工作树 A1 官方 Schedule 规则开发并行，未修改其调度文件。固定上游 Harness message-feedback/command-feedback 提供持久反馈、原消息关联和回执，不包含企业管理员工单处理状态；开物已经有 SQLite 反馈记录、截图和受控管理员处理接口。此切片直接复用原 FastAPI/SQLite 与 React 组件，按上游“记录反馈不唤醒 Agent”原则扩展只读本人状态，不另建反馈系统或接入上游遥测。

- GET /api/v1/conversations/{cid}/feedback 仅按当前身份及会话授权返回最近 100 条本人反馈：编号、描述、处理状态、创建/更新时间、关联轮次和消息 ID。不返回诊断日志、截图、请求参数或其他用户反馈；响应 no-store。
- 现有会话问题反馈弹窗保留提交回执，提供本人历史记录与手动刷新。重新打开、刷新页面后从原数据库读取；切换会话不复用旧弹窗状态。管理员原处理状态（待处理/处理中/已解决）成为用户可查询的状态。无独立推送通知、管理员解决说明、逐消息评价和用户全局历史；这些仍是 A3 后续任务。
- 未改生产进程、客户记录、认证/权限策略或模型调用，不发送外部遥测。新接口仅在现有非 trial 反馈路由中启用。无数据库迁移，回退到上一版本仍保留已提交反馈和管理员状态。

## 隔离验收

本次使用 /media/data3/dengkw/acceptance/kaiwu-feedback-tracking-20261125/ 作为证据目录及隔离 SQLite；使用已安装 Python、Node 24.9.0、Playwright Core 和 Chromium。无真实供应商或客户数据。

| 验证项 | 结果 |
|---|---|
| server/tests/test_conversation_feedback.py | 4/4 通过，含原截图/脱敏/留存与新增本人查询、状态传播、跨用户/跨会话拒绝、模型零调用 |
| client/src/ConversationFeedback.test.tsx | 4/4 通过，含 HTML 转义、回执与状态展示 |
| TypeScript + Vite 生产挂载路径 /project/kaiwu/ | 构建通过 |
| 真实 Chromium + 真实 FastAPI/SQLite，scripts/feedback_tracking_e2e.py + scripts/feedback_tracking_browser.mjs | 4 个端到端检查通过：浏览器提交及持久回执可见；管理员更新解决状态后刷新浏览器、重新打开可查询；非所属用户（含管理员）不能读取本人历史路由；浏览器未发送任何模型消息 |

第一次 Chromium 启动失败（data3 临时路径过长，Unix Socket path too long），随后使用 data3 下短路径的私有临时目录成功运行；证据 browser-2.log。无生产部署结论。测试脚本要求新建隔离 data 目录，浏览器只连 127.0.0.1，账号为测试固定数据，启动器使用 FakeRuntime，无法访问真实模型。

后续：A2 全局计划中心和任务生命周期仍优先；A3 尚需管理员处理说明、逐消息反馈、通知及生产验收。
