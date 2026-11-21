# Server worker：只完成 腾讯云控制面/Edge诊断后端

先读同目录 2026-11-19-huawi-diagnostics-parallel-contract.md，它冻结跨端API和所有权。历史run87已停，另一个工程师只写connector/，你独占约定server文件。
工作目录必须显式设为 /media/data3/dengkw/projects/open-claude-code-huawi-enterprise-20261118；当前基线6e44dbb，未提交Go变化是另一工程师工作，不碰、不回退、不提交。
目标：把客户Windows上报的安全运行事件存到现有控制面SQLite，提供受认证查询和可打开的轻量运维页，并在真实MCP Edge产生平台日志/trace。
直接实现新增 kaiwu_diagnostics.py、db.py表/索引、kaiwu_control.py设备签名upload/scoped-read/admin-read与/ops页面、kaiwu_edge.py trace/结果日志，必要时main.py最小挂载。不要改React主站，不新增大型collector、服务或端口。
完全遵循契约：固定Event字段与catalog；无自由message/details，无URL/业务正文/args/result/headers/凭据；client身份推tenant/connector；source属于设备；tool必须该source批准。
复用现有device/admin依赖与DeviceToken+Ed25519鉴权。签名不包括query，scope只能从认证与路径得出。100条/256KiB上限、幂等event_id、校验全部通过再事务写入后ACK；14天/perconnector10万/global100万上限，分页/filter/index。
Edge每次随机kwtr_32hex并覆盖外部X-Kaiwu-Trace-ID，向authproxy带同头，返回响应头；平台事件固定成功/失败、状态码、耗时、批准tool/source；HTTP200 JSONRPC error/isError不能算业务成功；SSE保持流式，能确定的是传输完成就不要伪称工具成功。Edge日志写失败不影响业务。
/ops是只读轻量运维页：无认证只显示空登录壳；password输入既有admin token，在闭包内存发Authorization头；不入URL/Cookie/storage/HTML默认值；刷新清空；无第三方脚本；CSP/no-store/referrer保护；纯textContent表格支持connector/source/level/trace/time筛选、近实时刷新、下载当前脱敏结果。
客户本机只通过设备scoped接口看到自己的平台日志，管理员token绝不能发到Windows连接器。
写有意义Python测试：上传正反、跨tenant/device、admin隔离、非法/超限字段、幂等、保留策略、上游报错canary不外泄、Edge/authproxy trace协议、401/超时/JSONRPC失败分类、ops认证壳/XSS/no-store。
只在临时隔离DB/测试server运行，不实际部署腾讯云，不读取真实secret，不动Shervin。不构建Windows包，不commit。
每个小阶段把checkpoint写 docs/evidence/huawi-diagnostics-20261119/SERVER_CHECKPOINT.md，<=60行；完成写 SERVER_CODE_READY.json，包含changed_files、测试证据、迁移与回滚说明、未测项。
上下文压缩后只读自己的checkpoint和本契约，继续写代码，不反复读取所有历史/1.3.6验收。
现在直接检查相关现有route/DB结构并实现最小完整链路。若已有公共依赖够用不要增新依赖，缓存/TMPDIR/pytest输出仅data3。
