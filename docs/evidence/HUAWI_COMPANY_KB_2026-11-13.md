# 华微公司知识库分支证据（2026-11-13）

## 1. 证据摘要

- **分支**：`oc-huawi-company-kb`（学校云工作树）。
- **知识库名称**：华微公司知识库。
- **来源范围**：经项目授权读取的华微项目群资料；原始归档仅留在学校云受控目录。
- **可提交 fixture**：`fixtures/huawi-company-kb-v1/`。
- **内容规模**：18 个纳入文档，2 个明确排除的非项目条目，2 个 PPTX 的 56 张页面预览。
- **图谱规模**：23 个人工整理实体、19 条关系种子；节点和关系写入文档、版本和字符偏移来源，其中未命中原始片段的种子明确回退到复核文档。
- **测试**：`server/tests/test_huawi_company_kb.py` 的清单脱敏、种子导入、管理员凭据边界、版本字段、BM25 命中和图谱质量断言已通过（3 项）；与既有 `test_knowledge_evidence.py` 合计 50 项通过。
- **可复现性**：使用同一原始归档和预生成页面预览重建 fixture 后，`manifest.json` 与人工图谱种子字节级一致。

## 2. 来源与脱敏证据

`manifest.json` 保存原始文件的相对来源、字节数和 SHA-256，并区分完整逐字稿、
智能纪要、方案文档和 PPTX。与华微项目无关的闲聊文档和 soundcore 录音被排除，
清单中记录排除原因。构建脚本会删除飞书内部链接、白板标记、只读块、群文件标记
和手机号码；检索正文只使用清洗后的本地副本。

原始归档目录 `huawi-source-materials/` 未加入 Git，也未被上传到腾讯云；它包含
内部协作资料，不能通过网页、引用链接或图谱接口直接暴露。可公开引用的入口是
知识服务产生的文档版本和文本偏移，不是原始飞书链接。

## 3. 图片和多模态边界

PPTX 的可见文字被提取到 Markdown，LibreOffice 和 `pdftoppm` 生成有界页面预览；
知识库 UI 和图谱 UI 可以使用受权限保护的缩略图、区域切片和页面来源。图谱中的
图片节点采用虚线连接到同一来源文档，便于从实体追到图片证据。

当前实现只保证本地缩略图、OCR 状态和证据链接，视觉语义向量明确为
`needs_model`。OCR、页面预览和区域切片不能被当作视觉 embedding，也不能据此声称
已完成图片实体识别。群聊中的三张图片附件未取得原始二进制，因此不伪造图片节点。

## 4. 图谱质量边界

`graph/curated_entities.json` 和 `graph/curated_edges.json` 是人工整理的种子，不是
自动规则共现结果。`graph-curated-review.md` 保存人工复核说明；没有找到原文的种子
会明确落到复核文档，并标记需要回到逐字稿核对。质量收据提供以下验收目标和当前可测字段：

- `core_entity_recall >= 0.90`（目标，当前为未测）；
- `candidate_noise_rate <= 0.10`（目标，当前为未测）；
- 规则抽取候选保持 `needs_review`，不进入已核验事实集合；
- 节点、关系都带来源文档、版本和字符偏移。

当前质量收据状态为 `curated_seed_not_evaluated`：记录人工种子规模、原文片段定位率
和未解析种子数量，但不会把未做标注集测量的召回率或噪声率写成 1.0/0.0。正式客户
验收前仍需标注集、抽样协议和独立评审。

## 5. 权限、版本和回退

知识库创建为团队资源但不自动绑定新助手。上传、检索、打开文档、打开图片缩略图和
图谱查询沿用知识服务的权限检查；版本哈希和版本号不会因重新索引而覆盖历史内容。
撤销或删除后，新检索和来源打开应停止；历史消息可以保留文字，但不能重新打开被
撤销的原始字节。

当视觉模型或 OCR 不可用时，服务保留文本、BM25 和页面预览回退，并通过状态字段
显示 `needs_ocr` 或 `needs_model`，不得把回退结果标成语义模型结果。

## 6. 本轮未做的事情

- 未修改共享看板代码、未重启共享服务、未部署到腾讯云。
- 未调用真实客户数据、未使用未授权付费模型、未将原始飞书资料上传外部服务。
- 未将该知识库绑定到生产助手，也未宣称图片视觉向量、多模态多跳检索或完整客户级
  图谱验收已经完成。

## 7. 复核入口

- 构建：`scripts/build_huawi_kb_fixture.py`
- 分支导入：`scripts/seed_huawi_company_kb.py`
- 分支测试：`server/tests/test_huawi_company_kb.py`
- 数据清单：`fixtures/huawi-company-kb-v1/manifest.json`
- 图谱复核：`fixtures/huawi-company-kb-v1/documents/graph-curated-review.md`
- 图谱种子：`fixtures/huawi-company-kb-v1/graph/curated_entities.json`、
  `fixtures/huawi-company-kb-v1/graph/curated_edges.json`
