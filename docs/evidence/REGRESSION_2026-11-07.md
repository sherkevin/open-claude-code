# 本地回归证据（2026-11-08，补充复验）

## 服务端

```text
cd server
PYTHONPATH="/media/data3/dengkw/projects/open-claude-code/server:/tmp/open-claude-pylib" \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.tmp/open-claude-pycache \
pytest -q -p no:cacheprovider --basetemp=/media/data3/dengkw/.tmp/open-claude-full-20261108
429 passed, 1 warning in 1036.51s
```

唯一警告是测试依赖发出的 `StarletteDeprecationWarning`，不是业务代码失败。

## 客户端

```text
cd client
npm test -- --run
npx tsc -b
18 个测试文件、82 个测试通过；TypeScript 编译通过
```

`npm run build` 通过；Vite 仅提示产物主包超过 500 kB 的性能建议，不是构建失败。

本轮完整服务端回归使用 `/media/data3/dengkw/.tmp/open-claude-full-20261108` 独立的 `--basetemp`，完成上述 429 项通过；聚焦回归额外覆盖 TokenHub 视觉候选写入、本地视觉运行时、无文字图片只依靠视觉向量完成索引、公开工业知识库清单、文件哈希和实体来源偏移。测试依赖仍有一个 Starlette/httpx 弃用警告，不是业务代码失败。

## 其他离线闸门

- `python3 -m unittest examples.test_evaluation_gate -v`：2 项通过。
- `python3 scripts/evaluation_gate.py --output /tmp/zhixing-evaluation-gate.json`：`status=passed`，固定合成任务全部通过。
- `python3 -m compileall -q server/app scripts/evaluation_gate.py examples/test_evaluation_gate.py`：通过。
- `server/tests/test_workflows.py`：7 项通过，覆盖工作流版本、发布、回滚、安全图校验、模板复制、结构化表单/电子表格预览和会话隔离。
- `git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check`：通过。

## 边界

此前完整回归使用本地代码、公开工业夹具、合成夹具、已授权的本地 TokenHub 回环视觉探针和注入式测试传输；没有连接真实客户数据、生产系统或把凭据写入证据。完整回归阶段没有执行服务重启；本轮增量仅重启本地开发进程，详见下节，未触碰生产。

## 2026-11-08 多模态图谱与本地稳定性增量

- `server/tests/test_multimodal_graph_quality.py` 与 `server/tests/test_knowledge_evidence.py`：54 项通过。覆盖 OCR 实体到图片区域和文字框的绑定、模型候选与 `evidence_only` 关系、提供方失败审计、知识文档链接及运行引用链接。
- `server/tests/test_model_recommendation.py server/tests/test_config.py`：21 项通过；覆盖有界 Fitness 路由、无授权默认拒绝、模型配置读取和本地安全回退。
- `cd client && npm test -- --run`：18 个测试文件、82 项通过；`npm run build` 通过，仅有既有大包体积提示。新增图谱实体/图片/区域点击放大、检索证据卡、本地文档链接、HTTP(S) 远程链接过滤、解析失败重试入口和 Fitness 上下文自动摘要测试。
- 本地开发服务已使用显式 `AGENT_PROVIDER_SETTINGS=/home/dengkw/.claude/settings.json` 重启（仅学校云开发进程，非生产）；`GET /api/v1/health/live` 返回 200，`GET /api/v1/health/ready` 返回 `{"status":"ready","model_configured":true}`。
- 图谱和视觉实体仍保持“部分实现”：模型实体是 `model_candidate`，关系是 `evidence_only`；没有独立人工实体/关系金标准，因此不能把当前公开样例的演示命中率升级为工业质量验收。

本次增量没有使用真实客户数据、没有输出凭据、没有部署生产或重启生产；TokenHub 视觉路线和付费模型只保留已有授权配置，未新增未经授权调用。

## 2026-11-08 合成对标 Token 口径增量

- `server/tests/test_benchmark.py`：4 项通过；验证压缩过程不计入总 Token、增强方案输入/总 Token 低于未压缩基线、压缩节省独立记录，以及客户助手和视觉任务仍为 `not_measured`。
- `cd client && npm test -- --run src/benchmark.test.tsx`：5 项通过；验证对标页面显示压缩口径、增强方案柱状低于基线、导出版本和待测边界保持不变。
- 固定夹具结果：增强方案输入/输出/总 Token 为 `153/34/187`，基线为 `236/34/270`，压缩节省 `83` 且不计入总 Token。数字是本地固定中文文本估算，运行器未调用模型、客户系统、生产数据、网络或计费服务。
- 相关记录：`docs/evidence/TOKEN_ACCOUNTING_2026-11-08.md`。真实供应商 Token、费用、模型速度和客户现有助手仍未测量，不能将本地估算宣传为账单或客户收益。
