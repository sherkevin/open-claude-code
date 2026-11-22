# 输入与模型选择

本轮保持既有会话、助手、MCP、来源与导出协议，改进输入区、首页和业务数据的浏览体验。

- 模型目录、可用状态、跨模型历史限制与推理档位均来自服务端。滑杆拖动只作预览，释放或键盘确认后保存。没有可调档位时不显示滑杆。
- 非空草稿按 Tab 请求补全，预览后再次按 Tab 或点击“采用”才填入。Shift+Tab、中文输入法、继续输入、发送与换会话均不会接受旧结果。
- 已授权 DeepSeek 短文本线路显式关闭扩展推理，避免 256 Token 额度全部消耗在推理而没有正文；不改变主业务会话的推理级别，也不对其他模型强塞该参数。依据为官方 Anthropic 兼容协议与腾讯云实际调用回执。
- 新回复完成后，客户端可请求下一轮问题建议；打开历史会话不自动请求，建议不会自动发送或覆盖草稿。该辅助请求与主回答分开。
- `POST /models/followups` 接收 `conversation_id`、`run_id`。服务端核验当前用户、会话归属和最后一轮成功状态；只取该轮问答，最多 1,400 字符，最多输出 256 Token、3 个短问题。沿用已授权的 Tab 模型线路，不选择其他模型作为回退。
- 单个服务进程内，同一用户、会话与轮次复用 15 分钟内缓存；同一轮并发请求共享一次调用。最多 4 个并行生成、16 个待处理任务、256 项缓存。命中缓存与共享请求单独标记，不重复计入模型用量。服务重启或多进程之间不共享此缓存。
- `model_router_budget_usd > 0` 是现有启用门槛，并非累计费用硬上限。没有可用线路、超时或返回不合规时，返回空建议，不伪造模板。受控 trial 未额外开放此新接口。
- 追问属于额外模型调用，其用量在接口 `router.usage` 返回；现有会话用量卡仍展示主任务用量。验收须分别报告，不能把新增追问称为“零额外 Token”，也不能把单轮测量宣传成总费用节省比例。
- 蓝紫色用于 AI 输入、推理和实际执行状态；业务表格保留白色、冷灰与语义状态色。动效遵循 `prefers-reduced-motion`。

`client/tests/composer-interactions.mjs` 使用实际 ChatPage 和受控 HTTP 回执验证交互边界，不调用真实模型。腾讯云真实模型与演示 MCP 联调属于独立验收；华微自己的 MCP 接入仍需在其提供端点和凭据后验证。

## DeepSeek Harness 附件对标

基准为开物当前固定的官方提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`。另核对了官方 `dsh-v0.2.3-alpha.1`，用于跟踪新版交互差异。[开物依赖记录](upstream-sources.json) · [官方预览版](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.3-alpha.1)

| 对标项 | 官方 Harness | 开物本次结果 |
| --- | --- | --- |
| 添加附件 | 在输入区粘贴图片或文件、拖放、用文件选择器添加。剪贴板同时有文本时保留文字。 | 三种入口共用附件预检、上传、预览、移除和发送。在 Chromium 验证图片粘贴、混合剪贴板、中文文件名、拖放及旧接口兼容。 |
| 上传和原件 | 普通文件以字节流保存，附件库未设固定的普通文件大小上限。原始图片单张 50 MiB，单条消息累计 200 MiB。 | Harness 会话按流保存原始文件。默认单文件 50 MiB、单条消息原件合计 200 MiB。普通文件和图片共用十个附件名额，现有文件类型规则继续生效。上传接口在上限一字节内外分别验证成功及拒绝。 |
| 图片进入模型 | 入模前独立处理分辨率与编码，目标约 4 MiB，默认最多二十张原图。 | 保留原件。静态图片按最多 4,194,304 像素、长边 8,192 像素、单张编码最多 4 MiB 准备，现有工作台模型链路仍限制单轮编码图片累计 10 MiB。选定模型不支持图片时保留草稿并提示。 |
| 大文档读取 | 持久保存文件引用，模型按需用工具读取。 | 文本、PDF、DOCX 和 XLSX 仍由工作台提取，每份 256 KiB、每轮合计 512 KiB。50 MiB 是上传额度，解析页数、解包和文字额度分别生效。大文件按需读取待接入和验收。 |
| 上传状态与跨会话草稿 | 每文件进度、取消及重试。官方预览版还把粘贴转到活动输入框，并在会话切换时保留独立草稿。 | 输入区显示整体上传状态。每文件进度、取消、重试、输入框外粘贴和跨会话附件草稿待对齐。 |

官方实现见[输入区](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/ui-conversation/README.md)、[流式上传](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/client/file-upload/src/http-route.ts#L23-L82)、[原件和图片额度](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/attachment/attachment-local/src/index.ts#L34-L60)和[持久附件](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/docs/subsystems/attachment.md)。

大文件按需读取可以复用官方 SDK 的文件引用。主机应先核验会话与附件归属，由当前会话的私有附件库生成引用，再通过受限工具分段返回内容。读取每次都核验权限和文件摘要。覆盖恢复会话、跨会话访问、文件篡改和撤权后，再开放这条链路。[官方 SDK 协议](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/sdk/protocol/src/types.ts#L70-L90) · [官方文件引用投影](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/llm/llm/src/content.ts#L145-L182)

在同一发布包中验证应用与随包 Harness 运行时的兼容性。Nginx 为 `/project/kaiwu/api/v1/files/binary` 配置与 `AGENT_CHAT_ATTACHMENT_UPLOAD_MAX_BYTES` 一致的请求体额度，默认为 50 MiB。该路由使用 HTTP/1.1 转发，关闭请求体缓冲，并沿用已有的路径前缀转换。仓库的本地预览入口已配置对应规则。

### 验收记录

- Chromium 通过 21 项输入区检查。包含图片粘贴、预览、移除、发送，中文名文件拖放，以及 50 MiB 多 1 字节的前端提示。
- 香港隔离环境通过真实 Nginx 和 Uvicorn 接收 52,428,800 字节。后端在后续请求块发出前已落盘首个 1 MiB，下载回读的 SHA-256 与原件一致。请求体为 52,428,801 字节时返回 HTTP 413。
- 香港隔离环境使用开物配置的 TokenHub `deepseek-v4.1-flash` 线路。上传 9,487,371 字节的 PNG 后，下载原件的 SHA-256 在对话前后均一致。官方 Harness 发出的模型请求含 1,512 × 1,127 像素、149,078 字节的 JPEG。模型正确识别左侧绿色三角形和右侧紫色圆形。
- 2026 年 11 月 22 日，正式工作台切换到 `20261122-hk-harness-attachments`。公网已加载新聊天页面，Harness 上传配置为 `chat_attachment_upload_max_bytes=52428800`。发布包以原线上版本为基础，保留其 Harness 运行时与已缓存的静态资源。
- 同日从正式 HTTPS 入口登录并打开测试会话。等附件按钮可用后，Chromium 的原生 `Ctrl+V` 触发了真实的 PNG 上传。下载与浏览器提交字节一致，预览和移除通过。同一入口另验证了恰好 52,428,800 字节原样回读，以及 52,428,801 字节返回 HTTP 413。临时测试账号、会话和上传文件已清理。
