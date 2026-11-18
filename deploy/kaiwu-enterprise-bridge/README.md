# 华微 MCP 企业桥接部署材料

本目录只包含部署模板，不包含真实中继令牌、STCP 密钥、企业集成凭据 K、
客户 Bearer、华微 HMAC 密钥或域名证书。

## 组件

- `frps`：每个 Windows 连接器一个专属实例，只监听受控网络。
- `frpc visitor`：云端进程，把 Edge 的请求送入对应 STCP 来源。
- `kaiwu_bridge.py`：读取控制面来源和路由，生成私有 TOML，守护 frps 和 visitor。
- Nginx：为 frps WebSocket 提供公网 WSS 443，并转发到 frps 的私有 `bindPort`；同一示例同时展示开物 Edge 的 HTTPS 固定路径反代，二者应按实际域名分 server 配置，不能与业务 HTTP 监听直接抢占同一端口。
- FastAPI Edge：固定路径 `/projects/kaiwu/mcp/sources/{source_id}/mcp`，验证企业集成 K。

## 启动顺序

1. 在控制面为连接器签发接入码，客户 Windows 完成绑定。
2. Windows 导入来源并登记允许工具后，控制面分配 loopback visitor 端口。
3. 管理员创建企业集成，取得一次性 K 和标准开物 MCP JSON。
4. 为专属实例启动 `kaiwu-bridge@<connector_id>.service`。
5. 配置 Nginx WSS 证书、`/~!frp` 反向代理，以及 Edge 的 `/projects/kaiwu/mcp/` HTTPS 反向代理。官方 frpc/visitor 的 `transport.protocol` 必须写 `wss`，不能写 `websocket` 后仅打开 `transport.tls.enable`。
6. 从公网验证错误 K、跨来源、撤销和真实工具调用。

Edge 会话保存在当前进程内，部署时必须保持一个 Edge worker。需要横向扩容时，
先把会话状态迁到受控共享存储，不能用多 worker 冒充已完成。

`kaiwu-bridge` 服务账号以 SQLite `mode=ro` 和 `query_only` 只读打开控制面
数据库，并只写 `/var/lib/kaiwu-bridge/%i`。如果 `AGENT_DATA_DIR` 不是
`/srv/kaiwu/data`，只需同步修改环境文件；不要授予服务账号数据库写权限，
也不要把数据库目录改成全局可写。

模板把服务工作目录和 Python 模块搜索路径都固定为 `/opt/kaiwu`。部署后先以
服务账号运行 `/opt/kaiwu/venv/bin/python -m app.kaiwu_bridge --help`，确认
`app` 包可导入，再启用 systemd 实例。

## 生成配置

控制面管理员接口：

```text
GET /api/kaiwu/v1/connectors/{connector_id}/data-plane
```

响应包含 `frps_toml` 和 `visitor_toml`，带 `Cache-Control: no-store`。这两个
响应包含派生运输秘密，只能在受控运维终端处理，不能写入 Git、聊天或普通日志。
守护进程会自行从数据库重建相同内容，正常部署无需人工保存响应。

## 撤销

管理员撤销设备后，控制面在同一事务中撤销该连接器的全部企业集成。守护进程
轮询数据库发现 `revoked=1` 后停止 visitor 和 frps、删除运行配置并以成功状态
退出。控制面同时释放该连接器的 visitor 路由端口，避免反复创建和撤销后耗尽
有界端口范围。systemd 使用 `Restart=on-failure`，不会自动恢复已撤销实例。

## 版本

frp 固定为官方 `0.61.1`。升级必须同时更换 frps、云端 frpc 和 Windows frpc，
核验官方发布归档和二进制 SHA-256，并重新执行错误 token、错误 STCP 密钥和
停止实例验收。

## 私有 CA 路径

生产优先使用受信任的公共 CA，此时两个可信 CA 配置都留空。若确实使用私有 CA，
云端 visitor 和服务端证书校验需要 Linux 本机路径
`AGENT_KAIWU_CONTROL_RELAY_TLS_TRUSTED_CA_FILE`；Windows frpc 需要它自己的本机
路径 `AGENT_KAIWU_CONTROL_RELAY_TLS_CLIENT_TRUSTED_CA_FILE`。两者不能共用一条
Linux 路径，否则控制面会把不存在的服务器路径下发给 Windows。
