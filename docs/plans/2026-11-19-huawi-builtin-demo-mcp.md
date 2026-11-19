# 华微连接器 1.3.8：随包内置测试 MCP 与公网链路自检

日期：2026-11-19（北京时间）
总控：本 ChatGPT 会话；用户已要求立即实施，并保持“开源优先、复用优先”。
工作树：/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118
基线：c55c6b3e1d22dd14e080cfaf2da1fc6ef89259a7；不要回退现有 1.3.7 诊断功能。
项目：abe1e80a-e4f4-40a4-9f9f-7e19f5261d73
连接器工程师：mcp:35201df6-4e4c-4291-8288-fb688503e0b6
服务端工程师：mcp:c7567803-0e7d-47df-8319-9f457ee3ac09

## 背景与交付目标

开物是公网智能体平台；华微世纪的 MCP 在 Windows Server 2019 Datacenter x64（客户资料 Build 17763.9020）内网运行，只能主动出网。桥接器使用原版 FRP，客户自己的模型/平台不需迁移。
用户新增明确要求：安装连接器时，一起安装并自动启动一个测试 MCP server，让客户先用已知正确的示例服务验证我方链路，再接自己的 MCP，快速区分问题所在。
当前已发客户的是已验证的 1.3.6；1.3.7 日志代码已在基线内但 Windows 安装验收未完成。本次增量使用 1.3.8，不覆盖旧版本下载文件。

## 用户实际体验

1. 双击安装器，完成普通 Windows 管理员确认，图形配置页自动打开。
2. 内置测试 MCP 随后台服务自动启动，不需要另装 Python、Node、Docker，不要求客户写 JSON 或配置端口。
3. 页面顶部显示“内置测试服务（示例数据）”，明确提示仅用于验证，不是华微真实业务数据。
4. 未绑定时可验证本机示例服务；绑定企业后点击“运行连接测试”，自动使用独立示例来源，完成真实公网 Edge 往返验证。
5. 展示分层结果：本机测试服务、企业绑定、隧道、公网 MCP 读取；未执行项不能标绿。
6. 给出可复制的开物测试问题和固定正确答案；最后开物真实对话必须独立验收，不把 Edge 往返冒充智能体对话成功。
7. 客户随后使用原表单接入自己的 MCP；测试来源与业务来源并存，不能覆盖、禁用或删除现有业务配置。
8. 测试服务可以单独关闭/再次开启，关闭不会停掉其他 MCP 或企业隧道；保存用户的关闭选择，升级不强行重新开启。

## 架构决策与复用

- 保留官方 FRP 0.61.1、现有 NSIS、DPAPI、云端设备签名、来源注册、授权白名单、Edge、diagnostics、现有 UI。
- 测试 MCP server 协议复用官方 github.com/modelcontextprotocol/go-sdk；查验并固定与可用 Go 工具链及 Windows Server 2019 兼容的正式版本，保留实际该版本 LICENSE/NOTICE，不使用浮动 @latest，不自行重写 MCP JSON-RPC/会话协议。
- 已查官方 StreamableHTTPHandler 可直接复用；许可证当前主线有 MIT/Apache-2.0 过渡，按所选固定版本实际文件记录，不武断写全部 MIT。
- 复用现有 synthetic fixture 的示例业务数据/断言；只重写必要的产品封装，不把旧测试 harness 全部打入客户包。
- 推荐将 SDK handler 封装为 connector/internal/demomcp，作为 KaiwuConnector 进程的独立 loopback HTTP listener，由现有 Windows 服务管理生命周期。无需第二个系统服务或客户额外运行时。
- 只监听 127.0.0.1。由系统分配空闲端口或处理固定端口冲突并更新内部示例来源地址；重启保持来源ID，不重复创建；不能因为测试端口失败而拖垮客户业务服务。不能使用公网监听、入站防火墙放行或临时隧道绕过实际 FRP。
- 所有 VIP 代码、缓存、构建、日志和临时文件放 /media/data3/dengkw 下；不要使用根分区 /tmp 或改 HOME/CODEX_HOME。

## 共同接口契约（两工程师按此并行）

示例工具只读，工具名固定：
- kaiwu_demo_ping，参数 nonce 字符串（最多128字符），返回 demo=true、marker=KAIWU_BUILTIN_DEMO_V1、原样 nonce、当前 UTC 时间和进程启动标识，不包含机器名、用户名、真实文件/数据库信息。
- kaiwu_demo_order_summary：固定示例订单汇总，样例订单数 12，标注这是示例数据。
- kaiwu_demo_inventory_query：固定产品 KAIWU-DEMO-001 库存 128，标注这是示例数据。
可复用现有样例数据结构，但上述名称与断言固定，双方共享此契约；不能读真实数据。

本地来源名称固定“开物内置测试服务（示例数据）”；ID使用现有来源ID规则生成后持久化，可在本地状态中明确记录 builtin_demo_source_id。未经企业绑定不注册云端，绑定后按现有单源追加方式注册并批准这3个工具。不得覆盖其他来源或用户白名单。

新增云端入口（沿用现有 /api/kaiwu 前缀及响应 envelope）：
POST /v1/connectors/{connector_id}/self-test
请求：{ "source_id": "已注册的本连接器示例来源ID", "nonce": "随机短字符串" }
鉴权：复用现有设备签名校验，校验 caller、connector_id、tenant_id 与 source归属一致。仅允许已登记并批准 kaiwu_demo_ping 的内置测试来源。
不接受用户提供的 URL、工具名、任意工具参数、租户、密钥；固定仅调用 kaiwu_demo_ping(nonce)。
必须通过实际公网 HTTPS Edge URL、现有授权及 FRP 路径回到本机 HTTP MCP server，执行 initialize / tools/list / tools/call。不得在云端构造成功样例或直接调 handler冒充往返成功。
优先复用现有安全调用和集成凭据机制；若需临时 K，只在服务端内存中持有、严格限制到本 tenant+source+kaiwu_demo_ping，设置最短可用有效期，finally撤销，不传到浏览器/客户端、不记日志。
响应 data：{ "ok": boolean, "trace_id": string, "source_id": string, "stages": [{ "id": string, "status": "passed|failed|skipped", "message": string }], "probe": { "demo": true, "marker": "...", "nonce": "...", "server_time_utc": "...", "instance_id": "..." } }
失败返回合适HTTP及上述可读分层原因，业务错误不得伪装 passed；控制接口尚未部署/不支持时显示明确提示。
目标Edge URL由服务器受信配置构造，不能构成任意URL代理。
本地 /api/self-test 使用现有 Origin/CSRF/method防护；先验证本机示例服务，再检查绑定与隧道，调用上述云端入口，显示 trace_id。不上传客户查询结果或凭据。

## 已发现必须一并修正的生命周期缺陷

connector/cmd/connector/service_windows.go 的 SCM Execute 当前 newServer/Serve/AutoConnect，却未调用 StartDiagnostics、RecordStartup、Close；main.go serve() 才有这些调用，SCM模式绕过serve。
本轮把示例MCP启动/停止和诊断启动/停止提取为共用生命周期，并同时接入 CLI 与 SCM，确保停服务/卸载后子监听释放、日志正常结束，重启后只启动一个示例实例与上报线程。
不要只在手动 serve 路径做成功演示。Windows SCM路径必须有实机证据。

## 工程分工与提交边界

连接器工程师独占：connector/ 源码、Go依赖与license打包、UI、NSIS/构建及Windows验收脚本；本轮暂不执行Windows安装或服务变更。
服务端工程师独占：server/ 设备鉴权self-test和对应测试；不改connector/、不部署腾讯云、不碰Shervin。
各自只提交自己文件，禁止git add .，保留既有工作区未提交文档；使用同一工作树但目录严格隔离。
总控负责接口审查、整体验证、云端最小增量部署、最终1.3.8打包、Windows验收和客户文档更新。
旧import工程师已停止且队列为空，不恢复旧长任务；沿用正确线程现有 deepseek/deepseek-flash 设置，不换模型。

## 实施顺序

A. 工程师先在各自范围完成实现和必要测试，不等客户数据。
B. 总控审查接口与租户隔离、生命周期、日志脱敏；Linux隔离完整往返验证，构建1.3.8候选并记录commit/SHA。
C. 只部署已审查的最小云端增量；既有公网产品/腾讯云使用已由用户在本项目授权，不能借机改其他服务或数据库。
D. Windows最终安装由总控串行协调。此前未签名安装器自动审批明确拒绝了1.3.7安装，不换工具/命令/portable方式绕过；先完成可审查候选和一条标准安装命令，若仍需明确确认由总控说明具体原因并请用户执行/批准。
E. Windows实际验收完成后才称“已验证可交付”，更新截图与简单使用说明，再按用户既有授权发送华微世纪群。不能把Linux构建成功当Windows安装成功。

## 必要验收（不做无关全量回归）

- 干净安装：无客户配置/无Python/Node也能自动启动示例MCP，UI可打开；SCM模式也有diagnostics。
- 升级：已有企业绑定、DPAPI凭据、业务source和白名单保留；示例来源只追加一次。
- 生命周期：SCM停止/启动后无孤儿监听或重复线程，关闭示例不影响业务源。
- MCP：真实协议握手、tools/list与3个工具结果；nonce往返与当前时间验证防静态假绿。
- 云端：错设备签名、他租户/他连接器来源、任意URL或任意工具请求均不能越权；临时凭据收尾。
- 故障分层：本机示例失败；未绑定；隧道断开；云端HTTP/API错误；Edge调用失败；成功各有准确提示与trace。
- 端口冲突/示例关闭不拖垮业务源；日志/导出不含内部Bearer/HMAC、K、接入码或业务数据。
- Shervin Windows11实机与客户Server2019目标差异准确记录，不冒称Server2019已测。
- 公网开物真实对话返回订单数12、KAIWU-DEMO-001库存128并明确示例；断开示例后失败，恢复后成功。工具调用证据、Trace与安装包SHA关联。
- 不兑换已写入华微客户说明的真实一次性接入码做测试；测试使用原独立合成租户。

## 工程交付

docs/evidence/huawi-builtin-demo-20261119/ 下分别记录 CONNECTOR_REPORT.md、SERVER_REPORT.md，必要精简测试证据及兼容/许可证记录。包含实际commit、通过/未执行、下一步准确命令。不要只交设计、不无限审查；做完实现和必要验证后停在规定边界回报。

## 总控补充：隔离示例测试，防止坏的客户来源阻断（开始实现前/提交前均检查）

只读复核发现 Connect/registerSources 遍历所有启用源，其中任一客户工具发现失败会使整次连接失败。示例测试必须采用仅本示例来源的非破坏范围，不能为了测试修改客户source.enabled或删除客户源；若已有业务隧道运行，应保留现有路由并只追加/更新示例路由。不要出现“客户MCP坏了，示例测试也被挡住”的耦合。

现有 Diagnose(...,probe) 强制动态鉴权，必须按实际source认证方式判断；无HMAC的内置示例应正常试调用，不硬塞客户签名参数。

云端示例身份不能只凭可编辑的显示名称。共同确定保留source ID算法：完成企业绑定后，source_id = "src_" + SHA256(("kaiwu_builtin_demo_v1:" + connector_id) UTF-8).hexdigest()[:32]（总长36，沿用既有src_32hex校验；禁止扩宽来源ID格式）。本地未绑定也自动启动示例监听并允许本机测试；绑定后按此ID幂等追加示例源。self-test服务器自行重算该ID并要求请求source_id匹配，校验该source属于认证设备/租户、已登记的只读工具白名单限于3个 kaiwu_demo_* 固定工具。不要接受其他source进行自检，不接受请求URL/工具参数。本地保留示例标记，若预期ID竟被非示例源占用则报冲突，不能覆盖。设备重新绑定只迁移自身示例记录，不能挪用旧企业来源。
