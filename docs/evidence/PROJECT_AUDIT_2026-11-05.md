# 知行工作台项目交付边界与记录审计（2026-11-05）

## 0. 审计身份、范围与边界

- 级别：L3 执行者；任务 `task_f038b5b9e7ee`；派发 `ctx_9236d690fdbb`；父级终端 `term_5d1b226f-ccce-4610-acab-1f6087a5667c`。
- 项目：open-claude-code（产品名“知行工作台”）；项目目录 `/data/dengkw/workspace/open-claude-code`；服务器：腾讯云。
- 本次工作：只读审阅交付边界、项目记录、正式发行清单、容器配置与客户端／服务端测试入口，并把结论写入本文件。
- 写入边界：仅新建 `docs/evidence/` 与本文件；未修改任何源码、`dist/releases`、`.runtime`、`.venv`、`.local`、`.env`、既有文件权限或所有者；未联网、未部署、未提交、未发送外部消息、未创建下级。
- 执行身份：本次 Shell 为 `root`，所有检查命令均为只读；新建的 `docs/evidence` 目录与本文件按项目惯例归属 `ubuntu:ubuntu`，权限分别为 770／660。
- 事实与推断区分：文中标注“已核验”的结论均由本次命令直接得到；“推断”处单独注明，未当作事实。

---

## 1. 检查命令（可复现）

以下命令均在 `/data/dengkw/workspace/open-claude-code` 执行，除最后一项外不写入任何项目文件。

```bash
# 1) 全部版本归档摘要与各自 manifest 对照
python3 - <<'PY'
import json, hashlib, os
for v in ['0.1.0','0.2.0','0.3.0','0.4.0','0.4.1','0.4.2']:
    d = f'dist/releases/{v}'; m = json.load(open(f'{d}/manifest.json'))
    ok = [hashlib.sha256(open(os.path.join(d, f['name']),'rb').read()).hexdigest() == f['sha256']
          for f in m['files']]
    print(v, m.get('release_status'), len(m['files']), all(ok))
PY

# 2) 0.4.2 归档成员数
cd dist/releases/0.4.2
unzip -Z1 zhixing-client-0.4.2.zip | wc -l          # 57
tar -tzf zhixing-server-0.4.2.tar.gz | grep -v '/$' | wc -l  # 56
unzip -Z1 zhixing-deployment-0.4.2.zip | wc -l       # 57

# 3) 0.4.2 每个归档成员与当前源码逐字节比较
python3 - <<'PY'
# 读取 manifest，逐个成员计算 SHA-256，与工作树同名文件比较
# 结果：170/170 成员与当前源码完全一致，0 个缺失、0 个多余
PY

# 4) 发行脚本的只读输入选择（不写归档）
.venv/bin/python scripts/build_release.py --version 0.4.2 --stage final --check-only

# 5) 服务端测试入口与可收集用例数（不执行、不写缓存）
cd server
PYTHONDONTWRITEBYTECODE=1 ../.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider

# 6) 客户端测试用例计数
cd client
grep -hcE '^\s*(it|test)(\.each)?\(' src/*.test.ts src/*.test.tsx | paste -sd+ | bc

# 7) 条件跳过标记
grep -rniE "pytest\.(skip|importorskip|xfail)|skipif" server/tests

# 8) 归档内容与清单成员名集合比较、私有证据路径存在性检查
```

---

## 2. 关键路径

| 类别 | 路径 | 本次用途 |
|---|---|---|
| 产品说明 | `README.md` | 交付物、部署方式、验证与打包声明 |
| 项目 TODO | `TODO.md` | 主线状态与混入的非产品任务 |
| 工程记录 | `docs/ENGINEER_TODO.md` | 0.1.0—0.4.2 工作项与证据指针 |
| 交付验证 | `docs/DELIVERY_VERIFICATION.md` | 0.4.2 FINAL 判定、测试数与证据边界 |
| 运维手册 | `docs/OPERATIONS.md` | 启动、备份恢复、升级、0.4 权限与记忆迁移 |
| 发行清单 | `dist/releases/0.4.2/manifest.json` | 三包成员、大小、SHA-256 |
| 容器编排 | `compose.yaml` | server/client 构建、数据卷与模型目录挂载 |
| 打包脚本 | `scripts/build_release.py` | FINAL 门禁、白名单、确定性归档 |
| 测试入口 | `server/pyproject.toml`、`server/tests/`、`client/package.json`、`client/src/*.test.ts(x)` | pytest／vitest 配置与用例 |
| 私有证据 | `.runtime/release-review/`、`.runtime/verify_release_042.py`、`.runtime/backups/` | 发布核验与备份存在性 |

---

## 3. 证据摘要

### 3.1 发行物完整性与不可变性：通过

1. **六个正式版本全部与清单匹配（已核验）**：0.1.0、0.2.0、0.3.0、0.4.0、0.4.1、0.4.2 各自的 3 个归档，SHA-256 全部等于对应 `manifest.json` 记录；6 个版本均标记 `release_status: final`。
2. **0.4.2 成员数与清单完全一致（已核验）**：客户端 57、服务端 56、部署包 57，合计 **170** 个成员；归档内没有清单之外的多余成员，也没有缺失成员。
3. **0.4.2 归档内容等于当前源码（已核验）**：170 个成员逐一计算 SHA-256 并与当前工作树同名文件比较，**0 个不一致、0 个缺失**。这与 `docs/ENGINEER_TODO.md:9` 和 `docs/DELIVERY_VERIFICATION.md:17` 的“170 个成员逐字节匹配当前源”声明一致。
4. **FINAL 门禁真实存在（已核验）**：`scripts/build_release.py:139-141` 在 `--stage final` 时要求 `docs/DELIVERY_VERIFICATION.md` 同时包含 `Release readiness: FINAL` 与 `Release version: 0.4.2`；`docs/DELIVERY_VERIFICATION.md:5-6` 确有这两行。已有正式目录会被拒绝覆盖（同脚本 147-148 行）。
5. **归档确定性实现存在（已核验）**：ZIP 固定时间戳（1980-01-01），gzip `mtime=0`，tar 的 uid/gid 置 0、mtime 置 0（`scripts/build_release.py:32、98、111-119`）。归档写入后会重新读取并逐成员比对（103-108、121-128 行）。
6. **私有核验记录存在（已核验）**：`.runtime/release-review/final-archive-verification-042.json` 的摘要字段为 `status=passed`、`version=0.4.2`、`all_members_match_source=true`、`previous_releases_match_original_manifests=true`；`.runtime/verify_release_042.py` 与 `.runtime/backups/pre-0.4.2-20261105T093629Z` 均存在。本次只读取了该 JSON 的摘要字段，未读取任何凭据或业务内容。

### 3.2 文档声明与当前产物的一致性：主体一致，存在记录缺口

| 文档声明 | 位置 | 本次核对结果 |
|---|---|---|
| 三份归档解压到同一目录后形成 `client/`、`server/`、`examples/`、`docs/`、`scripts/` | `README.md:14` | 一致：client 包含 `client/`，server 包含 `server/` 与 `examples/`，deployment 包含 `docs/`、`scripts/`、`README.md`、`compose.yaml` |
| 三包共 170 个成员 | `DELIVERY_VERIFICATION.md:17` | 一致：57+56+57=170 |
| 0.4.2 正式包目录在完成正式打包后才代表交付 | `README.md:159` | 一致：目录存在、清单为 final、三包摘要与清单一致 |
| 清单包含每份归档大小、SHA-256 与完整文件清单 | `README.md:159` | 一致：manifest 字段与实测文件大小、摘要、成员列表相符 |
| 服务端单进程、一个 worker | `README.md:74` | 文档层面一致；`compose.yaml` 未设置多 worker，客户端默认监听 127.0.0.1:8080（`README.md:99` 与 `.env.example` 的 `CLIENT_BIND/CLIENT_PORT` 相符） |
| 容器模板只做结构验证，尚未执行容器部署验收 | `README.md:101` | 边界声明明确，未发现相反声明；`compose.yaml` 结构完整（数据卷、只读模型目录挂载、`create_host_path: false`、`no-new-privileges`） |
| 0.4.2 使用 `conversation-memory/<user_id>/<conversation_id>/` | `OPERATIONS.md:124` | 代码一致：`server/app/memory.py:48` 返回 `conversation-memory/<user_id>/<conversation_id>` |
| 当前公开模型为 deepseek-v4.1-flash、GLM-5.3、Dano-v2.1 | `README.md:25` | 与 `server/model-catalog.example.json` 一致；该模板中 Dano 路由为空且禁用，`README.md:88-89` 已明确提示不能把 Dano 设为默认 |
| 0.4.1／0.4.2 历史归档保持不变 | `README.md:150` | 已核验：0.1.0—0.4.1 的归档摘要与各自清单仍匹配 |

**测试入口与数量（已核验）：**

- 服务端：`server/pyproject.toml` 配置 `testpaths=["tests"]`、`pythonpath=["."]`，测试依赖 `pytest==8.4.2`；当前实收集 **319 项**（命令见第 1 节，0.53 秒）。
- 客户端：`client/package.json` 的 `test` 为 `vitest run`；`client/src/*.test.ts(x)` 中共 **52** 个 `it/test` 用例，与 `DELIVERY_VERIFICATION.md:14` 和 `ENGINEER_TODO.md:9` 的“客户端 52 项”一致。
- 定向 19 项：`test_api.py` 11 + `test_identity_metadata.py` 2 + `test_session_isolation_042.py` 6 = **19**，与 `ENGINEER_TODO.md:9` 的“最终 API／身份／隔离定向 19 项”精确吻合。

### 3.3 发现的问题（按影响排序）

#### A1（中等，证据链缺口）服务端“完整 317 项通过”与当前可收集 319 项不一致

- 记录：`DELIVERY_VERIFICATION.md:12` 与 `ENGINEER_TODO.md:9` 写“腾讯云后端完整 317 项通过；随后……最终 19 项定向测试通过”。
- 实测：当前源码（与 0.4.2 归档逐字节相同）可收集 **319** 项；全仓库只有两处条件跳过逻辑：`server/tests/test_mcp_transport.py:17` 的 `importorskip("mcp")`（本机已安装 mcp，模块已正常收集）与 `server/tests/test_memory.py:150` 的符号链接权限跳过（Linux 下通常执行，不构成 2 项差值）。
- 推断（未证实）：`docs/CORE_0_4_TODO.md:49` 记录“owner-disable 修复新增 2 项测试，2 passed”，恰好解释 317→319；很可能 317 是新增这 2 项之前的完整回归数，之后只跑了 19 项定向测试，没有再跑完整 319 项。记录没有写明这一点。
- 影响：发布记录中的“完整套件通过”不能直接对应最终树的 319 项；这 2 项是否在最终树上与其余 317 项一起通过，没有书面证据。不是已确认的测试失败，而是**最终树全套回归证据缺失**。
- 本次未运行测试套件的理由：任务禁止写入与联网，而该套件会在 `server/` 下创建 `.test-*` 临时目录；本次只做无写入收集。

#### A2（中等，可复现性漂移）当前工作树重新打包会得到 77 个客户端成员，而 0.4.2 正式包是 57 个

- 实测：`python scripts/build_release.py --version 0.4.2 --stage final --check-only` 输出 `member_counts: client=77, server=56, deployment=57`；而正式 manifest 是 `client=57, server=56, deployment=57`。
- 原因已定位：`client/dist/assets/` 现有 22 个构建资源，其中 20 个是旧哈希文件（`index-8B5nvEa8.js`、`index-BvPIpkwc.js` 等，时间 11:44—15:58），当前 `client/dist/index.html` 只引用 `index-CkAqD4aB.js` 与 `index-DahepiB2.css`（17:39 构建）。打包脚本会收集 `client/` 下所有允许的文件（`scripts/build_release.py:58-64、80`），不会按 `index.html` 过滤，因此这 20 个旧文件会被带进新包。
- 结论：0.4.2 正式包本身没有被污染（170 个成员与清单、源码完全一致），但**正式包不是在当前这个工作树状态下直接打出来的**（或打包后 `client/dist` 被恢复/追加了旧文件），记录未说明构建目录。因此 `README.md:159` 的“相同源文件产生相同归档摘要”在当前树上无法复现；直接拿当前 `client/dist/` 做静态部署也会多带 20 个无用文件。
- 影响：任何人若现在重打 0.4.2 暂存包，成员数与摘要都会与正式包不同，容易误判“交付物被改动”；对“不可变归档”的审计信任有影响，但不影响已发布包的内容正确性。

#### A3（中低，证据指针断链）多条私有证据路径在当前腾讯云工作区不存在

- 记录引用但实测不存在：
  - `.runtime/product-0.4.2/identity-live.json`、`semantic-review.json`、`real-routing-evidence.json`（`ENGINEER_TODO.md:6`、`IDENTITY_0_4_2_TODO.md` 多处引用）；
  - `.runtime/product-0.4.2/isolation-live.json`、`browser-verification.json`（`ENGINEER_TODO.md:8`）；
  - `.runtime/local-client-install/verification-042-final.json`（`ENGINEER_TODO.md:9`）。
- 实测存在：`.runtime/release-review/final-archive-verification-042.json`、`.runtime/verify_release_042.py`、`.runtime/backups/pre-0.4.2-20261105T093629Z`。`.runtime` 顶层还有 `identity-04-final.json`、`workspace-04-*` 等 0.4.0 时期证据。
- 可能原因（未证实）：这些文件原本在本地 Windows 开发机或已被清理；记录没有标注主机与位置。
- 影响：在腾讯云工作区无法按记录路径复核 0.4.2 的真实浏览器、身份切换与会话隔离验收；只能依赖文档叙述和发布摘要。

#### A4（低，入口记录不一致）`TODO.md` 没有维护 0.4.x 主线状态

- `TODO.md:11` 的产品主线仍写“0.1.0 归档完成”，与当前 0.4.2 状态不一致；0.4.0—0.4.2 的工作全部只记在 `docs/ENGINEER_TODO.md`。
- `TODO.md:3-7` 是 Better AI PPT 理解任务（唯一 `[RUNNING]`），`TODO.md:66-70` 是 Mint Live USB／Mac 启动任务；两者都不属于产品主线。`l2-spec.md:25` 也提醒需要区分。
- 影响：按团队规范，项目 TODO 应是“已完成／进行中／待决策／未开始”的第一入口；现在读 `TODO.md` 会误以为产品停在 0.1.0，且无法看出主线已到 0.4.2。

#### A5（低，包内文档陈旧）0.4.2 服务端归档包含标题为“Server 0.2.0 verification”的文档

- `server/VERIFICATION.md:1-3` 标题是“Server 0.2.0 verification — 2026-11-05”，正文写 74 passed；该文件被列入 0.4.2 服务端归档成员（服务端包第 12 个成员），未标注“历史版本”。
- 影响：客户或审计者解压 0.4.2 服务端包后，可能把 0.2.0 的验证描述误读为 0.4.2 的验证结论。README 已指向 `docs/DELIVERY_VERIFICATION.md`，实际误导概率不高，但应在下一版本标注历史或移除。

#### A6（低，表述歧义）`OPERATIONS.md:23` 的容器健康检查表述与 `compose.yaml` 不一致

- 原文：“容器默认通过客户端入口 `http://127.0.0.1:8080/api/v1/health/ready` 检查。”
- `compose.yaml` 只有 `depends_on: server`，没有 `healthcheck`，也没有 `condition: service_healthy`。该句更合理的解释是“运维人员从客户端入口手工检查”，但字面上容易被理解为编排自动健康检查。
- 影响：不构成功能缺陷；建议改成“容器部署时请从客户端入口手工检查 ready”。

---

## 4. 产品主线状态盘点

依据：`README.md`、`docs/ENGINEER_TODO.md`、`docs/DELIVERY_VERIFICATION.md`、各 `docs/*TODO*.md`（全部 0 个 `[TODO]/[RUNNING]/[WAITING]` 标记）、`TODO.md`、`l2-spec.md`。

### 4.1 已完成（有本次可核验产物）

- **六个正式发行版本** 0.1.0—0.4.2 均存在，三包齐全，SHA-256 与各自 manifest 全部匹配。
- **0.4.2 交付物完整性**：170 个成员与清单、当前源码逐字节一致；FINAL 门禁由打包脚本真实执行。
- **0.4.2 记录宣称的功能修复**：会话级执行锁／记忆隔离、纯身份元信息查询、模型切换后身份纠正；对应客户端 52 项测试、服务端定向 19 项测试的计数可在源码中精确对上。
- **部署与本地试用**：文档记录腾讯云 0.4.2 部署、本地 0.4.2 安装、真实浏览器与模型验收；发布核对 JSON 存在且 `status=passed`。私有明细见 3.3-A3 的边界。
- **文档与配置**：README、运维手册、API 契约、模型目录模板、compose 模板结构一致，未见互相矛盾的交付范围声明。

### 4.2 进行中

- **无明确记录中的产品主线进行项**。全部产品 TODO 文档的未完成标记为 0；主线停在 0.4.2 发布边界，没有下一迭代的书面任务。
- `TODO.md` 唯一的 `[RUNNING]` 是 Better AI PPT 截图理解，不属于产品主线。

### 4.3 待决策

1. **商业化与计费边界**：`docs/customer-delivery-boundary.md:76-80` 明确指出品牌可选不等于获得统一代付／转售授权；客户自有凭据与我们统一采购转售必须分开确认。这是对外交付前必须由用户／商务确认的事项。
2. **客户真实接入资料**：真实客户 MCP 地址、认证方式、业务样本与原始方案基线尚未提供（`README.md:5、146`，`ENGINEER_TODO.md:101`）。没有这些资料，只能做合成样例验收，不能宣称业务正确率或节省比例。
3. **项目范围归属**：`TODO.md` 中的 Mint Live USB／Mac 启动任务是否继续由本项目跟踪，`l2-spec.md:25、48` 建议向用户确认；目前它们与产品主线混在同一 TODO。

### 4.4 未开始／明确不在本期

- **容器部署验收**：`README.md:101` 明确“模板和 Compose 仅做结构验证，尚未执行容器部署验收”。
- **生产负载与并发资格**：交付记录把生产负载列为证据边界；`AGENT_MAX_ACTIVE_SESSIONS` 等只是应用内限制，不等于容量测试。
- **客户环境灾备演练**：`OPERATIONS.md:118` 明确恢复命令“尚未代表已在客户环境完成灾备演练”。
- **真实客户 MCP 接入与 benchmark 基线**：`README.md:130-142、146` 要求客户提供任务与基线；当前只有合成样例。
- **任意问法／任意提示的身份保证**：`DELIVERY_VERIFICATION.md:21` 明确有限样本不构成无限保证。
- **明确不在当前范围**：员工本机 MCP、OAuth、客户 SSO、多客户共用实例、任意原生扩展脚本（`README.md:41`）。

---

## 5. 未决风险

| 编号 | 风险 | 依据 | 影响 |
|---|---|---|---|
| R1 | 当前工作树无法复现 0.4.2 归档摘要（77 vs 57 个客户端成员） | 3.3-A2 | 重打包结果与正式包不同，削弱“相同源文件→相同摘要”的可审计性；当前 `client/dist` 直接部署会带 20 个旧资源 |
| R2 | 最终树的完整服务端回归没有书面通过记录（317 vs 319） | 3.3-A1 | 若 2 项新增测试在最终树失败，发布记录不会暴露；需要一次干净的全套回归 |
| R3 | 多条私有验收证据路径在腾讯云工作区不存在 | 3.3-A3 | 浏览器、身份切换、会话隔离的结论无法按记录路径独立复核 |
| R4 | 项目入口 TODO 与工程记录脱节，包内 0.2.0 验证文档未标历史 | 3.3-A4、A5 | 新接手者／客户容易误判当前版本与验证范围 |
| R5 | 客户真实 MCP、业务基线、生产负载、容器执行、灾备演练均未验收 | 4.4 | 当前只能称为“试点技术交付”，不能称为客户生产验收完成 |
| R6 | 统一计费／转售授权未确认 | `customer-delivery-boundary.md:76-80` | 若采用我们代付或转售模式，存在合同与合规风险 |

---

## 6. 建议的下一步（1—3 项，均可验证）

### 下一步 1：在干净临时目录复跑服务端完整测试，固化 319 项的真实结果

- 做法：把 `server/` 与 `server/tests/` 复制到项目外的临时目录（或使用 `--basetemp` 指到项目外），运行：
  ```bash
  cd <临时副本>/server
  PYTHONDONTWRITEBYTECODE=1 /data/dengkw/workspace/open-claude-code/.venv/bin/python \
    -m pytest -q -p no:cacheprovider --basetemp=/tmp/zhixing-audit-pytest
  ```
- 验收：记录 `collected / passed / skipped / failed` 的精确数字，并把 `docs/DELIVERY_VERIFICATION.md:12` 与 `docs/ENGINEER_TODO.md:9` 更新为最终树的实际数字；若确有 2 项跳过，写明跳过原因。
- 价值：直接关闭 R2，让“317/319”的差异有结论，而不是推断。

### 下一步 2：清理并重建 `client/dist`，证明打包可复现

- 做法：先备份当前 `client/dist`（项目外），在 `client/` 执行一次会清空输出目录的 `npm run build`（或显式删除 `client/dist/assets` 后重建），再运行：
  ```bash
  .venv/bin/python scripts/build_release.py --version 0.4.2 --stage staging --check-only
  ```
- 验收：`member_counts` 必须回到 `client=57 / server=56 / deployment=57`；随后生成 `0.4.2-staging` 包，逐成员与正式 `0.4.2` manifest 比较名称与 SHA-256，应完全一致（若只有归档摘要因文件名带 `-staging` 不同，成员内容必须一致）。
- 价值：关闭 R1，并确认正式包确实来自“干净 dist”的构建状态；同时避免当前 `client/dist` 被直接拿去部署。

### 下一步 3：修复记录可追溯性与入口一致性

- 做法：
  1. 在 `TODO.md` 顶部增加“产品主线：0.4.2 已发布；下一阶段待定”，把 0.4.x 状态从 `ENGINEER_TODO.md` 摘要到入口，或明确声明“产品主线以 `docs/ENGINEER_TODO.md` 为准”；
  2. 为 A3 中不存在的私有证据路径补一张“证据位置索引”（标明在本地 Windows、已清理或需从备份恢复），或在 `docs/evidence/` 保存脱敏后的摘要；
  3. 在下一个版本把 `server/VERIFICATION.md` 标注为历史（不要改动已发布的 0.4.2 归档），或从下一版服务端包中移除；
  4. 将 `OPERATIONS.md:23` 的容器健康检查改为“手工检查”表述。
- 验收：`TODO.md` 能在一屏内说明当前版本与下一阶段；每个被记录引用的证据路径要么存在，要么有明确位置说明；0.4.2 归档保持不动。

---

## 7. 本次未做与证据边界

- **未执行完整测试套件**：只做了无写入收集（319 项）。未运行的原因见 3.3-A1；本次不据此判断任何测试失败或通过。
- **未读取 `.runtime` 中的敏感内容**：只列目录、检查路径存在性，并读取 `.runtime/release-review/final-archive-verification-042.json` 的顶层摘要字段；未打开 `provider-settings.json`、`service.env`、`admin-password` 等文件。
- **未验证腾讯云远端副本、浏览器交互、真实模型调用、容器构建与运行**：这些需要外部状态、费用或写入，超出本次只读边界。
- **未验证旧版本归档的历史“原始摘要”**：本次只验证了每个版本当前归档与当前 manifest 匹配；未拿到 0.1.0—0.4.1 发布当时的独立快照做二次对照（manifest 本身是当前对照源）。
- **结论性质**：本报告是工程与记录审计，不是客户验收、不是生产负载结论，也不是商务授权意见。
