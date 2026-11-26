# D2.3c4d1｜已提交清空回执的逻辑退役准入

基线 760f901；独立工作树 kaiwu-mem0-retirement-admission-20261126。复用 NativeMemorySourceReader 的当前权威快照/owner clear 校验、现有 SQLite BEGIN IMMEDIATE 事务及 ScopeStore 原子持久标记。独立显式宿主入口只允许确切当前已提交清空回执；SQLite 保留写锁直至逻辑退役标记 fsync，合作的其他数据库连接在此期间不能修改授权。拒绝忙、错误/过期清空、撤权、无证明历史退役；相同且仍有效的回执可显式确认，重读后 fsync，不自动重放。证明绑定 agent、完整 native generation 与 operation key，仅保存 SHA-256。未打开派生正文/SDK，不删除字节。旧无证明退役不自动升级。

后端首轮 71/71，最终 72/72（新增 4 项真实 SQLite/宿主测试，51.42 秒），真实固定 Mem0/Qdrant 合成回环 6/6，退出码均 0。真实 SDK probe 验证逻辑标记重开、拒绝退役路径、其他作用域仍可用及目标 SQLite 仍存在；该 probe 使用合成证明，不等于产品授权联调。真实宿主测试另外验证竞争 DB 连接写入被锁住、回执丢失后仅同证明显式确认。日志 /media/data3/dengkw/acceptance/kaiwu-mem0-retirement-admission-20261126/{full.log,full.exit,final.log,final.exit,real-sdk.log,real-sdk.exit}。

状态为 retired / cleanup_status=not_performed，绝非删除完成。当前稳定身份退役不可重开；普通清空后创建新派生代次需要单独产品生命周期方案，不接现有 clear API。禁用用户/归档/助手授权变化不等同删除许可；现有 patch_user 仅明确 Goal 撤权处理，尚无核实可复用的 Mem0 专用删除授权事件。下一步 D2.3c4d2：明确独立删除专用准入、持久处置回执和新代次边界，优先非破坏的隔离试验；同时继续独立产品接线与质量评测。不删除客户或生产数据、不默认外发、不打开生产闸门。
