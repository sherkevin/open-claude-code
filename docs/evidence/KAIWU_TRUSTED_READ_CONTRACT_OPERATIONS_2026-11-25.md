# 可信 MCP 只读规则：接入与生命周期运维

业务用户仍只选择会话的审批模式。新增供应方必须有可信的接口契约和授权范围；不根据工具名称、一次测试成功、远端注解或模型判断自动登记。

复用生产 e7bfcd8 的 trusted_mcp_read.py、原有 ACL、工具定义摘要及审批执行器，新增运维脚本 scripts/kaiwu_read_contract.py，不新增运行时权限路径或第三方依赖。适用于 Linux 服务账号下的现有 SQLite 工作台；不要把此脚本作为模型工具或公开 API 暴露。

## 新接入与变更处理

1. 正常导入 MCP、测试连接、绑定允许使用的助手。使用已经审核过的供应方接口说明与授权依据核对具体定义。
2. 运营人员读取连接的当前身份摘要、定义摘要、规则版本和有效期：
   PYTHONPATH=server python scripts/kaiwu_read_contract.py --data-dir /absolute/private/data inspect --connection-id CONNECTION_ID
   仅输出安全元数据，不解密或输出凭据、业务正文。状态区分未登记、已登记、身份变化、过期、人工覆盖、连接不可用；逐工具说明当前自动审批、未登记、定义变化及不合格，另报已登记但消失的工具数。
3. 在服务账号所有、0600 权限的独立审核清单中，填写 version=1、connection_id、expected_identity（来自 inspect 的 connection_identity）、expected_policy_sha256、policy_id、review_reference（实际评审或授权工单编号）、expires_at、authorization_expires_at、tools（明确审核过的工具名称到实际定义 SHA-256 的映射）。两个时间必须带时区；截止日期不得超过经供应方/控制面确认的实际授权截止日期。清单的上限时间来自运营人员已核实的外部授权证据，本命令不会自动向供应方核验该日期，不能用它给自己授权。不得从 inspect 输出自动选择所有 eligible_for_review=true 的工具。
4. 先执行只验证的 dry run：
   PYTHONPATH=server python scripts/kaiwu_read_contract.py --data-dir /absolute/private/data apply --manifest /absolute/private/review.json
   核对审核内容后才加 --commit。若身份、定义、当前规则摘要或人工覆盖状态变化，停止并重新检查，不盲目重放。dry run 的拟写入摘要包含当次时间，实际提交摘要以提交回执为准。
5. 提交后再次 inspect。通过现有会话真实只读业务验收，记录自动审批事件及人工审批数量。绝不在客户业务系统上执行写入来测试拦截。
6. 授权更新、重导入、凭据变化或者工具定义变化，重新核对当前身份、已审核契约及授权有效期，复用已有审核结果编制清单。不得自动复制或续期旧规则。新登记不放宽正在运行任务的授权；重复提交完全相同的清单不会改变登记时间。
7. 撤销平台默认登记：
   PYTHONPATH=server python scripts/kaiwu_read_contract.py --data-dir /absolute/private/data revoke --connection-id CONNECTION_ID --expected-policy-sha256 CURRENT_SHA
   dry run 后按授权加 --commit。该命令不撤销独立的人工核验或业务权限；需完全禁用自动审批时还应检查独立人工配置。供应方撤销优先由原有授权机制执行。

规则修改使用服务账号私有文件、进程间文件锁、摘要比较交换及原子替换；其他连接条目保留。记录提交回执中的前后摘要、审批依据、工具清单和期限，保存到运维审计记录，不在业务对话展示。所有运营写入应走本工具；不要并发手改规则文件。inspect 不修改规则或业务数据库，apply/revoke 默认不修改规则（可创建私有锁文件）。线上业务服务不需重启即可读取新规则。

## 验证

22 项本次后端回归通过：18 项新增运维边界测试及 4 项原有真实合成本地 MCP 自动审批测试。包含重复登记、隔离其他连接、旧摘要并发冲突、身份/凭据/定义变化、人工覆盖、超期、不安全工具、文件权限、撤销和非敏感输出。本次没有新的真实业务写入、没有自动扩展任何客户授权。
