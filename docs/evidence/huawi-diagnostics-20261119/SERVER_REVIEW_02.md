# 总控第二轮审查：完成当前实现后、CODE_READY 前处理
2026-11-19 15:48 CST。仍只改你负责的 server 文件；禁止 commit/build/部署/Windows。
1. 平台日志必须能证明来自Edge：当前设备上传允许platform_*，query_connector_events只按component=platform过滤，会把连接器自报和Edge双计数。DB增加由服务端派生的 origin=connector/edge（客户端正文不允许传），设备POST固定connector，record_platform_event固定edge，platform-events只查询origin=edge。admin视图显示origin。设备EventView不要新增origin等未知字段，以免Go doStrict拒绝。
2. Edge独立调用流也要触发节流的maybe_prune，不能仅设备上传触发；暂停诊断上传仍用MCP时必须仍有14天/每设备10万/全局100万边界。
3. 冻结rpc_error_code为数字STRING，范围 -32700..-32000，与Go完全一致。禁止正数32000/-32999/TOOL_ERROR/TIMEOUT。加契约边界测试；Go工程师同步收敛。
4. JsonRpcStreamInspector必须按SSE空行分隔完整事件，多data行\n合并解析。现有逐data行分类会遗漏有效多行JSONRPC error。HTTP200但outcome=None不能用“is not False”记成工具成功。结果未知/无响应用明确失败或固定unknown事件；notifications/initialized允许空ACK单独判定。完整SSE进度通知不等于工具结果。读取中断、EOF无结果、isError、HTTP200error、有多行data的错误均覆盖。
5. 固定字段严格作用域保留。首次未云端登记source的日志会卡整批，Go工程师负责上传前移除未登记source/tool元数据，不允许服务端放松跨设备source校验。
请真实跑针对测试，完成后短checkpoint/CODE_READY。不要扩展到主平台聊天/LLM/SQL正文日志；不要复读旧长历史。

6. 检查Ops实际JS时间格式化：不能把Intl.DateTimeFormat的dateStyle/timeStyle与timeZoneName/年月日显式字段混用（会TypeError）。本地页发现此bug，Go端修。若Ops同模式也修；UI必须真实JS运行而非只有语法检查。
