# 华微连接器 1.3.8 内置测试 MCP：连接器交付报告

日期：2026-11-19（北京时间）

## 背景与现状

开物企业桥接连接器的目标安装环境是华微世纪的 Windows Server 2019 内网，需要主动出网，通过原版 FRP、公网 Edge 和开物平台把客户内网 MCP 暴露给智能体。1.3.6 已发内部评审群，并在 Shervin 测试机验证过公网调用；1.3.7 的 Windows 安装验收因未签名安装器审批阻止而未完成，不能把 1.3.7 说成已经通过 Windows 验收。

本轮的 1.3.8 增量是在安装包内自动启动一个只读示例 MCP，让客户先用固定示例数据验证“本机 MCP、企业绑定、公网隧道、公网 Edge/MCP 往返”这条链路，再接自己的业务 MCP。这样可以把连接器、隧道、Edge 和客户业务 MCP 的问题分层定位。

## 交付边界

- 实际工作树基线：`c55c6b3e1d22dd14e080cfaf2da1fc6ef89259a7`。工作区原有未提交改动已保留，没有回退。
- 本报告只代表连接器侧实现与 Linux 本机验证。`server/` 由另一工程师负责；本轮没有修改、部署或验收 `server/`。
- 没有 commit、没有执行完整打包、没有部署腾讯云，没有操作 Shervin、Windows 安装、Windows 服务、UAC 或公网 DNS/防火墙。
- 没有把 Linux 测试或 Windows 交叉编译当成 Windows 实机验收。

## 已完成实现

### 官方 SDK 与内置示例 MCP

- 固定使用 `github.com/modelcontextprotocol/go-sdk v1.8.0`，没有使用浮动版本，也没有自行重写 MCP JSON-RPC 和 Streamable HTTP 会话协议。
- 新增 `connector/internal/demomcp`，使用官方 `mcp.NewStreamableHTTPHandler`，只监听 `127.0.0.1` 的系统分配端口。
- 固定提供三个只读工具：`kaiwu_demo_ping`、`kaiwu_demo_order_summary`、`kaiwu_demo_inventory_query`。
- 示例数据固定为订单数 12、产品 `KAIWU-DEMO-001` 库存 128，并明确标记为示例数据。`ping` 返回本次 nonce、UTC 时间和进程启动标识，用于防止静态假成功。
- Go 模块最低版本已改为 1.25；本次验证使用 Go 1.27.4，Windows 候选为 `CGO_ENABLED=0`、`windows/amd64` 交叉编译。Go 1.27.4 的支持范围覆盖 Windows Server 2019 平台条件，但没有 Server 2019 实机通过证据。

### 示例来源身份与客户来源隔离

- 企业绑定后的来源编号严格使用现有 `src_32hex` 规范：

```text
src_ + SHA256(("kaiwu_builtin_demo_v1:" + connector_id).encode("utf-8")).hexdigest()[:32]
```

- 已增加固定向量：`connector-test-1` 对应 `src_403571374c41e5c2d9f793ad5e334053`，总长仍为 36；没有扩宽来源编号校验，也没有使用 `demo-16hex`。
- 未绑定企业前可以生成本地 `src_32hex` 示例记录并运行本机测试；绑定后只迁移持久化的 `builtin_demo_source_id`，不会按显示名称或工具名认领客户来源。
- 如果服务器派生出的固定编号已被非示例来源占用，返回 `BUILTIN_SOURCE_CONFLICT`，不覆盖、停用或删除该客户来源。
- 业务来源列表、认证状态和导出结果均排除内置示例来源；示例来源不出现在业务来源下拉框中，也不能被业务来源编辑或启停接口修改。
- 同显示名称、同工具集合的客户业务来源保持不变，已有回归测试覆盖。
- 示例开关单独持久化到 `builtin_demo.json`，客户关闭后升级不会强行重新开启。

### 示例自检与故障隔离

- 新增本机 `POST /api/self-test` 和 `POST /api/builtin-demo/enabled`，复用现有 Host、Origin、POST 和会话令牌防护。
- 本机自检通过连接器现有的真实 MCP 客户端完成 `initialize`、`tools/list`、`tools/call` 和 nonce 校验。
- 绑定后只登记示例来源，再通过现有云端入口请求真实公网 HTTPS Edge 和 FRP 往返；请求只携带固定 `source_id` 和 nonce，不包含客户 URL、工具名、凭据或业务数据。
- 页面顶部显示“内置测试服务（示例数据）”、独立启停按钮、一键连接测试、四层状态、公网阶段明细、链路追踪编号、可复制测试问题和固定答案。未执行的阶段显示 `skipped`，不会标绿。
- 客户文案已收敛：顶部只说明“随安装自动启动，使用固定示例数据检查这台服务器到开物平台的连接，不读取您的业务系统”；测试问题旁明确“开物技术支持完成测试服务接入后，可在开物对话中使用”；自检成功只说明链路已通过，地址预览不包含内置示例服务，也不暗示客户已经可以直接提问。nonce 等协议细节只在诊断结果中出现。
- 坏的客户 MCP 不会阻止示例本机代理启动。示例公网自检只登记示例来源；发现工具 schema 或代理问题时不会把客户来源设为停用，也不会删除客户来源。
- 示例自检不会关闭或重建客户来源代理；需要新增示例代理时，只向现有代理集合追加独立示例项。
- 无动态签名来源的已批准工具可以真实试调用，工具允许列表仍强制生效；示例来源不需要 HMAC，`Diagnose` 不再强制要求动态认证配置。

### 第三轮独立审查修复

- `demoOnly` 遇到已有运行中的 supervisor 时，先按连接器实际 FRP common user 构造精确名称：common user 非空时为 `common user + "." + demoSourceID`，common user 为空时直接使用 `demoSourceID`。只接受 STCP 类型、`status=running`、`local_addr` 等于当前示例认证代理监听地址，且该代理上游等于当前示例 MCP 地址的精确路由；不使用后缀模糊匹配、显示名称或管理员用户名 `kaiwu` 认领路由。
- 精确示例路由成立时直接复用现有 supervisor 和 heartbeat，不写 `frpc.toml`、不停止或替换进程。其他业务路由处于错误状态也不会阻断示例专项诊断和真实 nonce 公网自检；`SelfTest` 不再要求全局状态等于 `READY`。
- 已有业务隧道但缺少上述精确示例路由、端口不一致或路由未运行时，返回“测试通道尚未就绪，请在方便时重新连接”。此路径不自动补路由、不调用 `/api/reload`，原 supervisor、配置文件、管理端口、客户代理、heartbeat 和全局状态保持不变。只有不存在旧运行隧道时，才进入原来的完整新建连接流程。
- 当不存在可复用的旧运行隧道、需要新建 FRP 隧道时，示例专项连接（`demoOnly`）同样只查询 `frpcProxyName(demoID, generated.User)` 对应的 STCP 路由，并要求它的 `local_addr` 等于当前示例认证代理端口；它不使用全量代理数量或业务路由错误判定示例是否成功。普通 `Connect` 仍保留原有的全量路由健康检查。
- 已按固定 FRP 0.61.1 官方源码确认：`/api/reload` 只让旧 daemon 重读原配置文件，进程启动时缓存的环境变量不会吸收新密钥。本版本因此不实现依赖 reload 的热更新体系，也不写明文密钥补丁。
- 示例关闭再开启时，保留同一个示例认证代理 listener 和 FRP localPort，只安全更新上游到重新启动后的示例 MCP，并清除旧 MCP 会话。关闭期间旧代理不能继续返回示例数据；重新开启后，下一次示例专项检查会原地更新上游，无需重启客户业务隧道。
- `CloseContext` 先标记关闭并取消共享运行时派生的在途连接和自检；listener、认证代理、示例服务、supervisor 与 heartbeat 的创建和关闭受生命周期保护，避免云端阻塞请求在关闭后重新拉起进程。

### CLI 与 Windows SCM 统一生命周期

- `cmd/connector/main.go` 的 CLI 路径和 `service_windows.go` 的 SCM 路径统一调用 `Agent.StartRuntime()`。
- `StartRuntime` 统一执行诊断启动、启动事件记录、内置示例服务启动和按原状态自动连接。
- CLI 与 SCM 停止时统一调用 `CloseContext`，关闭 frpc、认证代理、示例监听和诊断队列；`StartRuntime` 与关闭路径都有幂等保护。
- 修复旧隧道替换死锁：停止旧 supervisor 时已经释放 `Agent` 锁，停止回调不会再和状态锁互等；已有有限超时回归覆盖第二次 Connect。
- 诊断初始化失败时不再静默显示正常，固定返回 `LOCAL_STORAGE_FAILED`，并保证 MCP 主链继续工作、原始错误和路径不泄露。

### 第二轮审查的其余修复

- `rpc_error_code` 只接受 `-32700..-32000` 的十进制字符串；`TIMEOUT`、`TOOL_ERROR` 等枚举不能进入该字段，错误类型继续由固定 `event_code` 表达。
- 日志上传只保留已登记来源和已批准工具的作用域；首次诊断失败仍可在清除来源作用域后上传，登记成功后恢复来源字段。
- `authproxy.inspectMCPResponse` 只有收到匹配请求的有效 JSON-RPC 响应才判定成功；空对象、缺少 `result/error`、HTTP 200 的 `isError` 和只有进度通知的 SSE 都会失败。
- SSE 末尾没有空行的事件先执行 `flushEvent` 再返回，避免 Go 返回值拷贝错过最终错误结果。
- 页面时间格式已去掉 `dateStyle/timeStyle` 与 `timeZoneName` 的非法混用，使用显式年月日时分秒并保留本地时区；新增 Node 真实执行测试，不只做语法检查。

## 第三方许可证

- `connector/third_party/mcp-go-sdk/LICENSE` 已与 Go 模块缓存中的 SDK v1.8.0 原始文件逐字比对一致。
- 该固定版本许可证是 MIT 到 Apache-2.0 的过渡全文，同时包含 Apache-2.0、MIT 和文档 CC-BY-4.0 条款，不能概括成仅 MIT 或仅 Apache-2.0。
- 已按候选 Windows 二进制的实际依赖复制以下模块的 LICENSE；包含 PATENTS 的模块一并保留：
  - `github.com/google/jsonschema-go v0.4.3`
  - `github.com/segmentio/asm v1.1.3`
  - `github.com/segmentio/encoding v0.5.4`
  - `github.com/yosida95/uritemplate/v3 v3.0.2`
  - `golang.org/x/oauth2 v0.35.0`
  - `golang.org/x/sync v0.20.0`
  - `golang.org/x/sys v0.41.0`
  - `golang.org/x/time v0.15.0`
- `connector/scripts/build.sh` 已配置把 SDK 原文和上述传递依赖许可证复制到 Linux/Windows 的 `compliance/` 目录。
- `go mod verify` 通过。`go list -m all` 中的测试专用模块没有链接进候选二进制，因此不列入运行时传递依赖许可目录。

## 已执行验证

验证结果分两段记录：前两轮实现阶段执行过更广测试；第三轮最终补丁完成后只按总控要求重跑受影响包的定向测试，不把前两轮全量结果表述为最终补丁后的全量复测。

所有 Go 命令使用 data3 下的缓存和临时目录：

```text
GOCACHE=/media/data3/dengkw/cache/huawi-demo-138/go-build
GOMODCACHE=/media/data3/dengkw/cache/huawi-demo-138/go-mod
GOTMPDIR=/media/data3/dengkw/cache/huawi-demo-138/tmp
TMPDIR=/media/data3/dengkw/cache/huawi-demo-138/tmp
```

### 第三轮最终定向复核

- `gofmt -l connector/internal/agent/agent.go connector/internal/agent/review_round3_test.go`：无输出，格式通过。
- `go test ./internal/agent ./internal/authproxy ./internal/demomcp -count=1`：三个受影响包全部通过。
- `go test -race ./internal/agent -run 'Test(DemoNewTunnelQueryOnlyChecksExactRouteAfterSupervisorExit|DemoSelfTestUsesExactRunningRouteDespiteCustomerError|DemoSelfTestWithoutExactRouteKeepsCustomerTunnel)$' -count=1`：全部通过。
- `go vet ./internal/agent ./internal/authproxy ./internal/demomcp`：通过。
- 精确路由回归同时放入不带 common user 的 `demoSourceID` 和 `other-user.demoSourceID` 干扰路由，并把干扰路由的 `local_addr` 设为错误端口；只有实际 `tenant-demo.demoSourceID` 路由能通过，证明没有使用后缀或同名模糊匹配。
- 旧 supervisor 退出后，新建分支专项回归同时放入坏业务路由和精确示例路由：示例专项查询成功，普通全量连接检查仍因坏业务路由失败，证明两条检查路径没有被混用。
- 业务存在坏路由、全局状态为 `DEGRADED_RELAY` 时，精确示例路由仍完成本机协议检查和一次云端 nonce 自检，且不替换 supervisor、认证代理、heartbeat 或 frpc 配置。
- 缺少精确示例路由时返回重新连接提示；回归确认 `/api/reload` 调用次数为零，业务 supervisor、PID、端口、配置文件、heartbeat 和状态保持不变。
- 示例关闭再开启复用同一认证代理 listener；关闭期间旧代理请求失败，重新开启并通过专项连接后返回新实例标识和正确 nonce，客户代理与模拟隧道保持不变。
- 使用受控 `httptest` 阻塞在途 `AutoConnect`，调用 `CloseContext` 并释放响应后，没有留下新的 frpc、配置文件、heartbeat、认证代理或示例 MCP listener。

### 前两轮实现阶段执行记录

- `go test ./... -count=1`：前两轮实现代码全部通过。
- `go vet ./...`：前两轮实现代码全部通过。
- `go test -race ./internal/agent ./internal/authproxy ./internal/cloud ./internal/demomcp ./internal/web ./internal/diagnostics -count=1`：前两轮实现代码全部通过。
- `TestPageFormatTimeRunsInJavaScript`：使用 Node v20.20.2 实际调用页面函数，输出 `2026/11/19 GMT+8 11:04:05`，未发生 `Intl.DateTimeFormat` 异常。
- `TestPageSelfTestRendererRunsInJavaScript`：在 Node 中实际执行分层结果渲染，确认本机、绑定、开物平台连接状态和追踪编号可见，并显示包括临时凭据清理在内的全部云端阶段。
- `TestStartRuntimeAndCloseAreIdempotent`：通过，重复启动没有创建第二个示例实例，重复关闭没有留下监听器。
- `TestBuiltinDemoConnectFailureKeepsCustomerProxyAndState`：通过，示例登记失败时客户代理对象、端口和 READY 状态保持不变。
- `TestConnectReplacingRunningTunnelDoesNotDeadlockOnStopCallback`：通过，第二次 Connect 在有限时间内完成且旧隧道已停止。
- `bash -n connector/scripts/build.sh`：通过。
- SDK 原文比对：`SDK_LICENSE_EXACT_MATCH`。
- 最终客户文案和测试问题说明修改后，重新执行 `go test ./internal/web -count=1`，通过。

## 构建状态

本轮没有执行最终 1.3.8 打包。最终客户文案修改前曾完成 Windows/amd64 交叉编译可行性验证，但该中间产物不包含最终文案，因此不再作为交付候选，也不记录为交付 SHA。最终构建和 SHA 由总控执行。

## 未执行事项

- 未执行最终安装包构建、NSIS 安装器构建、签名、发布和 SHA 清单封版。
- 未操作 Shervin，未安装或卸载 Windows 服务，未验证 UAC、SCM 启停、端口释放、升级保留或 Windows 日志上报。
- 未在 Windows Server 2019 实机运行；不能声称 Server 2019 已通过。
- 未部署腾讯云，未运行真实公网 Edge/FRP 往返，未兑换客户一次性接入码，未完成开物真实智能体对话。
- 未做带真实后端的浏览器完整页面自动化；已完成页面时间函数和分层结果渲染的真实 JavaScript 运行验证，但最终的 Windows 图形界面、按钮和视觉效果仍需总控在 Windows 验收中确认。

## 下一步

1. 总控合并服务端 self-test 后做接口审查，确认两端的来源派生公式、分层响应和清理语义一致。
2. 总控重新执行完整构建并生成 1.3.8 安装器、许可证目录和 SHA256 清单。
3. 按标准安装流程完成 Windows SCM 启停、日志上报、示例关闭/恢复、客户来源并存和 Server 2019 实机验证。
4. 只有在公网 Edge/FRP 往返和开物真实对话都通过后，才能把 1.3.8 称为客户可交付版本。

```text
CODE_READY connector 1.3.8
```
