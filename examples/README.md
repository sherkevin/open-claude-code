# 演示 MCP 与验收

这些文件提供可重复的产品验收环境。知识条目、报销金额、响应时间和工单全部是合成演示数据，不代表任何客户的实际制度，也不连接客户生产系统。

## 启动演示 MCP

使用服务端的 Python 虚拟环境和已固定的依赖（`mcp==1.30.0`）：

```bash
cd /path/to/zhixing-project
.venv/bin/python examples/demo_mcp.py
```

默认地址为 `http://127.0.0.1:8767/mcp`，只接受本机连接。Agent server 与 MCP 在同一腾讯云主机时可直接使用此地址。若分别运行在不同容器，`127.0.0.1` 指向各自容器，需按部署网络配置地址，不能直接复制本机地址。

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DEMO_MCP_HOST` | `127.0.0.1` | 监听地址 |
| `DEMO_MCP_PORT` | `8767` | 监听端口 |
| `DEMO_MCP_DATA_DIR` | 项目下 `.runtime/demo-mcp` | 演示工单 SQLite 存储 |
| `DEMO_MCP_TOKEN` | 无 | 可选的共享 Bearer token；非回环监听要求至少 24 字符 |
| `DEMO_MCP_ALLOWED_HOSTS` | 无额外值 | 逗号分隔的额外 Host 白名单，如 `demo-mcp:*`；不含协议 |

非回环监听必须配置足够长度的 token，且明确设置可接受的 Host。此 fixture 的 Bearer 校验仅用于验收，不实现个人 OAuth 或生产业务权限体系。不得把无认证的演示服务开放到公网。

工具：

- `search_knowledge(query, limit=3)`：只读查询，返回 `DEMO-KB-*` 来源编号。输入“设备报修”可查到普通故障 4 个工作小时响应的演示规则。
- `create_ticket(title, description, requester, request_key)`：写入一张演示工单。`request_key` 唯一，相同键和内容重复调用不会重复创建；相同键但内容不同会报错。它应配置为需要审批。
- `list_tickets(request_key="", limit=20)`：只读查询真实持久化的演示工单。
- `slow_lookup(delay_seconds=15)`：只读、最多等待 30 秒，仅供中断验收。

工具注解是说明信息，不是权限控制。产品服务端仍必须执行助手的工具授权和人工审批。

## 从客户端配置

1. 管理员进入“业务连接”，添加 HTTP 连接并填写上面的地址。
2. 若启用了 `DEMO_MCP_TOKEN`，选择 Bearer 并通过受控方式填入 token；否则仅在本机隔离场景选择无认证。
3. 测试连接，应发现四个工具。
4. 建立“演示 QA 助手”，绑定这条连接。知识查询、工单查询和慢查询可自动批准；创建工单保留人工审批。
5. 员工打开工作台，查询演示报修流程，然后要求创建演示工单并确认审批。

## 运行 HTTP 验收脚本

先启动产品 server 和演示 MCP。server 的 MCP 目标地址策略必须明确允许这个回环地址。脚本不修改 server 的地址策略，也不会替用户打开网段。

管理员凭据只从以下来源读取：

- `SMOKE_ADMIN_USERNAME` 和 `SMOKE_ADMIN_PASSWORD` 环境变量（用户名默认 `admin`）；或
- `--credentials-file` 指定的私有 JSON 文件，其结构为 `{"username":"…","password":"…"}`；或
- `--password-file` 指定的仅含密码的私有 UTF-8 文件，用户名通过 `--username` 指定，默认 `admin`。

凭据文件只允许运行用户读取，不提交 Git，不放进分发包。项目预览启动器生成的是 `.runtime/admin-password`，可以直接使用。

最小验收不会调用模型：

```bash
.venv/bin/python scripts/smoke_api.py \
  --base-url http://127.0.0.1:18080 \
  --origin http://127.0.0.1:18081 \
  --password-file .runtime/admin-password \
  --report .runtime/api-smoke.json
```

若前端与 server 的来源地址不同，用 `--origin` 指定 server 已允许的前端来源；不要为了通过测试放开任意来源。

显式启用真实模型验收：

```bash
.venv/bin/python scripts/smoke_api.py \
  --base-url http://127.0.0.1:18080 \
  --origin http://127.0.0.1:18081 \
  --password-file .runtime/admin-password \
  --real-model \
  --report .runtime/real-model-smoke.json
```

`--real-model` 发起四轮真实模型任务，会消耗配置的模型供应商用量：

1. 查询知识并核对来源和已知答案；断开一次事件流后重连，再验证完成后的事件重放。
2. 批准创建工单，通过真实 MCP 查询确有一条符合参数的记录。
3. 拒绝创建工单，通过真实 MCP 查询确认没有记录。
4. 在慢工具开始后中断运行，核对最终状态。

所有模式都会测试登录、普通成员、连接工具发现、助手可见性、会话和文件归属、附件字节一致性以及用量可见性。重复消息请求使用同一个 `client_request_id`，必须返回同一运行。其他用户不得访问会话、文件、运行或审批。

脚本创建带唯一验收标识的连接、助手、用户和会话。默认结束时停用连接、助手与验收用户，并归档会话；`--keep-resources` 可以保留它们供检查。上传文件和合成工单保留为验收证据，不删除任何无关记录。验收用户随机密码不会打印；需要人工登录时由管理员重设密码。

报告只包含状态、耗时、资源 ID 和汇总用量，不包含密钥、口令、完整模型文本或原始工具参数。脚本失败时返回非零退出码，详细服务错误应在受控服务器日志中查看。

## 运行真实浏览器验收

安装 Playwright 到项目的 `.local/browser`，使用本机 Chrome；也可通过 `BROWSER_EXECUTABLE` 指定浏览器可执行文件。浏览器访问客户端地址，MCP 地址则由服务器访问；通过 SSH 隧道访问预览时，这两个回环地址属于各自运行环境。

```bash
npm install --prefix .local/browser playwright
node scripts/browser_smoke.mjs --password-file .runtime/admin-password
```

默认客户端地址为 `http://127.0.0.1:18081`，可用 `--base-url` 或 `SMOKE_BASE_URL` 调整。凭据来源与 HTTP 脚本一致，亦支持 `SMOKE_PASSWORD_FILE`；不要通过命令行直接传入口令。若演示 MCP 使用认证，通过 `DEMO_MCP_TOKEN` 环境变量提供 token。

脚本操作实际 DOM：登录、创建并测试 MCP 连接、给助手配置三个只读工具免确认与写工具审批、创建并重命名会话、上传和预览合成附件，以及访问成员和用量页面。随后使用同一登录态，在 390 像素宽移动页面检查工作台、连接与聊天，并断言没有横向溢出。桌面宽度为 1440 像素。

设置 `REAL_MODEL=true` 会增加一轮真实知识查询，核对页面显示的答案、来源与工具执行记录；该步骤会产生供应商用量。默认不调用模型。脚本不会伪造 API 响应或页面截图。

输出为 `.runtime/browser-smoke.json` 与 `.runtime/screenshots/*.png`，可用 `BROWSER_REPORT`、`BROWSER_SCREENSHOTS` 修改。初次未登录时的 `/auth/me` 401 单独记录；其他 HTTP 错误、JavaScript 错误、控制台错误和非预期网络失败均导致验收失败。截图隐藏密码输入框，报告会清除配置中的口令与 token；这些私有验收文件仍不应提交或公开。

新资源均以“浏览器验收”开头。默认结束时归档会话、停用助手和连接；`KEEP_RESOURCES=true` 可保留。上传的合成附件保留作为证据。脚本不删除无关资源，也不修改产品源码。

Windows 开发机可用 `server/.venv/Scripts/python.exe scripts/local_browser_check.py` 启动独立验收环境。它要求客户端已构建，使用 8768、18082、18083 三个空闲回环端口，将随机管理员凭据、隔离数据、日志和截图保存到 `.runtime/local-browser/<本次标识>/`。它清除子进程继承的模型供应商配置，不主动提交模型任务，并断言附件操作未触发消息提交。结束时只清理自己启动的进程树，不占用腾讯云预览的数据或端口。此结果只证明本地客户端与服务端的实际交互，不能替代远程部署和真实模型验收。

## 留下可体验的演示助手

演示 MCP 和产品服务启动后，管理员可以通过正常管理 API 建立一个明确标注合成数据的助手，不发起模型请求：

```bash
.venv/bin/python examples/configure_demo.py --password-file .runtime/admin-password
```

脚本只复用或新建名为“演示业务连接”和“业务演示助手”的配置；遇到同名但不同地址的连接会停止，交由管理员检查。知识与工单查询自动执行，创建演示工单仍需审批。客户真实 MCP 应另行在“业务连接”页面配置。

## 验证边界

同一会话连续执行、空闲进程回收和服务重启后的恢复，使用独立的四轮验收流程，见 [`docs/verification/SESSION_LIFECYCLE.md`](../docs/verification/SESSION_LIFECYCLE.md)。脚本须显式启用真实模型，并同时核对 SDK 会话编号与服务器／执行进程身份。已有 API 冒烟和浏览器验收不自动覆盖这些情形。

本地无模型单元验证命令：

```bash
python -m unittest discover -s examples -p 'test_*.py' -v
```

覆盖演示数据来源、工单幂等性、冲突拒绝、无效输入不写入、非回环认证约束，以及事件解析与工具名匹配。

- 无 `--real-model` 的通过结果不代表真实推理、审批或中断已经通过。
- HTTP 脚本不替代浏览器交互与视觉检查；浏览器脚本和截图可提供相应证据。两者均不替代进程重启后的会话恢复测试或真实客户 MCP 接入验收。
- 演示服务器不实现多租户业务数据隔离；它只供受控验收，正式权限由客户的业务 MCP 执行。
- 服务协议约定在 `docs/API_CONTRACT.md`。若产品协议发生变化，应同步更新脚本，不应放宽断言掩盖不兼容。

实现依据：[官方 Python MCP SDK v1 文档](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。
