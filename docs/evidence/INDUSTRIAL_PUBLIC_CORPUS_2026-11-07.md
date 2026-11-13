# 工业公开知识库样例证据（2026-11-07）

## 范围

`fixtures/industrial-public-v1/` 是用于本地开发和验收的公开资料样例，不含公司内部或客户数据。资料来自美国国家标准与技术研究院（NIST）、美国职业安全与健康管理局（OSHA）和中国政府网的官方公开页面；每份下载文件在 `source-catalog.json` 中记录了来源网址、落地页、发布日期、许可边界、检索日期和 SHA-256。

已下载四份 NIST 公共 PDF、一份 OSHA 结构化摘要和一份中文智能制造标准体系指南，合计约 32 MB。另有六份候选资料仅登记在 `catalog_only`，没有在未审查前自动抓取。

## 实体验收

`knowledge.py` 的图谱构建使用固定规则识别标准号、设备标识、工艺、材料、质量指标和组织，节点带文档版本、页码、段落和字符偏移。无法匹配的词保留为普通术语，边仍为同段共现。没有获授权的视觉或关系模型时，不宣称语义关系、实体链接或图片实体完成。

## 本地证据

```text
PYTHONPATH="$PWD:/tmp/open-claude-pylib" pytest -q \
  server/tests/test_public_corpus.py \
  server/tests/test_knowledge_evidence.py::test_knowledge_graph_extracts_typed_entities_with_offsets \
  server/tests/test_knowledge_evidence.py::test_public_osha_fixture_entities_are_source_bound \
  --basetemp=/media/data3/dengkw/public-corpus-test -p no:cacheprovider
```

结果：`4 passed`（其中知识库测试包含一个既有 Starlette 弃用警告）。完整回归由项目负责人在合并其他并行改动后执行。

## 仍需外部证据

- 客户脱敏制造业文件和图片样例，以及客户助手对标答案；
- 获授权的视觉向量、版面/实体识别和关系抽取模型；
- 对标准和法规的客户辖区适用性审查。公开资料只能作为示例，不能替代法务或安全工程审查。
