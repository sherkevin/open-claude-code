# 工业多模态公开样例（industrial-public-v2）

这是一个用于本地开发、图谱展示和回归验收的公开工业多模态样例集。它不包含华为世纪、Better AI 或任何客户数据，也不声称代表客户工厂流程。样例采用来源白名单、许可边界、原始文件哈希和代理复核记录；生产使用前仍需由法务和数据负责人复核许可。

## 本地样例

- `images/wikimedia-industrial-machine-960.jpg`：Wikimedia Commons 的工业机床照片缩略图，来源页面和作者归属见 `source-catalog.json`，许可为 CC BY 2.0。
- `images/nist-sp80082-scada-fig2.png`：从本仓库 `industrial-public-v1` 的 NIST SP 800-82r3 PDF 第 30 个 PDF 页面渲染的 SCADA 系统图，保留原始 PDF 哈希和页码，作为政府公开资料的图像证据样例。
- `images/nist-sp80082-page15.png`：同一 NIST PDF 的目录页渲染图，用来验证文档页图像、文字抽取和图片块的关联展示。
- `annotations/visual-entities.jsonl`：由代理逐图查看后整理的实体区域和来源绑定演示标注。坐标使用像素 `[x, y, width, height]`；它们不是独立人工金标准，状态为 `assistant_reviewed_demo`。

## 外部公开数据集清单

`source-catalog.json` 记录了可继续获取的工业视觉和标注数据集，包括 Aalto 激光粉末床熔融数据集、OPENZDM 电池焊缝缺陷数据集和 MVTec AD。仓库只保存来源与许可元数据，不复制体积很大或有非商业限制的压缩包。MVTec AD 明确是 CC BY-NC-SA 4.0，商业使用必须先取得权利人许可；其余数据集也必须按记录中的原始许可和署名要求使用。

## 标注与使用边界

1. 图像实体标注优先记录可见区域、图中文字和来源，不把图片中的推断当作事实；本夹具的框和标签只用于演示与回归，不是独立人工金标准；实体关系在真正的视觉关系模型授权前保持 `evidence_only`。
2. 公开样例可以用于本地演示和测试，但不能当作客户设备、工艺参数或安全控制建议。NIST 图像继承原始公开政府文档的来源约束，Wikimedia 图像必须保留作者、来源页和 CC BY 2.0 链接。
3. 任何新增数据都必须填入来源、获取时间、许可、文件哈希、是否可再分发、脱敏状态和标注版本。禁止抓取登录页、客户网页、个人信息或版权不明的全文。
