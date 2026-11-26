# D2.3c3｜当前已提交原生清空的派生索引清理准入

基线 73cdb79，独立工作树 kaiwu-mem0-clear-admission-20261126。复用原生 memory_operations 已提交清空记录、权威快照及 Mem0 单写锁和完整作用域枚举/逐条清理。宿主显式 clear_committed 只接受当前本人、空闲、有效授权的空快照及与当前 snapshot_id/revision/native_epoch 一致的 owner clear 回执；旧操作、其他操作、已撤权状态均拒绝。操作不创建权威快照、不写入正文、不调用 delete_all、不自动重放。复用 sync 对每次删除前后的权威版本检查；正常结果必须通过再次完整枚举确认无残留，并重新核验当前清空回执。错误、拒绝或部分结果不视为完成。

验收：原生真实 SQLite/快照 + 合成 Mem0 后端新增专项 2/2，完整后端 57/57（42.91 秒，退出码 0），含错误回执、旧 clear、非 clear、其他作用域保留、假删除回执及停用用户拒绝。真实 Mem0 SDK probe 增加宿主 clear + 完整回读测试，使用合成授权 reader；11/11 通过，退出码 0；212 次回环 embedding、0 次 extraction。证据 /media/data3/dengkw/acceptance/kaiwu-mem0-clear-admission-20261126/pytest.log、pytest.exit、real-sdk.log、real-sdk.exit。

本切片未增加实际产品入口、撤权后读取/删除授权、持久清理回执、物理删除或远端取消保证。隔离验收完成，未上线。下一项单独处理撤权后删除专用准入及不读取正文的对账机制，先核验固定 SDK 是否可支持，不复用历史读取权限。生产发布闸门不变。
