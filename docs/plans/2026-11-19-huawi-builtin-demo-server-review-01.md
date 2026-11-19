总控审查更正：继续当前1.3.8自检任务的server范围，不重开工程。先重新读取最新105行计划 docs/plans/2026-11-19-huawi-builtin-demo-mcp.md。请修复以下已经从WIP确认的问题，完成必要回归和报告，不部署：
1. 示例source_id统一为 "src_" + sha256(("kaiwu_builtin_demo_v1:" + connector_id).encode("utf-8")).hexdigest()[:32]；服务端重算并强制匹配，沿用src_32hex格式，不再使用demo-16hex。校验tenant/connector归属，以及允许工具只能为3个固定kaiwu_demo_*；可编辑display_name不是可信身份。
2. 临时K必须在Edge实际限制到kaiwu_demo_ping；当前只限制source会让K调用其他工具，不能仅靠当前HTTPX请求恰好只发ping。可复用服务器生成的可信临时集成类型标记，不为此无谓重构全套授权。初始化/发现按协议可用，其他业务tools/call拒绝；加真实Edge鉴权路径回归，禁止只mock掉授权函数。
3. self-test应单独强制受信Edge base使用公网HTTPS，不能继承旧_edge_public_base的http localhost/testserver例外而显示公网passed。单元测试用受控HTTPS transport/mock；实际HTTP本机测试只标本机模拟，不能冒充公网。
4. finally仅清理本次临时集成/映射；DELETE可以作为失效收尾，但用现有固定字段诊断记录trace和清理结果即可，不必新造审计系统，不动任何现有客户K。
5. 工程师报告须明确Windows未安装、云端本轮未部署；之前1.3.7 Windows验收也未完成。
不要停止或干扰connector工程师；他已通过UI Steer实时收到同一算法。读完更正后完成本轮代码与必要测试，提交仅server/与本轮SERVER_REPORT.md，不git add .。