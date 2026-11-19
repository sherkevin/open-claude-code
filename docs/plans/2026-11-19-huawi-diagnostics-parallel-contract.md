# 1.3.7 并行接口契约（总控冻结）

旧工程师 run87 已在 T3 正常 Stop，CLI 验证 status=interrupted、activeRunId=null、pendingRequestCount=0。不能恢复旧工程师。
共同工作树：/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118，分支 feat/huawi-enterprise-bridge-20261118。基线1.3.6 6e44dbb。
现有未提交 diagnostics 与 cloud.Client 是本任务WIP，允许继承修复；保留其他dirty文件。

## 所有权

- Connector worker：只写 connector/，自己的 CONNECTOR_CODE_READY.json 和 CONNECTOR_CHECKPOINT.md。
- Server worker：只写 server/app/kaiwu_diagnostics.py（新）、server/app/kaiwu_control.py、server/app/kaiwu_edge.py、server/app/db.py，以及 server/tests/ 对应测试；必要时 main.py 最小router挂载；自己的 SERVER_CODE_READY.json / SERVER_CHECKPOINT.md。
- 总控：代码审查、合并提交、发布协调、腾讯云部署、Windows现场、验证与最终交付。
- 两位worker不commit、不checkout/reset/clean、不打包、不部署、不操作Windows、不改对方文件。全局任务文件/共同状态文件由总控维护，勿争写。

## 唯一接口契约

API前缀由现有 router 决定，客户端BaseURL=https://flintw.com/api/kaiwu。
POST /v1/connectors/{id}/events，设备Bearer+Ed25519，body={"events":[Event,...]}。
成功200 body={"accepted":N}，只在已持久化后ACK，重复event_id计为已接受/幂等成功；整个batch校验失败则不部分写入。
GET /v1/connectors/{id}/platform-events?limit=200，设备签名+仅自身connector；成功body必须只有 {"events":[EventView,...]}，因为Go客户端 doStrict。
GET /v1/diagnostics/events，admin保护，返回 {"events":[EventView,...],"next_cursor":...}；允许必要filters，但设备scope绝不从query建立。
GET /ops（最终URL /api/kaiwu/ops）仅静态无数据登录壳，管理员token只在页面内存，无持久存储、URL或下发Windows。
本地 GET /api/logs、GET /api/platform-logs、POST /api/diagnostics/settings {"paused":boolean}，沿用本地Host/Origin/session防护。
Event字段与JSON类型固定，以现有 diagnostics.Event 为准：schema_version:int(1), event_id:evt_32hex, occurred_at:RFC3339, level:info|warn|error, component:connector|config|source|auth|diagnosis|tunnel|mcp|platform|diagnostics, event_code/message_code:固定目录代码, operation_id?:op_16..64hex, trace_id?:kwtr_24..64hex, source_id?:src_32hex, tool_name?:已批准且安全的工具名, http_status?:int, rpc_error_code?:严格数字字符串(不能任意文本), duration_ms?:非负int, attempt?:非负int。
EventView = Event + message(服务端固定目录翻译，可附tenant_id/connector_id/received_at给admin UI；设备平台接口不添加顶层其他字段)。
Catalog目前位于 connector/internal/diagnostics/catalog.go，Server worker只读它，用同名固定目录；两端不能随意自创不兼容事件代码。
无需新增事件目录即可覆盖平台结果：platform_request_succeeded / platform_request_failed。若必须新增，先写自己的checkpoint注明待总控同步。
Trace header唯一为 X-Kaiwu-Trace-ID；Edge生成并覆盖外部同名头，送authproxy；authproxy记录但不转发到客户MCP。相同trace两端可查。trace仅关联不鉴权。
无free message/details/headers/body/args/result/URL/环境变量等任意字段；云端设备身份推导tenant/connector；校验来源属于该设备，tool_name属于该来源批准集合。不要上传真正凭据、内网地址或业务数据。
现有heartbeat.last_error一并固定安全分类，禁止原始err.Error走上传。
服务端14天保留、每connector最多100000条、全局最多1000000条；有索引、限量分页、合理清理频率。batch最多100条、正文最大256KiB，字段与枚举校验；非法字段拒绝。
Go默认2秒批量、3秒上传超时、退避最大60秒；有限持久队列/异步写盘；与FRP生命周期独立。Local/Cloud不可用不阻断MCP。

## 本轮目标与已授权事实

客户在Windows Server2019 x64运行已有MCP，公网平台不可直连内网。复用官方FRP0.61.1；1.3.6已在Shervin Windows11真机及真实公网A/N/B通过。当前绑定和K保持，禁止真实revoke/rotate。
本次用户明确要求客户视角文案、双端日志和腾讯云近实时诊断上报。输出新的1.3.7图形化EXE。
腾讯云控制面独立18093，主站18092，不覆盖主站。只有总控部署。
所有工具chain/cache/TMPDIR/GOCACHE/GOMODCACHE/pytest temp都在/media/data3/dengkw。构建Go1.27.4位于既有工具环境，可从connector/scripts/build.sh/已有发布记录查；不要重新下载大工具。
无需重读历史对话/1.3.6全部原件。若上下文压缩，读取自己的短checkpoint和本契约即可，继续上次代码位置；不要重做背景确认循环。
