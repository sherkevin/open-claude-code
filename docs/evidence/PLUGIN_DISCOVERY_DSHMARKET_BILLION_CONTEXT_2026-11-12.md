# DeepSeek Harness 插件发现证据（2026-11-12）

## 结论

- `dshmarket` 和 `billion-context` 都有可追溯的公开源码与发行包，但本轮只完成来源、许可证、版本和静态权限证据核验，**没有安装、加载、重启或接入生产/共享服务**。
- `dshmarket` 适合作为受控的插件发现入口；它本身会下载、安装、更新和切换第三方插件，因此不能被视为安全边界，必须继续沿用本项目的管理员白名单、来源锁定、许可证审查、权限审查和默认拒绝策略。
- `billion-context` 适合作为上下文压缩候选，但仓库明确标注项目仍处于 Early 阶段，真实模型集成测试是后续里程碑；其发行包的 MIT 文本还附加了终端用户可见产品的署名条款。压缩比例、供应商 token 节省和回答质量不能在未做本地基准前宣称。
- 两项均不应直接标为“已接入”或“完全实现”。下一步应由受控任务先做离线清单解析、权限拒绝和合成上下文基准，达到验收门槛后再由人工批准实际安装。

## 权威来源和固定版本

### dshmarket

| 字段 | 证据 |
|---|---|
| 源码 | <https://github.com/dsh-market/dsh-market> |
| 最新发行版（本次核验） | `v1.73.18`；GitHub API `published_at=2026-11-11T16:42:32Z`；tag commit `7f32bb6c8566053916c5ba646263c132ac8d703c` |
| 本次源码头提交 | `ade3fbdc7c9bfbfeba5b8839e4fd78df0d36c036`（GitHub API，2026-11-12） |
| 包名/版本 | `dshmarket@1.73.18`（发行包 `package.json`） |
| 许可证 | MIT；<https://raw.githubusercontent.com/dsh-market/dsh-market/main/LICENSE> |
| 发行包 | <https://github.com/dsh-market/dsh-market/releases/download/v1.73.18/dshmarket-1.73.18.tgz> |
| 发行包 SHA-256 | `a41997fcb10e1e72360f18abecf6a92346dc43c272054e3bd0b7996c61616c85` |
| DSH 兼容声明 | `@deepseek-ai/cordis ^4.0.1`；`@deepseek-ai/dsh-settings ^0.1.0-rc.7 || ^0.1.1-rc.2 || ^0.1.2-alpha.2 || ^0.2.0-rc.1 || ^0.2.1-alpha.3 || ^0.2.1-rc.1`；要求 DSH Web `>=0.1.0-rc.6` |
| 官方安装说明 | `dsh plugin --profile <profile> add dshmarket`；GitHub README 明确提醒第三方插件需信任其安装来源 |

静态包中的 `package.json` 声明 `dsh.bundle.patch` 和 Web 客户端注入；`cordis.patch.yml` 会把插件插入 profile layer。README 还说明目录来自远程 `awesome-dsh-plugin.com/plugins.json`，默认不带静态快照。因此安装后会产生远程目录、安装和更新行为，必须在本项目中把网络来源和版本锁定为审计输入，而不能让模型任意调用。

静态代码还包含重启宿主、执行 `pnpm` 安装、WebDAV 备份和 GitHub Gist 备份路径；Gist 路径可以读取已经登录的 `gh auth token`，WebDAV 路径接收用户输入的用户名/密码。它虽然有日志脱敏和同源/回环检查，但这些能力在知行工作台中都必须默认关闭，禁止插件接触主账号凭据或执行外部写入。

### billion-context

| 字段 | 证据 |
|---|---|
| 源码 | <https://github.com/ranxianglei/billion-context> |
| 最新发行版（本次核验） | `v0.1.213`；GitHub API `published_at=2026-11-07T23:25:08Z`；tag commit `133f3525c628c4b959bb79ece82af6df82e131a3` |
| 本次源码头提交 | `9674cfafbc78c50245ae0d887483d3d6ce880c15`（GitHub API，2026-11-12） |
| 包名/版本 | `billion-context@0.1.213`（npm 发行包 `package.json`） |
| 许可证 | MIT + 额外终端用户可见产品署名条款；<https://raw.githubusercontent.com/ranxianglei/billion-context/master/LICENSE> |
| 发行包 | <https://registry.npmjs.org/billion-context/-/billion-context-0.1.213.tgz> |
| 发行包 SHA-256 | `e3dab47c9e2cbbe67b541190188a3217571c10cb3ea771ab7c04b1301251df98` |
| 运行时要求 | Node `>=20`；依赖 `mysql2`、`zod`，可选依赖 `jimp` |
| DSH 安装说明 | `dsh plugin --profile <profile> add billion-context`；GitHub README 警告直接安装 Git checkout 没有 `dist/` 时 bundle 不会加载 |

静态发行包中的 `dsh.bundle.patch.yml` 会插入 `bili-native` 和 `bili-compaction-gate`，关闭 DSH host-plane native compaction，并向 DSH preset 注入 `biliBasicCompactionPolicy`；另有对 DeepSeek retry policy 的 patch。该行为会修改宿主压缩/重试路径，必须先做隔离 profile 评估，并检查与本项目已有压缩、上下文快照和用量账本的冲突。

仓库 README 明确写出：项目状态为 Early，协议处理和压缩只对 mock tests 工作，真实模型集成测试仍是下一里程碑。README 还说明压缩发生在路由之前，真实 provider credentials 留在 gateway，但其代理支持非认证的本地管理端点、远程暴露时需可信网络/防火墙；因此不能把网络暴露和凭据边界交给默认配置。

发行包静态代码还暴露 `__bili/plugin/tool`、`/__bili/*` 管理端点和代理环境变量；README 明确提醒代理默认无认证，远程监听只能放在可信局域网或防火墙后。DSH 适配必须限制为本机隔离 profile，并把所有管理端点保持 loopback-only。

## 本机核验记录

在下载本次审计归档之前，本机和项目扫描没有找到已安装的同名插件源码或锁定配置；审计归档本身已明确排除在扫描路径之外：

```text
find /media/data3/dengkw -path '/media/data3/dengkw/.runtime/open-claude-code/plugin-audit' -prune -o -type f \
  \( -iname '*dshmarket*' -o -iname '*billion*context*' \)
# 无匹配

rg -il --hidden --glob '!docs/**' --glob '!*.jsonl' --glob '!*.log' \
  --glob '!node_modules/**' --glob '!dist/**' \
  'dshmarket|billion-context|billion context|billion_context' \
  /media/data3/dengkw/projects/open-claude-code \
  /media/data3/dengkw/projects/harnessisallyouneed \
  /media/data3/dengkw/.agents /home/dengkw/.agents
# 无已安装插件源码、package manifest 或运行配置匹配；项目 docs 中的需求草案不计入扫描
```

为便于离线审查，本次下载的发行包和解压内容只放在临时目录（不属于项目交付物，未加入 Git）：

```text
/media/data3/dengkw/.runtime/open-claude-code/plugin-audit/artifacts/
```

该目录包含两个发行包、`package.json`、许可证、DSH patch 和构建产物；项目仓库只保留本证据文档。

## 适配判断

| 插件 | 可解决的问题 | 当前适配性 | 主要阻塞 |
|---|---|---|---|
| `dshmarket` | 为 DeepSeek Harness 提供目录搜索、兼容性提示、版本/来源查看和人工确认后的安装入口 | **候选可接入（发现层）**，不应直接开放自动安装 | 需要把远程目录转换成项目的受控清单；需要管理员白名单、准确来源 pin、摘要校验、安装前审批和撤权测试 |
| `billion-context` | 在宿主请求前做上下文压缩、长期会话管理、压缩状态和 token 用量观测 | **候选可评估（运行时层）**，暂不能宣称节省比例或质量达标 | 早期状态；需验证 DSH 版本兼容、压缩与现有上下文/账本的冲突、重试 patch、副作用、取消/恢复、署名义务和本地安全边界 |

## 最小可验证 PoC（不安装到共享服务）

所有命令都应在 `/media/data3/dengkw/.runtime/open-claude-code/plugin-audit/` 下的隔离目录执行，禁止使用项目生产 profile、共享 DSH profile 或真实客户数据。

1. **清单和完整性**：离线解析两个 `package.json`、许可证和 DSH patch；比较表中版本、提交和 SHA-256；源地址不是固定提交/发行包时状态必须为 `blocked_external`。
2. **插件发现**：对 `dshmarket` 的远程目录使用固定快照，验证只返回候选清单和来源元数据；拒绝任意包名、未声明许可证、未声明 DSH 兼容范围和未锁定 tarball。
3. **压缩基准**：使用项目内合成工业对话（不含客户数据），固定相同问题、工具结果、上下文和模型桩；分别跑原始上下文与压缩候选，记录输入/输出 token、压缩前后字节数、检索命中、引用完整性、取消/恢复和错误率。压缩过程的本地 token 不得冒充供应商实际消耗。
4. **安全回退**：使压缩服务超时、返回无效摘要、取消、版本摘要不匹配，验证请求回退到未压缩路径或明确失败；不得静默发送未审计的完整上下文到第三方 endpoint。
5. **权限审计**：验证插件不能读取项目外文件、环境变量、凭据或真实客户目录；网络只允许声明的 registry/source；默认拒绝未知插件和未授权 host。

建议验收命令（在实际任务节点中实现后再运行）：

```bash
.venv/bin/python scripts/verify_harness_plugins.py \
  --plugins dshmarket,billion-context --format json --offline
.venv/bin/python -m pytest -q \
  server/tests/test_harness_plugin_catalog.py \
  server/tests/test_harness_plugin_permissions.py \
  server/tests/test_billion_context_adapter.py \
  server/tests/test_harness_usage_ledger_plugin.py
```

验收要求：来源、版本、许可证、完整性摘要和权限声明全部存在；未知或未锁定来源退出非零并返回 `blocked_external`；压缩 PoC 给出可复算的对照数据；权限撤销后不能加载；失败分支默认拒绝且保留审计字段。

## 风险与后续

- 不安装 `dshmarket` 或 `billion-context` 到当前运行 profile；没有重启服务，也没有调用真实客户数据或未授权付费模型。
- `dshmarket` 的目录和更新逻辑本身执行远程网络和第三方包安装，必须把市场视为“不可信输入”，不能让模型自动执行其一键安装。
- `billion-context` 的附加署名条款必须在知行工作台的文档或关于页落地；是否满足商业发布条件需要法务/所有者确认，不能只看 `license=MIT` 字段。
- 任务板应新增/派发两个受控节点：橙色类别“插件市场发现与白名单审计”（对应 `dshmarket`），红色类别“上下文压缩候选评估与账本接入”（对应 `billion-context`）。在来源验证和隔离 PoC 通过前，状态保持“待验证/外部阻塞”，不得宣称完成。
