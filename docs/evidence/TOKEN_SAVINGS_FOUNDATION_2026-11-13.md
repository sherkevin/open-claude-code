# Token 节省基础计量阶段证据

日期：2026-11-13。环境：学校云项目 `open-claude-code`（知行工作台）。

## 结论

本阶段完成了 Token 节省工作的第一步：建立可审计的分阶段计量边界，并以向后兼容方式接入现有 `UsageLedger`。当前没有改变模型请求内容、输出上限、检索结果或压缩策略，因此本阶段不声称已经节省供应商费用。

## 新增内容

- `server/app/token_savings.py`：独立的 Token 阶段账本，支持回答、压缩、路由、重试和工具阶段。
- `scripts/token_savings_report.py`：只读 JSON 报告命令，不访问数据库、不调用模型、不写入输入文件。
- `server/app/plugin_usage.py`：保留历史两字段接口，新增 `token_accounting` 视图，记录缓存读取和缓存写入 Token。
- `docs/plans/2026-11-13-token-savings-mainline.md`：后续确定性结果限额、输出预算和自动压缩消融计划。

## 计量规则

1. 供应商没有返回的字段保持未知，不补零。
2. 回答、压缩、路由、重试和工具调用分开记录，压缩阶段不能从总账中消失。
3. 缓存命中率只有在输入 Token 和缓存读取 Token 都完整时计算。
4. 本地字符数或压缩比例只能作为非权威估算，不能直接变成费用。
5. 当前报告不计算美元金额，除非以后取得经过核验的供应商价格和完整用量。

## 学校云验证

```text
PYTHONPATH=server:. python3 -m pytest -q \
  server/tests/test_harness_usage_ledger_plugin.py \
  server/tests/test_token_savings.py \
  server/tests/test_token_savings_report.py \
  server/tests/test_token_savings_integration.py
14 passed in 0.14s
```

纯 Python 编译检查通过：

```text
python3 -m py_compile \
  server/app/token_savings.py \
  scripts/token_savings_report.py \
  server/tests/test_token_savings.py \
  server/tests/test_token_savings_report.py
```

## 尚未完成的事项

- 尚未改变任何生产请求，因此真实 Token 节省仍为 `not_measured`。
- 尚未把工具结果裁剪、任务输出预算或检索上下文限额接入运行时。
- 尚未启用 Harness 自动整理或 billion-context。
- 尚未进行真实 TokenHub 账单对账；供应商缺失字段继续保持未知。
- 完整服务端回归未在本阶段重复执行；本阶段只执行新增计量及其直接回归。

下一阶段应使用只读报告先观察真实回答、压缩、缓存和重试占比，再按单一变量接入确定性结果限额，并用同一任务和同一模型做前后对照。
