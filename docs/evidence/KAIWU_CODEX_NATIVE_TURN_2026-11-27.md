# 开物会话在固定 Codex 上完成首轮并取消第二轮

在 VIP 的隔离产品 API 中，空会话可以关闭原生进程、继续第一轮、重取相同回执，再取消正在等待的第二轮。回环服务返回固定合成文本。

| 用户操作 | 原主线 6d040ea | E2.3 隔离验收 |
|---|---|---|
| 登录、创建会话，POST 会话下的 `/codex/thread` | 200；关闭初始化进程后没有原生会话记录文件 | 200；写入原生会话名后再向用户确认准备完成 |
| POST `/codex/turn` 发送第一条消息 | 422 `CODEX_REQUEST_REJECTED`；回环服务收件 0 次 | 200，原 thread_id，状态 completed，正文 `KAIWU_LOOPBACK_OK` |
| 用相同请求标识和正文重试 | 基线首轮未开始 | 200，原 turn_id、原文、replayed=true；回环累计收件仍为 1 次 |
| 重新打开会话，发送第二条消息 | 未执行 | 新原生进程恢复同一 thread_id，第二轮的 turn_id 与第一轮不同 |
| 在第二轮等待响应时取消 | 未执行 | 传上一轮 ID 得到 409；传活动轮次 ID 得到 200，发送请求最终返回 interrupted |
| 查询终态与归属 | 未执行 | `/codex/reconcile` 返回该已中断轮次且无需恢复。另一位登录用户查询原会话得到 404。两轮共 2 次回环收件 |

## 实测条件

用户入口是隔离运行的开物 FastAPI 应用。通过现有 TestClient 的 HTTP 适配访问同一登录、会话和 Codex 路由，服务显式注入真实 CodexHost，未使用浏览器。模型侧为本地 `/v1/responses` 的 HTTP/SSE 固定应答服务，fixture 仅为测试标识。实际启动的是固定 codex-cli 0.165.0，SHA-256 为 `c06a67fd8ac76508dc78bd99ce20c9f317b8d568beda59485f3f4134df9962d5`。每次启动都检查摘要。bubblewrap 创建独立网络、PID 和 IPC 命名空间，清空继承环境。网络接口仅 lo，零条 IPv4 路由；模型服务监听该命名空间内的 127.0.0.1 临时端口。测试使用全新的 data3 临时 HOME、产品数据库和工作区，服务代码、依赖和二进制只读挂载。

真实空线程在 `thread/start` 成功后尚未写入可恢复记录。初始产品代码此时关闭原生进程、保存线程 ID，下一次用户请求走 `thread/resume` 失败。在启动进程内提交一个固定、无业务内容的 `thread/name/set` 后，宿主才保存绑定。若元数据写入失败，服务拒绝本次准备、不会宣告会话已就绪。既有已绑定会话沿原 ID 恢复。基线曾报告成功但原生记录已经缺失的旧空会话，需要新开会话。

## 复跑和原始回执

在 VIP 执行下列命令。运行器在每次实测时创建独立目录，完整请求脚本位于同目录的 `probe.py`。替换 T 可对比其他待验收检出。

```bash
T=/media/data3/dengkw/projects/kaiwu-l3-codex-env-20261127
D=/media/data3/dengkw/acceptance/kaiwu-codex-real-env-l3-20261127
P=/media/data3/dengkw/projects/open-claude-code/.venv/bin/python
KAIWU_TEST_TREE="$T" bash "$D/run.sh"
cd "$T/server"
TMPDIR="$D/tmp" "$P" -m pytest -q tests/test_codex_appserver_host.py tests/test_conversation_codex.py
```

- `baseline.log/.exit` 留存实际 422 和退出码 1。该轮脚本为 `baseline-probe.py`。
- `metadata.log/.exit` 记录两进程精确恢复实验。期间尝试的 `thread/metadata/update` 被原生执行器拒绝。候选只用写会话名的 RPC。
- `candidate.log/.exit` 留存首次成功及幂等重试。`lifecycle.log/.exit` 留存两轮、原生恢复、精确取消、归属和累计请求数；两份退出码均为 0。
- `legacy-probe.json/.exit` 记录模拟协议的 8 项回归。`final-tests.log/.exit` 为 Codex 宿主与产品 API 专项，24 项通过，1 条既有 Starlette 警告，69.23 秒，退出码 0。

## 证据边界

这次实际恢复的是同一线程在多个原生执行器进程之间的状态；实际取消的是当前开物服务进程拥有的轮次。服务崩溃后对不确定轮次的处理另有模拟用例，本次未制造真实服务崩溃。单一合成实验使用临时测试数据；跨租户文件系统隔离、真实模型授权、工具执行、成本、统一聊天界面及统一运行队列均需另验。普通配置开关仍要求显式注入经过隔离的宿主。
