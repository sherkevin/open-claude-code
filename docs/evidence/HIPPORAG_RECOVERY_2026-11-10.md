# HippoRAG 进程取消与可信复制恢复证据

日期：2026-11-10 至 11 日。项目：`open-claude-code`（知行工作台）。阶段：P1 受控开发验证；原始日志另记 UTC 时间。

## 结论与边界

官方 HippoRAG 已通过独立进程的封存、可信复制恢复、模型接口挂起后的超时与取消验证。服务权限、版本、纯图片检查和引用登记边界保留。默认服务仍不启用官方后端，没有生产部署、共享服务重启或客户资料处理。本页生命周期测试使用合成模型、没有供应商调用；后续已授权的公开资料真实调用单独记在[公开质量诊断](HIPPORAG_PUBLIC_2026-11-10.md)，不能将该调用事实省略。

这是一条已验证的开发恢复路径，不是任意缓存加载、OS 沙箱、生产自动恢复或工业质量验收。此前[初始官方算法证据](HIPPORAG_OFFICIAL_2026-11-10.md)记录的“没有进程取消/适配器恢复”是历史状态，由本阶段补齐；当时的完整回归数字不能代替本阶段完整回归。

## 实现与安全合同

| 位置 | 本阶段能力与限制 |
|---|---|
| `server/app/hipporag.py` | `seal()` 严格关闭/刷新后发布完整文件清单和外部 receipt；`restore()` 验证并复制到新目录后二次验证，才构造官方引擎 |
| `server/app/hipporag_process.py` | 每实例一个独立 Python 进程/进程组，有界 JSON 通信、操作超时与取消、后代清理；不替换官方算法 |
| `server/app/knowledge.py` | 每请求取消信号、浏览器断开与任务/权限取消；取消不回退、不登记引用；官方错误仅透传固定枚举 |
| `server/tests/test_hipporag_adapter.py` | 新旧合同共同验证来源映射、快照、结果、安全文件树和可信恢复 |
| `server/tests/test_hipporag_process.py` | 真实子进程合同覆盖等待者隔离、超时、崩溃、协议、进程组与恢复；模型/引擎工厂为测试替身 |
| `scripts/verify_hipporag_recovery.py` | 真实固定上游算法与真实工作进程；只有模型输出是明确合成夹具 |

### 封存和恢复

1. `seal()` 先停止该实例查询，要求引擎严格关闭/刷新成功；失败不给 receipt，也不恢复该实例查询资格。
2. 完整清单记录每个普通文件的路径、大小、SHA-256、owner、精确 `0600` 权限及 `0700` 目录/空目录。软链接、硬链接、特殊文件、超量、篡改及未知旧清单均拒绝；清单原子发布并再次核验。
3. 可信宿主在索引之外保存八字段 receipt：`schema/source_dir/owner_uid/identity_digest/snapshot_digest/tree_digest/file_count/total_bytes`。`tree_digest` 认证 `seal.json`，其完整清单认证所有其他索引文件。目录内自带摘要不能自行成为信任根。
4. `restore()` 必须匹配本次授权 rows、配置 identity、源绝对路径和 receipt；验证源文件树后流式复制到全新私有目录，并核验副本和源路径稳定性。所有这些检查在官方引擎构造前完成，不让未经验证的 pickle 到达加载器。
5. 官方引擎只打开副本，不修改原封存目录，不重新 `.index()`；中断可变副本不自动重开。文件读取可能更新访问时间，因此“源不变”指内容、集合和权限，不作 atime 不变承诺。

receipt 不是签名、用户权限凭证或自动防回滚证明；其可信性来自宿主持有，最新授权与 receipt 生命周期仍由宿主管理。路径与文件校验不是对抗同 UID 攻击者的 OS 沙箱，可信工厂与宿主私有父目录仍是前提。设计及精确限额见[适配器设计](../design/HIPPORAG_ADAPTER.md)。

### 取消与资源所有权

工作进程使用 Linux/POSIX 进程组与 `waitid(WNOWAIT)`：观察退出但先不回收组长，清理所属组后再 wait，避免误向复用 PID/进程组发信号。宿主不得竞争回收该私有子进程；未验证 Windows。默认网络关闭、私有 HOME/tmp/cache、`-I -S -B` 与显式模块目录均为进程启动约束，不是 cgroup 配额或原生扩展网络沙箱。

等待锁的请求取消或超时只退出自己的等待，不停止正在执行的其他请求；已派发操作才能清理自己的工作进程。请求截止时间覆盖锁等待及通信，回收另有最多一秒等待预算；磁盘清理、输入校验和操作系统调度不在硬实时保证内。

服务层协程取消最多等待线程交接 5 秒，不强制终止 Python 线程，也不保证非协作后端在这 5 秒内结束。实际进程回收由进程适配器负责。独立审查发现 watcher 清理曾可能吞掉新到达的调用方取消，已改为 `gather(return_exceptions=True)` 并添加竞态测试。

终止本机工作进程也不等于供应商远端推理或计费已经取消。公开质量首轮有一次请求在本地终止前未返回，远端是否继续及其用量未知；该情况单独保留，不作为零费用或供应商取消成功的证明。

官方检索前后、线程交回事件循环后和引用登记前均保留账号/助手/知识库权限及片段版本检查；纯图片即使没有文本片段，也单独检查文档、版本、摘要和解析状态。运行取消、撤权或版本变化不登记引用；取消不会伪装为 lite 成功。`timeout/worker_failed/worker_protocol` 可显式回退，但必须再次检查当前授权与版本。

## 真实官方执行结果

报告：`.runtime/verification/hipporag-real-recovery-20261110.json`，状态 `PASS`，完成于 `2026-11-10T15:11:13.445471+00:00`。

| 检查 | 实际结果 |
|---|---|
| 真实官方构建、检索、封存 | 5 种合成文本对应 6 个本地来源；同正文的重复来源保留；封存后原工作进程退出 |
| 验证后复制恢复 | 恢复前后结果与分数一致，原封存文件内容不变；真实 `.index()` 总计仅 1 次 |
| 模型接口挂起超时 | 请求超时设为 2 秒，实测结束时间 2.063 秒；所属工作进程已回收 |
| 超时后显式恢复 | 从原可信 receipt 恢复到另一个全新目录，结果保持一致；不重开中断副本 |
| 模型接口挂起取消 | 仅在当前 PID 的 `blocked_model` 事件到达后触发取消；返回取消且该工作进程已回收 |
| 篡改拒绝 | 修改原 ready 清单后恢复被拒绝，未到达官方引擎构造器 |

六项检查通过；报告追踪的三个成功运行工作进程均已回收。`external_model_calls=0` 只表示本次显式合成模型执行事实。2.063 秒是一次受控测试测量，不是普遍的性能 SLA。

上游固定提交 `d5c8329422e0a0b834a15874545cb6a74b4f9b26`，逐字节核对 63 个 Python 文件；源码集合摘要 `3a26b151536e3c518fd11b939d33b1e884092a5b6aee72b783f5f70de926d130`。真实官方 OpenIE 解析、事实筛选、图与检索未被测试引擎替换；合成 LLM/embedding 不提供真实实体质量或成本证据。

### 真实公开资料索引恢复补证

第三轮公开模型诊断完成封存后，又单独验证了它的真实产物，而不是重新构造合成索引。报告 `.runtime/verification/hipporag-public-real-restore-20261111T0027/report.json` 为 `PASS`，耗时 16.123 秒：

- 独立核对冻结脚本、官方源码与许可、本地七个权重文件、公开原文/图片、19 条来源、配置身份及宿主外置 receipt 后，才调用默认禁网的进程适配器。
- 使用真实 `create_adapter`、真实 E5 和未替换的原 LLM 工厂，仅执行 `restore/close`，不执行 `.index()` 或 `.search()`；没有新模型请求，新的 trace 和预算文件均未创建。
- 原封存目录与新私有副本的 10 个文件、1,709,907 字节以及模式/所有者全部一致；原报告、模型账本、trace 和 receipt 未改变。进程 3322370 关闭后已退出并由宿主回收。

该补证说明第三轮的实际索引能通过受信任清单校验后加载，而非仅在合成输入下成立。它没有重测问答质量、供应商取消或生产运营恢复，也不绕过可信宿主及非操作系统沙箱的边界。

## 回归记录

**各批次有重叠，不能相加成完整或唯一总测试数。** 最终冻结版本的完整服务端回归已通过：59 个文件、1,140 passed、0 failed、0 errors、0 skipped；三个不重叠分片分别为 343、165、632 项。完成时间 `2026-11-10T16:33:16Z`，即本地 11 月 11 日 00:33。全过程源码摘要无变化、无新增源文件；当前 169 项诊断工厂合同已经包含在 1,140 内，不能再累加。

最终逐项审计见 `.runtime/verification/hipporag-process-full-short-20261110T160351Z/verification-summary.json`；同目录 `artifacts.sha256` 保存报告、三个 XML 和源码快照记录的摘要。独立解析 XML 得到 1,140 个唯一测试标识，覆盖当前全部 59 个服务端测试文件；审计时源码仍与测试快照一致。

| 批次 | 结果 | 证据 |
|---|---|---|
| 核心来源与恢复合同 | 145 passed | `.runtime/verification/hipporag-recovery-contract-20261110.log`、同名 XML；含原 83 项合同 |
| 独立进程合同 | 55 passed，19.42 秒 | `.runtime/verification/hipporag-process-proof-20261110.log`、同名 XML |
| 服务取消/来源/版权重点回归 | 47 passed，1 条第三方弃用警告 | `.runtime/verification/hipporag-service-phase2.log`、同名 XML；包括取消清理竞态和版权测试 |
| 最终客户端复验 | 19 文件、123 passed；TypeScript 检查通过 | `.runtime/verification/hipporag-final-client-node-20261111.log`、`.runtime/verification/hipporag-final-tsc-node-20261111.log`；显式使用已安装 Node 24.9.0，没有执行 Vite 生产构建 |
| 真实运行时与许可校验合同 | 19 passed | `.runtime/verification/hipporag-license-runtime-20261110-225434/tests.log` |
| MIT/NOTICE 开发 wheel 包含 | 两项 notice 均 PASS | `.runtime/verification/hipporag-phase2-wheel/notices.json` |
| 当前公开诊断工厂合同 | 169 passed、0 skipped | `.runtime/verification/hipporag-public-offline-20261110T160206Z/`；HTTP 为替身，不代表质量通过 |
| 最终冻结版本完整服务端 | 1,140 passed、0 failed、0 errors、0 skipped | `.runtime/verification/hipporag-process-full-short-20261110T160351Z/report.json`、三个 XML 与输入摘要；显式启用已安装的真实本地 Harness 测试，无跳过冒充通过 |

完整回归的失败和中止也保留：首轮 `.runtime/verification/hipporag-process-full-20261110T151317Z/` 为 970 passed、1 failed。失败原因是过长的 pytest 临时目录使 memory API 查询超过现有 240 字符上限；短数据盘目录下整个 memory 文件 10 passed，没有修改 API 限制或测试断言。中间 `.runtime/verification/hipporag-process-full-short-20261110T155211Z/` 因真实 E5 接口修正而主动停止三个自有测试进程，不能算全量通过；有精确 PID、命令与退出核对记录，未停止共享服务。最终回归重新从同一冻结代码快照完整执行，不拼凑旧分片作新版本全绿。

最终客户端第一次 clean-environment 启动因 PATH 中没有 Node 而在测试前退出 127，原日志 `.runtime/verification/hipporag-final-client-20261111.log` 保留。显式选择现有本地 Node 后全部 123 项和类型检查通过，未安装或更换依赖，也没有改测试来规避失败。

### 复验入口

下面仅启动隔离测试进程，不启停产品服务；需要先备好明确的测试依赖/官方环境，缺失依赖或跳过不能记作通过。

```bash
cd server
PYTHONPATH=/tmp/open-claude-pylib:. \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.tmp/open-claude-pycache \
TMPDIR=/media/data3/dengkw/.tmp \
python3 -m pytest -q -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/.tmp/hipporag-recovery-recheck \
  tests/test_hipporag_adapter.py tests/test_hipporag_process.py \
  tests/test_hipporag_cancellation.py tests/test_hipporag_official_search.py
```

```bash
python3 scripts/verify_hipporag_recovery.py \
  --work-root /media/data3/dengkw/.tmp/hipporag-recovery \
  --output /media/data3/dengkw/.tmp/hipporag-recovery-result.json
```

默认读取项目忽略目录中的独立官方环境、固定源码与安装清单，不从调用方 HOME 推导凭据或模型。该复验入口不是干净新机器上的安装器或生产部署过程。

## 许可与后续质量阶段

`server/LICENSE.HippoRAG` 保留完整 `Copyright (c) 2025 OSU Natural Language Processing` 及 MIT 授权/免责声明，摘要 `93b43d692b033b76129504448695bfe76ef22d18b11e51352bfab4b5622e5aaa`。本地开发 wheel 同时包含该文件与 `NOTICE.HippoRAG`，实物包含检查通过；wheel 摘要 `d4349e7c909ea4f45352ce2e8a0b80c7ce4a28b1f501d6cc9410193957f09a20`。没有发布该包，也未将隔离官方依赖加入默认运行环境。

MIT 只描述 HippoRAG 本身。实际依赖含 igraph 的 GPL-2.0-or-later、tqdm 的 MPL 部分及数值库的另行许可原生组件；完整分发义务仍未审完，详见初始证据中的依赖核对表。保留版权原文不等于完成整个依赖栈的商业分发审查，本记录不作法律结论。

P2 已冻结公开 NIST pilot：18 段原文、8 道助手编写的问题及 2 张既有图片，来源完整性检查通过，但不是独立人工 gold。真实 E5 权重已按固定 revision 下载并核验；真实模型试跑先后暴露了输出截断、官方 `numpy.str_` 与向量输入合同不兼容，以及图片定位不合格的问题。失败、已知用量及首轮一次未知用量均保留，详见[公开质量诊断](HIPPORAG_PUBLIC_2026-11-10.md)；不能再沿用早期“尚未发出真实调用”的状态。

修正后，真实固定官方算法、真实 E5 和明确合成 LLM 的无网全链冒烟在 `.runtime/verification/hipporag-real-e5-smoke-20261111-0008/report.json` 通过兼容性检查：段落、实体、事实及查询四类向量均为有限、单位化的 384 维；实体输入含真实 `numpy.str_`，构建和检索返回 6 个来源，官方事实筛选、图检索与 PageRank 均执行。官方 manifest 明确记录实际本机端点与 512 长度。状态为 `PASS_WITH_BLOCKED_DEPENDENCY_PROBE`：urllib3 导入期间一次本地 IPv6 探测在 socket 创建时被拒绝，没有放行 socket，也没有实际供应商调用；不将“零实际调用”写成“零尝试”。它是兼容性验证，不是抽取质量或操作系统沙箱证明。

旧 v2 SCADA 图框与全页坐标错位已在对应样例标注中修正，仍明确标为助手演示复核，不升级为独立金标准或模型抽取成功。见 `fixtures/industrial-public-v2/annotations/visual-entities.jsonl` 的 `1.2.0` 复核历史，以及 `.runtime/verification/scada-box-correction-20261110-230949/` 的失败/修后日志、XML 和输入摘要；它不属于本轮六项恢复检查。

仍待完成：已暴露的真实公开资料实体/关系和图片质量修复、更多独立留出标注、多模态融合改进、生产运营配置/资源隔离、完整依赖分发审查及客户业务验收。全量回归通过、真实诊断完成和质量达标是不同状态；不得据此把整个需求升级为完全实现或提前宣称产品可交付。
