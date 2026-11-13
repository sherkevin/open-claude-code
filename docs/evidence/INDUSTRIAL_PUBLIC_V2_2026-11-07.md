# 工业公开多模态样例证据（2026-11-07）

## 目的

为知识库检索、图片证据节点、实体区域展示和回归测试提供一组可审计的公开样例。该样例集不包含客户数据，不代表任何客户工厂的流程或安全要求。

## 本地文件

目录：`fixtures/industrial-public-v2/`

- 一张 Wikimedia Commons CC BY 2.0 工业机床照片缩略图，保留作者、来源页面和哈希。
- 两张从 NIST SP 800-82r3 PDF 渲染的页面图，保留原始 PDF 哈希、PDF 页码和渲染说明。
- `annotations/visual-entities.jsonl` 保存人工复核的像素框、标签、图片哈希和来源绑定；关系列表为空并标记为 `evidence_only`，不把人工展示标注冒充视觉关系模型输出。
- `source-catalog.json` 同时记录 Aalto 激光粉末床熔融数据集和 OPENZDM 电池焊缝数据集（CC BY 4.0），以及 MVTec AD（CC BY-NC-SA 4.0）。这些大型数据集仅保留目录项，避免把有非商业限制或数百兆至数十吉字节的压缩包复制到仓库。

## 验证

```text
PYTHONPATH=/media/data3/dengkw/projects/open-claude-code:/tmp/open-claude-pylib \
pytest -q server/tests/test_industrial_public_v2.py
2 passed
```

测试检查每个本地图像文件的 SHA-256、来源 URL、许可边界、脱敏状态和人工实体标注的图片哈希绑定，并断言当前没有伪造语义关系。

## 来源与许可

- [Wikimedia Commons 工业机床图片](https://commons.wikimedia.org/wiki/File:Industrial_machine_working_in_a_manufacturing_facility.jpg)；作者 Shixart1985，CC BY 2.0。
- [NIST SP 800-82r3](https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-82r3.pdf)；政府公开技术出版物，保留 NIST 署名和原始链接。
- [Aalto 激光粉末床熔融标注数据集](https://doi.org/10.5281/zenodo.14996805)；CC BY 4.0，按原始数据集署名要求使用。
- [OPENZDM 电池焊缝缺陷数据集](https://doi.org/10.5281/zenodo.14025630)；CC BY 4.0，按原始数据集署名要求使用。
- [MVTec AD](https://www.mvtec.com/company/research/datasets/mvtec-ad)；CC BY-NC-SA 4.0，商业使用前必须向权利人确认许可。
