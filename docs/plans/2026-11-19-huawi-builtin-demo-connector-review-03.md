# 1.3.8 连接器最终独立审查修正（第三轮）

总控已确认你的 ordinal 3 完成。此为新一轮明确的小范围修正，不是旧任务续跑。仍用当前 DeepSeek-V4.1-Flash；只改 connector/ 和本功能报告；不动 server、不 commit、不打包、不部署、不操作 Windows。

## 三个实际阻断项

### 1. 示例自检仍会停止正在工作的业务隧道

agent.go connect(ctx,demoOnly) 约1196–1229，即使 demoOnly 也先 stop previousSupervisor 再 s.Start。已有代理保留只能保护切换前失败，无法保护这个阶段。业务正常时点击示例自检不得重启全部 FRP，更不能新进程失败后留下断路。

实现最小方案：
- 在写当前 frpc.toml、替换 supervisor/heartbeat 之前处理 demoOnly + 已有运行 supervisor。
- 检查现有 FRP admin /api/status 的精确 demo sourceID 路由及当前 demo authproxy endpoint。已有对应路由则直接复用，仅完成 demo Diagnose 与公网 nonce 检查；客户其他 route 的 error 不应阻断 demo。
- 已有运行的业务隧道但缺少或无法精确确认 demo route，明确返回“测试通道尚未就绪，请在方便时重新连接”，保持旧业务 supervisor、配置、admin、heartbeat、所有代理与端口不变。本轮不实现 reload 或自动重启。
- 当前 queryFrpcTunnel 只看总数不够，新增精确 sourceID 状态判定。不要因为业务坏来源而把示例测试标失败。
- 固定 FRP v0.61.1 的 reload 是现有 daemon 重读配置，进程初始化时缓存环境变量，CLI 新环境不能给旧 daemon 添加 secret。本轮不引入热更新，不把密钥明文写入 TOML。正常首装/升级默认启用 demo；日常 off/on 保留原 demo authproxy listener/STCP route，只更新 upstream。关闭 demo 后重启服务再开启的边界需提示方便时重连。
- 若确无已运行隧道，原新建连接流程可用。

### 2. 示例 off/on 后 authproxy 仍指旧动态端口

demomcp.Server.Start 每次 Listen 127.0.0.1:0 得新端口；startAuthProxiesForDemo 复用旧 authproxy，旧 UpstreamURL 没更新。这样本机 probe 通过但公网仍打旧端口。
- 让内置示例 authproxy 安全更新 upstream，保留其 listener 和 FRP localPort；只改变示例代理，不重建客户代理。
- 客户关闭示例后不能继续通过旧代理拿到示例数据；重新启用后无需重启业务隧道即可正常查询。

### 3. Close 与在途 AutoConnect 的竞争可能重新拉起进程

StartRuntime 启动未登记 AutoConnect goroutine，AutoConnect 使用 background 90s；CloseContext 只停止当时 supervisor。阻塞 cloud 请求在 Close 后返回时仍可能启动新代理、frpc、heartbeat。
- 建立 runtime cancel/closed 语义，关闭先标记 closed 并取消在途 connect/selftest；在创建/提交 listener、supervisor 前检查关闭状态。
- 不要持 Agent.mu 等待 connectMu 造成新死锁。
- 已有 Supervisor.Start 使用 WithoutCancel，要从生命周期管理上处理，不能只取消一个无效上下文。

## 只补必要验证

- 运行中的客户隧道+精确demo路由：自检不替换/stop supervisor、不变业务端口；demo缺路由或状态错误时给出明确重连提示，旧业务配置、路径与状态保持。
- demo off/on：实际 MCP 请求经同一个 demo authproxy listener 返回新实例/正确nonce；客户代理与隧道保持。
- controlled httptest 阻塞 connect；调用 Close 后释放响应，确认无新 listener、frpc 或 heartbeat 留下。
- 对实际修改包定向测试及必要 race；无需在无改动的包上重复全量大循环。更新 CONNECTOR_REPORT/ CHECKPOINT，明确实际结果，再结束并冻结。

所有 cache/temp/artifact 在 /media/data3/dengkw；复用已下载 Go1.27.4、SDKv1.8.0 与原版FRP。Windows1.3.6未升级、1.3.7/1.3.8未经过Windows安装验收的事实保持。
