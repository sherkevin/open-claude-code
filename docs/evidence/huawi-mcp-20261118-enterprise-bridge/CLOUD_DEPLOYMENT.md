# 云端部署材料与操作顺序

## 当前状态

本次开发在学校云完成，只验证了回环地址上的真实官方 FRP、visitor、连接器核心和 Edge 合成链路。没有修改腾讯云或其他现网服务，没有新增 DNS 记录、证书或公网域名，也没有重启共享服务。因此本文是部署准备清单，不是“已经上线”报告。

部署细节以仓库中的 `deploy/kaiwu-enterprise-bridge/` 模板为准；模板中的域名、路径、端口和令牌值都是占位符，不能直接用于生产。

## 需要准备的组件

1. 现有 FastAPI 控制面和 Edge。控制面代码在 `server/app/kaiwu_control.py`，Edge 在 `server/app/kaiwu_edge.py`。
2. 官方 `frps` 0.61.1 和云端 visitor 使用的官方 `frpc` 0.61.1。固定哈希已经记录在 `deploy/kaiwu-enterprise-bridge/OFFICIAL_FRP_0.61.1_SHA256.txt` 和发布包中。
3. 每个连接器一个专属 `frps` 实例，由 `systemd` 模板 `kaiwu-bridge@.service` 托管。
4. 本机 visitor 监听地址和端口范围。当前模板使用 `127.0.0.1:31000-31999`，不能把 visitor 端口暴露到公网。
5. 一个受控的管理员根密钥文件，用于派生 R 和 T。该根密钥不能写入 Git、普通环境示例、日志或普通诊断包。
6. 公网 WSS 反向代理和有效证书。模板中的 `nginx-wss.example.conf` 只是样例，必须替换域名、证书路径和上游端口。

## 推荐部署顺序

1. 先在受控机器上核对代码提交、发布版本、配置模板和 frp 二进制哈希。
2. 创建专用服务账号、`/etc/kaiwu-bridge/environment` 和 `/var/lib/kaiwu-bridge`，权限只给服务账号和必要管理员。
3. 安装官方 frps/frpc，使用固定路径 `/opt/kaiwu/bin/`；不要使用来源不明或自动升级的二进制。
4. 部署 `app` 包和 Python 虚拟环境到 `/opt/kaiwu/app`，模板通过 `WorkingDirectory=/opt/kaiwu` 和 `PYTHONPATH=/opt/kaiwu` 保证 `/opt/kaiwu/venv/bin/python -m app.kaiwu_bridge --help` 可以运行。
5. 在管理员接口创建设备接入码并完成客户 Windows 绑定；来源登记完成后，控制面会分配 visitor 端口。
6. 为连接器启动 `kaiwu-bridge@<connector_id>.service`。服务从数据库读取路由，自行生成仅服务账号可读的 TOML，不把 R/T 写入普通配置。
7. 配置公网 WSS 反向代理到 frps 的私有 `bindPort`，确认 WebSocket upgrade、长连接和反向代理缓冲设置。
8. 配置 Edge 的 `AGENT_KAIWU_EDGE_PUBLIC_BASE` 与 `AGENT_KAIWU_EDGE_MOUNT_PATH`，二者路径必须完全一致；正式环境必须使用 HTTPS。
9. 管理员调用 `POST /api/kaiwu/v1/integrations` 创建企业集成，保存一次性 K 和 `mcpServers` JSON；数据库只能看到 K 的校验散列和短前缀。
10. 用现有开物导入器导入 JSON，完成 initialize、tools/list、允许工具的 tools/call，然后检查错误 K、跨来源、撤销和断线行为。

## 控制面与 Edge 配置要点

- `AGENT_KAIWU_CONTROL_ENABLED=true` 才启用控制面；关闭时必须失败关闭，不能降级成匿名访问。
- `AGENT_KAIWU_CONTROL_ADMIN_TOKEN_FILE` 必须是独立管理员根密钥文件的路径，不要和客户业务密钥复用。
- `AGENT_KAIWU_CONTROL_RELAY_ADDRESS`/`PORT`/`PROTOCOL` 是 Windows 连接器看到的公网 relay 入口。
- `AGENT_KAIWU_CONTROL_RELAY_BIND_ADDRESS`/`BIND_PORT` 是 frps 的私有监听入口；它通常由 Nginx 或其他受控反向代理转发，不应直接暴露给公网。
- `AGENT_KAIWU_EDGE_VISITOR_HOST` 必须保持回环或严格受控的内网地址；Edge 会再次校验数据库路由的 host，不能由客户端指定任意目标。
- `AGENT_KAIWU_EDGE_SESSION_TTL_SECONDS` 和 `AGENT_KAIWU_EDGE_REQUEST_TIMEOUT_SECONDS` 要按实际网络调整，不能通过关闭超时来“解决”断线。

## Edge 单进程约束

当前 Edge 的 MCP 会话表保存在进程内内存中。部署时必须保持一个 Edge worker，或者先把会话状态迁移到受控共享存储后再扩容。不能用多个 worker 共享同一个数据库，却让不同 worker 各自丢失 session 映射，造成看似随机失败。

## 上线前检查

- relay 地址、Edge 地址、控制面地址和 Windows 本机监听地址分别是哪一个，是否混用；
- DNS 和证书是否覆盖实际公网主机名；
- frps、visitor、Windows frpc 是否都是固定的 0.61.1 且哈希一致；
- 错误 relay token、错误 STCP 密钥、错误代理名是否都失败关闭；
- 停止专属实例后，Edge 是否在目标时间内返回 503 或等价失败，而不是继续返回旧结果；
- 撤销设备后，K 是否失效、会话是否停止、frps/visitor 是否关闭、systemd 是否不会自动复活实例；
- 日志、诊断包、SQLite 和 Windows 配置中是否找不到 K、客户 Bearer、HMAC 密钥、R、T；
- 导入 JSON 是否只包含我方 Edge 地址和 K，不包含客户内部 B/S/channel/userid。

## 回滚

回滚、停机和恢复步骤见同目录的 `ROLLBACK.md`。不要在没有备份和值班确认的情况下直接删除生产数据库、证书或现网域名。
