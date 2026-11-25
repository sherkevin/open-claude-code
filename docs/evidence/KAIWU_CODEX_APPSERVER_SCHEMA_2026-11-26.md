# 2026-11-26｜E2 Codex App Server 本地协议基线

复用 VIP 已有独立二进制 /media/data3/dengkw/.private-dev/bin/codex-bin，报告版本 codex-cli 0.165.0，SHA-256 c06a67fd8ac76508dc78bd99ce20c9f317b8d568beda59485f3f4134df9962d5。此为本地实际二进制固定候选，不等同已核实官方发行资产、签名及许可清单。未运行指向其他项目的默认 CLI 包装器，未读取或重新配置凭据。所有运行均使用独立 data3 HOME/CODEX_HOME/TMPDIR/XDG_CACHE_HOME，env -i；只调用 --version、app-server --help、generate-json-schema，不启动服务或模型 turn。

实际生成 320 个 JSON Schema 文件；新增 scripts/oss/codex_appserver_schema_probe.py 直接检查生成的协议，不手写协议定义。实际通过：4 组 RPC 方法、5 组参数字段、4 组必需身份字段、3 对审批请求/响应 schema。原始证据 /media/data3/dengkw/acceptance/kaiwu-codex-appserver-20261126/（version.txt、binary.sha256、app-server-help.txt、schema-generation.log、schema/、contract-probe.log）。

下一步：核验该二进制官方来源/发行资产与许可证；在经授权、隔离且禁止外部遥测的环境验证 stdio initialize/initialized、独立工作区 thread/start/read/resume，之后再用回环合成 provider 验证 turn/interrupt、审批拒绝及恢复；宿主固定用户、工作区、线程和审批身份，模型不能指定宿主路径或扩大 sandbox/权限。无可验证的隔离网络条件时不启动 app-server。不得将 schema 存在当作运行时联调或产品上线；本轮仅完成协议基线。生产 A2.10 发布闸门不变。
