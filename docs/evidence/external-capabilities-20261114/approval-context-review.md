# RunContext 审批接线审查

基线：学校云 `/media/data3/dengkw/projects/open-claude-code-main-merge`，`65a6170`。只读检查主线与本机已安装依赖源码，未进入两位工程师分支、未修改产品/测试/配置/看板、未读密钥、未运行模型/网络或部署。唯一产物为本文。

## 1. 可立即转发的结论

冻结的 `RunContext(run_id, conversation_id, user_id, assistant_id, tool_call_id, approval_id, recheck)` 可保留，但现有代码不能直接填齐这些字段。必须先补一条主机侧审批回执和 SDK 调用关联桥，否则实现者可能用“最近 allowed 审批”补洞，造成审批复用或串调用。

最小接线：保留原 `approve(...) -> bool` 默认接口，增加仅供四项能力使用的结构化回执选项/方法；Harness 在当前调用栈拿回确切 approval_id 后直接绑定 RunContext；SDK 从 `permission` 的 `context.tool_use_id` 获取精确调用 ID，通过主机生成的一次性 nonce 写入 `PermissionResultAllow(updated_input=...)`，SDK 专用 MCP 包装层验证并消费 nonce、剥离内部字段后才调用共享能力处理器。现有其它工具不必改审批接口或扩大双人审批范围。

两者都不得把 `Service.authorize_tool(...) is False` 当作批准；False 只表示需审批。只有已审核的精确回执可以使这种调用继续。异常始终是业务拒绝，不能被旧 approval_id 抵消。

## 2. 静态证据与当前断点

| 环节 | 当前代码 | 结论 |
| --- | --- | --- |
| 持久审批状态 | `server/app/runtime.py:567`、`:647`；`server/app/main.py:626` | 批准状态为 `allowed`；不是 `approved`。其它状态包括 pending、denied、expired |
| 审批 ID 出口 | `server/app/runtime.py:545`、`:563`、`:575`、`:632` | SessionActor.approve 创建精确 ID，但模型、人工、双人分支最后都只返回 bool |
| 审批记录结构 | `server/app/db.py:102`、`:233` | 有 run/user/tool/arguments/policy 信息，没有普通工具调用 ID 或一次消费绑定 |
| Harness 调用身份 | `server/app/harness_tools.py:118`、`:125`、`:132` | payload.callId 经格式校验，`(run_id,callId)` 去重；桥内另生成 event_id |
| Harness 审批后执行 | `server/app/harness_tools.py:149`、`:158`、`:165` | 批准后重查正确，但调用共享 handler 时只传 arguments，未传 approval_id/call ID |
| Harness 原生审批 | `server/app/harness_runtime.py:334` | 子进程 `approval/request` 一律 rejected；必须使用已有主机 runtime.approve，不能增加第二套 child approval |
| SDK 调用 ID | `server/app/runtime.py:166`、`:178` | PreToolUse 得到 tool_use_id；permission 的 context 当前未被使用 |
| SDK 共享工具包装 | `server/app/capabilities.py:6`、`server/app/workspace_tools.py:25` | SDK 工具注册直接包 definition.handler，后者只收 arguments |
| 当前共享 handler 的 False | `server/app/workspace_tools.py:54` | 依赖外层已经完成审批，所以忽略 check_tool 的 False；新执行器不能继承这一假设而没有可验证回执 |

`harness_memory.py:194` 现有“按 arguments 查询最新 allowed”带独有 operation_id 和记忆专用验签，不能作为新通用能力复制模板；普通 query/url/file_id 可重复，不能凭相同参数复用旧批准。

## 3. SDK 版本证据：有精确 tool_use_id，但 handler 不会自动收到

主线 `server/pyproject.toml` 固定 `claude-agent-sdk==0.2.178`、`mcp==1.30.0`。只读查询 `/media/data3/dengkw/projects/open-claude-code/.venv` 的包元数据，实际安装版本与此一致。

包源码根为 `/media/data3/dengkw/projects/open-claude-code/.venv/lib/python3.10/site-packages/`。

- `claude_agent_sdk/types.py:223` 定义 ToolPermissionContext，`:230` 有 `tool_use_id`；其注释明确：真实 can_use_tool 回调中该值总是非空字符串，同一 assistant message 的不同调用有不同 ID。生产代码仍应非空校验，测试缺失时拒绝，不能随机补一个 ID 冒充原调用。
- `claude_agent_sdk/_internal/query.py:630` 从 permission 请求构造 `tool_use_id`，`:639` 将该 context 传给 can_use_tool。
- 同文件 `:649` 附近把 PermissionResultAllow.updated_input 作为 `updatedInput` 回传，支持主机侧修改本次工具输入。
- `claude_agent_sdk/__init__.py:633` 最终仍只调用 `tool_def.handler(arguments)`；MCP wrapper 不把 permission context 传入 handler。
- `claude_agent_sdk/_internal/query.py:406` 为各 control request 创建独立任务，`:685` 的 mcp_message 另外路由至 SDK MCP server。ContextVar 在 permission 内 set 后不会自动传播到之后另一个任务里的 MCP handler；不能用这一方式补上下文。
- MCP RequestContext 的 request_id 是 JSON-RPC 请求号，代码没有证明它等于 SDK tool_use_id；不得直接替换。也不能假设供应商/SDK 自动把 tool_use_id 放在 MCP `_meta`，当前主机 wrapper 没有此合同。

因此，`runtime.last_approval_id`、`runtime.current_tool_call_id` 单个可变字段以及 `(tool,args)` FIFO 都不适用于 SDK 并发工具。必须用明确的主机发行关联票据。

## 4. 最小审批回执：保持 bool 兼容

建议在 SessionActor.approve 增加可选 `return_receipt=False` 与 `tool_call_id=None`，或提供 approve_with_receipt 并让原 bool 方法复用内部实现。不要简单把原返回值换成 dict/dataclass，否则旧 `if allowed` 会把一个拒绝对象也当真。

```python
@dataclass(frozen=True)
class ApprovalReceipt:
    allowed: bool
    approval_id: str | None
    tool_call_id: str

# 旧调用保持默认 bool；新能力显式获取 receipt 并检查 receipt.allowed is True。
receipt = await runtime.approve(
    tool, public_arguments, review=review, review_checked=True,
    tool_call_id=call_id, return_receipt=True,
)
```

回执必须由实际创建审批行的同一次调用构造，取人工/双人最终 settled row 的 ID 和结果，不用 emit 事件推断，也不在函数返回后搜索数据库。自动安全读取不需要伪造审批行，context.approval_id 可以为 None；但仅当当前业务授权为 True 且此次 model/dual 策略都不要求审批时允许 None。

批准应在持久化绑定中关联 `(approval_id, run_id, tool_call_id, canonical_tool, exact_arguments_digest)`，通过唯一约束禁止一个 approval_id 分配给不同调用。可以在紫色已有能力调用/使用记录表中增加这组绑定，避免另造审批表；也可给 approvals 增加 nullable tool_call_id，由紫色统一迁移。无论形式如何，必须防止同 run 同参数的两个不同调用复用一条批准。

execute_for_run 校验的是这一精确绑定：审批行 user/run/tool/arguments 对应 context、status=allowed、有效策略未撤销、调用 receipt 未被另一调用消费。登记/claim 以事务保护；首次 dispatch 后的同一逻辑调用重查允许读取自己已绑定的 receipt，不应把自己的后续 recheck 误判为重复使用。不同调用、重新进入 handler、重放 nonce 一律拒绝。

注意时间语义：模型代审行目前 `expires_at == decided_at`（`runtime.py:568`），不能对所有 allowed 行机械要求 expires_at > now，否则模型代审全部失败。四能力需要人工外发批准时应验证 user/dual_admin 来源，不能接受 model 行；其它允许模型代审的调用应按原模型策略复核语义处理。人工审批等待截止与一次执行票据有效期也应区分。

## 5. Harness：可直接在当前调用栈接入

1. 在 bridge.invoke 中已经有 validated payload.callId、run、tool、arguments 和局部 recheck。approve 获取结构化 receipt 后、active/check_tool/unchanged_policy 通过，再构造 RunContext。
2. 明确 context.tool_call_id 的含义：建议使用已校验 payload.callId，并把 started/completed 的 tool_call_id 统一到该 ID；现有 event_id 若保留则作为单独 event_id。另一种兼容方案是 context 使用现有 event_id，同时持久记录 transport_call_id=payload.callId；两方案选一个冻结，不能 receipt 用 callId、usage 用 event_id 却无映射。
3. 共享 WorkspaceTool 增加仅主机使用的 capability handler/context 参数或包装函数；不能把 context/approval_id 混入模型 arguments。局部 ContextVar 可以只在这个同一次 handler 调用栈内 set/reset；它不是跨 SDK 回调的关联方案。
4. 既有 payload 去重、取消、审批前后重查与业务 MCP 授权保持。`runtime.tool_call_id` 只是 Harness 当前传输任务的取消关联字段，不作为两运行时通用权限依据。

## 6. SDK：一次性 nonce 通过 updatedInput 关联

仅四能力使用如下路径，不要求其它工作区工具一起改造：

1. PreToolUse 对四能力完成部署/业务校验后返回 `ask`，使它们经过 permission 回调；这不等于每次人工弹窗。permission 内当前 automatic=True 且无额外策略时仍直接允许。其它工具保留原路径。
2. permission 获取并核对 `context.tool_use_id`，冻结 canonical tool、public arguments 和 run；按既有 model/dual/user 链得到精确 receipt。拒绝、配置变化或取消不发行 nonce。
3. 由主机使用安全随机数生成 nonce，服务端保存 run/session、tool_use_id、canonical tool、规范化精确参数 digest、approval_id、能力/助手/审批策略 revision 与到期时间。OCR 需要绑定已审核上传 ID 和内容 hash；供应商外发集合/配置版本也应固定，避免批准后换 provider 或文件内容。
4. permission 返回 `PermissionResultAllow(updated_input={**public_arguments, "__oc_capability_ticket": nonce})`。保留字段只是传输票据，不接受模型指定身份；模型输入中原先存在该字段直接拒绝，不能信任、覆盖后忽略或允许它影响原参数 digest。
5. 在 SDK 专用 MCP 包装层提取并原子消费 nonce，核对实际 tool、当前 run/session、剥离票据后的精确参数、当前权限/策略/审批记录，再由服务端票据建立冻结的 RunContext。票据不存在、已用、过期、撤销、跨 run/工具/参数均拒绝；失败不重新放回可用池。消费后仍由 execute_for_run 在 dispatch 前继续重查。
6. 剥离票据后，才校验公共参数 schema、调共享业务 handler、记录 tool arguments/来源。不要向供应商、模型工具结果、审计正文、日志、异常或来源 locator 输出 nonce。拒绝错误用固定错误码，不打印原 arguments。SDK post-hook 输入若含 nonce，也必须先去除再记录。
7. run 完成/取消、会话失效、策略/凭据换版时撤销未消费票据；旧 SDK 会话下一轮不能携带它继续调用。cleanup 要有总量与 TTL 限制。每次 execute 只消费一次票据，operation 内 retry/recheck 使用已绑定上下文，不重复消费。

**输入 schema 的关键接点**：当前 SDK helper 在调用 handler 之前做 jsonschema.validate。若新 schema 使用 additionalProperties=false，直接注入隐藏字段将先被 SDK 拒绝；不能为了 nonce 把公共工具参数校验整体放宽。最小稳妥做法是在 `capabilities.py` 给这四项能力提供专用 MCP dispatch 包装：模型公开的 schema 不含票据，主机 dispatch 先取出保留字段并核验，再对余下参数严格校验。SDK 当前支持提供自建 `mcp.server.Server` 实例（本机 `claude_agent_sdk/__init__.py:580` 附近说明）。若使用 SDK helper 的定制 schema/wrapper，必须以协议测试证明内部字段可到 wrapper 且未知公共参数仍被拒绝；不要假定它自动跳过 schema。

票据不是第二套审批：它只携带既有批准与本次调用的关联，不能自行批准。签名字符串但无一次消费状态也不够，必须防止重放。若暂不完成这一 SDK 桥，应对缺少可信上下文的四能力调用明确拒绝；不得用“最近 allowed”、参数相等、单个 runtime 属性或 ContextVar 作为上线替代。

## 7. 只限四项能力修补的策略差异

- Harness 显式计算 requires_dual（`harness_tools.py:144`）；SDK guard/permission 当前没有对称检查，automatic=True 时可能不调用 actor.approve。四能力桥应把当前 dual 判断纳入 ask/receipt 条件，不能为了本次任务顺便修改全部其它工具的审批范围。
- `ModelApproval.review` 在低风险工具的 authorize_tool 返回 False 时生成 deny review（`model_approval.py:270`）；因此把外发需人工批准工具简单继续标 low，会在启用模型代审策略时被拒绝，根本到不了人工卡。明确“需人工外发批准”在四能力 dispatch 中使用原 actor 的人工/双人路径：不能把模型 deny 改 allow，也不要削弱全局 ModelApproval.verify 的 `is True` 要求。治理侧最好返回独立、可信的 need_human_egress 决策供桥选择；仅为这个明确原因跳过模型代审，不能把所有 False 一概绕过策略。
- `recheck` 不是批准回执。它可以抛出业务拒绝并核对 active run/配置，但当授权仍返回 False 时，必须同时验证本次精确已批准的 receipt；有 receipt 不能跳过 recheck。
- 已冻结 operation 的第二参数是 **absolute monotonic deadline**，适配器应计算 `max(0, deadline-time.monotonic())` 作为剩余超时；不能把绝对时钟值直接作为秒数传 httpx。operation 内调用同一个 recheck，且只发送一次供应商请求。

## 8. 实现归属与最小离线验收

橙色负责 `runtime.py` 的 actor 回执选项、SDK guard/permission 桥，`capabilities.py` 专用 SDK dispatch 包装，`harness_tools.py` 局部上下文及共享工具接入。紫色负责冻结 RunContext/execute_for_run、持久化 receipt/attempt 绑定与唯一约束、ACL/外发决策。公共 db.py 由紫色单写；橙色只提交需要的字段/约束。两位先对齐 call ID 语义、回执接口和 nonce helper 签名，不另改其它工具。

必须有以下离线协议测试，不能仅把 approve mock 成 True：

1. 真实 actor 人工允许/拒绝/超时/取消，精确审批行 ID 进入 RunContext；回执拒绝对象不能 truthy 放行。
2. 两个相同 tool/arguments 的不同调用并发，分别批准，票据/approval ID 不串；只批准一个就只能出站一次。
3. SDK 仿真 PreToolUse -> permission（不同 tool_use_id）-> MCP updatedInput -> handler；真实锁定 SDK 的 schema 层不丢票据，公共未知参数仍拒绝。不同回调独立 task，不依赖 ContextVar 继承。
4. nonce 伪造、模型预设、重放、过期、跨工具/run/参数、批准后替换上传 hash 或路由、取消后调用，全部零出站；普通日志/异常/供应商 payload 无内部字段。
5. SDK/Harness 的新能力在 dual 当前启用时均进入原双人审批；need_human_egress 不被 model-review False 分支误当批准或悄悄吞成普通自动允许。
6. approve 旧调用仍返回 bool，原工作区/记忆工具回归不变；无需真实模型、真实供应商或实际密钥完成上述验证。

本文确认了依赖源码支持 tool_use_id 与 updatedInput；没有运行真实 CLI 工具回调链。因此端到端票据透传应由上述离线协议测试先证明，不能仅凭接口名称宣布已接通。
