# 总控第二轮审查：完成当前实现后、CODE_READY 前处理
2026-11-19 15:48 CST。仍只改 connector/；仍禁止 commit/build/部署/Windows。
1. 新死锁：agent.Connect 在 a.mu.Lock 内调用旧 supervisor.Stop(context.Background())；新的 s.OnChange 在 loop 退出时取 a.mu.RLock。Stop 等 done，而 done 必须等 OnChange 返回。必须在释放 agent 锁后 Stop，处理并发和 stoppingTunnel，添加针对已有隧道再次 Connect / 停止回调的有限超时回归测试。不要用跳过回调或无上限 goroutine 掩盖。
2. agent.New 遇 diagnostics.New 错误现在静默忽略，DiagnosticStats 返回零值会呈现正常。应以固定 storage_error_code / 不可用状态可见，MCP 主链继续。不暴露原始路径/错误。
3. 与服务端契约统一：rpc_error_code 仅十进制 STRING，数值范围 -32700..-32000。禁止 TIMEOUT/TOOL_ERROR 等枚举混入这个字段；错误类型以固定 event_code 表达。云端 Python 会同时收敛。检查 diagnostics.safeRPCErrorCode + authproxy 两处。
4. 首次来源尚未云端登记时，本地 source_saved/source_tools_failed/diagnosis_failed 等会带本地 src；云端严格 source-owned 校验将整批422卡死。上传时只携带已云端登记来源与云端已批准工具，未登记则移除 source_id/tool_name（保留 operation_id/event_code），不放松服务端作用域。已登记来源工具变更也应考虑。不要随意清除其他正常事件。增加首次诊断失败仍能上传、之后登记成功带source的集成测试。
5. authproxy.inspectMCPResponse 对 {}、缺少result/error、EOF未收到response仍默认为成功。对需要结果的 initialize/tools/list/tools/call/ping 仅确认有效响应才成功；notifications/initialized 可按明确ACK语义；SSE需按完整事件，忽略合法进度通知直到对应响应。HTTP200 error或isError都失败，保持业务转发和原有SSE能力。
6. streamSSE 末尾 return result, flushEvent() 有 Go 求值顺序风险：flushEvent更新result时，返回的result已经拷贝。先 err := flushEvent() 再 return result,err，覆盖末尾无空行错误事件。
7. 云端会把日志来源 origin=connector/edge 由服务端决定，platform-events GET只返回真正Edge记录；设备端可仍记录平台到达及MCP结果。新origin只在admin视图暴露，不在设备EventView响应新增未知字段，保证doStrict兼容。
完成后更新短checkpoint和CODE_READY，报告真实测试，不重新读旧长历史。

8. 前端实际运行阻断：page.go formatTime 用 new Intl.DateTimeFormat('zh-CN', {dateStyle:'medium', timeStyle:'medium', timeZoneName:'short'})，ECMAScript Intl 不允许 dateStyle/timeStyle 与 timeZoneName 这种 granular 选项混用，会抛 TypeError，日志一有事件或last_success_at整页渲染失败。去掉timeZoneName或全部用显式年月日时分秒字段；用真实JS运行验证，不止node --check。保留客户本地时区清晰显示。
