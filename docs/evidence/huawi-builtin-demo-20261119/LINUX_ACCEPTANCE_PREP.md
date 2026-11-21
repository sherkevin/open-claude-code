# Linux 1.3.8 冻结产物验收准备（仅本机）

**当前状态：只准备与 AST 语法检查；未启动候选、未读取管理员 token 文件、未创建/绑定企业、未启停服务、未部署。** 本轮总控只执行 `--local-only`。此前“等待新 bridge 自动预配”的说法无实际依据，已撤回。

## 1. 已只读核实的公网限制

2026-11-19 从腾讯云实际模板、非敏感配置字段和已部署源码核实：

| 项目 | 实际值/行为 |
| --- | --- |
| systemd 模板 | `/etc/systemd/system/kaiwu-bridge@.service` |
| EnvironmentFile | `/data/dengkw/workspace/kaiwu-enterprise-bridge-20261119/environment`，所有 instance 共用，没有按 %i 的独立 env |
| frps 监听 | `AGENT_KAIWU_CONTROL_RELAY_BIND_ADDRESS=127.0.0.1`；`AGENT_KAIWU_CONTROL_RELAY_BIND_PORT=37443` |
| 对外 relay | `flintw.com:443`，wss；当前单一公共 relay 配置 |
| instance 差异 | ExecStart 只按 `--connector-id %i` 和 `--runtime-dir .../runtime/%i` 区分 |
| 认证 | relay token 按 connector_id 派生；不同 connector 的 token 不同 |
| 配置生成 | `app.kaiwu_bridge.BridgeSupervisor._write_configs()` 自行写 frps.toml/visitor.toml；已有 supervisor 无需另写配置生成脚本 |
| 生命周期 | 旧 PublicDriver 是显式 systemctl start 精确 instance；没有观察到自动预配 unit/timer |

因此，**当前 template 不能直接同时启动两个 connector 的 bridge**：新 instance 仍争抢 127.0.0.1:37443，且无法用另一个 connector 的派生 token 登录既有专属 frps。仅新建 tenant 或更换 runtime 目录不解决问题。本任务不改多租户 relay，不停 Shervin 现有专属通道。

编排脚本的公共模式现在会在任何创建 tenant/绑定/启动候选前，先以 SSH **只读**检查活动/正在启动的 `kaiwu-bridge@*.service`。发现任何活动 bridge 即 `PUBLIC_RELAY_ALREADY_OWNED_BY_ACTIVE_BRIDGE`；无法核实也拒绝继续。即使没有活动 instance，本交付仍以 `PUBLIC_NEW_TENANT_RELAY_BOOTSTRAP_NOT_ENABLED` 停止，防止误把“空闲”当成已具备新的公网启动方案。

未加入 `--provision-new-test-bridge`，未加入任何 systemctl start/stop/restart。旧保活/cleanup 代码仅保留供审查既有 receipt；本轮不走该分支。真实公网测试留待 1.3.8 获准在 Shervin 安装后复用现有测试专属通道，不能用 Linux 新 tenant 绕过 Windows 安装审批。

## 2. 唯一执行模板

只需 ROOT_BUILD 冻结的 Linux 连接器路径、SHA-256 和源码提交。**不需要 FRPC、SSH、管理员 token、接入码或云端身份。** 工作目录必须全新、位于下述 acceptance 根，且含源码短提交。

```bash
REPO=/media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118
DRIVER="$REPO/docs/evidence/huawi-builtin-demo-20261119/run_linux_builtin138_acceptance.py"

# SOURCE_COMMIT、CONNECTOR_BIN、CONNECTOR_SHA256 从 ROOT_BUILD 冻结记录读取。
WORK_DIR="/media/data3/dengkw/acceptance/huawi-demo138-${SOURCE_COMMIT:0:7}-$(date -u +%Y%m%dT%H%M%SZ)"

python3 -B "$DRIVER" --local-only --execute \
  --work-dir "$WORK_DIR" --source-commit "$SOURCE_COMMIT" \
  --connector "$CONNECTOR_BIN" --connector-sha256 "$CONNECTOR_SHA256"
```

不加 `--execute` 只输出 prepared_only。local-only 拒绝与 `--keep-running-for-ui` 同用。启动时只给 `serve --data-dir ... --listen 127.0.0.1:<独立端口>`，清除子进程继承的 KAIWU_CLOUD_BASE/KAIWU_EDGE_BASE/管理员令牌和代理变量；TMPDIR/TMP/TEMP/XDG_CACHE_HOME 均落在本次 data3 目录。

## 3. 实际验收顺序

1. 校验冻结二进制 SHA-256，创建全新 0700 工作目录和独立进程组，启动冻结连接器。
2. 在按自检或添加来源之前，复用 `local-probe --expect-unbound`，证明内置 MCP 自动启动；调用其动态端口的三个工具，验证新 nonce、marker、真实时间、订单 **12** 和 `KAIWU-DEMO-001` 库存 **128**。不启动任何旧 mock。
3. 通过本机 `POST /api/source/save` 追加一个故意不可达、仍启用的合成来源。端口由“已 bind、未 listen”的 socket 保留，TCP 连接失败；随机合成 Bearer 只存本轮 data 目录。
4. 保存 snapshot，记录独立业务来源配置/认证元数据散列，以及本轮 secrets.json/auth_profiles.json 的完整字节散列和 0600 权限。
5. `POST /api/builtin-demo/enabled {"enabled":false}` → demo-state off → preserved；重新启用 → demo-state on → local-probe → preserved。每一步保留坏来源及秘密文件原样，不修改产品源码。
6. 调用本机 `POST /api/self-test {}`，核对未绑定行为：local_mcp=passed；enterprise_binding、tunnel、edge_connect=skipped；整体 ok=false、无公网 trace；验证本机 nonce 回包。**enterprise_binding 当前是 skipped，不是 failed**，不使用 `--expect-failure-stage enterprise_binding` 冒充检查成功。
7. 再次确认 bound=false、frpc_running=false、tunnel_connected=false、坏来源仍启用且不可达、配置和秘密文件散列未改变。
8. finally 只终止本次启动的 Linux 进程组，确认无活动成员；保留证据目录，不操作任何已有连接器、远端 bridge 或系统服务。

## 4. 结果与边界

主结果：`$WORK_DIR/acceptance-result.json`；分项结果在 `evidence/`；受限候选日志在 `private/connector.log`。所有新目录 0700，文件受 0077 umask 限制，秘密文件明确要求 0600。脚本不打印原始 MCP 回包、认证参数或异常全文。

结果明确标注 `local_only:true` 与 `public_chain_executed:false`。整体 passed 仅代表本机安装后自动启动行为的 Linux 参考、示例启停、本机 MCP 协议、未绑定提示、合成业务配置保全及自身清理通过；不代表公网链路、开物真实对话、Windows 安装/SCM、客户 Windows Server 或真实业务来源已通过。

复用文件：`builtin_demo_acceptance_138.py` 和 `../huawi-diagnostics-20261119/windows_diagnostics_acceptance_137.py`。源码协议验收维持原实现；本次只给总控补最小编排。冻结后若 self-test 阶段语义变化，应先对照源码调整断言，不能把未知结果默认放行。
