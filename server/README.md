# 知行工作台 Server

Python 3.10+、单进程 FastAPI 服务，使用真实 Claude Agent SDK 执行，SQLite 保存产品状态。配合独立 `client/` 使用。**生产环境没有模拟模型降级模式。**

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -c constraints-py310.txt -e '.[test]'
export AGENT_DATA_DIR=/absolute/private/data
export AGENT_PROVIDER_SETTINGS=/absolute/authorized/settings.json
export AGENT_MODEL_CATALOG=/absolute/private/model-catalog.json
export AGENT_MODEL='deepseek-v4.1-flash-1017'
export ANTHROPIC_BASE_URL='https://tokenhub.tencentmaas.com'
export AGENT_ALLOWED_ORIGINS='https://your-product.example'
export AGENT_MCP_ALLOWED_HOSTS='127.0.0.1,localhost,mcp.example'
export AGENT_COOKIE_SECURE=true
.venv/bin/python -m app.cli init-admin --username admin --password-file /private/admin-password
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

启动前将 `model-catalog.example.json` 复制到上述私有模型目录文件，填入已授权的上游线路并按需启用，保留0600且由服务运行账号读取。模板默认 `deepseek-v4.1-flash`，并提供 `hy4`（混元 4）和 `kimi-k3.1-pro`（Kimi 3.1 PRO）目录项；新目录项默认留空且禁用，填入已授权 TokenHub 上游后才会启用。Dano 的私有线路留空且禁用，在配置完成前不能作为默认项。没有 `AGENT_MODEL_CATALOG` 时仅保留单模型兼容模式，不会出现完整模型选择。

Windows 虚拟环境使用 `.venv/Scripts/python.exe`。密码至少 10 位，CLI 不输出密码。不要把密码文件或真实 `.env` 放入源码包。环境变量需由 shell/service manager 设置，服务不自动读取 `.env.example`。原生服务的 `AGENT_MODEL_CATALOG` 必须是宿主机可读的绝对路径；Compose 使用根 `.env` 的 `AGENT_MODEL_CATALOG_HOST` 将文件挂载到容器内固定路径，二者不能混填。

首次运行在数据目录生成 `encryption.key`，也可用 `AGENT_SECRET_KEY_FILE` 指定外部 Fernet 密钥文件，或用 `AGENT_SECRET_KEY` 注入。更换/遗失密钥会使已保存 MCP 凭据不可解密；备份数据库时须同步安全备份该密钥。

## 模型和 MCP 配置

`AGENT_MODEL_CATALOG` 定义公共模型名称、私有上游线路、可选推理级别与默认项；可选 `AGENT_DEFAULT_MODEL_ID` 应设置为已启用的公共模型ID。客户端仅接收公开名称和级别。会话保存选择，空闲时可切换；执行使用独立快照并保留原生会话。Dano 的 light/high/max 映射到上游 low/high/max，目录中的其他模型可使用 `default` 对应的供应商默认参数。

空闲会话可以通过 `POST /api/v1/models/recommend` 请求“Select the most fitness model”。默认使用有界本地评分；设置 `AGENT_MODEL_ROUTER_ENABLED=true` 后，服务会用 DeepSeek V4.1 Flash 对不超过 2,000 字的任务摘要和公开模型能力做一次有界判断，超时、额度或格式异常时回退本地评分。`AGENT_MODEL_ROUTER_TIMEOUT_SECONDS`、`AGENT_MODEL_ROUTER_MAX_OUTPUT_TOKENS` 和 `AGENT_MODEL_ROUTER_BUDGET_USD` 控制时限、输出和推荐调用预算。\n\n`AGENT_PROVIDER_SETTINGS` 只导入 `env` 中允许的 Anthropic 兼容供应商变量和顶层 `model`（见 `app/config.py`），不导入该文件的 Hooks、工具权限或 MCP 命令。外部环境覆盖同名供应商变量；目录中每个模型还可配置服务端 `provider_env` 覆盖项。生效配置仅在内存中传给执行器，数据库保存摘要；全局或单模型线路／凭据变更后需要明确重选，不能静默改变已准备的任务。

旧会话迁移及无目录的兼容模式继续按 `AGENT_MODEL` → 外部 `ANTHROPIC_MODEL` → 配置文件 `env.ANTHROPIC_MODEL` → 配置文件顶层 `model` → 项目默认型号取值。升级时保留原 `AGENT_MODEL`／`AGENT_EFFORT`，不要用新目录默认值改写历史配置。上面的示例使用已实测的腾讯服务端点。原始 SDK 估价不作为 TokenHub 账单：`estimated_cost_usd` 默认返回 `null`。

登录 client 后，在“业务连接”中添加 Streamable HTTP MCP，配置无认证或 Bearer，测试并发现工具；再绑定到助手，分别选择允许工具与自动批准工具。域名必须先列入 `AGENT_MCP_ALLOWED_HOSTS`，不支持通配符、地址内嵌凭据和重定向。应用服务必须能够访问 MCP；浏览器可达不代表服务器可达。第一版不接受任意 stdio 命令，不宣称提供 OAuth 或本地电脑连接器。

## 状态与执行

- `/api/v1` 为版本化接口；`/docs` 为请求模式和 OpenAPI。所有应用数据接口均校验登录身份；连接/助手管理需要 admin。
- 身份使用 HttpOnly cookie；同源部署推荐由 client 的 Web 服务器反向代理 `/api`。跨源必须准确配置允许来源，生产 HTTPS 开启 secure cookie。
- 每个对话的活动 worker 持有自己的 SDK client；空闲超时回收，后续以保存的 SDK session ID 恢复。同一对话一次只运行一个任务。必须使用 **一个应用进程**，不可直接增加 Uvicorn workers。
- SSE 事件先持久化再发送，支持 `after` 和 `Last-Event-ID`。浏览器断线不会取消任务。服务重启将未完成任务标为 interrupted，不自动重放写操作。
- `/api/v1/runs/{id}/trace` 为仅任务所有者可下载的 JSON 验收记录，包含该任务的公共模型名称／ID、推理级别、耗时、消息、产品事件和用量；供应商仅显示公共服务标签。私有上游型号、供应商主机和配置摘要留在服务端内部审计，不随轨迹返回；不包含凭据或原始 SDK 事件。未知费用保持 `null`。
- 审批仅批准服务器保存的原始参数；配置撤权在执行端检查，审批后再次校验。批准一次不修改长期工具权限。
- 上传附件按用户归属保存；文本由工作区 Read 使用，PDF 提取最多前 100 页文字。未验证的图像理解被明确拒绝，原件仍可预览和下载。扫描 PDF/OCR、复杂 Office 格式不在当前解析能力内。
- 文件产物写到会话 `artifacts/`，完成后保存为可下载的不可变快照。内置工具仅开放受路径限制的文件操作；不提供任意 shell、WebFetch 或子 Agent。
- 原生压缩和会话转录由 SDK 保持；跨会话记忆目录为 `memory/<用户>/<助手>/`，与每个会话的工作区分开。原生自动记忆显式启用，人工编辑需版本匹配且该记忆范围没有活动任务；下一轮重新加载。清空记忆不会删除历史对话或抹去已有上下文。运行配置排除祖先目录的 CLAUDE/AGENTS/rules 文档，并关闭 AGENTS 自动选择；组织管理策略仍由部署方控制。
- 普通消息通过 SDK 的逐条 `client_composed:true` 保留原文并禁用隐式 slash 命令与 `@路径` 附件扩展，文件读取必须经过工具权限。手动整理走专用 `/compact` 操作，只有原生 `compact_boundary` 才记录压缩完成；未发生、失败、中断分别记录。上下文统计由 SDK `get_context_usage()` 返回，手动刷新、60 秒缓存，不从累计用量推算，可能产生额外供应商计数请求。完整接口见 `../docs/API_CONTRACT.md`。

## 部署边界

当前为一组织一部署、admin/member 权限，数据库内仍对对话、执行、文件执行归属校验；管理员身份不自动获得他人聊天内容。执行设置、转录文件与每个会话工作区分开。工具路径与权限校验是应用保护，不等同于允许任意不受信代码的 OS 沙箱。需要任意代码执行或跨组织托管时，应使用额外容器／OS 隔离与网络策略。

对外提供 HTTPS；不要将模型供应商配置文件、数据库、数据目录或 SDK 原始事件通过静态服务器发布。反向代理需关闭 SSE 缓冲并设合适的上传和连接超时。`Dockerfile` 提供独立 server 镜像，运行时挂载持久数据目录及只读的供应商配置文件；配置文件中的 host 路径需改为容器可见路径。

应用在 multipart 解析前限制总请求体（附件上限加 1 MiB 表单开销），包括无 Content-Length 的分块上传；这部分请求先在内存中有界缓冲。生产入口仍应设置并发和速率限制。登录限制同时覆盖账号/IP组合与独立 IP，限流记录会过期清理。

## 验证

```bash
.venv/bin/python -m pytest -q
```

自动化测试通过依赖注入使用测试 runtime，覆盖身份、跨用户访问、MCP 凭据及主机策略、连接停用、审批、幂等提交、持久事件重放、中断、温会话复用和重启恢复。它们不声称验证真实推理；真实模型及 MCP 的验收记录由项目级交付文档单独提供。

## 运维

备份 `AGENT_DATA_DIR`（数据库 WAL 要一致快照或先停止服务），以及外置密钥和供应商配置；恢复时保留工作区及 SDK 配置目录。轮换管理员密码可通过受保护的 `/users/{id}` 接口，修改密码会注销其现有会话。`/health/live` 检查进程，`/health/ready` 检查数据库并报告是否配置模型凭据；它不保证远程模型额度或 MCP 连通。
