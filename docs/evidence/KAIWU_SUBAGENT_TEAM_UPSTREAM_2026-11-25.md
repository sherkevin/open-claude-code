# 2026-11-25｜官方持久子智能体与 Team 基线

固定 DeepSeek Harness fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 原始源码，tracked diff 为空；Node v24.9.0、Vitest 4.1.11、私有 data3 临时目录，清除继承模型凭据。不运行依赖真实外部 Codex/Claude Code 的 real-product 测试。

直接运行官方 Team 两个包、subagent continuation/continuation-inheritance/control/sibling-messages/parked-wait、spawn-in-process、fork-in-process multi-subagent、tool-subagent-control 测试。12 个文件、389/389 项通过。证据 /media/data3/dengkw/acceptance/kaiwu-subagent-team-upstream-20261125/upstream.log。

这仅证明固定官方组件基线，包括持久任务板、恢复、消息和控制等官方契约。开物目前没有接入这些原生完整组件；下一步评估宿主隔离及每轮独占 Node 与原生持久子智能体的生命周期边界，保持工具授权和事件审计，不把官方本地模式冒充分布式团队执行。
