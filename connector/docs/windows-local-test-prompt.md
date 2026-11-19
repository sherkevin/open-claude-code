# Windows 内网 MCP 连接器本地验收任务

## 一、你的角色和任务

你是 Windows 现场测试工程师。请在真实的 Windows 电脑或 Windows Server 上，验证“开物内网连接器”能否完成以下闭环：

1. 安装并启动 Windows 服务；
2. 在本机启动一个模拟的内网 MCP Server；
3. 在连接器中导入该 MCP Server 的地址和凭据；
4. 连接器能够完成 MCP 协议握手、发现工具并执行诊断；
5. Windows 服务重启后，配置仍然保留，模拟 MCP 仍然可以完成诊断。

本次测试只使用模拟数据，不使用华微真实业务数据，不使用真实客户凭据，不修改客户生产系统。

## 二、交付包和已知边界

Windows 测试包：

```text
kaiwu-connector-1.3.2-windows-amd64-setup.exe
```

当前版本的已知边界：

1. 安装包 SHA-256 和包内程序提交号以交付消息、`版本信息.txt` 及交付证据中的 `SHA256SUMS` 为准；不要使用任务书内嵌的旧哈希或旧提交号。
2. 云端连接器控制面、专属 FRP 和 MCP Edge 已在工程测试与本机合成链路中实现，但还没有完成真实公网部署和实际开物联调；如果没有我方平台提供的有效接入码，不要自行伪造接入码，也不要把“绑定企业”失败判定为 Windows 安装失败。
3. 本次必须先完成“本地 Windows 安装 + 模拟内网 MCP 诊断”，这是连接器本体能够工作的基础证据。
4. 不要把连接器的本机配置页面开放到局域网或公网。它只允许访问 `http://127.0.0.1:17872/`。
5. 不要关闭 Windows Defender、防火墙或 TLS 校验来让测试通过。
6. 不要使用 `0.0.0.0` 监听连接器配置页面。
7. 本安装包默认不配置云端控制面和 Edge。页面第 1、5、6 步会显示未配置；这是预期失败关闭，不是 Windows 服务故障。
8. 客户目标 Windows Server 2019 已确认具备图形化客户端桌面。本次必须在管理员图形界面中完成安装包解压、PowerShell 安装、Windows 服务查看、浏览器访问 `http://127.0.0.1:17872/` 和人工复测，不能只提交静默安装或命令行结果。

### 强制网络方向验收条件

这是模拟华微内网环境的必要条件，不是可选项：

1. Windows 测试机只能主动向开物云端发起出站连接。
2. 开物 Flint 平台不得直接连接 Windows 测试机的任何监听端口。
3. 模拟 MCP 的 `127.0.0.1:28765` 和连接器页面的 `127.0.0.1:17872` 只能绑定回环地址，不能绑定 Windows 局域网地址或 `0.0.0.0`。
4. 不得在 Flint 中填写 Windows 的局域网地址、公网地址、`127.0.0.1:28765` 或 `127.0.0.1:17872`。
5. 不得使用路由器端口映射、Windows 端口转发、SSH 反向隧道、ngrok、Cloudflare Tunnel、localhost.run 等方式把 Windows 的模拟 MCP 或连接器页面暴露给 Flint。
6. Windows 防火墙必须保持开启；测试不得新增允许外部入站访问 `28765` 或 `17872` 的规则。
7. 云端完整桥梁阶段，Windows 连接器必须主动连接开物控制面和 Edge。Flint 只能调用 Edge 发布的来源地址，不能调用 Windows 本身。
8. 只要 Flint 能够直接访问 Windows 的 `28765` 或 `17872`，本次模拟环境即判定失败，即使其他诊断项目显示通过。

## 三、测试拓扑

Windows 测试机只在本地运行连接器和模拟 MCP；开物控制面与 Edge 位于远端。现场结构必须保持“内网 MCP 只在 Windows 内网，连接器只主动出站连接公网平台”：

```text
Windows 测试机
┌──────────────────────────────────────────────────────┐
│ 开物连接器 Windows 服务                               │
│ 本机配置页面: http://127.0.0.1:17872/                 │
│                                                      │
│ 模拟内网 MCP Server                                  │
│ http://127.0.0.1:28765/mcp                           │
│ Authorization: Bearer demo-token-123                 │
└──────────────────────────┬───────────────────────────┘
                           │
             仅允许 Windows 主动发起出站连接
                           │
                           ▼
开物云端控制面 / MCP Edge
Flint 只能调用云端 Edge，不能连接 Windows
```

模拟 MCP Server 只监听回环地址，不向公网开放。

## 四、记录测试环境

以管理员身份打开 PowerShell，先创建测试目录并开始记录证据：

```powershell
$Root = Join-Path $env:USERPROFILE "Desktop\KaiwuConnectorTest"
$Evidence = Join-Path $Root "evidence"
$MockDir = Join-Path $Root "mock"
New-Item -ItemType Directory -Force $Root, $Evidence, $MockDir | Out-Null

Start-Transcript -Path (Join-Path $Evidence "transcript.txt") -Force

Get-ComputerInfo |
  Select-Object WindowsProductName, WindowsVersion, OsBuildNumber, OsArchitecture |
  Format-List |
  Tee-Object (Join-Path $Evidence "windows-info.txt")

whoami /all |
  Out-File (Join-Path $Evidence "whoami-all.txt")
```

如果 `Get-ComputerInfo` 或 `Tee-Object` 的参数形式在你的 PowerShell 版本中报错，改用：

```powershell
Get-ComputerInfo | Select-Object WindowsProductName, WindowsVersion, OsBuildNumber, OsArchitecture | Format-List *>&1 | Out-File (Join-Path $Evidence "windows-info.txt")
```

## 五、校验并运行安装包

把交付包放到例如 `C:\Users\<你的用户名>\Downloads\`，然后执行：

```powershell
$Package = Join-Path $env:USERPROFILE "Downloads\kaiwu-connector-1.3.2-windows-amd64-setup.exe"
Get-FileHash -Algorithm SHA256 $Package |
  Format-List |
  Tee-Object (Join-Path $Evidence "package-hash.txt")

Start-Process -FilePath $Package -Wait
```

必须确认安装包文件的 SHA-256 与上面给出的值一致。如果不一致，立即停止，不要继续安装。双击安装包时，Windows 会显示正常的用户账户控制提示；确认发布者信息和安装包来源无误后，选择“是”继续。

## 六、确认安装与服务

安装器会自动注册并启动 Windows 服务。安装完成后，在同一个管理员 PowerShell 窗口中采集状态：

```powershell
Get-Service -Name KaiwuConnector |
  Format-List Name, Status, StartType |
  Tee-Object (Join-Path $Evidence "service-status.txt")

sc.exe qc KaiwuConnector |
  Tee-Object (Join-Path $Evidence "service-config.txt")

Get-NetTCPConnection -LocalPort 17872 -ErrorAction SilentlyContinue |
  Format-Table -AutoSize |
  Tee-Object (Join-Path $Evidence "listener.txt")
```

预期结果：

1. 服务名称是 `KaiwuConnector`；
2. 服务状态是 `Running`；
3. 服务启动方式支持开机自动启动；
4. `17872` 端口只监听回环地址，不能监听 `0.0.0.0`；
5. 本机打开 `http://127.0.0.1:17872/` 能显示“开物 · 内网连接器”。

打开页面并保存截图：

```powershell
Start-Process "http://127.0.0.1:17872/"
```

## 七、启动模拟内网 MCP Server

在 `C:\Users\<你的用户名>\Desktop\KaiwuConnectorTest\mock\mock_mcp_server.py` 创建以下文件。使用 UTF-8 保存，不需要安装任何第三方 Python 包。

```python
#!/usr/bin/env python3
"""Minimal Streamable HTTP MCP server for Windows connector testing."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json

AUTH_HEADER = "Bearer demo-token-123"
TOOL_NAME = "get_inventory"


class Handler(BaseHTTPRequestHandler):
    server_version = "KaiwuMockMCP/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.log_date_time_string()} {fmt % args}", flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/mcp":
            self.send_json(404, {"error": "not found"})
            return
        if self.headers.get("Authorization", "") != AUTH_HEADER:
            self.send_json(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
        except Exception as exc:
            self.send_json(400, {"error": f"invalid JSON: {exc}"})
            return

        method = request.get("method")
        request_id = request.get("id")

        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return

        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "kaiwu-mock-inventory",
                    "version": "1.0.0",
                },
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": TOOL_NAME,
                        "description": "Return deterministic demo inventory rows",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "warehouse": {
                                    "type": "string",
                                    "description": "warehouse code",
                                }
                            },
                            "additionalProperties": False,
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = request.get("params") or {}
            if params.get("name") != TOOL_NAME:
                self.send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {
                            "code": -32601,
                            "message": "unknown tool",
                        },
                    },
                )
                return
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": "仓库 A：SKU-1001 库存 42；SKU-2002 库存 17",
                    }
                ]
            }
        else:
            self.send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"unknown method: {method}",
                    },
                },
            )
            return

        self.send_json(
            200,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=28765)
    args = parser.parse_args()
    print(f"Mock MCP listening on http://{args.host}:{args.port}/mcp", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
```

确认电脑有 Python 3：

```powershell
py -3 --version
```

如果 `py` 不存在，使用：

```powershell
python --version
```

然后另开一个 PowerShell 窗口启动模拟 MCP，并保持窗口不要关闭：

```powershell
$Root = Join-Path $env:USERPROFILE "Desktop\KaiwuConnectorTest"
$Mock = Join-Path $Root "mock\mock_mcp_server.py"
py -3 $Mock --host 127.0.0.1 --port 28765 2>&1 |
  Tee-Object (Join-Path $Root "evidence\mock-mcp.log")
```

预期看到：

```text
Mock MCP listening on http://127.0.0.1:28765/mcp
```

先直接验证模拟 MCP 的初始化：

```powershell
$Headers = @{ Authorization = "Bearer demo-token-123" }
$Body = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"local-test","version":"1.0"}}}'

Invoke-RestMethod `
  -Uri "http://127.0.0.1:28765/mcp" `
  -Method Post `
  -Headers $Headers `
  -ContentType "application/json" `
  -Body $Body |
  ConvertTo-Json -Depth 10 |
  Tee-Object (Join-Path $Root "evidence\mock-initialize.json")
```

预期返回 `serverInfo.name` 为 `kaiwu-mock-inventory`，协议版本为 `2025-06-18`。

## 八、在连接器中导入模拟 MCP

打开：

```text
http://127.0.0.1:17872/
```

在“第 2 步：导入现有 MCP 配置”中粘贴：

```json
{
  "mcpServers": {
    "华微库存演示": {
      "type": "http",
      "url": "http://127.0.0.1:28765/mcp",
      "headers": {
        "Authorization": "Bearer demo-token-123"
      }
    }
  }
}
```

重要：上面的 `http://127.0.0.1:28765/mcp` 只用于“连接器本机”的导入和诊断。这里的 `127.0.0.1` 指运行连接器的 Windows 电脑，不是云端平台执行服务器。

不要把这个地址直接导入知行工作台。平台测试连接时，`127.0.0.1` 指平台执行服务器自身；如果该服务器没有在 `28765` 端口运行这个模拟 MCP，平台会如实返回“连接失败，请检查执行服务器到 MCP 的网络、地址及协议”。这类结果不能判定为连接器安装失败。

本 Windows 验收不得为了“先让 Flint 连上”而暴露 Windows 地址。Flint 只能使用连接器在云端绑定成功后导出的 Edge 地址；连接器没有完成绑定和中继时，云端阶段必须保持阻塞。

点击“导入并校验”。

预期结果：

1. 页面显示“配置校验通过”；
2. 不出现错误；
3. 本机数据目录出现 `config.json` 和 `secrets.json`；
4. `secrets.json` 中保存演示凭据，不能出现在导出的平台配置中。

保存页面截图和文件信息：

```powershell
Get-ChildItem "$env:ProgramData\Kaiwu\Connector" -Force |
  Format-Table Name, Length, LastWriteTime |
  Tee-Object (Join-Path $Evidence "data-files.txt")
```

不要直接把 `secrets.json` 的内容发出去；只需要确认文件存在以及权限情况。

## 九、执行内网诊断

在连接器页面点击“开始检测”。

预期诊断结果至少包含以下检查，并且全部通过：

| 检查项 | 预期结果 |
| --- | --- |
| 配置校验 | `pass` |
| 内网连通性 | `pass`，能够连接 `127.0.0.1:28765` |
| 证书校验 | `skip`，因为这是本地明文 HTTP 测试 |
| 协议握手 | `pass`，服务端名称为 `kaiwu-mock-inventory`，版本 `1.0.0`，协议版本 `2025-06-18` |
| 认证与工具目录 | `pass`，可用工具 1 个 |

请把页面上诊断结果的完整 JSON 复制到：

```text
C:\Users\<你的用户名>\Desktop\KaiwuConnectorTest\evidence\diagnose.json
```

同时保存截图，截图中应能看到上述检查项和工具名称 `get_inventory`。

### 认证失败反向测试

在确认正常配置通过后，可以把导入配置中的演示凭据临时改成：

```json
"Authorization": "Bearer wrong-token"
```

重新导入并再次诊断。预期出现认证失败，错误码应接近 `UPSTREAM_AUTH_FAILED` 或 HTTP 401。完成后必须把凭据改回 `Bearer demo-token-123`，重新导入并确认诊断恢复通过。

恢复正确凭据后，再导入同一份配置一次，并确认：

1. 第 2 步仍然保留同一个来源编号；
2. 第 3 步的动态认证工具允许列表仍然包含 `get_inventory`；
3. `/api/status` 的 `sources[].tool_allowlist` 不为空；
4. 带 `tool_probe=get_inventory` 的诊断仍然通过。

这是 `1.2.0` 现场发现的 D01 回归项：重新导入同一模型上下文协议地址时，不能静默清空管理员已经批准的工具允许列表。

## 十、验证 Windows 服务重启恢复

执行：

```powershell
Restart-Service -Name KaiwuConnector -Force
Start-Sleep -Seconds 3

Get-Service KaiwuConnector |
  Format-List Name, Status, StartType |
  Tee-Object (Join-Path $Evidence "service-after-restart.txt")

Invoke-RestMethod "http://127.0.0.1:17872/api/status" |
  ConvertTo-Json -Depth 10 |
  Tee-Object (Join-Path $Evidence "status-after-restart.json")
```

预期结果：

1. 服务重新进入 `Running`；
2. 页面重新可以打开；
3. 状态仍然是“待配置”，英文状态为 `CONFIGURING`；
4. 之前导入的模拟 MCP 配置没有丢失；
5. 再次点击“开始检测”，协议和工具目录检查仍然通过。
6. 重启后再次确认 `tool_allowlist` 包含 `get_inventory`，并且 `tool_probe=get_inventory` 仍然通过。

这一步通过，说明 Windows 服务重启后能够恢复本机来源配置和密钥文件。

## 十一、检查权限和监听范围

执行以下命令并保存结果：

```powershell
icacls "$env:ProgramData\Kaiwu\Connector" |
  Tee-Object (Join-Path $Evidence "data-dir-acl.txt")

Get-ChildItem "$env:ProgramData\Kaiwu\Connector" -Force |
  ForEach-Object {
    icacls $_.FullName
  } |
  Tee-Object (Join-Path $Evidence "data-file-acl.txt")

Get-NetTCPConnection -LocalPort 17872 -ErrorAction SilentlyContinue |
  Format-Table -AutoSize |
  Tee-Object (Join-Path $Evidence "listener-after-restart.txt")
```

必须特别记录：

1. 服务实际以哪个 Windows 账号运行；
2. `config.json` 和 `secrets.json` 是否只允许服务账号、SYSTEM 和管理员读取；
3. 本机配置页面是否只监听 `127.0.0.1:17872`；
4. 是否有杀毒软件、SmartScreen、防火墙或系统策略拦截安装。

当前版本可能存在“服务默认以 LocalSystem 运行”以及 Windows ACL 没有单独收紧的问题。如实记录，不要为了让测试通过而手工放宽权限。

另外执行并保存以下网络方向证据：

```powershell
Get-NetTCPConnection -State Listen |
  Where-Object { $_.LocalPort -in 17872, 28765 } |
  Format-Table LocalAddress, LocalPort, State, OwningProcess -AutoSize |
  Tee-Object (Join-Path $Evidence "listen-addresses.txt")

Get-NetFirewallProfile |
  Format-Table Name, Enabled, DefaultInboundAction, DefaultOutboundAction -AutoSize |
  Tee-Object (Join-Path $Evidence "firewall-profiles.txt")
```

判定要求：

1. `17872` 和 `28765` 的 `LocalAddress` 只能是 `127.0.0.1` 或 `::1`；
2. 出现 `0.0.0.0`、`::`、Windows 局域网地址或公网地址即失败；
3. 防火墙配置文件必须保持启用；
4. 不应为了本次测试新增外部入站放行规则。

如果现场有第二台电脑或手机，也应从该设备尝试连接 Windows 的局域网地址和 `28765`、`17872` 端口，预期全部失败。没有第二台设备时，必须如实写“未执行外部入站探测”，不能伪造结果。

## 十二、云端桥梁测试

只有在交付人员提供了真实、一次性的平台接入码时，才执行本阶段。没有接入码就记录“未执行，缺少云端接入码”，不要自己生成假接入码。

如果提供了接入码：

1. 在连接器页面“第 1 步：绑定企业”输入接入码并点击“绑定”；
2. 保存页面截图；
3. 记录页面提示、HTTP 错误和连接器状态；
4. 绑定成功后点击“连接”；
5. 保存“通道状态”“状态原因”和任何错误信息；
6. 确认 Windows 主动向开物控制面和 Edge 建立出站连接；
7. 记录连接器导出的“Edge 地址预览”，确认其中没有 Authorization、客户 Bearer 或 Windows 地址；该预览不是可直接使用的正式平台授权；
8. 由云端管理员签发企业集成凭据 K，取得完整的 `mcpServers` JSON，再导入 Flint；
9. 从 Flint 调用模拟 MCP 工具并验证库存结果；
10. 不要反复点击，避免制造无意义的请求。

建立连接后采集出站连接证据：

```powershell
Get-NetTCPConnection -State Established |
  Where-Object { $_.RemotePort -eq 443 } |
  Format-Table LocalAddress, LocalPort, RemoteAddress, RemotePort, OwningProcess -AutoSize |
  Tee-Object (Join-Path $Evidence "established-outbound-443.txt")
```

必须确认：

1. 连接方向是 Windows 主动连接开物云端；
2. Flint 的导入配置不包含 Windows 的局域网地址、公网地址或本机回环地址；
3. Flint 使用云端 Edge 地址访问内网工具；
4. 断开连接器或撤销连接器后，Flint 不能继续调用模拟 MCP；
5. Flint 直接访问 Windows 的 `28765` 或 `17872` 必须失败，而且这种访问不是测试成功的必要条件。

当前需要特别记录的已知问题：

1. 包内默认不提供云端控制面和 Edge 地址；只有在交付人员完成云端 Edge、专属 relay 和证书准备并明确提供测试地址后才允许配置；
2. 控制面已经提供接入码兑换、期望状态、通道材料、心跳和企业集成签发，但本提示不能代替实际云端部署、DNS/证书和公网可达性验收；
3. 如果没有已部署的云端中继、Edge 和正式企业集成 JSON，连接器不能完成“安装 → 平台配置 → 调用内网 MCP”的完整桥梁验收。

因此，云端阶段失败时必须区分：

- 是 Windows 安装或本机 MCP 诊断失败；
- 还是云端控制面接口缺失、地址不一致或中继未启动。

不要把一个云端 404 误报成“Windows 安装包不可用”。

## 十三、卸载验证

在证据采集完成后，优先从 Windows“设置 → 应用 → 已安装的应用”运行“开物内网连接器”的卸载程序，或从开始菜单运行卸载快捷方式。默认卸载删除程序和服务，保留配置、本机身份和机器级云端地址，便于重新安装后恢复。以下命令用于记录默认卸载结果：

```powershell
Start-Process "$env:ProgramFiles\Kaiwu\Connector\uninstall.exe" -Wait

Start-Sleep -Seconds 2
Get-Service KaiwuConnector -ErrorAction SilentlyContinue |
  Format-List Name, Status |
  Tee-Object (Join-Path $Evidence "service-after-uninstall.txt")

Test-Path "$env:ProgramFiles\Kaiwu\Connector" |
  Tee-Object (Join-Path $Evidence "install-dir-after-uninstall.txt")

Test-Path "$env:ProgramData\Kaiwu\Connector" |
  Tee-Object (Join-Path $Evidence "data-dir-after-uninstall.txt")
```

预期结果：

1. `KaiwuConnector` 服务不存在；
2. 程序目录被删除；
3. 数据目录仍然存在，配置和本机身份没有被默认卸载删除；
4. 没有残留监听 `17872` 的进程。

只有在本机已明确是专用测试机、证据已经归档，并且确认不需要再次安装恢复配置时，才允许由我方工程师运行附加 ZIP 中的 `uninstall.ps1 -Purge` 彻底清除。普通客户卸载不得使用 `-Purge`。

## 十四、必须提交的证据

请把以下内容打包成 `KaiwuConnectorWindowsEvidence.zip`：

1. Windows 版本、架构和当前用户权限；
2. 安装包 SHA-256；
3. `sc.exe qc KaiwuConnector` 输出；
4. 服务启动、停止、重启和卸载结果；
5. 安装页面截图；
6. 模拟 MCP 启动日志；
7. 初始化请求的返回结果；
8. 连接器导入结果截图；
9. 诊断完整 JSON；
10. 正常凭据和错误凭据的对比结果；
11. 重启后的状态和再次诊断结果；
12. 数据目录 ACL 和监听端口结果；
13. `17872` 和 `28765` 的监听地址证据；
14. Windows 防火墙配置和外部入站探测结果；
15. 如果执行了云端绑定，记录完整错误信息和截图；
16. 云端绑定后的出站连接证据；
17. 连接器导出的 Edge 配置；
18. 任何杀毒软件、SmartScreen、防火墙或脚本执行策略提示。

不要提交真实凭据、真实客户数据、`identity.json` 或未经脱敏的 `secrets.json`。

## 十五、判定标准

本次测试分为三个结论：

### A. 本机安装通过

- 服务注册成功；
- 服务能够启动和自动恢复；
- 本机页面可以打开；
- 卸载能够清理服务和目录。

### B. 本地 MCP 链路通过

- 模拟 MCP 可以独立响应初始化；
- 连接器可以导入 `mcpServers` 配置；
- 配置、网络、协议和认证检查通过；
- 能发现工具 `get_inventory`；
- 服务重启后配置和诊断仍然有效。

### C. 云端完整桥梁通过

- 能用真实平台接入码绑定；
- 能从平台下发来源和中继参数；
- Windows 服务只通过主动出站连接建立隧道；
- 平台能够实际调用模拟内网 MCP 并收到库存结果；
- Flint 导入配置中没有 Windows 地址；
- Flint 不能直接访问 Windows 的 `28765` 或 `17872`；
- 断开或撤销后平台不能继续调用。

如果 C 无法执行，必须明确写“真实公网控制面、中继或 Edge 尚未部署或联调，或撤销与来源隔离验证尚未完成”，不能把 A、B 通过写成完整交付通过。如果 Flint 能直接访问 Windows 端口，即使工具调用成功，C 也必须判定失败。

## 十六、最终回复格式

请按以下格式回复：

```text
测试结论：A 通过 / B 通过 / C 阻塞或通过

Windows 环境：
- 版本：
- 架构：
- 是否管理员：

安装包：
- 安装包 SHA-256 是否匹配：
- 包内版本：

安装与服务：
- 服务是否运行：
- 是否监听 127.0.0.1:17872：
- 卸载是否清理：

本地 MCP：
- 服务端名称和版本：
- 协议版本：
- 工具列表：
- 正常诊断是否通过：
- 错误凭据是否被拒绝：

重启恢复：
- 服务是否恢复：
- 配置是否保留：
- 再次诊断是否通过：

网络方向：
- 17872 的监听地址：
- 28765 的监听地址：
- Windows 防火墙是否保持启用：
- 从其他设备访问 Windows 入站端口是否失败：
- Flint 配置中是否只包含云端 Edge 地址：
- Windows 是否只主动发起出站连接：

云端桥梁：
- 是否执行：
- 绑定结果：
- 连接结果：
- 若失败，是接口、地址、中继还是其他问题：

证据文件：
- 证据包路径：

发现的缺陷：
- ...

建议下一步：
- ...
```
