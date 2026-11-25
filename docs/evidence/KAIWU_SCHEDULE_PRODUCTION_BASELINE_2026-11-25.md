# 2026-11-25｜调度整包生产基线对账与隔离覆盖验收

## 范围和实际结论

基线 d392c2c（运行时代码 7f19631）。继续使用原受控工作台发布流程，不改变连接器控制面，不升级官方 Harness，不替换生产目录。通过既有授权 SSH 入口实际只读核对工作台与控制面的 systemd 状态、工作目录及文件 SHA-256；工作台仍为 320e9d7，两个服务均 active。未获取客户数据或执行业务调用。

- 既有 320e9d7 发布归档的 42 个文件 SHA-256 与历史 manifest 全部一致。
- 当前实际运行目录抽查 16 个指定路径（13 个存在、3 个新模块尚不存在）。其中 db.py、main.py、service.py 与 Git 同提交存在差异，但与历史生产归档逐字节一致；实际现有 Harness/权限模块和 pyproject.toml 与 Git 基线一致；index.html 与历史生产归档一致。3 个新模块均确认不存在。不能直接按 Git 主线覆盖整个生产目录。
- 在 data3 独立工作树 kaiwu-schedule-production-baseline-20261125 保留生产原 main.py，分别对 db.py、service.py 做实际生产旧版/Git 旧版/Git 新版三方合并，无冲突。既有未部署的主线安全模块与生产差异保留为该候选的明确范围约束，不据此修改或禁用生产安全配置；这不是完整生产镜像。
- 该隔离覆盖版本实际通过反馈、自然语言创建/修改/读取以及三项真实浏览器审批测试共 34/34（108.05 秒）。固定版 Harness，回环合成模型，使用新建数据目录；不是客户模型或生产运行验收。

## 生产发布闸门

进一步读取当前活动任务/队列、数据库完整性、发布元数据和存储条件的只读工具调用被安全检查拦截，提示 "This tool call was blocked by OpenAI's safety checks. Please double check what you are sending." 未重试或采用替代路径绕过。此次未核对当下数据库静止状态、未执行生产备份、未停止或切换服务、未恢复任何任务。不能以历史空闲记录或历史备份替代本次验证。

本次已解决实际运行文件与 Git 基线不一致的问题，但完整发布仍未获准进入执行。下次先检查是否有已授权、符合工具限制的正式发布核验证据；缺少新鲜活动任务/备份/回退证据时保持 320e9d7。继续独立的 A3 逐消息反馈入口和通知，不阻塞其他不涉及生产切换的开发。

证据目录：/media/data3/dengkw/acceptance/kaiwu-schedule-production-baseline-20261125/；baseline-audit.json、production-readonly.log、live-file-comparison.json、pytest-overlay.log。独立覆盖工作树保留未提交生产差异，不推入产品主线，不上传生产配置或客户数据。
