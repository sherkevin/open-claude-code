# TokenHub OpenAI 视觉适配器证据（2026-11-07）

## 范围

本证据只验证学校云本地 `magpie` 回环代理的 OpenAI Chat Completions 图片请求格式，使用公开的 NIST 工业控制系统图片夹具。没有读取公司或客户文件，没有把令牌写入日志，也没有重启服务或部署生产。

## 已实现

- `server/app/openai_visual.py` 使用 `POST /v1/chat/completions`，发送有界的文字提示和 `data:image/*;base64,...` 图片块；知识索引在显式开启视觉阶段后会把通过结构校验的实体和关系候选写入版本化图片图谱。
- 默认视觉线路为公开目录标识 `deepseek-v4-flash-202605`，私有上游映射只保存在模型目录；视觉、实体和关系角色可单独配置，普通聊天模型不会静默回退。
- 响应只保留结构化候选、模型标识、耗时、用量和响应正文的 SHA-256；不持久化原始响应、图片内容或认证令牌。
- `GET /api/v1/models/provider-preflight` 报告 Fitness、Tab、视觉、实体、关系和向量角色的授权状态，默认不发起模型请求。

## 本地探针

使用夹具 `fixtures/industrial-public-v2/images/nist-sp80082-scada-fig2.png`，提示要求只返回实体和关系 JSON。一次成功调用返回合法空候选（模型没有给出足够可靠的实体），记录如下：

```text
model: tencent-cloud/deepseek-v4-flash-202605
latency_ms: 2244.3
response_sha256: e959cc6ea0dff1d18aa5040edca316d8fef872b58aff54dd55abe220647be2ee
usage: prompt_tokens=972, completion_tokens=76, total_tokens=1048
json: {"entities": [], "relations": []}
```

这证明线路和协议字段可以工作，不证明视觉实体召回率、关系准确率或客户业务质量。模型偶尔可能只返回推理而没有正文；适配器在这种情况下安全拒绝，不把空响应伪装成成功。

## 自动化验证

```text
PYTHONPATH="$PWD/server:/tmp/open-claude-pylib" pytest -q \
  server/tests/test_openai_visual.py \
  server/tests/test_model_capabilities.py
12 passed, 1 warning
```

测试使用注入的本地假传输，覆盖图片块、模型白名单、响应摘要、超大图片拒绝和凭据不出证据；没有把测试令牌写入产品日志。

## 仍需验收

当前候选已绑定图片文档版本、模型公共标识、置信度和可选区域；模型输出为空、格式错误或超时会安全丢弃，不污染本地 OCR/CLIP 索引。仍需用公开或脱敏工业图片标注集验收召回率、关系准确率、区域定位和误召回，不能把线路可调用性探针单独宣称为多模态知识图谱完成。
