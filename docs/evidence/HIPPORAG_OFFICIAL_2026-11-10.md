# HippoRAG 官方算法与授权来源适配验收

日期：2026-11-10。服务器：学校云。项目：`open-claude-code`（知行工作台）。

## 结论与范围

本轮从设计推进到开发切片：真实官方算法可在隔离依赖环境运行，并通过薄适配器接回本项目的授权片段与证据来源。默认服务不启用该后端；没有生产部署、工作台重启、客户数据或真实供应商模型调用。不能据此宣称工业实体抽取质量、多模态知识图谱或完整产品已验收。

上游固定 [HippoRAG 提交](https://github.com/OSU-NLP-Group/HippoRAG/tree/d5c8329422e0a0b834a15874545cb6a74b4f9b26)（MIT）。验证逐个比较该提交的 63 个 Python 源文件并检查实际导入位置；源码集合摘要为 `3a26b151536e3c518fd11b939d33b1e884092a5b6aee72b783f5f70de926d130`。

## 实现

| 位置 | 本轮能力 |
|---|---|
| `server/app/hipporag.py` | 不可变授权快照、配置指纹、同文本一对多来源、原子 ready 清单、拒绝未知旧目录、结果严格校验、串行调用 |
| `server/app/knowledge.py` | 显式注入预构建官方索引；默认路径不变；检索前后及线程交接后复查账号、助手、文档版本和纯图片证据；复用来源登记 |
| `client/src/KnowledgePage.tsx` | 官方与本地算法分别标注；清晰说明回退；不把候选种子当作已核验关系；保留原件与图片证据卡 |
| `scripts/verify_hipporag_official.py` | 固定源码、独立环境、合成模型接口、真实官方算法与受控索引重开验证 |
| `scripts/evaluation_gate.py` | 官方适配文件纳入变更评测门禁 |

## 已验证结果

以下批次存在重叠，不能相加为唯一总测试数。

| 批次 | 结果 | 证据边界 |
|---|---|---|
| 适配合同 | 83 项通过；独立复跑也通过 | 测试引擎，不是模型质量验收 |
| 知识服务集成 | 基础 28 项通过，追加重点 11 项通过；合计覆盖 36 个不同用例 | 授权、重复来源、版本、撤权、引用、纯图片与线程交接边界 |
| 既有知识/图谱/公开资料/视觉回归 | 66 项通过，598.77 秒 | 开始于最后的纯图片后置复核修补之前；新增修补另由 11 项重点及完整回归覆盖 |
| 前端 | 19 文件、122 项通过；TypeScript 检查通过 | 包含实际状态组件的静态渲染；不是部署后浏览器验收 |
| 真实官方验证及启动边界 | 最终 10 项通过，35.34 秒；普通与空 HOME 环境均实际执行通过 | 官方算法未替换，模型为合成替身 |
| 评测门禁 | 3 项测试通过，合成基准 4/4 通过 | 不衡量供应商质量、时延或费用 |
| 完整服务端 | 51 个文件、三个独立进程：820 项通过，6 项跳过，0 失败/错误 | 2026-11-10 22:12:33 完成；跳过项及后续补测见下文，不将跳过记为通过 |
| 显式启用上游运行时补测 | 15 项通过，0 失败/跳过，152.71 秒 | 10 项 HippoRAG + 5 项 Harness；真实上游进程/算法与本地合成模型，补齐上述跳过项 |
| 按测试编号合并覆盖 | 当前收集的 832 个不同服务端用例全部有通过记录，0 未覆盖 | 全文件回归加最后补测，不冒充一次无跳过的单进程全量；XML 按编号去重核对 |

真实官方算法验证不是用假引擎证明成功。替身只用于语言模型的 `infer` 和向量模型的 `batch_encode`；官方信息抽取解析、事实筛选、同义近邻、图构建、图检索和 PageRank 未被替换。实际调用轨迹包含 2 次索引、10 次实体解析、10 次三元组解析、2 次同义近邻、3 次检索/事实筛选/图搜索/PageRank；合成图有 12 个节点、15 条边、4 个事实种子和 1 条同义边。

受控索引重开前后排序一致；适配器将 5 种合成文本映射到 6 个本地来源，相同正文的两份文档没有丢失。当前适配器仍拒绝跨实例加载旧目录：上游重开测试不等于产品恢复流程已经实现。

验证进程不继承供应商环境与用户 HOME，所有模型对象均为合成替身。Python I/O 审计未观察到受监测的外联尝试；这是开发验证，不是操作系统沙箱证明，也不覆盖原生扩展或启动 `.pth` 代码。

最终报告覆盖三个引擎阶段，总计 23 次合成语言模型调用和 9 次合成向量调用。普通执行完成于学校云 21:57:26，空 HOME 执行完成于 21:57:44。复核还发现启动器曾从调用方 HOME 推导共享依赖路径，清空 HOME 时会失败；现从所选隔离环境的 `installation.json` 读取明确的共享代码路径，修前失败与修后成功均保留，不读取调用方的供应商环境。

完整服务端批次从 21:41:23 开始，三组分别为 360 项通过、274 项通过/1 项跳过、186 项通过/5 项跳过。五项 Harness 实际进程测试需要显式设置隔离运行时路径，完整批次的干净环境未设置，因此跳过；另一项官方 HippoRAG 测试在收集时尚无环境安装清单，因此跳过。运行期间应用源文件均未改变，唯一变化是 `test_hipporag_official_runtime.py` 增加空 HOME 启动边界等六项测试并修正启动路径验证；不能将完整批次说成自动覆盖了这些后加的测试。

随后在干净环境显式启用两个隔离运行时，22:16:49 完成上述 15 项补测，全部通过，期间所核对的源文件没有改变；模型均为本地合成替身。按 XML 用例编号去重后得到 832 个通过用例，与最终 `pytest --collect-only` 的 832 个编号完全相同，没有漏项或额外编号。剩余警告为第三方 Starlette/httpx 弃用提示；首轮另有 JUnit 属性格式提示，补测选用兼容格式消除该提示。没有通过关闭断言或放宽安全策略获得通过结果。

## 日志与复验

- `.runtime/verification/hipporag-adapter-contract-20261110.log` 与同名 XML。
- `.runtime/verification/hipporag-adapter-independent-20261110-213917.log`。
- `.runtime/verification/hipporag-official-service-final-20261110-213710.log`。
- `.runtime/verification/hipporag-official-service-revocation-20261110-214013.log`。
- `.runtime/verification/hipporag-knowledge-regression-20261110.log` 与同名 XML。
- `.runtime/verification/hipporag-client-final-20261110.log`。
- `.runtime/verification/hipporag-gate-20261110.json`。
- `.runtime/verification/hipporag-official-20261110.json`：真实官方算法结果；以最后重跑版本为准。
- `.runtime/verification/hipporag-official-tests-20261110.log`：最终 10 项测试。
- `.runtime/verification/hipporag-clean-home-before-20261110.json`、`.runtime/verification/hipporag-clean-home-after-20261110.json`：启动边界修复前后证据。
- `.runtime/verification/hipporag-official-inputs-20261110.sha256`：6 项源代码和环境输入校验；清单摘要 `8cf741ad92b9f09b5439366e0070d504d0f33705e1d4aece6e594cc1edbb5a32`。
- `.runtime/verification/hipporag-full-20261110-214123/`：每个分片的日志、XML、输入哈希和最终报告。
- `.runtime/verification/hipporag-optional-final-20261110/`：15 项显式运行时补测、XML、输入哈希与最终报告。
- `.runtime/verification/hipporag-server-combined-20261110.json`：832 个不同用例的逐项最新结果；SHA-256 `4adeb25dab3599c5b9b13872eda4660ac21369ff58c40e8fed11fb7c941d6836`。
- `.runtime/verification/hipporag-final-collection-20261110.log`：最终完整用例清单，逐编号核对无漏项。
- `.runtime/hipporag-env/installation.json`：真实安装版本和来源；依赖没有覆盖服务环境。

知识服务测试（不要求官方环境）：

```bash
cd server
PYTHONPATH=/tmp/open-claude-pylib:. \
PYTHONPYCACHEPREFIX=/media/data3/dengkw/.tmp/open-claude-pycache \
TMPDIR=/media/data3/dengkw/.tmp \
python3 -m pytest -q -p no:cacheprovider \
  --basetemp=/media/data3/dengkw/.tmp/hipporag-recheck \
  tests/test_hipporag_adapter.py tests/test_hipporag_official_search.py
```

真实官方算法（必须先准备文中独立环境；缺失时不能把跳过称为通过）：

```bash
python3 scripts/verify_hipporag_official.py \
  --temp-root /media/data3/dengkw/.tmp/hipporag-official-recheck
```

隔离环境只读复用本机已有的部分 Python 代码依赖；共享代码路径来自安装清单而非调用方 HOME。环境及依赖版本记录可支持本机复验，但不是干净新机器上的自动安装或正式部署证明。

## 修正与残余风险

- 首轮服务测试发现错误代码白名单不一致，已修复并复测通过。
- 独立审查发现纯图片没有文本片段，不能仅重查文本集合；已增加图片文档/版本校验。线程任务回到会话后、登记引用前也增加复核。
- 真实验证报告已统计所有引擎实例，并从固定提交而非 Git 暂存区读取源码清单；最终 10 项测试及上述报告均在修正后重跑。
- 尚无真实模型实体质量、多模态语义、多跳质量增益或真实成本/速度结论。合成资料只证明算法路径与来源合同。
- 尚缺生产级进程超时/取消、可信缓存恢复、配置与运营入口、公开工业金标准和客户业务验收。图谱候选仍不自动成为正式事实。
- `HPR-007` 由设计阶段升为部分实现；没有需求因本轮而被标为完全交付。

## 分发许可边界

HippoRAG 本身为 MIT，不代表所有依赖同为 MIT。核心图依赖 [igraph 0.11.8 的维护者发布信息](https://pypi.org/project/igraph/0.11.8/) 标明 GPL。当前仅安装在忽略的开发验证环境，不进入默认服务依赖或交付包；在商业产品分发前必须另行审查完整依赖及许可义务。本记录只说明组件事实和项目验收门槛，不作法律结论，也不将 GPL 误称为禁止商业使用。

本机只读核对还发现以下边界。它们来自实际安装包，不是完整传递依赖或二进制组件的分发合规证明。

| 实际组件 | 已核实事实 | 分发前检查 |
|---|---|---|
| `igraph 0.11.8` | `igraph/__init__.py` 声明 GPL-2.0-or-later；发行包 LICENSE 为 GPLv2。上游声明 `python_igraph`，当前安装实际代码包 `igraph`，没有安装元包不改变其许可 | 按实际集成及分发方式核对义务 |
| `tqdm 4.67.3` | 安装元数据为 `MPL-2.0 AND MIT`，许可证正文包含 MPL 部分 | 不整体按 MIT 处理 |
| `NumPy 2.2.6`、`SciPy 1.15.3` | 主体 BSD；本机二进制包实际包含 `libgfortran`（GPL-3.0-with-GCC-exception）和 `libquadmath`（LGPL-2.1-or-later） | 保留原生组件及例外条款，按交付包审查 |
| `litellm 1.73.1` | 普通代码 MIT，许可证对 `enterprise/` 另作规定；当前发行包 RECORD 未发现该目录 | 不把未来企业组件并入 MIT 声明 |
| `torch 2.5.1+cpu` | 主体 BSD-3-Clause，汇总 LICENSE 另含第三方许可文本 | 按最终包含的实际组件审查，不能只看主体元数据 |

上述组件未在本轮加入默认产品依赖。准备客户安装包、容器或离线交付时，应根据实际包含的源码、二进制、修改和集成方式，另存完整依赖清单、LICENSE、NOTICE 与来源信息；未经审查不能把隔离验证环境直接作为交付环境。
