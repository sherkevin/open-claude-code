# 普通会话内的 Codex 工程回执历史（P2-001）

开发候选 cfee98ddc1f7a68df6d5930828f71a7f151bc5d9，基于 b17f630。独立验收时间 2026-11-28 01:44–01:45 CST。未部署生产。

普通会话的消息区上方增加可展开的 Codex 工程回执历史，无需原请求编号或原浏览器标签页。复用现有 owner 绑定和最近八份权威回执，最新核对的状态可能另保留一份；被淘汰的正文不会编造。显式提示未知、失败、截断和暂不可读状态。不把工程正文复制进普通消息/Run，不启动模型，不授予任何新工具权限。沿用现有默认关闭、trial 禁用、归档、普通活动运行、用户与助手的授权检查。历史正文为纯文本，对已识别的宿主路径与凭据标记隐藏；这是附加保护，不声称能识别任意秘密或提供通用 DLP。

| 验收项 | 最终隔离结果 |
|---|---|
| Codex 宿主与真实产品 API | 32/32，exit 0，43.58s；含 owner/会话隔离、保留上限、损坏/中断、禁止新执行、历史不创建普通消息/Run |
| 前端专项 | 12/12，exit 0 |
| TypeScript 与生产路径静态构建 | exit 0 |
| 真实 Chromium、固定 Codex 原生二进制、隔离回环模型 | exit 0；两次工程回答、另一会话无回执、关窗重开、整页刷新后两条同序回执；三个 history 请求均 GET，回访读取期间 0 写请求；第三轮续问保留 A/B 原始上下文 |

三次用户显式发送对应三次真实 turn/start 与三次回环 provider 收件。不是三次历史读取触发模型。浏览器样本 1 组；真实供应商、付费、生产客户、统一审批/队列不在本次验收范围。工人交付 exit 0（2026-11-27 23:01:29），请求 Astra/ultra，未独立确认模型内部路由。初始缺测试依赖的失败已留存，由总控复用现有依赖补验；第一次新基线 rebase 缺 Git 提交身份/恢复文件，已正常 abort 后使用既有提交身份重放，无全局配置变更。

证据根目录：VIP /media/data3/dengkw/acceptance/kaiwu-parity-codex-history-20261127/controller/20261128-0143/。
- base.txt、candidate.txt、backend/frontend/build/browser 的 log/exit、verify.sh、run.sh。
- browser/browser.json、provider.jsonl、native.jsonl、history-cold-return.png、history-after-refresh.png、other-conversation-empty.png。固定旧版原生执行器和回环模型在无外网、无真实用户 HOME 的独立环境中运行。
- 原工程师交付及上一轮验证在上级 controller/ 和 worker.jsonl；原始错误保留。

下一独立增量 P2-002a：在普通运行与 Codex 工程执行之间验证并封住会话准入竞态，再逐步接入受控队列、审批和实际工作环境。与独立 L3 首页体验线协调，不重复派工。
