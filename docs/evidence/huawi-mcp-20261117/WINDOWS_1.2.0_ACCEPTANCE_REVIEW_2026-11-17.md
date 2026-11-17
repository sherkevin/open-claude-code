# Windows 连接器 1.2.0 本地验收复核

- 复核日期：2026-11-17
- 复核对象：`docs/evidence/huawi-mcp-20261117/windows-1.2.0/`
- Windows 安装包：`kaiwu-connector-1.2.0-windows-amd64.zip`
- 安装包 SHA-256：`0d9b7abc3d700390fc52476b0b896b7439e9fc6630b060ff30fb0838202c51d6`
- 证据包 SHA-256：`ad471c6045c887b0ef3e5a169c8198ff6224af0fbe60f1d5912a48169342a528`
- 交接实现提交：`b26e131d0ee7fe0d5daf85c939a78839d97cb52`

## 1. 复核结论

本地 Windows 验收报告的 A 段和 B 段结论成立，但适用范围必须严格限定：

1. A 段通过：安装、Windows 服务、回环管理界面、服务重启、卸载和目录清理均有证据。
2. B 段通过：模拟 HTTP MCP 的导入、协议握手、工具发现、工具试调用、错误凭据拒绝、恢复和干净服务重启均有证据。
3. C 段阻塞：没有真实接入码、云端控制面、relay、visitor 和 Edge，不能从开物平台调用该 Windows 连接器。
4. 本次测试机是 Windows 10 Home China，不是客户目标 Windows Server 2019 Datacenter，因此不能替代 HWC-017、G5 和 G7 的客户目标环境验收。
5. 本次没有执行整机重启，只执行了 `Restart-Service`。
6. 测试时 Windows Defender 实时保护为关闭状态；虽然没有修改该状态，但不能证明客户环境默认开启安全软件时的安装行为。

## 2. 证据核验

1. 顶层证据压缩包 SHA-256 与报告一致。
2. 证据压缩包内未包含 `secrets.json`、`identity.json`、`device_key.json`、`auth_profiles.json` 或 `config.json`。
3. 安装包 SHA-256 与报告和既有证据目录中的安装包一致。
4. 证据压缩包包含 9 张页面截图、安装/服务/监听/防火墙/ACL/诊断/错误凭据/重启/卸载原始输出和缺陷说明。

## 3. D01：重复导入清空工具允许列表

该问题已由现场步骤和源码路径共同确认，属于真实缺陷，而不是测试误判。

修复前行为：

1. 导入同一 MCP 地址并配置动态认证。
2. 保存本机批准的工具允许列表。
3. 重新导入同一 `mcpServers` 配置。
4. 新导入的 `Source` 没有允许列表，但旧认证档案仍存在。
5. 页面显示“认证已配置”，实际 `allowed_tools` 为空，工具试调用被拒绝。

修复内容：

1. 相同上游地址和传输类型继续使用稳定 `source_id`。
2. 重复导入时，如果新导入没有显式允许列表，则按稳定 `source_id` 保留原管理员批准的工具允许列表。
3. 如果来源最终没有任何批准工具，同时删除不可用的动态认证档案，避免出现“已配置但无允许工具”的矛盾状态。
4. 代码位置：`connector/internal/agent/agent.go` 的 `ImportConfig` 和 `assignSourceIDs`。

回归测试：

1. `TestReimportPreservesApprovedToolAllowlist`
2. `TestCorrectAuthorizationSurvivesReimportAndRestart`

测试验证同一上游地址重新导入后来源编号不变化、工具允许列表不丢失、认证状态一致，并且服务重建后仍保留。

## 4. D02：第一次重启后出现一次 401

现场记录显示，错误凭据恢复正确凭据后第一次重启出现一次 401，重新导入正确配置后恢复，第二次干净重启通过。

当前判断：

1. 现有证据不足以确认这是连接器产品的确定性缺陷。
2. 测试步骤可能没有等待正确凭据持久化完成，或页面恢复时提交了旧凭据，但缺少页面请求体和 `secrets.json` 写入时序证据。
3. 已新增 `TestCorrectAuthorizationSurvivesReimportAndRestart`，验证“正确凭据 → 错误凭据 → 正确凭据 → 重启”后持久化的 Authorization 必须是最后一次正确值。
4. 仍需在 Windows 实机使用人工浏览器重测该序列，并保存页面请求结果、状态 JSON 和重启后诊断 JSON。

## 5. 交付判断

本轮修复后，`1.2.0` 现场包不能继续作为最新候选包使用，应重新构建 `1.2.1` Windows 安装包并在 Windows 环境重测。

在重新构建和重测完成前，当前状态是：

| 项目 | 状态 |
| --- | --- |
| Windows 本机安装和模拟 MCP 诊断 | 1.2.0 Windows 10 环境通过，目标 Windows Server 2019 未验证 |
| D01 工具允许列表丢失 | 已修复，自动化回归通过，待 Windows 实机复测 |
| D02 重启后单次 401 | 未确认根因，已增加持久化回归测试，待人工浏览器复测 |
| Windows Server 2019 | 未验证 |
| 整机重启和安全软件默认开启 | 未验证 |
| 云端控制面、relay、visitor、Edge | 阻塞，未联调 |
| 正式客户交付 | 未达到 |

## 6. 下一步验收

1. 构建 `1.2.1` Windows 安装包并核对包内 `SHA256SUMS`。
2. 在 Windows 实机重新执行安装、导入、动态认证、重复导入、错误凭据恢复、服务重启和卸载。
3. 增加一次整机重启，确认 `KaiwuConnector` 自动启动。
4. 保持 Defender 和防火墙默认开启，记录安装和运行结果。
5. 在 Windows Server 2019 Datacenter 候选机执行同一验收。
6. C 段资源具备后，再执行接入码兑换、来源注册、relay/visitor、Edge 和平台调用。
