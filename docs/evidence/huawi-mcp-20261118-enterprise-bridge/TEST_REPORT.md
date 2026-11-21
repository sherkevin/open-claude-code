# 华微 MCP 企业统一入口测试报告

## 1. 测试对象

- 版本：`1.3.0`
- 源码提交：`3d0d06cd87268ce61fc2b7c33b18a679a2d12d66`
- 代码基线：`476ff14f9a4663512925e5b14ba29a5729effdce`
- 测试日期：2026-11-18
- 候选包目录：`/media/data3/dengkw/releases/huawi-connector-1.3.0-3d0d06c`
- Linux 包 SHA-256：`f19b31b959f8cfd38a3234bc0a519c2a8acf1877397f24b80657b6a6480596cd`
- Windows 包 SHA-256：`705517ecde9d5ac72bbb30b24c523b607e108203681d79ba16ff69094ea8f5c2`

## 2. 自动化检查

以下检查已在固定源码提交 `3d0d06c` 上执行。此前已经决定的实现和测试没有因为收尾工作重复修改。

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `go test -count=1 ./...` | 通过 | 连接器全部 Go 包通过 |
| `go vet ./...` | 通过 | 无输出 |
| Kaiwu/import/resource Python 测试 | 通过 | `45 passed`，仅有一个 Starlette/httpx 第三方弃用警告 |
| `gofmt -l` | 通过 | 无未格式化 Go 文件 |
| `bash -n` | 通过 | Shell 脚本语法有效 |
| `node --check` | 通过 | 浏览器验收脚本语法有效 |
| Python `py_compile` | 通过 | 验收脚本可编译 |
| Nginx 模板 `nginx -t` | 通过 | 为规避非 root 监听 443 权限，临时端口改为 40443；不代表生产 443 已验证 |
| `git diff --check` | 通过 | 无空白错误 |

## 3. 端到端测试

### 3.1 本机 TCP 合成链路

证据：`e2e-synthetic-latest.json`

- 运行目录：`.runtime/huawi-enterprise-e2e-20261118T122456Z`
- 时间：2026-11-18T12:24:56Z 至 12:25:18Z
- 耗时：21.939 秒
- 源码：`3d0d06c`，`source_dirty=false`
- 结果：15 项全部通过

关键结果：

- 官方 `frps`/`frpc` 0.61.1、云端 visitor、连接器、Edge、控制面和固定本地 MCP 全部启动；
- 导入解析和工具发现只返回 `lookup_order`；
- 完整 MCP 生命周期返回 `SYNTHETIC-ORDER-42`；
- 错误 K 返回 401，跨来源返回 403，未批准工具返回 403；
- 错误 relay token 使 frpc 退出码为 1，错误 STCP 密钥和错误代理名无法连接；
- SSE 首事件在 0.149 秒到达，内容为 `text/event-stream`；
- 强杀专属 frps 后不可用 0.101 秒，2.427 秒恢复；
- 正常停止中继后 0.183 秒 Edge 返回 503，中继重启后 initialize 返回 200；
- 撤销后 0.502 秒停止专属实例，Edge 返回 401，frps/visitor 端口关闭，visitor 路由释放；
- 日志、控制数据库和 Windows 风格 `frpc.toml` 秘密扫描无命中。

### 3.2 本机 HTTPS/WSS 回环链路

证据：`e2e-tls-wss-latest.json`

- 运行目录：`.runtime/huawi-enterprise-e2e-20261118T122524Z`
- 时间：2026-11-18T12:25:24Z 至 12:25:39Z
- 耗时：15.501 秒
- 源码：`3d0d06c`，`source_dirty=false`
- 结果：8 项全部通过

测试使用临时 CA、SAN `DNS:localhost,IP:127.0.0.1`、Nginx 1.18、官方 FRP 0.61.1、WSS、Edge 和合成 MCP。验证了：

- HTTPS 导入解析保留集成凭据，工具列表只有 `lookup_order`；
- HTTPS 完整 MCP 生命周期返回 `SYNTHETIC-ORDER-42`；
- 错误 CA 被拒绝；
- 错误 K 返回 401，未知来源返回 403，未批准工具返回 403；
- 合成 MCP 实际收到一次工具调用且签名有效；
- Nginx 访问日志记录两个 `/~!frp` WebSocket `101`。

边界：这是本机回环 HTTPS/WSS，不是真实公网 DNS、正式证书、腾讯云部署或客户网络。

### 3.3 实际开物网页对话

证据：`e2e-chat-browser-latest.json`

- 运行目录：`.runtime/huawi-enterprise-e2e-20261118T122545Z`
- 时间：2026-11-18T12:25:45Z 至 12:26:31Z
- 耗时：45.969 秒
- 源码：`3d0d06c`，`source_dirty=false`
- 结果：5 项全部通过

测试流程：

1. 启动隔离控制面、真实官方 frps/frpc、visitor、Edge 和固定合成 MCP；
2. 使用仅所有者可读的随机管理员密码登录实际开物网页；
3. 在网页中导入本轮标准 MCP JSON；
4. 网页发现并绑定 `lookup_order`；
5. 选择网页模型 `dano-v2.1`，发起真实模型消息；
6. 模型实际调用工具，参数为 `{"sku":"SKU-1001"}`；
7. 工具调用成功，771 毫秒后完成，最终回答包含 `SYNTHETIC-ORDER-42`。

工具调用证据：开始事件 22、完成事件 26、调用成功、单个工具调用。截图保存在运行目录的 `browser-screenshots/`。

限定：模型通过学校云回环测试适配器 `127.0.0.1:3425`，`provider_test_mode=true`、`production_route=false`，不是腾讯云生产 TokenHub 路由；订单和 MCP 是固定合成数据。该结果证明实际产品网页、平台导入、模型工具循环和 MCP 调用链，不证明客户业务数据。

## 4. 候选包构建与校验

构建参数：

- `VERSION=1.3.0`
- `COMMIT=3d0d06c`
- `SOURCE_DATE_EPOCH=1795004692`
- `BUILD_TIME=2026-11-18T12:24:52Z`
- Go `1.27.4`、CycloneDX GoMod `v1.12.0`、govulncheck `v1.9.0`、官方 FRP `0.61.1`

构建方法：对同一固定提交执行两次独立的 `git archive`，分别复制相同固定哈希的官方 frpc 二进制后构建。两次的 Linux 包、Windows 包和外层 `SHA256SUMS` 逐字节一致。

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| 包内 Linux `sha256sum -c` | 通过 | 所有文件通过 |
| 包内 Windows `sha256sum -c` | 通过 | 所有文件通过 |
| 外层 `SHA256SUMS` | 通过 | 两个包均通过 |
| Windows ZIP `unzip -t` | 通过 | 无压缩数据错误 |
| Linux TAR 列表读取 | 通过 | 无读取错误 |
| Linux 连接器 | 通过 | 静态链接 ELF x86-64 |
| Windows 连接器 | 通过 | PE32+ x86-64 |
| Linux/Windows frpc | 通过 | 分别为 ELF x86-64、PE32+ x86-64 |
| 包内版本信息 | 通过 | `1.3.0`，提交 `3d0d06c`，固定构建时间 |
| SBOM | 通过 | CycloneDX 1.5，提交和固定时间正确 |
| `govulncheck` | 通过 | `No vulnerabilities found.` |

发布目录中的 `PACKAGE_VALIDATION.txt`、`REPRODUCIBILITY.txt`、`PACKAGE_*` 文件和两份构建日志保存了实际校验输出。

## 5. 未执行范围

- Windows Server 2019 实机安装、DPAPI、ACL、服务重启、整机重启、升级和卸载；按用户当前指示没有执行 Windows 远程匹配或授权流程。
- 模拟网络分区、长时间高并发和生产容量测试。
- 真实公网 DNS、正式证书、腾讯云 Edge、专属 frps 和反向代理部署。
- 腾讯云生产模型路由；实际网页测试使用的是学校云回环测试适配器。
- 华微真实业务 MCP、客户凭据和客户业务数据。

自动化测试、交叉编译和本机回环不能替代 Windows 实机、真实公网或客户业务验收。
