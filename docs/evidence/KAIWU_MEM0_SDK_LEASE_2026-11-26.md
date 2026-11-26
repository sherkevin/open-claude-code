# D2.4b｜独立作用域 SDK 打开/关闭持久回执

基线 05ade36；独立工作树 kaiwu-mem0-sdk-lease-20261126。在原 ScopeStore 合作互斥和持久身份标记中，SDK 初始化前原子写入 sdk_open，只有确认实际同步 close 返回（或预检失败且从未调用工厂）才恢复 active。工厂、close、回执不明确或进程异常退出后，独立作用域保持拒绝重开、同步和退役；其他作用域可继续使用。不读正文、不删除、不自动修复、不重放。若宿主存储自身故障，不能声称物理持久性或文件系统灾难恢复。

后端 77/77（60.60 秒），真实 Mem0 2.2.1 / Qdrant 1.20.0 回环合成 provider 5/5，均退出 0。新增真实独立子进程在未确认 SDK lease 时退出 17，重新打开拒绝；真实 SDK close 实际返回后模拟回执丢失，新的宿主拒绝再次初始化该作用域，独立作用域仍可查询。8 次合成 embedding、0 次 extraction。日志：/media/data3/dengkw/acceptance/kaiwu-mem0-sdk-lease-20261126/{full.log,full.exit,real-sdk.log,real-sdk.exit}。无生产或客户数据，不是物理清理和正式 API/运行接线。

下一项优先收敛真实 Service + SDK 同进程隔离验收，并评估默认关闭的宿主维护 API / 实际 run 检索接线；不得把 SDK 合成回环当作真实供应商授权/预算/质量验收。删除专用授权、新代次及不明确资源处置仍待做，不自动清除 sdk_open 状态。生产闸门不变。
