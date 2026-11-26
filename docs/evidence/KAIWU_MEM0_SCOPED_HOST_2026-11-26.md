# D2.4a｜独立作用域宿主 SDK 生命周期接线

基线 b6450df；独立工作树 kaiwu-mem0-scoped-host-20261126。复用 Mem0Host、ScopeStore、NativeMemorySourceReader 及原有单写锁，增加互斥的显式 scoped_factory 注入模式。仅明确 sync 可 provision；lookup 缺失或退役作用域不初始化 SDK，不自动同步。工厂路径由宿主权威作用域决定；持两个合作锁至同步 close 返回，关闭失败或权限在 close 中撤销时不交付命中。复用原 byte budget 与权威版本校验。作用域模式拒绝旧的仅向量 clear_committed，必须走独立退役准入。未接真实 API 或模型运行路径，默认关闭；未保证异常 close 后持久隔离、批次硬截止或提供方实际费用，也不声称 SDK 打开现有存储时零元数据写入。没有自动清理或重建退役路径。

真实宿主/SQLite + 合成 backend 后端组合 75/75（新增 3 项，56.53 秒）；真实固定 Mem0 2.2.1/Qdrant 1.20.0 + 同一宿主生命周期、合成 authority 的回环 probe 4/4，5 次 embedding、0 次 extraction，退出码均 0。SDK pilot 环境缺 cryptography，未声称完整真实 Service 和 SDK 同进程联调，不混装环境。证据 /media/data3/dengkw/acceptance/kaiwu-mem0-scoped-host-20261126/{full.log,full.exit,real-sdk.log,real-sdk.exit}。复现 scripts/oss/mem0_scoped_host_probe.py。

仍待：明确删除/撤权处置与独立授权事件、新代次身份、异常 close 的持久隔离、真实 Service/SDK 联调、配置与业务调用接线、质量/硬预算。无客户数据、外网 provider 或生产变更。下一步优先收敛处置策略和实际产品接线，不继续重复基础 SDK 试验。Remote Desktop Commander 提示月度使用 90%，data3 80G、根分区 104M；保留额度和受控发布闸门。
