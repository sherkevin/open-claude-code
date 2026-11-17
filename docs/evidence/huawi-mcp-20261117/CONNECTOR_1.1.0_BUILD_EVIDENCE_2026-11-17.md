# 开物内网连接器 1.1.0 构建与包级验证记录

- 执行日期：2026-11-17
- 执行环境：学校云 Linux 开发机
- 候选代码提交：`b098e3a89ec600c4e58ad411dab2b208fe0fe1aa`
- 功能分支：`feature/intranet-mcp-connector-20261116`
- 版本：`1.1.0`
- 构建时间：`2026-11-17T11:26:34Z`
- 构建工作树：从上述提交新建的独立 detached worktree，不包含共享开发目录中的其他工程师未跟踪文件

## 1. 交付包

| 平台 | 文件 | SHA-256 |
| --- | --- | --- |
| Windows amd64 | `artifacts/kaiwu-connector-1.1.0-windows-amd64.zip` | `2BF5E93E5E27476A28CE92E1A7C9A22182E222194D1C73C547111415B8BF3964` |
| Linux amd64 | `artifacts/kaiwu-connector-1.1.0-linux-amd64.tar.gz` | `39D5057EA9A811009E2CC4856F081A3E1F5DB2AB4BB99A398831C6C4E0CB9176` |

两个包的二进制构建信息都记录：

- `vcs.revision=b098e3a89ec600c4e58ad411dab2b208fe0fe1aa`
- `vcs.modified=false`
- `CGO_ENABLED=0`

## 2. 已执行的验证

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| `go test ./...` | 通过 | 2026-11-17 学校云执行 |
| `go test -race ./...` | 通过 | 2026-11-17 学校云执行 |
| `go vet ./...` | 通过 | 2026-11-17 学校云执行 |
| 页面脚本 `node --check` | 通过 | 本机诊断页面语法检查 |
| Go 工具链 | `go1.27.4 linux/amd64` | 包内 `compliance/BUILD_LOG.txt` |
| `govulncheck` | `No vulnerabilities found.` | 包内 `compliance/GOVULNCHECK.txt` |
| SBOM | CycloneDX 1.5 JSON，2 个组件和 3 条依赖关系 | 包内 `compliance/SBOM.cdx.json` |
| Windows ZIP 内层哈希 | `sha256sum -c SHA256SUMS` 全部通过 | 包内 `SHA256SUMS` |
| Linux TAR 内层哈希 | `sha256sum -c SHA256SUMS` 全部通过 | 包内 `SHA256SUMS` |
| Linux 包级启动 | 解包后的最终二进制可启动本机页面；云端和 Edge 均为未配置 | `UNPAIRED / cloud_configured=false / edge_configured=false` |
| 页面失败关闭 | 未配置云端时显示“本机诊断试用包”，连接按钮初始禁用 | 最终 Linux 包页面检查 |

## 3. 合规文件

两个包均包含：

- frp 0.61.1 官方二进制、固定 SHA-256、Apache License 2.0 和 NOTICE；
- Go 1.27.4 官方 `LICENSE` 与 `PATENTS`；
- CycloneDX 1.5 JSON SBOM；
- SBOM 工具原始警告和解释文件；
- 漏洞扫描结果；
- Go 模块清单、二进制构建信息、构建日志和源码链接信息。

`cyclonedx-gomod` 无法自动识别 Go 标准库许可证，也无法在 detached worktree 中解析主模块 Git 版本。因此构建脚本明确把 `std` 组件标记为 `BSD-3-Clause`，把应用自身许可证标记为 `NOASSERTION`，并在后处理阶段写入发布版本 `1.1.0` 和提交号，避免从无关仓库文件错误推断许可证。

## 4. 本记录能够证明什么

1. 两个候选包均可由记录的提交重新构建。
2. 源码单元测试、竞态测试和静态检查通过。
3. 当前依赖没有 `govulncheck` 报告的已知漏洞。
4. 包内文件哈希一致，Linux 最终产物可以启动并通过失败关闭检查。
5. Windows 产物是 x86-64 PE 文件，并从干净提交交叉编译得到。

## 5. 本记录不能证明什么

1. 没有在任何 Windows 机器上执行 `install.ps1`、服务安装、页面访问、MCP 导入、服务重启或卸载。
2. 没有在 Windows Server 2019 目标机、整机重启和客户安全软件开启条件下验收。
3. 没有执行 Authenticode 签名、签名校验或正式发布独立双构建。
4. 没有执行恶意软件扫描、秘密扫描和客户要求的第三方安全扫描。
5. 没有完成云端接入码、设备身份、来源登记、relay、visitor、Edge 和客户 MCP 的 C 段联调。
6. 没有使用华微真实业务数据、真实客户凭据或生产环境。
7. `SBOM-WARNINGS.txt` 保留的是工具生成时的原始警告；相关限制已由 `SBOM-NOTES.txt` 和本记录解释，不能把警告文件删除后当作自动消除。

因此，该候选包只满足“本机诊断试用准备”的构建和包级证据，不满足 G7a 受控试装、G7b C 段测试环境技术链路验收或正式生产交付条件。
