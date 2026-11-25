# 2026-11-26｜E2.1 Codex App Server 真实 stdio 与工作区元数据隔离验证

基于已合入的本地 Codex 0.165.0 协议基线 99da5c4，在独立工作树 kaiwu-codex-stdio-pilot-20261126 编写 scripts/oss/codex_appserver_stdio_probe.py。实际使用同一固定本地二进制，SHA-256 c06a67fd8ac76508dc78bd99ce20c9f317b8d568beda59485f3f4134df9962d5，未核实对应官方发行签名与许可证清单，故仅为本地候选验收，不是正式供应链认证。

安全边界：先验证已有无特权 bubblewrap 网络命名空间只有 lo，IPv4 route 为零；实际进程再次在同一沙箱启动前验证网络状态。没有使用宿主 /home、凭据、配置或项目目录；仅挂载系统只读运行目录、固定二进制、data3 新建隔离工作/配置/缓存/临时目录；清空环境，默认 analytics 未启用，stdio 单通道。未开放 TCP 服务、没有真实用户输入、没有模型 turn、没有同意服务器权限请求。探针遇到任何需回复的服务器请求立即失败。没有使用客户数据、生产运行器或网络绕过。

实际启动本地 App Server 并执行生成 schema 对应的 RPC，4/4 通过，退出码 0（stdio.log、stdio.exit）：

| 检查 | 结果 |
|---|---|
| initialize 与 initialized 握手 | 通过 |
| 新建隔离工作区 thread/list 空 | 通过 |
| thread/start，read-only sandbox、never 自动审批、零 turn | 通过 |
| thread/read 精确 threadId，工作区正确且零 turn | 通过 |

证据 /media/data3/dengkw/acceptance/kaiwu-codex-stdio-pilot-20261126/；初始网络能力证据 /media/data3/dengkw/acceptance/kaiwu-codex-appserver-20261126/network-isolation.log、network-isolation.exit。真实进程 stdout 返回协议响应，未启动模型或业务工具。

下一步：先复核固定二进制来源与许可证，构造独立回环合成 provider（仍在无外网沙箱内）；实际 turn/start、thread 持久化并重启后 resume、interrupt、拒绝审批和宿主 thread/turn/owner 身份绑定。不能把元数据握手称为真实编码执行或产品上线。生产 A2.10 闸门仍保持。
