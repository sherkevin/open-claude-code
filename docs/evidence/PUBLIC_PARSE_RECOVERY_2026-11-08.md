# 公开工业样例解析恢复证据（2026-11-08）

## 范围与边界

本次只重新读取仓库中已经存在的 `fixtures/industrial-public-v1/` 和
`fixtures/industrial-public-v2/`。检查使用本地 `pypdfium2`、RapidOCR 和
Pillow，不下载新文件、不调用外部模型、不使用公司或客户数据。每个文件的
本地哈希、来源网址、许可、页数、非空页数、字符数和解析状态都写入同目录的
`public-parse-recovery-2026-11-08.json`。

“解析成功”这里只表示能得到有界的页面文本或能确认图片可读，**不表示实体、
关系或语义质量已经通过验收**。图片没有文字时明确标为
`visual_only_ocr_empty`，它可以作为图片和视觉向量的候选资产，但不能进入文本
检索，也不能被包装成 OCR 实体。

## 重试结果

| 样例 | 之前运行时状态/原因 | 本地重试状态 | 页面/非空页 | 字符数 | 处理结论 |
| --- | --- | --- | ---: | ---: | --- |
| `nist-sp-800-82r3.pdf` | `failed`：旧运行时上限为 200 页 | `parsed` | 316/316 | 730,367 | 已确认可解析；重新索引需要运行时加载当前 400 页上限 |
| `smart-manufacturing-standards-2024.pdf` | `needs_ocr`：扫描页未安装本地 OCR | `parsed_with_local_ocr` | 36/36 | 15,786 | RapidOCR 已解析 1 页扫描内容，其余页面为 PDF 文本 |
| `wikimedia-industrial-machine-960.jpg` | `failed`：OCR 无可检索文字 | `visual_only_ocr_empty` | 1/0 | 0 | JPEG 可读（960×640），保留为视觉资产；不进入文本 RAG |
| `nist-sp80082-page15.png` | 之前已为可解析图片 | `parsed_with_local_ocr` | 1/1 | 1,524 | OCR 文本可检索，图片仍保留来源页绑定 |
| `nist-sp80082-scada-fig2.png` | 之前已为可解析图片 | `parsed_with_local_ocr` | 1/1 | 1,564 | OCR 文本可检索，图片仍保留来源页绑定 |

其余三个 NIST/OSHA 文件也完成了完整重试：`nist-ir-8183.pdf`（57/57 页，
121,061 字符）、`nist-ir-8219.pdf`（93/93 页，152,600 字符）、
`nist-ir-8227.pdf`（41/41 页，78,110 字符）和 `osha-1910-212.md`
（1/1 页，701 字符）均为 `parsed`。所有 9 个公开样例的哈希均与来源清单
一致，检查报告的 `accepted_for_demo` 为 `true`。

## 运行时落差

本证据是在不重启开发服务、不触碰生产的条件下生成的；因此已有运行时数据库
中的旧版本状态不会自动改变。当前仍需在开发环境执行一次受控重新索引：

1. 用当前解析器重新索引 316 页的 NIST SP 800-82r3；
2. 重新索引中文智能制造标准 PDF，确认 `needs_ocr` 消失；
3. 重新索引 Wikimedia 图片，确认状态是“可视资产 + OCR 为空”，而不是把它
   误报为文本解析成功；
4. 重新检查图片的缩略图、放大查看和实体到图片区域的跳转来源绑定。

开发运行时中名为 `quality-inspection-synthetic.png` 的文件属于合成测试夹具，
不是本次公开工业样例；本次没有改动它，也没有把它混入公开数据验收。

## 可复现命令

```bash
PYTHONPATH=/tmp/open-claude-pylib:server \
  python3 scripts/verify_public_corpus_parsing.py \
  --output docs/evidence/public-parse-recovery-2026-11-08.json
```

严格模式会在出现真正的 `failed`、`needs_ocr` 或哈希不匹配时返回非零；
`visual_only_ocr_empty` 是有意保留的、可读但无文字的图片状态：

```bash
PYTHONPATH=/tmp/open-claude-pylib:server \
  python3 scripts/verify_public_corpus_parsing.py --strict
```

## 未覆盖的验收

- 本地 OCR 的文字框位置和实体标签仍需独立人工金标准，不把当前演示标注当作
  独立人工真值；
- 视觉模型实体和关系抽取没有在本次脚本中调用，不能据此宣称语义图谱质量；
- 公开资料只能用于本地演示和回归，正式制造业务仍需客户脱敏样例、许可和安全
  工程复核。
