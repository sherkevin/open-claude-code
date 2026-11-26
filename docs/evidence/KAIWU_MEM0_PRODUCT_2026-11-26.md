# D3｜Mem0 默认关闭宿主接入正式产品入口（2026-11-26）

基线：`c90b204`（分支 `feat/kaiwu-mem0-product-20261126`）。本切片只把现有默认关闭的 `Mem0Host` 接到一个真实产品 API 入口，未改权威快照、版本复核、单写锁、作用域存储、SDK 租约或退役语义。工程师交付时未执行测试；总控复验记录见末尾。

## 1. 交付内容

| 文件 | 变更 |
| --- | --- |
| `server/app/mem0_product.py` | 新增 `Mem0ProductService` 产品门面 + `create_mem0_router` 四个 owner-only 路由 |
| `server/app/service.py` | `Service.__init__` 构造 `self.mem0_product`（默认无后端，不导入 SDK） |
| `server/app/main.py` | 挂载 Mem0 产品路由；未启用时注册稳定的 403 失败形状 |
| `server/app/config.py` | 新增默认关闭的 Settings 开关（`AGENT_MEM0_PRODUCT_ENABLED` 等），无默认供应商 |
| `server/tests/test_mem0_product_api.py` | 真实 FastAPI + 真实原生快照/SQLite + 注入合成 SDK 的端到端测试 |

未改动：`mem0_index.py`、`mem0_host.py`、`mem0_scope_store.py`、`mem0_retirement.py`、`memory.py`、`memory_snapshots.py`、计划与公共架构文档。

## 2. 产品入口

前缀 `/api/v1`，全部要求登录会话（cookie `authenticated`）：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/conversations/{cid}/memory/semantic/status` | 只读能力视图（enabled/configured/mode/agent/top_k/threshold/预算），不触达后端 |
| POST | `/conversations/{cid}/memory/semantic/query` (JSON q/run_id) | 只读语义检索；失败返回 `status=fallback`，携带来源坐标 |
| POST | `/conversations/{cid}/memory/semantic/sync` | owner 显式维护：把已提交权威快照镜像到派生索引（`infer=False`） |
| POST | `/conversations/{cid}/memory/semantic/retire` | 仅在已提交原生 clear 后做逻辑退役；不删除字节 |

检索响应（owner 自己的记忆内容）：
`status`、`reason`、`run_id`、`fallback`、`context_bytes`、`authoritative_source="native_memory"`，以及每个命中 `{source_path, source_id, content_sha256, score, revision, native_epoch, text}`。命中正文为整段已核实文本，超过宿主字节预算即整体回退。

错误码稳定且不泄露后端/路径/正文：
`MEM0_DISABLED`(403)、`MEM0_QUERY_INVALID`(422)、`MEM0_SOURCE_CHANGED`(409)、`MEM0_SCOPE_DENIED`(403)、`MEM0_UNAVAILABLE`(503)、`MEM0_SCOPE_REQUIRED`(409)。

## 3. 保留的边界

1. **权威不可变**：每次 sync/lookup 仍由 `NativeMemorySourceReader` 复核 owner、禁用/归档、已发布快照记录、初始迁移为空、run 绑定、revision/digest/native_epoch；产品层不新增任何身份参数。
2. **只读检索不创建**：`lookup` 走 `Mem0Host.lookup`（`provision=False`）；缺失/退役作用域在打开 SDK 前失败并回退，不创建私有目录。
3. **失败即回退**：适配器/供应商异常在 `Mem0Host` 内转为 `Lookup("fallback")`，产品返回 200 `fallback`，不当作“无记忆”；权威记忆仍可经原生接口读取。
4. **显式写**：只有 `POST /sync` 触发 `infer=False` 写入；模型/读取路径永不自动同步。
5. **撤权停止**：归档、禁用用户或 run 版本变化后，检索回退、sync 返回 403/409；不会继续向供应商发送正文。
6. **租约不确定拒绝重开**：SDK close 未确认时 `sdk_open` 持久标记保留，后续产品请求不再打开该作用域，只回退；其他作用域不受影响。
7. **逻辑退役 ≠ 物理删除**：`retire` 只在精确已提交 clear 后写持久元数据回执，`cleanup_status=not_performed`、`physical_deletion=false`，不读正文、不删文件、不自动重放。
8. **默认关闭、无默认供应商**：`AGENT_MEM0_PRODUCT_ENABLED=false`，且 `Service` 构造时不注入后端，`Mem0ProductService.host=None`，进程内无 Mem0 SDK 句柄；`load_mem0_backend` 仍由外部显式调用，不在产品代码中。

## 4. 正式产品启用所需配置（尚未在生产启用）

1. 部署环境设置 `AGENT_MEM0_PRODUCT_ENABLED=true`；可选 `AGENT_MEM0_AGENT_ID`、`AGENT_MEM0_TOP_K`(1–50)、`AGENT_MEM0_THRESHOLD`(0–1)、`AGENT_MEM0_MAX_CONTEXT_BYTES`(1–131072)。
2. 由受控集成代码在进程启动后显式调用 `service.mem0_product.configure(scoped_factory=...)`；该工厂必须使用 `mem0_index.load_mem0_backend` 加载**固定** `mem0ai==2.2.1 / openai==3.59.0 / qdrant-client==1.20.0`，传入 `ScopeStore` 给出的私有 `paths.vectors`/`paths.history`，使用经批准的内网/租户 OpenAI-compatible provider，并保持 `MEM0_TELEMETRY=false`、传输超时与 `max_retries=0`。
3. 出站网络、凭据与费用由宿主/容器策略限制；本切片不提供硬预算或远端取消。
4. 未调用 `configure` 时接口一律 403，不安装 SDK、不选供应商、不发客户数据。

## 5. 测试（已写入，未执行）

新增 `server/tests/test_mem0_product_api.py`，12 个用例，覆盖：

- 默认关闭 403、无后端注入时 fail-closed、status 不触达后端；
- 检索前不 provision、不写索引；
- owner 编辑已提交记忆 → 显式 sync → 语义检索 `verified`，来源路径/sha256/revision/native_epoch 可核验；
- 供应商 search 失败 → `fallback`，原生记忆仍可读；
- 非 owner 404；空白查询 422；
- 授权 run 绑定检索成功，run 状态变化后回退；
- 归档会话停止检索与 sync；
- 禁用 owner 停止；
- 逻辑退役需要精确已提交 clear，退役后历史文件仍存在、后续 sync 拒绝；
- SDK close 未确认后跨请求拒绝重开。

总控复验命令（使用项目实际含 FastAPI/pytest 的 Python；按 CLAUDE.md 把临时文件放 data3）：

```bash
cd /media/data3/dengkw/projects/kaiwu-mem0-product-20261126/server
TMPDIR=/media/data3/dengkw/tmp-mem0-product \
  python -m pytest tests/test_mem0_product_api.py -q
# 回归既有基线
TMPDIR=/media/data3/dengkw/tmp-mem0-product \
  python -m pytest tests/test_mem0_index_adapter.py tests/test_mem0_scope_store.py tests/test_memory.py tests/test_deployment_policy.py -q
```

预期：新套件全绿；原有 Mem0/原生记忆/部署策略套件不回归。若环境缺少固定 SDK，产品测试仍应通过（使用注入合成后端，不需要真实 Mem0 安装）。

## 6. 剩余依赖与风险

1. **真实 Service + 真实 SDK 同进程**：本切片用合成后端验证产品 API；真实 `load_mem0_backend` 与 `Service` 同进程仍待独立联调（原证据已注明 SDK pilot 环境缺 `cryptography`）。
2. **硬预算/质量**：无按次硬费用上限、远端取消或中文记忆质量评测；未做 LocMemEval/LoCoMo 对照，不得据此默认开启。
3. **删除专用授权与新代次**：逻辑退役不等于物理删除；删除字节、历史 SQLite 清理、新代次重建仍需独立授权事件，本切片不实现、不自授权。
4. **UI**：只提供 API 产品入口，未接工作台界面；模型运行中自动注入上下文也未接线（仅提供带 `run_id` 的显式检索）。
5. **并发接线冲突点**：`server/app/service.py`、`server/app/main.py`、`server/app/config.py` 是 Goal/Team/Kaiwu 等并行工作的共同热点；本切片只做小范围追加，合并时以最终主线为准核对 import 与路由顺序。`tests/test_deployment_policy.py` 要求 `TRIAL_ROUTES` 不变——本切片未加入 trial 能力，trial 下新路径仍 fail-closed 403。
6. 根分区空间紧张已按 CLAUDE.md 用 data3 路径，不在本记录重复。

## 7. 状态

实现已写盘，**未运行、未提交、未部署、未启用**。默认关闭，无真实供应商、客户数据或生产变更。


## 8. 总控复验（北京时间 14:22）

主线已为 b445307（客户限流修复已合入）；本候选仍基于 c90b204，未合入。初测 2 failed / 10 errors，全部由测试 app 要求真实模型凭据导致；以显式禁止模型调用的 synthetic runtime factory 修正夹具，并修正测试客户端参数、模拟历史文件，第二次真实 FastAPI/SQLite + synthetic Mem0 后端 12/12，退出 0（controller-product-v2.log）。

随后审查收紧产品边界：只允许独立 scoped factory、已配置 host 不允许替换，显式 config 不得覆盖部署开关；查询词改为 POST JSON 请求体，不放 URL 或响应回显（不声称所有日志均脱敏）。新增两项测试，14 项产品测试和现有 deployment policy 组合复验正在执行（controller-product-v3.log/.exit）；本次改动尚未验收。真实 SDK/真实供应商、实际模型上下文、硬预算和删除专用授权仍待，不启用生产。


## 9. 总控最终验收与合并条件（2026-11-26）

工程师已实际退出 0；任务书和原始 JSONL、模型及退出回执留存在 /media/data3/dengkw/acceptance/kaiwu-mem0-product-20261126/。总控将代码提交并基于 b445307 复验。先前产品与权限策略组合 40/40（controller-product-v3.log）。最终扩展组合包括产品 API、派生索引、作用域存储、部署权限、原生记忆及快照崩溃恢复：初次 173 项通过、10 项旧 Claude 记忆测试失败（controller-merged.log）。未包含本次产品接线的主线 b445307 独立复现 MODEL_UNAVAILABLE（controller-memory-baseline.log）。仅修正 tests/test_memory.py 夹具，显式注入禁止模型调用的 runtime factory，不改变生产配置或权限；失败组 10/10 重跑通过（controller-memory-fixed.log）。因此最终分组验收 183/183，不是声称一次全量执行通过。保留全部原始失败与复验日志及既有告警。

交付范围仅为默认关闭、显式隔离工厂注入的 owner-scoped 产品 API。实际生产供应商、真实 SDK 与 Service 同进程、UI、模型上下文自动注入、硬费用预算及独立删除授权仍未完成；未上线、未启用、无客户数据或外部模型请求。下一独立任务详见验收目录 NEXT_TASK.md。合并后只有在远端提交核验、工人退出、工作树干净及证据保留时才清理本任务分支；清理结果由 controller-acceptance.json 实际记录。
