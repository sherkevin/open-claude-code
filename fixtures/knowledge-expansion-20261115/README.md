# 公开工业知识补充包

本包回应 2026-11-15 最新反馈中“知识库数量和资料太少”的独立项。新增 3 个主题、8 份可独立检索的中文参考资料，主题为设备维护、统计质量和 OT 安全。不靠拆分同一文档或重复现有大型 PDF 增加数量。

## 内容与来源

| 主题 | 文档 | 官方来源 |
| --- | --- | --- |
| 设备维护与运维管理 | 维护策略；运维数据转行动 | PNNL、美国能源部 FEMP |
| 质量测量与统计过程 | 控制图；Cp/Cpk；测量过程控制 | NIST/SEMATECH 工程统计手册 |
| 工业网络与运行韧性 | OT 资产清单；连接与远程维护；事件响应管理 | ASD ACSC 联合指南、NIST SP 800-61r3 |

逐份原文链接、核对日期、内容 SHA-256、字节数和 16 个检索问题在 `manifest.json`。文档是有限篇幅的独立中文整理；建议字段与来源结论分别表述，未分发完整版权原文或图片。原文未归档，因此没有冒称原文快照或原文哈希。每份文档保留适用边界，不能当作企业真实制度、合同、业绩或法定合规意见。

准备时只读腾讯知识元数据得到 6 个有效知识库、47 份当前文档、621 页、2049 个片段；全部文档状态为 ready。数据库没有独立“分类”字段，主题按知识库区分。既有华微资料包有 18 个候选文件，但 manifest 明确标注 `school_cloud_only=true`，因此未复制或导入腾讯包；也未把“本地可用”解释为“可跨环境发布”。现有长篇 NIST/OSHA PDF 不重复打包。

## 离线核验与交付

在仓库根目录运行（Python 3.11+；规划仅用标准库）：

```sh
python scripts/expand_knowledge_feedback.py --report /absolute/review/plan.json --bundle /absolute/review/knowledge-expansion.zip
```

可附 `--inventory /absolute/review/sanitized-inventory.json` 对照元数据。默认只校验包、生成计划及白名单 ZIP，不读取密码、不启动 Service、不连数据库或 HTTP、不调用模型。报告、ZIP 和日志都拒绝覆盖旧文件。ZIP 只含 manifest、本 README 和 8 份文档，不含本机 data、学校云资料或凭据。

针对回归：`python -m pytest server/tests/test_knowledge_expansion_feedback.py -q`（仓库环境已配置 server 导入路径）。测试使用临时本地产品数据库及进程内 API，阻断外部 HTTP，覆盖内容校验、真实本地入库、来源保留、去重、不确定上传后核对及失败内容拒绝复制。8 组主题关键词的 BM25 首条结果属于预期文档；16 个自然语言问题检查前 10 个片段中能召回对应文档。这是有限语料下的检索基线，不是模型回答正确率或全库排名评估。

## 供发布操作者审阅的导入入口

本轮没有执行生产导入。完成审阅并决定目标 API 后，操作者可运行下列入口；所有占位值必须明确填写，不能直接照抄：

```sh
python scripts/expand_knowledge_feedback.py --apply --expected-manifest-sha256 REVIEWED_PLAN_SHA256 --base-url https://APP_HOST/APP_PREFIX/api/v1/ --admin-user EXISTING_ADMIN --password-file /private/admin-password --journal /private/new-import.jsonl --report /private/new-receipt.json
```

脚本使用现有管理员认证与产品上传 API；不创建账号、不重置密码、不读取数据库。密码文件不打包、不输出，Unix 下要求 0600。只接受 HTTPS，拒绝重定向。由同一操作者串行导入，避免并发创建同名知识库；脚本不宣称数据库级并发去重保证。

按主题精确匹配已有团队知识库，名称歧义或可见性/状态不兼容时停止。新库启用，但不自动绑定任何助手，也不设置为新助手默认库。导入后由产品管理员明确选择哪些助手使用这些公开资料。

仅追加缺失的 SHA-256 内容；已 ready 的相同内容跳过，已有失败内容要求检查或重新索引，不复制一份掩盖问题。每次写操作前写入并 fsync 意图日志；请求结果不明即停止，不重发。需要继续时，先核对日志与目标，使用新日志路径，入口会重新发现已存在的哈希。已成功部分保留，脚本不会自动删除或回滚知识数据。

本包只有 Markdown 文本，当前产品通过本地解析/索引处理，脚本不请求付费模型、OCR、视觉或外部抓取；生产操作者仍需确认服务器本地索引依赖可用。导入回执表示写入结果，不表示真实模型问答、生产上线或用户验收已经完成。
