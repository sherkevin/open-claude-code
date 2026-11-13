# 公开工业图片检索质量集（2026-11-07）

## 范围

本次只验证本地跨模态图片检索的最小闭环，不把三张图片包装成生产基准，也不把演示框标注当作独立人工金标准。质量集位于 `fixtures/industrial-public-v2/retrieval-quality.json`，包含三条英文文本查询，每条有一个正例、一个困难负例和一个普通负例。图片实际逐张查看过：一张 Wikimedia 工业机床照片、一张 NIST SP 800-82r3 的 SCADA 图、一张同一 NIST 文档的目录页。

来源、许可证、获取时间和文件哈希见 `fixtures/industrial-public-v2/source-catalog.json`。Wikimedia 图片为作者 Shixart1985 的 CC BY 2.0；NIST 图像为美国政府公开文档页面渲染，保留 NIST 来源和页码。质量集不含 Better AI、华为世纪或任何客户数据。

## 可复验命令

```bash
python3 scripts/evaluate_visual_public.py \
  --output /media/data3/dengkw/visual-public-eval-20261107.json
```

脚本只加载本地 `openai/clip-vit-base-patch32` 权重，禁止下载，使用 CPU 完成图片和文本向量计算。输出文件保存在 `/media/data3`，避免占用根分区。

## 实测结果

运行时间：2026-11-07 15:46 UTC；模型维度 512；设备 CPU；本地视觉模型和 RapidOCR 均报告可用。

| 查询 | 正例 | 排名（从高到低） | 正例余弦分数 | 正例排名 |
| --- | --- | --- | ---: | ---: |
| industrial machine tool cutting metal | Wikimedia 机床照片 | Wikimedia 0.29840319；SCADA 0.17672607；目录页 0.17172042 | 0.29840319 | 1 |
| SCADA control center with PLC/RTU/HMI | NIST SCADA 图 | SCADA 0.24535593；目录页 0.19561225；机床 0.19191478 | 0.24535593 | 1 |
| NIST SP 800-82r3 list of tables | NIST 目录页 | 目录页 0.32299282；SCADA 0.24982216；机床 0.22638542 | 0.32299282 | 1 |

汇总：3/3 命中第一名，平均倒数排名 1.0。该结果只说明当前三条查询在当前本地 CLIP 权重上排序正确；它不能外推到中文查询、工业缺陷识别、关系抽取、客户图片或生产吞吐量。

## 局限与下一步

- 三张图和三条查询规模很小，不能替代公开数据集上的完整基准；困难负例只有同一来源文档的目录页。
- `visual-entities.jsonl` 的框和标签状态为 `assistant_reviewed_demo`，不是第二位标注者独立复核的金标准；关系仍为 `evidence_only`。2026-11-10 复核发现 SCADA 旧框错位，已按后述记录更正；不能以当时的图片排序结果证明区域标注正确。
- 本次没有修改运行配置、没有重启服务、没有调用付费模型，也没有上传客户数据。
- 若要进入验收，需要补充经许可的工业缺陷/设备图片、多位标注者的框与标签一致性、中文及真实业务查询，并独立测量召回率、精确率、延迟和显存占用。

## 2026-11-10：SCADA 实体框更正

对当前 SHA-256 固定的 1020×1320 全页图重新查看后，确认旧版 `1.1.0` 的 Control Center、HMI、Engineering Workstation 和 Data Historian 框落在正文区域。例如旧 HMI 框为 `[339,278,82,52]`，实际组件框位于 `[338,501,82,52]`。该问题说明原有“像素检查”记录不足以支撑定位质量结论。

本次把 SCADA 的九个框全部重新对齐：四个控制中心区域、PLC、RTU 和三个 Field Site。仅该图片标注升级为 `1.2.0`，新增全页尺寸、`xywh_pixels_full_page` 坐标格式和修订历史；原图片/PDF 哈希、页码、实体 ID 和标签不变，其他两张图的标注不变。仍为助手演示标注，`independent_human_gold=false`，关系仍为空、`evidence_only`，不宣称独立金标准或分割能力。

新增回归固定这张公开图的已核对视觉锚点、父子框包含关系及图像边界。修前锚点测试失败，修后该文件四项测试通过（0.05 秒）；日志为 `.runtime/verification/scada-box-correction-20261110-230949/red.log` 与 `green.log`。这是来源/区域回归，不是模型定位分数。本轮没有重跑 CLIP、调用供应商模型或更改服务，前述 3/3 图片检索结果仍仅指 2026-11-07 的原批次。
