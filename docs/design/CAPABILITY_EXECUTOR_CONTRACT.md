# 能力执行器共享接口合同（FROZEN，root 已批准）

状态：FROZEN。基线：`feature/external-capability-governance` @ `f4d4842`。本文与实际实现一致；不再保留提议稿中的 `local_ocr`、SearXNG images、`run+tool` 生成 call id、False 视为已批准、首次逻辑调用一次计额等否决内容。

## 1. 所有权

| 归属 | 文件 |
| --- | --- |
| 紫色（purple） | `capability_settings.py`、`capability_policy.py`、`capability_execution.py`、`capability_usage.py`、`capability_receipts.py`、`capability_credentials.py`、`capability_diagnostics.py`、`config.py`、`db.py`、`models.py`、`service.py`、`web_tools.py`、`main.py`、`knowledge.py` 旧外部 OCR 门禁；客户端 `types.ts`、`capabilitySettings.ts`、`CapabilitySettingsPage.tsx`、助手权限编辑器及设置 CSS（仅最小功能接线，不重做整体设计） |
| 橙色（orange） | `deepseek_api.py`、`capabilities.py`/`external_capabilities.py`、OCR/图片适配器、`authorized_uploads.py`、`workspace_tools.py`、`evidence.py`、`harness_tools.py`、`harness_attachments.py`、`security.py` 新工具风险映射、`Sources.tsx`、来源 CSS/图像组件 |

`db.py` 由紫色单写；橙色提交所需字段/约束。公共 `types.ts` 由紫色单写。

## 2. 冻结的公共接口

```python
# server/app/capability_execution.py
@dataclass(frozen=True)
class RunContext:
    run_id: str
    conversation_id: str
    user_id: str
    assistant_id: str
    tool_call_id: str          # 实际主机工具调用 ID，绝不由 run+tool 生成
    recheck: Callable[[], None]  # 主机 runtime.check_tool(精确名称/参数) + active run
    approval_id: str | None = None  # 仅 False（需审批）时提供，指向一次性回执

@dataclass(frozen=True)
class RouteResult:
    value: Any
    provider: str
    requested_provider: str
    fallback: bool
    attempts: int
    usage_id: str
    settings_revision: int

class CapabilityExecutor:
    def __init__(self, service)
    async def execute_for_run(
        self, context: RunContext, capability: str, arguments: dict,
        operation: Callable[[str, float, Callable[[], None]], Awaitable[Any]],
        *, target_url: str | None = None, external_upload: bool = False,
    ) -> RouteResult
    def effective(self, context: RunContext, capability: str, arguments: dict) -> dict
```

`operation(provider, absolute_deadline_monotonic, recheck)`：只发一次真实请求；无内部重试、无授权决策、不登记 evidence、不读写配额。适配器用 `max(0, deadline-time.monotonic())` 作为剩余超时。

```python
# server/app/capability_policy.py（同步、无配额、无网络）
CAPABILITY_TOOLS: dict[str, str]
def capability_for_tool(tool_name) -> str | None
def effective_capability(capability, *, deployment_allowed, user_enabled,
                         assistant_grant, snapshot_grant, global_route) -> dict
def authorize_source_access(service, source_row, user) -> None
def classify_error(exc) -> str  # permission|input|transient|cancel|unknown
```

```python
# server/app/capability_receipts.py（紫色写；橙色桥调用 issue）
def issue_approval_receipt(service, *, run_id, tool_call_id, tool_name, arguments,
                           decision, source, approval_id=None, expires_at=None) -> str
def claim_approval_receipt(service, *, run_id, tool_call_id, tool_name, arguments,
                           call_scope_id, require_human=False, expected_receipt_id=None) -> str
def verify_approval_receipt(service, *, run_id, tool_call_id, tool_name, arguments,
                            call_scope_id, require_human=False, expected_receipt_id=None) -> None
```

回执唯一键 `(run_id, tool_call_id, tool_name, arguments_digest)`；首次 dispatch 在事务内标记 `claimed_by_call=call_scope_id`。同一逻辑调用的后续 recheck 读取自己已绑定的回执，不重复消费；不同调用、参数变更、过期、跨 run/工具、重放一律拒绝。禁止“最近 allowed 行”查找。自动授权（`authorize_tool=True`）不需要虚构回执。模型代审行的 `expires_at == decided_at` 不作为机械未来时间判断；以回执 `decision` 与一次性消费为准。需要人工外发批准时 `source` 必须为人工/双人（`user|dual|human_egress`），不接受 `model`。

## 3. 有效权限与默认值

- 四能力：`search|fetch|ocr|image_search`。`enabled` = 部署 AND 用户启用 AND 助手授权 AND run 快照授权 AND 全局路由启用。**缺失能力一律 False**；旧助手 `web_search_enabled` 仅显式映射 `search/fetch`，旧 run 快照同理；稀疏 v2 对象中未列出的能力为 False。
- 域名：全局与助手求交；`allowlist` 空前缀/通配符/IP 网段拒绝；`public` 仍走 `public_url`。域名限制只约束“返回内容/原件访问”，不宣称上游搜索服务只在指定域内检索。
- 外发：`deny < approval < allow` 取最严；OCR/图片上传默认 `deny`。OCR 与 `reverse_image` 的上传外发是独立权限。
- 配额：`calls_per_run`/`calls_per_day` 按**每次真实 attempt** 原子计数（逻辑调用与 attempt 分开）。OCR `max_attempts=1`、无回退，避免收件后远端继续执行/重复计费。
- 新能力默认关闭；搜索/读取在管理员首次保存 v2 前保持环境旧行为。
- 成员语义：`auto_approved` 仅管理员可设；非管理员只能收窄 domains/egress/limits（运行期始终与全局求交），不能把 denied 改成 allow 绕过管理员上限。团队助手仍仅管理员可编辑。

## 4. 供应商（本轮冻结）

| 能力 | 可用 provider | 说明 |
| --- | --- | --- |
| search | `bing_rss`、`searxng`、`brave`、`deepseek_api` | 现有适配器 |
| fetch | `public_http`、`deepseek_api` | 现有适配器 |
| ocr | `deepseek_api` | 仅官方 OCR；`max_attempts=1`；无本地假 provider |
| image_search | `deepseek_webtools`（`reverse_image`）、`brave_images`（`text_search`） | 固定官方端点；SearXNG 本轮不做；DeepSeek 独立 Search 不支持图片 |

`configured=true` 只表示依赖/凭据就绪，不等于 entitlement 或真实效果；verification 默认 `not_tested`。

## 5. 密钥、诊断、安全传输

- `PUT /capability-settings/providers/{id}/credential` body `{revision, credential}`；独立 `DELETE`。仅启用的非试用管理员。数据库仅存 `SecretStore` 密文；GET 只返 `present`/`revision`；审计只记谁何时替换/清除。删除写 tombstone，不得静默恢复环境 key。
- 安全传输：只接受服务端/中间件验证后的 `https`；`X-Forwarded-Proto` 仅在显式 `AGENT_TRUST_PROXY_HEADERS=true` 时采信；`Host` 头不能作为证据；本地开发需显式 `AGENT_CAPABILITY_LOCAL_DEV=true` 且真实 socket 对端为 loopback。远端 HTTP 伪造转发头/Host 一律拒绝。
- 诊断：`POST /capability-settings/providers/{id}/diagnostics` 只接受 `{capability, settings_revision, provider_revision, sample_id}`，固定服务端样例；真实网络探针由 root 授权后执行；本任务只做合成测试。

## 6. 兼容性

1. `Service.authorize_tool` 签名/布尔语义不变；四能力统一委托 `CapabilityPolicy`。
2. `WebTools.search_for_run/read_for_run` 返回字段不变；内部走执行器。
3. `harness_tools.py` 审批链由橙色维护；执行器在已批准 handler 内运行，`recheck` 仍是业务重查，不是批准回执。
4. 工作流保持禁用；`Sources.tsx` 归橙色；前端只做最小功能接线。
5. Evidence `capability`/图片字段由橙色写；紫色提供常量与 `authorize_source_access`。
