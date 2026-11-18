# 华微 MCP 企业统一入口测试报告

## 测试对象

- 版本：`1.3.0`
- 发布源提交：`8c889fec818be8ec4a440a86d742134f9f77927a`
- 测试日期：`2026-11-18`
- 发布目录：`/media/data3/dengkw/releases/huawi-connector-1.3.0-8c889fe`
- 发布包哈希以同目录 `SHA256SUMS` 和 `RELEASE_REPORT.md` 为准。

## 自动化与构建结果

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| `go test -count=1 ./...` | 通过 | 连接器全部 Go 包通过 |
| `go vet ./...` | 通过 | 无输出 |
| Kaiwu/import/resource Python 测试 | 通过 | `45 passed`，一个测试依赖的 Starlette/httpx 弃用警告 |
| `bash -n connector/scripts/build.sh` | 通过 | Shell 语法有效 |
| `gofmt -l` | 通过 | 无未格式化 Go 文件 |
| `git diff --check` | 通过 | 无空白错误 |
| 两次独立发布构建 | 通过 | Linux 包、Windows 包和外层校验文件逐字节一致 |
| 包内及外层 SHA-256 校验 | 通过 | Linux、Windows 和发布目录全部通过 |
| Windows ZIP 完整性 | 通过 | `unzip -t` 无错误 |
| SBOM JSON 解析 | 通过 | CycloneDX 1.5，版本和时间戳正确 |
| `govulncheck` | 通过 | `No vulnerabilities found.` |

## 本机合成端到端验收

最新证据文件：

`docs/evidence/huawi-mcp-20261118-enterprise-bridge/e2e-synthetic-latest.json`

最新运行目录：

`.runtime/huawi-enterprise-e2e-20261118T113707Z`

本轮总耗时 `25.222` 秒。运行使用真实官方 `frps`/`frpc` 0.61.1、云端 visitor、连接器、Edge、控制面和固定本地 MCP，但全部位于本机回环地址。

| 检查项 | 结果 | 关键数据 |
| --- | --- | --- |
| 连接器配置、导入、HMAC profile、frpc 启动 | 通过 | 配置和启动成功 |
| 专属 frps、visitor 监听 | 通过 | 端口打开 |
| 企业集成和一次性标准 MCP JSON | 通过 | K 和导入文档签发成功 |
| 连接器隧道状态 | 通过 | `1 个来源代理已运行` |
| 开物导入解析和工具发现 | 通过 | 仅发现 `lookup_order` |
| MCP 完整生命周期 | 通过 | `initialize`、初始化通知、`tools/list`、`tools/call` 返回 `SYNTHETIC-ORDER-42` |
| 错误 K、跨来源和未批准工具 | 通过 | 分别为 401、403、403 |
| 错误 relay token、STCP 密钥和代理名 | 通过 | frpc 退出码 1；错误 STCP 和代理名无法连接 |
| SSE 首事件 | 通过 | `0.186` 秒，内容为 `text/event-stream` 且首事件含数据 |
| 强杀专属 frps 后恢复 | 通过 | 不可用 `0.101` 秒，约 `2.691` 秒恢复，Edge 返回 200 |
| 正常停止中继 | 通过 | 约 `0.184` 秒后 Edge 返回 503 |
| 中继重启恢复 | 通过 | Edge initialize 返回 200 |
| 秘密扫描 | 通过 | 日志、控制数据库和 Windows 风格 `frpc.toml` 未发现 K、客户 Bearer、HMAC 密钥、R 或 T |
| 撤销连接器 | 通过 | 约 `0.502` 秒停止专属实例，Edge 返回 401，frps/visitor 端口关闭，visitor 路由释放 |

## 复现命令

Go 测试和静态检查：

```bash
cd /media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118/connector
GOCACHE=/media/data3/dengkw/.cache/go-build/huawi-release-20261118 \
GOTMPDIR=/media/data3/dengkw/.tmp/huawi-release-20261118 \
TMPDIR=/media/data3/dengkw/.tmp/huawi-release-20261118 \
/media/data3/dengkw/toolchains/go1.27.4/bin/go test -count=1 ./...

GOCACHE=/media/data3/dengkw/.cache/go-build/huawi-release-20261118 \
GOTMPDIR=/media/data3/dengkw/.tmp/huawi-release-20261118 \
TMPDIR=/media/data3/dengkw/.tmp/huawi-release-20261118 \
/media/data3/dengkw/toolchains/go1.27.4/bin/go vet ./...
```

Python 测试：

```bash
cd /media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118
TMPDIR=/media/data3/dengkw/.tmp/huawi-release-20261118 \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.cache/pycache/huawi-release-20261118 \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest -q \
  server/tests/test_kaiwu_bridge.py \
  server/tests/test_kaiwu_control.py \
  server/tests/test_kaiwu_enterprise.py \
  server/tests/test_mcp_import.py \
  server/tests/test_resources_04.py
```

本机合成端到端：

```bash
cd /media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118
PATH=/media/data3/dengkw/toolchains/go1.27.4/bin:/media/data3/dengkw/toolchains/bin:$PATH \
GOCACHE=/media/data3/dengkw/.cache/go-build/huawi-release-20261118 \
GOTMPDIR=/media/data3/dengkw/.tmp/huawi-release-20261118 \
TMPDIR=/media/data3/dengkw/.tmp/huawi-release-20261118 \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.cache/pycache/huawi-release-20261118 \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python scripts/kaiwu_enterprise_e2e.py
```

## 未执行范围

- Windows Server 2019 实机安装、DPAPI、ACL、服务重启、整机重启、升级和卸载。
- 模拟网络分区、长时间高并发和容量测试。
- 真实公网 DNS、证书、腾讯云 Edge、专属 frps 和反向代理部署。
- 实际开物网页登录、平台对话和真实模型调用。
- 华微真实业务 MCP、客户凭据和客户业务数据。

自动化测试和交叉编译不能替代上述实机、公网和客户验收。
