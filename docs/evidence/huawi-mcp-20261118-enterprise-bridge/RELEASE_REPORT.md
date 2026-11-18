# 华微 MCP 企业统一入口 1.3.0 候选发布报告

## 1. 发布身份

- 产品：开物内网连接器（Kaiwu Intranet Connector）
- 版本：`1.3.0`
- 分支：`feat/huawi-enterprise-bridge-20261118`
- 基线：`476ff14f9a4663512925e5b14ba29a5729effdce`
- 发布源码提交：`3d0d06cd87268ce61fc2b7c33b18a679a2d12d66`
- 发布源码短提交：`3d0d06c`
- 源码提交时间（`SOURCE_DATE_EPOCH`）：`1795004692`
- 固定构建时间：`2026-11-18T12:24:52Z`
- 独立发布目录：`/media/data3/dengkw/releases/huawi-connector-1.3.0-3d0d06c`

本包固定包含提交 `99fca7a` 的 WSS/TLS、实际网页验收脚本和部署模板，以及 `f9d4e07`、`3d0d06c` 的证据源码绑定和检查修正。旧的 `1.3.0-8c889fe` 包不包含这些变更，不能拿来替代本候选包。

## 2. 交付包

| 平台 | 文件 | 字节数 | SHA-256 |
| --- | --- | ---: | --- |
| Linux amd64 | `kaiwu-connector-1.3.0-linux-amd64.tar.gz` | 9,255,777 | `f19b31b959f8cfd38a3234bc0a519c2a8acf1877397f24b80657b6a6480596cd` |
| Windows amd64 | `kaiwu-connector-1.3.0-windows-amd64.zip` | 9,488,466 | `705517ecde9d5ac72bbb30b24c523b607e108203681d79ba16ff69094ea8f5c2` |

外层校验文件为发布目录中的 `SHA256SUMS`。两个压缩包内部也各自包含 `SHA256SUMS`，可用于解压后的文件校验。

Windows 包包含：

- `install.ps1`
- `uninstall.ps1`
- `windows-smoke.ps1`
- Windows amd64 连接器和官方 `frpc.exe`
- 配置模板、文档、SBOM、漏洞扫描结果、许可证和构建证据

Windows 包已构建和做格式/完整性检查，但没有在 Windows Server 2019 实机运行，不能称为 Windows 实机验收通过。

## 3. 构建环境

| 项目 | 固定值 |
| --- | --- |
| Go | `go1.27.4 linux/amd64` |
| CycloneDX GoMod | `v1.12.0` |
| govulncheck | `v1.9.0` |
| FRP | 官方 `0.61.1` |
| Linux frpc SHA-256 | `0bb98b7a69ce8ee23bc60fec1664f22040a2d44032296e03c8b6573a5d28dd0b` |
| Windows frpc SHA-256 | `d9a8cf8bc61bd5c8d57a2a4801316a18291f76d45dc5fd8aaf425bd91b57159d` |

构建产物、缓存和临时目录全部位于 `/media/data3/dengkw/`，没有向根分区写入发布包。

## 4. 可复现性

构建使用同一固定提交 `3d0d06c` 的两次独立 `git archive` 源码目录。每次只把被 `.gitignore` 排除的固定 frpc 二进制复制回 `connector/packaging/bin/`，随后使用完全相同的构建参数。

逐字节比较结果：

- Linux 压缩包：一致；
- Windows 压缩包：一致；
- 外层 `SHA256SUMS`：一致。

发布目录保留两份构建日志、两份包哈希、`REPRODUCIBILITY.txt` 和 `PACKAGE_VALIDATION.txt`。

## 5. 包校验结果

- Linux 包内 `sha256sum -c SHA256SUMS`：全部通过；
- Windows 包内 `sha256sum -c SHA256SUMS`：全部通过；
- 外层 `sha256sum -c SHA256SUMS`：全部通过；
- Windows ZIP `unzip -t`：无压缩数据错误；
- Linux TAR 列表读取：通过；
- Linux 连接器：静态链接 ELF x86-64，版本输出为 `1.3.0 (commit 3d0d06c, built 2026-11-18T12:24:52Z)`；
- Windows 连接器：PE32+ x86-64；
- 两个 frpc：分别为 ELF x86-64 和 PE32+ x86-64；
- 包内 `版本信息.txt`：版本 `1.3.0`、提交 `3d0d06c`、固定构建时间；
- SBOM：CycloneDX 1.5 JSON，时间戳固定为构建时间，提交属性为 `3d0d06c`；
- `govulncheck`：`No vulnerabilities found.`

## 6. 源码、证据和包对应关系

| 对象 | 标识 | 说明 |
| --- | --- | --- |
| 运行源码 | `3d0d06c` | 候选包中的连接器、服务端、脚本和部署模板 |
| 合成链路证据 | `e2e-synthetic-latest.json` | `source_commit=3d0d06c`、`source_dirty=false` |
| HTTPS/WSS 证据 | `e2e-tls-wss-latest.json` | `source_commit=3d0d06c`、`source_dirty=false` |
| 实际网页对话证据 | `e2e-chat-browser-latest.json` | `source_commit=3d0d06c`、`source_dirty=false` |
| 候选包 | `1.3.0` | 从同一提交 `3d0d06c` 双构建生成 |

本报告和验收文档所在 Git 提交是发布证据提交；该提交只包含公开证据和说明，不再改变候选包运行代码。具体提交可用 `git log -1 -- docs/evidence/huawi-mcp-20261118-enterprise-bridge/RELEASE_REPORT.md` 查询。

## 7. 验收边界

本次发布证明代码、自动化测试、本机 TCP 合成链路、本机 HTTPS/WSS 回环、实际开物网页合成对话和发布包是一致的。它不证明以下事项已经通过：

- Windows Server 2019 实机安装、DPAPI、ACL、服务重启、整机重启、升级和卸载；
- 真实公网 DNS、正式证书、腾讯云 Edge、反向代理和专属 frps 部署；
- 腾讯云生产模型路由；
- 华微真实业务 MCP、真实客户凭据和真实业务数据查询。

`SYNTHETIC-ORDER-42` 是一条固定模拟数据。即使它能通过实际网页对话返回，也不是客户业务正确的证据。
