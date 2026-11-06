# 私有证据位置索引（2026-11-06）

## 0. 目的、范围与读取边界

- 目的：修复 2026-11-05 项目审计（[PROJECT_AUDIT_2026-11-05.md](PROJECT_AUDIT_2026-11-05.md)）中 A3 的证据指针断链问题。项目记录引用了多项私有验收证据，但在当前腾讯云工作区无法按记录路径找到。本索引把“记录路径、当前是否存在、缺失时的主机/位置边界”集中在一处，避免后续接手者把“本机没有”误判成“证据从未存在”。
- 执行身份：L3 执行者；Task `task_3a9281059fff`；Dispatch `ctx_47e278a4fbe0`；父级终端 `term_0deedef4-6eb5-46b3-b84f-c05a03210924`。
- 服务器与工作区：腾讯云，`/data/dengkw/workspace/open-claude-code`。
- 检查时间：2026-11-06（CST）。
- 检查方法：只做路径存在性检查与目录列表（`test -e`、`find`、`ls`）。没有读取任何私有证据文件内容，没有打开凭据、模型路由、管理员密码、业务数据或会话内容。
- 重要边界：**“当前工作区不存在”不等于“这份证据从未存在或被销毁”**。记录没有为大多数路径标注生成主机；它可能位于本地 Windows 开发机、其他主机、备份归档中，或已被清理。本索引不判断证据真实性，只记录“在腾讯云工作区能否按记录路径找到”。
- 本轮未修改 `.runtime`、`.venv`、`.local`、`.env`、`dist/releases`、`server/VERIFICATION.md`。本索引之外只修改了 `TODO.md` 与 `docs/OPERATIONS.md`。

---

## 1. 结论摘要

1. **0.4.2 的发布级证据可以按记录路径找到**：发布摘要 JSON、发布校验脚本和升级前备份目录均存在（见 2.1）。已发布归档本身在 `dist/releases/0.4.2/`；2026-11-05 审计已核验三个归档合计 170 个成员与清单、当时的源码逐字节一致。
2. **0.4.2 的浏览器、身份切换、会话隔离、真实路由与本地安装证据，在当前工作区按记录路径均不存在**（见 2.2）。因此在本主机无法独立复核这些验收过程，只能引用文档叙述、截图和发布摘要。这不影响“已发布归档完整”的结论。
3. 同一轮扫描还发现 0.3.0、0.4.0、0.4.1、TokenHub、飞书等记录的私有证据路径也存在断链（见第 3 节）。其中一部分在本工作区**换了路径**存在（同名文件位于 `.runtime/` 顶层）；同名不等于同一份，本次没有读取内容，不能据此认定已经恢复。
4. 本轮修复的是“入口与索引”，不是“补齐丢失的证据”。要真正复核缺失的验收结论，需要用户授权后从原主机或备份取回文件，或重新执行对应验收。

---

## 2. 0.4.2 证据

### 2.1 按记录路径存在（只确认存在，未读取内容）

| 记录路径 | 类型/大小 | 修改时间 | 记录用途 | 本次状态 |
|---|---|---|---|---|
| `.runtime/release-review/final-archive-verification-042.json` | 文件，638 字节 | 2026-11-05 17:48 | 0.4.2 三份归档的最终核验摘要 | 存在。2026-11-05 审计只读取顶层摘要字段，记录为 `status=passed`、`version=0.4.2`、`all_members_match_source=true`；本索引未重新读取内容。 |
| `.runtime/verify_release_042.py` | 文件，1,710 字节 | 2026-11-05 17:48 | 0.4.2 发布包逐成员校验脚本 | 存在；本次未执行。 |
| `.runtime/backups/pre-0.4.2-20261105T093629Z/` | 目录 | 2026-11-05 17:39 | 升级 0.4.2 前的旧 SQLite/源码与停机数据备份 | 存在；本次未打开目录内容。 |

补充：0.4.2 的公开材料在 `dist/releases/0.4.2/`（三份归档加 `manifest.json`），以及 `docs/screenshots/session-memory-0.4.2.png`、`docs/screenshots/model-identity-0.4.2.png`。这些随项目或部署包保存，不属于私有证据断链范围。

### 2.2 记录引用但当前工作区不存在（A3 核心）

| 记录路径 | 记录出处（2026-11-06 当前行号） | 当前状态 | 主机/位置边界 |
|---|---|---|---|
| `.runtime/product-0.4.2/identity-live.json` | `docs/ENGINEER_TODO.md:37`；`docs/IDENTITY_0_4_2_TODO.md:8` | 不存在 | 记录未标注生成主机；腾讯云工作区无法复核。可能在本地 Windows 开发机、其他主机、备份中或已清理；未核验，未读取内容。 |
| `.runtime/product-0.4.2/semantic-review.json` | `docs/ENGINEER_TODO.md:37`（承接前一条路径的简写）；`docs/IDENTITY_0_4_2_TODO.md:8` | 不存在 | 同上。 |
| `.runtime/product-0.4.2/real-routing-evidence.json` | `docs/ENGINEER_TODO.md:37`（简写） | 不存在 | 同上。 |
| `.runtime/product-0.4.2/isolation-live.json` | `docs/ENGINEER_TODO.md:39`（简写）；`docs/IDENTITY_0_4_2_TODO.md:8` | 不存在 | 同上。 |
| `.runtime/product-0.4.2/browser-verification.json` | `docs/ENGINEER_TODO.md:39`（简写） | 不存在 | 同上。 |
| `.runtime/product-0.4.2/remote-final.json` | `docs/ENGINEER_TODO.md:40`；`docs/HANDOFF_REMOTE_2026-11-06.md:38`；`TODO.md:94` | 不存在 | 同上。注意：2026-11-06 交接记录把它当作可核对文件引用，但本工作区没有该文件。 |
| `.runtime/product-0.4.2/sdk-actual-request-identity.json` | `docs/IDENTITY_0_4_2_TODO.md:3` | 不存在 | 同上。 |
| `.runtime/local-client-install/verification-042-final.json` | `docs/ENGINEER_TODO.md:40` | 不存在；`.runtime/local-client-install/` 整个目录也不存在 | 记录未标注主机；本地客户端安装证据很可能在本地 Windows 主机，但记录没有写明，不能当作事实。 |

说明：表中标注“简写”的路径，记录原文承接同一段前文的 `.runtime/product-0.4.2/` 前缀；路径全名按该前缀补全后进行检查。

**影响：**

- 0.4.2 的“真实浏览器切换、身份切换、会话隔离、真实路由、腾讯云最终状态、本地安装最终校验”这些结论，在当前腾讯云工作区没有可按记录路径打开的证据文件，无法独立复核过程细节。
- 可独立复核的替代材料是：`docs/DELIVERY_VERIFICATION.md` 的叙述、`docs/ENGINEER_TODO.md` 的工作记录、两张 0.4.2 截图，以及发布摘要 JSON 与三份归档的摘要校验。这些能证明“发布物完整”，不能证明“当时浏览器、身份切换和会话隔离验收的过程细节”。
- `docs/DELIVERY_VERIFICATION.md:17` 只写“本地安装和腾讯云最终核验记录保存在私有开发证据目录”，没有给出具体路径；这本身也是一处不可追溯的表述，建议下一版本补上具体路径或本索引链接。

---

## 3. 其他被记录引用的私有证据路径（同一轮扫描）

本节超出 A3 的原始清单，但属于同一类“记录引用了、当前工作区按路径找不到”的问题。继续遵守“只查存在性、不读内容、不判断全局是否存在”的边界。

### 3.1 记录路径不存在，但本工作区 `.runtime/` 顶层有同名文件（路径漂移，未确认同一份）

| 记录路径 | 记录出处 | 本工作区同名文件 | 状态 |
|---|---|---|---|
| `.runtime/remote-preview/multimodel-03-glm-boundary-final.json` | `docs/ENGINEER_TODO.md:74` | `.runtime/multimodel-03-glm-boundary-final.json` | 记录路径不存在；顶层同名文件存在。未读取内容，无法确认是否为同一份证据。 |
| `.runtime/remote-preview/native-context-03-report.json` | `docs/ENGINEER_TODO.md:75` | `.runtime/native-context-03-report.json` | 同上。 |
| `.runtime/remote-preview/release-0.2.0-verification.json` | `docs/ENGINEER_TODO.md:123` | `.runtime/release-0.2.0-verification.json` | 同上。 |
| `.runtime/remote-preview/relocation-report.json` | `docs/ENGINEER_TODO.md:90` | `.runtime/relocation-report.json` | 同上。 |
| `.runtime/remote-preview/relocation-verification.json` | `docs/ENGINEER_TODO.md:90` | `.runtime/relocation-verification.json` | 同上。 |

### 3.2 记录路径在当前工作区完全不存在

| 记录路径 | 记录出处 | 当前状态与边界 |
|---|---|---|
| `.runtime/remote-preview/browser-smoke-relocated.json` | `docs/ENGINEER_TODO.md:97` | 不存在；主机未标注，位置未知。 |
| `.runtime/remote-preview/release-0.3.0-verification.json` | `docs/ENGINEER_TODO.md:76` | 不存在；主机未标注，位置未知。 |
| `.runtime/product-0.4.1/browser-verification.json` | `docs/ENGINEER_TODO.md:46` | 不存在；主机未标注，位置未知。 |
| `.runtime/product-0.4.1/remote-final.json` | `docs/ENGINEER_TODO.md:48` | 不存在；主机未标注，位置未知。 |
| `.runtime/product-0.4/independent-staging-release-audit.json` | `docs/ENGINEER_TODO.md:59` | 不存在；`.runtime/product-0.4/` 目录也不存在。 |
| `.runtime/local-client-install/verification.json` | `docs/ENGINEER_TODO.md:84` | 不存在；整个目录不存在。 |
| `.runtime/local-client-install/verification-03-final.json` | `docs/ENGINEER_TODO.md:72` | 不存在；整个目录不存在。 |
| `.runtime/local-client-install/verification-04-final.json` | `docs/ENGINEER_TODO.md:59` | 不存在；整个目录不存在。 |
| `.runtime/verify_installed_03.py` | `docs/ENGINEER_TODO.md:76` | 不存在。 |
| `.runtime/verify_installed_04.py` | `docs/ENGINEER_TODO.md:59` | 不存在。 |
| `.runtime/verify_installed_041.py` | `docs/ENGINEER_TODO.md:48` | 不存在。 |
| `.runtime/tokenhub-cli/billing-verification.json` | `docs/ENGINEER_TODO.md:78` | 不存在；`.runtime/tokenhub-cli/` 目录不存在。 |
| `.runtime/feishu-guide/verification.json` | `docs/ENGINEER_TODO.md:65` | 不存在；`.runtime/feishu-guide/` 目录不存在。 |
| `.runtime/identity-042-complete-local` | `docs/IDENTITY_0_4_2_TODO.md:7` | 不存在；它是 pytest 的临时 `--basetemp` 目录，不是持久证据文件。 |

### 3.3 存在但不属于“缺失证据”的运行态路径

以下路径在记录里也出现，但用途是运行态数据、凭据或示例输出，不应与验收证据混为一谈。本次只确认存在性，没有读取内容：

- `.runtime/admin-password`、`.runtime/provider-settings.json`、`.runtime/service.env`、`.runtime/model-catalog.json`：运行凭据或配置，存在；不得打印、复制或写入文档。
- `.runtime/data/`：生产数据目录，存在；不在本次检查范围。
- `.runtime/api-smoke.json`、`.runtime/benchmark.json`：README 示例命令的输出位置；当前不存在，属于“尚未在该路径生成”，不是发布证据丢失。
- `.runtime/config/model-catalog.json`：容器部署示例路径；当前不存在，因为本工作区没有执行容器部署。

---

## 4. 复核命令（可复现、只读）

```bash
cd /data/dengkw/workspace/open-claude-code

# 1) 0.4.2 证据路径存在性
for p in \
  .runtime/release-review/final-archive-verification-042.json \
  .runtime/verify_release_042.py \
  .runtime/backups/pre-0.4.2-20261105T093629Z \
  .runtime/product-0.4.2/identity-live.json \
  .runtime/product-0.4.2/semantic-review.json \
  .runtime/product-0.4.2/real-routing-evidence.json \
  .runtime/product-0.4.2/isolation-live.json \
  .runtime/product-0.4.2/browser-verification.json \
  .runtime/product-0.4.2/remote-final.json \
  .runtime/product-0.4.2/sdk-actual-request-identity.json \
  .runtime/local-client-install/verification-042-final.json
do
  if [ -e "$p" ]; then echo "EXISTS  $p"; else echo "MISSING $p"; fi
done

# 2) `.runtime` 顶层文件名清单，用于识别路径漂移
find .runtime -maxdepth 1 -type f -printf '%f\n' | sort

# 3) 记录中的路径引用与当前行号
grep -rn '\.runtime/' docs/ENGINEER_TODO.md docs/IDENTITY_0_4_2_TODO.md \
  docs/DELIVERY_VERIFICATION.md docs/HANDOFF_REMOTE_2026-11-06.md
```

本次执行结果与上表一致：2.1 节三项目标存在；2.2 节八项目标全部不存在；3.1 节五条记录路径不存在，但五个同名文件存在于 `.runtime/` 顶层；3.2 节列出的路径全部不存在。

---

## 5. 本轮修复与其他审计缺口的关系

| 缺口 | 状态 | 说明 |
|---|---|---|
| A3 证据指针断链 | **部分修复** | 本索引提供了集中入口和主机边界说明；缺失的私有证据本身没有恢复。 |
| A4 `TODO.md` 未维护产品主线 | **已修复** | `TODO.md` 顶部新增“产品主线状态（2026-11-06）”：0.4.2 已发布、当前工作树已超出 0.4.2、三项候选任务、独立线程与待用户决策事项。 |
| A5 `server/VERIFICATION.md` 历史标注 | **未处理，留待下一版本** | 该文件是已发布 0.4.2 服务端归档成员；按本轮边界不得修改，下一版本应标注历史或从包中移除。 |
| A6 容器健康检查表述歧义 | **已修复（仅工作树）** | `docs/OPERATIONS.md` 已改为“从客户端入口手工检查 ready”，并注明 `compose.yaml` 当前没有 healthcheck。该文件也是 0.4.2 部署包成员，因此**已发布 0.4.2 部署包中仍是旧表述**；已发布归档未改动。 |
| A1 服务端完整回归证据 | **仅记录现状，未修复** | 0.4.2 最终树在 2026-11-05 审计时可收集 319 项；当前开发树已可收集 322 项（2026-11-06 检查，只收集未执行）。发布记录仍写“317 项完整通过 + 19 项定向通过”。该问题保留为下一阶段候选任务。 |
| A2 重打包成员数漂移 | **情况已变化，仍未以 0.4.2 复现** | 2026-11-06 交接已把 `client/dist` 恢复为 0.4.2 构建（当前只有 `index-CkAqD4aB.js`、`index-DahepiB2.css`、字体和 `index.html`），20 个旧哈希资产不再存在。但当前源码已超出 0.4.2，`python3 scripts/build_release.py --version 0.4.2 --stage final --check-only` 现在给出 client=88、server=79、deployment=57，与 0.4.2 正式包的 57/56/57 不同。0.4.2 归档仍按原清单冻结，不能再用当前工作树重打。 |

---

## 6. 证据边界

- 本索引不是证据真实性认证，只是路径索引；存在性检查可能因权限、挂载或后续清理而变化。
- 没有读取任何私有文件内容；没有执行测试套件、客户端构建、真实模型调用、联网安装、生产重启或部署。
- 没有修改 `dist/releases`、`server/VERIFICATION.md`、`.runtime`、`.venv`、`.local`、`.env`。
- 2026-11-06 的服务端 322 项只是 `pytest --collect-only` 的收集结果，不代表这 322 项已经通过；客户端 53 项是源码中的用例计数，不代表已经执行。
- “主机边界”只依据记录文本：记录没有写明主机的路径，本索引统一写“主机未标注，位置未知”，不推断具体主机。
