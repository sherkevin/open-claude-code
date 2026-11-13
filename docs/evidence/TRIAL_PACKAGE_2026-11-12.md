# 原生试用发行包证据（2026-11-12，OC-T14 整改后）

**服务器：** 学校云
**任务：** OC-T14 可复现独立试用发行包整改窗口
**结论：** `integrity-verified-staging` 通过；**不是**可发布版本，`release_ready=false`、`publishable=false`、`product_runtime_verified=false`；也**不是可交付客户的试用包**。
**范围：** 包完整性、排除项、固定工具链、显式候选能力、全新私有目录复制、复制后逐字节复验、真实 Node/Harness 零供应商握手，以及独立审核提出的 F1 至 F5 五项整改。不包含 FastAPI 产品运行时、浏览器验收、真实模型调用和容量压测。

## 1. 交付物

- 候选包：`.runtime/trial-release-staging/final-20261112-v3`
- 源码树摘要：`871060968c542a0b267c04e29374e7e07343d4eabaec4327dca69a286bc7eb0d`
- manifest 摘要：`16240ff7e86e236c9bce9751391704906c7b8e65c3485e1d3e2ece814b9eb751`
- 包内文件数：329 个列出的文件（另有 `manifest.json`，安装复验时目标共 330 个文件条目）
- 源码控制：HEAD `38c0a04`，分支 `main`，工作树 dirty，250 条未提交/未跟踪条目；manifest 如实记录，不伪称冻结提交。
- 全新安装目标：`/media/data3/dengkw/projects/open-claude-code-trial-install-20261112f`
- 安装证据：`<目标>/.runtime/install-evidence.json`
- SDK 协议证据：`<目标>/.runtime/sdk-protocol-evidence.json`

说明：原 `final-20261112-v2` 包保留但不删除，也不再用它代表新源码；本文件的结论只对 v3 包和上述源码树摘要成立。v3 的源码树已经包含本轮 F1 至 F5 的代码修改，因此必须使用新的目录和新的摘要。

## 2. 独立审核五项整改（F1 至 F5）

1. **F1：历史 FINAL 文档不再误导。** 包内新增构建时生成的 `STAGING_README.md` 封面，明确写出本包是 `integrity-verified-staging`、`release_ready=false`、`publishable=false`、`product_runtime_verified=false`，并逐项列出包内原样保留的历史 FINAL 文档：`docs/DELIVERY_VERIFICATION.md`、`docs/verification/DELIVERY_0_1_0.md`、`DELIVERY_0_2_0.md`、`DELIVERY_0_3_0.md`、`DELIVERY_0_4_0.md`、`DELIVERY_0_4_1.md`。历史文档本身没有被改写或删除；manifest 记录 `historical_documents_modified=false`，验证器会重算清单并拒绝被篡改或缺失的封面。
2. **F2：私有运行时字段改用准确表述。** 删除容易被误读的 `private_runtime_used=false`，改为 `private_runtime_access_scope=license_text_only`、`private_runtime_license_inputs`、`private_runtime_credentials_used=false`、`private_runtime_code_or_data_read=false`、`private_runtime_code_or_data_copied=false`，并补充说明：构建只读取 `.runtime/third_party` 下两个固定 MIT 许可证文本，不读取或复制私有代码、账号库、管理员密码、供应商配置、凭据、客户数据或模型权重。验证器拒绝任何自相矛盾的 `private_runtime_used=true` 或错误字段。
3. **F3：虚拟环境证据记录真实解释器并证明隔离。** 安装器现在记录**未解引用**的 `.runtime/venv/bin/python` 路径，同时记录其解引用结果、以及该解释器自身输出的 `sys.prefix`、`sys.base_prefix`、`sys.executable`。只有 `sys.prefix` 等于目标 venv、`sys.base_prefix` 不同于它、并且安装后 `pydantic` 从目标 venv 内导入，才判定隔离成立。实测：`interpreter=/media/data3/dengkw/projects/open-claude-code-trial-install-20261112f/.runtime/venv/bin/python`（未解引用），`interpreter_resolved=/usr/bin/python3.10`，`sys_prefix` 为目标 venv，`pydantic 2.14.2` 从目标 venv 的 `site-packages` 导入，`isolation_verified=true`。不再把系统 Python 误解为 venv 证据。
4. **F4：pnpm store 来源钉死并校验内容寻址。** 安装器新增必填参数 `--pnpm-store-expected-manifest-sha256`；缺少该参数、摘要格式错误、摘要不符、或提供摘要却没有 store，都失败关闭。复制前和复制后都会重新计算内容清单摘要，并逐个校验内容寻址文件名与其内容摘要一致：64 位十六进制名按 SHA-256，128 位十六进制名按 SHA-512。pnpm v11 的真实 store 使用 SHA-512 的 `files/<前2位>/<后126位>` 布局，因此不能用盲目的单一 SHA-256 规则；本轮实测 67,815 个内容文件全部通过 SHA-512 内容校验。`projects/` 注册表永不复制。
5. **F5：目标执行禁止写 pyc 并记录执行前后摘要。** 每次目标 Python 执行都显式设置 `PYTHONDONTWRITEBYTECODE=1`，包括 Harness 握手、可选的 `verify_harness_plugins.py` 和 SDK 协议验证。安装器在全部执行结束后重新测量 `.runtime` 之外的源码树摘要，记录 `target_source_tree_sha256_after_execution`、`target_tree_unchanged_after_execution` 和 `target_after_execution`；出现任何 `.pyc` 或其他漂移都失败关闭。本轮执行前后摘要均为 `871060968c...`，`bytecode_files=[]`，漂移为零。插件验证以退出码 1 记录为 `BLOCKED_PRODUCT_RUNTIME_DEPENDENCY`（缺少 FastAPI），这是已知缺口，不是隐藏的通过。

## 3. 自动测试

```bash
TMPDIR="$PWD/.runtime/tmp" .venv/bin/python -m pytest server/tests/test_trial_release_manifest.py -q
```

结果：**37 passed**（2026-11-13 H1 整改后；第二轮审核时基线为 **28 passed**）。覆盖：

- 构建副本排除、`client/dist` 保留、秘密/数据库/日志/密钥不打包；
- 缺依赖、错许可证哈希、验收报告未绑定源码树时失败关闭；
- manifest 确定性、源码变更使摘要失效；
- 可发布声明无条件拒绝，fixture PASS 报告不能伪造发布通过；
- 能力清单必须显式输入、未知/未验收默认关闭、`enabled` 拒绝、policy 摘要绑定；
- 运行时状态不得声称已验证封禁；
- 目标复制后复验拒绝额外文件、篡改内容和符号链接，可信摘要不匹配失败；
- pnpm store 拒绝符号链接、私有文件名和根目录多余条目，只复制内容寻址载荷；
- 新增：staging 封面生成与历史 FINAL 文档不改写、封面篡改被拒绝；
- 新增：`build_environment` 的 license-only 字段准确，矛盾声明被拒绝；
- 新增：venv 证据记录未解引用路径、用 `sys.prefix` 证明隔离，非隔离解释器被拒绝；
- 新增：pnpm store 必须带摘要钉死、内容寻址名与内容不一致时失败关闭；
- 新增：`PYTHONDONTWRITEBYTECODE=1` 存在，执行后 `.pyc` 漂移会被检测。
- 新增：恶意 manifest 不能把 `build_environment.product_runtime_installed`、`install_contract.product_runtime_verified`、安装范围、顶层/发布块产品运行时声明、`publishable_claim` 门禁或未绑定验收门禁升级成通过或完整运行时。
- 新增：门禁清单必须无重复且恰好覆盖约定的十类门禁；验收绑定状态和 `acceptance.effect=advisory_only` 会重新核对。

现有 `scripts/build_release.py --check-only` 退出码为 `0`。

## 4. 全新目录安装与握手

安装命令（节选）：

```bash
TMPDIR="$PWD/.runtime/tmp" .venv/bin/python scripts/trial_release_install.py \
  --package .runtime/trial-release-staging/final-20261112-v3 \
  --target /media/data3/dengkw/projects/open-claude-code-trial-install-20261112f \
  --harness-source .runtime/third_party/deepseek-harness \
  --toolchain-cache .runtime/harness-toolchain/downloads \
  --pnpm-store .runtime/harness-toolchain/pnpm-store \
  --pnpm-store-expected-manifest-sha256 2e097236c48cccb3040ac04e0217f9dbc97bd9a5783475412eff530eed825b89 \
  --expected-source-tree-sha256 871060968c542a0b267c04e29374e7e07343d4eabaec4327dca69a286bc7eb0d \
  --run-plugin-verify \
  --execute
```

结果：退出码 `0`，`install-evidence.json` 的 `status=PASS`，开始时间 `2026-11-12T15:24:29.784996+00:00`，结束时间 `2026-11-12T15:28:38.569094+00:00`。

关键证据：

- `package_source_tree_sha256`、`expected_source_tree_sha256`、`target_source_tree_sha256` 三者一致，均为 `87106096...`；
- `target_pre_execution_verified=true`；复制后复验检查了 330 个文件条目，`extra_files=0`、`symlinks_in_target=0`；
- `clone_harness`、`checkout_pin`、`create_handshake_venv`、`download_handshake_wheels`、`install_handshake_wheels`、`check_handshake_pydantic`、`freeze_handshake_environment`、`setup_harness_runtime`、`verify_harness_runtime` 全部退出码 `0`；
- 安装检查项 `target_copy_verified_before_execution`、`python_runtime_scope`、`harness_worktree_pin`、`toolchain_cache_verified`、`pnpm_store_seeded`、`isolated_handshake_venv`、`handshake_wheels_verified`、`handshake_pydantic_import`、`real_harness_zero_provider_handshake`、`post_execution_source_tree_unchanged` 全部为 `PASS`；`plugin_verification_execution` 为 `BLOCKED`（缺少 FastAPI，已知缺口）；
- `verify_harness_runtime.py`：`status=PASS`，`source_commit=fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`，`real_runtime_started=true`，`provider_requests=0`，真实 Node 是目标内 `v24.9.0`，干净退出；
- `verify_harness_sdk_protocol.py`：`status=PASS`，`scope=fake-runtime-only`，`source.commit=fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`，`model_calls=0`；SDK 验证后再次测量目标源码树，仍然 `unchanged=true`、`bytecode_files=[]`；
- 目标隔离虚拟环境：Python `3.10.12`，只安装固定校验的 `pydantic==2.14.2`、`pydantic-core==2.50.2`、`annotated-types==0.8.0`、`typing-extensions==4.16.0`、`typing-inspection==0.4.5`；`pip freeze` 已记录；没有使用主项目 `.venv`；
- `virtualenv` 是主机构建工具（用户站点，非主项目虚拟环境），仅用于创建目标隔离虚拟环境；目标环境内容全部来自固定 wheel；
- pnpm store 校验：`manifest_sha256` 与 `expected_manifest_sha256` 均为 `2e097236...`，`manifest_pin_bound=true`、`manifest_match=true`，67,815 个内容文件全部通过内容寻址校验，`projects_registry_copied=false`、`symlinks_in_payload=0`；
- `private_runtime_copied=false`、`environment_switch_performed=false`。

## 5. 已知缺口（不掩饰）

- **产品运行时未安装。** 目标目录没有 FastAPI、数据库驱动等 `server/pyproject.toml` 完整依赖，也没有启动产品或执行浏览器/接口验收。`product_runtime_verified=false`。不能称为产品运行时全依赖或可浏览器启动。
- **不是客户试用包。** 本包只完成 staging 完整性和零供应商握手，不是可交付客户的试用包；客户试用还需要完整产品依赖、浏览器验收、真实模型和容量证据及单独授权。
- **插件校验失败。** 在目标内运行 `scripts/verify_harness_plugins.py` 退出码 `1`，错误为 `ModuleNotFoundError: No module named 'fastapi'`；这是完整产品依赖缺口，不冒称通过。该执行已使用 `PYTHONDONTWRITEBYTECODE=1`，没有产生 `.pyc`，也没有导致目标源码树漂移。
- **构建时验收报告未绑定。** 构建 v3 时默认验收报告仍绑定旧源码树摘要 `91db207b...`，被打包器按失败关闭规则跳过，manifest 为 `acceptance.bound=false`、`reports=[]`。本文件是构建后的外部审计证据，绑定 v3 源码树摘要 `87106096...`，但不随包分发，也不产生发布效果。
- **完整验收聚合未实现。** 本打包器没有产品运行时验收聚合合同，因此永远不生成 final/publishable 状态；`release_ready` 和 `publishable` 永远为 `false`。
- **运行时能力封禁未验证。** 候选清单只是范围记录，`runtime_state=unverified`，没有验证实际接口是否被封禁。
- **真实模型、容量和浏览器验收未执行。** 这些需要单独授权、供应商调用和证据，不在本次零供应商握手范围内。

## 6. 启动与回滚合同

- 安装目标是全新的独立私有目录，使用独立回环端口；不替换现有服务、不迁移现有数据、不修改现有入口。
- 任何失败只停止候选进程、保留 `install-evidence.json`、删除候选目录；旧服务继续运行。
- 本任务不执行任何环境切换，不接触腾讯云，不发布，不使用客户数据/凭据/付费模型。
- 完整产品启动需要在目标目录另行安装 `server/pyproject.toml` 依赖；本次没有也不能用主项目虚拟环境冒称完整安装。

## 7. 修改文件

- `scripts/trial_release_manifest.py`
- `scripts/trial_release_install.py`
- `server/tests/test_trial_release_manifest.py`
- `docs/design/TRIAL_NATIVE_RELEASE.md`
- `docs/evidence/TRIAL_PACKAGE_2026-11-12.md`
- `docs/evidence/TRIAL_PACKAGE_2026-11-12.json`

本轮没有修改 `scripts/build_release.py`，也没有重置或回退其他负责人的改动。

## 8. 结论边界

这是一份 **staging 完整性 + 零供应商 Harness 握手 + F1 至 F5 整改** 的通过证据，不是发布授权，也不是最终 board 退回结论；所有审查均是当前快照的早期审查。历史 `DELIVERY_VERIFICATION 0.4.2 FINAL` 不能作为本次 release 通过的依据；包内该文档保持原样，并由 `STAGING_README.md` 明确划清历史与本次候选的边界。下一步应由 L2 在完整产品验收聚合合同落地后，重新冻结源码树并触发最终候选构建与重装。

## 9. 第二轮独立审核 H1 整改（2026-11-13）

第二轮独立审核的原始报告文件已被审核会话清理，结论保存在 ORCA 消息 `msg_676079cfa0c7`：v3 包按可信摘要 `87106096...` 仍可通过完整性验证，但审核发现验证器只看 staging 封面和 `release_ready/publishable`，没有拒绝 manifest 中把 `product_runtime_verified`、`product_runtime_installed`、`install_contract.scope`、`publishable_claim` 或 `acceptance_binding` 改成成功值的自声明。

本轮已修复：

- `scripts/trial_release_manifest.py` 现在失败关闭地校验 `build_environment.product_runtime_installed=false`、`install_contract.product_runtime_verified=false`、`install_contract.scope=harness_handshake_only`，以及新目录、不切换环境、禁止私有运行时复制、可信摘要和复制后执行前复验等安装边界。
- 顶层和 `release` 块里任何真值的 `product_runtime_verified` / `product_runtime_installed` 都会被拒绝，避免旧版或手工字段绕过 staging 合同。
- 门禁名称必须恰好覆盖十类且不能重复；`acceptance_binding` 按实际证据绑定结果要求 `pass` 或 `missing`，`publishable_claim` 永远必须为 `blocked`，授权门禁只能表示记录不足或未请求。
- `acceptance.bound`、`release.acceptance_evidence_recorded` 和 `acceptance.effect=advisory_only` 都会重新核对。
- 新增 8 个逐字段恶意声明参数化用例和 1 个门禁重复用例。`server/tests/test_trial_release_manifest.py` 结果为 **37 passed**，`py_compile` 与 `git diff --check` 退出码均为 0。
- 合法 v3 冻结包仍按可信摘要 `871060968c542a0b267c04e29374e7e07343d4eabaec4327dca69a286bc7eb0d` 验证为 `PASS`、`release_ready=false`、`publishable=false`，证明修复没有把合法 staging 包误杀。

**H1 独立复核：PASS。** 报告见 `.runtime/evidence/oc_t14_h1_independent_review_20261113.md`。审核人在隔离目录中对 v3 复制包构造 30 个恶意 manifest 样本，覆盖运行时安装/验证、安装范围、顶层与发布块声明、硬门禁、授权、验收绑定、重复门禁及多项安装边界；30/30 均退出码 1、验证失败且原因指向被篡改字段。合法 v3 仍按可信摘要验证 PASS，37 项合同测试退出码 0。

审核同时记录了不阻断 H1 的后续加固项：包内验收 JSON 仍是无签名自声明；自由文本依赖说明和未知键可能误导读者；fixture 来源列表尚需与实际文件一一对应；非法门禁名应返回结构化失败；整数 `0` 的类型判断可更严格；`acceptance_bound` 在失败报告中不应被误读；HippoRAG 来源字段尚未逐项固定。上述项目必须在 v4 冻结前处理或明确降级为纯展示字段。

**当前边界：** 这证明 H1 验证器缺口已在源码层修复、通过独立复核并有回归测试，不代表 OC-T14 已通过最终审核。当前工作树相对 v3 已继续漂移；必须等本轮和并行源码修改停止后重新冻结、重建 v4、重新安装并取证，再由独立审核人复核。v3 仍只是冻结的历史 staging 快照，不得作为当前源码的发布候选。
