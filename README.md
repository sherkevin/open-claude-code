# 知行工作台

可独立交付的企业智能助手客户端与执行服务。每位成员可以配置自己的助手、知识库、MCP 和 Skills；管理员管理团队共享配置。用户在同一个工作台中选择助手、管理多个会话、处理工具审批并查看有出处的结果。产品名称可配置。

当前按华微世纪场景开发。客户只需提供内部读取数据的 MCP，直接使用我们的完整客户端；不需要自备 Harness 或自行处理模型调用。持续会话、上下文压缩、会话独立记忆和执行过程由本产品负责。性能和节省比例须用真实任务与原方案比较。

## 两份交付物

| 目录／发行包 | 内容 | 部署方式 |
|---|---|---|
| `client/`／`zhixing-client-*.zip` | React Web 客户端、构建后的静态文件、独立 Nginx 镜像定义 | 静态站点或容器，`/api` 反向代理到 server |
| `server/`／`zhixing-server-*.tar.gz` | FastAPI、Python Agent SDK 适配、SQLite、会话与文件存储、管理 CLI | Python 虚拟环境或独立容器 |

`zhixing-deployment-*.zip` 包含组合部署配置、接入与验收说明，以及 `preview.py`、`smoke_api.py`、`browser_smoke.mjs`、`benchmark.py`、`lifecycle_smoke.py` 和打包脚本。三份归档应解压到同一个目标目录，使 `client/`、`server/`、`examples/`、`docs/` 和 `scripts/` 处于同一项目根目录。原始会议资料、密钥、账号配置、运行数据库及私有日志不进入发行包。

## 产品操作

1. 在**知识库**上传公司或业务资料，查看解析状态、文件版本和检索结果。在**业务连接**添加或导入客户的 HTTP MCP，测试连接并查看可用工具。
2. 在**Skills**创建稳定的工作流程或导入 SKILL.md，再点击工作台中的助手，在详情页直接配置数据连接、工具权限、知识库、Skills 和网页搜索，点击保存。
3. 助手详情同时提供**会话**页签；点击**新建会话**选择模型和推理级别。助手配置可以复用，每个会话有独立的消息和上下文。名称、角色规则、默认模型和使用范围也可在详情页调整。
4. 描述任务，添加附件，或输入 `@` 选择本次调用的 Skill。执行记录展示模型、工具与审批进度；答案中的脚注可打开实际来源。
5. 在**会话管理**搜索标题、消息、分类或标签，按助手筛选，重命名、归档或恢复。归档保留历史；**记忆**按助手筛选并选择具体会话，管理该会话独立的记忆文件。同一助手下的多个会话可以并行执行。
6. 聊天顶部的**上下文**入口提供原生统计和手动压缩；管理员在**成员管理**维护账号，在**用量记录**查看团队汇总。

当前公开模型为 `deepseek-v4.1-flash`、`GLM-5.3`、`Dano-v2.1`。Dano-v2.1 是 Better AI 的工业智能助手，提供 `light`、`high`、`max` 三档推理级别。Dano 与 DeepSeek 可以在任务空闲时切换，Dano 推理档位也可调整。已有原生上下文的会话切入或切出 GLM 时需新建会话，原历史和长期记忆保留。模型服务配置变化时，用户明确重新连接当前模型，再自行继续任务。

MCP 服务由客户已有系统提供或实现业务适配；普通 REST 地址不会自动获得 MCP 能力。使用步骤见[工作台指引](docs/WORKSPACE_GUIDE.md)，资源组织与权限见[可配置工作空间](docs/CONFIGURABLE_WORKSPACE.md)。

## 0.4 功能范围

- 成员私有和管理员维护的团队资源；会话、附件、运行与记忆按用户授权，管理员身份不自动开放其他成员的私有内容。
- 助手绑定多个知识库、远程 HTTP MCP、具体工具、Skills 和可选 Web Search。工具仍受业务系统自身权限控制。
- 持续会话、原生上下文压缩与恢复、事件流重连、审批、中断、附件和文件产物。同一会话一次执行一个任务，同一助手的多个会话各自隔离并行。
- 原文件及不可变版本保存，PDF 文本和服务器本地 OCR、DOCX／Markdown／TXT／CSV／JSON 解析；按文件、PDF 页或文本段落检索和读取。
- 本地语义向量与关键词结合的 RAG；部署时准备模型，索引与 OCR 不把企业文件上传给新增外部服务。未配置语义模型时使用明确标识的词法检索模式。
- 三类可核对来源：数据库／表／字段／筛选与时间口径，知识文件／版本／页码或段落，网页原址／访问时间／引用片段。来源详情和原件读取时重新校验权限。
- 可视化 Skill 创建、导入、版本管理和助手绑定；`@Skill` 只应用于本条任务，不自行扩大工具权限。
- 按助手启用公开网页搜索和读取，保留网页来源。支持公开文本页面，不访问内网地址或需要登录的页面。
- 运行环境就绪、首个模型事件、首段正文和总耗时观测。供应商未提供的计费数值保留未知。

当前是一客户组织、一服务端进程部署。员工本机 MCP、OAuth、客户 SSO、多客户共用实例和任意原生扩展脚本不在当前范围内。图片可以上传预览，视觉理解按所选模型实际能力另行验证。源码与技术运维文档保留所用组件和依赖信息。

## Python＋Web 部署

需要 Python 3.10+、Node.js 22+。从项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install ./server
cd client
npm ci
npm run build
cd ..
```

服务端配置见 `.env.example` 与 `server/.env.example`。原生进程不会自动读取根目录 `.env`，需要由运行环境导出变量。关键项为：

```bash
export AGENT_DATA_DIR=/absolute/private/path/zhixing-data
export AGENT_PROVIDER_SETTINGS=/absolute/private/path/provider-settings.json
export AGENT_MODEL_CATALOG=/absolute/private/path/model-catalog.json
export AGENT_ALLOWED_ORIGINS=https://agent.example.com
export AGENT_MCP_ALLOWED_HOSTS=mcp.example.com
export AGENT_COOKIE_SECURE=true
export PYTHONPATH=server
.venv/bin/python -m app.cli init-admin --username admin
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

`init-admin` 交互式读取密码，不打印密码。`provider-settings.json` 格式为 `{"env":{...}}`，仅导入服务端支持的模型环境字段；也可直接设置 `ANTHROPIC_BASE_URL` 和模型凭据环境变量。不要把密钥写入客户端构建变量。

将 `client/dist/` 交给 Nginx 等静态服务器；参考 `client/nginx.conf`，调整后端地址，保留 SSE 的关闭缓冲与长连接配置。前端与 `/api` 使用同一个站点来源。生产 TLS 可由现有网关负责。

服务端目前必须以**单进程、一个 worker**运行；扩大并发需要独立设计执行节点调度。没有默认公开管理员密码。

日常启动、健康检查、日志、配置生效、账号凭据、停机整体备份、空目录恢复及升级回退，见 [运维手册](docs/OPERATIONS.md)。恢复须使用原数据目录路径与配对加密密钥。

## 容器部署

先准备服务器私有模型目录。以下 Linux 示例使用被源码和打包器排除的 `.runtime/config/`；也可将 `.env` 中的 `AGENT_MODEL_CATALOG_HOST` 改为现有私有 JSON 的绝对路径。不能直接把宿主路径填成容器内路径。

```bash
cp .env.example .env
install -d -m 700 .runtime/config
install -m 600 server/model-catalog.example.json .runtime/config/model-catalog.json
# 编辑私有 .env：模型凭据、catalog 宿主路径、实际 MCP 主机和站点来源。
# 编辑私有 JSON：填入已授权线路；按需要启用模型和设置默认公共 ID。
# 默认模板选择已启用的 deepseek-v4.1-flash；Dano 路由留空且禁用，
# 填好获授权线路并启用前，不要将 Dano 设为默认项。
# Linux bind mount 文件需让容器内服务账号 UID 10001 可读，保留 0600。
sudo chown 10001:10001 .runtime/config/model-catalog.json
docker compose build
docker compose run --rm server python -m app.cli init-admin --username admin
docker compose up -d
```

Compose 将 `AGENT_MODEL_CATALOG_HOST` 指定的文件只读挂载到 `/run/agent-config/model-catalog.json`，并为 server 设置对应的 `AGENT_MODEL_CATALOG`。文件缺失时不会自动创建同名目录；先准备好文件再启动。Windows／Docker Desktop 使用对应的私有文件权限，确保容器服务账号能读取，不能把文件改成公开可读来排错。原生 Python 部署继续使用实际宿主绝对路径的 `AGENT_MODEL_CATALOG`，不使用容器路径；根 `.env` 不会被原生进程自动读取。

默认监听 `127.0.0.1:8080`，生产入口通过现有网关接入。持久化数据卷包含数据库、连接加密密钥、SDK 会话、工作区与附件，应整体备份。不要只备份 SQLite 文件而遗漏加密密钥和会话目录。

私有模型目录是独立的只读挂载，须和模型凭据一起另外受控备份。修改 JSON 后等待任务结束，再重启 server；若修改 `.env` 中的挂载路径或环境变量，使用 `docker compose up -d --force-recreate server`。旧会话遇到线路或凭据变化时需明确重选模型。模板和 Compose 仅做结构验证，尚未在本次试点执行容器部署验收。

容器里的 `localhost` 指容器自身。MCP 放在同一云主机时，应配置执行容器实际可达的地址，不能直接沿用宿主机回环地址。

## Linux 开发预览

从解压后的项目根目录运行。`scripts/preview.py` 在本项目下启动独立进程和 Nginx 配置，不修改服务器现有系统服务：

```bash
.venv/bin/python scripts/preview.py start
.venv/bin/python scripts/preview.py status
.venv/bin/python scripts/preview.py stop
```

预览 client 为 `127.0.0.1:18081`，server 为 `127.0.0.1:18080`，验收用 MCP 为 `127.0.0.1:8767/mcp`。管理员密码保存在服务器 `.runtime/admin-password`，仅账号本人可读。可通过 SSH 端口转发访问预览；不会默认把预览暴露到公网。

## 验证和比较

```bash
cd server
../.venv/bin/python -m pip install '.[test]'
../.venv/bin/python -m pytest -q
cd ../client
npm test
npm run build
cd ..
.venv/bin/python scripts/smoke_api.py --password-file .runtime/admin-password --origin http://127.0.0.1:18081 --report .runtime/api-smoke.json
```

真实模型与工具验收需额外加 `--real-model`，会调用配置的模型服务并产生用量。验收用 MCP 与知识库独立保存样例，正式助手由用户选择自己的资源。

逐任务比较可使用：

```bash
.venv/bin/python scripts/benchmark.py \
  --password-file .runtime/admin-password \
  --assistant-id YOUR_ASSISTANT_ID \
  --tasks docs/acceptance-tasks.example.json \
  --label current --output .runtime/benchmark.json
```

先确认助手允许对应只读工具。脚本不会自动批准待审批操作；输出 JSON、CSV 和运行轨迹。任务成功状态与业务正确率分开，业务正确性需依据客户标准答案审核。对比时保留相同模型、任务、工具和数据，单独记录模型差异。DeepSeek 价格未核实时金额为未知，不使用 Claude 估价替代账单。

0.4 的实际测试、浏览器操作、模型调用和部署结果统一记录在[交付验证记录](docs/DELIVERY_VERIFICATION.md)。文档中的功能说明和验收脚本不代替已执行结果。0.3 的多模型与上下文记录保存在[历史验证](docs/verification/DELIVERY_0_3_0.md)，0.2 的重启和原生记忆记录保存在[历史验证](docs/verification/DELIVERY_0_2_0.md)。

0.4 配置资源后，可用 `scripts/workspace_04_smoke.py` 检查公司知识、数据来源、网页来源和 Skill 调用；必须显式加 `--real-model` 才执行模型任务。客户实际 MCP、业务样本和原方案基线需单独验收。数据连接的来源字段约定见 [MCP 来源契约](docs/MCP_SOURCE_CONTRACT.md)。

## 打包

0.4.2 修正会话并发、记忆隔离和切换模型后的身份查询；0.4.1 已修正助手详情配置入口与保存冲突。0.1.0—0.4.1 正式归档保持不变。先检查本版收录文件，完成对应版本的 FINAL 验证记录后再生成正式包：

```bash
python scripts/build_release.py --version 0.4.2 --stage staging --check-only
python scripts/build_release.py --version 0.4.2 --stage final
```

暂存包名称和清单明确标注 `staging`，不代表已完成最终验收。正式打包使用 `--stage final`，要求交付验证记录为同一版本且标记 FINAL；已有正式发行目录拒绝覆盖。

0.4.2 正式包目标目录为 `dist/releases/0.4.2/`；只有完成正式打包后该目录才表示本版交付。尚未完成验证时使用独立暂存目录。`manifest.json` 包含每份归档的大小、SHA-256 和完整文件清单；相同源文件产生相同归档摘要。打包器使用文档／脚本白名单，排除私有和运行目录，检查明显密钥标记并重新读取归档验证内容。

收件人应先核对摘要，再将三个归档解压到同一个目录。Linux 可使用 `sha256sum`，Windows 可使用 `Get-FileHash -Algorithm SHA256`，与清单逐项对照。管理员账号、模型凭据、MCP 凭据与站点域名均在部署时单独配置，不随包提供。

交付后的接入顺序为：配置模型及 MCP 网络访问 → 创建管理员 → 启动 server 与 client → 在页面测试连接并发布助手 → 运行 API／浏览器检查 → 用客户真实任务验收。接口定义见 `docs/API_CONTRACT.md`；服务运行时可查看 `/docs` 的 OpenAPI 说明。
