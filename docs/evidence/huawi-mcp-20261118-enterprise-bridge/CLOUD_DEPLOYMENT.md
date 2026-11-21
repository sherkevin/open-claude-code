# 云端部署材料与操作顺序

## 1. 当前状态

本次开发在学校云完成，已经验证：

- 本机回环 TCP 合成链路；
- 本机回环 HTTPS/WSS，包括临时 CA、证书校验、Nginx WebSocket 升级和官方 FRP 0.61.1；
- 实际开物产品网页导入、工具发现、模型消息和 `lookup_order` 工具调用，返回固定合成结果。

本次没有修改腾讯云或其他现网服务，没有新增 DNS、正式证书或公网域名，也没有重启共享服务。因此本文仍是部署准备和操作说明，不是“已经上线”报告。

HTTPS/WSS 回环证据为 `e2e-tls-wss-latest.json`，运行目录 `.runtime/huawi-enterprise-e2e-20261118T122524Z`。该证据明确限定 `scope=loopback HTTPS/WSS only`，不能代替生产证书和公网 DNS。

## 2. 需要准备的组件

1. 现有 FastAPI 控制面和 Edge。控制面代码在 `server/app/kaiwu_control.py`，Edge 在 `server/app/kaiwu_edge.py`。
2. 官方 `frps` 0.61.1 和云端 visitor 使用的官方 `frpc` 0.61.1。固定哈希见 `deploy/kaiwu-enterprise-bridge/OFFICIAL_FRP_0.61.1_SHA256.txt`。
3. 每个连接器一个专属 `frps` 实例，由 `systemd` 模板 `kaiwu-bridge@.service` 托管。
4. visitor 监听地址和端口范围。当前模板使用 `127.0.0.1:31000-31999`，不能把 visitor 端口暴露到公网。
5. 一个受控管理员根密钥文件，用于派生 relay token R 和 STCP token T。该根密钥不能写入 Git、普通环境示例、日志或普通诊断包。
6. 公网 WSS 反向代理和有效证书。模板 `deploy/kaiwu-enterprise-bridge/nginx-wss.example.conf` 中的域名、证书路径和上游端口必须替换为正式值。
7. 公网 Edge HTTPS 路径，例如 `/projects/kaiwu/mcp/`，必须与 `AGENT_KAIWU_EDGE_PUBLIC_BASE`、`AGENT_KAIWU_EDGE_MOUNT_PATH` 完全一致。

## 3. 推荐部署顺序

1. 核对代码提交 `3d0d06c`、候选版本 `1.3.0`、配置模板和 frp 二进制哈希。
2. 创建专用服务账号、`/etc/kaiwu-bridge/environment` 和 `/var/lib/kaiwu-bridge`，权限只给服务账号和必要管理员。
3. 安装官方 frps/frpc 到固定路径 `/opt/kaiwu/bin/`，禁止自动跟随最新版升级。
4. 部署 `app` 包和 Python 虚拟环境到 `/opt/kaiwu/app`。模板通过 `WorkingDirectory=/opt/kaiwu` 和 `PYTHONPATH=/opt/kaiwu` 保证 `/opt/kaiwu/venv/bin/python -m app.kaiwu_bridge --help` 可运行。
5. 在管理员接口创建设备接入码并完成 Windows 绑定；来源登记后，控制面会分配 visitor 端口。
6. 为连接器启动 `kaiwu-bridge@<connector_id>.service`。服务从数据库重建路由，自行生成仅服务账号可读的 TOML，不把 R/T 写入普通配置。
7. 配置公网 WSS 反向代理到 frps 的私有 `bindPort`，确认 WebSocket upgrade、长连接和反向代理缓冲设置。
8. 配置 Edge 的公开 HTTPS 路径，正式环境必须使用 HTTPS。
9. 管理员调用 `POST /api/kaiwu/v1/integrations` 创建企业集成，保存一次性 K 和 `mcpServers` JSON；数据库只能看到 K 的校验散列和短前缀。
10. 用现有开物导入器导入 JSON，完成 initialize、tools/list、允许工具的 tools/call，然后检查错误 K、跨来源、撤销和断线行为。

## 4. 官方 FRP 与 WSS 要点

- `frps`、Windows `frpc` 和云端 visitor 必须固定为官方 0.61.1，并核验 SHA-256。
- 官方 frpc/visitor 的 `transport.protocol` 必须写 `wss`。写成 `websocket` 后再设置 `transport.tls.enable=true` 不会建立 WSS。
- 公网证书正常时应使用公共 CA，两个可信 CA 路径留空。
- 如果使用私有 CA，云端 visitor 使用 Linux 本机路径 `AGENT_KAIWU_CONTROL_RELAY_TLS_TRUSTED_CA_FILE`。
- Windows frpc 使用客户 Windows 本机路径 `AGENT_KAIWU_CONTROL_RELAY_TLS_CLIENT_TRUSTED_CA_FILE`。
- 两个 CA 路径用途不同，不能把 Linux 路径下发给 Windows。
- frps 的 `bindPort` 应保持私有，由 Nginx 或其他受控反向代理转发。不要直接把 frps 管理接口或 visitor 端口暴露到公网。

本机回环测试使用临时 CA、SAN `DNS:localhost,IP:127.0.0.1`、Nginx 1.18 和官方 FRP 0.61.1。错误 CA 被拒绝，Nginx 访问日志记录到两个 `/~!frp` WebSocket `101`。这证明配置路径在本机成立，不证明正式证书链、公网防火墙和 DNS 已正确。

## 5. 控制面与 Edge 配置要点

- `AGENT_KAIWU_CONTROL_ENABLED=true` 才启用控制面；关闭时必须失败关闭，不能降级成匿名访问。
- `AGENT_KAIWU_CONTROL_ADMIN_TOKEN_FILE` 必须是独立管理员根密钥文件的路径，不能与客户业务密钥复用。
- `AGENT_KAIWU_CONTROL_RELAY_ADDRESS`/`PORT`/`PROTOCOL` 是 Windows 连接器看到的公网 relay 入口。
- `AGENT_KAIWU_CONTROL_RELAY_BIND_ADDRESS`/`BIND_PORT` 是 frps 的私有监听入口，通常由 Nginx 或其他受控反向代理转发，不应直接暴露给公网。
- `AGENT_KAIWU_EDGE_VISITOR_HOST` 必须保持回环或严格受控的内网地址；Edge 会再次校验数据库路由的 host，客户端不能指定任意目标。
- `AGENT_KAIWU_EDGE_SESSION_TTL_SECONDS` 和 `AGENT_KAIWU_EDGE_REQUEST_TIMEOUT_SECONDS` 要按实际网络调整，不能通过关闭超时来“解决”断线。

Edge 的 MCP 会话表保存在进程内内存。部署时必须保持一个 Edge worker；如果要扩容，先把会话状态迁移到受控共享存储。

## 6. 我方部署前必须确定的参数

以下值尚未在现网创建或填写，不能猜测：

- 公网 relay 主机名，例如 `relay.<客户或我方域名>`；
- 公网 Edge 主机名和完整 MCP 路径；
- 正式证书和私钥路径；
- frps 私有 bind 地址和端口；
- visitor 端口范围；
- 控制面、Edge 和 relay 的部署机器；
- 管理员根密钥文件位置；
- 企业集成名称、来源和允许工具列表；
- 正式开物导入时使用的标准 JSON。

## 7. 上线前检查

- relay 地址、Edge 地址、控制面地址和 Windows 本机监听地址是否分别正确，没有混用；
- DNS 和证书是否覆盖实际公网主机名；
- frps、visitor、Windows frpc 是否都是固定的 0.61.1 且哈希一致；
- 错误 relay token、错误 STCP 密钥和错误代理名是否都失败关闭；
- 错误 CA 是否被拒绝，公共 CA 和私有 CA 两个路径是否分别正确；
- 停止专属实例后，Edge 是否在目标时间内返回 503 或等价失败；
- 撤销设备后，K 是否失效、会话是否停止、frps/visitor 是否关闭、systemd 是否不会自动复活实例；
- 日志、诊断包、SQLite 和 Windows 配置中是否找不到 K、客户 Bearer、HMAC 密钥、R、T；
- 导入 JSON 是否只包含我方 Edge 地址和 K，不包含客户内部 B/S/channel/userid；
- 实际开物网页是否只发现批准工具，并且对话确实触发工具调用。

## 8. 回滚

回滚、停机和恢复步骤见同目录的 `ROLLBACK.md`。不要在没有备份和值班确认的情况下直接删除生产数据库、证书或现网域名。
