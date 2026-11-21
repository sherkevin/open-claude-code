# Windows 1.3.7 最小验收

这份脚本由总控准备，使用 Shervin 已有的 Python 3.12.4 标准库。它不安装或停止服务，不改连接器配置，不暂停 FRP 或诊断上传，不撤销或轮换 K，不更换 source，也不改库存文件。只有验收证据会写入本机 evidence 目录。

## 文件与前提

将 `windows_diagnostics_acceptance_137.py` 复制到 Windows：

```text
D:\KaiwuAcceptance\public-e2e-20261119\scripts\windows_diagnostics_acceptance_137.py
```

使用已有 Python：

```text
D:\CodeLanguage\Python\Python3.12.4\python.exe
```

脚本只在 Windows 执行业务步骤。它使用已经存在的以下内容：

- 连接器本机界面与 API：`http://127.0.0.1:17872/`。
- 本机模拟 MCP：`http://127.0.0.1:28765/mcp`，健康检查为同端口的 `/health`。
- 当前库存：`D:\KaiwuAcceptance\public-e2e-20261119\data\inventory.json`。读取当前 SKU-1001 的 quantity、revision、note，动态比对，绝不改数据。
- 公网 MCP 配置：`handoff-private\kaiwu-shervin-mcp.private.json`。只在 Windows 当前 Python 进程读取既有访问头，且只发送给该文件声明的 flintw.com 开物 MCP 地址；禁止重定向。
- 本机模拟 MCP 的 `data\mock_mcp_config.private.json`。其中既有 bearer_token 与 hmac_key 仅用于在内存扫描诊断日志，不导出、不打印、不发送到公网。

不要把上述私有配置复制到 VIP 或聊天中。脚本不会创建管理员凭据或把管理员凭据交给连接器。

## 推荐执行顺序

所有命令都在 Windows CMD 运行。每一步可以独立执行，便于总控穿插安装操作与真实 UI 截图。

### 1. 升级前只读基线

```bat
"D:\CodeLanguage\Python\Python3.12.4\python.exe" "D:\KaiwuAcceptance\public-e2e-20261119\scripts\windows_diagnostics_acceptance_137.py" precheck
```

要求已有连接器处于 READY，FRP 与隧道已连接，目标 source 未 disabled，本机模拟 MCP 健康。记录版本、连接器与 source 的不透明 ID、启用的 source 集合、库存文件摘要，不记录返回业务数据。

必须在升级前执行这一步，之后由总控执行已审查的 1.3.7 安装流程。如果先升级再做 precheck，该基线只能证明升级后的稳定状态，不能据此声称升级保留状态已验证。脚本不会执行安装流程。

### 2. 真实公网查询并立即关联日志

```bat
"D:\CodeLanguage\Python\Python3.12.4\python.exe" "D:\KaiwuAcceptance\public-e2e-20261119\scripts\windows_diagnostics_acceptance_137.py" public-success --verify-logs
```

使用现有私有 MCP JSON，依次发送 initialize、notifications/initialized、tools/call get_inventory。返回的 SKU、quantity、revision、note 必须与执行时的本机库存一致。收集经格式验证的 X-Kaiwu-Trace-ID；从 tools/call 返回起，最多五秒内必须在本机“连接器日志”与“开物调用日志”看到同一个 trace。平台侧只能有一个最终结果事件。

该步骤验证真实公网 MCP 通路，不能替代开物聊天页面的对话验收；聊天 UI 由总控另行操作、截图。

### 3. HTTP 200 中的业务错误

```bat
"D:\CodeLanguage\Python\Python3.12.4\python.exe" "D:\KaiwuAcceptance\public-e2e-20261119\scripts\windows_diagnostics_acceptance_137.py" business-error --verify-logs
```

只使用一个不存在的合成 SKU 发起 get_inventory，不修改数据。现有模拟 MCP 应返回 HTTP 200、JSON-RPC error -32602；两端日志必须记录失败，平台日志仍记录 HTTP 200 和 RPC 错误码，不能把它当成功。

### 4. 截图后复核已有 trace（可选）

```bat
"D:\CodeLanguage\Python\Python3.12.4\python.exe" "D:\KaiwuAcceptance\public-e2e-20261119\scripts\windows_diagnostics_acceptance_137.py" verify-logs --case public-success
"D:\CodeLanguage\Python\Python3.12.4\python.exe" "D:\KaiwuAcceptance\public-e2e-20261119\scripts\windows_diagnostics_acceptance_137.py" verify-logs --case business-error
```

复用该 case 最近一次已通过 RPC 断言的证据中的 trace，重新检查两端日志。独立 verify-logs 的五秒从本次查询开始计算；只有前两条命令的 --verify-logs 才证明从公网调用结束开始的五秒关联时限。

### 5. 最终只读检查

```bat
"D:\CodeLanguage\Python\Python3.12.4\python.exe" "D:\KaiwuAcceptance\public-e2e-20261119\scripts\windows_diagnostics_acceptance_137.py" postcheck --expected-version 1.3.7
```

要求 1.3.7、READY、本机 MCP 健康、诊断上传未暂停且无当前存储或上传错误。与 precheck 比较连接器 ID、source ID、启用的 source 集合和库存文件摘要，同时扫描两类日志。

## 日志与隐私断言

每条事件只允许已核对的固定字段；检查 schema、事件 ID、级别、组件、事件码/文案码关系。分别统计真实私有凭据、当前库存修订和备注、无效 SKU 合成 canary、本机 MCP URL 是否出现在日志数据中。控制台和证据只输出扫描数量和命中数量，不输出 canary 的内容。出现任一命中或合同外字段即失败。对空事件数组与 Go 的 null 空切片均能正常等待。

日志接口使用本机网页提供的 kconn 会话 cookie、X-Connector-Token 与 Origin；公网调用使用独立客户端，绝不携带本机会话 cookie。平台日志仍通过连接器自己的签名请求获取，脚本不读取云端管理员接口。

## 证据与失败定位

所有证据写到：

```text
D:\KaiwuAcceptance\public-e2e-20261119\evidence
```

每个 case 产生带时间的 `diag137-<case>-<timestamp>-<suffix>.json`，以及对应 `diag137-<case>-latest.json`。内容包含开始时间（UTC 和北京时间）、结束时间、退出码、通过状态，以及必要的非敏感 trace/ID 和计数。退出码 0 表示本 case 通过，1 表示失败，130 表示被中断。

失败时仅输出固定 failure_category 和异常类型，不输出 response body、请求/响应头、原始错误或 traceback。日志关联失败时，先由总控在真实双日志卡片和云端只读诊断页按该 trace 检查，不要通过打印私有文件排障。若 evidence 文件写入失败，控制台会给出 EVIDENCE_WRITE_FAILED。

## 本次不自动执行的场景

FRP 暂停/恢复、诊断上传暂停/补传由总控单独、显式操作。建议在每个场景前后截图并分别留证：FRP 暂停后本机模拟 MCP 应仍健康、公网调用失败，暂停事件仍能上传；恢复后公网成功。上传暂停期间事件应留在有界持久队列，恢复后补传且不重复。不要把这些场景与安装、私有配置变更或数据修改混在同一自动脚本中。

本脚本已在 VIP 进行 AST 与纯内存逻辑检查，不代表已经在 Windows 运行。本轮 Shervin Windows 的结果也不能代替华微 Windows Server 2019 的实际运行验证。
