# 固定SDK与目标Windows支持范围核查
核查日期：2026-11-19；这是源码、许可证与官方支持范围检查，未运行客户Windows Server。

- 官方Go Windows支持表目前为Windows10/Windows Server2016及以上（amd64）。客户Windows Server2019 Datacenter x64在范围内；不是Build17763.9020实机已通过。
- 实际工具链 /media/data3/dengkw/toolchains/go1.27.4，VERSION=go1.27.4。
- github.com/modelcontextprotocol/go-sdk固定v1.8.0，实际go.mod要求Go1.25.0。构建脚本需将旧Go1.22说明更新为1.25，版本改1.3.8。
- 现有CGO_ENABLED=0、GOOS=windows、GOARCH=amd64路线不需要客户安装Go、Python、Node或C工具链；SDK源码未发现import C。
- 固定v1.8.0 LICENSE同时包含Apache-2.0/MIT过渡声明及条款，必须打包完整原文及依赖必要许可，不能写“全部MIT”。
- LICENSE SHA256: af679003d933f045393a6a029f43da113f9ae364eac651d9ae268392985580f5。
- SDK go.mod SHA256: 8679c6a4a8447d6d2d7e3ac99e922161ba1a0eaa881d7666d503cc473cdbc8a3。
- 首次go mod tidy较慢的证据是segmentio/encoding v0.5.4临时下载ZIP持续增大（17:58:41北京9,992,831字节，17:59:21为11,762,303字节），不能误判为MCP服务死锁；未干预下载进程或修改代理。
- Shervin Windows11与客户WindowsServer2019的验证结果必须分开记录。
官方依据：
https://go.dev/wiki/Windows
https://go.dev/wiki/MinimumRequirements
https://go.dev/doc/go1.27
https://github.com/modelcontextprotocol/go-sdk/blob/v1.8.0/LICENSE
