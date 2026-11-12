# OC-T4 知识库完整摄取、版本化和失败重试：验收证据（2026-11-12）

## 结论

本任务在隔离 worktree `worktree-oc-t4-knowledge-ingest` 内完成，未部署、未重启线上服务，未读取真实客户资料，未调用付费模型。

- C1：新增 `server/tests/test_knowledge_ingest.py`，在本 worktree 的 `server` 目录执行 `pytest -q tests/test_knowledge_ingest.py`，退出码 0，22 项测试通过。
- C2：本报告记录 8 种格式样例（Markdown、TXT、CSV、JSON、DOCX、XLSX、PDF、PNG）、不可变版本、瞬时失败重试、重试耗尽、取消、崩溃恢复和重开索引；产品内示例为 `examples/knowledge_ingest_demo.py`，只使用仓库内 `fixtures/synthetic-industrial-v1`。
- C3：客户端 `KnowledgePage` 已能显示排队、解析、OCR、分块、索引、重试等待、失败、取消状态和尝试次数；新增 4 个前端状态测试，Vitest 全套 17 个文件 113 项测试通过，`tsc -b` 退出码 0。

## 安全边界与数据来源

- 只使用仓库已有的合成工业夹具 `fixtures/synthetic-industrial-v1`（`synthetic=true`，仅开发与验收）以及测试运行时生成的最小 PDF、DOCX、XLSX、PNG 字节夹具。
- 没有读取、上传或索引任何真实客户资料；没有使用真实凭据；没有调用未授权付费模型或外部 OCR/嵌入服务。
- 图片 OCR 测试使用确定性替身返回固定文本，不把目录图、装饰图或像素猜测当作有效工业图片。未安装 RapidOCR 时，图片明确进入 `needs_ocr`，检索为空。
- 所有命令都在 worktree 内执行；上传大小、页数、文本和分块上限全部由服务端强制执行。

## 统一摄取流程

上传、上传新版本和重建索引都写入同一张 `knowledge_ingest_jobs` 任务账本，阶段为：

`queued → prepare → parse →（图片：ocr）→ chunk → embed → graph → done`

任务记录 `status`（queued/running/succeeded/failed/cancelled）、`stage`、`attempts/max_attempts`、`error_code/error_message`、`events`、`cancel_requested`、租约和 `available_at`。`GET /knowledge-documents/{id}/ingestion` 返回当前任务、阶段中文标签、尝试次数、是否可重试/可取消和最近 20 条事件；上传接口带 `?background=true` 时立即返回排队任务，由应用启动的后台工作线程执行。

版本保持不可变：每次上传新版本新增一条 `knowledge_versions` 记录（sha256、原始文件、MIME），旧版本原文和分块保留。`last_ready_version` 让客户端在新版本处理中或失败时仍知道哪个旧版本可检索。删除文档会取消未完成任务并移除检索来源访问。

## 多格式样例与结果

`server/tests/test_knowledge_ingest.py::test_multi_format_ingestion_records_version_stages_and_evidence` 对以下 8 个合成样例逐一断言：状态 `ready`、版本 1、sha256 匹配、页码至少 1、分块至少 1、指定检索词可命中、任务 `succeeded` 且阶段事件完整。

| 样例 | 格式 | 提取路径 | 断言检索词 |
| --- | --- | --- | --- |
| `guide.md` | Markdown | 文本 | 压力 |
| `notes.txt` | 纯文本 | 文本 | P-101 |
| `batches.csv` | CSV | 文本 | P-101 |
| `record.json` | JSON | 文本 | P-101 |
| `manual.docx` | DOCX | `docx-paragraphs` | P-101 |
| `quality.xlsx` | XLSX | `xlsx-cells`，保留工作表来源 | 7.2 |
| `spec.pdf` | PDF | `pdf-text`，保留页码 | P-101 |
| `nameplate.png` | PNG | `local-ocr`（测试替身） | P-101 |

长文本测试使用 6,000 余字符并夹带首尾唯一标记，断言至少 5 个分块、每块不超过 1,100 字符、`text[start:end]` 与原文完全一致，且首尾标记都能检索到，证明没有静默丢字。

## 版本化证据

`test_version_upgrade_keeps_previous_version_and_both_jobs` 上传 `limits.md` 的 v1（7 bar）与 v2（9 bar）：

- v2 `status=ready`，`last_ready_version=2`，任务 `succeeded`；
- v1 的分块与上传时完全一致，仍含 7 bar；当前版本分块含 9 bar；
- 两个版本各有一条成功任务记录：`[(1, succeeded, 1), (2, succeeded, 1)]`；
- 图谱节点只引用 v2 来源。

## 失败、重试、取消与恢复

### 有界重试

- 瞬时故障（`PARSER_UNAVAILABLE`、`EMBEDDING_UNAVAILABLE`、`INDEX_UNAVAILABLE`）按 `retry_delay × 2^(attempt-1)` 退避，默认最多 3 次。
- 测试注入解析器连续两次 503、第三次成功：任务 `succeeded`、`attempts=3`、2 条 `retry_scheduled` 事件，最终可检索。
- 解析器持续 503：任务 `failed`、`error_code=PARSER_UNAVAILABLE`、`attempts=3`、`can_retry=true`；文档 `chunk_count=0`，数据库无分块，检索为空，绝不生成空的就绪索引。

### 确定性失败不重试

- 损坏 PDF 返回 `INVALID_DOCUMENT`，`attempts=1`，解析器只被调用一次。
- 空内容返回 `EMPTY_DOCUMENT`；不支持的类型在创建版本前拒绝。
- 图片缺少本地 OCR 返回 `OCR_REQUIRED`，文档状态 `needs_ocr`、图片 `ocr_status=needs_ocr`、分块为零、检索为空。修复 OCR 后重建索引成功，任务历史为 `failed → succeeded`，`last_ready_version=1`。

### 取消

- 排队任务取消：任务与版本均变为 `cancelled`，无分块、无检索结果；再次执行 `process_pending()` 不会复活；随后重建索引成功并可检索。
- 运行中取消：测试解析器在每个页面循环检查取消回调，取消后版本变为 `cancelled`，分块为零。

### 崩溃恢复

- 用过期租约模拟进程中断：`recover()` 将任务重新排队并写入 `requeued` 事件，保留已用尝试次数；继续执行后 `succeeded`、`attempts=2`，索引可用。
- 尝试次数已耗尽的过期租约：`recover()` 明确置为 `failed`、`error_code=LEASE_EXPIRED`，版本状态 `failed`，不会无限重试。

## 超量输入明确拒绝

- 2,000,001 字符文本：`DOCUMENT_TOO_LARGE`、`attempts=1`、`chunk_count=0`、无分块、检索为空，返回的限额说明包含“超出单文件、页数、文本或片段上限时明确拒绝，不静默截断。”
- 自定义 1,024 字节上传上限上传 2,048 字节：返回 HTTP 413 `DOCUMENT_TOO_LARGE`，且在创建文档/版本前拒绝，数据库中文档数为 0。
- 把 `MAX_CHUNKS` 临时收紧到 2 后上传可产生 4 个分块的文本：任务 `failed`、`DOCUMENT_TOO_LARGE`、`attempts=1`、无分块、检索为空，证明片段上限同样明确拒绝而不是截断。
- 服务端统一限额：20 MB/文件、200 页、2,000,000 字符、5,000 分块；分块步长 900 字符、窗口 1,100 字符。

## 产品内公开工业示例

`examples/knowledge_ingest_demo.py` 使用仓库内合成工业夹具，通过真实 `KnowledgeService` 完成上传、排队、执行和检索，不部署服务、不联网、不调用模型。实际运行输出摘要：

```text
limits: max_bytes=20971520 max_pages=200 max_text_characters=2000000 max_chunks=5000 max_attempts=3
quality-policy.md: v1 ready pages=1 chunks=5 sha256=6ab0f675... job=succeeded attempts=1
  stages: stage:prepare -> stage:parse -> stage:chunk -> stage:embed -> stage:graph -> stage:done
quality-batches.csv: v1 ready pages=1 chunks=1 sha256=ab876702... job=succeeded attempts=1
  stages: stage:prepare -> stage:parse -> stage:chunk -> stage:embed -> stage:graph -> stage:done
hit: v1 page=None score=0.032787 对同一产线和日期范围内的多个批次，先汇总 qualified_count 与 inspected_count，再计算加权合格率
```

注意：该示例的 Markdown/CSV 属于文本资料，`page=None` 是真实无物理页码，不伪造页码。图谱只产生有来源定位的候选节点/边；未启用视觉语义模型时 `visual_embedding.status=needs_model`。

## 客户端可见性（C3）

`client/src/KnowledgePage.tsx` 新增并在测试中固定以下行为：

- 文档行显示摄取任务状态：排队等待解析、解析文本/OCR/分块/建立索引（含第 N/M 次尝试）、等待重试、摄取完成、摄取失败（错误码 + 可重试/需修正输入）、摄取已取消；
- 显示任务错误原因和最近可用版本 `vN`；
- 对 `queued`、`running`、`retry_wait` 任务每 2.5 秒轮询文档列表；
- 上传、上传新版本、重新解析使用 `?background=true`，避免请求长时间阻塞；
- 服务端判定 `can_cancel` 时显示“取消摄取”，`knowledgeDocumentNeedsReindex` 为真时显示“重新解析”。

前端测试命令与结果：

```sh
cd client
./node_modules/.bin/vitest run src/knowledgeSearch.test.ts
# 8 passed
./node_modules/.bin/vitest run
# 17 files, 113 tests passed
./node_modules/.bin/tsc -b
# exit 0
```

新增 4 个测试覆盖：阶段与尝试次数文案、瞬时重试与确定性拒绝区分、轮询/重试/取消条件、错误原因优先级。

## 可重复命令与结果

### 服务端验收（C1）

```sh
cd server
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest -q tests/test_knowledge_ingest.py -p no:cacheprovider
```

结果：`22 passed, 1 warning in 96.31s`，退出码 0。唯一 warning 是 TestClient 提示 `httpx2` 弃用，不影响测试；此前同 worktree 空载运行 21 项记录为 50.59 秒。测试使用模块级临时目录和合成夹具，运行结束后不残留线上数据。

### 产品内示例

```sh
AGENT_KNOWLEDGE_EMBEDDING_MODEL=local-lexical-v1 \
  /media/data3/dengkw/projects/open-claude-code/.venv/bin/python examples/knowledge_ingest_demo.py
```

结果：退出码 0；两个样例均 `ready`，阶段事件完整，检索命中；数据目录打印在最后一次输出行。

### 前端

```sh
cd client
./node_modules/.bin/vitest run src/knowledgeSearch.test.ts
./node_modules/.bin/tsc -b
```

结果：8 项测试通过，类型检查退出码 0。worktree 内 `node_modules` 从主检出目录复制，不提交。

## 已知限制和未覆盖的外部证据

- 未部署、未重启任何环境；未执行浏览器端到端或截图验收。C3 证据为组件状态逻辑的 Vitest 测试和 TypeScript 类型检查，不是线上界面截图。
- RapidOCR 和 BGE-M3 语义向量未在本次隔离验收中启用：图片 OCR 使用确定性测试替身，向量为 `local-lexical-v1` 词法回退，检索元数据明确 `semantic=false`。
- 未启用视觉模型；不宣称图片像素的实体或语义关系，图片只登记尺寸、缩略图、安全网格区域和 OCR 状态。
- 图谱只验证有来源定位的候选节点/边和版本一致性，不声明语义关系质量合格。
- 旧回归文件 `server/tests/test_knowledge_evidence.py` 的完整重跑结果见下方“回归”小节；若因机器并发资源紧张超时，会明确记录而不是省略。

## 回归

（待完整回归运行结束后补充；已单独通过前 5 项兼容性测试：授权、原文版本与历史引用、BM25/混合检索、公式相邻来源、跨会话伪造来源，5 passed。）

## 变更文件

- `server/app/knowledge.py`：摄取任务账本、阶段、重试、取消、恢复、限额、接口视图。
- `server/app/main.py`：应用生命周期启动/停止后台摄取工作线程。
- `server/tests/test_knowledge_ingest.py`：22 项服务端验收测试。
- `client/src/types.ts`、`client/src/KnowledgePage.tsx`、`client/src/knowledgeSearch.test.ts`：摄取状态、轮询、取消、重试与测试。
- `examples/knowledge_ingest_demo.py`：合成工业夹具的产品内示例。
- `docs/API_CONTRACT.md`、`docs/KNOWLEDGE_AND_SOURCES.md`：接口字段、阶段、限额、重试/取消/恢复说明。
- 本证据报告。
