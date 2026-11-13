# OC-T3 文件工具、子任务与插件治理

日期：2026-11-12（Asia/Shanghai）

任务节点：`OC-T3`

颜色组：橙色“文件工具、子任务与插件”

执行角色：橙色-项目子负责人

隔离分支：`worktree-oc-t3-file-subagent-plugin`

## 1. 本次交付范围

本节点在受控前提下补齐三组能力：

1. **会话私有文件工具**：`file_read`、`file_write`、`file_search`。文件只能落在当前会话的 `workbench` 目录；读、写、搜索都经过运行时的工具授权检查，写入必须携带读到的版本号并经用户确认；写入使用临时文件替换、数据库版本事务和失败回滚。
2. **子任务生命周期隔离**：每个子任务拥有独立的工作区目录和记忆目录；并发数受应用会话上限约束；父任务取消会级联取消全部未完成子任务；单个子任务失败不拖垮同批其他子任务；服务重启后未完成子任务标记为 `interrupted`，不重放任务输入。
3. **插件治理**：新增管理员插件注册表，登记固定版本、HTTPS 来源、许可证、安装包 SHA-256 和申请权限；管理员在界面中逐项审核后批准或撤销；登记、批准、撤销全部写入审计事件。

同时新增客户端“插件治理”页面，使上述插件治理在管理员界面中可操作。

## 2. 实现位置

| 能力 | 位置 |
|---|---|
| 文件工具、版本历史、路径边界 | `server/app/file_tools.py` |
| 工具注册与 MCP 接入 | `server/app/capabilities.py` |
| 文件工具授权与路径预检 | `server/app/service.py` |
| 文件工具风险等级 | `server/app/security.py` |
| 插件注册表、审批、撤权、审计 | `server/app/plugins.py` |
| 插件 API 路由 | `server/app/main.py` |
| 文件版本和插件数据表 | `server/app/db.py` |
| 子任务工作区/记忆目录隔离、并发与恢复 | `server/app/subagents.py` |
| 客户端插件治理页面 | `client/src/PluginsPage.tsx` |
| 导航和类型 | `client/src/App.tsx`、`client/src/types.ts` |

## 3. 验收命令与结果

### C1 自动化测试

工作目录：`server`

命令：

```bash
pytest -q tests/test_file_tools.py tests/test_subagent.py
```

结果见第 4 节“实际运行记录”。测试环境为项目 `.venv`（Python 3.10.12），没有调用真实模型、生产凭据、客户数据或共享服务。

### C2 子任务高风险审批级联、父取消和失败隔离

`tests/test_subagent.py` 覆盖以下场景：

- 高风险子任务继承父任务双人审批策略：两名管理员分别投票后才允许执行。
- 父任务没有高风险双人审批策略时，子任务默认拒绝并写入策略拒绝原因。
- 父任务取消时，正在等待审批的子任务立即结束，审批单转为过期，来源为 `parent_cancelled`。
- 最终投票后父任务审批策略被撤销时，子任务重新校验并安全拒绝。
- 单个子任务异常不会影响同批其他子任务完成。
- 并发子任务数量不超过应用会话上限，并发峰值有断言。
- 同一父任务重复提交完全相同的计划是幂等的；提交不同计划返回 `SUBAGENT_PLAN_EXISTS`。
- 服务重启后的恢复路径把未完成子任务标记为 `interrupted`，不会重放任务内容。

### C3 插件安装、版本、许可证和权限审核界面

管理员导航新增“插件治理”页面，支持：

- 登记插件：名称、固定版本、HTTPS 来源、许可证、安装包 SHA-256、声明权限和审核说明。
- 版本不可篡改：同名同版本不能更换摘要，必须发布新版本。
- 许可证审核：批准时服务端只接受 MIT、Apache-2.0、BSD-2-Clause、BSD-3-Clause、ISC 等宽松许可证。
- 权限审核：界面逐项展示七类权限的中文说明；服务端只接受白名单内的权限标识。
- 批准和撤销：批准与撤销都必须填写审核说明，并写入不可变审计记录。
- 非管理员不能读取、登记、批准或撤销插件。

## 4. 如何重复

任务书中的 `pytest` 指项目虚拟环境里的 pytest。复核时请在 `server` 目录使用：

```bash
../.venv/bin/python -m pytest -q tests/test_file_tools.py tests/test_subagent.py
../.venv/bin/python -m pytest -q tests/test_plugins.py tests/test_capabilities_04.py
```

客户端：

```bash
cd ../client
npx tsc -b
npx vitest run
npx vite build
```

## 5. 实际运行记录

运行日期：2026-11-12。共享服务器同时有其他团队任务在跑，以下耗时只用于说明本次实际执行，不作为性能结论。

### 5.1 验收命令 C1

```bash
cd server
../.venv/bin/python -m pytest -q tests/test_file_tools.py tests/test_subagent.py
```

结果：`14 passed, 1 warning in 232.31s`；退出码 0。其中文件工具 5 项、子任务 9 项。

原始日志：`docs/evidence/oc_t3_c1_targeted.log`

唯一警告来自 FastAPI `TestClient` 对当前 httpx/Starlette 组合的弃用提示，不影响测试结论。

### 5.2 插件与既有能力回归

```bash
cd server
../.venv/bin/python -m pytest -q tests/test_plugins.py tests/test_capabilities_04.py
```

结果：`5 passed, 1 warning in 63.34s`；退出码 0。其中插件治理 3 项，既有工作区能力 2 项。

原始日志：`docs/evidence/oc_t3_plugins_capabilities.log`

### 5.3 相关服务端回归

```bash
cd server
../.venv/bin/python -m pytest -q tests/test_api.py tests/test_high_risk_approval.py tests/test_web_tools.py tests/test_session_isolation_042.py
```

结果：`54 passed, 1 warning in 420.05s`；退出码 0。原始日志：`docs/evidence/oc_t3_regression.log`。

### 5.4 客户端

```bash
cd client
npx tsc -b
npx vitest run
npx vite build
```

结果：TypeScript 检查退出码 0；Vitest 18 个测试文件、112 项全部通过；生产构建成功（`built in 11.43s`）。

原始日志：`docs/evidence/oc_t3_client.log`、`docs/evidence/oc_t3_client_build.log`

## 6. 未覆盖边界

- 子任务执行器仍是确定性的本地合成执行器，不调用真实模型、网络、MCP 连接或客户数据。因此本节点证明的是隔离、并发、取消、失败和恢复合同，不是真实模型子任务的质量。
- 插件注册表只治理来源、版本、许可证、摘要和权限声明，不下载或执行第三方代码；运行时也尚未加载插件包。批准不等于完成第三方代码安全审计。
- 本轮没有部署、重启共享服务、切换默认检索或访问客户数据。
- 文件工具只处理 UTF-8 文本，单文件上限 512 KiB，读取返回上限 200000 字符；二进制文件由上层继续使用受控附件能力。
