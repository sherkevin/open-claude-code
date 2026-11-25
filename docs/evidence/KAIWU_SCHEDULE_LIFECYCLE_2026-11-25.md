# 2026-11-25｜周期任务暂停与安全恢复

## 范围及复用

主线基线 437e5e5；独立工作树 kaiwu-schedule-lifecycle-20261125。核查活动进程及既有候选工作树，没有发现同模块并行开发。复用固定版官方 DeepSeek Harness fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 的 Cron/RRULE occurrence 解析，沿用企业宿主 SQLite、FastAPI、FIFO、React 与原会话授权；不新增调度器或业务工具权限。

- 周期计划新增会话内暂停、恢复 API 和前端入口。暂停阻止新周期派发，已生成的 occurrence（含已进入队列的任务）不被撤销；恢复只选严格未来的规则时间，不补跑暂停期间的周期。
- 上一次 occurrence 尚未确认终态时拒绝恢复；失败后必须明确确认可能产生的影响，确认绑定到原失败 occurrence ID，保留历史及队列回执，不重试旧任务。后续新的失败仍触发暂停。
- 已耗尽的有限 RRULE 标记 exhausted，不生成额外运行。重新计算使用原固定版规则模块；用户发起的计算与创建共享单槽限制，避免耗尽定时派发计算资源。事务内重新核验计划、运行状态和授权。
- 数据库仅增加 nullable acknowledged_occurrence_id 列，无历史任务删除或重写。旧代码不理解该确认列时可能重新暂停已确认的失败系列，属于安全失败；但不代表可直接把活跃高级周期规则交给旧版本，A1 发布回退闸门仍必须验证。

## 隔离验收

证据目录：/media/data3/dengkw/acceptance/kaiwu-schedule-lifecycle-20261125/。测试数据库与临时目录均在 data3，使用 FastAPI TestClient、SQLite、FakeRuntime、实际固定上游规则模块。无客户数据、真实供应商调用或生产服务修改。

| 验收 | 结果 |
|---|---|
| 新增生命周期 7 项及既有周期/官方规则回归，共 18 项 | 18/18 通过（pytest-final.log） |
| 前端会话任务组件 | 6/6 通过（vitest.log） |
| TypeScript noEmit | 通过（tsc.log） |
| Vite 生产路径 /project/kaiwu/ 构建 | 通过（build.log） |

覆盖暂停/重复恢复、跳过历史周期、失败确认与新失败、活动 occurrence、跨用户和跨会话拒绝、真实 Cron/RRULE 有限规则结束及高级规则用户计算单槽保护。首轮 17 项通过后补了限流并完整复验。前端验证为组件测试和构建，未运行本切片真实浏览器交互验收；未开展生产验证。Starlette TestClient 的既有 httpx 弃用警告不影响本次断言通过。

## 下一步

A2 尚需全局任务中心、编辑、立即运行、自然语言创建，以及真实浏览器交互验收、重启与回退闸门和负载演练。禁止直接发布或自动重放已发生的业务动作。A3、Goal、Team、Mem0 等按总计划推进。
