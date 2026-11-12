# OC-T11 DeepSeek Harness 插件市场发现与上下文压缩插件评估

日期：2026-11-12（Asia/Shanghai）

任务节点：`OC-T11`

颜色组：橙色“DeepSeek Harness 插件市场发现与节省 Token 插件评估接入”

执行角色：橙色组 Cloud Code 执行者

隔离分支：`worktree-oc-t11-harness-plugins`

基线提交：`b69f1f0`（`OC-T3` 受控插件治理、文件工具和子任务隔离）

## 1. 结论

本轮完成了两个候选插件的来源、版本、许可证、SHA-256、权限和兼容性核验，并实现了默认拒绝的受控发现、激活前置门禁、上下文压缩适配器边界和两阶段用量账本。

- `dshmarket`：**发现层可交付**。只输出固定清单中的元数据，不下载、不安装、不执行第三方代码；远程目录被视为不可信输入。
- `billion-context`：**适配器边界可交付，真实压缩效果不可交付**。仓库和发行包真实存在，版本和摘要已核验；但本轮没有安装或执行插件，真实模型压缩效果标记为 `not_measured`。合成基准只验证边界契约，不代表插件质量或供应商账单。
- 两个插件都**没有安装到当前运行 profile**，没有重启服务，没有切换默认检索，没有使用真实客户数据、生产凭据或未授权付费模型。
- `billion-context` 的 MIT 许可证带有额外署名条款，产品界面可见前必须完成署名放置决策；这不是单纯 `license=MIT` 就能关闭的事项。

## 2. 权威来源与固定版本

### 2.1 dshmarket

| 字段 | 核验结果 |
|---|---|
| 源码仓库 | <https://github.com/dsh-market/dsh-market> |
| 固定发行版 | `v1.73.18` |
| 发行版发布时间 | GitHub API：`2026-11-11T16:42:32Z` |
| 标签提交 | `7f32bb6c8566053916c5ba646263c132ac8d703c`（`git ls-remote refs/tags/v1.73.18`） |
| 核验时默认分支头 | `ade3fbdc7c9bfbfeba5b8839e4fd78df0d36c036`（仅记录，不作为固定版本） |
| 包名/版本 | `dshmarket@1.73.18` |
| 许可证 | MIT（发行包根目录 `LICENSE`） |
| 发行包 | <https://github.com/dsh-market/dsh-market/releases/download/v1.73.18/dshmarket-1.73.18.tgz> |
| 本地授权归档 | `/media/data3/dengkw/.runtime/open-claude-code/plugin-audit/artifacts/dshmarket-1.73.18.tgz` |
| 归档大小 | 899,872 字节 |
| SHA-256 | `a41997fcb10e1e72360f18abecf6a92346dc43c272054e3bd0b7996c61616c85` |
| 官方资产摘要 | GitHub Release 资产 `digest=sha256:a41997fcb10e1e72360f18abecf6a92346dc43c272054e3bd0b7996c61616c85`，与本地归档一致 |
| DSH 兼容声明 | `@deepseek-ai/cordis ^4.0.1`；`@deepseek-ai/dsh-settings` 多个 rc 版本；README 要求 DSH Web `>=0.1.0-rc.6` |

静态包内元数据：

- `dsh.bundle.patch` 指向 `cordis.patch.yml`，补丁只把 `dsh-market` 插入 profile layer。
- 声明 Web 客户端注入和 `platform: web`。
- README 明确：目录来自远程 `awesome-dsh-plugin.com/plugins.json`，也支持 npm/镜像路由；安装会调用 npm/pnpm，第三方插件构建脚本默认被 pnpm ≥10 阻止，但仍属于执行第三方代码。
- README 明确安全边界：安装来源限制在 curated registry、安装端点只接受同源 POST、认证 Gist 请求直接去 `api.github.com`；这些是插件自身声明，不是本项目可依赖的安全保证。

### 2.2 billion-context

| 字段 | 核验结果 |
|---|---|
| 源码仓库 | <https://github.com/ranxianglei/billion-context> |
| 固定发行版 | `v0.1.213` |
| 发行版发布时间 | GitHub API：`2026-11-07T23:25:08Z` |
| 标签提交 | `133f3525c628c4b959bb79ece82af6df82e131a3`（`git ls-remote refs/tags/v0.1.213`） |
| 发行包内构建信息 | `dist/build-info.json` 的 `commit` 为 `133f3525c`，与标签提交一致 |
| 核验时默认分支头 | `9674cfafbc78c50245ae0d887483d3d6ce880c15`（仅记录，不作为固定版本） |
| 包名/版本 | `billion-context@0.1.213` |
| 许可证 | MIT + 额外“终端用户可见产品署名”条款（发行包根目录 `LICENSE`） |
| 发行包 | <https://registry.npmjs.org/billion-context/-/billion-context-0.1.213.tgz> |
| 本地授权归档 | `/media/data3/dengkw/.runtime/open-claude-code/plugin-audit/artifacts/billion-context-0.1.213.tgz` |
| 归档大小 | 11,075,887 字节 |
| SHA-256 | `e3dab47c9e2cbbe67b541190188a3217571c10cb3ea771ab7c04b1301251df98` |
| npm 完整性 | `sha1=9573e0c081195cb41048d8e7ce8a493426d093d7`；`sha512-27GMDHpqImsGev2Pw2JUyWImzJChggJVUqo6RNNeZ4CWFMnuKReQWFghOAZTBU05pmYho/2K0UZ7WWjQ+aBBWQ==` |
| 运行时要求 | Node `>=20`；依赖 `mysql2`、`zod`，可选 `jimp` |
| DSH 兼容声明 | 包内没有 `peerDependencies`；文档记录默认最新已验证 DSH 为 `0.2.0-rc.2`，更高版本只给启动提示，不阻断流量 |

许可证附加条款要求：如果产品或服务对终端用户可见或可交互，必须在首页、文档或 About/Credits/Acknowledgements 至少一处清晰标注使用 `billion-context`，并链接到 <https://github.com/ranxianglei/billion-context>。

静态包内行为：

- `dsh.bundle.patch` 指向 `dsh.bundle.patch.yml`，会插入 `bili-native` 和 `bili-compaction-gate`，关闭宿主 `compaction-basic` 的自动压缩，并向预设注入 `biliBasicCompactionPolicy`，同时修改 DeepSeek 重试策略。
- README 明确项目状态为 **Early**：协议处理和压缩主要针对 mock tests，真实模型集成测试仍是下一里程碑。
- 默认代理监听 `127.0.0.1:8787`；管理端点 `/__bili/*` 没有认证，绑定 `0.0.0.0` 时只能放在可信网络。
- 启动时和每 3 分钟检查 npm 更新，带自动更新、advisory 检查和自重启能力；这些行为都需要在受控评估中单独关闭或审计。

## 3. 权限、网络、文件、模型和数据范围

| 维度 | dshmarket | billion-context |
|---|---|---|
| 本节点声明的原生权限 | `web.read`、`workspace.files.write`、`process.execute` | `workspace.files.read`、`workspace.files.write`、`web.read`、`memory.read` |
| 只读发现所需权限 | `web.read` | `workspace.files.read`、`web.read` |
| 本项目默认授权 | 只读权限集合 | 只读权限集合 |
| 原生安装能力 | 会下载、写 profile、调用 npm/pnpm | 会写配置和本地状态、启动代理、修改宿主压缩/重试路径 |
| 本节点执行状态 | `not_installed` / `not_executed` | `not_installed` / `not_executed` |
| 网络 | 本轮不对插件目录发请求；仅用公开 API 核验发行版身份 | 本轮不启动代理，不访问供应商模型 |
| 文件 | 不读取插件目录以外文件 | 不读取宿主配置和会话 |
| 模型调用 | 无 | 无；真实压缩效果 `not_measured` |
| 客户数据/凭据 | 未读取、未写入、未输出 | 未读取、未写入、未输出 |

`process.execute` 被登记为“已知但本产品不可授予”的原生权限。`credentials.read`、`filesystem.any` 被登记为禁止权限；候选一旦声明就整体拒绝。激活门禁按“加载插件即获得全部声明权限”处理，而不是只看调用方申请的子集。

## 4. 实现位置

| 能力 | 位置 |
|---|---|
| 固定插件清单、来源/版本/许可证/摘要/权限校验、发现过滤、激活门禁、审计持久化 | `server/app/plugin_catalog.py` |
| 上下文压缩适配器边界、超时/取消/失败/版本变化、事实与约束断言、合成长上下文 | `server/app/billion_context.py` |
| 两阶段用量账本、未知用量不补零、本地估算与供应商用量分离 | `server/app/plugin_usage.py` |
| 插件发现 API：`GET /api/v1/admin/plugins/discovery` | `server/app/plugins.py` |
| 插件发现审计表 `plugin_catalog_events` | `server/app/db.py` |
| 离线核验命令 | `scripts/verify_harness_plugins.py` |
| 定向测试 | `server/tests/test_harness_plugin_catalog.py`、`test_harness_plugin_permissions.py`、`test_billion_context_adapter.py`、`test_harness_usage_ledger_plugin.py` |

### 4.1 发现链路

- 只读取 `CATALOG_SNAPSHOT` 中的固定元数据；不读取远程目录，不下载 tarball，不导入插件。
- 每个候选必须同时具备：名称、固定版本、源码提交、HTTPS 来源、HTTPS 发行地址、64 位十六进制 SHA-256、允许的宽松许可证、非空的权限声明、非空的发现权限、能力声明、`offline_verified` 审核状态、`not_installed` 和 `not_executed` 状态。
- 任一字段缺失或格式错误即拒绝，并记录 `rejected` 审计。
- 来源主机必须在管理员白名单内；权限必须在管理员授权集合内；Harness 版本必须满足声明范围。
- 网络异常返回 `blocked_network` 且条目为空；取消返回 `cancelled` 且条目为空；不会返回“部分成功”的候选列表。
- 发现结果只包含元数据，不含安装命令、脚本内容或可执行代码路径；`metadata_only=true`。

### 4.2 激活门禁

`authorize_activation()` 在运行时接入前再次校验：

- 固定清单字段、许可证、摘要和审核状态；
- 注册表记录必须存在且状态为 `approved`，名称、版本、许可证和 SHA-256 必须完全一致；
- 候选未撤销；
- Harness 版本兼容；
- 插件声明的全部原生权限都必须在管理员授权集合内；
- 调用方申请的权限必须已声明且可授予。

任何一项不满足都返回 `allowed=false` 和稳定原因码，`execution=not_started`。因此：

- `dshmarket` 因声明 `process.execute`，在当前策略下不能激活，只允许只读发现；
- `billion-context` 因声明 `workspace.files.write` 等原生权限，在默认只读策略下不能激活；
- 将管理员授权集合显式扩展后，`billion-context` 可以通过门禁，但仍不会自动执行插件。

### 4.3 压缩适配器边界

- 适配器默认关闭（`enabled=false`）；启用必须显式调用 `enable()`，且清单校验必须通过。
- `rotate_manifest()` 在版本变化时关闭适配器；缓存键包含清单指纹（名称、版本、提交、摘要、许可证）、请求摘要和压缩器标识，旧版本摘要不会被复用。
- 超时、取消、压缩器异常、无效输出、事实/约束缺失都不会写入缓存；返回原上下文作为回退，`used_compressed=false`。
- 事实和约束断言由适配器独立检查，不信任压缩器自报。
- `rollback()` 关闭适配器并清空缓存，返回 `fallback=original_context`。
- 测试使用 `SyntheticExtractiveCompressor`，结果固定标记：
  - `plugin_executed=false`
  - `measurement=synthetic_reference`
  - `real_plugin_measurement=not_measured`
  - `tokens.authoritative=false`

### 4.4 用量账本

- 阶段固定为 `compression` 和 `answer`，分开记录。
- 供应商用量只接受非负整数；缺失、`null`、负数、浮点数、布尔值和字符串都保持未知，并写入 `invalid_fields` / `unknown_fields`。
- 任一阶段某字段未知时，聚合 `provider_totals` 中该字段为 `null`，不会补零；已知部分单独放在 `known_subtotals`。
- 压缩阶段的本地字符/Token 估算标记 `authoritative=false`，不能当作供应商账单。
- 压缩调用本身仍然作为独立阶段保留；失败、超时和取消也保留尝试记录，不删除可审计成本字段。
- 未知供应商用量测试验证：`provider_usage_complete=false`，`unknown_stages` 明确指出缺失阶段和字段。

## 5. 验收结果

### C1：来源、固定版本、许可证和 SHA-256

命令：

```bash
python3 scripts/verify_harness_plugins.py \
  --plugins dshmarket,billion-context --format json --offline
```

结果：退出码 0，两个插件均为 `verified`，`integrity.scope=pinned_review_record`，`network_used=false`，`third_party_code_executed=false`。原始输出：`docs/evidence/oc_t11_verify_metadata.json`。

带本地授权归档的实际摘要核对：

```bash
python3 scripts/verify_harness_plugins.py \
  --plugins dshmarket,billion-context --format json --offline \
  --artifact-dir /media/data3/dengkw/.runtime/open-claude-code/plugin-audit/artifacts
```

结果：退出码 0，两个 `integrity.match=true`。原始输出：`docs/evidence/oc_t11_verify_artifacts.json`。

拒绝路径：缺少许可证、版本、来源或摘要时注册 API 返回 422；摘要格式错误、非 HTTPS 来源、内嵌凭据、未声明权限都会拒绝；以上路径不会执行任何第三方代码。

### C2：dshmarket 受控发现、权限、撤销、取消和网络失败

覆盖测试：

- 只返回元数据，`metadata_only=true`，不含安装命令或代码路径。
- 未白名单来源被过滤并写审计。
- 未知权限、未声明发现权限、禁止权限被拒绝。
- 旧 Harness 版本（`0.1.0-rc.5` 低于 `0.1.0-rc.6`）被拒绝。
- 撤销候选被拒绝并写 `plugin_revoked` 审计。
- 前置取消返回 `cancelled` 且无条目。
- 目录网络异常返回 `blocked_network` 且无条目。
- 非管理员访问发现 API 返回 403。
- 管理 API 发现结果写入 `plugin_catalog_events`，包含操作者、动作、原因和时间。

### C3：billion-context 合成上下文、失败关闭和账本口径

覆盖测试：

- 固定合成长上下文可重复运行，三次摘要完全一致；事实 `FACT-001`、`FACT-002` 和约束 `CONSTRAINT-A`、`CONSTRAINT-B` 全部保留。
- 失败、超时、取消、事实缺失和版本变化都不会复用旧摘要。
- 回滚后适配器关闭、缓存清空，后续请求直接返回 `disabled`。
- 压缩阶段和回答阶段分开记录；未知供应商用量不补零。
- 真实插件执行状态始终为 `plugin_executed=false`，真实效果为 `not_measured`。

合成基准（`docs/evidence/oc_t11_benchmark.json`，仅验证适配器边界，不评价插件质量）：

| 指标 | 数值 |
|---|---|
| 原始上下文字符数 | 18,118 |
| 原始本地 Token 估算 | 4,530（`chars_div_4`，非权威） |
| 压缩后字符数 | 300 |
| 压缩后本地 Token 估算 | 75（`chars_div_4`，非权威） |
| 本地估算缩减比例 | 0.983444（合成参考压缩器，不代表真实插件） |
| 事实/约束保持 | 全部 true |
| 三次运行摘要一致性 | true |
| 第二次、第三次缓存命中 | true |
| 压缩器异常 | `failed`，`used_compressed=false` |
| 超时 | `timeout`，`used_compressed=false` |
| 取消 | `cancelled`，`used_compressed=false` |
| 插件是否执行 | false |
| 真实插件效果 | `not_measured` |

账本样例：

- `compression` 阶段：无供应商调用，供应商用量未知，本地估算单独记录。
- `answer` 阶段：合成回答桩记录 120 输入 / 30 输出 Token，明确标注为合成，不是真实供应商账单。
- 聚合 `provider_totals` 因压缩阶段未知而保持 `null`；`known_subtotals` 单独显示 120 / 30；`provider_usage_complete=false`。
- 这满足“压缩调用与回答阶段分开计量”，同时避免把本地估算或合成桩冒充供应商真实消耗。

### C4：定向 pytest

在 `server` 目录执行：

```bash
TMPDIR=/dev/shm/oc-t11-tests python -m pytest -q -p no:cacheprovider \
  tests/test_plugins.py \
  tests/test_harness_plugin_catalog.py \
  tests/test_harness_plugin_permissions.py \
  tests/test_billion_context_adapter.py \
  tests/test_harness_usage_ledger_plugin.py
```

结果：`36 passed, 1 warning in 7.86s`，退出码 0。完整输出：`docs/evidence/oc_t11_targeted.log`。

测试环境为项目 `.venv`（Python 3.10.12，pytest 8.4.2）。`TMPDIR` 指向 `/dev/shm` 只是为了避开本机共享磁盘的高 I/O 负载，不改变被测代码或测试语义。

覆盖矩阵：

| 场景 | 覆盖位置 |
|---|---|
| 成功发现 | `test_discovery_returns_metadata_only_and_audits_each_decision` |
| 拒绝缺失字段/许可证/来源/权限 | `test_discovery_rejects_*`、`test_registration_rejects_*` |
| 撤销 | `test_discovery_skips_revoked_candidate`、`test_activation_gate_*` |
| 取消 | `test_discovery_cancellation_*`、`test_cancellation_*` |
| 网络失败 | `test_discovery_network_failure_*` |
| 激活撤权/摘要不一致 | `test_activation_gate_requires_exact_approval_*` |
| 压缩成功/失败/超时/取消/版本变化/回滚 | `test_billion_context_adapter.py` |
| 未知供应商用量不补零 | `test_harness_usage_ledger_plugin.py` |
| 审计持久化 | `test_discovery_*_audited`、`test_discovery_endpoint_*` |

### C5：可交付性与外部缺口

可交付：

- 两个插件的来源、固定版本、标签提交、许可证、SHA-256 和权限证据。
- 默认拒绝的受控发现 API、激活门禁和审计表。
- 可回滚、默认关闭的上下文压缩适配器边界。
- 区分压缩与回答阶段的用量账本。
- 合成边界测试和离线核验脚本。

未交付 / 仍阻塞：

1. **真实安装和加载两个插件**：没有在共享或生产 DSH profile 安装，没有执行第三方代码。需要所有者批准隔离 profile、指定 DSH 版本和回滚窗口。
2. **billion-context 真实压缩效果**：没有授权模型线路和真实插件执行，压缩比例、质量、供应商 Token 节省全部为 `not_measured`。合成基准不能替代。
3. **DSH 运行时集成**：本分支基线只包含 `OC-T3` 插件治理，不包含未合并的 `server/harness` 主线代码；真实插件加载和宿主压缩补丁冲突验证需要在包含 Harness 主线的隔离分支进行。
4. **许可证署名落地**：`billion-context` 的附加条款需要产品首页/文档/About 页署名方案；需要所有者或法务确认后再启用。
5. **dshmarket 远程目录**：没有把 `awesome-dsh-plugin.com`、npm 镜像或 `dshmarket.com` 目录接入运行时；任何远程目录内容都按不可信输入处理。
6. **真实客户数据和生产服务**：按任务边界未接触，未部署，未重启。

## 6. 回滚方式

- 代码回滚：在隔离分支 `git revert` 本节点提交，或在合并前丢弃该分支；未修改主工作树和共享看板代码。
- 运行时回滚：适配器默认关闭，`rollback()` 会关闭并清空缓存；没有任何生产 profile 被修改。
- 数据回滚：新增 `plugin_catalog_events` 表只用于审计，不影响既有表；如需回滚可保留审计记录，不删除证据。
- 插件回滚：本轮没有安装插件，因此不存在需要卸载的 profile 变更。

## 7. 复现命令汇总

```bash
# 1. 离线元数据核验（无本地归档）
python3 scripts/verify_harness_plugins.py \
  --plugins dshmarket,billion-context --format json --offline

# 2. 实际 SHA-256 核验（本地授权归档）
python3 scripts/verify_harness_plugins.py \
  --plugins dshmarket,billion-context --format json --offline \
  --artifact-dir /media/data3/dengkw/.runtime/open-claude-code/plugin-audit/artifacts

# 3. 合成边界基准（不执行插件）
python3 scripts/verify_harness_plugins.py \
  --plugins billion-context --format json --offline --benchmark

# 4. 定向测试
cd server
TMPDIR=/dev/shm/oc-t11-tests \
  /media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest -q \
  -p no:cacheprovider \
  tests/test_plugins.py \
  tests/test_harness_plugin_catalog.py \
  tests/test_harness_plugin_permissions.py \
  tests/test_billion_context_adapter.py \
  tests/test_harness_usage_ledger_plugin.py
```

## 8. 证据文件摘要

| 文件 | SHA-256 |
|---|---|
| `docs/evidence/oc_t11_targeted.log` | `aeea68c4378febc7b4058eb0db64831ae72a51c4f90d3ea097b077bf534daa04` |
| `docs/evidence/oc_t11_verify_artifacts.json` | `72450aaae9fb20affab44b244b1b63de19f7ca4c26f3417120404bee23c285a0` |
| `docs/evidence/oc_t11_verify_metadata.json` | `61e021103cf876ee322e2c4fc34b37608c5b581bfa81002d91de5815d0d400e7` |
| `docs/evidence/oc_t11_benchmark.json` | `6fd9f9c073da6e17a20b4e80f47fb19bfa82683107ac250c6fb22ea7bf5f9d5d` |

## 9. 风险与后续建议

- 不要把“元数据 verified”理解成“插件已接入”。当前状态是：发现层受控可交付，运行时接入未开始。
- `dshmarket` 的远程目录、安装和更新链路本身是高权限操作；即使未来接入，也必须保留管理员白名单、固定 tarball、摘要校验和人工确认。
- `billion-context` 会修改宿主压缩和重试路径，且管理端点默认无认证；如果未来评估，应该使用独立 profile、禁用自动更新/advisory 检查、仅绑定回环地址，并准备一键回滚。
- 建议下一步由所有者决定两件事：是否批准在隔离 DSH profile 安装固定版本做真实基准；是否接受 `billion-context` 的署名条款并在产品界面落地。
- 在获得批准前，本节点保持“发现与边界可交付、真实接入未完成”，不标记为完全完成。
