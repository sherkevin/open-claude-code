# Server worker checkpoint（2026-11-19）

## 背景
1.3.7 要求客户 Windows 连接器把安全运行事件上报到腾讯云控制面 SQLite，提供受认证查询和只读运维页，并让真实 MCP Edge 产生平台日志/追踪。两端由不同工程师并行实现，本窗口只负责 server 文件。

## 现状
服务端链路已实现：设备 Ed25519 签名批量上报、设备 scoped 平台日志、管理员分页查询、`/api/kaiwu/ops` 只读运维壳、Edge 随机 `kwtr_32hex` 追踪头与平台结果事件。第二轮审查的 6 项已全部处理。

## 本轮完成
- 新增 `server/app/kaiwu_diagnostics.py`：固定 catalog、严格 Event/EventBatch 模型、来源与工具授权校验、幂等写入、14 天/每设备 10 万/全局 100 万保留策略、cursor 查询、SSE JSON-RPC 帧解析、ops 页面。
- `server/app/db.py`：新增 `kaiwu_diagnostic_events` 表、`origin` 字段及索引；旧库启动时补充 `origin`。
- `server/app/kaiwu_control.py`：新增设备 `POST /v1/connectors/{id}/events`、设备 `GET .../platform-events`、管理员 `GET /v1/diagnostics/events`、`GET /ops`。
- `server/app/kaiwu_edge.py`：Edge 每次生成并覆盖 `X-Kaiwu-Trace-ID`，向 authproxy 带同头并回传；平台成功/失败、状态码、耗时、批准来源/工具写库；HTTP 200 JSON-RPC error/isError 不算成功。
- `server/app/main.py`：安全头改为 `setdefault`，允许 ops 使用更严格的 `no-referrer`/CSP。
- 新增 `server/tests/test_kaiwu_diagnostics.py`，覆盖审查要求的正反路径。

## 第二轮审查修复
- `origin=connector/edge` 由服务端派生，客户端正文不能传；设备平台接口只查 `origin='edge'`，admin 视图返回 origin，设备 EventView 不新增字段。
- Edge 独立写平台事件后也调用节流的 `maybe_prune`，暂停设备上报时仍执行保留边界。
- `rpc_error_code` 冻结为数字字符串 `-32700..-32000`；拒绝正数、`-32999`、`TOOL_ERROR`、`TIMEOUT` 和空串。
- SSE 按空行分帧并合并多 `data:` 行；HTTP 200 但无结果/仅进度/EOF 无结果/读取中断均显式记失败；`notifications/initialized` 空 ACK 单独允许成功。
- 跨设备 source/tool 校验保持严格，未放松。
- Ops 页面不使用 Intl.DateTimeFormat；Node 真实执行测试验证了日期转换、认证头、内存令牌清理和表格渲染。
- `origin` 索引已移到列补齐之后创建；新增隔离旧表迁移用例，确认旧表启动时先补列再建索引，不再出现 `no such column: origin`。

## 测试证据
命令（临时隔离 DB，缓存/TMP 在 data3）：
`TMPDIR=/media/data3/dengkw/tmp PYTHONPATH=/media/data3/dengkw/scratch/tmp/rootfs-archive-20261112/open-claude-pylib python3.10 -m pytest -q tests/test_kaiwu_control.py tests/test_kaiwu_enterprise.py tests/test_kaiwu_bridge.py tests/test_kaiwu_diagnostics.py -p no:cacheprovider --basetemp=/media/data3/dengkw/tmp/pytest-final3-kaiwu`

结果：`25 passed, 1 warning`（warning 为既有 Starlette/httpx 弃用提示）。

## 迁移与回滚
- 迁移：仅新增表和索引；`Database` 启动时自动创建，旧库自动补 `origin` 列。
- 回滚：停止使用新路由/页面；保留表不删除即可。如需彻底回滚，先备份 SQLite，再删除 `kaiwu_diagnostic_events` 表及其索引。

## 未测项与边界
- 未在真实腾讯云 SQLite 上跑 100 万级容量压测；保留策略用缩小上限的单元测试验证。
- 未做真实公网 Edge 与 Windows 连接器联调（由总控部署和 Windows 验收）。
- 未构建 Windows 包、未 commit、未部署。
- SSE 仅能根据已收到的 JSON-RPC 帧判定结果；纯传输进度不伪称工具成功。
