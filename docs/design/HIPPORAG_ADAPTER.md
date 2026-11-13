# HippoRAG 官方实现的授权快照适配

状态：开发中的隔离切片，默认关闭；已完成受控开发环境的独立进程取消与可信复制恢复验证，不是生产启用、资源沙箱或论文质量复现。2026-11-10 更新。

## 1. 选择与边界

复用 MIT 官方仓库 [OSU-NLP-Group/HippoRAG](https://github.com/OSU-NLP-Group/HippoRAG/tree/d5c8329422e0a0b834a15874545cb6a74b4f9b26)，固定提交 `d5c8329422e0a0b834a15874545cb6a74b4f9b26`。本项目只实现来源、权限和产品接口适配，不复制官方 OpenIE、事实筛选、同义边或 PageRank。

旧方案先建立八个协议模块，容易只有结构没有真实算法。当前保留 `server/app/hipporag.py` 的薄来源适配器，以及 `KnowledgeService` 的显式注入点；新增 `server/app/hipporag_process.py` 只处理独立工作进程、取消和 JSON 通信，不增加另一套检索算法。官方依赖不加入默认服务环境；正常启动不导入官方包、不下载模型、不构建索引。

许可不能只看顶层仓库：官方图依赖 `igraph 0.11.8` 为 GPL-2.0-or-later，故不把整套依赖称为 MIT 或视为已通过商业分发审查。完整、未修改的 MIT 版权及许可原文已保存到 `server/LICENSE.HippoRAG`，与 `server/NOTICE.HippoRAG` 一同通过本地开发 wheel 的包含校验；这不表示官方依赖已打包或分发审查已完成。详见[恢复阶段证据](../evidence/HIPPORAG_RECOVERY_2026-11-10.md)。

服务默认继续使用混合检索。`hipporag` 模式默认仍是本地 `hipporag2-lite`；只有可信开发调用方注入已经构建的 `HippoRAGAdapter`，才尝试官方后端。没有对外启用接口、环境变量自动启用或生产切换。

## 2. 实际接口

```python
backend = HippoRAGAdapter(
    engine_factory=trusted_engine_factory,
    chunk_factory=official_Chunk,
)
backend.build(authorized_rows, identity=identity, index_dir=fresh_directory)
scores, stats = backend.search(query, rows=current_authorized_rows, limit=6)
# Explicit development opt-in; the creator owns backend.close().
service.knowledge.hipporag_backend = backend
```

上述同步接口用于合同测试或受控调用，不提供强制取消线程的能力。需要有界取消时，由可信宿主创建 `HippoRAGProcessAdapter(**trusted_worker_configuration)`，其 `build/search/seal/restore` 分别接受 `cancel_event` 与 `timeout_seconds`。解释器、模块目录、工厂及网络授权只来自宿主配置，不从用户请求或模型输出选取。

```python
worker = HippoRAGProcessAdapter(**trusted_worker_configuration)
worker.build(authorized_rows, identity=identity, index_dir=fresh_directory)
receipt = worker.seal()
# The trusted host persists this receipt outside the index directory.
recovered = HippoRAGProcessAdapter(**trusted_worker_configuration)
recovered.restore(authorized_rows, identity=identity,
                  source_dir=fresh_directory, receipt=receipt,
                  index_dir=another_fresh_directory)
service.knowledge.hipporag_backend = recovered
```

`recovered` 是显式创建的新适配实例；进程适配器在中断后也允许显式 `build/restore` 启动新工作进程，但不自动重开中断时的可变索引。宿主负责所有权和 `close()`，当前没有产品启动时自动恢复或运营配置入口。

`engine_factory(save_dir=...)` 必须由可信调用方显式提供已审查的官方引擎及模型配置。适配器本身不选择供应商、不读取环境凭据；真实验证脚本另行检查固定提交下 63 个 Python 文件逐字节一致及实际导入位置，不保证整个 Git 工作树干净。不可把任意工厂返回的测试替身称为真实官方算法。

身份字段为 `upstream_commit`，以及 `parser_digest`、`extraction_digest`、`embedding_digest`、`chunking_digest`、`acl_digest` 五个 SHA-256 指纹。这里记录模型、提示与处理配置的摘要，不包含密钥。调用方更换模型配置时必须新建身份与索引，不能复用旧对象。

## 3. 先隔离候选，再做图检索

官方 `retrieve()` 没有本项目的候选权限掩码。不能对跨用户共享图做查询后过滤：即使命中文档最终被过滤，越权实体仍可能影响图传播。因此索引必须严格匹配本次全部授权文本片段的集合及版本，少一项、多一项、正文或来源定位改变都拒绝调用官方检索。

知识服务先刷新账号、知识库可访问性与启用状态，再查询未删除、已完成解析的片段。运行内检索还校验任务归属、活动会话、助手权限交集和不可变运行快照。检索后再次检查权限及片段集合；检索期间删除、撤权、重新解析或无运行快照的当前版本变化，拒绝返回旧结果，而不是使用旧片段回退。运行快照中的旧版本仍可用，但不能读取之后导入的新版本。

纯图片可能没有文本片段，因此另查图片文档、版本、文件摘要、解析状态及授权范围；不能以“文本集合没有变化”替代图片证据验证。线程结果交回事件循环后，在数据库锁内再次校验，再登记引用。取消或撤权不会转成 lite 结果并登记来源。

## 4. 重复文本与证据映射

上游 `index()` 按文本内容散列合并片段，同文本的 `Chunk.metadata` 可能被覆盖。因此一个上游 `Chunk` 仅代表一种文本，本地维护这一文本对应的多个来源。

每条来源绑定：知识库、文档、文档版本、文件 SHA-256、片段 ID、正文摘要、页码/段落/字符位置以及本地标题。传入上游的元数据只携带快照摘要和分组标识。检索结果必须同时满足原文、分组元数据、快照、分数有效性和数量边界；任一未知或篡改结果使整个官方结果被拒绝。

映射展开后只返回本地片段 ID 和分数。标题、正文、原件链接、页面/图片预览和引用登记全部由当前知识服务重建，不信任上游提供的链接或文本。有限的零分、负分保留；同文本的多份文档在结果数量允许时均可展示。

上游 `graph_seeds` 是候选事实三元组，不是本项目已核验的节点标识或可点击路径。当前只公开有界统计，不将候选关系写入正式图谱，也不把它们标成已核验事实。

## 5. 索引信任与复制恢复

- `build` 的 `index_dir` 必须全新，已有目录或不可信路径拒绝；不自动重开他人提供的索引，因为上游有 pickle 文件。
- 先创建私有目录并完成官方索引，最后通过临时文件、`fsync` 和原子替换发布 `state=ready` 的清单。
- 保持索引目录绝对路径稳定，避免官方引擎后续写入已改名的旧路径。
- 查询前后重新校验来源身份、目录与清单；构建失败的目录不当成可查询缓存。
- `seal()` 必须成功调用引擎的严格关闭/刷新；关闭失败不给 receipt。进入封存后实例不可继续查询，即使封存最终失败也不能复用。
- 封存将本实例文件/目录规范为当前 UID 的 `0600/0700`，拒绝软链接、硬链接和特殊文件；逐文件散列生成规范 `seal.json`，原子发布后再校验。完整清单包含正文缓存、向量、图、原 ready 清单及空目录；`seal.json` 不递归列自身，由外部 receipt 的摘要单独认证。
- `restore(rows, identity, source_dir, receipt, index_dir)` 先校验全部来源与配置、源路径、owner、精确权限、文件内容与集合以及清单摘要，再流式复制到全新私有目录；副本再校验一遍，才构造官方引擎。绝不原地恢复，不调用 `.index()` 补建，也不把源目录交给官方引擎。
- `receipt` 只能由可信宿主在索引目录之外持有。严格八字段为 `schema/source_dir/owner_uid/identity_digest/snapshot_digest/tree_digest/file_count/total_bytes`，最大 8192 字节。不能从缓存自身、用户上传或模型响应取得 receipt 并视为可信；它不是签名、权限证明或自动防回滚服务。
- 当前限额：最多 10000 个文件（含封存清单）、20000 个目录项、目录深度 16；单文件 128000000 字节，总量 512000000 字节，封存清单最多 4000000 字节。超限拒绝，不截断清单放行。
- 校验失败、中断副本及未知目录不自动复用；旧授权的撤销与最新 receipt 的选择仍由宿主管理。没有在线增量更新或自动跨租户图共享。

逐层路径检查、no-follow 打开与校验后复制减少缓存替换风险，但不是抵抗同 UID 攻击者持续改名/篡改的操作系统沙箱。工厂、解释器、源码和私有父目录必须可信，不能声称任意不可信 pickle 已可安全加载。官方索引保存正文、向量和图；初次恢复使用合成文本，后续补验公开资料真实模型索引，两者均没有客户资料，详见恢复证据。

## 6. 独立进程与取消

每个进程适配实例独占一个 fresh Python 工作进程与进程组，使用 `-I -S -B`、显式代码目录、私有 HOME/tmp/cache、有界 JSON 请求/响应和白名单错误码。不继承供应商凭据，不执行 `.pth`；默认 `allow_network=False`。网络审计是 Python 层约束，不覆盖所有原生扩展，不等于 OS 网络或文件系统沙箱。

实现依赖 Linux/POSIX 进程组及 `waitid(WNOWAIT)`。先观察但不回收组长 PID，再对所属组 TERM/KILL，最后 wait 回收，避免先回收 PID 后误向复用进程组发信号；宿主不得用竞争的 SIGCHLD handler 回收该私有子进程。此版本不宣称 Windows 可用，也没有 cgroup 内存/CPU 配额。

锁等待、请求派发与 JSON 通信计入操作截止时间；等待者取消/超时在取得所有权前退出，不停止另一条正在执行的请求。只有已派发操作才能清理自己的工作进程；中断后状态失效，必须显式重建或从可信 receipt 恢复。终止/回收另有最多一秒的等待预算，输入验证、磁盘清理和系统调度不属于硬实时承诺。

服务层 `search_async` 为每个请求生成取消信号；浏览器断开、任务取消及权限撤销会传给支持取消的后端。协程取消时最多等待线程交接 5 秒，**不是强制杀死 Python 线程**，也不能把超时返回宣称为后台资源必定清理完毕。真正进程回收由进程适配器执行并另有真实进程测试；同步/非协作后端不能获得相同保证。watcher 清理使用 `gather(return_exceptions=True)`，保留清理期间新到达的调用方取消。

## 7. 回退与界面

成功时 `retrieval.algorithm=hipporag2-official`、`backend=official`。官方排名直接映射到本地证据卡，不添加本地图分数、相邻片段或视觉召回来冒充官方结果。

失败或没有命中时保留现有 `hipporag2-lite`，`backend=lite`，并通过受限的 `retrieval.fallback` 说明原因：未配置、未就绪、输入超限、身份不符、索引不可信、结果无来源、检索失败，或进程 `timeout/worker_failed/worker_protocol`。`cancelled` 不进入回退；已撤权或版本变化也不会回退到旧来源。异常原文、模型响应、路径和凭据不进入接口。界面分别显示官方与本地算法，并明确提示回退；官方统计不伪造节点、边或完整图路径。

默认的混合、BM25、向量检索不改变。开发调用方显式注入后，聊天知识工具也使用同一官方尝试及来源登记链路；没有注入时保持原来的混合检索。

## 8. 验证与未完成项

- 合同测试：`server/tests/test_hipporag_adapter.py`，用测试引擎覆盖映射、不可变身份、结果校验和缓存信任。
- 进程测试：`server/tests/test_hipporag_process.py`，用真实子进程与测试工厂验证超时、活跃/等待者取消、组内后代清理、协议与恢复边界。
- 服务测试：`server/tests/test_hipporag_official_search.py`，使用真实知识服务与适配器、测试检索引擎，覆盖权限、版本、引用、撤销和可见回退；不将该测试引擎计为官方算法验证。
- 取消集成：`server/tests/test_hipporag_cancellation.py`，验证服务取消信号、无引用登记、撤权和 watcher 清理竞态；不将协作测试后端冒充真实进程清理证据。
- 官方测试：`scripts/verify_hipporag_official.py` 与对应测试，只在独立环境运行；必须使用真实上游图实现，替身只能位于模型接口。
- 恢复验证：`scripts/verify_hipporag_recovery.py` 已通过真实官方算法的构建、封存、复制恢复、模型接口挂起后的超时/取消、显式再恢复及构造前篡改拒绝。
- 初始阶段历史证据见 `docs/evidence/HIPPORAG_OFFICIAL_2026-11-10.md`；该阶段“没有跨实例恢复/进程取消”的描述只对应当时版本。当前证据以 `docs/evidence/HIPPORAG_RECOVERY_2026-11-10.md` 为准，不能把两阶段重复测试数相加。

仍缺少 OS 级资源/租户隔离、生产自动恢复与运营配置入口、大规模独立公开工业标注与质量评价、有效的多模态融合及客户样例验收。2026-11-11 已完成 E5 权重核验、公开资料真实模型诊断、实际索引可信恢复与用量记录，旧“下载中/尚未调用”不再是当前状态；但关系误抽、六个图片定位目标零命中和融合退步尚未解决，供应商最终账单与生产负载也未验收。实际结果见[公开质量诊断](../evidence/HIPPORAG_PUBLIC_2026-11-10.md)，完成条件统一见[主需求第 7.2 节](../REQUIREMENTS_ALIGNMENT.md)。完成开发恢复不等于完整 HippoRAG、多模态图谱或产品已交付，默认官方后端仍关闭。
