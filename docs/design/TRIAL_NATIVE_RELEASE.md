# 原生试用发行包：干净目录、可重复安装与验收合同

**日期：** 2026-11-12
**状态：** 设计生效，staging 合同已由自动测试覆盖；本文件不宣称任何最终发布或学校云验收已经通过。
**适用范围：** 学校云原生安装（不使用 Docker；现有 Docker 无法运行默认 Harness）。腾讯云、共享常驻服务、客户数据和付费模型均不在本任务范围内。

本版打包器**只声明 integrity-verified staging**。完整的产品运行时验收聚合合同尚未实现，因此 `release_ready` 和 `publishable` 永远为 `false`；JSON 验收报告和授权字符串只作审计证据，不能让包变成可发布状态。任何声称可发布的 manifest 都会被验证器拒绝。

## 1. 为什么需要这份合同

现有 `scripts/build_release.py` 只生成 client / server / deployment 三个归档和归档摘要。它存在三个不能用于“独立试用”的问题：

1. `server/build/lib` 等构建副本会被当成源码收进 server 包，旧副本可能覆盖或误导安装者。
2. 交付脚本和文档没有覆盖 Harness 固定源码校验、真实握手、试用烟囱和容量压测，也没有试用范围/门禁和上游来源清单。
3. 归档摘要只能证明“压缩包没坏”，不能证明“测试对象就是发布对象”，也不能证明本次源码、测试证据和授权是绑定的。

因此新增 `scripts/trial_release_manifest.py` 和 `scripts/trial_release_install.py`，把发行包变成一个**可逐文件核对、可失败关闭、默认不可发布**的 staging 目录；能力清单只作为显式候选范围绑定，不在包内声明任何已启用功能。

## 2. 包内内容和明确排除项

### 2.1 包内内容

- `client/`：源码、锁文件、构建配置和 `client/dist` 静态成品。`client/dist` 是唯一允许进入包的 `dist`。
- `server/`、`examples/`：服务端源码、测试和示例；不包含任何构建副本。
- `deployment`：README、compose 示例、环境变量样例、API/操作/工作区文档、历史验收记录、审阅过的截图、Harness 校验/握手/烟囱/容量脚本和本发行合同。
- `STAGING_README.md`：构建时生成的 staging 封面。它明确写出本包是 `integrity-verified-staging`、`release_ready=false`、`publishable=false`、`product_runtime_verified=false`，并逐项列出包内原样保留的历史 `FINAL` 文档；历史文档不会被改写或删除，但读者必须先看到这份封面，不能把历史 `0.4.2 FINAL` 误当成本次结论。
- `licenses/`：DeepSeek Harness MIT 许可证、HippoRAG MIT 许可证，归档名分别为 `licenses/deepseek-harness-MIT.txt` 和 `licenses/HippoRAG-MIT.txt`。
- `trial/capabilities.json`：构建时显式输入的候选能力清单，逐字节进入包并参与源码树摘要；它只描述候选范围，既不打开运行时能力，也不验证运行时封禁。
- `docs/design/TRIAL_CAPABILITY_POLICY.md`：受控试用能力边界；候选清单的 `policy_sha256` 必须与包内这份文件一致。
- `fixtures/`：只允许显式选择的合成/公开元数据。本版选择：
  - `fixtures/synthetic-industrial-v1/manifest.json`
  - `fixtures/synthetic-industrial-v1/answers.json`
  - `fixtures/industrial-public-v2/source-catalog.json`
  - `fixtures/industrial-public-v2/README.md`

本版**不携带知识库正文、图片、PDF、数据库或客户资料**。公开样本必须由使用者在正常知识库接口中自行导入；包内元数据只用于说明来源、许可和验收口径。

### 2.2 明确排除项

以下内容即使出现在源码树里也不会进入包；如果某个显式文件命中秘密标记，构建直接失败关闭：

- `server/build/lib`、任意 `build/`、`.tox`、`.nox`、`.eggs`、`htmlcov`、`site-packages`、`.pytest_cache`、`node_modules`、`.venv`；
- 除 `client/dist` 以外的任何 `dist/`；
- `.env`（`.env.example` 除外）、管理员密码、provider 设置、token、凭据、私钥；
- `.db`、`.sqlite`、`.sqlite3`、`.log`、`.jsonl`、`.pem`、`.key`、`.p12`、`.pfx`、`*.egg-info`；
- `.runtime/`、`data/`、`internal/`、`.local/`、`.git/`；
- 真实客户数据、客户 MCP 地址/凭据、模型权重。

## 3. 固定依赖与来源

| 组件 | 固定值 | 校验方式 |
| --- | --- | --- |
| DeepSeek Harness | 官方仓库提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` | `setup_harness_runtime.py` 要求 Git 工作树、HEAD 等于该提交、工作树干净；`docs/upstream-sources.json` 记录同一提交 |
| Harness 许可证 | MIT，SHA-256 `ebb4f09972aee8608be255debaf78451a68e95c290f55c240dec2ecfa16ea6be` | 包内 `licenses/deepseek-harness-MIT.txt` 与来源清单逐字节核对 |
| Node | `24.9.0`，SHA-256 `f52ec50e959d72d5c680d9731420b2661cd2a8070e94c7369b6ddfcd8b7278be` | `setup_harness_runtime.py` 下载校验；安装器预置缓存时再次校验 |
| pnpm | `11.28.5`，SHA-512（base64）`SEVj0wtMKtJ8tIqpT/KzAtvOvI/2vyiBbCWJiG4UbOf3bzVPO8GkOOb16XaMMCqgpvet2CMpA81M+BQmy8FNng==` | 同上 |
| HippoRAG | MIT，SHA-256 `93b43d692b033b76129504448695bfe76ef22d18b11e51352bfab4b5622e5aaa` | 包内许可证与 `docs/upstream-sources.json` 核对 |

允许复用已经验证过的公开只读缓存（Harness 源码 Git 检出、Node/pnpm 下载包、pnpm store 的内容寻址载荷），但目标必须是一个全新的独立私有目录。pnpm store 仅在通过版本化布局、内容寻址文件名、无符号链接、无私有后缀和内容摘要校验后才允许复用；其 `projects/` 注册表永不复制。安装器还要求显式提供 `--pnpm-store-expected-manifest-sha256`，把调用路径和内容清单摘要一起钉死；每个内容寻址文件名必须与其内容摘要一致（64 位十六进制名按 SHA-256 校验，128 位十六进制名按 SHA-512 校验；pnpm v11 的实际 store 使用 SHA-512 的 `files/<前2位>/<后126位>` 布局）。缺少摘要、摘要格式错误、摘要不符或出现其他长度的名字都失败关闭。不得把学校云 `.runtime`、账号库、管理员密码或私有 provider 配置复制进去。

## 4. 构建 staging 候选

构建必须显式提供一个候选能力清单 JSON，不能依赖脚本内硬编码。清单格式为 `trial-capability-candidates/v1`：

```json
{
  "schema": "trial-capability-candidates/v1",
  "mode": "trial",
  "default_when_unlisted": "disabled",
  "policy_sha256": "<docs/design/TRIAL_CAPABILITY_POLICY.md 的 SHA-256>",
  "candidates": [
    {"id": "login_and_workspace", "state": "candidate", "scope": "..."},
    {"id": "mcp_business_connections", "state": "disabled", "scope": "..."}
  ]
}
```

规则：`state` 只允许 `candidate` 或 `disabled`，`enabled` 会被直接拒绝；没有列出的能力一律按 `disabled` 处理。清单中的每一项在 manifest 里都标记 `release_open: false`、`runtime_enforcement_verified: false` 和 `runtime_state: unverified`：这只表示“本包没有打开它，也没有验证运行时是否封禁”，**不表示实际接口已经被封禁**。运行时是否开放仍由部署策略和逐项验收决定。清单的 SHA-256 和规范化内容写入 manifest，并且清单文件以 `trial/capabilities.json` 逐字节进入候选包、参与 `source_tree_sha256`。清单的 `policy_sha256` 与包内能力边界文档不一致时构建失败关闭。

在项目源码树中执行：

```bash
cd /media/data3/dengkw/projects/open-claude-code
TMPDIR="$PWD/.runtime/tmp" python3 scripts/trial_release_manifest.py build --version 0.4.2 \
  --capabilities .runtime/trial-release-staging/capabilities-20261112.json
```

输出目录默认是 `dist/trial-releases/0.4.2-staging/`。如果目录已存在，命令拒绝覆盖；请改用新的 `--output`，避免任何隐式删除。

manifest 至少包含：

- `source_control`：真实 `HEAD`、分支、是否 dirty、逐条 dirty 清单和数量。不伪称当前工作树是冻结提交。
- `staging_notice`：生成的 `STAGING_README.md` 的路径、摘要、staging 声明、历史 `FINAL` 文档清单，以及 `historical_documents_modified=false`。验证器会重算这份清单并拒绝被篡改或缺失的封面。
- `build_environment`：除工具链版本外，记录 `private_runtime_access_scope=license_text_only`、`private_runtime_license_inputs`、`private_runtime_credentials_used=false`、`private_runtime_code_or_data_read=false`、`private_runtime_code_or_data_copied=false`。构建只从 `.runtime/third_party` 读取两个固定 MIT 许可证文本，不再使用容易被误读的 `private_runtime_used=false` 字段。
- `source_tree_sha256`：按“路径 + NUL + 文件 SHA-256 + 换行”排序计算的源码树摘要；除 `manifest.json` 和 `evidence/` 外，所有包内源码都参与。
- `source_files` / `files`：逐文件路径、字节数、SHA-256 和分组。
- `pinned`：Harness 提交、Node/pnpm 版本与摘要、许可证摘要、HippoRAG 许可证摘要。
- `licenses`、`fixtures`：许可证和每个显式 fixture 的来源、许可、摘要和 `customer_data=false` 断言。
- `capabilities`：候选清单来源、`input_sha256`、`policy_sha256`、规范化候选条目、`default_when_unlisted=disabled`、空的 `release_open`、`runtime_state=unverified`、`runtime_enforcement_verified=false`，以及“候选不打开能力，也不验证运行时封禁”的声明；不再有硬编码的 `enabled` 功能表。
- `acceptance`：引用的验收报告、报告摘要、报告声明的 `source_tree_sha256` 以及是否绑定。
- `gates`：源码、排除项、许可证、固定依赖、fixture、能力候选、staging 封面、验收证据、可发布声明（blocked）和授权（recorded_not_sufficient 或 not_requested）十类门禁。
- `install_contract` 和 `rollback`：新目录、独立端口、不切换现有环境；安装命令和回滚步骤。`install_contract.scope=harness_handshake_only`，并明确 `product_runtime_verified=false`、安装器必须接收可信的 `expected_source_tree_sha256`、复制后必须先逐字节验证目标再执行任何目标代码。

manifest 使用固定排序、固定缩进、UTF-8 且不带构建时间戳，因此相同输入重复构建得到逐字节相同的 `manifest.json`。验收报告只进入 `evidence/`，不参与 `source_tree_sha256`，避免“先有包还是先有报告”的循环依赖。

## 5. 验收证据的定位：只作审计，不产生可发布标记

验收报告必须是 JSON，至少包含：

```json
{
  "schema": "trial-release-acceptance/v1",
  "source_tree_sha256": "<候选包 manifest 中的源码树摘要>",
  "status": "PASS",
  "tests": [{"name": "...", "status": "PASS", "command": "...", "exit_code": 0}],
  "environment": {"server": "学校云", "provider_calls": 0}
}
```

规则：

1. **显式**通过 `--acceptance` 提供的报告必须 `status=PASS` 且 `source_tree_sha256` 与本次候选包完全一致；否则构建失败关闭。缺省路径 `docs/evidence/TRIAL_PACKAGE_2026-11-12.json` 只有在存在且已绑定时才会被收进 `evidence/`；陈旧报告会被跳过，不会被当作本次证据。
2. 报告只进入 `evidence/`，不参与 `source_tree_sha256`，也不会改变 `release_ready` 或 `publishable`。它们的作用是留存审计线索，不是发布通行证。
3. `--stage final` 在当前版本被明确拒绝并返回非零退出码。完整的产品运行时验收聚合合同（逐项能力、真实产品启动、浏览器/容量/供应商调用等）尚未实现，因此本打包器不提供 final 或 publishable 状态。
4. 即使调用方传入 `--authorized-by`，manifest 也只记录 `authorization_effect: none`；授权字符串不能把包变成可发布状态。
5. 任何 manifest 只要出现 `release_ready=true`、`publishable=true`、`stage=final`、`status` 不是 `integrity-verified-staging`，或 `claim_scope` 不是 `integrity_verified_staging`，验证器都会失败关闭。

最终候选应在琥珀修复 runtime/main 等源码、工作树重新冻结，并且完整验收聚合合同落地后，由 L2 触发重跑；本任务不生成、也不宣称最终候选。

## 6. 验证候选包

```bash
TMPDIR="$PWD/.runtime/tmp" python3 scripts/trial_release_manifest.py verify \
  --package dist/trial-releases/0.4.2-staging \
  --expected-source-tree-sha256 "<可信渠道取得的源码树摘要>"
```

验证器不信任 manifest 里的结论，而是重新计算：

- 包内实际文件集合必须与 `files` 完全一致；多一个文件、少一个文件都失败。
- `STAGING_README.md` 必须存在、摘要一致，并包含 staging 声明；manifest 记录的历史 `FINAL` 文档清单必须与实际包内文档一致，且 `historical_documents_modified=false`。
- `build_environment` 必须声明 `private_runtime_access_scope=license_text_only`、许可证输入路径与固定值一致，并且凭据、私有代码或数据都没有被读取或复制；任何 `private_runtime_used=true` 的自相矛盾声明都失败。
- 每个文件的字节数和 SHA-256 必须一致。
- 重新计算 `source_tree_sha256` 和 `source_files`，必须与 manifest 一致；如果提供了 `--expected-source-tree-sha256`，还必须与这个可信摘要完全一致。
- 实际内容必须通过秘密标记、数据库/日志/密钥后缀、构建副本和私有路径扫描。
- 包内 `docs/upstream-sources.json` 的 Harness/HippoRAG 提交和许可证摘要必须与固定值一致。
- `fixtures/` 只能出现显式选择清单中的文件，且每个都声称 `customer_data=false`。
- `trial/capabilities.json` 的摘要、规范化候选条目和 `policy_sha256` 必须与 manifest 一致；`release_open` 必须为空，任何 `enabled` 声明、未知状态、`runtime_enforcement_verified=true` 或非 `unverified` 的运行时状态都失败。
- `evidence/` 中的验收报告摘要、源码树绑定和 `PASS` 状态必须与 manifest 一致；报告只是审计证据，不改变发布状态。
- `release_ready` / `publishable` 必须为 `false`，`stage` 必须是 `staging`，`status` 必须是 `integrity-verified-staging`；任何可发布声明都失败。
- `build_environment.product_runtime_installed` 和 `install_contract.product_runtime_verified` 必须为 `false`，`install_contract.scope` 必须为 `harness_handshake_only`；任何试图把 staging 包升级成完整产品运行时的声明都失败。
- 顶层或 `release` 块中不得新增真值的 `product_runtime_verified` / `product_runtime_installed` 声明；这类旧版或手工篡改字段同样失败关闭。
- `gates` 必须包含且只包含约定的十类门禁。前七类硬门禁必须为 `pass`，`acceptance_binding` 必须与实际验收证据绑定结果一致，`publishable_claim` 必须永远为 `blocked`，授权门禁不得表示“授权已充分”。
- `acceptance.effect` 必须继续以 `advisory_only` 开头，`acceptance.bound` 和 `release.acceptance_evidence_recorded` 必须与重新计算的验收证据绑定结果一致。

退出码：`0` 通过 staging 合同；`1` 失败；参数错误由 argparse 返回 `2`。

## 7. 干净目录安装合同

安装器默认只输出计划，只有显式 `--execute` 才真正写入：

```bash
TMPDIR="$PWD/.runtime/tmp" python3 scripts/trial_release_install.py \
  --package dist/trial-releases/0.4.2-staging \
  --target /media/data3/dengkw/projects/open-claude-code-trial/fresh-20261112 \
  --harness-source .runtime/third_party/deepseek-harness \
  --toolchain-cache .runtime/harness-toolchain/downloads \
  --pnpm-store .runtime/harness-toolchain/pnpm-store \
  --pnpm-store-expected-manifest-sha256 "<可信渠道取得的 store 内容清单 SHA-256>" \
  --expected-source-tree-sha256 "<可信渠道取得的源码树摘要>" \
  --execute
```

`--execute` 必须提供 `--expected-source-tree-sha256`；这个值必须来自包外的可信渠道（例如构建报告或 L2 的书面记录），不能只从待安装包自己的 `manifest.json` 读取，否则无法发现包和 manifest 一起被替换的情况。

执行顺序：

1. 先运行 `verify_trial_release`，并把包摘要与可信的 `expected_source_tree_sha256` 比较；包不合格或摘要不符直接停止。
2. 目标必须是不存在或空的全新目录；不允许指向包自身或已有服务目录。
3. 把包内容复制到目标目录。
4. **在任何目标代码执行之前**，逐文件比较复制后的目标与包：路径集合必须完全一致，不能多、不能少；每个文件的字节内容和 SHA-256 必须一致；目标中不允许符号链接；重新计算的源码树摘要必须等于可信的 `expected_source_tree_sha256`。任何一项不符立即失败，并写入 `install-evidence.json`。
5. 从已验证的本地 Git 检出（或官方仓库固定提交）克隆 Harness 到 `<目标>/.runtime/third_party/deepseek-harness`，检出固定提交并确认工作树干净、许可证摘要一致。
6. 预置 Node/pnpm 下载缓存并校验摘要；pnpm store 必须同时提供 `--pnpm-store-expected-manifest-sha256`。只允许复用经过严格校验的版本化内容寻址载荷（`vN/files/<两位十六进制>/<十六进制摘要>` 和 `vN/index.db*`），每个名字都必须与内容摘要一致（64 位十六进制名对 SHA-256，128 位十六进制名对 SHA-512）；`projects/` 注册表、任意符号链接、非内容寻址文件名、私有后缀、store 根目录多余条目、缺少摘要或摘要不符全部失败关闭；复制后再次计算内容清单摘要，必须与钉死的期望值一致。
7. 在目标目录创建隔离的 `.runtime/venv`，记录**未解引用**的 `.runtime/venv/bin/python` 路径，并用该解释器自身输出的 `sys.prefix`、`sys.base_prefix` 和 `sys.executable` 证明隔离；按固定版本和 SHA-256 下载并安装握手所需的 pydantic wheel；wheel 集合或摘要不符时失败关闭，不从主项目虚拟环境借用任何包。安装后再探测一次，要求 `pydantic` 从目标 venv 内导入。
8. 使用最小环境变量（不继承学校云凭据、代理和用户 npm 配置）执行，且每次目标 Python 执行都设置 `PYTHONDONTWRITEBYTECODE=1`：
   - 系统 Python：`python3 scripts/setup_harness_runtime.py --install --prepare-source`
   - 目标隔离虚拟环境：`.runtime/venv/bin/python scripts/verify_harness_runtime.py`
   - 可选：`.runtime/venv/bin/python scripts/verify_harness_sdk_protocol.py`
   - 可选：`python3 scripts/verify_harness_plugins.py --offline`（缺少 FastAPI 等产品依赖时记录为已知缺口，不隐藏失败，也不写 `.pyc`）
9. `verify_harness_runtime.py` 必须返回 `status=PASS` 且 `provider_requests=0`；真实 Harness 进程必须来自目标内 Node，并且干净退出。
10. 任何一步失败，安装器把精确命令、退出码、输出尾部写入 `<目标>/.runtime/install-evidence.json`，不继续、不重试、不掩盖。证据必须记录实测的 `target_source_tree_sha256` 和 `target_pre_execution_verified=true`。全部执行结束后，安装器再次测量目标中 `.runtime` 之外的源码树摘要，记录 `target_source_tree_sha256_after_execution` 和 `target_tree_unchanged_after_execution`；出现任何 `.pyc` 或其他漂移都失败关闭。证据同时记录 `bytecode_policy`，说明执行前摘要、执行后摘要以及漂移的含义。

安装范围边界：`setup_harness_runtime.py` 只使用 Python 标准库，因此用最小环境里的系统 `python3` 执行。`verify_harness_runtime.py` 会通过官方 Harness Python SDK 真实导入 `pydantic`，因此安装器会在目标目录创建隔离的 `.runtime/venv`，只安装版本和 SHA-256 都固定的 pydantic 及传递依赖（`pydantic==2.14.2`、`pydantic-core==2.50.2`、`annotated-types==0.8.0`、`typing-extensions==4.16.0`、`typing-inspection==0.4.5`，本机 Python 3.10 wheel），并记录 `pip freeze`、wheel 摘要、**未解引用的** `.runtime/venv/bin/python` 路径、该解释器实测的 `sys.prefix`、`sys.base_prefix`、`sys.executable` 和 `pydantic` 文件路径。只有 `sys.prefix` 等于目标 venv、`sys.base_prefix` 不同于它、且 `pydantic` 从目标 venv 内导入才判定隔离成立。它**不**从主项目 `.venv` 借用环境，也不安装 FastAPI 等完整产品依赖。完整产品运行需要在目标目录另行安装 `server/pyproject.toml` 依赖，这是本次未完成的缺口，不能用握手成功冒称完整产品安装成功。

安装完成后，目标目录是独立私有目录：`.runtime` 下的 tmp、cache、data、config、home、toolchain 都指向目标自身。除经过严格布局和内容摘要校验的 pnpm 内容寻址缓存外，不读取源项目的私有 `.runtime`；pnpm 的 `projects/` 注册表不会被复制。

## 8. 回滚

- 候选包安装在全新目录、使用独立回环端口，不替换现有服务、不迁移现有数据、不修改现有入口。
- 任何失败：停止候选进程、保留 `install-evidence.json`、删除候选目录即可；旧服务继续运行。
- 本任务**不执行任何环境切换**。学校云验收通过并经用户书面批准之前，不讨论腾讯云入口切换；腾讯云不在本任务范围内。

## 9. 已知限制

- 当前主工作树有大量未提交改动；manifest 会如实记录 `HEAD 38c0a04` 和 dirty 清单，不能当作冻结提交。
- 本版只带合成/公开元数据，不带知识库正文；真实文档解析、RAG 质量和客户业务正确率不在本包结论内。
- 包内 `STAGING_README.md` 是生成的 staging 封面；历史 `DELIVERY_*` 文档保持字节原样，其中的 `FINAL` 只代表过去版本，不代表本次候选已发布或已验收。
- 构建读取 `.runtime/third_party` 的范围仅限两个固定 MIT 许可证文本；不读取或复制私有代码、账号库、管理员密码、供应商配置、凭据、客户数据或模型权重。
- pnpm store 复用必须同时钉死内容清单 SHA-256；内容寻址名与内容摘要不一致、缺少钉死摘要或出现不支持的名字长度都会失败关闭。pnpm v11 实际使用 SHA-512 名字，因此校验会按名字长度选择 SHA-256 或 SHA-512，而不是盲信单一算法。
- 每次目标 Python 执行都设置 `PYTHONDONTWRITEBYTECODE=1`；安装器在执行后重新测量 `.runtime` 之外的源码树，任何 `.pyc` 或其他漂移都会使安装失败。注意：目标在安装完成后如果被人工再次执行且没有沿用该环境变量，仍可能自然漂移；对目标做额外验证时必须使用同一变量。
- 能力清单只记录候选范围；本包既不打开能力，也不验证运行时封禁。`runtime_state=unverified`、`runtime_enforcement_verified=false` 是事实记录，不代表实际接口已经被封禁。未知和未验收能力按部署策略默认不开放，候选不等于已验收或已开放。
- 本打包器只声明 `integrity-verified-staging`，没有 final 或 publishable 状态；完整产品运行时验收聚合合同未实现，不能用验收报告或授权字符串伪造发布通过。
- 本任务只验证 Node/Harness 工具链与零供应商握手，握手依赖装在目标隔离的 `.runtime/venv` 中；不安装或验证 FastAPI 产品运行时。完整产品运行需要在目标目录按 `server/pyproject.toml` 安装完整依赖，这是尚未完成的缺口。
- Harness 安装依赖 Git 工作树，散文件复制无法通过 `setup_harness_runtime.py` 校验。
- `trial_release_install.py` 的完整执行需要批准重安装窗口；未执行时只记录精确计划和缺口，不冒称干净安装通过。
- 真实模型调用、供应商计费、浏览器验收和企业容量压测不由本包自动执行；需要单独授权和证据。
- HippoRAG 的 igraph 依赖采用 GPL，当前仅限隔离开发诊断；发行包只包含其 MIT 许可证和来源说明，不分发 HippoRAG 运行环境。
