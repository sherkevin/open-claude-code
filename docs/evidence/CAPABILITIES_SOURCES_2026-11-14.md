# 学校云外部来源与能力配置验收

## 范围与环境

- 对应需求 §18，节点 OC-T22（来源）与 OC-T23（能力配置）。不替代整个产品或客户验收。
- 基线 `f8f1f86`，学校云独立工作树；主工作树的既有未跟踪文件保持不动。
- 执行分别为 `collaboration /root/source_binding_hardening`、`collaboration /root/capability_settings_impl`，独立审阅为 `collaboration /root/independent_source_review`；L2 只做协调、集成和最终回归。没有伪造 Claude Code/ST 身份。
- 合成账号与工具响应，不读取真实客户数据或已有凭据，不调用真实付费 API，不重启共享服务，不同步腾讯云。浏览器只使用本轮自有回环测试服务。
- 解释器：`/media/data3/dengkw/projects/open-claude-code/.venv/bin/python`；`PYTHONPATH=server`；临时数据与缓存均在 `/media/data3/dengkw`。

## 来源缺陷与修复

独立审阅在旧主线复现四项失败：真实 MCP 文本封套不能取来源、Harness 重复来源绑定丢失、全局前三十个标识截断后续网页正文、最终来源载荷没有候选/已读/明确引用语义。

修复提交 `0dceb4be89f29bd290d2e0cd728d4f6c542103b5`：

- 从本次实际工具结果解析标识，并经登记库、当前身份、运行与权限核验。
- 用成功完成事件形成分页、去重来源清单，不再用数据库新记录差集归属工具来源。
- 最终引用与客户端分别呈现明确引用、已读未引用、未核验正文的搜索候选；补附资料不代表回答有依据。
- 原网页按钮每次重新鉴权，使用登记库原地址；撤权关闭新窗口并提示；保留既有业务数据原页面入口。
- 审核追加发现停用账号后台回调仍可取来源，已补当前账号状态检查，不仅依赖 HTTP 登录校验。

执行者报告：新增后端来源/撤权分批十七项通过，前端全套一百六十二项和构建通过。一次并行负载下的旧审批测试两秒等待超时，单独复跑两项通过；不修改生产审批逻辑掩盖超时，最终全量结果另列。

执行者浏览器检查使用真实 Sources 组件、合成来源接口（不是付费模型或端到端供应商验收）：桌面和 390 像素窄屏的三类标签、原文按钮、先鉴权后跳转、撤权错误和零横向溢出。截图由独立审阅者及 L2 查看：

- [桌面来源卡](capabilities-sources-20261114/sources-desktop.png)
- [窄屏撤权提示](capabilities-sources-20261114/sources-mobile-revoked.png)

## 能力配置切片

实际 App 管理员导航新增“能力与来源”，配置 search/fetch 开关、首选提供方、最多一个回退与一至六十秒总超时；服务端持久化、乐观版本检查和无密钥审计。部署策略、当前助手与账号权限继续生效。

这里不是完整四能力自助配置：密钥仍由服务端部署管理；对话 OCR、独立图片搜索、配额计费和域名白名单细分管理尚未接入页面。现有知识摄取 OCR 路径仅显示状态，不因保存此页开启文档外发。

稳定提交 `845a9b96b688481ad52cc572bfa703207e44df1f`。执行者相关后端 78 项、前端 159 项及构建通过。[浏览器记录](capabilities-sources-20261114/settings-browser-validation.md) 使用实际编译的 App、隔离合成账号和真实测试库 API：管理员导航、保存版本递增、真实 409 冲突、成员不可见及 API 403、未配置提供方禁选；加载/错误状态仅用浏览器本地 fetch 夹具。1440/390 像素无横向溢出，修正了旧 CSS 类重名导致的卡片内滚动。

- [配置桌面页](capabilities-sources-20261114/settings-desktop.png)
- [配置窄屏页](capabilities-sources-20261114/settings-mobile.png)
- [真实版本冲突](capabilities-sources-20261114/settings-conflict.png)

## 当前审核与集成状态

两个稳定提交均通过独立审阅，无本轮切片内未关闭阻断：

- [来源独立审阅](capabilities-sources-20261114/source-independent-review.md)：稳定提交后端 21 项（含独立契约）、前端来源 9 项亲跑通过。先前补充大组因磁盘日志提交等待，由审核者中断其自有进程，不计通过、不当产品失败。
- [配置独立审阅](capabilities-sources-20261114/settings-independent-review.md)：此前相关 76 项亲跑通过；稳定提交新增 2 项亲跑通过，另核实最多一个回退约束，检查执行者最终 78 项通过证据。不是声称独立审阅者再跑了一次完整 78 项。
- 来源合并 `e236aae`，配置合并 `3b004c15812b8d52c2ab20447ca2ef7e2592805a`，没有冲突。合并后的单进程服务端完整回归与客户端完整回归单独执行，避免多个 SQLite FULL 测试组争用磁盘。

合并后客户端 25 文件、168 项测试通过，TypeScript 与 Vite 构建通过；仅存在单个代码包 minification 后大于 500 kB 的构建提示（非 gzip 大小）。

服务端首轮完整回归在代码基线 `3b004c1` 执行，期间只合并文档，无产品代码变化：**1627 passed、51 skipped、1 failed、3 warnings，1815.41 秒**。唯一失败是既有 `test_lease_expiry_and_heartbeat_drive_recovery`，第二个 ConnectorRegistry 把请求标为 failed，而测试期望 running。不能把本轮称为完整回归通过。已在独立 `fix/connector-lease-recovery` 分支定位 0.3 秒租约、磁盘写入与测试时钟的关系，保留 [首轮完整日志](capabilities-sources-20261114/merged-server-first-run.txt)，修复后另跑最终完整回归，不将定向重跑冒充完整通过。

## 完整回归触发的执行租约修复

原有 0.3 秒租约测试将真实睡眠和同步 SQLite FULL 写入耗时混在一起，单凭“截止时间比之前晚”不能证明第二个实例扫描时仍未过期。保留正常持久化与租约策略，测试改用受控时钟和真实心跳事件，而不是扩大超时掩盖问题。

独立审核另外使用确定性的合成执行器，在修复前复现三项真正的隔离缺陷：恢复后的连接器仍返回迟到成功；兼容入口的迟到成功覆盖失败终态并发布引用；迟到错误覆盖既有中断状态。该三项与原短租约时间假设分别记录。

- 执行提交 `44d4f7cfd6ecf4610759f0036642e6fecb16de79`，合并 `5eed11a199d09e063292fbde1005b2f6a4b0c910`，独立分支 `fix/connector-lease-recovery`，仅四个实现/测试文件。
- 心跳与成功写入必须同时满足当前所有者、运行中、租约严格未到期；获得写锁后再取时间，不使用锁等待前的过期时间判断。
- 丢失所有权后的迟到成功/错误不覆盖终态，返回 `503 INTERRUPTED` 并记录丢弃；调用者主动取消仍保留取消语义，不能伪造成功或新的终态。
- 兼容入口成功状态、响应索引和工具引用在同一事务提交。失去租约会触发取消，但不承诺撤销已发生的外部操作。
- 执行者增强前两个测试文件 58 项通过；稳定提交受影响组 31 项通过、另一个写锁用例 1 项通过。二者不是声称最终完整两个文件已重跑。
- 独立审核在稳定提交亲跑 25 项通过，39 项未选中；其中修复前的三项确定性失败全部转绿。见 [独立报告](capabilities-sources-20261114/lease-independent-review.md) 和 [独立日志](capabilities-sources-20261114/lease-independent-tests.txt)。

合并基线 `5eed11a` 的第二轮后端完整回归在第 172 个用例 `test_concurrent_submissions_do_not_block_or_queue_forever` 出现失败点，L2 为及时诊断于 16:33 中止自己的测试进程；中止时 pytest 的 tmp_path 清理另报 KeyError，未取得该用例原始异常栈。因此此轮记为中止，不能把点阵计数当作完整测试结果或由清理异常推断产品根因。后续独立定向复核见下文，未调大阈值或修改实现。保留 [第二轮中止日志](capabilities-sources-20261114/merged-server-interrupted.txt)。

同一基线客户端重新执行：25 文件、168 项测试通过，TypeScript/Vite 构建通过；构建仍提示单个代码包 minification 后大于 500 kB（非 gzip 大小）。该租约加固不等于客户规模并发、多机时钟偏差或真实上游故障验收。

第二轮失败的定向诊断：[独立报告](capabilities-sources-20261114/event-loop-independent-review.md) 和 [原始日志](capabilities-sources-20261114/event-loop-independent-tests.txt)。同一提交、同样阈值、同样 FULL 持久化，整个事件循环文件 9 项通过，64.45 秒。原失败未复现、根因未定，不把定向转绿写成“已修复”或认定纯磁盘波动。再次完整回归加入仓库外只记录结果的 pytest observer，发生失败时即时保存原始异常，避免中断后丢失诊断证据；不更改产品、测试断言或跳过项。

## 最终主线完整回归

学校云代码基线 `5eed11a199d09e063292fbde1005b2f6a4b0c910`，2026-11-14 16:36 至 17:04 单进程执行完整 `server/tests`：**1656 passed、51 skipped、0 failed、3 warnings，1714.68 秒，退出码 0**。完整 [后端日志](capabilities-sources-20261114/merged-server-final.txt) 包括源码提交、起止时间、全部跳过原因和退出码；[客户端日志](capabilities-sources-20261114/merged-client-tests.txt) 与 [构建日志](capabilities-sources-20261114/merged-client-build.txt) 同样绑定此代码提交。

复现命令（observer 只保留异常，不修改测试行为）：

```bash
PYTHONPATH=/media/data3/dengkw/tmp/oc-test-observer:server \
OC_TEST_REPORT_DIR=/media/data3/dengkw/tmp/oc-combined-observed-20261114/reports \
TMPDIR=/media/data3/dengkw/tmp/oc-combined-observed-20261114/tmp \
XDG_CACHE_HOME=/media/data3/dengkw/tmp/oc-combined-observed-20261114/cache \
/media/data3/dengkw/projects/open-claude-code/.venv/bin/python -m pytest \
server/tests -q -ra -x -p oc_test_observer \
--basetemp=/media/data3/dengkw/tmp/oc-combined-observed-20261114/pytest
```

观察插件副本保存在 [oc_test_observer.py](capabilities-sources-20261114/oc_test_observer.py)。再次执行应使用新的临时目录，避免覆盖本轮证据。

本次完整运行没有失败，但不能抹去第二轮的未复现并发失败；该事件保留为间歇性风险，不能宣传“任何负载下均稳定”。51 项跳过涉及固定 Harness/Node 24、上游依赖、可选官方 HippoRAG 运行环境以及 torch 缺失，仍待补齐，不算通过。3 项警告为 Starlette 测试客户端弃用提示和已下线工作流的两个重复 OpenAPI 操作标识。

阶段结论：WEB-001/002 的来源修复、CAP-EXT-001/002/003 的 search/fetch 实现切片，已具备学校云代码、浏览器、独立审核和主线回归证据。OCR/图片搜索、精细治理、真实供应商效果、客户数据和常驻服务发布均不升级为完成；整个第二、第三阶段仍为部分实现。

未更新任何常驻学校云或腾讯云服务，不能把代码合并称为线上已生效。

## 看板记录限制

已通过 boardctl 新增节点、验收项和进展，并更新本项目颜色分组表。boardctl 要求 running 才能 submit，但既有规范要求真实 Claude Code ST 才能 running；本轮仅有可核实的 collaboration 身份。故未伪造 start/ST，节点暂为 ready，独立结论保存在报告和验收项；需要 L1 明确此类会话的合法看板映射，项目未修改共享看板代码或状态机。

独立审阅者先将两节点 IR1 标为 passed，并保守将其他验收项记为 needs_human 等待集成证据。最终完整日志形成后，独立核验归档日志摘要、产品源码未变、逐用例记录及浏览器图片，已将 T22-C2/C3、T23-C1/C2 标为 passed。来源组 97 项和配置组 78 项属于 L2 完整回归中的实际用例，不冒称审核者另跑相同命令；见 [最终核验记录](capabilities-sources-20261114/final-acceptance-review.json)。

T22-C1 仍为 needs_human：L2 首次登记时误用了两个不存在的测试文件名，已补 C3 为真实文件名，保留错误记录，不伪报原命令通过。现有 boardctl 只支持新增与设验收结果，不能修改原命令，需 L1 提供合法更正/废止途径。两节点仍为 ready，身份映射与该错误验收项更正前不伪造 start/submit/done。代码与证据已推 GitHub，不代表服务发布。
