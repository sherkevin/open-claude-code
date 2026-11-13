# HippoRAG2-lite 本地增量证据（2026-11-09）

## 上游代码核验（2026-11-10）

- 官方开源实现存在且仍在维护：[`OSU-NLP-Group/HippoRAG`](https://github.com/OSU-NLP-Group/HippoRAG)，许可证为 MIT；主分支项目版本为 `2.0.0a5`。
- 上游源码包含完整的 OpenIE、图传播检索、评测和可选模型上下文协议服务。2026-11-10 续接时已取得数据盘忽略目录 `.runtime/third_party/HippoRAG` 的固定源码副本，提交为 `d5c8329422e0a0b834a15874545cb6a74b4f9b26`；尚未安装 `hipporag` 包或接入官方运行后端。
- 因此本项目当前的 `hipporag2-lite` 仍应标记为本地适配切片；复用上游代码前还需完成依赖、模型端点、索引身份、权限和版本快照适配验证。

## 已验证内容

- `server/app/knowledge.py` 增加可选 `mode=hipporag`，默认 `hybrid` 不变。
- 查询实体在版本和权限边界内生成图种子；最多两跳、最多 8 次 PageRank 迭代，结果投影回原始文本段落。
- 响应保留 `content_url`、版本、页码和现有来源登记；无图、无种子或无段落证据时返回明确 `graph_trace.fallback`。
- 前端知识库检索增加“HippoRAG2-lite（图扩散）”选项，并展示算法、种子和回退原因；没有追踪字段时不伪造。

## 离线测试

- 服务端聚焦回归：`tests/test_knowledge_evidence.py`、`tests/test_multimodal_graph_quality.py`、`tests/test_hipporag_retrieval.py` 共 `57 passed`，另有 1 条既有 Starlette/httpx 弃用警告。
- `server/tests/test_hipporag_retrieval.py`：3 项通过，覆盖确定性排序、两跳图分数、无种子回退、权限、运行快照和来源登记。
- `client/src/knowledgeSearch.test.ts`：8 项通过，覆盖检索模式、追踪摘要和证据链接。
- 客户端 `npm run build`：TypeScript 与 Vite 构建通过；仅保留既有大 chunk（超过 500 kB）提示。
- 这些测试使用临时 SQLite、合成工业文本和本地词法向量；没有网络、生产数据、真实客户资料或付费模型。

## 不能据此宣称的内容

当前不是完整 HippoRAG 2 论文复现：尚未接入授权 OpenIE、同义词向量边、query-to-triple、语言模型 recognition filter、真实多模态实体融合，也没有在论文数据集或客户样例上测 Recall、F1、成本和延迟。相关需求仍为“部分实现”，后续必须按设计文档逐阶段验收。
