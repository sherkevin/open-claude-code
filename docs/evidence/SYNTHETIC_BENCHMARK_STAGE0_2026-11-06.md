# 阶段 0 合成工业基准闭环（2026-11-06）

本记录覆盖需求 `PROT-003`、`BENCH-001` 和 `BENCH-002` 的第一阶段最小闭环。
它只证明夹具、标准答案、来源标记、报告格式和重复运行入口可用，不证明真实
模型质量、客户业务正确率或本平台优于任何现有助手。

## 固定输入

- 夹具：`fixtures/synthetic-industrial-v1/`
- 版本：`1.0.0`
- 随机种子：`20261106`
- 清单 SHA-256：`0d4f4aaa0ec37517c427ab7bcef9218f080580a641c5275e859eb63adf447e54`
- 标准答案 SHA-256：`f9d22e1b197d0dd385488bef37726e57ad9e90968a7b99632f2d912840bf5a23`
- MCP 工具契约 SHA-256：`193180aa76141769341e3f3fe7cf4639521d257c9bd321fe1f76829ddee5f46d`
- 对比键：`355b2398f15d812b1cbcf3f0`

夹具包含合成质量统计文档、批次表、可登记来源的 SVG 图片、只读工具契约和
标准答案。所有工具结果、来源和报告都带有 `synthetic=true` 与夹具标识。HTTP
入口 `examples/synthetic_industrial_mcp.py` 只允许回环地址；不启动它也可以用
离线运行器完成本阶段验收。

## 可重复命令

以下命令不访问网络、模型、产品数据库、生产数据或客户 MCP：

```bash
python3 scripts/synthetic_benchmark.py plan
python3 scripts/synthetic_benchmark.py run \
  --platform synthetic-reference \
  --output /tmp/zhixing-synthetic-benchmark-stage0.json
python3 scripts/synthetic_benchmark.py verify \
  --report /tmp/zhixing-synthetic-benchmark-stage0.json
python3 -m unittest discover -s examples -p 'test_synthetic_benchmark.py' -q
python3 -m py_compile scripts/synthetic_benchmark.py examples/synthetic_industrial_mcp.py
python3 scripts/synthetic_benchmark.py clean \
  --output /tmp/zhixing-synthetic-benchmark-stage0.json
```

## 实测结果

- 4 个任务全部通过：质量加权统计、知识口径检索、会话记忆标记、图片来源登记。
- 任务正确性和来源覆盖率为 `measured`；每个任务有一个已登记、可校验的合成来源。
- 本地函数耗时记录为可观测性字段，不代表模型响应速度；没有伪造首字节时间。
- Token、费用、原生上下文和视觉质量均为 `not_measured`，原因明确写入 JSON 报告。
- 会话记忆任务仅验证合成会话范围内的固定标记；不宣称服务端原生记忆能力。
- `verify` 通过后，`clean` 只删除命令中明确给出的报告文件，不触碰项目运行目录。

JSON 报告包含原始工具结果、答案摘要哈希、任务检查、来源定位、固定输入哈希、
指标状态和证据边界。后续接入真实产品或对标助手时，必须保持相同任务、夹具、
工具契约和 `comparison_key`，并把未测指标替换为真实证据；不能把本记录中的本地
耗时或合成正确率宣传为客户效果。
