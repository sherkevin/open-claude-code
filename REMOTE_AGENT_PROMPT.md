# 远程 Agent 接手提示词

把下面整段粘贴到腾讯云上的 Codex/Claude Code 远程会话。路径和约束已经写死，不需要再向用户确认项目位置。

---

你接手的是 Better AI 工业 Agent 项目。代码在 `/data/dengkw/workspace/open-claude-code`，后续所有开发都在这个目录直接进行。先读 `docs/HANDOFF_REMOTE_2026-11-06.md`、`docs/REQUIREMENTS_ALIGNMENT.md`、`docs/ENGINEER_TODO.md`、`TODO.md`、`docs/API_CONTRACT.md`、`docs/CONFIGURABLE_WORKSPACE.md` 和 `README.md`，然后开始工作。

当前状态：

- 这是已有生产环境的项目，systemd 服务 `zhixing-5400d3fe73-server/client/mcp` 正在运行；server 在 `127.0.0.1:18080`，数据在 `.runtime/data/`。
- 使用项目已有的 `.venv`；Python 3.10.12，Node 22.14.0，npm 10.9.2。
- 之前的本地会话已经完成 `docs/REQUIREMENTS_ALIGNMENT.md`，但还没完成逐项审计和回归。
- 根目录 `l2-spec.md` 属于 root，若你能读就作为独立 L2 规范参考；不要覆盖它。若与本文冲突，先问用户。

你的第一轮任务：

1. 只读审计：读取需求文档、代码、测试和现有验收记录，不做部署、不重启服务、不改 `.runtime/data`。
2. 生成 `docs/verification/REQUIREMENTS_AUDIT_NOTES.md`：对 `REQUIREMENTS_ALIGNMENT.md` 里的每个 ID 记录状态、代码位置、验证命令、观测结果、剩余缺口。设计中的能力（多模态图谱、生产 Sub-agent、RAG-005）必须标为“设计中”，不能写成“已实现”。
3. 修复审计发现的最小范围文档/代码不一致；代码改动必须补测试。
4. 跑回归：`cd server && ../.venv/bin/python -m pytest -q && ../.venv/bin/python -m compileall app`；`cd client && npm test && npm run build`。记录精确命令、通过/失败数量和失败原因。
5. 更新 `docs/ENGINEER_TODO.md` 和 `TODO.md`，把审计与回归标成 DONE 或保留为明确的 TODO，部署另列为待用户批准。
6. 最后给用户一份中文简报：审计结论、改了哪些文件、测试结果、仍未完成/待客户确认的事项、下一步建议。

硬性约束：

- 不重启、不重新部署 systemd 服务；不覆盖 `.runtime/data/`、`.runtime/backups/`、`.runtime/model-catalog.json`、`.runtime/service.env`、`.runtime/admin-password`、`.venv/`。
- 不把密钥、MCP 凭据、管理员密码、私有上游地址写进代码、日志或提交。
- 不运行 `rm -rf` 项目根目录；改文件前先做时间戳备份。
- 真实模型调用、客户 MCP 调用、花钱的验收必须先征得用户同意。
- 不把“设计文档存在”当成“功能已上线”；所有“已实现”必须有代码位置和可重复验证命令。
- 生产部署必须单独做备份、验证和回滚方案，并等用户明确批准。

如果审计发现需求文档与远程实际部署不一致，以“源码 + 测试 + 远程实测”为准，先记录差异，再修正文档；不要为了让文档好看而删掉需求。

现在开始第一步：读文档并列出审计计划，然后直接生成 `docs/verification/REQUIREMENTS_AUDIT_NOTES.md` 的第一版。

---

## 给用户的补充说明

- 本地不再需要继续在 Codex 里恢复那个旧窗口；远程 agent 以这份文档和 `REQUIREMENTS_ALIGNMENT.md` 为准继续。
- 远程目录直接就是生产目录，所以提示词明确禁止在审计阶段重启服务。
- 如果希望远程 agent 先只做审计、不改代码，把提示词第 3 步改成“只记录，不改代码”，其余不变。
