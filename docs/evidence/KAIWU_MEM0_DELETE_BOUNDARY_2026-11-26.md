# D2.3c4a｜Mem0 固定 SDK 删除语义与正文残留

基线 8d111c8；独立工作树 kaiwu-mem0-delete-boundary-20261126。仅用现有固定 Mem0 2.2.1、Qdrant client 1.20.0、回环合成 provider 和 data3 临时库。未接真实客户数据、未增加产品调用或撤权后删除权限。

源码核对：Mem0 memory/main.py get_all（1269）、_get_all_from_vector_store（1340）固定返回 memory 正文；vector_stores/qdrant.py list（573）使用 with_payload=True。delete（1883）、_delete_memory（2114）取完整旧记录，将 payload.data 写入 SQLite DELETE 事件的 old_memory；memory/storage.py add_history（150）保留 old_memory/new_memory，get_history（227）可读回历史。delete_all 同样调用 _delete_memory，并非无正文删除接口。既有 clear_committed 的完整枚举只能确认向量索引无残留，不代表历史正文已消失，也不代表物理擦除。

新增独立可复现脚本 scripts/oss/mem0_delete_boundary_probe.py：真实 SDK 合成数据 5/5，退出码 0；官方 Qdrant scroll 支持按三重主体过滤并只投影归属字段（不返回正文或向量）；Mem0 ADD 历史保留正文；SDK delete 删除向量却把旧正文复制到 DELETE 历史；另一用户记录保留；关闭并重开后 SQLite 仍有被删除正文。2 次回环 embedding、0 次 extraction，删除阶段零新增模型调用。证据 /media/data3/dengkw/acceptance/kaiwu-mem0-delete-boundary-20261126/real-sdk.log、real-sdk.exit。脚本内读取的历史均为明确合成数据，不是撤权后的生产读取。

结论：不能用 SDK delete/delete_all 充当撤权后的无正文清理；也不能用 Qdrant 原始删除假装 SQLite 历史已清理。现阶段不使用未经授权的底层删除，不把现有清空回执当成物理删除证明。正式接线前须明确派生缓存和历史的隔离与保留策略。优先验证按宿主权威作用域独立存放 Mem0 本地 Qdrant 和 history DB 的可行性，使独立删除准入可以不读取旧正文，并可覆盖向量已删而历史仍在、写入回执丢失、重启恢复、其他作用域保留；若复用共享库则必须先有持久化归属登记及可核验的无正文清理能力。不得访问生产数据，不因本次发现自动清除任何已有记录。
