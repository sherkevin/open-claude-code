# 华微 1.3.8 服务端内置测试 MCP 自检报告

- 项目：华微连接器 1.3.8
- 任务基线：`c55c6b3e1d22dd14e080cfaf2da1fc6ef89259a7`
- 生成时间：`2026-11-19T10:05:07Z`
- 服务端范围：`server/` 与 `docs/evidence/huawi-builtin-demo-20261119/SERVER_REPORT.md`

## 背景与目标

华微连接器安装在 Windows 客户内网，只能主动出网，需要借助现有 FRP、公网 Edge 和云
端控制面访问连接器内的 MCP 服务。客户在接入自己的业务 MCP 之前，要先使用随包内置
的示例 MCP 验证“本机示例服务、企业绑定、隧道、公网 Edge、MCP 协议”整条链路，以便
快速区分客户业务配置问题与我方平台链路问题。

本服务端增量新增设备签名鉴权的云端自检入口：

```text
POST /api/kaiwu/v1/connectors/{connector_id}/self-test
```

请求只接受 `source_id` 和 `nonce`。服务端通过受信配置构造 Edge 地址，执行
`initialize`、`notifications/initialized`、`tools/list` 和固定的
`kaiwu_demo_ping(nonce)`，再校验 nonce、固定标记、UTC 时间和进程启动标识。云端不会
伪造成功结果，也不接受用户提供的 URL、主机、工具名、工具参数、租户或密钥。

## 已完成的修正

### 固定示例来源身份

内置示例来源 ID 只按共同接口契约生成：

```python
"src_" + hashlib.sha256(
    ("kaiwu_builtin_demo_v1:" + connector_id).encode("utf-8")
).hexdigest()[:32]
```

服务端基于已认证设备对应的 `connector_id` 重新计算该值，并强制请求中的 `source_id`
完全匹配。来源必须属于当前连接器和当前企业，`allowed_tools` 必须恰好是以下三个固定
工具：

- `kaiwu_demo_ping`
- `kaiwu_demo_order_summary`
- `kaiwu_demo_inventory_query`

`display_name` 只作为展示字段，不再作为可信身份。测试中把正确来源的显示名称改成其
他文字后自检仍然成功；把错误来源伪装成示例显示名称则被拒绝。

### 临时集成凭据限制

服务端创建只在当前请求内存中持有的临时集成凭据 K，数据库只保存摘要，不返回浏览器
或客户端。临时集成记录使用服务器可信生成的
`created_by=device-self-test:<device_key_id>` 标记。Edge 在真实鉴权路由中识别该标记，
并对临时 K 强制应用以下限制：

- 允许 `initialize`、`notifications/initialized` 和 `tools/list`；
- `tools/call` 只允许 `kaiwu_demo_ping`；
- 该工具的参数只允许一个 `nonce` 字符串；
- 批量 JSON-RPC 中只要出现任意其他方法、工具或参数，整个请求在转发到 FRP/上游前被拒绝；
- 普通客户集成 K 不受这些临时标记规则影响。

回归测试直接使用 Edge 的真实 FastAPI 路由和真实集成鉴权函数，没有把
`_authenticated_integration` mock 掉。

### 自检专用公网 HTTPS 约束

自检使用独立的 `_self_test_edge_base`，不继承旧的
`_edge_public_base` 在 `http://localhost`、`http://testserver` 等本机测试场景下的例外。
自检地址必须来自受信配置 `AGENT_KAIWU_EDGE_PUBLIC_BASE`，并且满足：

- scheme 必须是 `https`；
- 不允许 `localhost`、`testserver` 或以这些名称结尾的主机；
- 如果主机是 IP 字面量，必须是公开可路由地址；
- 不允许用户名、密码、查询串或片段；
- 路径必须与受信配置的 Edge 挂载路径一致。

生产代码仍通过 `httpx` 请求受信 Edge，并禁止环境代理和重定向。单元测试使用受控
`httpx.ASGITransport` 驱动真实 Edge 路由，再由本机合成的 MCP HTTP 上游响应。这个
测试证明本地代码路径和鉴权逻辑，不证明真实公网 TLS、FRP 或 Windows 环境已经验收。

### 清理与诊断

`finally` 只删除本次请求创建的临时 `kaiwu_integrations` 记录和
`kaiwu_integration_sources` 映射。清理结果写入现有
`kaiwu_control_audit`，固定 action 为 `integration.self_test_cleanup`：

- 清理成功：`outcome=allow`，`detail.result=succeeded`；
- 清理失败：`outcome=deny`，`detail.result=failed`；
- 记录 `trace_id`、`source_id` 和固定工具名；
- 不记录临时 K、Authorization、MCP 请求正文、参数或客户业务数据。

清理成功不等于 MCP 诊断通过，因此不会写入 `diagnosis_succeeded` 或其他 MCP 检查
结论。没有新建第二套审计系统，也没有修改或撤销任何现有客户集成 K。

## 验证结果

已执行以下测试，使用 `data3` 下的临时数据库和缓存：

```text
cd /media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118
TMPDIR=/media/data3/dengkw/tmp \
PYTHONPATH=/media/data3/dengkw/scratch/tmp/rootfs-archive-20261112/open-claude-pylib \
python3.10 -m pytest -q \
  server/tests/test_kaiwu_control.py \
  server/tests/test_kaiwu_enterprise.py \
  server/tests/test_kaiwu_bridge.py \
  server/tests/test_kaiwu_diagnostics.py \
  server/tests/test_kaiwu_self_test.py \
  -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/tmp/pytest-kaiwu-self-test-all-current
```

结果：`34 passed, 1 warning in 90.76s`。唯一警告是现有 Starlette/httpx 测试客户端
弃用提示，不是本次功能失败。

完成清理日志修正后，相关自测文件单独回归：

```text
TMPDIR=/media/data3/dengkw/tmp \
PYTHONPATH=/media/data3/dengkw/scratch/tmp/rootfs-archive-20261112/open-claude-pylib \
python3.10 -m pytest -q server/tests/test_kaiwu_self_test.py \
  -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/tmp/pytest-kaiwu-self-test-cleanup-audit
```

结果：`9 passed, 1 warning in 27.78s`。

新增和修正的测试覆盖：

- 固定 `source_id` 算法、企业/连接器归属和三个固定工具白名单；
- 显示名称不可作为可信身份；
- 额外 URL 字段、未知来源、错误企业、错误连接器和额外工具参数均被拒绝；
- 受控 HTTPS transport 下的成功握手、nonce 回显、UTC 时间、trace 和清理；
- 临时 K 的 initialize、tools/list 可用，其他工具、额外参数和批量越权调用被 Edge 拒绝；
- HTTP 本机地址、HTTPS 回环地址和其他本机测试地址不能冒充公网 HTTPS 自检；
- Edge HTTP 失败、JSON-RPC 工具错误和 nonce 不匹配产生分层失败，并且临时记录仍被清理；
- Edge 失败但临时 K 清理成功时，最终自检仍为失败，清理审计为成功，且
  `diagnosis_succeeded` 和 `diagnosis_failed` 数量都不增加。

## 未执行事项与准确边界

- 本轮没有部署腾讯云；云端生产环境未更新。
- 本轮没有安装、启动或验收 Windows 连接器；Windows 安装器和 Windows 服务未验证。
- 之前 1.3.7 的 Windows 验收仍未完成，本轮不能把它写成已完成。
- 没有使用 Shervin 或其他 Windows 远程设备，没有修改 Shervin。
- 没有执行真实公网 HTTPS Edge、真实 FRP 隧道或真实 Windows 内置 MCP 的跨网络验收；
  上述单元测试中的 HTTPS 传输是受控模拟，本机 HTTP 上游只是合成测试服务。
- 没有执行开物真实智能体对话，不能把 Edge MCP 往返写成开物对话成功。
- 没有构建安装包、没有提交 Git、没有打包、没有使用 `git add .`。
- 没有签发或兑换客户接入码，没有修改 `connector/`。

## 变更文件

- `server/app/kaiwu_control.py`
- `server/app/kaiwu_edge.py`
- `server/tests/test_kaiwu_self_test.py`
- `docs/evidence/huawi-builtin-demo-20261119/SERVER_REPORT.md`
