# TokenHub 单一路由门禁证据（2026-11-13）

**服务器：** 学校云
**项目：** open-claude-code（知行工作台）
**结论范围：** 代码路径与合成测试门禁；未调用真实模型、未读取凭据或客户数据；本轮仅通过现有 SSH 别名对腾讯云既有预览做只读健康检查，未部署、未重启。

## 目标

用户已明确：知行工作台接入的模型全部经过腾讯云 TokenHub。聊天、Fitness 自动路由、Tab 补全、知识视觉/实体/关系角色和后续图片角色不能直连厂商公网模型；缺少真实 TokenHub 授权时必须保持不可用，不能用其他线路冒充。

## 实现

- `server/app/provider_policy.py` 只把 `https://tokenhub.tencentmaas.com` 归为生产 `tokenhub` 路由；loopback 和 RFC 保留域名分别标为 `local_proxy`、`synthetic`，不具备生产就绪属性。
- `Settings.provider_test_mode` 对应服务端变量 `AGENT_PROVIDER_TEST_MODE`，默认关闭。只有学校云隔离合成测试进程可以显式开启它；腾讯云候选和客户试用必须保持未设置或 `false`。
- `Service.effective_provider` 缺少地址时只补 TokenHub canonical 地址，不回退到 Anthropic 或其他厂商默认端点；`validated_provider` 在运行 admission 前拒绝未授权外部地址。
- `model_router`、Tab 补全、工作流和 Harness 辅助路径在创建 HTTP 客户端前都执行路由门禁；拒绝时不会构造请求、发送摘要/草稿或发送凭据。
- `SDKRuntime.connect` 和 `HarnessRuntime.connect` 也在传输层再次校验路由；即使未来出现配置注入，也不会在 TokenHub 之外启动模型客户端。Harness 同时接受 `ANTHROPIC_API_KEY` 和 `ANTHROPIC_AUTH_TOKEN`。
- 模型目录对显式非 TokenHub 外部地址标为不可用，即使该目录项暂时没有凭据；路由预检在生产模式要求 `tokenhub_route=true` 才能 `ready`。
- `/health/ready` 只有在凭据存在且路由为生产 TokenHub 时才返回 `model_configured=true`；loopback/synthetic 仅用于合成测试，不能伪装生产就绪。
- 知识视觉索引和 OpenAI-compatible 视觉适配器复用同一类地址校验；视觉模型仍需独立目录能力、开关和授权，不会把通用聊天模型冒充视觉模型。

## 合成验证

以下测试均在学校云本地运行，不访问真实 TokenHub：

```text
server/tests/test_provider_policy.py                         10 passed
server/tests/test_model_recommendation.py                    32 passed
server/tests/test_model_catalog.py + capabilities + config
server/tests/test_openai_visual.py                           合并套件共 95 passed
完整服务端回归（当前工作树）                               1537 passed, 17 skipped, 1 warning
路由/运行时加固定向回归                                   124 passed, 8 skipped, 1 warning
```

专项断言包括：

1. canonical TokenHub、loopback、synthetic、真实外部地址及带查询串地址分类正确；
2. 带凭据的真实非 TokenHub 地址在运行前返回 `TOKENHUB_ROUTE_REQUIRED`；
3. 无凭据但显式写入真实外部地址的模型目录项显示不可用；
4. 生产模式下 loopback/synthetic 预检不是 `ready`；
5. Fitness 和 Tab 遇到真实非 TokenHub 地址时不会创建 HTTP 客户端，`provider_attempted=false`；
6. 视觉适配器遇到真实非 TokenHub 地址时在网络请求前拒绝。
7. 健康检查不会把 synthetic provider 报告为 `model_configured=true`；SDK 与 Harness 传输层的二次路由门禁通过定向回归。

## 未证明事项

- 尚未使用真实授权的 1007、1017、视觉或图片 TokenHub 线路，因此没有真实延迟、限流、账单、语义质量或并发证据。
- loopback 适配器只在合成测试模式允许；代码不把它当作已证明的 TokenHub 上游。
- 腾讯云已有历史预览入口，但当前学校云工作树尚未冻结、同步或在该入口完成一致性验收；本证据不构成发布批准，也不把历史入口当作当前版本地址。
- 完整回归的 17 个 skip 仍代表受控环境/真实 Harness 等外部证据缺口，不能按通过处理。
