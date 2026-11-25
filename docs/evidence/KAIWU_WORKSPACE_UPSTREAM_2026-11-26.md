# 2026-11-26｜E1 官方工作环境组件基线

基线 ee5b491；Goal driver 与 Mem0 工人分别在独立工作树实际开发，未修改其候选。复用固定官方 Harness fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159，执行前后 tracked diff 为空。Node 24.9.0 / Vitest 4.1.11，env -i、独立 data3 HOME/TMPDIR/XDG_CACHE_HOME、2 workers；未传入模型凭据、真实客户数据或生产连接。测试退出码 0，37 文件、737/737，运行 24.01 秒（外层 26.21 秒）。

| 官方组件 | 套件数 | 测试通过 |
|---|---:|---:|
| packages/api/workspace-controller | 5 | 73/73 |
| packages/api/workspace-files | 12 | 211/211 |
| packages/browser-use/browser-use | 1 | 2/2 |
| packages/computer-use/computer-use | 1 | 2/2 |
| packages/jobs/jobs | 2 | 8/8 |
| packages/jobs/jobs-local | 5 | 109/109 |
| packages/jobs/tool-jobs | 2 | 87/87 |
| packages/sandbox/sandbox-policy | 1 | 24/24 |
| packages/terminal/terminal | 1 | 23/23 |
| packages/terminal/terminal-bash | 3 | 32/32 |
| packages/util/workspace-path | 3 | 84/84 |
| packages/workspace/workspace | 1 | 82/82 |

原始证据 /media/data3/dengkw/acceptance/kaiwu-workspace-upstream-20261126/upstream.log。包含工作区模型与传输恢复、文件路径和范围、Jobs 服务/本地执行记录及工具控制、终端服务/配置/输出缓冲、sandbox policy、浏览器与计算机 provider 注册。未运行真实外部浏览器/桌面控制 provider、terminal-bash 实际会话及 OS sandbox 强隔离测试；不能据此宣称这些能力已可用或具备跨租户隔离。

## 产品接入边界及下一断点

- 现有 server/app/workspace_tools.py 的宿主授权和 WorkspaceFiles，以及 server/harness/profile.py 的固定工具注册继续作为权限边界；不得直接把官方通用文件/任意 shell/浏览器能力加入可用工具集。
- 官方 Jobs local 和 terminal 的进程内状态不能当跨进程持续任务；后续先做只读当前会话工作区/任务视图，保留既有权限和恢复语义，再验证持久执行器、租户隔离及审批。
- Team 官方 README 确认任务板由 root 会话持有、CAS 认领和依赖、共享 checkout；文件冲突提示不是隔离。不能将跨租户用户或不同进程的 worker 当成一个 Team。先接入只读 root scope 及原生模式/任务板，后续才开放模型任务/通信，保留已有 delegate_tasks 宿主权限。
- Codex App Server 尚未固定发行版及验证；不得使用指向其他项目的默认 CLI 包装器。需独立固定二进制及协议版本、合成无凭据握手和审批/取消恢复测试。

生产 A2.10 发布闸门不变；此项只是官方基线，不是开物 E 阶段完成。
