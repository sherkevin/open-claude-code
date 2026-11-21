# 1.3.7 客户文案与双端诊断日志：实施任务

日期：2026-11-19；总控：当前 ChatGPT；执行工程师：import:codex:01a1d40f-0e5e-7382-8865-79bbb09e28c2。
项目：/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118。
沿用当前 DeepSeek 主力模型。不要改 provider/model，不要重开项目或替换 FRP。

## 0. 最新事实与权限

先读 docs/evidence/huawi-shervin-public-e2e-20261119/FINAL_ACCEPTANCE_1.3.6.md 和 docs/plans/2026-11-19-huawi-shervin-current-supervision-state.md 的最新段落。
你的 run 86 所说“Shervin 仍为1.3.3、尚未验收”已经过时：总控已安装 1.3.6 并完成真实 Windows 回归，passed；真实公网开物 A=137 → 暂停桥接而本地MCP仍活着时调用失败 → 改B=308后重连返回308，全部通过。
现有绑定、数据源、K 与助手必须保持。source=src_40138190e7218f45fe610ed77fe342a6；connector=conn_effca7dc22704c8e9fcd7f472c447c46。
当前K已更新至2026-11-20 14:45:37北京时间。不能轮换/撤销现有真实绑定、来源或K。
用户这轮明确授权把诊断日志近实时发送到“我们自己的腾讯云后台”，不是要求只做方案。学校云默认规则不阻止本次已授权的腾讯云日志能力；但部署由总控在审阅后执行。
所有开发、缓存、临时、构建、下载与日志仅写 /media/data3/dengkw，不写根分区或/tmp。
你负责代码、针对性测试、打包与可审阅的部署方案；总控负责审查、腾讯云实际更新、Shervin安装/验收。不要操作Shervin、UAC、Windows服务或浏览器，以免并发冲突。
保存本次阶段状态为 docs/evidence/huawi-diagnostics-20261119/IMPLEMENTATION_STATUS.json，并在每个阶段完成时更新；不要长时间只分析。

## 1. 用户要交付的东西

A. 整个连接器页面改成面向“正在使用工具的客户”说话。
B. 客户本地页有“连接器日志”和“开物调用日志”，可读、可筛选、自动刷新、复制错误编号/下载脱敏日志。
C. Windows连接器将操作、状态变化和错误近实时发送到我方现有腾讯云控制面，便于开物技术支持远程排障。
D. 腾讯云有可实际打开的运维诊断页面与受认证查询接口，而非仅在文档中写一个未接入的API。
E. 完成后生成1.3.7 Windows x64图形化安装包。官方frpc0.61.1与已有协议继续复用。
“所有动作/报错”指连接器自身操作、状态、工具调用结果等诊断元数据，不是扫描整台Windows，也不是上传客户MCP原始业务数据/所有stderr文本。

## 2. 文案要求

清理 page.go 等客户可见界面中的“客户提供”“客户的MCP”“填写客户要求”等第三人称表述，改为“您”“您的”“当前服务器”“已有的MCP服务”。
建议原始JSON说明：
“粘贴您已有的 MCP 配置，支持 mcpServers 格式或单个 HTTP MCP 对象。连接所需的访问凭据会加密保存在当前服务器，不会写入导出的配置或诊断日志。”
Bearer/channel/userid/HMAC帮助文案同样改成“填写您的MCP服务要求的…”。
不要改动导入替换全部来源的实际语义；保留明确提示，不把危险的整表替换伪装成增量。
主操作流程保持现有表单、工具勾选与“保存并连接”。不要借此重做整个产品。
把“企业集成凭据K”等实现术语尽量收进高级详情，普通提示改成“开物平台的接入授权由开物技术支持完成”，不要伪称尚未确认的平台状态已成功。
增加清晰说明：
“为便于开物技术支持排查连接问题，连接状态、操作结果和错误代码会自动上报至开物服务，不包含访问密钥、查询参数或业务数据。”
本地页面显示诊断上报状态、最后成功时间、待发送条数、丢弃条数；提供暂停/恢复诊断上报开关，暂停不影响MCP连接。默认开启，说明必须可见。

## 3. 最小架构，优先复用

复用 Go 标准库 log/slog（BSD-3-Clause，已在Go工具链许可范围）、现有 cloud.Client、DeviceToken+Ed25519 签名、store/secretfile、本机会话与Origin保护、FastAPI/Python logging、现有SQLite控制面。
已比较 OpenTelemetry Collector（Apache-2.0）agent/gateway方案。它可用于未来统一收集，但此单客户MVP不引入第二个Windows服务、OTLP端口或额外collector/数据库，避免扩大安装与部署面。本次自定义部分限于产品事件目录、现有设备认证适配、有限本地待发队列和界面。
官方依据：
- https://pkg.go.dev/log/slog
- https://github.com/golang/go/blob/master/LICENSE
- https://opentelemetry.io/docs/collector/deploy/agent/
- https://opentelemetry.io/docs/collector/deploy/gateway/
- https://github.com/open-telemetry/opentelemetry-collector/blob/main/LICENSE
在实施记录中保留选择/拒绝理由与可替换边界。继续复用未修改的官方FRP。

## 4. 事件和隐私边界（必须先实现）

事件只允许明确字段：schema_version,event_id,occurred_at,level,component,event_code,message_code,operation_id,trace_id,source_id,tool_name,http_status,rpc_error_code,duration_ms,attempt 等必要标量；tenant_id/connector_id由服务端认证身份补齐，不信客户端任意声明。
不允许任意details字典、自由message、原始error、原始URL/query、headers/body/arguments/result/schema/会话正文/文件内容/环境变量/命令行/设备用户名等进入诊断上传。
message由固定事件目录生成中文说明；错误按已知类别归一化，unknown归一为固定“操作失败，请提供事件编号”。工具名只允许当前来源已批准工具名并限长。
密钥包括MCP Bearer、HMAC、device token/private key、K、隧道R/T、Cookie、Authorization，任何编码/大小写形式不能进入日志或导出。必须在本地入队前过滤，并在服务端再次校验。
历史隐私风险一起修：startHeartbeat目前上传status.LastError；diag的err.Error/RPC上游message、cloud非标准响应body、supervisor原始stderr可能含内部地址/凭据。不能把它们原样传到新的日志系统；heartbeat.last_error也要使用相同固定安全错误分类。
不要把原始stderr/service.log整文件传云端。frpc启动/退出/重试/重连用类型化事件记录，捕获的原始字符串仅用于本地分类，不持久到新日志。
接口输入限长、batch限量、未知字段拒绝/明确剔除；所有前端文本使用textContent避免XSS。

## 5. 采集范围

覆盖：连接器启动/退出/版本；配置导入/保存/来源启停；工具目录读取；企业绑定成功失败（不含接入码）；连接/暂停/断开；诊断阶段成功失败；FRP启动/退出/重试/恢复；上游MCP请求开始/结束/超时/401/403/断连；日志上传恢复/队列丢弃摘要。
已有线上状态不能被日志功能改变。UI事件在执行成功或失败时都留记录，不能只记按钮点击。
平台日志覆盖本次MCP链路的控制面/Edge请求是否到达、来源、方法/已授权工具、状态、耗时和错误类别。暂不扩成聊天正文/LLM prompt或全站观测系统。

## 6. 本地队列与实时性

近实时目标：联网正常时<=5秒批量发出，关键错误可立即唤醒发送；独立后台goroutine，不能阻塞MCP或UI请求。
上报走现有HTTPS控制面，与FRP生命周期解耦；暂停FRP之后仍可上报“暂停”事件。
复用现有设备签名HTTP客户端；超时3秒左右、指数退避带抖动（最大60秒），不能热循环。
磁盘待发队列有界，建议<=5000条且<=5MiB；本地日志<=10000条且<=10MiB，按更先到达的边界淘汰，公开dropped计数。
待发event_id持久化；ack后删除；网络失败/服务重启保留；服务端按(connector_id,event_id)幂等去重，重试不重复。
队列满/磁盘不可写/上报401/404/5xx均不能阻断主连接和工具调用；本地页面给可理解的固定提示。上报失败日志不能递归产生无限日志。
未绑定可本地记录，绑定后只发送该设备正常生命周期事件；撤销或换绑时不能把前企业队列发给新企业。记录关联企业身份并清空/隔离不匹配的待发记录。
云端保留14天并设置行数/请求容量上限（给出实际值），通过轻量周期清理/索引控制容量。不要无界写满磁盘。

## 7. 接口与权限

建议实现（允许依据源码保持清晰同等路由）：
- POST /v1/connectors/{id}/events：复用现有设备Bearer+Ed25519，按已认证设备保存batch。
- GET /v1/connectors/{id}/platform-events：相同设备认证，仅允许读取自己连接器对应的脱敏Edge/控制面日志。
- GET /v1/diagnostics/events：既有admin依赖保护，支持tenant/connector/source/level/component/trace/time/cursor过滤并限量。
- 本机 GET /api/logs，GET /api/platform-logs，以及诊断上报设置POST：复用已有localhost Host/Origin/会话保护。
现有签名不包含query string，因此过滤条件不得作为跨设备授权依据；作用域只从路径与认证绑定确定。禁止设备token访问管理员全量日志。
Trace由Edge为每个请求生成 kwtr_<随机hex>，覆盖外部传入的同名header；经已保护的FRP路径送到authproxy，仅用于日志关联。authproxy不把该内部诊断header转发到客户MCP。
两侧事件使用同trace_id；如果上游HTTP200但JSON-RPC返回error/isError，应按已有可解析响应路径记录工具失败，不能全标成功。不得为了日志吞掉/全缓冲SSE，保持现有流式协议。

## 8. 图形化页面

本机在普通配置后加可折叠“运行日志”：两页签“连接器日志”“开物调用日志”。时间（带时区）、级别、动作、结果、耗时、错误/关联编号；可筛级别、刷新/自动刷新、下载脱敏JSONL或JSON，滚动不会被轮询打断。
平台日志网络不可达时给提示，不影响连接器本地日志查看。不要显示云端管理凭据。
腾讯云控制面新增轻量 /ops 静态运维页（部署后通常为 /api/kaiwu/ops），不改/发布整个开物React前端，避免覆盖其他项目工作。
/ops未登录只能看到空登录壳；password输入现有控制面管理员Bearer，只在JS闭包内存中保留，以Authorization头调用现有admin保护的API；不写URL、localStorage、sessionStorage、Cookie或HTML默认值，刷新清空。
页面/API no-store，CSP限制同源并无第三方脚本，日志只textContent渲染。支持筛选、近实时刷新、复制trace、下载当前脱敏结果。只读页不要新增管理写权限。
此运维页供开物团队使用，客户只能在本机查看自己的日志。

## 9. 必须给出的有意义测试

后端/Go自动测试：
1. 成功及失败动作确实记录；敏感Bearer/HMAC/内部地址/业务内容canary出现在上游报错时，不出现在本地新日志、heartbeat payload、上传batch、DB/API/导出。
2. 上传失败后重试成功；重启后补传；相同event_id重复不重复入库；队列上限与丢弃计数；失败不阻断正常MCP。
3. 未认证/错设备/跨企业/超大batch/非法字段/非法trace不能查询或污染别人的日志。
4. Edge→authproxy→云端事件同trace，成功、上游401、超时/断线、暂停FRP负向可区分。
5. 暂停FRP后日志仍能上报；暂停诊断上传不影响MCP。
6. UI日志和后端过滤基本行为、没有第三人称客户文案，无需给纯文字替换单独写镜像测试。
执行既有受影响Go测试、Python控制面/Edge测试和go vet。保持已通过的包/现有真实配置不变。
Windows由总控做目标明确的覆盖升级、GUI/日志、真实MCP成功/失败/重连及腾讯云到达验证。不要求又跑无关整套历史平台测试。

## 10. 阶段与交付

先实现事件/队列/安全过滤+后端接收查询，再加trace/界面和测试，最后打包。
每阶段更新IMPLEMENTATION_STATUS.json，明确已做、正在做、测试结果、真实未测项；完成代码后及时产出CODE_READY供总控review，不要等最后才反馈。
提交仅本任务自己改动，保留已有dirty文档/node_modules，禁止git reset/clean与改既有历史发行包。
候选版本1.3.7；Windows图形化EXE、ZIP、包内SHA256/SBOM/原FRP许可/完整部署与回退说明。
最终 PACKAGE_READY.json 必须包含新包hash、提交、可下载URL与所有测试证据路径；Windows及Server2019未测如实标注。
把候选包发到新的不可变 /downloads/kaiwu/1.3.7-<commit>/ 可继续沿用已授权发布流程；不得覆盖1.3.6。
云端代码升级只准备具体可回滚步骤和必须更新的文件列表，指明SQLite新增表/索引与兼容性；不得替换整个开物主站或改huawei账号权限/域名白名单。总控执行实际服务更新。
当你完成工程任务后停在可审查交付状态，不操作Windows；不要把“代码存在”报成“实际云端日志已经收到”。

## 总控中途审查补充

总控已新增 docs/evidence/huawi-diagnostics-20261119/SUPERVISOR_REVIEW_01.md；打包前逐条修复并验证。总控独立新增的 connector/internal/diagnostics/supervisor_review_test.go 是本任务验收用例，可纳入提交，不能删除用例以绕过失败。实现文件继续由你独占。
