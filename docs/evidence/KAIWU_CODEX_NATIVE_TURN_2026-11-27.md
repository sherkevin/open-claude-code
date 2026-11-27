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

这次实际恢复的是同一线程在多个原生执行器进程之间的状态；实际取消的是当前开物服务进程拥有的轮次。下节另列整个开物服务进程退出后的实测。单一合成实验使用临时测试数据；跨租户文件系统隔离、真实模型授权、工具执行、成本、统一聊天界面及统一运行队列均需另验。普通配置开关仍要求显式注入经过隔离的宿主。


## 整个开物服务重启后的同会话恢复

固定候选 a9aab5e，三次启动独立的 Python 进程、创建开物应用及 Codex 宿主并以同一用户重新登录。三个进程读取同一份隔离产品数据库、工作区和会话绑定。HTTP 仍通过 TestClient 进入产品路由。模型侧仍是合成回环响应。

| 进程与故障点 | 用户在原会话看到的结果 | 回环模型累计收件 |
|---|---|---:|
| 进程一正常完成第一轮并退出 0 | completed，取得原生轮次 ID 和固定正文 | 1 次 |
| 进程二重登，按原请求标识重试第一轮 | 重启后仍返回原 ID 和完整原文，replayed=true | 1 次 |
| 第二轮原生执行结束、开物尚未落盘回执时，测试钩子让进程二立即退出 73 | 进程三重登，读到正确的活动 ID 和待核对状态；直接重发同一请求得到 409 | 2 次 |
| 进程三显式核对，随后再取第二轮 | 按精确轮次查到 completed；本地丢失的正文显示为空且 text_truncated=true，审批次数未知 | 2 次 |
| 进程三显式发送第三轮 | 同一线程的新轮次正常完成，ID 独立 | 3 次 |

证据目录同上。以 `KAIWU_TEST_TREE="$T" bash "$D/run-restart.sh"` 复跑；脚本 `restart-probe.py` 记录进程退出点与累计回环收件。`restart.log/.exit` 记载隔离命名空间中的服务 PID 3、206、312 和退出码 0、73、0；总控退出码 0。前提是已经持久化精确轮次 ID，且原生历史能确认终态。只存有请求标识、尚未收到原生轮次 ID 的更早崩溃不在该样本内。

另用独立的回环服务把第二轮响应正文一直扣住。收到请求、宿主记录精确活动 ID 后，令开物进程立即退出 74。回环提供方随总控继续存活。第三个开物进程重新登录，先拒绝重发（409），随后显式核对得到同一轮次的 interrupted；重取回执不产生新增请求。用户显式发出的第三轮恢复正常，三轮累计 3 次模型侧收件。与上一组的 completed 结果可直接区分“原生执行已结束但产品回执丢失”和“执行中宿主退出”。各进程 PID 为 4、204、308。复验命令为 `KAIWU_TEST_TREE="$T" bash "$D/run-active.sh"`，原始 `active.log/.exit` 退出码 0。两组均未涉及模型侧在外部系统产生写入的情况。
