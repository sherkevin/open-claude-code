# Harness 接入基础验证（2026-11-10，学校云）

## 范围

- 修改旧 SDK 的工具包装层，业务工具移到框架无关注册表；不改变对话运行时选择。
- 新增默认关闭、仅进程内使用的知识工具桥；没有 HTTP 端点或真实 Harness 进程。
- 固定官方源码并运行官方 Python 客户端的模拟对端协议验证。
- 未重启本地或生产服务，未部署、发布、使用真实客户资料或调用付费模型。

设计：`docs/design/DEEPSEEK_HARNESS_MIGRATION.md`。计划：`docs/plans/2026-11-10-deepseek-harness-foundation.md`。源码版本与许可证：`docs/upstream-sources.json`。

## 开发与独立检查

1. 新桥测试先以模块未实现失败，随后实现默认拒绝和真实知识服务调用。
2. 独立审查发现初版遗漏双人策略判断，已在读取前后加上真实 `requires_dual()`，并补调用前/读取期间启用真实策略的测试。
3. 独立审查发现取消缺终态及审计异常可能泄露错误类型，已补取消传播、固定错误和不返回内容的测试。
4. 初轮测试中的两条跨身份夹具直接写入不存在的外键而失败，已改成创建真实第二用户和第二会话；没有关闭数据库外键检查。
5. 二次只读检查确认当前默认关闭的进程内试点范围内无剩余阻塞项；真实插件与进程隔离仍未验收。

## 可重复验证命令

所有服务端命令在 `server/` 执行，缓存和临时目录均位于数据盘：

```bash
export PYTHONPATH=/tmp/open-claude-pylib:.
export PYTHONPYCACHEPREFIX=/media/data3/dengkw/.tmp/open-claude-pycache
export TMPDIR=/media/data3/dengkw/.tmp
python3 -m pytest -q -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/.tmp/harness-targeted-repeat \
  tests/test_harness_bridge.py tests/test_workspace_tools.py \
  tests/test_capabilities_04.py tests/test_harness_protocol_smoke.py
python3 -m pytest -q -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/.tmp/harness-full-repeat
```

项目根目录的协议检查：

```bash
PYTHONPATH=/tmp/open-claude-pylib python3 -B scripts/verify_harness_sdk_protocol.py
```

客户端目录：`npm test -- --run`、`./node_modules/.bin/tsc -b`。没有运行 Vite 构建或部署命令。

## 已取得的证据

- 原工具与注册表：19 项通过，包含原 3 项及新增 16 项契约测试。
- 官方客户端协议：8 项 PASS；固定源码、初始化、字面量消息、审批拒绝、未知请求拒绝、请求并发、会话过滤和关闭。报告为 `.runtime/verification/harness-protocol-20261110.json`，明确 `fake-runtime-only`、零模型调用、未启动真实 Harness。
- 客户端：18 个文件、117 项通过；TypeScript 检查通过。
- 服务可达性：现有 `http://127.0.0.1:18086/login` 返回 200；`/api/v1/health/ready` 返回 `status=ready, model_configured=true`。这是登录页和配置就绪检查，不是一次真实模型对话验收。
- 服务端最终定向回归：桥接器、注册表、旧 SDK 包装及离线协议共 66 项通过，耗时 434.21 秒；原始日志 `.runtime/verification/harness-targeted-20261110.log`。包含修正后的外键夹具、真实双人策略、取消、审计故障和真实配置指纹变化。
- 服务端初轮全量回归：508 项通过、2 项失败，共 510 项，耗时 1728.78 秒；原始日志 `.runtime/verification/harness-full-20261110.log`。仅失败 `test_revocation_and_binding_changes_fail_closed[run_owner]` 和 `[run_conversation]`，均为该进程收集到的旧外键夹具；最终定向复跑已覆盖修复后的两项。
- 最终覆盖关系：全量中的 472 项非桥接测试通过；最终桥接测试 44 项全部通过（包含在上述 66 项定向结果中），合并覆盖当前收集到的 516 项服务端测试，没有未解决失败。修复后没有重新启动第二次单进程全量回归；不能把第一次全量日志表述为零失败。
- 两次服务端执行均仅有第三方 Starlette 关于测试客户端使用 httpx 的弃用警告，没有因此升级或替换依赖。

## 证据边界

- 没有验证真实 Node Harness、模型路由、原生工具、插件安装或流式前端闭环；桥目前不被 `RunManager` 选用。
- 真实权限、文档和来源测试使用临时 SQLite、合成资料、本地词法向量；模型凭据边界用替身隔离。
- 官方 HippoRAG 只有固定源码副本，尚未安装官方依赖或完成 OpenIE/query-to-triple 等完整检索链路。
- 现有部分实现需求不整体升级；本轮完成的是框架无关工具切片和离线安全接入基础。
