# D2.3c4c｜Mem0 权威作用域持久存储绑定

基线 e07fd52；独立工作树 kaiwu-mem0-scope-binding-20261126。沿用固定 Mem0 2.2.1 / Qdrant 1.20.0 和现有 TrustedScope，宿主薄适配 `ScopeStore` 将 user/agent/conversation 结构化哈希绑定独立本地向量/历史路径。显式 provision 与仅解析 lease 分开，默认关闭；私有根、目录 inode、绑定文件、SQLite 文件权限与符号/硬链接核验，合作进程持有 flock 时拒绝并发退役。独立持久标记与 SDK 数据目录分开，退役终态即使目录不存在也禁止自动创建或重开，且不读取 SDK 正文、不删除数据。未接 API、模型或正式客户数据；不声称对同 UID 恶意进程、全量 SDK 子路径、分布式文件系统、备份或物理擦除的保证。调用方必须在 lease 内关闭 SDK，实际授权仍需宿主实现；退役元数据接口不等于业务授权。

后端组合 68/68（含新增 11/11，44.12 秒），真实固定 SDK 合成回环 4/4，退出码均 0；两套持久库经新宿主实例重开、历史隔离、退役拒绝初始化及其他作用域继续可用，2 次合成 embedding、0 次 extraction。复现 `scripts/oss/mem0_scope_binding_probe.py`。证据 /media/data3/dengkw/acceptance/kaiwu-mem0-scope-binding-20261126/{full.log,full.exit,real-sdk.log,real-sdk.exit}。最初环境缺 pytest/mcp，切回现有项目 .venv 后完整通过，无新增安装。所有临时文件在 data3。根分区复查仅余 60 MB，停止非必要写入，不清理其他项目。

下一项：基于宿主当前权威的清空回执或显式撤权事件建立不读取派生正文的专用退役准入和持久清理回执，验证异常恢复、过期授权及其他作用域不受影响。保留策略、物理擦除、生产接线及发布闸门均未完成。
