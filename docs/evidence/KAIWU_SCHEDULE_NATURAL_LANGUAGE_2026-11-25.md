# A2.5 自然语言创建计划：隔离验收

基线：5145307；独立工作树 kaiwu-schedule-natural-language-20261125。复用固定 DeepSeek Harness fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159 的 schedule_create 参数契约及实际 RRULE 规则计算，宿主沿用现有 SQLite/FIFO，不启用第二调度器。上游 Host 范围和新建 Session 能力不直接开放。

## 行为与安全边界
- 仅当前已授权人工前台 Harness 轮次，可请求 title/prompt/rrule/target=this_session；明确时区，拒绝任意其他目标。一次性规则用 COUNT=1。宿主显示完整指令、归一化规则、首个 UTC 与当地执行时间，必须本人确认。
- 写入事务重验活动轮次、会话归属、当前策略、批准人、批准状态、期限和完整参数；首个时间已过去则拒绝，不自动改时间。原计划与唯一 run/approval/series 回执同一事务写入。每轮至多成功创建一项；定时轮次、子智能体、恢复轮次不得创建。模型审批不能替代本人批准；双人审批配置暂拒绝该入口以保护私有提示，沿用原手动任务中心。未来运行继续受原业务权限约束。
- 公共 API 创建与工具共享准备、校验和持久化，不重复实现规则；计划正文仍是 prompt，title 保留在批准与回执中，不另增目录标题字段。自然语言查看/修改/取消尚未实现，现有任务中心可操作。

## 验收结果
日志目录：/media/data3/dengkw/acceptance/kaiwu-schedule-natural-language-20261125/。
- 专项后端 7/7（pytest-third.log）；实际固定 Node/Harness + 本地合成 Messages 服务 + HTTP 人工批准 + SQLite 1/1（real-3.log），模型收到真实计划 ID，批准前无计划、批准后写入。没有接触供应方生产模型或客户数据；不代表自然语言理解质量实测。
- 组合后端 119 通过、18 失败（pytest-final.log）。原主线同一 profile 套件复现 43 通过、18 失败（profile-baseline.log），原因均为旧测试模拟源码缺少 message.ts；补模拟模块后 60 通过、1 失败（profile-fixed.log），旧图片上限断言 11 与既有 20 上限不一致。仅更新测试夹具和边界断言（20 接受/21 拒绝），不改实际运行限制。最终完整 profile 套件 61/61（profile-fixed2.log），结合组合中的其他 76 项均通过；没有声称一次完整组合重跑。调度/编辑/目录专项另有 19/19（diagnostic.log）。
- 前端 17/17（vitest.log），TypeScript（tsc.log）与 /project/kaiwu/ 构建（build.log）通过；本切片尚未真实浏览器验收。
- 实际联调过程中修正旧注册表未列新工具以及原生 schema 不支持 maxLength/enum 的问题；严格长度和目标校验仍在宿主实现。测试配置缓存变更导致初测失败，修正夹具后通过。

## 后续
优先接续原反馈解决说明工作树，不覆盖其未提交内容；该工作树第二次浏览器验收曾因等待“问题反馈”按钮超时（20s）失败。随后补齐自然语言管理、浏览器真实验收及调度重启/负载/回退闸门。没有切换生产、没有新增客户业务写入。
