# 任务说明：交给下一位 Agent 的接手包（2026-11-06）

**你接手的是：** open-claude-code（产品名「知行工作台」）——Better AI 工业 Agent 场景的企业智能助手平台。  
**接手上一个会话：** Codex `01a1af39-f0d7-7b03-aa48-e4552a4059a1`（被用户中断，见 [transcript](sessions/01a1af39-f0d7-7b03-aa48-e4552a4059a1_transcript.md)）。  
**先读这份，再读：** [进展报告](PROGRESS_2026-11-06.md) → [产品升级计划](../PRODUCT_EVOLUTION_PLAN_2026-11-06.md) → [需求基线](../REQUIREMENTS_ALIGNMENT.md) → [需求审计](../verification/REQUIREMENTS_AUDIT_NOTES.md) → [L2 交接证据](../evidence/L2_HANDOFF_2026-11-06.md) → [远端接手提示词](../HANDOFF_REMOTE_2026-11-06.md) → [API 契约](../API_CONTRACT.md) → [可配置工作空间](../CONFIGURABLE_WORKSPACE.md) → [README](../../README.md)。

---

## 1. 身份、位置与权限

| 项目 | 值 |
|---|---|
| 生产服务器 | 腾讯云 `VM-0-8-ubuntu`（ssh 别名 `tencent`，用户 `ubuntu`，可 `sudo -n`） |
| 项目目录 | `/data/dengkw/workspace/open-claude-code`（属主 `ubuntu`，部分文档属 `root`，改前先看权限） |
| 生产服务 | systemd：`zhixing-5400d3fe73-server` / `-client` / `-mcp`，server 监听 `127.0.0.1:18080` |
| 生产数据 | `.runtime/data/`、`.runtime/admin-password`、`.runtime/model-catalog.json`、`.runtime/service.env`（**只读，不要动**） |
| 运行环境 | 项目内 `.venv`（Python 3.10.12）、Node 22.14.0、npm 10.9.2；客户端依赖已恢复 |
| 源码仓库 | <https://github.com/sherkevin/open-claude-code>（private，默认 `main`） |
| 学校云副本 | `/media/data3/dengkw/projects/open-claude-code`（ssh 别名 `school-ssh`，用户 `dengkw`，可 `git pull`） |

## 2. 硬性约束（违反即事故）

1. **不重启、不重新部署** systemd 服务；不覆盖生产 `client/dist`；不升级版本号发布。
2. **不修改** `.runtime/`（数据、密钥、模型目录、管理员密码）、`.venv/`、`.env`、`l2-spec.md`（root 所有）。
3. 测试必须用独立目录：pytest `--basetemp=/tmp/...`，Vite `--outDir /tmp/...`，绝不直接写 `client/dist`。
4. 真实模型调用、客户 MCP、付费上游、外部网络抓取：**先征得用户同意**。
5. 不把设计文档当作「已实现」；所有「完成」必须给出代码位置 + 可重复命令 + 观测结果。
6. 密钥、MCP 凭据、管理员密码、私有上游地址不得进入代码、日志、提交或本仓库。

## 3. 五分钟上手

```bash
ssh tencent
cd /data/dengkw/workspace/open-claude-code
ls; git status 2>/dev/null || echo "not a git repo (expected: production worktree)"
../open-claude-code/.venv/bin/python -V            # 3.10.12
curl -fsS http://127.0.0.1:18080/api/v1/health/ready   # {"status":"ready",...}
systemctl is-active zhixing-5400d3fe73-{server,client,mcp}
```

在**仓库副本**里开发（推荐，不要在腾讯云生产目录直接大改）：

```bash
git clone git@github.com:sherkevin/open-claude-code.git ~/work/open-claude-code
cd ~/work/open-claude-code
```

## 4. 现状一句话

- 0.4.2 已发布并**冻结**；当前工作树已超出 0.4.2（服务端 322 tests、客户端 53 tests、构建通过）。
- 需求基线 v0.2 共 44 个编号：22 已实现 / 11 部分实现 / 4 设计中 / 7 TODO；其中 17 个新编号**还没逐项审计**。
- 用户把产品目标升级为「对标并全面超越客户现有助手」，并已冻结协议、上传格式、四级审批、对标方式等边界。

## 5. 你的第一轮任务（按顺序，做完再动代码）

### T0. 状态校准（只读，约 30 分钟）
- 复跑服务端回归：`cd server && ../.venv/bin/python -m pytest -q --basetemp=/tmp/zhixing-t0-server`
- 复跑客户端：`cd client && npm test && npx tsc -b && npx vite build --outDir /tmp/zhixing-t0-client --emptyOutDir`
- 记录实际数字（收集/通过/失败/跳过/警告/退出码），与 [SERVER_REGRESSION](../evidence/SERVER_REGRESSION_2026-11-06.md)、[CLIENT_REGRESSION](../evidence/CLIENT_REGRESSION_2026-11-06.md) 对账。
- **产出：** `docs/evidence/REGRESSION_RECHECK_<日期>.md`；若数字与 322/53 不符，先查原因再继续。

### T1. 补审计：v0.2 新增的 17 个编号
逐项补齐 `docs/verification/REQUIREMENTS_AUDIT_NOTES.md` 中缺失的编号：

`PROT-001..003、DOC-001、RAG-006..008、APR-001..004、SUB-001..002、UX-001、BENCH-001..003`

每项必须写：状态（已实现/部分实现/设计中/TODO）、服务端代码位置、客户端入口、测试文件、验证命令、实测结果、剩余缺口。**设计中就是设计中**，不要美化。

### T2. 收口阶段 0（需求冻结与基准登记）
- 把 `docs/PRODUCT_EVOLUTION_PLAN_2026-11-06.md` 的阶段 0 拆成可执行任务，登记到 `TODO.md` 与 `docs/ENGINEER_TODO.md`。
- 明确每个阶段的验收命令、证据文件路径、数据哈希与清理策略。
- 明确「本项目不跟踪」的独立线程（Better AI PPT、Linux Mint USB、旧 Intel Mac），不要混进产品版本计划。

### T3. 选一个高价值改动开工（建议顺序）
1. **自动选模**（MOD-004）：把按钮式推荐升级为任务提交时自动路由，保留用户覆盖、活动任务保护、失败回退；写测试 + 更新 API 契约。
2. **执行进度滚动展示**（UX-001）：把已有阶段事件接到前端滚动小字，安全摘要，不含思维链。
3. **Tab 补全打磨**（已存在基础实现，见 `client/src/ChatPage.tsx` 与 `workspace.test.tsx`）。

每次改动：先写/改测试 → 跑回归 → 更新需求状态与证据 → 提交。

## 6. 阶段路线（来自产品升级计划）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 需求与基准冻结（编号、验收命令、合成夹具设计） | **进行中，未收口** |
| 1 | 协议与数据摄取（OpenAI-compatible 适配器、合成 MCP、八类文档摄取） | 待执行 |
| 2 | RAG 与多模态图谱（版本化模型、来源指针、检索链/图谱 API、可视化） | 待执行 |
| 3 | 模型路由与审批控制（自动路由、四种审批、审计字段） | 待执行 |
| 4 | Sub-agent 与执行体验（有界拆分、隔离、级联取消、滚动进度） | 待执行 |
| 5 | 对标与交付（合成基准 → 真实样例，JSON + 图表） | 待执行 |

## 7. 交付与汇报

- **代码：** 提交到 `main`（或按需开分支），提交信息用「做了什么 + 验证命令」。
- **文档：** 需求变更先改 `REQUIREMENTS_ALIGNMENT.md`，再加审计与证据；TODO 状态用 `DONE / RUNNING / TODO / BLOCKED`。
- **证据：** 每项验收产出一个 `docs/evidence/*.md`，写清命令、环境、数字、退出码、失败原因。
- **汇报格式（团队约定）：** `1. 背景` / `2. 现状` / `3. 建议`，用中文。
- **禁止：** 把「设计存在」写成「功能上线」；用宣传数字替代实测；未经批准动生产。

## 8. 已知风险与决策闸门

| 编号 | 风险 | 需要谁决策 |
|---|---|---|
| R1 | 0.4.2 发布树回归口径（319/316/3）未复现 | 你（技术），完成后报用户 |
| R2 | 自动选模会改变既有会话行为 | 用户确认默认策略与回退规则 |
| R3 | 模型代用户批准涉及安全边界 | 用户/管理员确认白名单与额度 |
| R4 | 对标需要客户真实数据与标准答案 | 用户提供 |
| R5 | TokenHub 新模型（混元 4、Kimi 3.1 PRO）未开通 | 商务/上游 |
| R6 | 生产部署与重启 | 用户单独批准 |

## 9. 术语与参照

- **0.4.2**：已发布冻结版本；**当前工作树**：超出 0.4.2，含 0.4.3 方向改动。
- **合成基准**：我们自己造的助手 + MCP + 文档/图片 + 标准答案，用于对标，标记 `synthetic`。
- **四种审批**：安全只读自动 / 模型代用户 / 用户明确 / 管理员或双人。
- **执行进度**：安全状态（计划、阶段、工具、审批、子任务），不是模型私有思维链。
