# 独立 OCR、图片搜索与细粒度治理补齐记录

状态：开发与验证中，未完成整链验收，未发布。本记录不能替代完整服务端回归、浏览器验收或客户验收。

## 1. 基线和隔离

- 学校云集成基线 `f4d4842f596afea7af877fd31019bc3f74fac946`，主工作树 `open-claude-code-main-merge`。
- 橙色 `feature/ocr-image-capabilities`，目录 `open-claude-code-ocr-images`，负责独立工具、附件与来源。
- 紫色 `feature/external-capability-governance`，目录 `open-claude-code-external-governance`，负责配置、权限与统一执行治理。
- 项目负责人文档分支 `docs/capability-completion-plan`，目录 `open-claude-code-capability-plan`。其他工程师分支和未提交内容不修改，不清理。
- Orca run `run_7bbd04216a23`；橙色 dispatch `ctx_32d11eac85de`，紫色 dispatch `ctx_8c44bad53f6d`。Orca 注册项目是 folder，因此启动目录与实际编辑 Git 工作树不同；任务书强制先核对独立工作树，不冒称自动创建成功。
- 18:43 用户确认其他工程师负责前端设计；两名执行者的全部 `client/` 写权限冻结，后端继续。当时两分支 `git status --short -- client` 均为空，未回退或覆盖任何前端文件。设计工程师的具体分支/会话待确认；前端接入和浏览器整链门禁仍保留，不冒充后端完成即可发布。

## 2. 已执行的真实供应商探针

所有探针在学校云运行，只使用公开图片和自建合成检查单；现有、所有者已授权的官方凭据仅在进程内读取，没有复制进代码、日志或前端，没有修改全局模型配置。以下是六次显式、无自动重试的接口请求，不是产品端到端测试，也不是并发或精度基准。

| 请求 | 真实结果 | 判定 |
|---|---|---|
| 独立 Search：ABB 工业机器人资料 | HTTP 200，3 条实际结果，0.618 秒 | 接口合同成功；尚不证明工作台来源整链 |
| 独立 Fetch：ABB 公开机器人产品页 | HTTP 200，1266 个正文字元，7.843 秒 | 接口合同成功；未宣称获得供应商不可见的最终重定向链 |
| PDF，rich_layout_recognition | HTTP 200，一页 `stop`，但文字为空；5 个关键字段均未识别 | **质量失败**，保留回归用例，不能显示完整解析成功 |
| 渲染检查单 PNG，text_recognition | 一页完整；设备编号、两项压力、日期、结论全部正确，2.520 秒 | 这一固定样例成功；不外推所有文档识别精度 |
| 原 PDF，text_recognition | 一页完整，同样 5 个字段全部正确，2.329 秒 | 显式更换识别用途的诊断，不是自动重传；支持选择默认文字识别 |
| 公开 ABB 图片，官方 WebTools 以图搜图 | 真实 `server_tool_use(kind=image_search)` 和关联 `web_search_tool_result`，11 条带 `image_url` 的结果，5.350 秒 | 证明真实以图搜图协议可用；原件可达性与相关性另审，不等同工作台整链通过 |

机器可读摘要与返回摘要：`docs/evidence/external-capabilities-20261114/official-contract-probes.json`。原始公开响应、合成附件和脚本在受控学校云临时目录 `/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/`，不包含密钥。

独立只读复核见 `docs/evidence/external-capabilities-20261114/provider-probe-review.md`：两次文字识别的标签和值均正确配对；前三图片分别为同图缩小转载、被站点拦截未核实、相关但不同照片。原网页可达 1/3、图片可达 2/3，仅为这次观察。模型正文提及但结构化工具结果未包含的来源不得进入证据；原始 Commons 出处此次没有召回。摘要里的 `response_sha256` 为脚本报告的原始 HTTP body 摘要，原字节未留存；新增 `artifact_sha256` 才是可从落盘格式化 JSON 重算的摘要，二者不能混淆。

以图搜图探针报告输入 16142、输出 222 个词元，服务端 `tool_image_search=2`。单个 HTTP 尝试不等于供应商只执行一次内部工具，也不等于低成本；需展示真实用量和未知费用，不能给出节省宣传数字。没有调用 Brave Images，缺少其产品授权证据时保持未验证。

测试图片来自 [Robot ABB 1](https://commons.wikimedia.org/wiki/File:Robot_ABB_1.jpg)，作者 Peter Potrowl，选用 [CC BY 2.5](https://creativecommons.org/licenses/by/2.5/)；原图未修改，仅用于这次有界公开测试，不写入客户知识库。图片 SHA-256 及实际字节数保留在探针摘要。真实图片结果可能来自第三方，不能把搜索返回当成再分发许可。

## 3. 安全和验收约束

- 普通聊天仍走既有模型线路，独立官方能力是明确授权的工具供应商；不声称 TokenHub 具备官方独立接口。
- OCR 和图片外发默认禁止；助手四能力独立授权。保留既有审批、部署禁用、来源读取鉴权；设置不能扩大试用白名单。
- OCR 不自动重试或回退。供应商接收上传后可能继续处理和计费；本地取消只保证不再派发及不接纳迟到结果。
- 官方独立 Search 不是图片搜索。以图搜图桥只发送选定图片和固定最小指令，不发送完整对话；文字搜图与以图搜图必须在配置和工具模式中区分。
- 域名规则约束返回内容与原件访问，不冒充上游搜索范围隔离。每个真实网络尝试都须授权复核和原子记账；历史来源访问不因原任务结束失效，但必须受当前权限约束。
- 密钥只写不读，诊断受固定样例与配置版本约束；配置存在不等于授权或效果已验证。

## 4. 尚未通过的门禁

实现分支尚未提交完整验收；合并后专项、完整回归、浏览器、并发额度、取消、撤权和真实工具整链尚待执行。之前 `1656 passed / 51 skipped` 只证明旧基线，不能覆盖本批次。第二、第三阶段仍部分实现，不能对外宣称可交付。

2026-11-14 18:24 补跑学校云基线 `65a6170` 的固定真实 Harness 离线集成（四个测试文件），得到 **86 passed / 2 failed / 13 warnings，236.48 秒**，退出码 1。使用本机已有 Node `24.9.0`、Harness `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` 与本地模拟模型，没有请求付费模型。两项失败分别位于 `test_harness_real_runtime.py:268` 和 `test_harness_real_mcp.py:150`：实际工具集合比断言多 `file_read`、`file_write`、`file_search`。目前只确认差异，尚不能判定是旧断言遗漏还是工具权限问题；已交执行者和独立只读审查核对，不能通过删断言或恢复跳过来掩盖。日志及 JUnit 位于 `/media/data3/dengkw/tmp/oc-capabilities-completion-20261114/harness-baseline.log`、同目录 `harness-baseline.xml`。下一次全量必须显式启用这两个运行时路径，不能继续把本机已具备资源的集成用例跳过。

审批接线的独立只读审查还确认：现有 `approve` 只返回布尔值，SDK 的权限回调与 MCP handler 不共享调用上下文。新能力须将批准记录精确绑定到本次工具调用；SDK 需主机发行、参数绑定、一次消费的内部票据，不能查询“最近一次批准”或借用并发调用的全局属性。方案和待测项记录在 `docs/evidence/external-capabilities-20261114/approval-context-review.md`；这是实现前的审查结论，不是接线测试已通过。

18:31 独立根因复核确认两处失败是既有私有工作台文件工具加入后，旧集成测试预期没有同步；不是本批次新增原生文件系统权限。保留精确集合比较、补齐三个字面工具名的修复已交橙色执行者，尚未复跑。另用独立目录执行已有文件工具、注册和试用隔离测试得到 **22 passed / 1 warning，退出码 0**；这些单测不能替代真实桥接中的写入审批、拒绝和撤权验证。历史提交、授权路径、最小修复和准确命令见 `docs/evidence/external-capabilities-20261114/harness-baseline-review.md`。

发布顺序仍为学校云验收、独立审核、冻结 Git 版本、推送远端、按用户本次授权和回滚记录发布。腾讯云仅接收已验收发布候选，不承担开发、知识数据源或界面验收。本记录时尚未操作腾讯云或重启任何共享服务。
