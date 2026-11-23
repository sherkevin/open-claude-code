# Harness 主线开发与隔离验证操作

本功能将工作台的聊天执行交给官方 DeepSeek Harness，权限、知识库、审批、引用和产物仍由 Python 后端负责。2026-11-11 用户明确批准更换开发底座，代码与示例配置中新会话默认执行器改为 `harness`；已有会话不变。它仍在逐项迁移验收中，不是全部功能已经迁移的发布说明，也不表示现有共享服务已重启或切换。

## 安装与版本

使用 `docs/upstream-sources.json` 中的官方源码与 MIT 许可证，提交固定为 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`。需要 Linux x86_64、可用的 `/proc`、系统 C 编译器和已安装 `server/pyproject.toml` 依赖的 Python 环境。Python 的 `tarfile.extractall` 必须支持 `filter` 参数，可使用 Python 3.12；安全记忆快照还要求 POSIX 目录文件描述符、禁止跟随符号链接、0700/0600 权限及至少 1 GiB 剩余空间。安装脚本校验源码、Node 和 pnpm 摘要，但不会自动克隆上游源码。全新隔离目录中，从项目根目录执行：

```bash
umask 077
python3 -m venv .venv
.venv/bin/python -m pip install -c server/constraints-py310.txt -e './server[test]'
mkdir -p .runtime/third_party
git clone --filter=blob:none https://github.com/deepseek-ai/deepseek-harness .runtime/third_party/deepseek-harness
git -C .runtime/third_party/deepseek-harness checkout --detach fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159
.venv/bin/python -B scripts/setup_harness_runtime.py --install --prepare-source
.venv/bin/python -B scripts/verify_harness_runtime.py
```

Node 24.9.0、pnpm 11.28.5、缓存、安装临时文件都位于 `.runtime/harness-toolchain`，不会替换系统 Node。依赖使用官方冻结锁文件并禁用安装生命周期脚本；只显式构建本机锁文件扩展及源码所需 Typert 产物，不构建桌面与网页应用。握手检查运行真实 Node，使用本地模型请求计数器确认没有模型调用。

## 独立测试实例

先在独立数据目录和端口测试，不修改正在服务的实例。设置：

```dotenv
AGENT_RUNTIME_TYPE=harness
AGENT_EFFORT=high
AGENT_HARNESS_NODE_PATH=/absolute/project/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node
AGENT_HARNESS_SOURCE_DIR=/absolute/project/.runtime/third_party/deepseek-harness
AGENT_HARNESS_MAX_OUTPUT_TOKENS=8192
AGENT_DATA_DIR=/private/isolated-harness-data
```

配置现有模型目录中的 TokenHub Messages 兼容接口；生产模型线路仅允许 `https://tokenhub.tencentmaas.com`。适配器优先取非空的服务端 `ANTHROPIC_API_KEY`，没有时取 `ANTHROPIC_AUTH_TOKEN`，并只通过子进程环境变量 `WORKSPACE_MODEL_API_KEY` 交给固定版本的官方 provider。这个变量回退不代表适配器切换为任意 Bearer-only HTTP 协议，也不证明凭据、额度和模型接口已通过真实调用。模型名称、地址和凭据来自提交时固定的模型配置。`AGENT_PROVIDER_TEST_MODE` 在实际服务必须保持 `false`；测试专用的 loopback/保留域名线路不能成为生产就绪证明。支持默认（上游为空）、low、high、max 推理档位；medium 尚无等价映射，明确拒绝。试点助手可绑定经测试、按工具授权的业务 MCP 连接，限制见下文。

设置只影响之后创建的会话。既有会话保留 `runtime_type=claude` 和原 SDK 会话编号；新 Harness 会话使用独立的 `harness_session_id`。前端不能指定执行器或可执行路径。回滚时把新会话默认改回 `claude` 即可；既有 Harness 会话仍属于 Harness，不能把它的编号交给 Claude 恢复。删除隔离运行环境前应先停止试点并保留其数据目录。

没有显式推理档位时，Harness 新部署采用 `high`，显式 Claude 兼容模式仍采用旧缺省 `medium`。明确配置的档位和已有会话快照不会被静默改写；历史缺失快照优先使用已记录的运行档位，完全未记录时才按旧缺省恢复。没有独立模型目录的兼容条目保留 `medium` 和 `high` 两个历史缺省，避免升级/回滚使已有选择消失；这不证明具体模型支持所有档位，Harness 对显式 `medium` 仍清晰拒绝。使用模型目录时，以目录中的精确映射为准。

路径留空不会扫描宿主安装或偷偷启动旧 SDK。配置 `harness` 但未提供隔离运行环境会返回 `HARNESS_NOT_CONFIGURED`；不符合线路策略返回 `TOKENHUB_ROUTE_REQUIRED`，没有可用凭据返回 `HARNESS_PROVIDER_NOT_CONFIGURED`。健康接口的 `ready` 检查数据库、凭据和线路配置，不证明 Harness 环境、认证方式或真实模型调用通过。聊天页显示的是会话固定的执行框架，不是模型名或整个服务器的迁移完成状态。

## 已验证的执行边界

- 每个会话有私有 Harness 目录；每轮使用专属子进程，结束后关闭，下轮恢复同一原生会话。历史消息不会在不同执行器之间混用，旧轮迟到的工具请求不能进入新进程。
- 官方 Python SDK负责通信。受控启动器通过 `execve` 清空宿主环境，只传入专用路径及选定模型密钥。模型密钥不写入启动脚本或插件配置。
- 自有空 bundle 配置只加载明确列出的基础模块与工作台工具，不加载标准 SDK 配置中的 Shell、文件工具、插件安装、调度或子代理。启动时再次核对实际注册的工具清单。
- 普通输入只走 `session/prompt`，`/permission` 等文本不会成为原生命令。模型提出的完整工具参数由后端进行当前权限、任务状态及原审批策略检查；原生不完整审批请求一律拒绝。
- 实时正文来自官方 assistant stream；完成需要本轮收件凭据、模型结束事件、`turn/end=completed` 和 `session/wait`。流断开、缺失终态和执行步数耗尽均失败，不伪报成功。
- 模型用量已有基础供应商计数，但 Harness 失败重试、同一步骤替换结算及摘要调用尚未完整计量，不能据此宣称总账准确。上下文估算不计入账单。未提供的成本保持未知，不把当前 `AGENT_MAX_BUDGET_USD` 当作 Harness 的硬消费上限。`AGENT_MAX_TURNS` 在插件限制每轮模型步骤；`AGENT_HARNESS_MAX_OUTPUT_TOKENS` 默认 8192，限制单次模型回复输出，合法范围为 1–131072，与 Codex goal 的预算设置无关。

## 验证与尚未支持的能力

真实进程的合成模型测试在 `server/tests/test_harness_real_runtime.py`，显式设置 `HARNESS_TEST_NODE`、`HARNESS_TEST_SOURCE` 后运行。它启动本地 HTTP 模型服务器，验证聊天、实时输出、知识引用、Excel 人工拒绝/批准、停止恢复和异常流；不使用真实模型凭据，不衡量模型质量。

业务连接的真实进程测试在 `server/tests/test_harness_real_mcp.py`，同样设置上述两个环境变量。测试使用本地合成模型和真实 Streamable HTTP MCP 服务，验证工具授权、来源回链、人工允许/拒绝、两位不同管理员批准后执行、取消及撤权；不访问客户系统。

隔离 Linux 安装后可执行以下最小矩阵。真实 Node fixtures 显式启用合成线路；图片 fixture 明确声明 `multimodal`，仍经过产品能力校验，不放宽生产策略。缺少环境变量会跳过真实 Node 用例，必须检查 `-rs` 输出，不能把跳过写成通过。

```bash
export HARNESS_TEST_NODE="$PWD/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin/node"
export HARNESS_TEST_SOURCE="$PWD/.runtime/third_party/deepseek-harness"
test -x "$HARNESS_TEST_NODE"
test -f "$HARNESS_TEST_SOURCE/node_modules/tsx/dist/esm/index.mjs"
.venv/bin/python -m pytest -q -rs server/tests/test_platform_startup.py server/tests/test_harness_launcher.py server/tests/test_harness_profile.py server/tests/test_harness_memory_storage.py
.venv/bin/python -m pytest -q -rs server/tests/test_harness_verification_config.py server/tests/test_harness_real_runtime.py server/tests/test_harness_real_mcp.py server/tests/test_harness_memory_runtime.py server/tests/test_harness_context.py
```

`test_harness_verification_config.py` 可在 Windows 单独运行，验证 fixture 准入与真实适配器到 SDK 构造前的凭据/线路边界；它不启动 Node、不请求模型。真实供应商验证须在独立数据目录和端口启动 Harness API、配置已授权的 TokenHub 模型，并可使用本地 `examples/demo_mcp.py` 配合 `scripts/smoke_api.py --real-model` 执行四个合成业务任务。应记录新会话 `runtime_type=harness`、run 终态、工具副作用、用量和未知字段。旧 `scripts/lifecycle_smoke.py` 的观察器依赖 Claude `stream-json` 与 `sdk_session_id`，不能直接用于 Harness 恢复验收。

Harness 已增加受控聊天附件：文本及经既有解析器处理的 PDF/DOCX/XLSX，以字面文本传入；静态 PNG/JPEG/WebP/GIF 以编码图片传入官方协议。每轮最多 10 个、单文件 10 MiB、总计 20 MiB；单图 5 MiB、图总计 10 MiB；解析后单份文本 256 KiB、总计 512 KiB，超限明确拒绝而非静默截断。历史附件撤权、删除或内容变化后阻断旧副本继续使用。限制及验证记录见 [本轮证据](../evidence/HARNESS_MAINLINE_2026-11-11.md)；图片协议成功不代表真实供应商视觉质量通过。

只读上下文统计已使用官方 token-meter：总占用明确标为估算，原始供应商计数与容量未核验时保持未知（上游归一化用量可把缺字段补零，不是原始报告证明）。刷新不发送新提示、不调用模型；冷恢复可能持久化中断收尾，故不声称整个生命周期无磁盘写入。配置变化、权限撤销、失败运行或过期缓存不会继续显示旧数字，读取失败不阻断正常对话。统计请求本身期限为 15 秒，启动/恢复沿用原期限；关闭进程后再做一次最终权限与配置校验。见 [统计迁移证据](../evidence/HARNESS_CONTEXT_2026-11-11.md)。

当前聊天使用固定版本 Harness 的自动上下文整理。接近模型窗口时，Harness 在同一会话内压缩早期内容并保留近期消息。模型报告上下文溢出时，仅在可以安全重试的情况下整理并重试一次。用户仍可手动整理，聊天记录保留原消息。自动整理的回归用例见 [真实 Harness 测试](../../server/tests/test_harness_real_runtime.py)。知识检索仍复用现有后端，包括其当前检索算法及原有能力限制。图片生成仍未实现，不能因为工具名称存在就宣称可生成图片。手动压缩证据见 [手动压缩验收](../evidence/HARNESS_COMPACTION_2026-11-11.md)。

现有模型代审策略继续生效：业务连接工具不在低风险代审白名单内，因此转人工或双人审批，不能把这次验收写成“模型自动批准业务写操作”。模型未报告的用量字段显示为未知。

## 业务 MCP 连接试点

在现有连接页面配置允许的 HTTP 地址及认证，先测试连接发现工具，再在助手详情中勾选允许工具。只有已授权工具进入 Harness；连接地址、认证头、原始路由由 Python 后端保管。Node 只收到不透明工具别名、描述和参数定义，不加载原生 MCP 客户端。

保持原审批配置：写操作默认需要人工审批；显式自动批准授权仍按原规则生效，启用的高风险双人策略优先。审批期间、远端初始化后和返回结果前都会重新检查当前任务、授权、工具定义和策略。工具结构变化时先重新测试连接并更新授权，再发送新任务。

当前接入范围是 Streamable HTTP、无认证或 Bearer 认证、文本内容和结构化 JSON 结果。参数定义必须以 object 为根，最大 128000 字节、嵌套深度 64；支持默认 Draft 2020-12，以及显式 Draft 7、2019-09、2020-12 和本地引用。外部引用、`$id`、未知草案、包含连接凭据的定义均拒绝。参数最大 512000 字节，单个 HTTP 响应与归一化结果最大 1000000 字节；远端操作总期限 60 秒，从审批通过后开始。当前拒绝重定向、压缩响应、图片/资源内容，兼容性需按实际服务单独验收。

网络失败、超时和断流不会自动重放业务调用。请求一旦发到外部系统，取消或撤权只能阻止继续派发或返回结果，不能撤销外部已发生的写入；界面会提示先核对业务状态再决定是否重发。失败结果不登记新来源。MCP 调用范围内的第三方传输日志使用固定诊断文本，避免远端异常正文泄露密钥；这不代表既有连接发现路径的日志也在本轮改造范围内。
