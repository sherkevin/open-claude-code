# Connector worker：只完成 Windows 连接器端代码

先读同目录 2026-11-19-huawi-diagnostics-parallel-contract.md。它是唯一跨端协议和分工依据。历史run87已停，你独占connector/；不要读旧长会话或从头规划。
工作目录必须显式设为 /media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118。先用git status确认，再按小范围读取相关函数，单次读不超过约200行，避免大段截断/反复compact。
任务目标是用户要的客户口吻+本地双日志+安全实时上报。接续现有diagnostics/catalog.go、diagnostics.go和cloud.Client，不重写FRP。
先运行并修复 diagnostics/supervisor_review_test.go（总控真实复现过失败）：自动重试无新事件、危险字段、换owner隔离、写盘失败可见、Record不因持久化锁阻塞。旧worker中断前已有修复，请据实际测试判断，不重新造一遍。
随后接入agent状态/配置操作、authproxy MCP成功失败与trace、FRP生命周期；heartbeat不得上传原始LastError。所有操作只记录固定白名单metadata。manager与FRP生命周期分离；2秒上传、3秒timeout、有界队列/断网退避/重启补传、不影响MCP。
本地API和UI：连接器日志、开物调用日志、级别筛选、自动刷新、下载脱敏日志、同步时间/待发/丢弃/存储错误状态、诊断暂停/恢复；本地日志UI通过本机后端查询平台events，浏览器不见device/admin凭据。
客户文案统一“您/您的/当前服务器”，改掉“客户提供的/客户的MCP”；JSON提示：
“粘贴您已有的 MCP 配置，支持 mcpServers 格式或单个 HTTP MCP 对象。连接所需的访问凭据会加密保存在当前服务器，不会写入导出的配置或诊断日志。”
诊断提示：“为便于开物技术支持排查连接问题，连接状态、操作结果和错误代码会自动上报至开物服务，不包含访问密钥、查询参数或业务数据。”
普通页不要展示K等实现术语；高级导入整体替换的真实语义必须保留。默认上报开，可暂停而不影响MCP。使用textContent防注入。
必要测试：现有全部Go测试和vet；重点privacy canary/queue/resume/trace/isError/SSE协议不变、本地方法/Origin保护。不要为文字替换写镜像测试。
Go工具、缓存均data3。每完成一个小阶段把已完成/下一函数/待修测试写 docs/evidence/huawi-diagnostics-20261119/CONNECTOR_CHECKPOINT.md（短，最多60行，真实时间）。
完成写 CONNECTOR_CODE_READY.json：changed_files, tests结果, 已知限制, required_server_contract, version。不要宣称Windows已测。不要commit/build/publish/部署/碰Shervin。
可以参考主计划 docs/plans/2026-11-19-huawi-customer-diagnostics-1.3.7.md 的相应小节，但不得反复读背景和历史。
现在直接修复/运行本package测试并继续实现，不只汇报“将要开始”。
