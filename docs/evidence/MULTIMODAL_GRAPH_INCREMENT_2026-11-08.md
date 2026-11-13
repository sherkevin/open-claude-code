# 多模态图谱与证据卡增量验收（2026-11-08）

## 变更范围

- 后端 `server/app/knowledge.py`：OCR 实体保存字符偏移、文字框和图片区域；模型输出标记为 `model_candidate`，模型关系标记为 `evidence_only`；视觉失败写入可审计失败状态；文本和视觉检索返回知识文档链接，运行引用返回来源链接。
- 客户端 `client/src/KnowledgePage.tsx`：实体、图片节点、图片区域、知识库缩略图和检索图片证据均可点击；灯箱显示版本、尺寸、处理状态和证据边界；解析失败显示重新解析入口；来源链接只放行 HTTP 或 HTTPS。
- 客户端 `client/src/ChatPage.tsx`：Fitness model 点击后从当前输入、最近用户消息和助手职责生成不超过 2000 字的有界摘要，直接执行推荐和应用，不要求重新输入。

## 可重复验证

```text
PYTHONPATH=/tmp/open-claude-pylib:server \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.tmp/open-claude-pycache \
python3 -m pytest -q -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/.tmp/open-claude-graph \
  server/tests/test_multimodal_graph_quality.py \
  server/tests/test_knowledge_evidence.py
54 passed, 1 warning

PYTHONPATH=/tmp/open-claude-pylib:server \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.tmp/open-claude-pycache \
python3 -m pytest -q -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/.tmp/open-claude-router \
  server/tests/test_model_recommendation.py server/tests/test_config.py
21 passed, 1 warning

cd /media/data3/dengkw/projects/open-claude-code/client
npm test -- --run
npm run build
18 个测试文件、82 个测试通过；构建通过，仅有既有大包体积提示。
```

本地开发健康检查：

```text
GET http://127.0.0.1:8000/api/v1/health/live  -> 200 {"status":"ok"}
GET http://127.0.0.1:8000/api/v1/health/ready -> 200 {"status":"ready","model_configured":true}
GET http://127.0.0.1:18086/                  -> 200
GET http://127.0.0.1:18086/login             -> 200
```

## 证据边界

- 当前实体质量证据来自公开样例、本地 OCR、确定性规则和受控测试；没有大规模独立人工工业实体/关系金标准，因此需求继续标记为“部分实现”。
- 模型候选和同区域共现关系不作为语义事实或验收金标准；缺视觉模型时不会伪造向量或解析完成状态。
- 本轮只重启学校云本地开发进程以加载显式 provider settings；未部署、未重启生产、未使用真实客户数据或输出凭据，也未新增未授权付费模型调用。
