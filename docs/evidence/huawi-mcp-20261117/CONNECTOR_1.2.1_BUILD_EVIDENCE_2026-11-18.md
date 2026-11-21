# 开物内网连接器 1.2.1 构建与包级验证记录

- 执行日期：2026-11-18
- 执行环境：学校云 Linux 开发机
- 候选代码提交：`f942bf7dd0c20e17984ba46cb194650aad6bb518`
- 功能分支：`feature/intranet-mcp-connector-20261116`
- 版本：`1.2.1`
- 构建时间：`2026-11-18T01:50:53Z`
- 构建工作树：`/media/data3/dengkw/worktrees/kaiwu-connector-1.2.1-build-f942bf7`

## 1. 交付包

| 平台 | 文件 | SHA-256 |
| --- | --- | --- |
| Windows amd64 | `artifacts/kaiwu-connector-1.2.1-windows-amd64.zip` | `f8ef9a21ae93c7735a988b9c1adefb3a4f9c0c3749c6ea79810688b2e6302e0d` |
| Linux amd64 | `artifacts/kaiwu-connector-1.2.1-linux-amd64.tar.gz` | `dabbe76e49668c61e11d340ece90e3c0c20832359769213cb7b932ae5ae13bf1` |

两个二进制均记录：

- `vcs.revision=f942bf7dd0c20e17984ba46cb194650aad6bb518`
- `vcs.modified=false`
- `CGO_ENABLED=0`

Windows 主程序是 `PE32+ x86-64`，Linux 主程序是静态链接的 `ELF 64-bit x86-64`。

## 2. 本版修复

本版修复了 `1.2.0` Windows 现场验收发现的 D01：

1. 同一上游地址和传输类型重复导入时，继续使用原来的不透明来源编号。
2. 新导入没有显式工具允许列表时，保留客户本机管理员此前批准的工具允许列表。
3. 来源最终没有任何批准工具时，删除不可用的动态认证档案，避免页面显示“认证已配置”但工具列表实际为空。

对应回归测试：

- `TestReimportPreservesApprovedToolAllowlist`
- `TestCorrectAuthorizationSurvivesReimportAndRestart`

第二个测试覆盖“正确凭据 -> 错误凭据 -> 恢复正确凭据 -> 服务重建”的持久化顺序，但 D02 的现场表现仍未在 Windows 人工浏览器中完成复测，因此不能据此宣称 D02 根因已经关闭。

## 3. 已执行的验证

| 检查 | 结果 |
| --- | --- |
| `go test ./...` | 通过 |
| `go test -race ./...` | 通过 |
| `go vet ./...` | 通过 |
| `git diff --check` | 通过 |
| 服务端 `pytest -q tests/test_kaiwu_control.py` | 6 项通过 |
| 内嵌页面脚本 `node --check` | 通过 |
| Windows ZIP 完整性 | 通过 |
| Windows 包内 `SHA256SUMS` | 27 个文件全部通过 |
| Linux TAR 包内 `SHA256SUMS` | 28 个文件全部通过 |
| CycloneDX SBOM | 1.5 JSON，应用组件版本为 `1.2.1` |
| `govulncheck` | `No vulnerabilities found.` |
| Linux 最终包启动 | 本机页面和 `/api/status` 可访问 |
| 失败关闭 | 未配置云端时返回 `UNPAIRED`、`cloud_configured=false`、`edge_configured=false` |

包内合规文件继续包含 frp `LICENSE` / `NOTICE`、frp 上游哈希、Go `LICENSE` / `PATENTS`、SBOM、SBOM 工具原始警告说明、漏洞扫描结果、构建日志和二进制构建信息。

## 4. 本记录能够证明什么

1. `1.2.1` 可由提交 `f942bf7` 在干净隔离工作树中重新构建。
2. D01 的自动化回归通过，且重复导入不会再静默清空已批准工具。
3. 当前源码通过单元测试、竞态测试、静态检查、控制面合成测试、依赖漏洞扫描和包内完整性检查。
4. Windows 交付物是 x86-64 PE，Linux 最终包可以启动并保持默认失败关闭。
5. 包内 Windows 验收任务书已明确客户目标服务器具备图形化桌面，要求管理员图形界面安装、服务管理、浏览器诊断和人工复测。

## 5. 本记录不能证明什么

1. 没有在 Windows 实机执行 `1.2.1` 的安装、重复导入、错误凭据恢复和重启验收。
2. 没有在客户目标 Windows Server 2019 Datacenter Build `17763.9020` 上执行整机重启、安全软件默认开启、升级和卸载。
3. 没有完成 Authenticode 签名、恶意软件扫描、秘密扫描或正式发布的独立双构建。
4. 没有完成云端控制面、relay、visitor、Edge、Token Exchange、平台 grant 和 Windows 内网 MCP 的真实端到端联调。
5. 没有使用真实客户数据、真实客户凭据或生产环境。

因此，`1.2.1` 是已修复 D01、通过自动化和包级检查的受控试装候选包，不是正式生产可交付版本，也不能宣称开物平台已经能够调用客户内网 MCP。
