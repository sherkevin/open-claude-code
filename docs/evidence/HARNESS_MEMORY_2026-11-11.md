# Harness 受控记忆与推理代次验收

日期：2026-11-11。状态：B2 在开发代码、本地合成模型和完整服务端回归范围内通过；尚未启用共享服务，也不能据此宣称整个产品交付完成。

## 1. 范围和边界

本批实现并验证 Harness 会话记忆的不可变快照、版本 CAS、原生推理代次、附件收据按代次隔离、管理页面编辑/删除/清空、模型 `memory_list`/`memory_read`/`memory_write`、完整正文人工审批、成功后提交、失败/取消回滚以及历史单次迁移。

没有读取真实客户数据，没有调用真实供应商付费模型，没有部署生产、重启共享服务、切换默认检索或修改官方上游源码。测试使用固定的官方提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159`、本地合成 Messages 服务、私有临时数据库和宿主生成文件。临时目录、浏览器资料、缓存及日志均位于 `/media/data3/dengkw`。

模型写入只有在所有者查看完整正文并明确批准后才进入 `approved_pending_commit`；本轮成功、最终附件/授权复核通过且所属 Node 进程确认退出后才提交。批准界面/审批正文不会进入普通 SSE 日志。删除、清空和内容变化会建立新原生代次，旧应用聊天与审计保留，但不自动重放为新一代模型上下文。

## 2. 完整回归

最终回归目录：

`/media/data3/dengkw/projects/open-claude-code/.runtime/verification/harness-memory-full-20261111T140516Z/`

- 收集 65 个测试文件、1,378 个唯一用例。
- 三片分别通过 `460`、`459`、`459` 项，零失败、错误或跳过；耗时分别约 `1108.3`、`1188.4`、`1630.8` 秒。
- `verification-summary.json` 记录 `source_snapshot_stable=true`、`changed_inputs=[]`，即回归运行期间源码输入未变化。
- 测试使用真实固定官方 Node、本地回环合成模型和 Python 服务；没有外部模型调用。真实供应商记忆质量、真实客户数据和共享服务切换仍为未验收项。
- 保留现有 Starlette 测试客户端弃用警告和 JUnit 自定义属性警告，没有为消除警告升级依赖。

## 3. 首轮失败及修正

首次冻结回归：

`/media/data3/dengkw/projects/open-claude-code/.runtime/verification/harness-memory-full-20261111T120719Z/`

该轮发现 8 项失败，均已定位并修正：

- `test_full_content_requires_owner_live_pending_approval`：测试夹具没有进入真实待批准状态。
- `test_real_config_fingerprint_detects_knowledge_revision`：旧运行时夹具缺少执行器快照。
- `test_session_memory_revision_fingerprint_context_and_context_busy_are_isolated`、`test_legacy_snapshot_migration_is_once_per_existing_session_and_new_sessions_start_empty`、`test_invalid_legacy_memory_preserves_original_and_retries_only_that_session`：旧记忆迁移来源和 Harness 存储分支需要兼容真实历史。
- `test_new_default_is_harness_and_explicit_rollback_only_affects_new_sessions`、两项 `test_harness_native_history_keeps_model_switch_guards`：旧接口兼容与默认迁移夹具未覆盖新代次语义。
- 另有大文件迁移错误分类问题：容量超限应返回 `413 MEMORY_TOO_LARGE`，真实摘要/结构损坏仍返回 `409 MEMORY_INTEGRITY`。

修正没有放宽权限、没有恢复随机 `operation_id`、没有把失败或取消伪报成已提交，也没有覆盖旧快照回退。受影响测试文件和定向重测均通过，随后才执行上述最终完整回归。

## 4. 合同结果

- 管理写入必须提供稳定 `operation_id`；缺失返回 `422 MEMORY_OPERATION_REQUIRED`。相同键/相同请求幂等，相同键/不同正文拒绝。
- 大文件超限返回 `413 MEMORY_TOO_LARGE`；真实完整性失败返回 `409 MEMORY_INTEGRITY`。
- 模型写申请不能借助手自动批准或模型代审绕过；必须由会话所有者查看完整正文后人工允许。拒绝、取消、超时、撤权、进程退出未确认或提交前校验变化均不发布。
- 成功提交与快照指针、revision、native epoch、resume ID 清空和上下文失效处于同一 SQLite 提交；文件树先写私有临时目录并同步，不能以半写目录作为有效快照。
- 历史迁移只导入当前会话的旧目录一次，保留来源并拒绝篡改；清空或内容变化后的新代不继承旧原生历史、旧附件或旧工具结果。
- 审批等待、断线恢复和取消不会留下可被下一轮误用的待提交写入；已提交写入被取消时返回“已完成，未撤回”，不伪装回滚。

## 5. 未验收边界和下一步

可在无客户数据条件下继续验证的合同已在本批完成。以下仍缺真实资源或独立验收，不能算完全实现：

- 真实供应商在长对话中的记忆读取质量、模型是否实际遵守当前快照，以及真实 token/费用；
- 客户真实业务资料、跨团队权限策略和客户验收样例；
- 共享服务采用本批代码、生产部署、重启和容量/长稳测试；
- C 批已另行完成手动压缩开发迁移，证据见[手动压缩验收](HARNESS_COMPACTION_2026-11-11.md)；自动整理、完整用量账本与真实供应商压缩后任务质量仍按 [主线计划](../plans/2026-11-11-harness-mainline.md) 继续。
