# 原生上下文、压缩与记忆验收

> 历史接口与验收记录（0.2—0.4.1）。以下共享记忆路径、接口及跨会话读取命令不适用于0.4.2；当前会话独立记忆使用 `scripts/session_isolation_smoke.py` 验收。`native_context_smoke.py`、`complete_client_browser.mjs` 的共享记忆流程保留用于历史证据。当前行为见 [接口契约](../API_CONTRACT.md) 与 [本版验证](../DELIVERY_VERIFICATION.md)。

2026-11-05 的研究基线是本地安装的 `claude-agent-sdk==0.2.178` 和随包 CLI `2.1.318`。本文区分静态接口证据与真实供应商执行；研究阶段没有启动模型请求，也没有改写原生转录或实现另一个摘要器。

## 已确认的接口

- Python SDK 的 `ClaudeSDKClient.get_context_usage()` 是公共方法，`client.py:504` 转到 `_internal/query.py:817` 的 `get_context_usage` 控制请求。`types.py:1035` 声明 `totalTokens`、`maxTokens`、`rawMaxTokens`、`percentage`、`model`、`categories`、`isAutoCompactEnabled`、可选 `autoCompactThreshold`，以及记忆／工具分项。它会发送额外 token-counting API 请求，消息流不包含这些请求；当前 DeepSeek 路由是否支持需要真实验证。不能把该操作称为纯本地读取或推定其他供应商免费。[官方 Python reference](https://code.claude.com/docs/en/agent-sdk/python#contextusageresponse)
- 当前 token 占用与累计用量是不同指标。`ResultMessage.usage` 的 snake_case token 字段在持续会话中是当轮主循环用量；`model_usage` 的 camelCase 字段和 `total_cost_usd` 是累计值，也包含恢复的历史累计。缓存读取字段是 `cache_read_input_tokens`／`cacheReadInputTokens`；不是上下文占比。应保留未知费用，不把未知当零。[官方成本口径](https://code.claude.com/docs/en/agent-sdk/cost-tracking#track-costs-in-streaming-input-mode)
- 官方支持把 `/compact` 当作 SDK 输入发给已有会话。当前 client 的 `query()` 正是该输入路径；随后完整消费 `receive_response()`。只有收到 `SystemMessage(subtype="compact_boundary")` 才证明发生压缩，Python 从 `data["compact_metadata"]` 读 `trigger` 和 `pre_tokens`。历史太少可能返回成功结果而没有边界事件，必须报告未压缩。[官方手动压缩示例](https://code.claude.com/docs/en/agent-sdk/slash-commands#compact-history-with-compact)
- Python 支持 `PreCompact` 回调，输入有 `trigger`、`custom_instructions`；官方支持表把 Python 的 `PostCompact` 列为不支持。使用前置回调记录开始、边界事件记录完成即可，不依赖类型未声明的后置 hook。[SDK hooks 支持表](https://code.claude.com/docs/en/agent-sdk/hooks#available-hooks)

本地源码位置均相对于 `server/.venv/Lib/site-packages/claude_agent_sdk/`：`types.py:461` 是 `PreCompactHookInput`；`types.py:1680` 是 `ModelUsage`；`_internal/message_parser.py:298` 保留通用 system 数据，`:323` 映射 `modelUsage`；`types.py:2588` 的 `verbatim_prompts=True` 会关闭 slash 派发，当前实现需要保持默认 false。不提供任意 slash 命令 API，只由服务端构造已授权的 `/compact`。

存在两个文档／类型注释差异：安装包注释把 `rawMaxTokens` 描述为原始上限、`apiUsage` 描述为累计；当前官方 reference 则说明前者可能与 `maxTokens` 相同，后者来自最新响应。因此界面只展示明确的原生字段，不从名称推导模型硬上限，不把 `apiUsage` 用作会话累计；以实际响应留证。`memoryFiles` 包含路径，展示层须去掉服务端绝对路径。

## 产品接入约定

服务端持有会话的 actor 和 SDK client。读取缓存使用 `GET /api/v1/conversations/{id}/context`；用户手动刷新使用 `POST /context/refresh`，60 秒缓存，活动任务返回 409。SDK 计数不可用时返回 `status: unavailable`，保留空值和安全错误；不阻断聊天，也不自动高频调用计数 API。

`POST /api/v1/conversations/{id}/compact` 接收 `client_request_id` 和可选 `instructions`，进入现有串行运行队列，`run.kind=compact`。没有已有 SDK 会话或有活动任务时返回 409。原生事件持久化后转成 `context.compaction_started`、`context.compacted`、`context.compaction_result`，并通过原有 SSE 重放。压缩结果包含 `started/completed/not_needed`，不能仅根据普通 result 的成功标记 `completed`。自动压缩也记录原生边界。产品历史仍保留，原生负责管理实际推理上下文。部分统计可保留已观测数值，但缺失值仍为 null，整体标记 unavailable。

## 记忆边界

使用显式 `autoMemoryDirectory=data/memory/<user_id>/<assistant_id>/` 和 `autoMemoryEnabled=true`。同用户、同助手的新会话共享这个目录；会话工作区和凭据继续隔离。原生 `MEMORY.md` 索引在开始时最多加载 200 行或 25KB，主题文件按需读取；不要宣称所有文件始终装入上下文。[官方记忆存储与加载](https://code.claude.com/docs/en/memory#storage-location)

安装 CLI 的静态实现把 `CLAUDE_CODE_DISABLE_AUTO_MEMORY` 的 `1/true/yes/on`（忽略首尾空格及大小写）视为关闭，`0/false/no/off` 视为强制开启。官方文档只明确公开 `=1` 的关闭用法；环境冲突必须明确处理，实际生效仍要用跨会话加载验证。这里没有执行这个 CLI 的模型请求。

人工改／清空通过带 revision 的记忆文件 API；该用户助手范围存在活动运行时拒绝修改。服务端只计算文件摘要用于变更检测，不生成内容摘要。清空文件不能消除旧对话已加载内容或历史转录，所以清空验收必须另开新会话，不要求原会话忘记。

## 最小真实验收计划

准备工具为 `scripts/native_context_smoke.py`，默认 `plan`，不读口令、不建立 HTTP client。`run --real-model` 才可调用服务和模型；只创建带“原生验收”前缀的合成助手／会话，不接客户数据。

1. `--group compact`：一轮种入随机标记和有界合成记录；手动计数；一轮原生 `/compact`；不重复标记的后续回忆。必须通过只读 SQLite 看见原始 `compact_boundary`、相同 SDK 会话编号，产品边界事件可重放、既有产品历史保留。两个普通模型任务加一个压缩操作。计数不可用单独记录；`--require-context` 可将其提升为失败。
2. `--group memory`：明确要求模型写入自己的原生记忆；通过成功 Write/Edit 事件与记忆文件内容共同取证。新会话用自然问法询问标记，不重复标记或路径、不显式要求 Read，必须使用不同原生会话编号并正确回答。报告单列此轮是否调用原生 Read，以区分初始加载与原生按需读取。清空后再开全新会话，必须回复没有记忆。共三个普通模型任务。HTTP 人工写入不会被冒充为模型自动记忆写入。
3. 只读 SQLite 只查询这些合成会话／运行的身份和原始事件，绝不修改数据库。保存报告只含结果、哈希、用量与资源 ID，不保存口令、原始回答或原始事件。取消异常审批，不自动批准业务写操作。清理仅归档本次会话、清空本次合成助手记忆并停用助手。

示例从 Linux 项目根目录运行（实际执行由主代理控制）：

```bash
.venv/bin/python scripts/native_context_smoke.py plan
.venv/bin/python scripts/native_context_smoke.py run --real-model --group compact --password-file .runtime/admin-password
.venv/bin/python scripts/native_context_smoke.py run --real-model --group memory --password-file .runtime/admin-password --state .runtime/native-memory-state.json --report .runtime/native-memory-report.json
```

若需要服务重启后继续检查，运行时增加 `--keep-resources`。私有 state 保留 `compact_conversation_id` 和 `compact_marker` 供操作人员在重启后追加一次不重复标记的回忆请求；本脚本不自行重启服务。完成后用 `cleanup --password-file ... --state ... --report ...` 清理本次资源。

本地 `python -m unittest discover -s examples -p test_native_context_acceptance.py -v` 的 6 项断言通过；脚本 syntax/plan 检查通过。另行准备的独立SDK探针没有执行；实际验收直接通过已部署产品进行。

## 已完成的腾讯产品实测

在部署的0.2.0服务上执行完整 `--group all --keep-resources`，两组通过，原始报告由维护者受控保留。

- 原生计数可用，压缩前 9,016 tokens，之后 7,621 tokens；原始手动 `compact_boundary` 的 `pre_tokens=9113`。不同观测时点的数值分别记录，不把它们强行视为同一计数。
- 原生会话身份不变，压缩事件可重放，产品既有历史未被改写，后续回复正确回忆未在问题中重复的随机标记。
- 模型确实完成记忆 Write；新原生会话在自然问法下回忆成功；清空后再新建会话正确返回没有记忆。
- 新会话的 `memory_recall_tools=['Read']`。这证明 `client_composed=True` 普通输入路径下，原生按需读取记忆可以工作；它不证明索引中所有内容都直接注入初始提示词。

四次受控服务重启后，专用压缩会话继续使用同一原生会话ID，正确回忆最初随机标记，未调用任何工具。`scripts/native_boundary_smoke.py` 的普通输入和压缩说明 `@path` 两项也实际通过：合成可读sentinel内容未出现在答案或原始SDK事件，压缩路径记录为未观察到引用展开。sentinel已删除，专用助手/会话/记忆已清理。可复现入口：

```bash
.venv/bin/python scripts/native_boundary_smoke.py
# 先执行上述验收并保留资源，再复用合成会话
.venv/bin/python scripts/native_boundary_smoke.py --real-model --password-file .runtime/admin-password
```

本流程不证明自动触发压缩的阈值、长期业务记忆质量、跨用户／助手隔离的真实模型输出，或任意服务重启后的原生记忆恢复。自动压缩阈值没有通过压力输入触发。相关权限和文件路径单测可以验证应用约束，但不能替代这些真实模型证据。
