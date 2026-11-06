# 证据、命令与备份清单（详细版）

**项目：** open-claude-code（知行工作台）  
**日期：** 2026-11-06（Asia/Shanghai）  
**用途：** 下一位接手者复现验证、更新证据和做安全备份时使用  
**配套文档：** `docs/handover/DETAILED_TASK_SPEC_2026-11-06.md`（任务分解）、`docs/handover/PROGRESS_2026-11-06.md`（进展报告）

---

## 1. 环境

| 项目 | 值 |
| --- | --- |
| 服务器 | 腾讯云 |
| 项目目录 | `/data/dengkw/workspace/open-claude-code` |
| Python | 项目 `.venv`，Python 3.10.12 |
| pytest | 8.4.2 |
| Node.js | 22.14.0 |
| npm | 10.9.2 |
| Vite | 6.1.0 |
| Vitest | 3.0.5 |
| 服务端地址 | `127.0.0.1:18080` |
| systemd 服务 | `zhixing-5400d3fe73-server`、`zhixing-5400d3fe73-client`、`zhixing-5400d3fe73-mcp` |
| 生产数据 | `.runtime/data/` |
| 在线前端 | `client/dist` |

---

## 2. 服务端回归

### 2.1 完整测试

```bash
cd /data/dengkw/workspace/open-claude-code/server
../.venv/bin/python -m pytest -q \
  --basetemp=/tmp/zhixing-server-regression-$(date +%Y%m%d) \
  -p no:cacheprovider
```

2026-11-06 的结果：

```text
322 passed, 1 warning in 40.16s
退出码 0
```

唯一警告来自第三方 `fastapi/testclient.py` 的 `StarletteDeprecationWarning`，不是项目源码警告。

### 2.2 编译检查

```bash
cd /data/dengkw/workspace/open-claude-code/server
../.venv/bin/python -m compileall app
```

2026-11-06 的结果：退出码 0。

### 2.3 定向测试（provider 指纹 fixture）

```bash
cd /data/dengkw/workspace/open-claude-code/server
../.venv/bin/python -m pytest \
  tests/test_model_catalog.py::test_sdk_receives_selection_and_all_model_aliases \
  -q
```

2026-11-06 的结果：3 passed, 1 warning。

### 2.4 记录要求

每次跑完，在 `docs/evidence/` 下新建或更新一份证据文件，至少写：

- 日期和时间；
- 命令原文；
- 收集数、通过数、失败数、跳过数、警告数；
- 退出码；
- 失败用例的完整原因；
- 当时工作树的版本或提交标识（如果有）。

---

## 3. 客户端回归

### 3.1 依赖恢复（仅在 `node_modules` 缺失时）

```bash
cd /data/dengkw/workspace/open-claude-code/client
npm ci --registry=https://mirrors.tencentyun.com/npm/ --no-audit --no-fund
```

2026-11-06 的结果：退出码 0，新增 197 个包；`package-lock.json` 的 SHA-256 前后一致：

```text
a1430efb7d653504641b6e64ebc8b335d2abc560d9f44bd86220910086e9296f
```

这条命令使用腾讯云内网镜像，只有在用户授权后才能再次联网安装。

### 3.2 单元测试

```bash
cd /data/dengkw/workspace/open-claude-code/client
npm test
```

2026-11-06 的结果：10 个测试文件、53 个测试通过，退出码 0。

### 3.3 类型检查

```bash
cd /data/dengkw/workspace/open-claude-code/client
npx tsc -b
```

2026-11-06 的结果：退出码 0。

### 3.4 独立目录构建

```bash
cd /data/dengkw/workspace/open-claude-code/client
npx vite build \
  --outDir /home/ubuntu/client-dist-build-$(date +%Y%m%d-%H%M%S) \
  --emptyOutDir
```

2026-11-06 的结果：退出码 0，1,855 个模块转换完成。

**禁止**在 `client/` 直接执行 `npm run build`，因为它会清空并覆盖在线 Nginx 正在服务的 `client/dist`。

---

## 4. 生产环境检查（只读）

```bash
curl -fsS http://127.0.0.1:18080/api/v1/health/ready
systemctl is-active zhixing-5400d3fe73-server zhixing-5400d3fe73-client zhixing-5400d3fe73-mcp
```

2026-11-06 的结果：

```text
{"status":"ready","model_configured":true}
active
active
active
```

这些检查是只读的，不会重启或修改服务。

---

## 5. 需求覆盖检查

### 5.1 统计需求编号

```bash
cd /data/dengkw/workspace/open-claude-code
grep -E '^\| [A-Z]+-[0-9]+' docs/REQUIREMENTS_ALIGNMENT.md \
  | awk -F'|' '{gsub(/^ +| +$/,"",$2); gsub(/^ +| +$/,"",$5); print $2"\t"$5}' \
  | sort
```

2026-11-06 v0.2 的结果：44 个编号，22 已实现、11 部分实现、4 设计中、7 TODO。

### 5.2 审计表覆盖检查

```bash
cd /data/dengkw/workspace/open-claude-code
grep -c '^| [A-Z]*-[0-9]' docs/verification/REQUIREMENTS_AUDIT_NOTES.md
```

当前结果是 27，目标是 44。下一位接手者必须补齐。

### 5.3 设计文档存在性检查

```bash
cd /data/dengkw/workspace/open-claude-code
test -s docs/design/RAG_MULTIMODAL_GRAPH.md
test -s docs/design/SUBAGENT_ORCHESTRATION.md
test -s docs/design/LIVE_EXECUTION_PROGRESS.md
```

注意：文件存在只证明设计文档存在，不证明功能已实现。

---

## 6. 改动前的备份

任何修改项目文件之前，先做时间戳备份：

```bash
cd /data/dengkw/workspace/open-claude-code
backup_dir=/tmp/open-claude-code-backup-$(date +%Y%m%d-%H%M%S)
mkdir -p "$backup_dir"
cp -a README.md TODO.md docs/REQUIREMENTS_ALIGNMENT.md docs/ENGINEER_TODO.md \
  docs/verification/REQUIREMENTS_AUDIT_NOTES.md "$backup_dir"/
```

如果要改 `server/` 或 `client/` 源码，把对应文件也复制进去。

2026-11-06 会话已经做过的备份：

| 备份路径 | 内容 |
| --- | --- |
| `/tmp/open-claude-code-backup-20261106-req-audit/server/tests/test_model_catalog.py` | 修改前的模型目录测试 |
| `/tmp/open-claude-code-backup-20261106-req-audit-README.md` | 修改前的 README |
| `/tmp/open-claude-code-backup-20261106-req-audit-client-package-lock.json` | 修改前的 `package-lock.json` |
| `/tmp/open-claude-code-backup-20261106-product-scope/` | 需求文档、审计、TODO 和工程待办的修改前副本 |

`/tmp` 可能在重启后清理。如果这些备份还要用，尽早复制到项目的私有备份目录或用户指定的位置。

---

## 7. 证据文件地图

| 文件 | 内容 |
| --- | --- |
| `docs/evidence/SERVER_REGRESSION_2026-11-06.md` | 服务端 322 项通过、编译检查、fixture 修正说明 |
| `docs/evidence/CLIENT_REGRESSION_2026-11-06.md` | 客户端依赖恢复、53 项测试、类型检查、独立构建 |
| `docs/evidence/L2_HANDOFF_2026-11-06.md` | L2 交接身份、历史基线、风险和首阶段任务 |
| `docs/evidence/EVIDENCE_INDEX_2026-11-06.md` | 私有证据路径索引、缺失路径和主机边界 |
| `docs/evidence/BASELINE_2026-11-05.md` | 0.4.2 发布树历史基线 |
| `docs/evidence/PROJECT_AUDIT_2026-11-05.md` | 2026-11-05 项目审计 |
| `docs/verification/REQUIREMENTS_AUDIT_NOTES.md` | 27 项逐项需求和回归记录（需扩展） |
| `docs/DELIVERY_VERIFICATION.md` | 历史发布验证记录 |
| `docs/ENGINEER_TODO.md` | 工程实现、测试和部署记录 |
| `TODO.md` | 项目待办和主线状态 |

---

## 8. 当前已知的临时路径

| 路径 | 用途 | 是否可清理 |
| --- | --- | --- |
| `/tmp/zhixing-server-pytest-20261106.log` | 服务端 pytest 日志 | 可以，证据已写入 docs |
| `/tmp/zhixing-server-compileall-20261106.log` | 编译日志 | 可以 |
| `/tmp/zhixing-client-npm-ci-20261106.log` | npm ci 日志 | 可以 |
| `/tmp/zhixing-client-npm-test-20261106.log` | 客户端测试日志 | 可以 |
| `/tmp/zhixing-client-tsc-20261106.log` | TypeScript 日志 | 可以 |
| `/tmp/zhixing-client-vite-build-20261106.log` | Vite 构建日志 | 可以 |
| `/tmp/zhixing-model-catalog-fixture/` | 定向测试临时目录 | 可以 |
| `/tmp/open-claude-code-backup-20261106-*` | 修改前备份 | 确认不再需要后再清理 |
| `/home/ubuntu/client-dist-build-20261106-1348/` | 独立客户端构建产物 | 可以作为证据保留，也可以清理 |

---

## 9. 一句话提醒

证据不是“我记得跑过”，而是“别人拿到这份记录，能用同样的命令得到同样的结果”。
