# 华微 MCP 企业统一入口 1.3.0 发布报告

## 发布身份

- 产品：开物内网连接器（Kaiwu Intranet Connector）
- 版本：`1.3.0`
- 分支：`feat/huawi-enterprise-bridge-20261118`
- 基线：`476ff14f9a4663512925e5b14ba29a5729effdce`
- 发布源提交：`8c889fec818be8ec4a440a86d742134f9f77927a`
- 发布源短提交：`8c889fe`
- 源码提交时间（`SOURCE_DATE_EPOCH`）：`1795001538`
- 固定构建时间：`2026-11-18T11:32:18Z`
- 独立发布目录：`/media/data3/dengkw/releases/huawi-connector-1.3.0-8c889fe`

发布提交包含功能提交 `23bcddd2e3339801bcca37becdf710152432fe6b`，并在其上补齐了可复现构建和 Windows 冒烟脚本打包。功能提交与发布提交之间没有运行时业务代码变化。

## 交付包

| 平台 | 文件 | 字节数 | SHA-256 |
| --- | --- | ---: | --- |
| Linux amd64 | `kaiwu-connector-1.3.0-linux-amd64.tar.gz` | 9,255,865 | `407a371efa6f8acf071a0830f753a81c045250e86fe04eaac22325f08ee41509` |
| Windows amd64 | `kaiwu-connector-1.3.0-windows-amd64.zip` | 9,488,573 | `e9cc49f1dfffeb68ba5061a47648f30ef74df1fd1d332fbb3ab86a784ff1f6ae` |

外层校验文件为发布目录中的 `SHA256SUMS`。两个压缩包内部也各自包含 `SHA256SUMS`，可用于解压后的文件校验。

Windows 包已确认包含以下三个脚本：

- `install.ps1`
- `uninstall.ps1`
- `windows-smoke.ps1`

两个包都包含 `bin/`、`docs/`、`compliance/`、`版本信息.txt` 和包内 `SHA256SUMS`。Linux 包另含 systemd 单元和安装卸载脚本；Windows 包另含 DPAPI/ACL 安装、卸载和冒烟检查脚本。

## 构建环境

| 项目 | 固定值 |
| --- | --- |
| Go | `go1.27.4 linux/amd64` |
| CycloneDX GoMod | `v1.12.0` |
| govulncheck | `v1.9.0` |
| FRP | 官方 `0.61.1` |
| Linux frpc SHA-256 | `0bb98b7a69ce8ee23bc60fec1664f22040a2d44032296e03c8b6573a5d28dd0b` |
| Windows frpc SHA-256 | `d9a8cf8bc61bd5c8d57a2a4801316a18291f76d45dc5fd8aaf425bd91b57159d` |
| frps SHA-256 | `62c2177b6add2f48d01f73578fe130ecbfafd0ed6571ddeba8ef917434ecb607` |

构建产物和缓存全部位于 `/media/data3/dengkw/`，没有向根分区写入发布包。

## 可复现性验证

第一份构建直接从发布提交 `8c889fe` 的干净工作树执行。第二份构建从同一提交的 `git archive HEAD` 独立源码目录执行，只把被 `.gitignore` 排除的两个固定 frpc 二进制复制进去。

两次构建使用完全相同的以下参数：

- `VERSION=1.3.0`
- `COMMIT=8c889fe`
- `SOURCE_DATE_EPOCH=1795001538`
- `BUILD_TIME=2026-11-18T11:32:18Z`
- Go、CycloneDX、govulncheck、Python 和 frpc 固定版本
- 相同的数据盘 Go 缓存和临时目录

逐字节比较结果：

- Linux 压缩包：一致
- Windows 压缩包：一致
- 外层 `SHA256SUMS`：一致

构建脚本已禁用 VCS 自动元数据、规范化 Go 模块清单中的绝对路径、固定 SBOM 时间戳，并规范化压缩包时间和文件顺序。

## 包校验结果

- Linux 包内 `sha256sum -c SHA256SUMS`：全部通过
- Windows 包内 `sha256sum -c SHA256SUMS`：全部通过
- 外层 `sha256sum -c SHA256SUMS`：全部通过
- Windows ZIP `unzip -t`：无压缩数据错误
- Linux TAR 列表读取：通过
- Linux 连接器：静态链接 ELF x86-64
- Windows 连接器：PE32+ x86-64
- 两个 frpc：分别为 ELF x86-64 和 PE32+ x86-64
- `版本信息.txt`：版本 `1.3.0`，提交 `8c889fe`
- SBOM：CycloneDX 1.5 JSON，时间戳固定为构建时间
- `govulncheck`：`No vulnerabilities found.`

## 验收边界

本次发布证明代码、自动化测试、本机回环合成链路和发布包是一致的。它不证明以下事项已经通过：

- Windows Server 2019 实机安装、DPAPI、ACL、服务重启、整机重启、升级和卸载；
- 真实公网 DNS、证书、腾讯云 Edge、反向代理和专属 frps 部署；
- 登录实际开物网页并完成平台对话调用；
- 华微真实业务 MCP、真实客户凭据和真实业务数据查询。

以上事项分别需要后续 Windows 实机验收、受控公网部署和客户业务验收。即使 `SYNTHETIC-ORDER-42` 能完整返回，也只是一条固定合成数据，不是客户业务正确的证据。
