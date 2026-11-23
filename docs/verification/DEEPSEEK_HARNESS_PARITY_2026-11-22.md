# 开物与 DeepSeek Harness 体验问题清单

2026 年 11 月 22 日，按开物正式工作台、固定版官方 Harness 和最新官方预览发行说明完成两轮对标。清单共 32 项，含 7 项 P0。优先级由本次审计按使用影响给出。P0 阻断当前工作，或影响业务结果的可信度，P1 影响日常使用，P2 属于扩展功能或旧入口兼容。

- 开物正式站版本为 `20261122-hk-harness-mcp-names`，对话前端入口为 `index-sj5G41xd.js`。公开配置对正常 Harness 会话声明单个原始附件 50 MiB、单条原件合计 200 MiB。正式站测试账号、会话及附件已清理。
- 开物固定的官方内核提交为 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`。独立安装并启动官方 `0.2.2-rc.1` Web 发行包，浏览器实测三个默认权限选项、完全权限确认框和 9,254,553 字节的 PNG 添加，未提交模型请求。最新官方预览为 `dsh-v0.2.3-alpha.2`，其 Node 版本变更延续了 `alpha.1` 的产品功能。本轮对最新预览版核验了源码及发行说明。[官方界面实测](../../.runtime/deepseek-official-experience-N1Aw8S/stock-browser.json) · [官方 alpha.2](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.2) · [官方 alpha.1](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.1)
- 官方桌面版使用相同的 Web 客户端并接入桌面宿主。开物使用官方内核、自有 React 工作台和服务端的工具与权限桥。两者的输入框、工具目录、会话默认值、客户端插件和运行环境由各自产品的装配决定。[官方桌面装配](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/desktop/README.md) · [开物 Harness 装配](../../server/harness/profile.py)

## 问题和验收条件

| 编号 | 优先级 | 问题 | 实测或代码证据 | 通过标准 |
| --- | --- | --- | --- | --- |
| H01 | P0 | 会话缺少手动审批、自动审批、完全权限档位 | 订单分析助手四个文件 MCP 工具没有逐工具自动通过配置。后台查到该助手这四项工具范围内有 7 次人工单审；两个演示工具已经配置自动通过。会话更新接口拒绝 `permission_mode`，当前管理员代审是只覆盖少数内置工具的模拟审核。官方界面、状态持久化和实验自动审查均有对应实现。[开物权限面板](../../client/src/ApprovalPolicyPanel.tsx) · [官方权限界面](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/ui-permission-presets/README.zh.md) | 用同一个受控只读 MCP 依次验证三档。手动弹卡后执行，自动依据策略放行或回到人工，完全权限在管理员授权范围内无需逐项点击。刷新与重新打开后读回当前档位，审批记录显示决策来源及结果，未授权工具在三档均无法执行。 |
| H02 | P0 | 粘贴图片在加载中、输入框外静默丢失 | 正式站 Chromium 捕获了两种情况下的真实 PNG 粘贴事件，均无请求、无附件、无提示。会话就绪且光标在输入框内时，同一入口分别成功粘贴 14,583 和 9,287,012 字节的 PNG，两次 HTTP 201 并原样回读。官方新版会在适当的页面焦点下把图片转给当前输入框。[公网复现](../../.runtime/kaiwu-attachment-user-report-1jMsRG/public.json) · [官方粘贴路由](https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v0.2.3-alpha.1/packages/client/ui-conversation/src/client/input/file-intake.ts#L36-L77) | 会话打开时粘贴、空白区域粘贴、输入框粘贴都进入正确的草稿或给出明确状态。图片加文字混合粘贴保留文字，其他编辑框与弹窗中的粘贴仍在其作用域内。 |
| H03 | P0 | 旧标签页仍报 `5120 KiB` | 用上一版页面连接同一正式后端，图片粘贴无响应，选择 9,254,553 字节的 PNG 时精确复现用户所述上限提示，浏览器在请求前拒绝。新版正式入口同级别 PNG 上传成功。已确认复现条件，用户当前标签页的资源版本还待核对。[公网复现](../../.runtime/kaiwu-attachment-user-report-1jMsRG/public.json) | 模拟跨版本发布后的常驻标签页及新窗口。过期页面能更新或明确提示，草稿能保留。正式 Harness 同一 9 MiB 图片通过粘贴和文件选择成功，实际额度与界面提示一致。 |
| H04 | P1 | 旧会话上传额度和发送额度不一致 | 隔离环境同一张 7,151,467 字节的 PNG 可进入 Harness 图片处理，旧 Claude 会话在发送前返回 `ATTACHMENT_LIMIT`。正式站仍有 10 个未归档旧会话。现行页面的旧会话上传按 20 MiB 通用限制，发送端图片按 5 MiB。[边界复测](../../.runtime/deepseek-official-experience-N1Aw8S/attachment-gates.jsonl) · [旧会话后端校验](../../server/app/sdk_image_attachments.py) | 在旧会话和新会话都验收粘贴、选择及发送。对已迁移会话验证历史完整。对仍使用旧运行器的会话，在添加时显示实际可用额度和可操作路径。 |
| H05 | P1 | 多图经过上传后仍可能无法发送 | 开物每条最多 10 个附件，准备模型请求时图片编码合计仍限 10 MiB。隔离环境 11 张小图在预检失败，三张分别在 4 MiB 以下的 PNG 通过原件预检后，在准备时合计超限。官方附件原件默认为单张 50 MiB、20 张和合计 200 MiB，另有模型请求额度。[边界复测](../../.runtime/deepseek-official-experience-N1Aw8S/attachment-gates.jsonl) · [官方额度及归一化](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/attachment/attachment-local/src/index.ts#L34-L60) | 按入口测 10、11、20、21 张，20 张小图在支持的线路上可发送。通过目标模型实测单张和合计归一化限制，在发送前明确告知模型输入问题。 |
| H06 | P1 | 切换会话丢失未发送内容 | 正式站在 A 写文字、上传图片，切到 B 再返回 A，文字及图片均清空。B 的文字草稿也会丢失。官方新版说明包含草稿附件、进行中的上传和预览随会话保留。[公网草稿实测](../../.runtime/kaiwu-attachment-draft-audit-wYoOFa/public.json) · [官方新版](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.1) | A、B 两个会话的文字、已选附件和上传状态分别保留。切换、返回及刷新后校验文件归属和发送内容。 |
| H07 | P1 | 上传缺少每文件进度、取消和重试 | 开物输入框仅显示整体上传状态，当前页面串行发送已选文件。官方使用流式上传并显示单文件状态，新版发布说明记录多图并行与限流重试。[开物上传代码](../../client/src/ChatPage.tsx) · [官方上传协议](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/file-upload/src/http-route.ts#L23-L82) | 在限速及模拟 HTTP 429 下上传三份文件，界面独立显示进度、支持取消和重试，成功文件与草稿保持可用且不会重复保存。 |
| H08 | P1 | 大文档能上传，无法按需分段阅读 | 304,000 字节文本在隔离环境通过初始文件预检，准备模型请求时返回 `ATTACHMENT_TEXT_LIMIT`。开物每份提取 256 KiB、每轮 512 KiB。官方保存持久文件引用并允许工具按需读取。[边界复测](../../.runtime/deepseek-official-experience-N1Aw8S/attachment-gates.jsonl) · [官方附件设计](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/attachment.md) | 对含首尾不同标记的大文本及文档验证模型能定位并读取指定部分，会话重启后引用可恢复。跨会话访问、撤权及篡改的文件读取被拒绝。 |
| H09 | P1 | 排队任务依赖当前聊天页面 | 真 Chromium 加载开物 ChatPage，受控接口使前一轮在用户离开页面后完成，后台未发派发请求。返回该对话才发出一次。官方的会话控制器在宿主服务处理队列与运行中补充指令。[浏览器复现](../../.runtime/harness-feature-audit-BUSWwt/输入区交互边界回执.json) · [开物队列](../../client/src/MessageQueue.tsx) · [官方队列](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/api/session-controller/src/commands.ts) | 当前任务结束后，用户切换页面或断开浏览器，队列仍依次启动。重连能看状态，取消或编辑不会重复提交。区分下一轮排队和当前轮追加指令。 |
| H10 | P1 | 缺少结构化澄清与计划审阅 | 官方有问题选项、作答后恢复以及独立计划审阅。已运行固定版相应测试。开物有普通文字回复和工具审批卡，当前装配没有对应工具与计划面板。[官方提问](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/web/tests/question-composer.e2e.ts) · [官方计划审阅](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/web/tests/plan-review.e2e.ts) | 用存在歧义的受控任务验证选项、自由输入、取消、停止等待与重连恢复。计划可查看、要求修改和确认。业务工具审批继续由后台授权判定。 |
| H11 | P1 | 长会话主要靠手动压缩 | 初审时支持查看上下文和手动压缩，当时未启用自动压缩。后续修复见下方 H11 验收记录。官方具有溢出恢复，预览版还提供压缩摘要侧栏。自动恢复需用长会话专项实测。[开物压缩配置](../../server/harness/compaction_plugin.ts) · [官方新版](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.1) | 用受控模型模拟上下文接近上限和溢出。跟踪自动动作、压缩前后用量及摘要，验证原会话可继续，已执行的工具不会重复调用。 |
| H12 | P2 | 旧兼容地址公布的附件上限与网关不一致 | `/zhixing-next/` 网页入口跳转，但其兼容 API 公布 52,428,800 字节。相同大小的声明请求在该路径返回 HTTP 413，正式路径接受请求头。整个对照未发送文件正文。[兼容路径实测](../../.runtime/kaiwu-attachment-user-report-1jMsRG/alias-limits.json) | 每个仍受支持的入口公布可真实达到的额度。分别验证精确边界与多 1 字节，旧浏览器能获得清楚的迁移或兼容结果。 |
| H13 | P2 | 会话分支、命令与引用入口不足 | 开物支持搜索、归档、会话轨迹及 `@` Skill，当前分支没有官方会话分叉入口与文件/会话引用。官方固定版有分叉测试，预览版还有空白输入历史召回和侧栏细化。[开物会话路由](../../server/app/main.py) · [官方分叉](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/web/tests/fork-mid-turn.e2e.ts) · [官方预览](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.1) | 从历史中间一轮建立新分支并保留前文，后续消息各自独立。用输入框调用命令并插入授权文件或会话引用，提交前可编辑。 |
| H14 | P2 | 子智能体与计划任务缺少统一工作台 | 开物有独立子任务 API 和执行轨迹，当前对话没有官方团队入口、父子会话关系视图和计划任务管理。官方调度单元固定版测试已运行，预览任务树属于实验插件。[开物子任务路由](../../server/app/main.py) · [官方调度测试](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/web/tests/schedule-manual.e2e.ts) | 对无副作用任务验证子任务进度、父任务汇总、取消与授权继承。计划任务支持设定时间、运行历史和撤销。 |
| H15 | P2 | MCP 授权方式与资源管理较少 | 开物配置入口为无认证或 Bearer，可测试工具及按助手授权。官方新版增加可选的连接器账号、MCP OAuth 和凭据刷新。正式华微绑定流程本轮未改动。[开物连接界面](../../client/src/ConnectionsPage.tsx) · [官方新版](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.1) | 用测试 OAuth MCP 验证登录、过期刷新、撤销和工具更新，保留企业授权及审计能力。 |
| H16 | P2 | 文档预览类型和长文呈现有限 | 开物附件预览可打开图片、PDF，并读取最多 1 MiB 普通文本，其他格式主要走下载。官方文档侧栏支持额外文档预览和翻阅方式。[开物附件预览](../../client/src/components.tsx) · [官方预览插件](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/ui-sidebar-documentpreview/README.md) | 用真实 Word、Excel 和长文本验收预览、翻页或指定表格、原件下载及读取错误。 |

## 第二轮按任务全流程补测

补测从打开会话开始，覆盖历史加载、模型和 MCP 故障、已执行动作后的重试、任务结果归属、统计、检索和导出。故障用合成数据注入，浏览器使用 Chromium 和正式站同哈希的前端资源，实际工具调用只走隔离环境的本机 FastMCP。表内的重试次数、上游错误及展示值来自测试回执，压力样本不代表生产时延。

| 编号 | 优先级 | 问题 | 触发方式与实际结果 | 通过标准 |
| --- | --- | --- | --- | --- |
| H17 | P1 | 刷新或另开窗口会丢失当前会话位置 | 打开一条已保存会话，地址保持工作台首页。刷新或在第二个浏览器标签打开该地址，都回到首页。人工重新打开可看到原历史。官方固定版有活动流、已完成轮次和会话重开的浏览器回归。[开物实测](../../.runtime/harness-parity-round2-kdXjp9/navigation-history.json) · [官方恢复用例](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/web/tests/session-replay-reload.e2e.ts) | 页面刷新和已有访问权限的另一标签能打开同一会话，恢复活动状态与可见结果。退出账号后清空会话状态。 |
| H18 | P1 | 历史全量请求和渲染 | 打开 360 条消息的合成会话两次，每次请求全部 218,301 字节并渲染 360 条消息和 2,340 行表格。实际 API 带 `limit=20` 仍返回全部 360 条。官方会按消息及轮次加载旧页。性能影响以合成压力结果为准。[开物浏览器证据](../../.runtime/harness-parity-round2-kdXjp9/navigation-history.json) · [开物接口实测](../../.runtime/harness-parity-round2-kdXjp9/history-storage-api.json) · [官方历史分页](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/api/session-controller/README.md) | 长会话先请求最近消息，再按需加载更早内容。跳转、流式更新及二次打开不重复整段加载。 |
| H19 | P1 | 短暂网络错误可让运行状态和最终回答一直停在旧画面 | 已收到完成事件后，读取最终消息时一次 HTTP 503 就让界面停在半截回答。若完成事件也丢失，服务器已成功而界面仍在运行。进入运行中的会话时一次状态 HTTP 503 也会阻断订阅。3,350 毫秒观察期内没有重试，手动刷新后恢复。[SSE 浏览器故障注入](../../.runtime/harness-execution-fault-DSGpqL/browser-fault.json) | 在首个状态、事件流和最终读回各注入一次错误。自动恢复订阅及最终回答，按钮与运行状态一致，期间保留明确的重连提示。 |
| H20 | P0 | 已执行的写操作在任务重试时可能执行两次 | 隔离 FastMCP 写入成功，模型随后断流，运行显示失败。使用原请求编号重放不会重复写。「重试本次任务」会用新编号重发，经第二次正常审批后，相同写动作实际执行第二次。复现未访问客户系统。[真实 Harness 和 MCP 故障注入](../../.runtime/harness-execution-fault-DSGpqL/partial-effect.log) | 保留已确认的工具回执，默认从中断位置继续。无法继续时明确展示已执行及结果不确定的操作。重新执行写动作须独立确认并验证幂等，同一测试在自动放行策略下也不能无提示重复写入。 |
| H21 | P1 | 短暂模型限流直接结束任务 | 真实固定版 Harness 连接本机模拟模型。首个请求返回 HTTP 429 和 `Retry-After: 1`，任务以通用 `HARNESS_TURN_FAILED` 结束。等待 1.5 秒仍只有一次模型请求，手工新发一轮才成功。官方重试模块支持基于错误类别和等待提示的有限重试。[本机故障回执](../../.runtime/harness-execution-fault-DSGpqL/rate-and-restart.log) · [官方重试模块](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/llm/llm-retry/README.md) | 模拟 429、超时与暂时性 5xx，验证延时、重试次数和取消。只重试安全的模型请求阶段，已有业务动作不被再次派发。 |
| H22 | P1 | 同名同内容的 Excel 再生成，第二轮无附件 | 两轮都用真实工作台 API 写出字节相同的 `same.xlsx` 并成功。第一轮有附件与产物事件，第二轮两者均为空。产物采集代码与正式站一致。[产物实测](../../.runtime/deliverable-audit-EbZOVh/artifact_scope_results.json) · [生产代码比对](../../.runtime/deliverable-audit-EbZOVh/prod_match.json) | 存储允许复用同一文件字节，但每个成功产出该文件的轮次都要有可下载的结果引用。 |
| H23 | P0 | 失败轮次的文件挂到下一轮回答 | 合成任务写出了 `before-failure.xlsx` 后失败。下一轮只回文字、没有写文件，却出现该工作簿的下载入口，下载 HTTP 200。正式站与测试的归属代码一致。[产物实测](../../.runtime/deliverable-audit-EbZOVh/artifact_scope_results.json) | 为每个轮次建立文件归属和状态。失败文件可作为该轮的未确认结果保留，后续与其无关的成功轮次不能领取或引用。 |
| H24 | P0 | 会话概览用固定值覆盖真实统计 | 在与公网资源哈希一致的浏览器版本里，测试 API 分别返回 2 个会话、3 个请求、0.07 美元。界面显示 12、84、0.42 美元。可视的「当前为展示数据」位于默认折叠的指标说明。[Chromium 对照](../../.runtime/deliverable-audit-EbZOVh/browser_session_results.json) · [覆盖逻辑](../../client/src/SessionObservability.tsx) | 正常账户展示与统计接口一致的数值，空数据和未计量状态可区分。演示数据只出现在显式演示入口，并在数值旁明示。 |
| H25 | P1 | 搜到会话却找不到命中的那条消息 | 旧消息包含唯一编号，搜索能筛到正确会话。结果无命中片段，打开后停在最新消息，命中位置不在可视区，也没有高亮。官方提供带片段和分页的可选会话查询实现。[Chromium 搜索复现](../../.runtime/deliverable-audit-EbZOVh/browser_session_results.json) · [官方查询模块](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/session-query/session-query-sqlite/README.md) | 搜索结果显示命中片段。打开结果定位到该轮并高亮，未加载的旧历史按需定位。 |
| H26 | P2 | 对话无法整体复制或导出 | 正式站同款会话界面没有整场会话复制、导出入口。实际隔离 API 对会话导出返回 404。官方固定版浏览器用例验收 Markdown 复制和 ZIP 导出。[开物 Chromium](../../.runtime/deliverable-audit-EbZOVh/browser_session_results.json) · [官方导出用例](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/web/tests/navigation-panes.e2e.ts#L377-L419) | 具备权限的使用者可以复制完整会话和导出含引用的记录，导出说明附件、审批及工具证据的覆盖范围。 |
| H27 | P1 | 自动选模会把带图片的消息分给纯文字模型 | 对同一 4×4 PNG，开启自动选模时接口选择纯文字模型并返回 HTTP 422 `VISION_UNAVAILABLE`，模型执行 0 次。关闭自动后，同一个会话、同一附件及同一问题进入视觉模型并完成。正式模型目录包含纯文字和视觉模型，本例在隔离环境复现。[模型调度实测](../../.runtime/enterprise-parity-round2-T39iYz/enterprise-results.json) | 自动选模先按图片、附件及能力要求筛选候选。视觉模型不可用时，在创建任务前给出原因并保留附件和草稿。 |
| H28 | P1 | 无凭据的模型仍显示可用 | 在隔离环境给模型配置有效声明和缺失的供应商凭据。就绪接口返回 `model_configured=false`，目录仍返回 `available=true`。打开会话 HTTP 201，发送 HTTP 202，之后运行失败 `HARNESS_PROVIDER_NOT_CONFIGURED`，未访问供应商。[模型目录及启动守卫测试](../../.runtime/enterprise-parity-round2-T39iYz/enterprise-results.json) | 凭据缺失、配置缺失和已经验证可用分别呈现。服务端在接收任务前阻止已知无法运行的模型，并保持草稿。 |
| H29 | P0 | 一条 MCP 测试故障会阻断无关会话，恢复网络后仍不可用 | 本机真实 FastMCP 正常时，绑定它的助手可完成普通文字任务。一次连接测试遇 HTTP 503 后，开物清空工具清单，随后不需要工具的普通文字也失败 `HARNESS_MCP_NOT_TESTED`。恢复 MCP 服务后继续失败，直到再次手工测试。整个测试无 MCP 业务工具调用。[真实 MCP 发现及注册试验](../../.runtime/enterprise-parity-round2-T39iYz/enterprise-results.json) | 一个测试连接离线时，普通文字和其他健康工具可继续工作。失败的连接显示原因。连接恢复后可按预期重新发现，实际启用仍服从授权。 |
| H30 | P1 | 登录过期会无提示跳转并丢掉未发送文字 | 在正式站同款浏览器资源中构造发送返回 HTTP 401。页面直接跳登录，无会话过期提示。登录成功回到首页，手工打开原会话后，刚才未发出的草稿已清空。[重新登录实测](../../.runtime/enterprise-parity-round2-T39iYz/reauth-browser.json) | 在原会话提交时模拟过期。明确提示登录过期，重新认证后回原会话并恢复草稿，保留对应账号边界，避免重复提交。 |
| H31 | P2 | 放弃未发送附件，页面缺少从服务器撤回的方式 | 草稿上的移除只变更页面状态。隔离真实接口上传后，未绑定会话的文件仍有数据库记录和磁盘原件。当前文件删除接口返回 HTTP 405，文件所有者仍可回读。长期回收时限本轮未作时钟推进测试。[上传和删除接口实测](../../.runtime/harness-parity-round2-kdXjp9/history-storage-api.json) · [输入框移除逻辑](../../client/src/ChatPage.tsx) | 放弃附件后可核对撤回或回收状态。仍有消息引用时保护有效文件，未发送文件按明确的留存期限清理。 |
| H32 | P1 | 服务进程重启后，任务和审批缺少可继续的状态 | 对临时数据库实际关闭并重新打开服务。排队任务进入 `interrupted`、活动指针清空，待双管理员审批变为 `expired`，之后投票返回 HTTP 409。开物当前不自动续跑，被打断的任务需要恢复入口与已完成步骤说明。[服务重开与审批测试](../../.runtime/harness-execution-fault-DSGpqL/rate-and-restart.log) | 在排队、执行中、待审批分别做重启测试。界面明确显示剩余步骤和已执行动作，允许安全恢复或重新审批。所有已确认的写动作保持可核对和可防重。 |

## 仍需验证的组合

| 范围 | 已有证据 | 还缺什么 |
| --- | --- | --- |
| Windows、macOS 与最新官方预览 | 固定版官方 Web 发行包和 Chromium 已覆盖权限、附件。用户报错有旧页面重放证据。 | 使用同一张图在具体 Windows 浏览器、WebKit 和官方桌面预览版复测。核对用户原窗口的页面地址与前端版本。 |
| 企业内网断线、轮换和撤权 | 隔离 FastMCP 已测一次发现失败及恢复。历史上已验证开物可列出赵鹏的 MCP 工具。 | 现场环境在运行中切断及恢复连接，验证凭据轮换、只读与写入权限、在途操作的结果。使用经授权的测试数据。 |
| 多会话并发和超大历史 | 正式站同哈希浏览器已做 360 条合成历史。单次 429 和本机 MCP 503 已故障注入。 | 在压测环境对并发用户、上传、模型限流、长时流式会话和滚动发布一起验收，记录吞吐、尾延迟与重复执行。 |
| 完整的官方 Web 恢复对照 | 已核对固定版上游重载回归用例，开物已实测刷新丢当前会话及状态读回故障。 | 本轮本机独立模拟的官方发行包未收到模拟模型请求。官方恢复效果以其已发布的验收用例为对照。 |

## 历史会话和权限对齐口径

官方固定版 Web/Desktop 默认档位为「仅可查看」「工作区内修改」「完全权限」。启用实验插件后，当前会话选择器多出「自动审查」，默认仍有独立的工作区内修改策略。开物关心的「手动审批 / 自动审批 / 完全权限」是产品的会话审批档位，需明确每档适用的资源及操作。对同一个已授权操作，应能看到决策来源、批准或拒绝状态，以及真实执行结果。[官方预设](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/interaction/permission-presets/README.md) · [官方实验自动审查](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/experimental/auto-review/README.zh.md)

开物属于多用户工作台。完全权限档位的验收范围仍由企业管理员授予的连接、目录、工具、数据范围和高风险审批规则决定。官方桌面产品的本机终端、工作树、文件系统与操作系统集成可在拥有独立宿主环境的场景单独对标。[官方桌面宿主](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/apps/desktop/README.md)

## 测试覆盖

固定版官方权限相关测试 126 项通过。附件、提问、计划、调度、会话分叉和队列等 7 组共 210 项通过。开物审批回归 17 项通过，配合官方 Harness 和本机 FastMCP 的 3 项集成检查通过。开物正式站的浏览器复验使用 Chromium 和临时测试账号。用户原报错标签页的浏览器及加载版本，官方 Windows/macOS 安装包与最新预览的客户端端到端体验，本轮仍待实测。[官方功能测试回执](../../.runtime/harness-feature-audit-BUSWwt/upstream-native.log) · [开物对话框回执](../../.runtime/harness-feature-audit-BUSWwt/输入区交互边界回执.json)

## 开发分支修复复验

| 验收项 | 实测结果 |
| --- | --- |
| H10 结构化交互 | 固定版 Harness 连接本机测试模型。模型发出澄清问题和审阅计划后暂停，两种回答均进入原轮次的下一次模型请求，重复提交被拒绝。 |
| H11、H21 上下文压缩与模型请求恢复 | 通过固定版 Harness 的接近上限、溢出恢复和 12 个请求恢复用例，包含限流、可见输出边界和取消。 |
| H13 会话分支 | 从中间轮次建分支，模型收到该轮及其图片，随后各自独立。重复请求返回同一分支，原附件撤回后，下一轮不会将历史图片发给模型。分支标出继承历史的末尾，空编辑器可用方向键召回已加载的用户消息。消息引用覆盖已加载的当前会话历史。 |
| H09 后台队列和当前轮追加 | 宿主独立派发下一轮队列。固定版 Harness 在同一个任务中接收追加指令，同一编号重复请求只接收一次。任务结束后拒绝新的追加指令。 |
| H14 子任务和计划任务 | 一次性文字计划到期后执行一次，撤销后不会运行。两个独立 Harness 子任务实际调用模型，分别交回结果和用量。模型分派的结果回到父轮，可由父助手汇总。手动从面板分派时，使用「用结果起草汇总」后可自行修改或发送。取消父任务会取消运行中的子任务。 |
| H15、H16 OAuth 和文档预览 | 使用本机测试 MCP 验证授权、刷新、撤销及工具更新。长文本和 Word 可预览，Excel 可切换工作表。 |
| H31 附件回收 | 二进制与表单上传都按草稿记录留存时限。超过七天且无消息、队列引用的草稿可以由后台删除。 |

## 独立复验与修复

独立子 Agent 在 Chromium 打开生产构建，并使用本机 FastAPI、固定版 Harness 和受控模型复现操作。首次候选版为 `index-jMwVdBMm.js`。复验使用 `index-Dhor0yjg.js`，分支及输入历史的补测使用后续构建。

| 发现 | 修复与复验 |
| --- | --- |
| H06、H07 上传中刷新 | 同时上传两个文件，其中一个延迟 12 秒，一个首次返回 HTTP 429。刷新后保留原文件、原错误和重试入口。用户重试成功一项并移除另一项后可继续发送。HTTP 重试复用原编号，相同内容只保留一份。另一组回归验证第一件上传较慢时仍保留选择顺序。 |
| H06、H30 消息提交回执丢失 | 草稿记住请求编号及提交路径。浏览器模拟首次返回 HTTP 503，刷新后重试沿用同一编号，收到确认才清空草稿。 |
| H14 手动子任务结果 | 手动分派的两项结果都可加入已有草稿。起草时模型请求数没有增加，点击发送后受控模型收到原草稿和两项结果。 |
| H18、H25、H26 历史定位与导出 | 96 条存量消息首屏取 40 条，再加载得 80 条。检索摘要定位到视口内的旧消息。Markdown 和 ZIP 都含早期与最新消息，剪贴板不可用时提供手动复制。 |
| H09 追加指令后的回答分段 | 浏览器实测显示两段回答，后台持久化文本同样以空行分隔。加入下一轮队列的消息离开会话后仍启动独立任务。 |
| H17 正式路径的登录和会话入口 | 候选版使用 `/project/kaiwu/` 挂载。在浏览器从带 `conversation` 的地址登录后回到原会话，首次分页取 40 条并显示最新消息。 |

本轮后端回归 150 项通过，前端测试 513 项通过，Chromium 输入区交互 25 项通过。独立验收的逐项覆盖见 [32 项复验矩阵](../../.runtime/kaiwu-independent-review-sgrGcK/verification.md)。

这批工作在开发候选版验证。两个支持更新检测的构建之间，提示更新和恢复会话定位已经实测。早于更新检测功能的正式站标签页、客户内网在途断线、生产并发负载及桌面客户端仍需现场验收。子任务当前用于经过授权的独立文字分析、归纳和对照，没有授予子任务父会话的文件、记忆或 MCP 权限。
