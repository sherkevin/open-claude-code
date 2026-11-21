# 1.3.8 内置测试 MCP 验收准备

状态：**已准备脚本；只做源码阅读与语法检查，尚未执行运行时验收。**
日期：2026-11-19。适用范围：总控工程师的隔离候选验收及安装获准后的 Windows 验收，不是客户操作说明。

**后续收敛：腾讯云实际为共享单一 relay 监听/环境，不能并行启动新 connector bridge。本轮 Linux 只执行 LINUX_ACCEPTANCE_PREP.md 的 --local-only；下文第 5 节的新 tenant 公网流程暂停，不作为当前可执行步骤。真实公网测试留待获准安装 Shervin 1.3.8 后复用已有测试专属通道。**

## 1. 本次要证明什么

最终要证明：新安装包自带并自动启动独立的测试 MCP；测试请求从开物公网经过现有 FRP 回到本机；坏的客户 MCP 不阻断这个测试；测试服务的启停不改动或中断已有业务来源。仅本机 HTTP 返回 200、仅前端绿色、仅旧 mock 成功，均不足以证明这些目标。

本轮准备没有安装 Windows、启动候选连接器、签发/兑换接入码、创建企业、部署云端、调用自检或修改任何来源。客户真实接入码、Shervin 现有 1.3.6 企业身份和业务来源不用于 Linux 隔离试验。

交付的脚本：

- `docs/evidence/huawi-builtin-demo-20261119/builtin_demo_acceptance_138.py`
- 复用的独立 HTTP/CSRF/JSON-RPC/SSE helper：`docs/evidence/huawi-diagnostics-20261119/windows_diagnostics_acceptance_137.py`

脚本不会安装、绑定、签发凭据、改来源、改示例开关、暂停隧道或操作 Windows 服务。只有显式执行 `self-test --allow-self-test` 才调用产品自检 API；该产品 API 会注册/连接自己的示例来源，须在总控已确定的验收范围内运行。其余命令只读取状态或调用固定的只读示例工具。

## 2. 已从当前源码确认的接口

这些是工作树的实现，不等于已部署的生产接口。候选版本冻结后应再次核对源码提交和安装包散列。

| 接口/路径 | 当前实现及用途 |
| --- | --- |
| 本机界面 `http://127.0.0.1:17872/` | Windows 默认；隔离 Linux 使用另一个空闲端口 |
| `GET /api/status` | 包含 version、connector_id、sources、auth_profiles、builtin_demo、隧道状态 |
| `POST /api/self-test` | 请求空对象；响应 `{ok:true,data:{ok,trace_id,source_id,stages,probe,...}}`；HTTP 200 不代表 data.ok 为真 |
| `POST /api/builtin-demo/enabled` | 请求 `{"enabled":true}` 或 false；只改变内置示例开关；本脚本不发送该请求 |
| 本机防护 | 先打开首页取得 kconn cookie；API 带 X-Connector-Token 与 Origin；脚本复用现有 helper |
| 内置 MCP 地址 | 从 status.sources 按 builtin_demo.source_id 查找；当前是动态端口 `http://127.0.0.1:<port>/mcp`，绝不写死为旧 mock 的 28765 |
| 公网控制面配置 | `KAIWU_CLOUD_BASE=https://flintw.com/api/kaiwu`，来自 connector.env.example |
| 公网 Edge 配置 | `KAIWU_EDGE_BASE=https://flintw.com/projects/kaiwu/mcp`，来自同一模板 |
| 公网来源地址 | `https://flintw.com/projects/kaiwu/mcp/sources/<source_id>/mcp` |
| 云端自检 | `POST /api/kaiwu/v1/connectors/<connector_id>/self-test`，由连接器使用设备签名调用；验收脚本不伪造该签名 |

当前本机自检阶段包括 local_mcp、enterprise_binding、tunnel 和云端返回的阶段。不要将云端阶段列表预先写成固定数量；但成功验收要求所有实际返回阶段均为 passed。断网/404/解码失败不算预期失败通过。

已绑定设备的示例 source_id 必须是：

```python
"src_" + sha256(("kaiwu_builtin_demo_v1:" + connector_id).encode("utf8")).hexdigest()[:32]
```

未绑定的新安装允许临时来源 ID；绑定后的每次自检、连接器重启、示例重新启用必须保持公式对应的同一 ID。

## 3. 总控需要提供的真实输入

以下不由本脚本猜测或自动生成：

1. 候选源码提交、候选 Windows 安装包绝对路径及 SHA-256、对应 Linux 候选二进制与 FRPC 路径。源码 version 字符串不能代替二进制/安装包的真实来源核对。
2. Linux 独占数据目录和空闲回环 UI 端口。所有数据、日志、缓存、证据都在 `/media/data3/dengkw/` 下。
3. 经总控创建并授权的新测试企业/连接器及其一次性测试接入码。不得使用华微客户码，不得复制 Shervin 已安装连接器的 identity/state/来源文件。
4. 服务端 self-test 的实际部署版本、允许访问的真实测试租户、可查询到 trace 的云端日志入口。源码已有接口不能证明部署完成。
5. 如执行独立公网三工具探测，总控提供只含该示例来源的一项 mcpServers 私有 JSON，使用该测试来源允许调用三个示例工具的集成凭据。文件在本机受限目录，不打印、不加入 git、不发群。云端仅供 ping 的临时自检凭据不可冒充这个三工具凭据。
6. Windows 精确安装包的执行授权和真实安装后的服务/程序路径。安装授权未获得前只允许准备文件。
7. 华微真实 Windows Server 版本/位数的最终确认或对应测试机。Shervin Windows 11 上成功不能改写为 Windows Server 2019 已实测。

## 4. 脚本命令及证据含义

| 命令 | 检查内容 |
| --- | --- |
| snapshot | 记录版本、企业身份、非示例来源的配置散列、脱敏认证资料散列；不保存业务地址、名称、工具参数或凭据 |
| local-probe | 要求内置服务 enabled/running，示例来源唯一；初始化 MCP、tools/list 恰好 3 工具；逐项调用并验证新 nonce、时间、示例 marker、订单 12、指定商品库存 128 |
| public-probe | 使用显式提供的测试 JSON 走公网 HTTPS；重复独立 MCP 握手和 3 工具查询；校验公网 trace，比较公网和本机 instance_id 相同 |
| self-test --allow-self-test | 调用产品的真实自检 API；要求 data.ok、全部阶段、trace、source_id、nonce/时间/marker；比较自检前后业务来源及认证元数据不变 |
| preserved --baseline FILE | 对比 snapshot 的身份与非示例来源/认证元数据散列；已有示例 ID 也必须稳定 |
| demo-state --expected-demo-state on/off | 只读取开关结果；off 要求 enabled=false、running=false 且对应来源 disabled=true；不替使用者切换开关 |

可选参数：

- `--expected-version` 默认 1.3.8；升级前快照明确传 1.3.6。
- `--expected-commit` 要求版本信息包含指定提交；只有实际构建确实嵌入提交时才使用。
- `--expect-unbound` 确认全新隔离目录尚未绑定。
- `--bad-source-id` 只在自检时检查既有坏测试来源仍启用、回环端口不可达且自检前后配置不变；脚本从不创建这个来源。
- `--expect-failure-stage` 只接受明确指定阶段出现 failed；不得用它把发布候选正常链路失败改写成通过。
- `--artifact PATH --expected-artifact-sha256 SHA256` 校验关联工件字节；这不证明 Windows 当前服务一定运行该工件，仍须核对 SCM 路径与已安装 exe 散列。

每条命令产生唯一 JSON 证据和对应 `builtin138-<命令>-latest.json`。退出码 0 只表示该条检查通过。必须使用唯一快照文件做基线，防止后续快照覆盖 latest 后误比较。证据不写原始请求/响应、授权头、接入码、客户查询结果或异常全文。

配置保全比较覆盖本机状态 API 公开的来源配置和脱敏认证元数据。秘密字节没有被 API 公开，脚本不会读取或宣称已逐字节验证；通过既有业务来源的真实授权查询补充功能回归。

## 5. Linux 新 tenant 公网验收：历史准备，当前暂停

本节是未来执行模板，**本轮未运行**。总控先填写路径/端口并确认候选和云端范围。

### 5.1 独立启动，先证明自动启动

建议目录形态为 `/media/data3/dengkw/acceptance/huawi-demo138-<候选短提交>/`。这是建议的新目录，不宣称已经存在。候选程序、FRPC、data、evidence、private 各自位于该目录或同样受控的 data3 路径。不得把 Shervin Windows 数据复制进来，不得指向现有生产连接器目录。

当前 CLI 真正支持的启动参数来自 `connector/cmd/connector/main.go`：

```bash
# 以下变量先由总控填成实际已核对值；此处不提供默认凭据。
"$CANDIDATE_BIN" serve \
  --data-dir "$ISOLATED_DATA_DIR" \
  --listen "127.0.0.1:$UI_PORT" \
  --cloud "https://flintw.com/api/kaiwu" \
  --edge "https://flintw.com/projects/kaiwu/mcp" \
  --frpc "$CANDIDATE_FRPC"
```

另一个已授权终端准备公共参数：

```bash
REPO=/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118
SCRIPT="$REPO/docs/evidence/huawi-builtin-demo-20261119/builtin_demo_acceptance_138.py"
HELPER="$REPO/docs/evidence/huawi-diagnostics-20261119/windows_diagnostics_acceptance_137.py"
COMMON=(--local-url "http://127.0.0.1:$UI_PORT" --evidence-dir "$EVIDENCE_DIR" --helper "$HELPER")
python3 -B "$SCRIPT" local-probe "${COMMON[@]}" --expect-unbound
```

这一条必须在点击“连接自检”、运行其它 mock 或补装运行时之前通过。它直接访问新版连接器自动创建的动态端口。另行核对该端口的监听进程属于候选连接器 PID；脚本不自动杀进程或纠正错误安装。

### 5.2 绑定测试身份，验证公网

总控在隔离实例界面中使用新的测试接入码完成企业绑定。此动作不由验收脚本代做。绑定后再运行：

```bash
python3 -B "$SCRIPT" local-probe "${COMMON[@]}"
python3 -B "$SCRIPT" snapshot "${COMMON[@]}"
# 把上一步生成的唯一 snapshot JSON 绝对路径记入 BASELINE。
python3 -B "$SCRIPT" self-test "${COMMON[@]}" --allow-self-test
python3 -B "$SCRIPT" public-probe "${COMMON[@]}" --private-mcp-json "$TEST_DEMO_JSON"
python3 -B "$SCRIPT" preserved "${COMMON[@]}" --baseline "$BASELINE"
```

公网三工具探测会发自己生成的随机 nonce，比较实际回包，并确认公网到达的 instance_id 与当前本机内置服务一致。产品自检的 nonce 由产品生成；脚本检查其格式、时间与 marker，真正的 nonce 原值往返由产品代码验证，独立公网探测补充外部证据。云端还须按 trace 查到对应 Edge/FRP 调用记录，不能只凭 trace 字符串外观宣称日志链路通过。

### 5.3 坏业务来源隔离

仅在隔离测试企业做。为免影响业务，使用一个经确认没有监听的回环端口作为独立坏测试来源；不要更改任何真实客户来源。

由总控在连接前通过现有界面的“添加来源”单独添加，不使用会替换全部来源的导入接口。当前 SaveSource 在 READY/CONNECTING/DEGRADED 等连接状态会要求先暂停，因此必须把这个准备纳入隔离实例自己的步骤；不要为了凑测试去暂停 Shervin 现有隧道。

保留坏来源启用状态，记录其 source_id。它必须不同于示例 source_id。准备后另存新基线，再执行：

```bash
python3 -B "$SCRIPT" snapshot "${COMMON[@]}"
python3 -B "$SCRIPT" self-test "${COMMON[@]}" --allow-self-test --bad-source-id "$BAD_SOURCE_ID"
python3 -B "$SCRIPT" preserved "${COMMON[@]}" --baseline "$BAD_SOURCE_BASELINE"
```

通过条件：坏来源确实不可达、仍启用、配置散列不变，内置测试来源仍完成实际公网自检。脚本不会接受通过静默禁用/删除坏来源获得的绿色结果。

### 5.4 示例开关与业务来源互不影响

由总控通过新版界面关闭“内置测试服务”，不用停止连接器或 FRPC：

```bash
python3 -B "$SCRIPT" demo-state "${COMMON[@]}" --expected-demo-state off
python3 -B "$SCRIPT" preserved "${COMMON[@]}" --baseline "$BASELINE_WITH_BUSINESS"
```

随后使用事先已验证的独立业务来源执行一次真实只读查询，确认其仍可调用；只看来源 enabled 标志不足以证明链路连续。再通过界面重新启用示例：

```bash
python3 -B "$SCRIPT" demo-state "${COMMON[@]}" --expected-demo-state on
python3 -B "$SCRIPT" local-probe "${COMMON[@]}"
python3 -B "$SCRIPT" self-test "${COMMON[@]}" --allow-self-test
python3 -B "$SCRIPT" preserved "${COMMON[@]}" --baseline "$BASELINE_WITH_BUSINESS"
```

恢复后示例 source_id 应不变。示例 instance_id 可以因进程/服务重启改变，不应把它当成永久设备身份。

## 6. Windows：精确安装包获准后执行

### 6.1 安装前保全

由总控把两份 Python 脚本复制到新的验收目录，例如 `D:\KaiwuAcceptance\builtin-demo-1.3.8-<短提交>\scripts\`。该目录是建议路径，须以实际复制结果为准；本轮未访问或改动 Windows。验证程序使用测试机已有 Python，客户安装包不因此依赖 Python。

```powershell
$Python = 'D:\CodeLanguage\Python\Python3.12.4\python.exe'
$Script = Join-Path $AcceptanceDir 'scripts\builtin_demo_acceptance_138.py'
$Helper = Join-Path $AcceptanceDir 'scripts\windows_diagnostics_acceptance_137.py'
$Common = @('--local-url', 'http://127.0.0.1:17872',
            '--evidence-dir', (Join-Path $AcceptanceDir 'evidence'),
            '--helper', $Helper)
& $Python -B $Script snapshot @Common --expected-version 1.3.6
```

若现场版本已变，先记录真实状态并由总控决定正确基线，不能只改 expected-version 来掩盖未知升级。记住生成的唯一 snapshot 路径。核对已有业务来源、企业绑定和凭据仍正常。

### 6.2 正常安装及自动启动

获准后运行指定散列对应的正式图形化安装包，完成一次正常管理员确认。不得用绕过审批的临时任务、便携启动或另一个未核对安装包代替。记录安装完成界面、版本和服务真实启动路径。

先核对已安装 exe SHA-256 与构建产物；核对 KaiwuConnector 服务/进程及本机界面属于这个安装，不是另开的旧进程。用 netstat/Get-NetTCPConnection 等只读方式确认内置 MCP 动态端口属于该进程。随后立即运行：

```powershell
& $Python -B $Script local-probe @Common
& $Python -B $Script preserved @Common --baseline $PreUpgradeSnapshot
& $Python -B $Script self-test @Common --allow-self-test
& $Python -B $Script public-probe @Common --private-mcp-json $TestDemoJson
```

第一次 local-probe 必须在按“连接自检”前执行，避免自检帮忙启动服务后掩盖安装未自动启动的问题。不能先启动旧 28765 mock 再声称新包已内置服务。

原有业务来源的独立回归可复用旧脚本已存在的 `public-success --verify-logs` 模式及总控已验证的旧 handoff 目录；具体旧目录由总控核对，不能推测或打印其中私有 JSON。**不要原样执行旧 postcheck**，它把旧来源 ID 列表当作精确集合，新增独立示例来源会被误判。

### 6.3 必须单列的发布门槛

- 允许一次 SCM 服务重启后，在不运行验收辅助 MCP、不打开调试终端的情况下再次 local-probe 和 self-test，证明服务生命周期会自动恢复示例。此操作须由总控在允许的窗口实施，脚本不发起重启。
- 通过新版界面执行示例 off/on，运行 demo-state、preserved，且原有业务来源真实查询始终有效。
- GUI 截图至少覆盖：版本、内置测试服务状态、分阶段连接自检结果、查看/复制开物 JSON 入口。截图不得出现接入码、集成凭据、客户原始数据。
- 在开物真实页面导入示例 MCP JSON，通过实际智能体对话问：“请用内置测试服务查询订单数和 KAIWU-DEMO-001 的库存。”确认工具调用存在，答复明确为示例订单 12、示例库存 128；不能把固定文案或 API 探针当作真实智能体调用。
- 连接器和开物日志按同一 trace 可查询，未泄露密钥/请求头/业务数据；SCM 方式也能启动诊断上传。
- 记录 Shervin 实际 Windows 版本及架构。Windows Server 目标版本的兼容性证据单独列出；没有目标系统实测时明确标“未实测”。

## 7. 失败判定与交付限制

| 观察结果 | 能得出的结论 |
| --- | --- |
| local-probe 失败 | 先排查安装、进程归属、本机内置服务或 MCP 协议；不能让客户先改自己服务 |
| local-probe 成功，公网自检失败 | 结合失败阶段与日志排查绑定、隧道、Edge、部署或访问校验 |
| 公网内置示例通过，客户来源失败 | 公共示例链路已获验证；再定位该客户来源的地址、协议、权限、工具配置，不能直接断言客户一定配置错误 |
| 公网探针通过，开物对话失败 | 继续排查开物 MCP 导入、工具选择、智能体调用；未达到最终业务验收 |
| 任一“passed”证据 | 只覆盖该 case；不自动证明安装、SCM、Windows Server、日志和开物对话全部合格 |

当前仍需总控收齐的证据：冻结候选/安装包散列、云端新路由实际部署结果、隔离租户实际调用、获准的 Windows 安装与 SCM 回归、业务来源保全、公网 trace 日志、开物真实对话、目标 Windows Server 兼容性。本文不提前把这些项目标绿。
