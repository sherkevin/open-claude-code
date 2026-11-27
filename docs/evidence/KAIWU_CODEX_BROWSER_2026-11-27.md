# 从开物会话发送、查看和取消 Codex 工程任务

开物的会话工具栏现有“Codex 工程任务”按钮。用户能准备会话、显式发送、查看上一轮结果和取消当前轮次。入口跟随服务下发的 `codex_appserver` 能力位；试用、归档或正被普通运行占用的会话保留原有限制。验收环境为真实 Chromium、开物应用和固定 Codex 0.165.0，模型应答来自同一隔离网络中的测试服务。

| 浏览器操作 | 原界面 b168171 | 本次隔离浏览器实测 |
|---|---|---|
| 登录并打开已有业务会话 | 后端已声明能力、本人状态接口 200，但无操作入口 | 功能开启时会话工具栏出现按钮；点击准备会话后才允许发送 |
| 提交第一条任务 | 无可点击的发送入口 | 获得完成状态和正文 `KAIWU_LOOPBACK_OK` |
| 刷新页面并重新打开同一会话的工具 | 无结果查看入口 | 看到原正文，仅通过 GET 取回状态，无重复发送 |
| 发送第二条任务并在等待响应时刷新 | 无可点击的取消入口 | 重新打开弹窗后仍可取消活动轮次，最终展示“已取消” |
| 默认关闭时打开会话 | 无按钮 | 无按钮；直接查询后端接口返回 404，模型侧收件 0 次 |

两轮操作包括两次整页刷新。合成模型服务收到 2 次请求，页面未抛出运行时错误。页面使用原有登录表单、会话、弹窗和本人授权的 API。准备、取消和重取结果不会触发模型请求。普通聊天的输入框和消息历史仍属于原运行链；Codex 试点在其会话弹窗内显示最近回执。

## 复现

VIP 证据目录为 `/media/data3/dengkw/acceptance/kaiwu-l3-codex-browser-20261127`。其中 `server.py` 启动仅监听回环的开物服务，`journey.mjs` 驱动真实 Chromium。通过表单登录；合成助手及空会话使用产品 API 创建作为前置数据。测试使用真实编译产物及 `/project/kaiwu/` 部署前缀。bubblewrap 隔离网络、进程、HOME 和工作区，网络仅有 lo；固定摘要校验、独立测试提供方和只读执行条件同 [原生轮次实测](KAIWU_CODEX_NATIVE_TURN_2026-11-27.md)。没有接入外部模型或客户凭据。

```bash
D=/media/data3/dengkw/acceptance/kaiwu-l3-codex-browser-20261127
T=/media/data3/dengkw/projects/kaiwu-l3-codex-ui-20261127
NODE_DIR=/media/data3/dengkw/projects/open-claude-code/.runtime/harness-toolchain/node-v24.9.0-linux-x64/bin
(cd "$T/client" && PATH="$NODE_DIR:$PATH" BASE_PATH=/project/kaiwu npm run build)
KAIWU_TEST_TREE="$T" KAIWU_BROWSER_DIST="$T/client/dist" bash "$D/run.sh"
```

| 原始回执 | 结果和边界 |
|---|---|
| `browser-before.log/.exit` | 旧前端以原生轮次候选 b168171 后端启动。能力已开、API 200、Chromium 无入口，退出 0。复用了 6d040ea 的浏览器产物，两个提交的 client 树相同 |
| `build-final.log/.exit` | TypeScript 和 Vite 生产前缀构建退出 0 |
| `browser-final.log/.exit` | 同一构建的开、关两个配置均通过，退出 0。该日志首行为对应的隔离目录，内含 completed.png、running-after-refresh.png、cancelled.png |
| `pytest.log/.exit` | Codex 宿主和产品 API 专项 24 项通过，32.21 秒，1 条既有弃用警告，退出 0。包括本人有界正文、同进程精确中断能力，以及另一宿主不能声明可中断 |

刷新从已持久化的最近回执取正文。若原生已完成而宿主在保存正文前退出，界面只展示已核实的终态和正文缺失提示。显式核对会调用原有 `/reconcile`；参见原生轮次实测中的服务崩溃样本。本次浏览器没有制造崩溃或触发工具审批。生产环境、真实模型授权、业务工具执行和统一消息历史需分别验收。
